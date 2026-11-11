"""«Аналитика рекламы» (the owner 2026-11-11, stage 1: read only): what Avito takes for each ad of the feed accounts (placement and
promotion together), its views and contacts, the sales of the ad and the share of the spending in the revenue and the margin (ДРР);
the current price of a view and the options Avito offers. Nothing here writes to Avito.

Collection: stats v2 «itemAnalytics» grouped by ad, one request for one day (the table keeps days: 7 / 30 / 90 are sums of days), at most one
request a minute; the first runs fill the last HISTORY_DAYS days DAYS_PER_RUN at a time, newest first, then «yesterday» and the day before
(Avito corrects the recent days). The prices of a view (cpxpromo) once a day for the ads promoted in the last 30 days.
The shapes of Avito's answers have not been seen live: they are read leniently, and the first answer of each method is kept (its keys,
never its values) in the state `ads_spend_shape:<method>` and printed by the command."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from . import connections, store

METRICS = ("allSpending", "presenceSpending", "promoSpending", "views", "contacts")
HISTORY_DAYS = 90
KEEP_DAYS = 400
PAGE = 1000                 # itemAnalytics: ads per answer at most
DAYS_PER_RUN = 10           # days of the backfill in one run (a minute each): the 2-hourly runs fill 90 days within a day
REFRESH_DAYS = 2            # the daily run reads yesterday and the day before again
STATS_PAUSE = 61.0          # itemAnalytics: one request a minute per method (Avito)
PROMO_CHUNK = 200           # getPromotionsByItemIds: ads per request
PROMO_PAUSE = 6.0           # its limit is not written: ten a minute, to be safe
BIDS_PAUSE = 3.1            # getBids: 20 a minute
BIDS_PER_RUN = 60
BID_DAYS = 30               # the ads promoted within these days get their prices of a view read
RATE_PAUSE = 600            # seconds without requests of the account after Avito answered 429 through all the repeats
PERIODS = (7, 30, 90)
ROWS_MAX = 300              # rows of one account on the page
SORTS = ("spend", "drr", "drr_margin")
MARK = 0                    # avito_id of the row «the day was read»


class Pacer:
    """Keeps the pause between two requests of one method (Avito's limits): sleeps the rest of `gap` since the last request of `key`."""

    def __init__(self, now: Callable[[], float], sleep: Callable[[float], None]):
        self._now, self._sleep, self._last = now, sleep, {}

    def wait(self, key: str, gap: float) -> None:
        last = self._last.get(key)
        if last is not None:
            left = last + gap - self._now()
            if left > 0:
                self._sleep(left)
        self._last[key] = self._now()


# ---- reading Avito's answers ---------------------------------------------------------------------------------------------------------

def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _rub(penny) -> Optional[float]:
    if penny is None or isinstance(penny, bool):
        return None
    try:
        return round(float(penny) / 100, 2)
    except (TypeError, ValueError):
        return None


def _item_id(entry: dict) -> Optional[int]:
    for key in ("id", "itemId", "itemID", "item_id"):
        value = entry.get(key)
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            return number
    return None


def _metrics(entry: dict) -> Dict[str, float]:
    raw = entry.get("metrics")
    if isinstance(raw, dict):
        return {str(k): _num(v) for k, v in raw.items()}
    if isinstance(raw, list):
        out = {}
        for m in raw:
            if isinstance(m, dict):
                name = m.get("slug") or m.get("name") or m.get("metric") or m.get("key")
                if name:
                    out[str(name)] = _num(m.get("value"))
        return out
    return {k: _num(entry[k]) for k in METRICS if k in entry}


def _list_in(data, *keys) -> List[dict]:
    """The list of entries of an answer: under result / data, then one of `keys`; a bare list is taken as it is."""
    if isinstance(data, list):
        return [e for e in data if isinstance(e, dict)]
    if not isinstance(data, dict):
        return []
    for holder in (data.get("result"), data.get("data"), data):
        if isinstance(holder, list):
            return [e for e in holder if isinstance(e, dict)]
        if isinstance(holder, dict):
            for key in keys:
                if isinstance(holder.get(key), list):
                    return [e for e in holder[key] if isinstance(e, dict)]
    return []


def parse_spend(data) -> List[dict]:
    """One row per ad of an itemAnalytics answer: the money in rubles (Avito gives kopecks), views, contacts. An entry without a
    usable id is left out. allSpending missing: placement + promotion."""
    rows = []
    for entry in _list_in(data, "groupings", "items"):
        avito_id = _item_id(entry)
        if avito_id is None:
            continue
        m = _metrics(entry)
        presence, promo = m.get("presenceSpending", 0.0), m.get("promoSpending", 0.0)
        spend = m["allSpending"] if "allSpending" in m else presence + promo
        rows.append({"avito_id": avito_id, "spend": round(spend / 100, 2), "presence": round(presence / 100, 2), "promo": round(promo / 100, 2),
                     "views": int(m.get("views", 0)), "contacts": int(m.get("contacts", 0))})
    return rows


def parse_promotions(data) -> Dict[int, dict]:
    """avito_id → {mode: manual / auto / '', bid, day_limit} (rubles) of a getPromotionsByItemIds answer."""
    out = {}
    for entry in _list_in(data, "items", "promotions"):
        avito_id = _item_id(entry)
        if avito_id is None:
            continue
        manual, auto = entry.get("manualPromotion"), entry.get("autoPromotion")
        if isinstance(manual, dict) and manual:
            out[avito_id] = {"mode": "manual", "bid": _rub(manual.get("bidPenny")), "day_limit": _rub(manual.get("limitPenny"))}
        elif isinstance(auto, dict) and auto:
            out[avito_id] = {"mode": "auto", "bid": None, "day_limit": _rub(auto.get("budgetPenny") or auto.get("limitPenny"))}
        else:
            out[avito_id] = {"mode": "", "bid": None, "day_limit": None}
    return out


def parse_bids(data) -> dict:
    """The prices of a view of a getBids answer (rubles): bid (the current manual one), rec_bid, min_bid, max_bid and the options as
    Avito lists them: each with its price and the rest of its fields kept as they came (the forecast)."""
    src = data.get("manual") if isinstance(data, dict) and isinstance(data.get("manual"), dict) else (data if isinstance(data, dict) else {})
    options = []
    for entry in src.get("bids") or (data.get("bids") if isinstance(data, dict) else None) or []:
        if not isinstance(entry, dict):
            continue
        price_key = next((k for k in ("valuePenny", "bidPenny", "pricePenny") if k in entry), None)
        if price_key is None:
            continue
        rest = {str(k): v for k, v in entry.items() if k != price_key and isinstance(v, (int, float, str)) and not isinstance(v, bool)}
        options.append({"bid": _rub(entry.get(price_key)), "info": rest})
    return {"bid": _rub(src.get("bidPenny")), "rec_bid": _rub(src.get("recBidPenny")), "min_bid": _rub(src.get("minBidPenny")),
            "max_bid": _rub(src.get("maxBidPenny")), "options": options}


def shape(data, depth: int = 0) -> str:
    """The keys of an answer without its values (lists by their first entry): what is logged on the first run."""
    if depth > 6:
        return "…"
    if isinstance(data, dict):
        return "{" + ", ".join("%s: %s" % (k, shape(v, depth + 1)) for k, v in list(data.items())[:20]) + "}"
    if isinstance(data, list):
        return "[%s%s]" % (shape(data[0], depth + 1) if data else "", ", …×%d" % len(data) if len(data) > 1 else "")
    return type(data).__name__


def _note_shape(conn, method: str, data, notes: List[str]) -> None:
    key = "ads_spend_shape:%s" % method
    if store.get_state(conn, key) is None:
        text = shape(data)[:2000]
        store.set_state(conn, key, text)
        notes.append("%s: %s" % (method, text))


# ---- collection ----------------------------------------------------------------------------------------------------------------------

def _pause_key(account_id: int) -> str:
    return "ads_spend_pause:%d" % account_id


def error_key(account_id: int) -> str:
    return "ads_spend_error:%d" % account_id


def days_to_read(conn, account_id: int, today: date, daily: bool, limit: int = DAYS_PER_RUN) -> List[str]:
    """The days this run reads: on the daily run yesterday and the day before again; then the days of the last HISTORY_DAYS not read yet,
    newest first; `limit` in all."""
    read = {row[0] for row in conn.execute("SELECT day FROM ad_spend_day WHERE account_id = ? AND avito_id = ?", (account_id, MARK))}
    wanted = [(today - timedelta(days=n)).isoformat() for n in range(1, HISTORY_DAYS + 1)]
    out = [d for d in wanted[:REFRESH_DAYS] if daily or d not in read]
    out += [d for d in wanted[REFRESH_DAYS:] if d not in read]
    return out[:limit]


def _read_day(conn, client, user_id, day: str, pacer: Pacer, notes: List[str]) -> List[dict]:
    rows, offset = [], 0
    while True:
        pacer.wait("stats", STATS_PAUSE)
        data = client.item_analytics(user_id, day, day, list(METRICS), limit=PAGE, offset=offset)
        _note_shape(conn, "itemAnalytics", data, notes)
        page = parse_spend(data)
        rows += page
        if len(_list_in(data, "groupings", "items")) < PAGE:
            return rows
        offset += PAGE


def collect(conn, client, account_id: int, today: date, now: datetime, pacer: Pacer, daily: bool = True,
            days_limit: int = DAYS_PER_RUN) -> dict:
    """Read the spending of the days due (days_to_read) and, on the daily run, the prices of a view. Each day is kept as soon as it is
    read (a failure later keeps it). Raises AvitoError / OSError; after a 429 that outlived the repeats the account rests RATE_PAUSE.
    The result: days, rows, unknown (ads of the answers the list of the account does not know: a sign that the ids are not the ads'),
    bids, notes (the shapes of the first answers)."""
    from .avito import AvitoError, RateLimited
    stamp = now.isoformat(timespec="seconds")
    until = store.get_state(conn, _pause_key(account_id))
    if until and until > stamp:
        raise RateLimited(429, None, "пауза после ответа 429 до %s" % until[11:16])
    notes: List[str] = []
    result = {"days": 0, "rows": 0, "unknown": 0, "bids": 0, "notes": notes}
    known = {row[0] for row in conn.execute("SELECT avito_id FROM avito_ad WHERE account_id = ?", (account_id,))}
    try:
        user_id = connections.cabinet_user_id(conn, client, account_id)
        for day in days_to_read(conn, account_id, today, daily, days_limit):
            rows = _read_day(conn, client, user_id, day, pacer, notes)
            conn.execute("DELETE FROM ad_spend_day WHERE account_id = ? AND day = ?", (account_id, day))
            conn.executemany(
                "INSERT OR REPLACE INTO ad_spend_day (account_id, avito_id, day, spend, presence, promo, views, contacts, read_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                [(account_id, r["avito_id"], day, r["spend"], r["presence"], r["promo"], r["views"], r["contacts"], stamp) for r in rows]
                + [(account_id, MARK, day, 0, 0, 0, 0, 0, stamp)])
            conn.commit()
            result["days"] += 1
            result["rows"] += len(rows)
            result["unknown"] += sum(1 for r in rows if r["avito_id"] not in known)
        if daily:
            result["bids"] = collect_bids(conn, client, account_id, today, stamp, pacer, notes)
        conn.execute("DELETE FROM ad_spend_day WHERE account_id = ? AND day < ?", (account_id, (today - timedelta(days=KEEP_DAYS)).isoformat()))
        conn.commit()
    except RateLimited:
        conn.rollback()
        store.set_state(conn, _pause_key(account_id), (now + timedelta(seconds=RATE_PAUSE)).isoformat(timespec="seconds"))
        store.set_state(conn, error_key(account_id), "%s: Авито ограничило частоту запросов (429)" % stamp)
        raise
    except (AvitoError, OSError) as err:
        conn.rollback()
        store.set_state(conn, error_key(account_id), "%s: %s" % (stamp, err))
        raise
    store.set_state(conn, error_key(account_id), "")
    return result


def collect_bids(conn, client, account_id: int, today: date, stamp: str, pacer: Pacer, notes: List[str]) -> int:
    """The promotion and the prices of a view of the ads promoted within BID_DAYS (most spent first) and those read before, at most
    BIDS_PER_RUN. An ad Avito refuses (it cannot be promoted) is left as it was; a 429 stops the account (the caller)."""
    from .avito import AvitoError, RateLimited
    since = (today - timedelta(days=BID_DAYS)).isoformat()
    ids = [row[0] for row in conn.execute(
        "SELECT avito_id FROM ad_spend_day WHERE account_id = ? AND avito_id <> ? AND day >= ? GROUP BY avito_id HAVING SUM(promo) > 0 "
        "ORDER BY SUM(promo) DESC", (account_id, MARK, since))]
    seen = set(ids)
    ids += [row[0] for row in conn.execute("SELECT avito_id FROM ad_bid WHERE account_id = ? ORDER BY taken_at", (account_id,)) if row[0] not in seen]
    ids = ids[:BIDS_PER_RUN]
    promotions: Dict[int, dict] = {}
    for start in range(0, len(ids), PROMO_CHUNK):
        pacer.wait("promotions", PROMO_PAUSE)
        data = client.promotions_by_items(ids[start:start + PROMO_CHUNK])
        _note_shape(conn, "getPromotionsByItemIds", data, notes)
        promotions.update(parse_promotions(data))
    taken = 0
    for avito_id in ids:
        pacer.wait("bids", BIDS_PAUSE)
        try:
            data = client.item_bids(avito_id)
        except RateLimited:
            raise
        except AvitoError:
            continue
        _note_shape(conn, "getBids", data, notes)
        bids = parse_bids(data)
        promo = promotions.get(avito_id, {})
        conn.execute(
            "INSERT OR REPLACE INTO ad_bid (account_id, avito_id, taken_at, mode, bid, day_limit, rec_bid, min_bid, max_bid, options) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (account_id, avito_id, stamp, promo.get("mode", ""), promo.get("bid") if promo.get("bid") is not None else bids["bid"],
             promo.get("day_limit"), bids["rec_bid"], bids["min_bid"], bids["max_bid"], json.dumps(bids["options"], ensure_ascii=False)))
        conn.commit()
        taken += 1
    return taken


