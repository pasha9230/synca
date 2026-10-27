from __future__ import annotations

import dataclasses
import re
import xml.etree.ElementTree as ET

import hashlib
import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional

from . import connections, photo_cache, store
from .candidates import evaluate
from .catalog import suggest
from .decide import decide, hold_decision
from .feed import (
    AGE_NOTE, AGE_WORD, ad_from_text, ad_to_text, assemble_feed, check_feed, compose_title, render_ad, validate_ad, write_atomic,
)
from .models import Action, Decision, Settings
from .photo_library import image_urls, is_library_link
from .suppliers import supplier_of_id
from .webmim import parse_unload


class SupplierDataError(Exception):
    pass


@dataclass
class CycleReport:
    counts: Dict[str, int]
    guard_tripped: bool
    unmatched: List[dict]
    other_territories: Dict[str, int]
    invalid_ads: Dict[str, List[str]]
    feed_path: Optional[Path]
    feed_count: int
    run_id: int
    decisions: list = field(default_factory=list)
    id_codes: Dict[str, str] = field(default_factory=dict)    # the Id of every ad of the feed -> the code of the position behind it
    titles_held: int = 0                                         # the supplier renamed this many tires at once: their titles are kept (guard)


def _final_id(code: str, id_map, id_suffix: str, reissued, acct=None) -> str:
    """The Id the ad of this position has in the feed (the same rules as the last loop of build_ads)."""
    base = (id_map or {}).get(code) or code + id_suffix
    stamp = (reissued or {}).get(code)
    return "%s_%s" % (base, stamp) if stamp and not base.endswith("_" + stamp) and _takes_mark(base, code, acct) else base


REISSUE_MARK = re.compile(r"_r\d{8}(?!\d)")                          # a renewal mark (`_r20261020`)


def _takes_mark(ad_id: str, code: str, acct=None) -> bool:
    """Whether the renewal of `code` marks this Id. An Id it took over from another position (same_tire, a binding to an older ad) that already
    carries a mark stays as it is in the cabinet: `106_r…` never becomes `106_r…_r…` (tails2 report, 2026-10-25). Its own Id is marked as before."""
    if re.sub(r"-x\d+$", "", ad_id.split("_")[0]) == code:
        return True
    if ALIAS_ID.match(ad_id) and acct is not None:
        from .mirror_feed import alias_id
        if ad_id.startswith(alias_id(acct, code)):
            return True
    elif ALIAS_ID.match(ad_id):
        return True                                                       # no account to tell: an alias counts as its own, as before
    return not REISSUE_MARK.search(ad_id)


ALIAS_ID = re.compile(r"k[0-9a-z]{11}(-x\d+)?(_r\d{8})?$")                         # an Id of the alias scheme: nothing to take a code from, already final


def _pending_item(d: Decision, ev, rendered, rendered4) -> dict:
    item = {"goods_id": d.goods_id, "action": d.action.value, "price": d.price, "point": d.point,
            "suppressed": d.suppressed, "first_hold": d.first_hold.isoformat() if d.first_hold else None, "reason": d.reason}
    if d.action in (Action.PUBLISH, Action.UPDATE):
        item["base_price"] = ev.ok[d.goods_id].offer.base_price
        item["snapshot"] = ad_to_text(rendered[d.goods_id])
        if d.goods_id in rendered4:
            item["snapshot4"] = ad_to_text(rendered4[d.goods_id])
    elif d.action is Action.KEEP and d.goods_id in rendered4:
        item["snapshot4"] = ad_to_text(rendered4[d.goods_id])
    return item


