"""The mirror of a feed somebody keeps by hand: every ad is carried over as it is, with the same Id; the end date is kept alive and the price follows the supplier.

Avito knows an ad by its Id: an ad whose Id is missing from the feed is taken down, an ad with a new Id is a new ad. The mirror therefore never
changes an Id and never drops an ad for any reason but the supplier not having the position; texts and photos stay as the owner of the file made them.
The one thing it may fix is a `Model` that Avito's reference does not have, and only to what the mapping table (the «Сопоставления» page) holds.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import tempfile
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, List, Optional, Tuple

from . import photo_library, store
from .catalog import normalize_model, resolve_brand
from .feed import sale_terms, stable_date_end
from .pricing import position_price

MAX_BYTES = 20_000_000
MAX_ADS = 5000


class MirrorError(ValueError):
    pass


def import_feed(conn, account_id: int, data: bytes, now: str) -> dict:
    """Store the ads of a feed file for an account and switch the account to the mirror mode. A file that is not clean changes nothing."""
    if not data or len(data) > MAX_BYTES:
        raise MirrorError("Файл пустой или слишком большой.")
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise MirrorError("Файл не читается как XML.") from None
    ads = root.findall("Ad") if root.tag == "Ads" else []
    if not ads:
        raise MirrorError("В файле нет объявлений (ожидается корневой элемент Ads с элементами Ad).")
    if len(ads) > MAX_ADS:
        raise MirrorError("В файле больше %d объявлений." % MAX_ADS)
    ids = [(a.findtext("Id") or "").strip() for a in ads]
    if not all(ids):
        raise MirrorError("У части объявлений нет Id: по нему Авито узнаёт объявление.")
    if len(set(ids)) != len(ids):
        raise MirrorError("В файле повторяются Id объявлений.")
    if conn.execute("SELECT 1 FROM account WHERE id = ?", (account_id,)).fetchone() is None:
        raise MirrorError("Такого аккаунта нет.")
    with conn:
        conn.execute("DELETE FROM imported_ad WHERE account_id = ?", (account_id,))
        conn.executemany("INSERT INTO imported_ad (account_id, position, ad_id, raw, imported_at) VALUES (?,?,?,?,?)",
                         [(account_id, n, ids[n], ET.tostring(ad, encoding="unicode"), now) for n, ad in enumerate(ads)])
        conn.execute("UPDATE account SET feed_mode = 'mirror' WHERE id = ?", (account_id,))
    return {"ads": len(ads), "with_images": sum(1 for a in ads if a.find("Images") is not None)}


SHAPE_TAGS = ("NDS", "WholesaleType", "MultiItem", "ContactPhone")
# Tags that Avito's template of the category (14-10-2026) no longer has: cut from every ad of the file and from the new positions
# (`Quantity`: selling by one is no parameter, and the minimum order is not sent at all since 2026-10-20, see `feed.sale_terms`).
OBSOLETE_AD_TAGS = ("TargetAudience", "Quantity")


def match_shape(conn, account_id: int, ads: Iterable[ET.Element]) -> None:
    """Make the new ads look like the ads of the file as far as the account's own way of selling goes: the same
    VAT, wholesale and contact phone (the phone belongs to the Avito account). Minimum order is not sent (`feed.sale_terms`)."""
    row = conn.execute("SELECT raw FROM imported_ad WHERE account_id = ? ORDER BY position LIMIT 1", (account_id,)).fetchone()
    if row is None:
        return
    donor = ET.fromstring(row[0])
    for ad in ads:
        if ad.find("RunFlat") is None:
            ET.SubElement(ad, "RunFlat").text = "Нет"                  # named explicitly: only the positions the supplier marks are «Да»
        for tag in SHAPE_TAGS:
            source = donor.find(tag)
            if source is None or not (source.text or "").strip():
                continue
            node = ad.find(tag)
            if node is None:
                node = ET.SubElement(ad, tag)
            node.text = source.text


def stored_codes(conn, account_id: int) -> set:
    return {supplier_code(i) for i in stored_ids(conn, account_id)}


def reissue_ids(conn, account_id: int, ad_ids: Iterable[str], stamp: str, now: str, author: str) -> Dict[str, str]:
    """Give stored ads new Ids (`<Id>_r<stamp>`): Avito does not take back an ad deleted in the cabinet under its old Id («to upload it as new,
    change the Id»). The supplier code in front of the first `_` stays, so prices, stock and models still find the position; the order of
    the file is kept. All or nothing: one unknown Id changes nothing. Returns {old Id: new Id}."""
    if not re.fullmatch(r"\d{8}", str(stamp)):
        raise ValueError("Метка даты: восемь цифр, например 20261014.")
    ids = list(dict.fromkeys(str(i).strip() for i in ad_ids if str(i).strip()))
    stored = set(stored_ids(conn, account_id))
    unknown = [i for i in ids if i not in stored]
    if unknown:
        raise ValueError("Этих Id нет в файле аккаунта: %s." % ", ".join(unknown[:5]))
    new: Dict[str, str] = {}
    for old in ids:
        if old.endswith("_r" + stamp):
            raise ValueError("Объявление %s уже переиздано с этой меткой." % old)
        fresh = "%s_r%s" % (old, stamp)
        if fresh in stored or fresh in new.values():
            raise ValueError("Id %s уже есть в файле аккаунта." % fresh)
        new[old] = fresh
    with conn:
        for old, fresh in new.items():
            raw = conn.execute("SELECT raw FROM imported_ad WHERE account_id = ? AND ad_id = ?", (account_id, old)).fetchone()[0]
            ad = ET.fromstring(raw)
            ad.find("Id").text = fresh
            conn.execute("UPDATE imported_ad SET ad_id = ?, raw = ? WHERE account_id = ? AND ad_id = ?",
                         (fresh, ET.tostring(ad, encoding="unicode"), account_id, old))
    store.write_audit(conn, now, author, "mirror_reissue", {"account_id": account_id, "ids": new})
    return new


def _own_key(account_id: int) -> str:
    return "mirror_reissued_own_%d" % account_id


def reissued_own(conn, account_id: int) -> Dict[str, str]:
    """{supplier code: Id suffix} of the positions we make ourselves (not from the file) whose Ids were renewed."""
    raw = store.get_state(conn, _own_key(account_id))
    try:
        return {str(k): str(v) for k, v in json.loads(raw).items()} if raw else {}
    except (ValueError, AttributeError):
        return {}


def reissue_own(conn, account_id: int, codes: Iterable[str], stamp: str, now: str, author: str) -> Dict[str, str]:
    """Positions made by our own rules (the file did not have them) get a new Id in the feed (`<code>_r<stamp>`, `<code>-x4_r<stamp>`); they stay
    tracked by their code. Returns {code: new Id}."""
    if not re.fullmatch(r"\d{8}", str(stamp)):
        raise ValueError("Метка даты: восемь цифр, например 20261014.")
    known = reissued_own(conn, account_id)
    out: Dict[str, str] = {}
    for code in dict.fromkeys(str(c).strip() for c in codes if str(c).strip()):
        if not re.fullmatch(r"[0-9A-Za-z.\-]+", code) or "_" in code:
            raise ValueError("Код позиции не распознан: %s." % code)
        if known.get(code) == "r" + stamp:
            raise ValueError("Позиция %s уже переиздана с этой меткой." % code)
        known[code] = "r" + stamp
        out[code] = "%s_r%s" % (code, stamp)
    store.set_state(conn, _own_key(account_id), json.dumps(known, ensure_ascii=False))
    store.write_audit(conn, now, author, "mirror_reissue_own", {"account_id": account_id, "ids": out})
    return out


def unreissue_own(conn, account_id: int, codes: Iterable[str], now: str, author: str) -> List[str]:
    """Take back the renewal of positions (the ad that was alive in the cabinet is used again under its own Id). Returns the codes that were renewed."""
    known = reissued_own(conn, account_id)
    gone = [c for c in dict.fromkeys(codes) if known.pop(c, None)]
    if gone:
        store.set_state(conn, _own_key(account_id), json.dumps(known, ensure_ascii=False))
        store.write_audit(conn, now, author, "mirror_unreissue_own", {"account_id": account_id, "codes": gone})
    return gone


def apply_reissued_own(conn, account_id: int, ads: Iterable[ET.Element]) -> None:
    """Rename the Ids of the renewed positions among the ads we made ourselves (a set of four is `<code>-x4`)."""
    known = reissued_own(conn, account_id)
    if not known:
        return
    for ad in ads:
        node = ad.find("Id")
        if node is None or not node.text:
            continue
        code = PACK_SUFFIX.sub("", node.text.split("_")[0])
        if code in known and not re.search(r"_r\d{8}(?!\d)", node.text):   # build_ads marked it already, or it is a renewed Id taken over
            node.text = "%s_%s" % (node.text, known[code])


def excluded_by_type(conn, account_id: int, types: Iterable[str]) -> Dict[str, str]:
    """{Id: reason} of the stored ads whose `TireType` the owner left out of this account's feed."""
    wanted = set(types)
    if not wanted:
        return {}
    out: Dict[str, str] = {}
    for ad_id, raw in conn.execute("SELECT ad_id, raw FROM imported_ad WHERE account_id = ? ORDER BY position", (account_id,)):
        kind = (ET.fromstring(raw).findtext("TireType") or "").strip()
        if kind in wanted:
            out[ad_id] = "тип «%s» исключён для аккаунта" % kind
    return out


