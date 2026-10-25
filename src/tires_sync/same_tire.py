"""One tire, one ad: the supplier may list the same tire under several codes (another lot, another country of origin). Avito takes the second
ad for a repeat of the first (it compares the product and the photos, not the text) and blocks it.

Among the positions of one account the tires with the same identity are published once: the code that is already published, otherwise the one
with the most stock. The ad carries the Id of the oldest ad of that tire in the cabinet, so Avito edits it instead of seeing a new one. A lot that
has left the supplier's unload (or is held on its saved ad) is recognised by that saved ad."""
from __future__ import annotations

import dataclasses
import xml.etree.ElementTree as ET
from typing import Callable, Dict, Iterable, Optional, Tuple

from .candidates import Candidate, Evaluation
from .feed import _tire_type


def _key(brand, model, width, height, diameter, tire_type, load, speed, runflat) -> Optional[Tuple]:
    load, speed = str(load or "").split("/")[0].strip(), str(speed or "").strip().upper()
    if not (brand and model and load and speed):
        return None                                                    # not enough to say two ads are the same tire
    return (str(brand).strip().upper(), str(model).upper().replace(" ", ""), str(width), str(height), str(diameter), tire_type, load, speed, bool(runflat))


def identity(c: Candidate) -> Optional[Tuple]:
    o = c.offer
    return _key(c.avito_brand, c.avito_model, o.width, o.height, o.diameter, _tire_type(o.season, bool(o.studded)), o.load, o.speed, o.runflat)


def identity_of_ad(text: str) -> Optional[Tuple]:
    """The identity of an ad we published (its saved XML)."""
    try:
        ad = ET.fromstring(text)
    except ET.ParseError:
        return None
    g = lambda tag: (ad.findtext(tag) or "").strip()
    return _key(g("Brand"), g("Model"), g("TireSectionWidth"), g("TireAspectRatio"), g("RimDiameter"), g("TireType"), g("LoadIndex"), g("SpeedIndex"),
                g("RunFlat") == "Да")