def plan_decisions(conn, parsed, ref, settings: Settings, acct: int, today: date, now=None, removed_before: int = 0,
                   cross_supplier: Optional[bool] = None, tochki_trial=None):
    """What the cycle would do with every position of one account: (published now, evaluation, decisions). Writes nothing.
    `now` (Moscow time) turns on the wait for the morning refill of the supplier's catalog (see decide.CART_REFILL_HOUR).
    `cross_supplier`: one tire at two suppliers is one ad (tire_match); None reads the switch `tire_match.feed` (off by default).
    `tochki_trial`: the 4tochki trial of this account (tochki_feed.trial); None reads it, False: none. Out of the trial the 4tochki
    positions are not seen at all; in it the new ones meet the strict entry (tire_match.tochki_entry) and `merge` turns the merge on."""
    from . import tochki_feed
    tochki = tochki_feed.config(conn)["supplier_id"]
    trial = tochki_feed.trial(conn, acct) if tochki_trial is None else (tochki_trial or None)
    parsed = tochki_feed.for_account(parsed, tochki, trial)
    published = store.load_published(conn, acct)
    if not parsed.offers and published:
        raise SupplierDataError("no offers parsed for territory %s while %d items are published" % (
            settings.territory_rn, len(published)))
    ev = evaluate(parsed.offers, parsed.issues, settings, ref, store.load_confirmed_mappings(conn),
                  store.load_brand_map(conn))
    for gid, stock in getattr(parsed, "stocks", {}).items():                # a broken row or a stale supplier: a known fall below the threshold takes it off
        ev.stocks.setdefault(gid, stock)
    for gid in published:
        if gid not in parsed.offers and supplier_of_id(gid) in parsed.unavailable:
            ev.problems.setdefault(gid, ["supplier_unavailable"])
    from . import mirror_feed, same_tire
    id_map = mirror_feed.adopted_maps(conn, acct)[0]
    aliases: Dict[str, str] = {}
    if mirror_feed.alias_scheme(conn, acct):                              # an account whose Ids have no supplier code in them
        aliases = {c: mirror_feed.alias_id(acct, c) for c in set(ev.ok) | set(published) if "-x" not in c and c not in id_map}
        id_map = dict(id_map, **aliases)                                  # a code that is already bound (the Id of an older ad of its tire) keeps it
    suffix = "" if acct == connections.PRIMARY_ACCOUNT else "_a%d" % acct
    reissued = mirror_feed.reissued_own(conn, acct)

    def live_id_of(code):                                                 # the Id the code has (or would have) in this account's cabinet
        return _final_id(code, id_map, suffix, reissued, acct)            # a renewed one with its mark (`_r<stamp>`), as build_ads writes it

    def first_seen_of(ad_id):
        row = conn.execute("SELECT MIN(first_seen) FROM avito_ad WHERE account_id = ? AND ad_id = ?", (acct, ad_id)).fetchone()
        return row[0] if row else None
    foreign: set = set()                                                  # the Ids the other accounts have now: never handed over from the memory of retired ads
    for other in connections.list_feed_accounts(conn):
        if other["id"] != acct:
            bound = mirror_feed.adopted_maps(conn, other["id"])[0]
            other_suffix = "" if other["id"] == connections.PRIMARY_ACCOUNT else "_a%d" % other["id"]
            other_reissued = mirror_feed.reissued_own(conn, other["id"])
            foreign |= {_final_id(code, bound, other_suffix, other_reissued, other["id"]) for code in store.load_published(conn, other["id"])}
    from . import tire_match
    merge = tire_match.switch_on(conn) if cross_supplier is None else cross_supplier
    trial_merge = cross_supplier is None and not merge and trial is not None and trial["mode"] == "merge"
    cross = (tire_match.for_feed(conn, parsed.offers, acct, now, getattr(parsed, "unavailable", ()))
             if merge or trial_merge else None)
    if cross is not None and trial is not None and not tire_match.switch_on(conn):
        cross = tire_match.only_webmim_and(cross, tochki)                 # the trial merges WebMIM row 1 and 4tochki only (owner, question 8)
    retired = store.load_retired(conn, acct, today)
    if trial is not None:                                                 # one tire, one ad: a new 4tochki position goes in only as a tire of its own
        match = cross if cross is not None else tire_match.pairs(
            parsed.offers, store.load_model_synonyms(conn), tochki, not_pairs=frozenset(store.not_tire_pairs(conn)))
        prefix = "s%d-" % tochki
        saved = [store.get_snapshot(conn, g, acct) for g in published if g not in parsed.offers and not g.startswith(prefix)]
        keys = [same_tire.identity_of_ad(text) for text in saved if text]
        mode = trial["mode"] if cross is not None else "no_twin"           # no merge in this plan: a pair would be two ads
        for gid, why in tire_match.tochki_entry(ev, match, parsed.offers, published, tochki, mode, [k for k in keys if k],
                                                retired, trial["allow"]).items():
            ev.ok.pop(gid, None)
            ev.problems.pop(gid, None)
            ev.excluded[gid] = why
    ev.inherit = same_tire.dedupe(ev, published, settings.min_stock, live_id_of, first_seen_of,
                                  lambda code: store.get_snapshot(conn, code, acct), retired, foreign, cross=cross)
    if trial is not None and cross is not None:                           # a 4tochki position that goes on under a WebMIM ad (this cycle or
        group_of = cross.group_of()                                       # before): its «Артикул» and «Страна» from WebMIM (owner, question 4)
        prefix = "s%d-" % tochki
        for kept, cand in list(ev.ok.items()):
            id_code = re.sub(r"-x\d+$", "", (ev.inherit.get(kept) or live_id_of(kept)).split("_")[0])
            if not kept.startswith(prefix) or id_code == kept:
                continue
            twin = id_code if id_code in parsed.offers and not id_code.startswith(prefix) else next(
                (g for g in group_of.get(kept, ()) if not g.startswith(prefix) and g in parsed.offers), None)
            if twin is not None:
                other = parsed.offers[twin]
                ev.ok[kept] = dataclasses.replace(cand, offer=dataclasses.replace(
                    cand.offer, code=cand.offer.code or other.code, country=cand.offer.country or other.country))
    if trial is not None:                                                 # a live 4tochki ad under another position's Id keeps its Avito brand
        prefix = "s%d-" % tochki                                          # and model (the WebMIM side may be gone, its names with it)
        for gid, cand in list(ev.ok.items()):
            id_code = re.sub(r"-x\d+$", "", live_id_of(gid).split("_")[0])
            if gid.startswith(prefix) and gid in published and id_code != gid and id_code not in parsed.offers:
                text = store.get_snapshot(conn, gid, acct)
                try:
                    ad = ET.fromstring(text) if text else None
                except ET.ParseError:
                    ad = None
                brand, model = ((ad.findtext("Brand") or "").strip(), (ad.findtext("Model") or "").strip()) if ad is not None else ("", "")
                if brand and model and (brand, model) != (cand.avito_brand, cand.avito_model):
                    ev.ok[gid] = dataclasses.replace(cand, avito_brand=brand, avito_model=model)
    ev.match = cross                                                      # the cycle remembers its pairs and changes of supplier (run_cycle)
    for code in list(aliases):                                            # a code that takes the Id of an older ad does not need its own alias
        if code in ev.inherit:
            aliases.pop(code)
    ev.aliases = aliases
    known_points = store.load_known_points(conn)
    rules = {(r["brand_key"], r["season"]): r["address_id"] for r in connections.list_point_rules(conn, acct)}
    for gid, cand in ev.ok.items():
        target = rules.get((cand.offer.brand_key, cand.offer.season.value))
        if target in settings.point_values:
            known_points[gid] = target
    bound_now = mirror_feed.adopted_maps(conn, acct)[0]
    live_bound = frozenset(c for c, i in bound_now.items()
                           if not (mirror_feed.alias_scheme(conn, acct) and i == mirror_feed.alias_id(acct, c)))   # a plain alias says nothing about being live
    ds = decide(ev, published, store.load_suppressed(conn, acct), known_points, settings, today, now=now, already_live=live_bound,
                removed_before=removed_before)
    return published, ev, ds


