"""The prices of the competitors on Avito for our positions (docs/plans/price-parser.md, stage 0: no network here, ever).

The Mac (the owner 2026-10-30: the parser runs there, with the home internet) sends what one search of a tyre size showed: the listings with
their price and the fields of the card. The server matches them with every position of ours of that size (the truth about our positions is
here): the hard features (brand, model with its synonyms, size, season and studs, «new», not our own ad, not older than 30 days) or the
listing is no competitor; the soft ones (the indexes ±1 step, RunFlat, XL, an old year) lower its weight. The price is for one tyre. Fewer
than three reliable analogues: «мало данных», nothing to compare with. The outliers are cut by the interquartile range; the guide is the
lower quartile and the median, never the minimum.

No personal data: no phones, no names of private sellers (only «частник»), the name of a shop only. Nothing here touches the feed or a price."""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

from .catalog import resolve_brand, resolve_model
from .tire_match import brand_form, model_form

MAX_AGE_DAYS = 30                                  # an older listing is not taken (the plan, section 2)
RELIABLE = 0.75                                    # the weight of an analogue that counts in the summary
MIN_ANALOGUES = 3                                  # fewer: «insufficient», the markup is not touched
OLD_YEARS = 3                                      # a tyre made three years and more before the run: weight lowered
KEEP_LISTINGS_DAYS, KEEP_SNAPSHOTS_DAYS = 60, 365
SPEEDS = "JKLMNPQRSTUHVWY"                         # the speed indexes in order (H stands between U and V)
SLOTS = ("morning", "evening")
STATUSES = ("ok", "insufficient")                 # of a snapshot; a run stopped or failed is said in its `note`
PRIVATE = "частник"
MAX_LOT = 8
TIRE_TYPES = {"winter": {True: "Зимние шипованные", False: "Зимние нешипованные"}, "summer": "Летние", "all": "Всесезонные"}
NEW_WORDS = ("new", "новое", "новые", "новая", "новый")


@dataclass(frozen=True)
class Ours:
    """A position of ours as Avito names it (the brand and the model of the Avito reference, as in our ads)."""
    goods_id: str
    brand: str
    model: str
    width: int
    height: int
    diameter: int
    tire_type: str
    load: str
    speed: str
    runflat: bool
    xl: bool
    price: Optional[float] = None                  # our price of the position (one tyre), for «the place» only


def ours_of(offer, avito_brand: str, avito_model: str, price=None) -> Ours:
    from .feed import _tire_type
    return Ours(offer.goods_id, avito_brand, avito_model, int(offer.width), int(offer.height), int(offer.diameter),
                _tire_type(offer.season, bool(offer.studded)), str(offer.load or "").split("/")[0].strip(), str(offer.speed or "").strip().upper(),
                bool(offer.runflat), bool(getattr(offer, "xl", False)), price)


def ours_from_parsed(offers: Dict[str, object], ref, brand_map, confirmed, wanted: Iterable[str], prices: Optional[Dict[str, float]] = None) -> Dict[str, Ours]:
    """Our positions `wanted` (the codes of the feed) that Avito can name: a brand or a model not in the reference has no competitors to find."""
    found = {}
    for goods_id in wanted:
        offer = offers.get(goods_id)
        if offer is None:
            continue
        brand = resolve_brand(offer.brand_key, ref, brand_map)
        model = resolve_model(brand, offer.model_raw, confirmed, ref) if brand else None
        if brand and model:
            found[goods_id] = ours_of(offer, brand, model, (prices or {}).get(goods_id))
    return found


# ---- a listing of a competitor -------------------------------------------------------------------------------------------------------

def _int(value) -> Optional[int]:
    text = str(value if value is not None else "").strip()
    return int(text) if text.isascii() and text.isdigit() and len(text) <= 12 else None     # «²» is a digit to isdigit, never to int


def size_of(raw: dict) -> Optional[Tuple[int, int, int]]:
    """(width, height, diameter) of a search or a listing; «R16» is 16."""
    width, height, diameter = _int(raw.get("width")), _int(raw.get("height")), _int(str(raw.get("diameter") or "").strip().upper().lstrip("R"))
    return (width, height, diameter) if width and height and diameter else None


PHONE = re.compile(r"(?:\+7|\b8)[\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}")


def _date(value) -> Optional[datetime]:
    """The date the listing was placed: ISO, or «25.10.2026» as the card writes it; None when it cannot be read."""
    text = str(value or "").strip()
    for form in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(text[:19] if "T" in text else text[:10], form)
        except ValueError:
            continue
    return None