def dedupe(ev: Evaluation, published: Iterable[str], min_stock: int, live_id_of: Optional[Callable[[str], str]] = None,
           first_seen_of: Optional[Callable[[str], Optional[str]]] = None, snapshot_of: Optional[Callable[[str], Optional[str]]] = None,
           retired: Optional[Iterable[Tuple]] = None, foreign_ids: Iterable[str] = (), cross=None) -> Dict[str, str]:
    """Publish each tire once. Duplicates among the candidates, and published codes that are not candidates now but are the same tire (gone from the
    unload, held on their saved ad), are taken off (reason `same_tire`). Returns {kept code: Id of the oldest ad of the tire} when that is not its own.

    `cross` (tire_match.MatchResult, only with the switch `tire_match.feed` on; None: nothing of this): one tire at two suppliers is one tire
    here too, whatever Avito models the two sides are mapped to. Its ads carry the Avito brand and model of the main side (WebMIM first),
    the keys of the other sides (of their candidates, saved ads and retired ads) count as the main one, and a new tire takes WebMIM first.
    An ad goes over to another supplier at most once a day (`cross.wait_until`): before that the side that would take it waits (hidden as
    `same_tire`, no Id of its own either) and the group goes on as if it were not there; a change made is noted in `cross.switches`. The
    supplier behind an ad is the code that has its Id now (published), else the one last out under it; within one supplier nothing of this."""
    published = set(published)
    foreign_ids = set(foreign_ids)
    retired = list(retired or ())
    canon: Dict[Tuple, Tuple] = _cross_canon(ev, cross, published, snapshot_of, retired) if cross is not None else {}
    groups: Dict[Tuple, list] = {}
    for gid, cand in ev.ok.items():
        key = identity(cand)
        if key is not None:
            groups.setdefault(canon.get(key, key), []).append(gid)
    if snapshot_of:
        for gid in published - set(ev.ok):                             # published, not a candidate now: its saved ad tells which tire it is
            text = snapshot_of(gid)
            key = identity_of_ad(text) if text else None
            key = canon.get(key, key) if key is not None else None
            if key is not None and key in groups:
                groups[key].append(gid)
    if canon:
        retired = [(goods_id, ad_id, canon.get(tuple(key), key), removed_at) for goods_id, ad_id, key, removed_at in retired]
    tire_of = {g: key for key, members in groups.items() for g in members}
    holder: Dict[str, str] = {}                                        # Id -> the code whose ad has it (or gets it in this step)
    owner_of: Dict[str, str] = {}                                      # Id -> the published code that has it now
    last: Dict[str, Tuple[str, str]] = {}                              # Id -> (removed_at, code) of the last one out under it
    waits: set = set()
    if live_id_of:                                                     # a lot whose own Id the ad of another tire has (a lot that took it while
        for gid in sorted(published):                                  # this one was down, and this one came back as another tire): two ads under
            holder.setdefault(live_id_of(gid), gid)                    # one Id would stop the feed; it waits, hidden like a duplicate, no new Id.
        owner_of = dict(holder)                                        # The same tire is the groups' business below (one ad, the oldest Id)
        for goods_id, ad_id, _key, removed_at in retired:
            if ad_id not in last or removed_at > last[ad_id][0]:
                last[ad_id] = (removed_at, goods_id)
        claims: Dict[str, list] = {}
        for gid in sorted(ev.ok):
            if gid not in published and ev.ok[gid].offer.stock >= min_stock:
                claims.setdefault(live_id_of(gid), []).append(gid)
        for ad_id, codes in claims.items():                            # unpublished lots after one Id: the one last out under it (its tire was
            if ad_id not in holder:                                    # on the ad), else the first by code
                before = last.get(ad_id, ("", None))[1]
                holder[ad_id] = before if before in codes else codes[0]
        waits = {g for codes in claims.values() for g in codes if _apart(g, holder, live_id_of, tire_of)}
    inherit: Dict[str, str] = {}
    kept_of: Dict[Tuple, str] = {}

    def oldest_of(live):                                               # the lot whose Id is the oldest ad of the tire (dates, else the published one)
        ids = {g: live_id_of(g) for g in live}
        dated = sorted(((first_seen_of(i) if first_seen_of else None) or "~", g) for g, i in ids.items() if i)
        oldest = next((g for d, g in dated if d != "~"), None)
        if oldest is None:                                             # no dates known: the published lot is the one the cabinet has
            oldest = next((g for g in sorted(live) if g in published), None)
        return oldest, ids
    latest_cut: Dict[Tuple, Tuple[str, str, str]] = {}                # the merge: key -> the latest retired ad (as the retired step below)
    live_ids_now: set = set()
    if cross is not None and live_id_of:
        live_ids_now = {live_id_of(g) for g in published}
        for goods_id, ad_id, key, removed_at in retired:
            if key not in latest_cut or removed_at > latest_cut[key][2]:
                latest_cut[key] = (goods_id, ad_id, removed_at)

    def would_take(g, key, members) -> Tuple[str, Optional[str]]:     # the merge: (the Id `g` would come out under, the code behind it now)
        live = [m for m in members if m in published]
        if live:
            oldest, ids = oldest_of(live + [g])
            ad_id = ids[oldest] if oldest and oldest != g and ids.get(oldest) else live_id_of(g)
        else:
            ad_id = live_id_of(g)
            hit = latest_cut.get(key)
            if (hit and hit[0] != g and hit[1] not in live_ids_now and hit[1] not in foreign_ids
                    and not (hit[0] in ev.ok and ev.ok[hit[0]].offer.stock >= min_stock)):
                ad_id = hit[1]
        return ad_id, owner_of.get(ad_id) or last.get(ad_id, ("", None))[1]
    for key, members in groups.items():
        eligible = [g for g in members if g in ev.ok and ev.ok[g].offer.stock >= min_stock and g not in waits]   # another lot of the tire goes instead
        if cross is not None and live_id_of:
            eligible = [g for g in eligible if not _waits_a_day(ev, cross, g, published, *would_take(g, key, members))]
        if eligible:
            if cross is None:
                kept_of[key] = sorted(eligible, key=lambda g: (g not in published, -ev.ok[g].offer.stock, g))[0]
            else:                                                      # the owner 2026-10-20: a live published ad stays, a new tire takes WebMIM
                kept_of[key] = sorted(eligible, key=lambda g: (g not in published, cross.rank_of(g), -ev.ok[g].offer.stock, g))[0]
        if len(members) < 2 or not eligible:
            continue
        kept = kept_of[key]
        for gid in members:
            if gid == kept:
                continue
            if gid in ev.ok and gid not in eligible:
                continue                                               # a lot below the threshold is judged by the stock rule, not hidden by this one
            ev.ok.pop(gid, None)
            ev.problems.pop(gid, None)
            ev.excluded[gid] = "same_tire"
        if live_id_of:
            oldest, ids = oldest_of([g for g in members if g in published or g == kept])
            if oldest and oldest != kept and ids.get(oldest):
                inherit[kept] = ids[oldest]
    if retired:                                                        # a new lot of a tire whose ad was taken off the feed lately: that ad is the first one
        live_ids = {live_id_of(g) for g in published} if live_id_of else set()
        latest: Dict[Tuple, Tuple[str, str, str]] = {}
        for goods_id, ad_id, key, removed_at in retired:
            if key not in latest or removed_at > latest[key][2]:
                latest[key] = (goods_id, ad_id, removed_at)
        taken = set(inherit.values()) | set(foreign_ids)               # an Id another account has now is never handed over
        for key, kept in kept_of.items():
            if kept in published or kept in inherit or key not in latest:
                continue                                               # a published ad keeps its own Id; an inherited one is already decided
            goods_id, ad_id, _ = latest[key]
            if goods_id == kept or ad_id in live_ids or ad_id in taken:
                continue
            if goods_id in ev.ok and ev.ok[goods_id].offer.stock >= min_stock:
                continue                                               # that lot can be published again itself; below the threshold it cannot
            if cross is not None and goods_id in cross.waiting:
                continue                                               # it waits for the day (the merge): its ad is not handed over meanwhile
            inherit[kept] = ad_id
            taken.add(ad_id)
    if live_id_of:
        holder.update({ad_id: gid for gid, ad_id in inherit.items()})  # an Id handed over in this step: the lot that takes it has it
        for gid in [g for g in ev.ok if g not in published and g not in inherit and ev.ok[g].offer.stock >= min_stock]:
            if gid in waits or _apart(gid, holder, live_id_of, tire_of):
                ev.ok.pop(gid)
                ev.problems.pop(gid, None)
                ev.excluded[gid] = "same_tire"
        if cross is not None:                                          # the changes of supplier made in this step (the same one again is not new)
            from .tire_match import supplier_of
            for kept in sorted(set(kept_of.values())):
                ad_id = inherit.get(kept) or live_id_of(kept)
                before = owner_of.get(ad_id) or last.get(ad_id, ("", None))[1]
                if (kept in ev.ok and before and before != kept and supplier_of(before) != supplier_of(kept)
                        and not cross.same_change(ad_id, kept)):
                    cross.switches[kept] = (ad_id, before)
    return inherit