def live_titles_of(out_dir) -> Dict[str, str]:
    """Id -> Title of the ads of the feed that Avito is reading now (`<token>/avito_feed.xml`, next to the draft folder `next/` or `new/`).
    An ad that stands on Avito keeps its title (owner, 2026-10-14): the title format of compose_title is for new ads only. The saved snapshots
    are not the source: they can hold older, unclean titles than the file Avito really reads."""
    if out_dir is None or Path(out_dir).name not in ("next", "new"):
        return {}
    try:
        root = ET.parse(str(Path(out_dir).parent / "avito_feed.xml")).getroot()
    except (OSError, ET.ParseError):
        return {}
    return {a.findtext("Id"): a.findtext("Title") for a in root.findall("Ad") if a.findtext("Id") and a.findtext("Title")}


def _basis_key(account_id: int) -> str:
    return "title_basis_%d" % account_id


def title_basis(conn, account_id: int) -> Dict[str, list]:
    """Id -> [code, supplier's name] of the position behind the ad at the time its kept title was last right for it (see `renamed`)."""
    try:
        return dict(json.loads(store.get_state(conn, _basis_key(account_id)) or "{}"))
    except (ValueError, TypeError):
        return {}


def renamed(before: str, now: str) -> bool:
    """The supplier changed the name of the tire in a way the title shows (owner, 2026-10-19: a kept title stays only while the supplier's name
    is the same). Both names go through the same compose_title, so a change of our title format, spaces, «Ш.» or the origin code is not a rename."""
    return compose_title(before) != compose_title(now)