def _price(value) -> Optional[float]:
    text = re.sub(r"[\s ₽]", "", str(value if value is not None else "")).replace(",", ".")
    try:
        number = float(text)
    except ValueError:
        return None
    return number if 0 < number < 10_000_000 else None


def _tire_type(raw: dict) -> str:
    """«Зимние шипованные» and the like: the card's own text, else from the season and the studs."""
    text = str(raw.get("tire_type") or "").strip()
    if text:
        return text[:40]
    season = str(raw.get("season") or "").strip().lower()
    kind = TIRE_TYPES.get(season, "")
    if isinstance(kind, dict):                                      # a winter tyre with its studs not said: not known, no competitor
        return kind[bool(raw["studded"])] if raw.get("studded") is not None else ""
    return kind


def parse_listing(raw: dict) -> Optional[dict]:
    """The fields of a listing the Mac sent that we keep, cleaned; None when it has no number or no price. Phones, the names of private
    sellers and any field not listed here are never kept."""
    if not isinstance(raw, dict):
        return None
    avito_id, price = _int(raw.get("avito_id")), _price(raw.get("price"))
    if avito_id is None or price is None:
        return None
    lot = _int(raw.get("lot")) or 1
    if lot > MAX_LOT:                                               # not a set of tyres: its price for one is not known
        return None
    year = _int(raw.get("year"))
    size = size_of(raw) or (None, None, None)
    shop = str(raw.get("seller_kind") or "").strip().lower() == "shop"
    url = str(raw.get("url") or "").strip()
    return {"avito_id": avito_id, "price": price, "lot": lot, "title": PHONE.sub("…", str(raw.get("title") or "")).strip()[:200],
            "brand": str(raw.get("brand") or "").strip()[:60], "model": str(raw.get("model") or "").strip()[:80],
            "width": size[0], "height": size[1], "diameter": size[2],
            "tire_type": _tire_type(raw), "load": str(raw.get("load") or "").split("/")[0].strip()[:6],
            "speed": str(raw.get("speed") or "").strip().upper()[:3], "runflat": bool(raw.get("runflat")), "xl": bool(raw.get("xl")),
            "condition": str(raw.get("condition") or "").strip().lower()[:20],
            "seller": str(raw.get("seller_name") or "").strip()[:80] if shop else PRIVATE,
            "seller_id": str(raw.get("seller_id") or "").strip()[:40],
            "url": url[:300] if url.startswith("https://www.avito.ru/") or url.startswith("https://avito.ru/") else "",
            "listed_at": str(raw.get("listed_at") or "").strip()[:19], "year": year if year and 1990 <= year <= 2100 else None,
            "in_stock": None if raw.get("in_stock") is None else bool(raw.get("in_stock"))}


def _model(brand: str, model: str, synonyms) -> str:
    form = model_form(model, brand)                                 # «Ikon Character Ice 8» is «Character Ice 8» of Ikon
    return (synonyms or {}).get((brand_form(brand), form), form)


def _steps(ours: str, theirs: str, scale: Optional[str]) -> Optional[int]:
    """How far apart two indexes are: the load by its number, the speed by its letter; None when one of them is not known."""
    if not ours or not theirs:
        return None
    if scale is None:
        return abs(int(ours) - int(theirs)) if ours.isdigit() and theirs.isdigit() else None
    return abs(scale.index(ours) - scale.index(theirs)) if ours in scale and theirs in scale else None


def match(ours: Ours, listing: dict, run_at: str, own_ids=frozenset(), own_sellers=frozenset(), synonyms=None) -> Tuple[float, str]:
    """(weight, '') for a competitor of our position, (0, the reason) for a listing that is not one."""
    if brand_form(listing["brand"]) != brand_form(ours.brand):
        return 0.0, "другой бренд"
    if _model(listing["brand"], listing["model"], synonyms) != _model(ours.brand, ours.model, synonyms):
        return 0.0, "другая модель"
    if (listing["width"], listing["height"], listing["diameter"]) != (ours.width, ours.height, ours.diameter):
        return 0.0, "другой размер"
    if listing["avito_id"] in own_ids or (listing["seller_id"] and listing["seller_id"] in own_sellers):
        return 0.0, "наше объявление"
    if not listing["condition"]:
        return 0.0, "состояние не указано"
    if listing["condition"] not in NEW_WORDS:
        return 0.0, "не новая"
    if listing["tire_type"] != ours.tire_type:
        return 0.0, "другой сезон или шипы"
    if listing["listed_at"]:
        placed = _date(listing["listed_at"])
        if placed is None:
            return 0.0, "дата не разобрана"
        if datetime.fromisoformat(run_at[:19]) - placed > timedelta(days=MAX_AGE_DAYS):
            return 0.0, "старше %d дней" % MAX_AGE_DAYS
    weight = 1.0
    for steps in (_steps(ours.load, listing["load"], None), _steps(ours.speed, listing["speed"], SPEEDS)):
        if steps is None or steps == 0:
            continue
        if steps > 1:
            return 0.0, "индексы дальше одной ступени"
        weight *= 0.5
    if listing["runflat"] != ours.runflat:
        weight *= 0.5
    if listing["xl"] != ours.xl:
        weight *= 0.75
    if listing["year"] and listing["year"] <= int(run_at[:4]) - OLD_YEARS:
        weight *= 0.75
    return round(weight, 3), ""