def _adopted_key(account_id: int) -> str:
    return "adopted_%d" % account_id


def adopt_plan(conn, account_id: int) -> Dict[str, Dict[str, str]]:
    """What a move of the account to our own assembly keeps: {"ids": {code: his Id}, "images": {code: his photo link}}. Only single tires (a code
    that appears once); sets of four are not published by us."""
    ids: Dict[str, str] = {}
    images: Dict[str, str] = {}
    seen: Dict[str, int] = {}
    for ad_id, raw in conn.execute("SELECT ad_id, raw FROM imported_ad WHERE account_id = ? ORDER BY position", (account_id,)):
        if PACK_SUFFIX.search(str(ad_id).split("_")[0]):
            continue
        code = supplier_code(ad_id)
        seen[code] = seen.get(code, 0) + 1
        ids.setdefault(code, ad_id)
        image = ET.fromstring(raw).find("Images/Image")
        if image is not None and (image.get("url") or "").startswith(("http://", "https://")):
            images.setdefault(code, image.get("url"))
    dup = {c for c, n in seen.items() if n > 1}
    return {"ids": {c: i for c, i in ids.items() if c not in dup}, "images": {c: u for c, u in images.items() if c not in dup}}


def rebind_ids(conn, account_id: int, feed_ids: Iterable[str], cabinet_by_code: Dict[str, List[str]], now: str, author: str,
               shared: Optional[Dict[str, str]] = None, taken: Iterable[str] = ()) -> Dict[str, str]:
    """Positions whose Id in our feed differs from the Id of the ad the cabinet already has for the same product: Avito takes the new Id for a repeat
    («you already sell this; the first ad is published again») and blocks it. The position is bound to the Id of the existing ad instead, so Avito
    edits that ad. `cabinet_by_code` is {supplier code: [XML Ids of the cabinet's live ads, oldest first]}. Returns {code: Id} of what changed."""
    ids, images = adopted_maps(conn, account_id)
    shared_now = _shared_raw(conn, account_id)
    taken = set(taken)
    shared = shared or {}                              # {code: date until which the Id may be the same as another account's}
    changed: Dict[str, str] = {}
    for current in feed_ids:
        if PACK_SUFFIX.search(str(current).split("_")[0]):
            continue
        code = supplier_code(current)
        existing = [e for e in (cabinet_by_code.get(code) or []) if e not in taken or code in shared]
        if existing and current not in existing:
            changed[code] = existing[0]
    if changed:
        ids.update(changed)
        shared_now.update({c: shared[c] for c in changed if c in shared})
        store.set_state(conn, _adopted_key(account_id), json.dumps({"ids": ids, "images": images, "shared": shared_now}, ensure_ascii=False))
        store.write_audit(conn, now, author, "ids_rebound", {"account_id": account_id, "count": len(changed), "shared": sorted(c for c in changed if c in shared)})
    return changed


