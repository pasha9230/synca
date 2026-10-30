"""«Защита от снижения цены поставщика» (the owner 2026-10-30, plan docs/runs/2026-10-30-price-guard, the case of Gislaved −23 %).

Each position (goods_id) has an anchor: the last supplier's price we accepted. A price of the supplier 5 % and more below the anchor is not
followed: our price is counted from the anchor (the markups, the account's corrections, an own price in percent still apply: the purchase is
held, not the sale price) until the owner presses «Принять» (the anchor becomes the new price) or «Оставить прежнюю» (not asked again until
the price falls 5 % further). A rise of 5 % and more moves the anchor at once and is told; within 5 % the price goes as before and the anchor
stays, so that small falls add up. The feed only: the client's catalog and the orders count from the supplier's price of today.

The anchor is kept apart from the published state: it outlives an ad taken off. With no anchor yet (the first run, a new position) the
anchor is the price of today (the first run changes no price: the review of 2026-10-30); a tire that changes supplier (one ad, same_tire)
takes the anchor of the position whose ad it takes, once.
Reading (load, base_for, apply) writes nothing: the previews use it; the cycle records (record)."""
from __future__ import annotations

import dataclasses
import hashlib
import json
from typing import Dict, Iterable, List, Optional, Tuple

PCT = 5.0                                   # the owner 2026-10-30: ±5 % and more
FIELDS = ("goods_id", "base_price", "set_at", "how", "prev", "pending", "pending_since", "kept", "kept_at", "kept_by", "told", "brand", "told_rise")
WAITS = "в ближайший цикл (до часа)"


def _low(base: float) -> float:
    return base * (1 - PCT / 100.0)


def _high(base: float) -> float:
    return base * (1 + PCT / 100.0)


def load(conn) -> dict:
    """{"anchors": goods_id → row}: one read."""
    return {"anchors": {r[0]: dict(zip(FIELDS, r)) for r in conn.execute("SELECT %s FROM price_anchor" % ", ".join(FIELDS))}}


def _anchor_of(guard: dict, gid: str, inherited: Optional[str] = None) -> Tuple[float, Optional[dict]]:
    """The anchor of `gid`: its own, unless it takes the ad of `inherited` and has only the anchor of its first sight (review 2026-10-30:
    a tire of 4 Точки seen long before it takes the WebMIM ad is still judged by that ad's anchor)."""
    row = guard["anchors"].get(gid)
    other = guard["anchors"].get(inherited) if inherited else None
    if other is not None and float(other["base_price"]) > 0 and (row is None or row["how"] == "first"):
        return float(other["base_price"]), row
    if row is not None and float(row["base_price"]) > 0:
        return float(row["base_price"]), row
    return 0.0, None


def base_for(guard: dict, gid: str, price: float, inherited: Optional[str] = None) -> Tuple[float, str]:
    """The purchase our price of `gid` is counted from at the supplier's `price`, and why: "" (the price itself), "held" (a fall waiting for
    the owner), "kept" («Оставить прежнюю»). `inherited`: the position whose ad a tire of another supplier takes (same_tire)."""
    anchor, row = _anchor_of(guard, gid, inherited)
    if anchor <= 0 or price <= 0 or price > _low(anchor):
        return float(price), ""
    if row is not None and float(row["kept"] or 0) > 0 and price > _low(float(row["kept"])):
        return anchor, "kept"
    return anchor, "held"


def _inherited(ev, gid: str) -> Optional[str]:
    import re
    ad_id = (getattr(ev, "inherit", None) or {}).get(gid)
    if not ad_id:
        return None
    code = re.sub(r"-x\d+$", "", ad_id.split("_")[0])
    return code if code != gid else None


def apply(guard: dict, ev, settings) -> None:
    """The candidates of an evaluation priced from the purchase base_for gives; `ev.held`: goods_id → (anchor, the supplier's price, why)."""
    from .pricing import position_price
    for gid, cand in list(ev.ok.items()):
        current = float(cand.offer.base_price)
        base, why = base_for(guard, gid, current, _inherited(ev, gid))
        if not why:
            continue
        price = position_price(dataclasses.replace(cand.offer, base_price=base), settings)
        if price:
            ev.ok[gid] = dataclasses.replace(cand, price=price)
            ev.held[gid] = (base, current, why)