# ---- the report ----------------------------------------------------------------------------------------------------------------------

def bounds(today: date, days: int) -> Tuple[str, str]:
    """The period ends yesterday: today's spending is not whole yet."""
    until = today - timedelta(days=1)
    return (until - timedelta(days=days - 1)).isoformat(), until.isoformat()


def drr(spend: float, base: float) -> Optional[float]:
    """The share of the spending in the revenue or the margin, %; None (a dash) when there is nothing to divide by."""
    if base is None or base <= 0:
        return None
    return round(spend / base * 100, 1)


def sales_by_ad(conn, since: str, until: str) -> Tuple[Dict[Tuple[int, int], dict], dict]:
    """The sales of the days (not reversed) by the ad they came from: the order of the sale (order_card.sale_ids) → its chat → the chat's
    ad (avito_chat.item_id; an order with several chats: the first chat with an ad). The rest (manual sales, an order without a chat with
    an ad) is «без объявления»."""
    link: Dict[int, Tuple[int, int]] = {}
    for sale_ids, account_id, item_id in conn.execute(
            "SELECT c.sale_ids, ch.account_id, ch.item_id FROM order_card c JOIN avito_chat ch ON ch.id = ("
            "SELECT MIN(x.id) FROM avito_chat x WHERE x.order_id = c.id AND x.item_id <> '') WHERE c.sale_ids NOT IN ('', '[]')"):
        try:
            ids, avito_id = json.loads(sale_ids or "[]"), int(item_id)
        except (ValueError, TypeError):
            continue
        for sale_id in ids if isinstance(ids, list) else []:
            if isinstance(sale_id, int):
                link[sale_id] = (account_id, avito_id)
    by_ad: Dict[Tuple[int, int], dict] = {}
    none = {"pieces": 0, "revenue": 0.0, "margin": 0.0, "count": 0}
    for sale_id, pieces, revenue, margin in conn.execute(
            "SELECT id, pieces, sale_total, margin_total FROM sale WHERE day >= ? AND day <= ? AND reversed_at IS NULL", (since, until)):
        bucket = by_ad.setdefault(link[sale_id], {"pieces": 0, "revenue": 0.0, "margin": 0.0, "count": 0}) if sale_id in link else none
        bucket["pieces"] += pieces
        bucket["revenue"] += revenue
        bucket["margin"] += margin
        bucket["count"] += 1
    return by_ad, none