def _adopted_raw(conn, account_id: int) -> dict:
    raw = store.get_state(conn, _adopted_key(account_id))
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


def _shared_raw(conn, account_id: int) -> Dict[str, str]:
    return dict(_adopted_raw(conn, account_id).get("shared") or {})


def code_for_id(conn, account_id: int, ad_id: str) -> Optional[str]:
    """The code of the position that carries `ad_id` when that Id was inherited from another lot of the same tire (see same_tire.py); None
    when the Id is the position's own. Only a position that is published now counts: a binding of a lot that is gone says nothing."""
    ids, _images = adopted_maps(conn, account_id)
    for code, bound in ids.items():
        if bound == ad_id and supplier_code(ad_id) != code:
            if conn.execute("SELECT 1 FROM published_state WHERE account_id = ? AND goods_id = ?", (account_id, code)).fetchone():
                return code
    return None


def bind_ids(conn, account_id: int, new_ids: Dict[str, str], now: str, author: str) -> None:
    """Remember {code: Id} for an account (the tire keeps the Id of its oldest ad in the cabinet)."""
    data = _adopted_raw(conn, account_id)
    ids = dict(data.get("ids") or {})
    changed = {c: i for c, i in new_ids.items() if ids.get(c) != i}
    if not changed:
        return
    ids.update(changed)
    store.set_state(conn, _adopted_key(account_id), json.dumps({"ids": ids, "images": data.get("images") or {}, "shared": data.get("shared") or {}}, ensure_ascii=False))
    store.write_audit(conn, now, author, "ids_bound", {"account_id": account_id, "ids": changed})