def _fresh_names(owners, rendered, rendered4, ev) -> Dict[int, tuple]:
    """id(ad) -> (code, supplier's name), for the ads built from the supplier's data this cycle (an ad held on its snapshot has none)."""
    fresh = {id(a) for a in list(rendered.values()) + list(rendered4.values())}
    return {id(a): (code, ev.ok[code].offer.name_orig) for a, code in owners if id(a) in fresh and code in ev.ok}


def _name_before(entry, code: str) -> Optional[str]:
    """The remembered name, if it was remembered for the same code: an Id that passed to another lot of the same tire (same_tire) or to another
    position starts afresh, the other lot's name is not a rename."""
    if isinstance(entry, (list, tuple)) and len(entry) == 2 and entry[0] == code:
        return entry[1]
    return None


def next_title_basis(old: Dict[str, list], titles: Dict[str, str], ads, names: Dict[int, tuple]) -> Dict[str, list]:
    """The basis to remember after a build, for the Ids whose title is kept (`titles`: live and bound titles). An Id seen for the first time takes
    the name it has now (what was before is not known, the title stays). A renamed tire moves to its new name only once the published title is
    the fresh one, so a cycle that is not published does not lose the rename. An Id with no fresh ad keeps its entry; an Id with no kept title
    is dropped."""
    current = {a.findtext("Id"): (names[id(a)], a.findtext("Title")) for a in ads if id(a) in names and a.findtext("Id")}
    basis = {}
    for ad_id, kept in titles.items():
        before = old.get(ad_id)
        if ad_id not in current:
            if before is not None:
                basis[ad_id] = before
            continue
        (code, name), title = current[ad_id]
        known = _name_before(before, code)
        if known is None or not renamed(known, name):
            basis[ad_id] = [code, known if known is not None else name]
        else:
            basis[ad_id] = [code, name] if kept == title else [code, known]
    return basis