def record(conn, offers: Dict[str, object], now: str, ev=None, live: Optional[set] = None) -> None:
    """After the cycle's plan (sync.run_cycle): the anchors of new positions, the rises, the falls waiting and the questions over. A fall is
    asked only of a position held in this account's feed (`live`: the codes the feed keeps or puts on Avito). No commit: saved with the
    cycle's own state."""
    guard = load(conn)
    held = {g for g in (getattr(ev, "held", None) or {}) if live is None or g in live}
    for gid, offer in offers.items():
        price = float(getattr(offer, "base_price", 0) or 0)
        if price <= 0:
            continue
        brand = getattr(offer, "brand_raw", "") or getattr(offer, "brand_key", "")
        row = guard["anchors"].get(gid)
        inherited = _inherited(ev, gid) if ev is not None else None
        other = guard["anchors"].get(inherited) if inherited else None
        if other is not None and float(other["base_price"]) > 0 and (row is None or row["how"] == "first"):
            if row is None:                                             # takes the ad of another position: its anchor, once
                conn.execute("INSERT INTO price_anchor (goods_id, base_price, set_at, how, brand) VALUES (?,?,?,'inherited',?)",
                             (gid, float(other["base_price"]), now, brand))
            else:
                conn.execute("UPDATE price_anchor SET base_price = ?, set_at = ?, how = 'inherited' WHERE goods_id = ?",
                             (float(other["base_price"]), now, gid))
            row = dict(row or {"pending": 0, "kept": 0, "told": "", "told_rise": ""}, base_price=float(other["base_price"]), how="inherited")
        elif row is None:                                               # first seen (also the first run): the price of today
            conn.execute("INSERT INTO price_anchor (goods_id, base_price, set_at, how, brand) VALUES (?,?,?,'first',?)", (gid, price, now, brand))
            continue
        anchor = float(row["base_price"])
        if price >= _high(anchor):
            conn.execute("UPDATE price_anchor SET base_price = ?, prev = ?, set_at = ?, how = 'rise', pending = 0, pending_since = '', kept = 0, "
                         "kept_at = '', kept_by = '', told = '', told_rise = '', brand = ? WHERE goods_id = ?", (price, anchor, now, brand, gid))
        elif price <= _low(anchor):
            kept = float(row["kept"] or 0)
            if kept > 0 and price > _low(kept):
                continue                                                # «Оставить прежнюю»: quiet until it falls 5 % further
            if gid not in held:                                         # not on Avito in this account: no question (another account may ask)
                continue
            if float(row["pending"] or 0) != price:
                conn.execute("UPDATE price_anchor SET pending = ?, pending_since = CASE WHEN pending > 0 THEN pending_since ELSE ? END, kept = 0, "
                             "kept_at = '', kept_by = '', brand = ? WHERE goods_id = ?", (price, now, brand, gid))
        elif float(row["pending"] or 0) or float(row["kept"] or 0):     # back within 5 %: the question is over
            conn.execute("UPDATE price_anchor SET pending = 0, pending_since = '', kept = 0, kept_at = '', kept_by = '', told = '' WHERE goods_id = ?",
                         (gid,))                                        # a fall again later is told again (review 2026-10-30)


class _Held:
    def __init__(self, held):
        self.held, self.inherit = held, {}


def record_mirror(conn, offers: Dict[str, object], codes: Iterable[str], now: str) -> None:
    """The ads of a mirrored account (mirror_feed.prices_for holds their price too): a fall of their positions is asked as of the feeds we
    build (review 2026-10-30: never held quietly). No commit."""
    guard = load(conn)
    mine = {c: offers[c] for c in codes if c in offers}
    held = {c: base_for(guard, c, float(getattr(o, "base_price", 0) or 0)) for c, o in mine.items()}
    held = {c: v for c, v in held.items() if v[1]}
    record(conn, mine, now, _Held(held), set(held))


# ---- the owner's decision ----------------------------------------------------------------------------------------------------------------

def waiting(conn) -> List[dict]:
    """The falls waiting for the owner and those kept, the biggest fall first."""
    rows = [dict(zip(FIELDS, r)) for r in conn.execute("SELECT %s FROM price_anchor WHERE pending > 0 OR kept > 0" % ", ".join(FIELDS))]
    for r in rows:
        r["now"] = float(r["pending"] or r["kept"])
        r["pct"] = round((r["now"] / float(r["base_price"]) - 1) * 100, 1)
    return sorted(rows, key=lambda r: (r["pending"] <= 0, r["pct"], r["goods_id"]))