def _titles_key(account_id: int) -> str:
    return "prebind_titles_%d" % account_id


def set_bound_titles(conn, account_id: int, titles: Dict[str, str]) -> None:
    """Id -> the title the live ad has on Avito, for the Ids a position took over before publication (the published file does not know them yet):
    the ad keeps its title (owner, 2026-10-14 and 2026-10-19)."""
    store.set_state(conn, _titles_key(account_id), json.dumps(dict(bound_titles(conn, account_id), **titles), ensure_ascii=False))


def _manual_key(account_id: int) -> str:
    return "manual_ads_%d" % account_id


def manual_ads(conn, account_id: int) -> List[dict]:
    """[{avito_id, title}]: the live ads of the cabinet that have no Id from our file (made or switched on by hand), as the last cycle read them."""
    try:
        return list(json.loads(store.get_state(conn, _manual_key(account_id)) or "[]"))
    except (ValueError, TypeError):
        return []


def _manual_entry(ad: dict) -> dict:
    entry = {"avito_id": int(ad["avito_id"]), "title": str(ad.get("title") or "")}
    if ad.get("ad_id"):                                                   # switched on by hand under an Id of a file (owner, 2026-10-22)
        entry.update(ad_id=str(ad["ad_id"]), code=str(ad.get("code") or supplier_code(ad["ad_id"])))
    return entry


def set_manual_ads(conn, account_id: int, ads: Iterable[dict]) -> bool:
    """Remember the manual ads of the cabinet (owner, 2026-10-19: a manual ad stays, our feed does not put the same tire next to it). Returns
    whether the list changed."""
    new = sorted((_manual_entry(a) for a in ads), key=lambda a: a["avito_id"])
    if new == manual_ads(conn, account_id):
        return False
    store.set_state(conn, _manual_key(account_id), json.dumps(new, ensure_ascii=False))
    return True


def manual_keys(conn, account_id: int, by_hand: bool = False) -> Dict[tuple, dict]:
    """{tire key: manual ad} of the manual ads whose title names the tire (`reconcile.key_title`); a title that does not is left out. The ads
    without an Id from a file, or with `by_hand` the ones switched on by hand under such an Id (their tire is only not published anew)."""
    from . import reconcile
    out: Dict[tuple, dict] = {}
    for ad in manual_ads(conn, account_id):
        if bool(ad.get("ad_id")) != by_hand:
            continue
        key = reconcile.key_title(ad.get("title") or "")
        if key:
            out.setdefault(key, ad)
    return out


def manual_codes(conn, account_id: int) -> FrozenSet[str]:
    """The codes of the positions whose own Id a manual ad has (switched on by hand under an Id of a file): never published next to it."""
    return frozenset(a["code"] for a in manual_ads(conn, account_id) if a.get("code"))


REENABLED_ACCOUNTS = frozenset({2})       # owner, 2026-10-22: account 2, the cabinet with the ads of the old files of others
CLOCK_SLACK = timedelta(minutes=5)        # the clocks of Avito and of our server


def _stray_key(account_id: int) -> str:
    return "manual_stray_%d" % account_id


def stray_seen(conn, account_id: int) -> Dict[str, dict]:
    """{avito_id: {ad_id, since, confirmed?}} of the live ads seen out of our published file (`reenabled_ads`)."""
    try:
        seen = json.loads(store.get_state(conn, _stray_key(account_id)) or "{}")
    except (ValueError, TypeError):
        return {}
    return dict(seen) if isinstance(seen, dict) else {}