def build_ads(conn, parsed, ev, published, ds, settings: Settings, acct: int, today: date, description_template=None,
              id_suffix: str = "", image_prefix: str = "", id_map=None, image_map=None, contact_name: str = "", live_titles=None,
              alias_packs: bool = False, title_template: Optional[str] = None, photo_version: str = "", basis=None, title_guard=None,
              own_photos=None, live_images=None):
    """The ads of one account's feed from its decisions: (ads, invalid, final decisions, rendered, rendered 4-packs). Writes nothing.
    A position whose ad does not build is skipped, or held on its snapshot, or taken off when the wait is over.
    `live_images`: Id -> the photo links of the ad Avito reads now (live_images_of): a 4tochki position that goes on under the Id of
    another position's ad keeps that ad's photos (owner 2026-10-27)."""
    ads = []
    invalid: Dict[str, List[str]] = {}
    final = []
    rendered: Dict[str, object] = {}
    rendered4: Dict[str, object] = {}
    owners: List[tuple] = []                                              # (ad, code of the position): which position stands behind which Id
    old_models = store.load_old_model_names(conn)                         # {old_model}: only a template that has it uses this
    from . import tochki_feed
    tochki = tochki_feed.config(conn)["supplier_id"]
    tochki_prefix = "s%d-" % tochki if tochki else None

    def no_pack(code):                                                    # no set of four of a 4tochki position in the trial (plan §4з)
        return bool(tochki_prefix) and code.startswith(tochki_prefix)
    model_photos = ModelPhotos(conn, parsed.offers, tochki_prefix)
    for d in ds.decisions:
        if d.action not in (Action.PUBLISH, Action.UPDATE, Action.KEEP, Action.HOLD):
            final.append(d)
            continue
        pub = published.get(d.goods_id)
        use_snapshot = d.action is Action.HOLD or (pub is not None and pub.location == "own")
        if not use_snapshot:
            ad = render_ad(ev.ok[d.goods_id], d.price, d.point, settings, today, template=description_template, title_template=title_template,
                           old_models=old_models)
            errors = validate_ad(ad)                                       # judged on the fresh ad: a kept title never hides a broken one
            if not errors:
                ads.append(ad)
                owners.append((ad, d.goods_id))
                rendered[d.goods_id] = ad
                if settings.pack4_for(ev.ok[d.goods_id].offer) and ev.ok[d.goods_id].offer.stock >= 4 and not no_pack(d.goods_id):
                    twin = render_ad(
                        ev.ok[d.goods_id], d.price, d.point, settings, today, template=description_template, pack=4, title_template=title_template,
                        old_models=old_models)
                    ads.append(twin)
                    owners.append((twin, d.goods_id))
                    rendered4[d.goods_id] = twin
                final.append(d)
                continue
            invalid[d.goods_id] = errors
            if pub is None:
                final.append(Decision(d.goods_id, Action.SKIP, "invalid_ad:" + ",".join(errors)))
                continue
            d = hold_decision(d.goods_id, pub, "invalid_ad:" + ",".join(errors), settings, today)
            if d.action is Action.REMOVE:
                final.append(d)
                continue
        snapshot = store.get_snapshot(conn, d.goods_id, acct)
        if snapshot is None:
            invalid[d.goods_id] = invalid.get(d.goods_id, []) + ["no_snapshot"]
            final.append(d)
            continue
        held_ad = ad_from_text(snapshot)
        ads.append(held_ad)
        owners.append((held_ad, d.goods_id))
        offer = parsed.offers.get(d.goods_id)
        # a set of four is not offered for a position that waits for the morning, nor when it is known that fewer than four are left
        no_four = d.reason.startswith("cart_wait:") or (offer is not None and offer.stock < 4) or no_pack(d.goods_id)
        twin = None if no_four else (
            store.get_snapshot4(conn, d.goods_id, acct) if settings.pack4 and (offer is None or settings.pack4_for(offer)) else None)
        if twin:
            twin_ad = ad_from_text(twin)
            ads.append(twin_ad)
            owners.append((twin_ad, d.goods_id))
        final.append(d)
    id_map, image_map = id_map or {}, image_map or {}
    from . import mirror_feed, reconcile
    manual = mirror_feed.manual_keys(conn, acct)
    by_hand, by_hand_keys = mirror_feed.manual_codes(conn, acct), mirror_feed.manual_keys(conn, acct, by_hand=True)
    if manual or by_hand or by_hand_keys:            # owner, 2026-10-19: a manual ad of the cabinet stays; the feed does not put the same tire next to it
        # 2026-10-22: an ad switched on by hand under an Id of a file: neither the position whose Id it has nor the same tire is published anew;
        # an ad of ours already out stays (team lead: one ad less on Avito would make no sense)
        gone = {code for ad, code in owners if reconcile.key_ad(ad) in manual
                or (published.get(code) is None and (code in by_hand or reconcile.key_ad(ad) in by_hand_keys))}
        if gone:
            owners = [(a, c) for a, c in owners if c not in gone]
            ads = [a for a, _c in owners]
            final = [Decision(d.goods_id, Action.REMOVE if published.get(d.goods_id) is not None else Action.SKIP, "manual_ad") if d.goods_id in gone else d
                     for d in final]
            for code in gone:
                rendered.pop(code, None)
                rendered4.pop(code, None)
    reissued = mirror_feed.reissued_own(conn, acct)                      # {code: "r<stamp>"} of the positions the owner renewed
    owner_of = {id(a): code for a, code in owners}                        # the position behind every ad (a saved ad too)
    names = _fresh_names(owners, rendered, rendered4, ev)
    basis = basis or {}
    renames = []                                                          # (ad, live title) of the ads whose tire the supplier renamed
    kept = photo_cache.root_for(conn) if image_prefix else None          # the photos the panel serves under /img/<token>/<code>.jpg
    from_cache = 0
    for ad in ads:
        if contact_name:                                                  # the contact person of this account, in every ad (also the ones held on a snapshot)
            name_node = ad.find("ManagerName")
            if name_node is None:
                name_node = ET.SubElement(ad, "ManagerName")
            name_node.text = contact_name
        phone_node = ad.find("ContactPhone")
        if phone_node is not None and settings.contact_phone is not None and acct != 1:
            phone_node.text = settings.contact_phone        # also the ads kept on a saved snapshot: no other number than the account's own
        node = ad.find("Id")
        if node is None or not node.text:
            continue
        code = re.sub(r"-x\d+$", "", node.text.split("_")[0])
        single = node.text.split("_")[0] == code
        pack = node.text.split("_")[0].endswith("-x4")
        if ALIAS_ID.match(node.text):                       # an alias Id (a saved ad of an aliased account): final as it is
            code = owner_of.get(id(ad)) or code
        elif single and code in id_map:
            node.text = id_map[code]                           # an account moved from a mirror keeps the Ids its ads already have on Avito
        elif alias_packs and not single and code in id_map:
            node.text = id_map[code] + "-x4"                   # a set of four in an account with the alias scheme: the Id of its single ad + -x4
        elif id_suffix and not (node.text.endswith(id_suffix) or (id_suffix + "_") in node.text):
            node.text = node.text + id_suffix                  # the Id of this account: the same position never has the same Id in two accounts
        owner = owner_of.get(id(ad)) or code                 # the position behind the ad: a saved ad may carry the Id it took over
        stamp = reissued.get(owner)
        if stamp and not node.text.endswith("_" + stamp) and _takes_mark(node.text, owner, acct):
            node.text = "%s_%s" % (node.text, stamp)        # renewed on purpose (mirror-reissue): Avito does not take back an ad deleted in the cabinet
        if live_titles and live_titles.get(node.text) and ad.find("Title") is not None:
            before = _name_before(basis.get(node.text), names[id(ad)][0]) if id(ad) in names else None
            aged = (id(ad) in names and AGE_WORD.search(live_titles[node.text]) and not AGE_WORD.search(ad.find("Title").text or "")
                    and (AGE_NOTE.search(names[id(ad)][1] or "") or AGE_NOTE.search(before or "")))   # the note now, or when the title was kept
            if (before is None or not renamed(before, names[id(ad)][1])) and not aged:
                ad.find("Title").text = live_titles[node.text]   # the ad stands on Avito under this Id: its title does not change
            elif ad.find("Title").text != live_titles[node.text]:
                renames.append((ad, live_titles[node.text]))   # the supplier renamed the tire: the fresh title (owner, 2026-10-19), unless too many;
                # also a live title that still shows the supplier's age note («… старше в наличии»): the fresh one has none (owner, 2026-10-25)
        photo_code = set_code = code                         # the position whose photos the ad shows, the one whose own set it leads with
        if tochki_prefix and owner.startswith(tochki_prefix) and not ALIAS_ID.match(node.text):
            id_code = re.sub(r"-x\d+$", "", node.text.split("_")[0])
            if id_code != owner:                             # it goes on under the Id of another position's ad: that ad's photos stay
                photo_code = set_code = id_code
                if _keepable((live_images or {}).get(node.text), own_photos):
                    _images_node(ad)
                    _set_images(ad, live_images[node.text])
                    continue
            else:                                            # a 4tochki ad of its own: the WebMIM picture of the same model, if any
                photo_code = model_photos.code_for(parsed.offers.get(owner)) or code
        image = ad.find("Images/Image")
        if image is None and photo_code != code and (image_prefix or photo_code in image_map or getattr(parsed.offers.get(photo_code), "image", "")):
            image = _images_node(ad).find("Image")
        if image is None and kept is not None and photo_cache.cached(kept, code) is not None:
            # hotfix 2026-10-21: the supplier emptied the picture of a position whose photo we keep: the ad keeps the same link of the
            # account (Avito sees no change); with no kept photo there is no Images and the check «photo» stops the account, as before
            image = ET.SubElement(ET.SubElement(ad, "Images"), "Image", url="")
            from_cache += 1
        if image is not None:
            if photo_code in image_map:
                image.set("url", image_map[photo_code])
            elif image_prefix:
                url = "%s%s.jpg" % (image_prefix, photo_code)
                query = []
                if stamp or alias_packs:                       # a renewed or aliased ad is a new ad: its photo link is new too (same picture, its own address)
                    query.append("v=" + node.text)
                if photo_version:                              # the account changed the look of its photos: a new address, so Avito fetches them again
                    query.append("s=" + photo_version)
                image.set("url", url + ("?" + "&".join(query) if query else ""))
            elif photo_code != code and getattr(parsed.offers.get(photo_code), "image", ""):
                image.set("url", parsed.offers[photo_code].image)
            # own photos on: our approved frames first, this account's own files; off: the one link (a saved ad loses earlier own photos too)
            offer = parsed.offers.get(set_code)
            fallback = image.get("url")
            if is_library_link(fallback):                      # a saved ad led with our frame and no account link replaced it: the supplier's photo
                source = parsed.offers.get(photo_code)
                fallback = source.image if source is not None and source.image else None
            urls = image_urls(own_photos, offer, pack, fallback)
            if (tochki_prefix and owner.startswith(tochki_prefix) and _keepable((live_images or {}).get(node.text), own_photos)
                    and not any(is_library_link(u) for u in urls)):
                urls = live_images[node.text]                  # a live 4tochki ad keeps its photos until our own frames lead (owner 2026-10-27)
            _set_images(ad, urls)
    if from_cache:
        logging.getLogger("tires_sync.sync").warning("фото из кэша: %d позиций (поставщик убрал картинку)", from_cache)
    limit = max(settings.guard_min, int(settings.guard_fraction * len(live_titles or {})))
    if len(renames) > limit:                                              # the supplier changed the names of many tires at once (a new format?):
        for ad, title in renames:                                         # no mass change of titles on Avito, the owner looks first (2026-10-19)
            ad.find("Title").text = title
    if title_guard is not None:
        title_guard["held"] = len(renames) if len(renames) > limit else 0
    return ads, invalid, final, rendered, rendered4, owners