def _waits_a_day(ev: Evaluation, cross, code: str, published: set, ad_id: str, before: Optional[str]) -> bool:
    """The owner 2026-10-20: the supplier behind an ad changes at most once a day. True: `code` would take the ad `ad_id` from `before`, a
    code of another supplier, within a day of the last change of that ad; it waits, hidden (no Id of its own either: two ads of one tire).
    A published code is never taken off."""
    from .tire_match import supplier_of
    if code in published or not before or before == code or supplier_of(before) == supplier_of(code):
        return False
    until = cross.wait_until(ad_id, code)
    if not until:
        return False
    ev.ok.pop(code, None)
    ev.problems.pop(code, None)
    ev.excluded[code] = "same_tire"
    cross.waiting[code] = (ad_id, until)
    return True


def _cross_canon(ev, cross, published, snapshot_of, retired=()) -> Dict[Tuple, Tuple]:
    """{key of a side: key of the main side} of the groups of one tire at several suppliers; the candidates of the other sides take the Avito
    brand and model of the main side (in `ev.ok`), so the ad keeps its Model when its source changes. The main side: WebMIM first, then the
    published one, then by code; a published side that is not a candidate now is known by its saved ad. A group whose keys meet another
    group's is left as it is (the old rule decides). A side that has left the unload and whose ad is off the feed (a remembered pair) is
    known by its retired ad; it has no Avito names, so the main side is one that has them."""
    out: Dict[Tuple, Tuple] = {}
    names_of: Dict[Tuple, Tuple[str, str]] = {}
    retired_key: Dict[str, Tuple[str, Tuple]] = {}                    # code -> (removed_at, key) of its latest retired ad
    for goods_id, _ad_id, key, removed_at in retired:
        if goods_id not in retired_key or removed_at > retired_key[goods_id][0]:
            retired_key[goods_id] = (removed_at, tuple(key))
    for members in cross.groups:
        keys: Dict[str, Tuple] = {}
        names: Dict[str, Tuple[str, str]] = {}
        for gid in members:
            if gid in ev.ok:
                key, names[gid] = identity(ev.ok[gid]), (ev.ok[gid].avito_brand, ev.ok[gid].avito_model)
            elif gid in published and snapshot_of:
                text = snapshot_of(gid)
                key = identity_of_ad(text) if text else None
                if key is not None:
                    ad = ET.fromstring(text)
                    names[gid] = ((ad.findtext("Brand") or "").strip(), (ad.findtext("Model") or "").strip())
            else:
                key = None
            if key is None and gid not in ev.ok and gid not in published and gid not in cross.identities and gid in retired_key:
                key = retired_key[gid][1]                              # a remembered side that has left the unload, its ad off the feed
            if key is not None:
                keys[gid] = key
        if len(keys) < 2 or not any(g in ev.ok for g in keys):
            continue
        if len({k[2:] for k in keys.values()}) > 1:
            continue                                                   # the saved or retired ad of a side is another size or index: not this tire
        main = sorted((g for g in keys if g in names), key=lambda g: (cross.rank_of(g), g not in published, g))[0]
        main_key = keys[main]
        if any(k in out or (k != main_key and k in out.values()) for k in set(keys.values())) or main_key in out.values():
            continue
        for gid, key in keys.items():
            if key != main_key:
                out[key] = main_key
        names_of[main_key] = names[main]
    for gid, cand in list(ev.ok.items()):                              # every candidate whose key now counts as the main one (the sides of
        key = identity(cand)                                           # the group and any other lot under the same Avito model) writes
        if key in out:                                                 # the main side's brand and model
            brand, model = names_of[out[key]]
            if (cand.avito_brand, cand.avito_model) != (brand, model):
                ev.ok[gid] = dataclasses.replace(cand, avito_brand=brand, avito_model=model)
    return out


def _apart(gid: str, holder: Dict[str, str], live_id_of: Callable[[str], str], tire_of: Dict[str, Tuple]) -> bool:
    """True: the Id of `gid` is had by a lot of another tire (or of a tire not known)."""
    other = holder.get(live_id_of(gid), gid)
    return other != gid and (tire_of.get(other) is None or tire_of.get(other) != tire_of.get(gid))