def reenabled_ads(conn, account_id: int, cabinet: Dict[int, dict], published: Iterable[str], left_at: Dict[str, datetime],
                  upload_started: Optional[datetime], now: datetime) -> List[dict]:
    """[{avito_id, title, ad_id, code}]: the live ads of the cabinet that were switched on by hand under an Id of a file (owner, 2026-10-22).
    Such an ad is live, its Id is not in our published file, and the last successful upload of Avito started after we first saw that AND after
    our feed last took it (or its position) off (`left_at`, {Id or "goods:<code>": when}, from the memory of retired ads: the Id may have been
    back in a file published between two looks). Then Avito took a file without it and the ad stays: our own ad that Avito has not taken off
    yet is not one.
    Once confirmed, an ad stays one for as long as it lives out of our file (also when Avito leaves its Id out of one answer); gone from the
    live ads or back in our file, it is forgotten. `cabinet` is {avito_id: {status, title, ad_id}}; the times are Moscow time. Only for
    REENABLED_ACCOUNTS."""
    if account_id not in REENABLED_ACCOUNTS:
        return []
    published = set(published)
    seen = stray_seen(conn, account_id)
    inverse = {v: k for k, v in adopted_maps(conn, account_id)[0].items()}
    kept: Dict[str, dict] = {}
    out: List[dict] = []
    for avito_id, item in sorted(cabinet.items()):
        entry = seen.get(str(avito_id))
        entry = entry if isinstance(entry, dict) else {}
        ad_id = item.get("ad_id") or entry.get("ad_id")                   # Avito left the Id out of this answer: the one seen before stays
        if item.get("status") != "active" or not ad_id or ad_id in published:
            continue
        try:
            since = datetime.fromisoformat(entry["since"]) if entry.get("ad_id") == ad_id else None
        except (KeyError, TypeError, ValueError):
            since = None
        if since is None:                                                 # first seen out of our file (or under another Id): the wait starts now
            entry, since = {"ad_id": ad_id, "since": now.isoformat(timespec="seconds")}, now
        start = max([since] + [t for t in (left_at.get(ad_id), left_at.get("goods:" + (inverse.get(ad_id) or supplier_code(ad_id)))) if t])
        if not entry.get("confirmed") and upload_started is not None and upload_started > start + CLOCK_SLACK:
            entry = dict(entry, confirmed=True)
        kept[str(avito_id)] = entry
        if entry.get("confirmed"):
            out.append({"avito_id": avito_id, "title": item.get("title") or "", "ad_id": ad_id, "code": inverse.get(ad_id) or supplier_code(ad_id)})
    if kept != seen:
        store.set_state(conn, _stray_key(account_id), json.dumps(kept, ensure_ascii=False))
    return out


def bound_titles(conn, account_id: int) -> Dict[str, str]:
    try:
        return dict(json.loads(store.get_state(conn, _titles_key(account_id)) or "{}"))
    except (ValueError, TypeError):
        return {}


def shared_codes(conn, account_id: int) -> FrozenSet[str]:
    """The codes of an account whose Id is shared with another account until a date (agreed in advance): never bound to another Id meanwhile."""
    return frozenset((_adopted_raw(conn, account_id).get("shared") or {}).keys())


def adopted_maps(conn, account_id: int, today=None) -> Tuple[Dict[str, str], Dict[str, str]]:
    """({code: Id}, {code: photo link}) of an account whose ads were bound to Ids the cabinet already has. An Id that is shared with another
    account only until a date stops being used from that date: the position then gets its own Id again."""
    data = _adopted_raw(conn, account_id)
    ids = dict(data.get("ids") or {})
    today_iso = (today or date.today()).isoformat()
    for code, until in (data.get("shared") or {}).items():
        if until <= today_iso:
            ids.pop(code, None)
    return ids, dict(data.get("images") or {})


def shared_ids(conn, account_id: int, today=None) -> FrozenSet[str]:
    """The Ids this account is allowed to share with another account right now."""
    data = _adopted_raw(conn, account_id)
    ids = adopted_maps(conn, account_id, today)[0]
    return frozenset(ids[c] for c in (data.get("shared") or {}) if c in ids)


def adopt(conn, account_id: int, now: str, author: str) -> Dict[str, int]:
    """Move the account from the mirror to our own assembly: the Ids and photo links of his ads are remembered and kept (Avito sees the same ads),
    the file stays stored (nothing is deleted). Returns the counts."""
    account = conn.execute("SELECT feed_mode FROM account WHERE id = ?", (account_id,)).fetchone()
    if account is None:
        raise ValueError("Такого аккаунта нет.")
    if account[0] != "mirror":
        raise ValueError("Аккаунт уже не в режиме «зеркало».")
    plan = adopt_plan(conn, account_id)
    if not plan["ids"]:
        raise ValueError("В файле аккаунта нет объявлений, которые можно перенести.")
    kept = _adopted_raw(conn, account_id)                         # bindings made before (a tire that took the Id of its oldest ad) win over the file's
    merged = {"ids": dict(plan["ids"], **(kept.get("ids") or {})), "images": dict(plan["images"], **(kept.get("images") or {})),
              "shared": kept.get("shared") or {}}
    store.set_state(conn, _adopted_key(account_id), json.dumps(merged, ensure_ascii=False))
    with conn:
        conn.execute("UPDATE account SET feed_mode = 'generated' WHERE id = ?", (account_id,))
    store.write_audit(conn, now, author, "mirror_adopt", {"account_id": account_id, "ids": len(plan["ids"]), "images": len(plan["images"])})
    return {"ids": len(plan["ids"]), "images": len(plan["images"])}