class ModelPhotos:
    """The WebMIM position whose picture a 4tochki ad of its own shows (owner 2026-10-27): one of the same model (brand and main name, the
    owner's synonyms), with a picture, the smallest code; none: the ad shows the 4tochki picture. Built on the first question."""

    def __init__(self, conn, offers, tochki_prefix):
        self.conn, self.offers, self.prefix, self.index = conn, offers, tochki_prefix, None

    def _model(self, offer, synonyms):
        from .tire_match import brand_form, model_form
        brand = brand_form(offer.brand_raw or offer.brand_key)
        main = model_form(offer.model_raw, offer.brand_raw or offer.brand_key)
        return (brand, synonyms.get((brand, main), main)) if brand and main else None

    def code_for(self, offer) -> Optional[str]:
        if offer is None or not self.prefix:
            return None
        if self.index is None:
            self.synonyms = store.load_model_synonyms(self.conn)
            self.index = {}
            for gid in sorted(self.offers):
                other = self.offers[gid]
                if gid.startswith(self.prefix) or not other.image:
                    continue
                key = self._model(other, self.synonyms)
                if key is not None:
                    self.index.setdefault(key, gid)
        key = self._model(offer, self.synonyms)
        return self.index.get(key) if key is not None else None


def _keepable(urls, own_photos) -> bool:
    """Live photo links an ad may keep: every link of our library among them is still this account's (a withdrawn frame, a set out of the
    feed or own photos off: the ad is built afresh, the check `own_photo_foreign` would stop the account every cycle otherwise)."""
    allowed = own_photos.allowed if own_photos is not None else frozenset()
    return bool(urls) and all(u in allowed for u in urls if is_library_link(u))