def _row(avito_id, title, ad_id, spend: dict, sold: dict, bid: Optional[dict], covered: int) -> dict:
    total = spend.get("spend", 0.0)
    views = spend.get("views", 0)
    revenue, margin = round(sold.get("revenue", 0.0), 2), round(sold.get("margin", 0.0), 2)
    row = {"avito_id": avito_id, "title": title, "ad_id": ad_id, "spend": round(total, 2), "presence": round(spend.get("presence", 0.0), 2),
           "promo": round(spend.get("promo", 0.0), 2), "views": views, "contacts": spend.get("contacts", 0),
           "view_cost": round(total / views, 2) if views else None, "pieces": sold.get("pieces", 0), "revenue": revenue, "margin": margin,
           "drr": drr(total, revenue), "drr_margin": drr(total, margin),
           "per_day": round(total / covered, 2) if covered else None, "bid": None}
    if bid:
        try:
            options = json.loads(bid["options"] or "[]")
        except ValueError:
            options = []
        rec = bid["rec_bid"]
        row["bid"] = {"mode": bid["mode"], "bid": bid["bid"], "day_limit": bid["day_limit"], "rec_bid": rec, "min_bid": bid["min_bid"],
                      "max_bid": bid["max_bid"], "options": options, "taken_at": bid["taken_at"],
                      "rec_per_day": round(rec * views / covered, 2) if rec and covered else None}
    return row