def duplicate_positions(conn, account_id: int) -> List[Tuple[str, str]]:
    """The (supplier code, pack) that more than one stored ad carries: Avito treats the same product twice as a repeat and takes one down.
    They are only reported: the mirror never drops an ad of the file."""
    seen: Dict[Tuple[str, str], int] = {}
    for ad_id in stored_ids(conn, account_id):
        found = PACK_SUFFIX.search(str(ad_id).split("_")[0])
        key = (supplier_code(ad_id), "x%d" % (int(found.group(0)[2:]) if found else 1))
        seen[key] = seen.get(key, 0) + 1
    return sorted(k for k, n in seen.items() if n > 1)


def stored_ids(conn, account_id: int) -> List[str]:
    return [r[0] for r in conn.execute("SELECT ad_id FROM imported_ad WHERE account_id = ? ORDER BY position", (account_id,))]


PACK_SUFFIX = re.compile(r"-x\d+$")


def _alias_key(account_id: int) -> str:
    return "id_scheme_%d" % account_id


def alias_scheme(conn, account_id: int) -> bool:
    """The account's ads carry Ids that have nothing of the supplier's code in them (owner, 2026-10-15): `alias_id`."""
    return store.get_state(conn, _alias_key(account_id)) == "alias"


def set_alias_scheme(conn, account_id: int, on: bool, now: str, author: str) -> None:
    if on and not alias_scheme(conn, account_id):                         # a fresh start: the older bindings of this account (`<code>_aN`, Ids of other codes) go
        data = _adopted_raw(conn, account_id)
        store.set_state(conn, _adopted_key(account_id), json.dumps(
            {"ids": {}, "images": data.get("images") or {}, "shared": {}}, ensure_ascii=False))
    store.set_state(conn, _alias_key(account_id), "alias" if on else "")
    store.write_audit(conn, now, author, "id_scheme", {"account_id": account_id, "scheme": "alias" if on else "code"})


def alias_id(account_id: int, code: str) -> str:
    """The Id of a position in an account with the alias scheme: a letter and 11 letters and digits, the same every cycle, different for every
    account, no supplier code in it and no `_` (so nothing else in the system takes a code out of it)."""
    import hashlib
    number = int(hashlib.sha256(("tires-alias-v1|%d|%s" % (account_id, code)).encode("utf-8")).hexdigest()[:20], 16)
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while len(out) < 11:
        number, rest = divmod(number, 36)
        out += digits[rest]
    return "k" + out


def supplier_code(ad_id: str) -> str:
    """The supplier's code behind an ad Id: `<code>`, `<code>-x4` or `<code>_<stamp>`."""
    return PACK_SUFFIX.sub("", str(ad_id).split("_")[0])


def unavailable(conn, account_id: int, offers: Dict[str, object], min_stock: int) -> Dict[str, str]:
    """The stored ads that must not be published now: the supplier does not have the position, or has fewer than the threshold. {Id: reason}."""
    out: Dict[str, str] = {}
    for ad_id in stored_ids(conn, account_id):
        offer = offers.get(supplier_code(ad_id))
        if offer is None:
            out[ad_id] = "нет у поставщика"
        elif offer.stock < min_stock:
            out[ad_id] = "остаток %d меньше порога %d" % (offer.stock, min_stock)
    return out


def pack_count(ad_id: str) -> int:
    match = PACK_SUFFIX.search(str(ad_id))
    return int(match.group(0)[2:]) if match else 1


def prices_for(conn, account_id: int, offers: Dict[str, object], settings) -> Dict[str, int]:
    """The price of every stored ad that the supplier has, by the account's own price of the position, else its pricing rule (a pack costs its
    count of pieces).
    An ad without a rule or with a bad supplier price gets no entry and keeps the price it has. A fall of the supplier's price of 5 % and more
    is priced from the anchor (price_guard), as in our own feeds."""
    from . import price_guard
    guard = price_guard.load(conn)
    out: Dict[str, int] = {}
    for ad_id in stored_ids(conn, account_id):
        offer = offers.get(supplier_code(ad_id))
        base = getattr(offer, "base_price", 0)
        if offer is None or not base or base <= 0:
            continue
        held, _why = price_guard.base_for(guard, supplier_code(ad_id), float(base))
        if held != float(base):
            offer = dataclasses.replace(offer, base_price=held)
        price = position_price(offer, settings)                              # the account's own price of the position first
        if price is None:
            continue
        out[ad_id] = price * pack_count(ad_id)
    return out