def _images_node(ad):
    """The ad's Images with at least one Image (made empty when the ad has none)."""
    images = ad.find("Images")
    if images is None:
        images = ET.SubElement(ad, "Images")
    if images.find("Image") is None:
        ET.SubElement(images, "Image", url="")
    return images


def live_images_of(out_dir) -> Dict[str, List[str]]:
    """Id -> the photo links of the ads of the feed that Avito is reading now (as live_titles_of)."""
    if out_dir is None or Path(out_dir).name not in ("next", "new"):
        return {}
    try:
        root = ET.parse(str(Path(out_dir).parent / "avito_feed.xml")).getroot()
    except (OSError, ET.ParseError):
        return {}
    out = {}
    for a in root.findall("Ad"):
        urls = [i.get("url") for i in a.findall("Images/Image") if i.get("url")]
        if a.findtext("Id") and urls:
            out[a.findtext("Id")] = urls
    return out


def _set_images(ad, urls: List[str]) -> None:
    """Put `urls` into the ad's Images in this order (the first Image node keeps its place, the rest are replaced; no urls: no Images)."""
    images = ad.find("Images")
    first = images.find("Image")
    for node in images.findall("Image")[1:]:
        images.remove(node)
    if not urls:
        ad.remove(images)                              # nothing left that the account may carry
        return
    first.set("url", urls[0])
    for url in urls[1:]:
        ET.SubElement(images, "Image", url=url)