def rev(conn) -> str:
    """The state of the list the page shows: a press of a page older than a change does nothing."""
    raw = json.dumps([(r["goods_id"], r["pending"], r["kept"]) for r in waiting(conn)])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _audit(conn, now: str, author: str, action: str, detail: dict) -> None:
    conn.execute("INSERT INTO audit (at, author, action, detail) VALUES (?,?,?,?)", (now, author, action, json.dumps(detail, ensure_ascii=False)))


def accept(conn, goods_ids: Iterable[str], now: str, author: str) -> int:
    """«Принять»: the anchor becomes the price waiting; the feed follows in the next cycle. Returns how many."""
    done = []
    for gid in dict.fromkeys(str(g) for g in goods_ids):
        if conn.execute("UPDATE price_anchor SET prev = base_price, base_price = CASE WHEN pending > 0 THEN pending ELSE kept END, set_at = ?, "
                        "how = 'accepted', pending = 0, pending_since = '', kept = 0, kept_at = '', kept_by = '', told = '' WHERE goods_id = ? AND (pending > 0 OR kept > 0)",
                        (now, gid)).rowcount == 1:                      # also one kept before: the owner changed the mind
            done.append(gid)
    if done:
        _audit(conn, now, author, "price_guard_accept", {"goods": done})
    conn.commit()
    return len(done)


def keep(conn, goods_ids: Iterable[str], now: str, author: str) -> int:
    """«Оставить прежнюю»: our price stays at the anchor's; asked again when the supplier's price falls 5 % further."""
    done = []
    for gid in dict.fromkeys(str(g) for g in goods_ids):
        if conn.execute("UPDATE price_anchor SET kept = pending, kept_at = ?, kept_by = ?, pending = 0 WHERE goods_id = ? AND pending > 0",
                        (now, author, gid)).rowcount == 1:
            done.append(gid)
    if done:
        _audit(conn, now, author, "price_guard_keep", {"goods": done})
    conn.commit()
    return len(done)


# ---- the summary ---------------------------------------------------------------------------------------------------------------------------

def _by_brand(rows: List[dict], sign: str) -> List[str]:
    groups: Dict[str, List[float]] = {}
    for r in rows:
        groups.setdefault(r["brand"] or "?", []).append(r["pct"])
    return ["%s: %d поз. %s%d %%" % (brand, len(p), sign, round(abs(sum(p) / len(p)))) for brand, p in sorted(groups.items())]


def news(conn) -> dict:
    """What the summary has not said yet: {"drops": lines, "rises": lines, "marks": [(goods_id, mark)]}."""
    drops, rises, marks = [], [], []
    for row in conn.execute("SELECT %s FROM price_anchor WHERE (pending > 0) OR (how = 'rise' AND prev > 0)" % ", ".join(FIELDS)).fetchall():
        r = dict(zip(FIELDS, row))
        if float(r["pending"] or 0) > 0:
            said = float(r["told"][5:]) if str(r["told"]).startswith("drop:") else 0.0
            if not said or float(r["pending"]) <= _low(said):           # told once; again only 5 % further down
                drops.append(dict(r, pct=(float(r["pending"]) / float(r["base_price"]) - 1) * 100))
                marks.append((r["goods_id"], "told", "drop:%g" % float(r["pending"])))
        if r["how"] == "rise" and float(r["prev"] or 0) > 0:
            mark = "rise:%g" % float(r["base_price"])
            if r["told_rise"] != mark:
                rises.append(dict(r, pct=(float(r["base_price"]) / float(r["prev"]) - 1) * 100))
                marks.append((r["goods_id"], "told_rise", mark))
    return {"drops": _by_brand(drops, "−"), "rises": _by_brand(rises, "+"), "marks": marks}


def told(conn, found: dict) -> None:
    """The summary went: its changes are not said again."""
    for gid, column, mark in found["marks"]:
        conn.execute("UPDATE price_anchor SET %s = ? WHERE goods_id = ?" % ("told_rise" if column == "told_rise" else "told"), (mark, gid))
    conn.commit()


def text(found: dict, link: str) -> str:
    parts = []
    if found["drops"]:
        parts.append("Tires Sync: цена поставщика снизилась на 5 %% и больше — наша цена на Авито оставлена прежней до вашего решения:\n%s\n"
                     "Принять или оставить: %s" % ("\n".join(found["drops"]), link))
    if found["rises"]:
        parts.append("Tires Sync: цена поставщика выросла на 5 %% и больше — наша цена поднята:\n%s" % "\n".join(found["rises"]))
    return "\n\n".join(parts)