def runflat_for(conn, account_id: int, offers: Dict[str, object]) -> Dict[str, str]:
    """RunFlat of every stored ad that the supplier has: «Да» or «Нет», sent explicitly so Avito does not guess."""
    out: Dict[str, str] = {}
    for ad_id in stored_ids(conn, account_id):
        offer = offers.get(supplier_code(ad_id))
        flag = getattr(offer, "runflat", None)
        if offer is not None and flag is not None:
            out[ad_id] = "Да" if flag else "Нет"
    return out


def _model_status(brand_text: str, model_text: str, ref: Dict[str, FrozenSet[str]], confirmed: Dict[Tuple[str, str], str],
                  brand_map: Dict[str, str], norm_cache: Dict[str, FrozenSet[str]]) -> Tuple[Optional[str], Optional[str], str]:
    """(Avito brand, Avito model to send, state) of one ad: `ok` the reference has the model, `mapped` a replacement was confirmed,
    `pending` neither, `skip` nothing to judge: no model, a brand the reference does not know, or a brand written differently from the
    reference one (the `Brand` text stays as written, so that is a brand problem and a model replacement would not fix the ad)."""
    brand = resolve_brand(brand_text.upper(), ref, brand_map) if brand_text else None
    if brand is None or not model_text or brand.upper() != brand_text.upper():
        return brand, None, "skip"
    if brand not in norm_cache:
        norm_cache[brand] = frozenset(normalize_model(m) for m in ref.get(brand, ()))
    wanted = normalize_model(model_text)
    if wanted in norm_cache[brand]:
        return brand, None, "ok"
    target = confirmed.get((brand, wanted))
    if target in ref.get(brand, ()):
        return brand, target, "mapped"
    return brand, None, "pending"


def _judged(conn, account_id: int, ref: Dict[str, FrozenSet[str]]):
    """(ad_id, Avito brand, model as written, replacement, state) for every stored ad."""
    confirmed, brand_map = store.load_confirmed_mappings(conn), store.load_brand_map(conn)
    norm_cache: Dict[str, FrozenSet[str]] = {}                                    # the normalized models of a brand, once for all its ads
    for ad_id, raw in conn.execute("SELECT ad_id, raw FROM imported_ad WHERE account_id = ? ORDER BY position", (account_id,)):
        ad = ET.fromstring(raw)
        brand_text, model_text = (ad.findtext("Brand") or "").strip(), " ".join((ad.findtext("Model") or "").split())
        brand, target, state = _model_status(brand_text, model_text, ref, confirmed, brand_map, norm_cache)
        yield ad_id, brand, model_text, target, state


def models_for(conn, account_id: int, ref: Dict[str, FrozenSet[str]]) -> Dict[str, str]:
    """{Id: the Avito model} for the stored ads whose model is not in the reference but has a replacement in the mapping table
    (confirmed one by one or accepted in bulk on the «Сопоставления» page; the table is shared with the supplier feed)."""
    return {ad_id: target for ad_id, _brand, _model, target, state in _judged(conn, account_id, ref) if state == "mapped"}


def unmatched_models(conn, account_id: int, ref: Dict[str, FrozenSet[str]]) -> List[dict]:
    """The (brand, model) pairs of the stored ads that Avito would not find in its reference and that have no replacement yet.
    [{avito_brand, model, ad_ids}], by brand and model; spelling variants (case, spaces, dashes) are one pair."""
    pairs: Dict[Tuple[str, str], dict] = {}
    for ad_id, brand, model, _target, state in _judged(conn, account_id, ref):
        if state != "pending":
            continue
        item = pairs.setdefault((brand, normalize_model(model)), {"avito_brand": brand, "model": model, "ad_ids": []})
        item["ad_ids"].append(ad_id)
    return sorted(pairs.values(), key=lambda p: (p["avito_brand"], p["model"]))


_PRICE_LINE = re.compile(r"(Цена:\s*)\d+(\s*руб)")