def per_unit(listing: dict) -> float:
    return round(listing["price"] / max(listing["lot"], 1), 2)


def _quartiles(values: List[float]) -> Tuple[float, float, float]:
    if len(values) == 1:
        return values[0], values[0], values[0]
    q1, q2, q3 = statistics.quantiles(values, n=4, method="inclusive")
    return q1, q2, q3


def summarize(ours: Ours, listings: List[dict], run_at: str, own_ids=frozenset(), own_sellers=frozenset(), synonyms=None) -> Tuple[dict, List[dict]]:
    """The snapshot of one position from the listings of its size, and every listing of its brand and model with its weight and the reason it
    was left out (a listing of another tyre altogether is not kept for this position)."""
    rows, used, seen = [], [], set()
    for listing in listings:
        if listing["avito_id"] in seen:                              # the same ad on two pages of the answer (VIP, promoted): once
            continue
        seen.add(listing["avito_id"])
        weight, reason = match(ours, listing, run_at, own_ids, own_sellers, synonyms)
        if reason in ("другой бренд", "другая модель", "другой размер"):
            continue
        if not reason and weight < RELIABLE:
            reason = "низкий вес"
        row = dict(listing, per_unit=per_unit(listing), weight=weight, reason=reason)
        rows.append(row)
        if not reason:
            used.append(row)
    if len(used) >= 4:                                              # the outliers by the interquartile range
        q1, _q2, q3 = _quartiles(sorted(r["per_unit"] for r in used))
        low, high = q1 - 1.5 * (q3 - q1), q3 + 1.5 * (q3 - q1)
        for r in used:
            if not low <= r["per_unit"] <= high:
                r["reason"] = "выброс"
        used = [r for r in used if not r["reason"]]
    snapshot = {"goods_id": ours.goods_id, "count": len(used), "min": None, "q1": None, "median": None, "q3": None, "max": None,
                "confidence": round(min(1.0, len(used) / 8), 2), "status": "insufficient" if len(used) < MIN_ANALOGUES else "ok", "note": ""}
    if used:
        values = sorted(r["per_unit"] for r in used)
        q1, median, q3 = _quartiles(values)
        snapshot.update(min=values[0], q1=round(q1, 2), median=round(median, 2), q3=round(q3, 2), max=values[-1])
    return snapshot, rows


def place(price, snapshot: Optional[dict]) -> str:
    """Where our price stands: «дешевле всех», «в нижней четверти», «ниже медианы», «выше медианы на 7 %»; '' when there is nothing to say."""
    if not snapshot or snapshot.get("status") != "ok" or price is None or not snapshot.get("median"):
        return ""
    if price < snapshot["min"]:
        return "дешевле всех"
    if price <= snapshot["q1"]:
        return "в нижней четверти"
    if price <= snapshot["median"]:
        return "ниже медианы"
    above = round((price - snapshot["median"]) / snapshot["median"] * 100)
    return "выше медианы на %d %%" % above if above else "на уровне медианы"


def dearer(price, snapshot: Optional[dict]) -> bool:
    """«Дороже рынка»: our price above the median of a snapshot with enough data."""
    return bool(snapshot and snapshot.get("status") == "ok" and price is not None and snapshot.get("median")
                and round((price - snapshot["median"]) / snapshot["median"] * 100) >= 1)


# ---- the base -------------------------------------------------------------------------------------------------------------------------

SNAPSHOT_COLUMNS = ("id", "goods_id", "run_at", "slot", "count", "min", "q1", "median", "q3", "max", "confidence", "status", "note")
LISTING_COLUMNS = ("avito_id", "price", "lot", "per_unit", "title", "seller", "url", "listed_at", "year", "in_stock", "weight", "reason")


def own_ids(conn) -> frozenset:
    """The numbers of the ads of our cabinets (avito_ad: what Avito says is there)."""
    return frozenset(r[0] for r in conn.execute("SELECT DISTINCT avito_id FROM avito_ad"))