def _sort(rows: List[dict], by: str) -> List[dict]:
    if by in ("drr", "drr_margin"):
        return sorted(rows, key=lambda r: (r[by] is None, -(r[by] or 0), -r["spend"]))
    return sorted(rows, key=lambda r: (-r["spend"], -r["revenue"], r["avito_id"]))


def _total(rows: Iterable[dict]) -> dict:
    rows = list(rows)
    spend, revenue, margin = (round(sum(r[k] for r in rows), 2) for k in ("spend", "revenue", "margin"))
    views = sum(r["views"] for r in rows)
    return {"ads": len(rows), "spend": spend, "views": views, "contacts": sum(r["contacts"] for r in rows),
            "view_cost": round(spend / views, 2) if views else None, "pieces": sum(r["pieces"] for r in rows), "revenue": revenue,
            "margin": margin, "drr": drr(spend, revenue), "drr_margin": drr(spend, margin)}


def report(conn, today: date, days: int, sort: str = "spend") -> dict:
    """The page: per feed account its ads (those with spending, views or sales in the period), their totals, how many days of the period
    are read; the sales without an ad apart. Read only."""
    if days not in PERIODS:
        raise ValueError("period must be one of %s" % (PERIODS,))
    sort = sort if sort in SORTS else "spend"
    since, until = bounds(today, days)
    sold, none = sales_by_ad(conn, since, until)
    accounts = []
    shown = set()
    for account in connections.list_feed_accounts(conn):
        aid = account["id"]
        shown.add(aid)
        covered = conn.execute("SELECT COUNT(*) FROM ad_spend_day WHERE account_id = ? AND avito_id = ? AND day >= ? AND day <= ?",
                               (aid, MARK, since, until)).fetchone()[0]
        spend = {row[0]: {"spend": row[1], "presence": row[2], "promo": row[3], "views": row[4], "contacts": row[5]} for row in conn.execute(
            "SELECT avito_id, SUM(spend), SUM(presence), SUM(promo), SUM(views), SUM(contacts) FROM ad_spend_day "
            "WHERE account_id = ? AND avito_id <> ? AND day >= ? AND day <= ? GROUP BY avito_id", (aid, MARK, since, until))}
        ads = {row[0]: (row[1], row[2]) for row in conn.execute("SELECT avito_id, title, ad_id FROM avito_ad WHERE account_id = ?", (aid,))}
        cols = ("avito_id", "taken_at", "mode", "bid", "day_limit", "rec_bid", "min_bid", "max_bid", "options")
        bids = {row[0]: dict(zip(cols, row)) for row in conn.execute("SELECT %s FROM ad_bid WHERE account_id = ?" % ", ".join(cols), (aid,))}
        keys = set(spend) | {k[1] for k in sold if k[0] == aid}
        rows = [_row(k, ads.get(k, ("", ""))[0], ads.get(k, ("", ""))[1], spend.get(k, {}), sold.get((aid, k), {}), bids.get(k), covered)
                for k in keys]
        rows = [r for r in rows if r["spend"] or r["views"] or r["pieces"]]
        rows = _sort(rows, sort)
        accounts.append({"id": aid, "name": account["name"], "covered": covered, "rows": rows[:ROWS_MAX], "count": len(rows),
                         "total": _total(rows), "error": store.get_state(conn, error_key(aid)) or ""})
    for (aid, _), bucket in sold.items():                                   # a sale through an account without a feed: not on the page
        if aid not in shown:
            for k in ("pieces", "revenue", "margin", "count"):
                none[k] += bucket[k]
    none = {"pieces": none["pieces"], "revenue": round(none["revenue"], 2), "margin": round(none["margin"], 2), "count": none["count"]}
    return {"days": days, "since": since, "until": until, "sort": sort, "accounts": accounts, "none": none}