def render(conn, account_id: int, today: date, settings, skip: Iterable[str] = (), prices: Dict[str, int] = None,
           extra_ads: Iterable[ET.Element] = (), runflat: Dict[str, str] = None, models: Dict[str, str] = None,
           contact_phone: str = "", own_photos=None, offers: Dict[str, object] = None) -> bytes:
    """The feed for the account: the stored ads in their order (except `skip`), the end date moved like in our own feeds (so no ad runs out),
    the price from `prices` where there is one (also the figure in the text, if the text names it). With `own_photos` (the switch is on)
    our approved frames of the position (`offers`) come first, then the file's own photo links."""
    skipped = set(skip)
    prices = prices or {}
    runflat = runflat or {}
    models = models or {}
    end = stable_date_end(today, settings.date_end_days, settings.date_end_renew_days).isoformat()
    root = ET.Element("Ads", {"formatVersion": "3", "target": "Avito.ru"})
    for ad_id, raw in conn.execute("SELECT ad_id, raw FROM imported_ad WHERE account_id = ? ORDER BY position", (account_id,)):
        if ad_id in skipped:
            continue
        ad = ET.fromstring(raw)
        for name in OBSOLETE_AD_TAGS:
            for node in ad.findall(name):
                ad.remove(node)
        sale_terms(ad)
        if ad_id in runflat:
            node = ad.find("RunFlat")
            if node is None:
                node = ET.SubElement(ad, "RunFlat")
            node.text = runflat[ad_id]
        if contact_phone:                                         # the phone of the Avito account, not the one of the file's author
            node = ad.find("ContactPhone")
            if node is None:
                node = ET.SubElement(ad, "ContactPhone")
            node.text = contact_phone
        if ad_id in models:                                       # a model outside the reference, replaced by the one the owner confirmed
            node = ad.find("Model")
            if node is None:
                node = ET.SubElement(ad, "Model")
            node.text = models[ad_id]
        node = ad.find("DateEnd")
        if node is None:
            node = ET.SubElement(ad, "DateEnd")
        node.text = end
        price = prices.get(ad_id)
        if price:
            price_node = ad.find("Price")
            if price_node is None:
                price_node = ET.SubElement(ad, "Price")
            price_node.text = str(price)
            text_node = ad.find("Description")
            if text_node is not None and text_node.text:
                text_node.text = _PRICE_LINE.sub(lambda m: "%s%d%s" % (m.group(1), price, m.group(2)), text_node.text)
        if own_photos is not None:
            ours = photo_library.image_urls(own_photos, (offers or {}).get(supplier_code(ad_id)), pack_count(ad_id) == 4, None)
            if ours:
                images = ad.find("Images")
                if images is None:
                    images = ET.SubElement(ad, "Images")
                theirs = [node.get("url") for node in images.findall("Image") if node.get("url")]
                for node in images.findall("Image"):
                    images.remove(node)
                for url in list(dict.fromkeys(ours + theirs))[:photo_library.MAX_IMAGES]:
                    ET.SubElement(images, "Image", url=url)
        root.append(ad)
    for ad in extra_ads:                                         # the positions the file did not have, made by our own rules
        for name in OBSOLETE_AD_TAGS:
            for node in ad.findall(name):
                ad.remove(node)
        sale_terms(ad)                                           # the file's shape may have brought its own MultiItem and minimum order
        if contact_phone:                                        # they took the file's phone with the rest of the shape: the account's own wins
            node = ad.find("ContactPhone")
            if node is None:
                node = ET.SubElement(ad, "ContactPhone")
            node.text = contact_phone
        root.append(ad)
    if len(root) == 0:
        raise MirrorError("Для аккаунта не загружен файл: нечего отдавать Авито.")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def write_candidate(conn, account: dict, feed_root, today: date, settings, skip: Iterable[str] = (), prices: Dict[str, int] = None,
                    extra_ads: Iterable[ET.Element] = (), runflat: Dict[str, str] = None, models: Dict[str, str] = None,
                    contact_phone: str = "", own_photos=None, offers: Dict[str, object] = None) -> int:
    """Write the account's mirror feed to its private `next/` folder (it becomes public the usual way). Returns the number of ads."""
    data = render(conn, account["id"], today, settings, skip=skip, prices=prices, extra_ads=extra_ads, runflat=runflat,
                  models=models, contact_phone=contact_phone, own_photos=own_photos, offers=offers)
    folder = Path(feed_root) / account["feed_token"] / "next"
    folder.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(folder), prefix="avito_feed.", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(tmp, 0o640)
        os.replace(tmp, str(folder / "avito_feed.xml"))
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return len(ET.fromstring(data))


def missing_ids(conn, account_id: int, feed_bytes: bytes, skip: Iterable[str] = ()) -> List[str]:
    """The stored ads that a feed does not carry (except those left out on purpose): must be empty before the feed goes to Avito."""
    left_out = set(skip)
    try:
        present = {(a.findtext("Id") or "").strip() for a in ET.fromstring(feed_bytes).findall("Ad")}
    except ET.ParseError:
        return [i for i in stored_ids(conn, account_id) if i not in left_out]
    return [i for i in stored_ids(conn, account_id) if i not in present and i not in left_out]