def own_sellers(conn) -> frozenset:
    """The Avito user ids of our cabinets (Заман's among them: an account of the panel)."""
    from . import connections
    found = set()
    for account in connections.list_accounts(conn):
        user = str((connections.get_avito_identity(conn, account["id"]) or {}).get("id") or "").strip()
        if user:
            found.add(user)
    return frozenset(found)


def save(conn, ours: Dict[str, Ours], searches: List[dict], run_at: str, slot: str, now: str, note: str = "", synonyms=None) -> int:
    """One run (or a part of it): a snapshot for every position of ours of a size searched, with its listings. A run stopped by a captcha
    sends the searches it did finish: they count, the `note` says why it stopped. The matching is done before the base is locked; the old
    rows go by the server's `now` (60 days the listings, a year the snapshots). Returns the snapshots written. One transaction."""
    by_size: Dict[Tuple[int, int, int], List[dict]] = {}
    for search in searches:
        size = size_of(search)
        if size is None:
            continue
        found = by_size.setdefault(size, [])
        found += [p for p in (parse_listing(raw) for raw in (search.get("listings") or [])) if p and size_of(p) == size]
    mine, sellers = own_ids(conn), own_sellers(conn)
    made = [summarize(tire, by_size[(tire.width, tire.height, tire.diameter)], run_at, mine, sellers, synonyms)
            for _goods_id, tire in sorted(ours.items()) if (tire.width, tire.height, tire.diameter) in by_size]
    try:
        for snapshot, rows in made:
            cur = conn.execute("INSERT INTO market_snapshot (goods_id, run_at, slot, count, min, q1, median, q3, max, confidence, status, note) "
                               "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                               (snapshot["goods_id"], run_at, slot, snapshot["count"], snapshot["min"], snapshot["q1"], snapshot["median"],
                                snapshot["q3"], snapshot["max"], snapshot["confidence"], snapshot["status"], note[:200]))
            conn.executemany("INSERT INTO market_listing (snapshot_id, goods_id, run_at, avito_id, price, lot, per_unit, title, seller, url, listed_at, "
                             "year, in_stock, weight, reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             [(cur.lastrowid, snapshot["goods_id"], run_at, r["avito_id"], r["price"], r["lot"], r["per_unit"], r["title"], r["seller"],
                               r["url"], r["listed_at"], r["year"], None if r["in_stock"] is None else int(r["in_stock"]), r["weight"], r["reason"])
                              for r in rows])
        purge(conn, now)
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    return len(made)


def purge(conn, now: str) -> None:
    when = datetime.fromisoformat(now[:19])
    conn.execute("DELETE FROM market_listing WHERE run_at < ?", ((when - timedelta(days=KEEP_LISTINGS_DAYS)).isoformat(timespec="seconds"),))
    old = (when - timedelta(days=KEEP_SNAPSHOTS_DAYS)).isoformat(timespec="seconds")
    conn.execute("DELETE FROM market_listing WHERE snapshot_id IN (SELECT id FROM market_snapshot WHERE run_at < ?)", (old,))
    conn.execute("DELETE FROM market_snapshot WHERE run_at < ?", (old,))


def latest(conn, goods_ids: Optional[Iterable[str]] = None) -> Dict[str, dict]:
    """The last snapshot of each position (all of them, or of `goods_ids`: one index lookup each)."""
    columns = ", ".join(SNAPSHOT_COLUMNS)
    if goods_ids is None:
        rows = conn.execute("SELECT %s FROM market_snapshot WHERE id IN (SELECT MAX(id) FROM market_snapshot GROUP BY goods_id)" % columns).fetchall()
    else:
        rows = [r for r in (conn.execute("SELECT %s FROM market_snapshot WHERE goods_id = ? ORDER BY id DESC LIMIT 1" % columns, (g,)).fetchone()
                            for g in set(goods_ids)) if r]
    return {r[1]: dict(zip(SNAPSHOT_COLUMNS, r)) for r in rows}


def moscow(run_at: str) -> str:
    """The time of a run as the panel shows times (Moscow, UTC+3; the base keeps UTC)."""
    try:
        return (datetime.fromisoformat(run_at) + timedelta(hours=3)).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return run_at


def listings_of(conn, snapshot_id: int) -> List[dict]:
    """The listings of a snapshot: the analogues first (cheapest first), then those left out."""
    rows = conn.execute("SELECT %s FROM market_listing WHERE snapshot_id = ? ORDER BY reason != '', per_unit, avito_id" % ", ".join(LISTING_COLUMNS),
                        (snapshot_id,)).fetchall()
    return [dict(zip(LISTING_COLUMNS, r)) for r in rows]