def run_cycle(
    xml_bytes: bytes,
    settings: Settings,
    ref: Dict[str, FrozenSet[str]],
    conn,
    today: date,
    out_dir=None,
    description_template: Optional[str] = None,
    account_id: Optional[int] = None,
    record_seen: bool = True,
    expected_removals: int = 0,
    parsed=None,
    input_sha: Optional[str] = None,
    now=None,
    check_size: bool = True,
    id_suffix: str = "",
    image_prefix: str = "",
    id_map=None,
    image_map=None,
    contact_name: str = "",
    title_template: Optional[str] = None,
    photo_version: str = "",
    own_photos=None,
    removed_before: int = 0,
) -> CycleReport:
    if parsed is None:
        parsed = parse_unload(xml_bytes, settings.territory_rn)
    acct = account_id or 1
    published, ev, ds = plan_decisions(conn, parsed, ref, settings, acct, today, now=now, removed_before=removed_before)

    from . import mirror_feed
    if ev.inherit or ev.aliases:                                          # remembered: the tire keeps the Id of its oldest ad in later cycles too
        if account_id is not None:
            mirror_feed.bind_ids(conn, acct, ev.inherit, datetime.utcnow().isoformat(timespec="seconds"), "same_tire")
        id_map = dict(id_map or {}, **ev.aliases, **ev.inherit)
    if ev.match is not None and account_id is not None:                  # the merge on: its pairs and changes of supplier are kept (only added)
        from . import tire_match
        tire_match.record(conn, acct, ev.match, store.moscow_iso())
    live = live_titles_of(out_dir)
    titles = dict(mirror_feed.bound_titles(conn, acct), **live)
    live_unread = not live and out_dir is not None and (Path(out_dir).parent / "avito_feed.xml").exists()   # a broken file: do not forget renames
    basis = title_basis(conn, acct) if account_id is not None else {}
    title_guard: Dict[str, int] = {}
    ads, invalid, final, rendered, rendered4, owners = build_ads(
        conn, parsed, ev, published, ds, settings, acct, today, description_template, id_suffix, image_prefix, id_map, image_map,
        contact_name, titles, alias_packs=bool(account_id is not None and mirror_feed.alias_scheme(conn, acct)),
        title_template=title_template or None, photo_version=photo_version or "", basis=basis, title_guard=title_guard,
        own_photos=own_photos, live_images=live_images_of(out_dir))
    id_codes = {a.findtext("Id"): code for a, code in owners if a.findtext("Id")}
    if ev.aliases and account_id is not None:                              # an alias is remembered for the positions that are in the feed (not for the whole catalog)
        mirror_feed.bind_ids(conn, acct, {c: ev.aliases[c] for _a, c in owners if c in ev.aliases},
                             datetime.utcnow().isoformat(timespec="seconds"), "alias")
    ds.decisions = final

    if check_size:                                      # a feed that is only part of another one (the new positions of a mirror) is judged as a whole
        check_feed(len(ads), store.count_published_ads(conn, acct, twins=settings.pack4), allow_shrink=settings.allow_mass_removal,
                   expected_removals=expected_removals)
    feed_path = None
    if out_dir is not None:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        feed_path = out / "avito_feed.xml"
        feed_bytes = assemble_feed(ads)
        write_atomic(feed_path, feed_bytes)
        if account_id is not None:
            pending = []
            for d in ds.decisions:
                twin_sync = (d.action is Action.KEEP and d.goods_id in rendered
                             and (d.goods_id in rendered4) == (store.get_snapshot4(conn, d.goods_id, acct) is None))
                back = d.action is Action.KEEP and published.get(d.goods_id) is not None and published[d.goods_id].first_hold is not None
                if d.action in (Action.PUBLISH, Action.UPDATE, Action.REMOVE, Action.HOLD) or twin_sync or back:
                    item = _pending_item(d, ev, rendered, rendered4)
                    if d.action is Action.REMOVE:
                        item["ad_id"] = _final_id(d.goods_id, id_map, id_suffix, mirror_feed.reissued_own(conn, acct), acct)   # the Id the ad has on Avito
                    if back and not twin_sync:
                        item["back_only"] = True          # it only stops waiting: its set of four stays as it is
                    pending.append(item)
            store.save_pending(conn, account_id, feed_bytes, pending)
            new_basis = next_title_basis(basis, titles, ads, _fresh_names(owners, rendered, rendered4, ev))
            if titles and new_basis != basis and not live_unread and not title_guard.get("held"):   # held renames are not forgotten
                store.set_state(conn, _basis_key(acct), json.dumps(new_basis, ensure_ascii=False))

    unmatched = [
        {"goods_id": gid, "brand": brand, "model": model, "suggestions": suggest(brand, model, ref)}
        for gid, (brand, model) in sorted(ev.unmatched.items())
    ]
    counts = dict(Counter(d.action.value for d in ds.decisions))
    run_id = store.save_run(
        conn, datetime.utcnow().isoformat(timespec="seconds"), True,
        input_sha or hashlib.sha256(xml_bytes).hexdigest(), json.dumps(counts, ensure_ascii=False), account_id,
    )
    if record_seen:
        store.record_seen(conn, run_id, parsed.offers.values())
    return CycleReport(
        counts, ds.guard_tripped, unmatched, parsed.other_territories, invalid, feed_path, len(ads), run_id,
        ds.decisions, id_codes, title_guard.get("held", 0),
    )
