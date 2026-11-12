"""«Аналитика рекламы» (the owner 2026-11-11, stage 1: read only): what Avito takes for each ad of the feed accounts (placement and
promotion together), its views and contacts, the sales of the ad and the share of the spending in the revenue and the margin (ДРР);
the current price of a view and the options Avito offers. Nothing here writes to Avito.

Collection: stats v2 «itemAnalytics» grouped by ad, one request for one day (the table keeps days: 7 / 30 / 90 are sums of days), at most one
request a minute; the first runs fill the last HISTORY_DAYS days DAYS_PER_RUN at a time, newest first, then «yesterday» and the day before
(Avito corrects the recent days). The prices of a view (cpxpromo): of the ads Avito took money for in the last BID_DAYS days (whatever
Avito calls the money: the pay per view of tyres comes as «presence») and of those read before, BIDS_PER_RUN a run, the longest unread
first, each at most once in BID_FRESH_HOURS; an ad Avito refuses keeps the refusal (the state `ads_bid_fail:<account>`) for the page.
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
CPX_PAUSE = 4.0             # cpxpromo, both methods together: 15 a minute against Avito's 20 (the client's repeats fit in the rest)
BIDS_PER_RUN = 60           # ads a run: 4 minutes of an account; the runs every 2 hours pass 720 ads a day
BID_DAYS = 30               # the ads with spending within these days get their prices of a view read
BID_FRESH_HOURS = 20        # an ad read (or refused) within these hours is not asked again: once a day each
FAILS_MAX = 2000            # refusals kept per account
SAMPLE = 3                  # ids of an example in the log line
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
        entries = _list_in(data, "groupings", "items")
        if not page:                                         # nothing to take: what such an answer looks like, once (an empty day or another shape)
            _note_shape(conn, "itemAnalytics (пустой ответ)" if not entries else "itemAnalytics (строки без id)", data, notes)
        rows += page
        if len(entries) < PAGE:
            return rows
        offset += PAGE


def _ad_lists(conn, account_id: int) -> Tuple[set, set]:
    """The ads of the account's list (avito_ad: filled from Avito's active ads only, a row is never deleted) and those of them Avito
    showed at the last reading of the list; the rest of the list are removed ads."""
    rows = conn.execute("SELECT avito_id, seen_at FROM avito_ad WHERE account_id = ?", (account_id,)).fetchall()
    last = max((r[1] or "" for r in rows), default="")
    return {r[0] for r in rows}, {r[0] for r in rows if (r[1] or "") == last}


def collect(conn, client, account_id: int, today: date, now: datetime, pacer: Pacer, daily: bool = True,
            days_limit: int = DAYS_PER_RUN, bids: Optional[bool] = None) -> dict:
    """Read the spending of the days due (days_to_read) and the prices of a view (`bids`; by default on the daily run). Each day is kept
    as soon as it is read (a failure later keeps it). Raises AvitoError / OSError; after a 429 that outlived the repeats the account
    rests RATE_PAUSE.
    The result: days, rows, first_day / last_day, empty_days (answers without a row), unknown (rows of ads the list of the account does
    not hold), unknown_ids (such ads), unknown_sample (some of them), removed_ids (ads of the list Avito no longer shows as active),
    bids, bid_fails, bid_left (due, beyond this run), notes (the shapes of the answers and the refusals)."""
    from .avito import AvitoError, RateLimited
    stamp = now.isoformat(timespec="seconds")
    until = store.get_state(conn, _pause_key(account_id))
    if until and until > stamp:
        raise RateLimited(429, None, "пауза после ответа 429 до %s" % until[11:16])
    notes: List[str] = []
    result = {"days": 0, "rows": 0, "unknown": 0, "bids": 0, "notes": notes, "first_day": "", "last_day": "", "empty_days": 0,
              "unknown_ids": 0, "unknown_sample": [], "removed_ids": 0, "bid_fails": 0, "bid_left": 0}
    known, active = _ad_lists(conn, account_id)
    strangers, removed = set(), set()
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
            result["empty_days"] += not rows
            result["first_day"] = min(result["first_day"] or day, day)
            result["last_day"] = max(result["last_day"], day)
            strangers.update(r["avito_id"] for r in rows if r["avito_id"] not in known)
            removed.update(r["avito_id"] for r in rows if r["avito_id"] in known and r["avito_id"] not in active)
            result.update(unknown_ids=len(strangers), unknown_sample=sorted(strangers)[:SAMPLE], removed_ids=len(removed))
        if daily if bids is None else bids:
            got = collect_bids(conn, client, account_id, today, stamp, pacer, notes)
            result.update(bids=got["taken"], bid_fails=got["failed"], bid_left=got["left"])
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


def fail_key(account_id: int) -> str:
    return "ads_bid_fail:%d" % account_id


def bid_fails(conn, account_id: int) -> Dict[int, dict]:
    """avito_id → {at, why} of the ads whose price of a view Avito refused at the last try (gone at the next good reading)."""
    try:
        raw = json.loads(store.get_state(conn, fail_key(account_id)) or "{}")
    except ValueError:
        return {}
    out = {}
    for key, value in raw.items() if isinstance(raw, dict) else []:
        if isinstance(value, dict) and str(key).isdigit():
            out[int(key)] = {"at": str(value.get("at") or ""), "why": str(value.get("why") or "")}
    return out


def refusal(err) -> Tuple[str, str]:
    """Avito's refusal for the page and for the log: (the status, the code and Avito's words; the same with the keys of the answer's
    body, never its values). The request and its token are not in an AvitoError."""
    why = "ответ %s" % err.status + (", код %s" % err.code if err.code is not None else "")
    words = " ".join(str(err.message or "").split())[:160]
    if words and words != "HTTP %s" % err.status:
        why += ": %s" % words
    try:
        fields = shape(json.loads(err.body)) if err.body else ""
    except ValueError:
        fields = "не JSON"
    return why, why + (" (поля %s)" % fields[:300] if fields else "")


def bid_queue(conn, account_id: int, today: date, stamp: str, fails: Dict[int, dict]) -> List[int]:
    """The ads whose price of a view is due, in the order of reading: those with spending within BID_DAYS (most spent first), then the
    rest of those read before; of them the never tried first, then the longest untried (a refusal is a try: a refused ad does not hold
    the head of the queue). Tried within BID_FRESH_HOURS: not due."""
    since = (today - timedelta(days=BID_DAYS)).isoformat()
    ids = [row[0] for row in conn.execute(
        "SELECT avito_id FROM ad_spend_day WHERE account_id = ? AND avito_id <> ? AND day >= ? GROUP BY avito_id HAVING SUM(spend) > 0 "
        "ORDER BY SUM(spend) DESC, avito_id", (account_id, MARK, since))]
    tried = {row[0]: row[1] or "" for row in conn.execute("SELECT avito_id, taken_at FROM ad_bid WHERE account_id = ?", (account_id,))}
    seen = set(ids)
    ids += sorted(k for k in tried if k not in seen)
    for avito_id, fail in fails.items():
        tried[avito_id] = max(tried.get(avito_id, ""), fail["at"])
    fresh = (datetime.fromisoformat(stamp) - timedelta(hours=BID_FRESH_HOURS)).isoformat(timespec="seconds")
    due = [k for k in ids if tried.get(k, "") <= fresh]
    return sorted(due, key=lambda k: tried.get(k, ""))                     # stable: equal tries stay in the order of the spending


def collect_bids(conn, client, account_id: int, today: date, stamp: str, pacer: Pacer, notes: List[str]) -> dict:
    """The promotion and the prices of a view of the first BIDS_PER_RUN ads of bid_queue: reads only. An ad Avito refuses keeps what was
    read before and the refusal (bid_fails); a refused getPromotionsByItemIds leaves the mode and the daily limit as they were; each
    kind of refusal is noted once a run. A 429 stops the account (the caller): what is read by then is kept.
    The result: taken, failed, left (due ads beyond this run)."""
    from .avito import AvitoError, RateLimited
    fails = bid_fails(conn, account_id)
    queue = bid_queue(conn, account_id, today, stamp, fails)
    ids = queue[:BIDS_PER_RUN]
    refused: Dict[str, int] = {}
    promotions: Optional[Dict[int, dict]] = {}
    taken = failed = 0
    try:
        for start in range(0, len(ids), PROMO_CHUNK):
            pacer.wait("cpxpromo", CPX_PAUSE)
            try:
                data = client.promotions_by_items(ids[start:start + PROMO_CHUNK])
            except RateLimited:
                raise
            except AvitoError as err:
                refused["getPromotionsByItemIds: " + refusal(err)[1]] = 1
                promotions = None
                break
            _note_shape(conn, "getPromotionsByItemIds", data, notes)
            promotions.update(parse_promotions(data))
        for avito_id in ids:
            pacer.wait("cpxpromo", CPX_PAUSE)
            try:
                data = client.item_bids(avito_id)
            except RateLimited:
                raise
            except AvitoError as err:
                why, logged = refusal(err)
                fails[avito_id] = {"at": stamp, "why": why}
                refused["getBids: " + logged] = refused.get("getBids: " + logged, 0) + 1
                failed += 1
                continue
            _note_shape(conn, "getBids", data, notes)
            bids = parse_bids(data)
            if promotions is None:                                          # the promotion was not read: the mode and the limit of the last reading
                old = conn.execute("SELECT mode, bid, day_limit FROM ad_bid WHERE account_id = ? AND avito_id = ?", (account_id, avito_id)).fetchone()
                promo = {"mode": old[0], "bid": None, "day_limit": old[2]} if old else {}
            else:
                promo = promotions.get(avito_id, {})
            conn.execute(
                "INSERT OR REPLACE INTO ad_bid (account_id, avito_id, taken_at, mode, bid, day_limit, rec_bid, min_bid, max_bid, options) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (account_id, avito_id, stamp, promo.get("mode", ""), promo.get("bid") if promo.get("bid") is not None else bids["bid"],
                 promo.get("day_limit"), bids["rec_bid"], bids["min_bid"], bids["max_bid"], json.dumps(bids["options"], ensure_ascii=False)))
            conn.commit()
            fails.pop(avito_id, None)
            taken += 1
    finally:
        notes += ["отказ Авито %s%s" % (text, " ×%d" % count if count > 1 else "") for text, count in refused.items()]
        old = (datetime.fromisoformat(stamp) - timedelta(days=BID_DAYS)).isoformat(timespec="seconds")      # an ad long out of the queue
        fails = dict(sorted(((k, v) for k, v in fails.items() if v["at"] >= old), key=lambda kv: kv[1]["at"])[-FAILS_MAX:])
        store.set_state(conn, fail_key(account_id), json.dumps({str(k): v for k, v in fails.items()}, ensure_ascii=False))
    return {"taken": taken, "failed": failed, "left": len(queue) - len(ids)}


# ---- the report ----------------------------------------------------------------------------------------------------------------------
# The owner 2026-11-12: a summary per Avito account with the total above, the ads of one account below (tabs); the sales of an ad from the
# orders on a won stage (the table «Продажи» is empty while nobody records the sales of the orders), the short cell of the price of a view,
# no column empty for every row, the ads out of the list in one line.

FILTERS = ("", "sales", "spend")
NEAR = 5                    # options of the price of a view shown: the nearest to the current price
NO_CHAT, NO_ITEM, NO_FEED, MANUAL = "no_chat", "no_item", "no_feed", "manual"
COLUMNS = ("spend", "views", "contacts", "view_cost", "pieces", "revenue", "margin", "drr", "drr_margin", "bid", "per_day")


def bounds(today: date, days: int) -> Tuple[str, str]:
    """The period ends yesterday: today's spending is not whole yet."""
    until = today - timedelta(days=1)
    return (until - timedelta(days=days - 1)).isoformat(), until.isoformat()


def drr(spend: float, base: float) -> Optional[float]:
    """The share of the spending in the revenue or the margin, %; None (a dash) when there is nothing to divide by."""
    if base is None or base <= 0:
        return None
    return round(spend / base * 100, 1)


def _won_days(conn, cards: Dict[int, str]) -> Dict[int, str]:
    """The day each order of `cards` (id → entered_at) came to a won stage and stayed on won stages since: by the journal of the order
    (a move between two won stages keeps the first day); an order without such records: the day it entered its stage now."""
    from . import analytics, orders
    stages = [s for s in orders.list_stages(conn) if s["kind"] == "won"]
    won = {s["name"] for s in stages} | {old for code, old in orders.OLD_NAMES_2026_11_03.items() if code in {s["code"] for s in stages}}
    since: Dict[int, Optional[str]] = {}
    for order_id, at, kind, text in conn.execute(
            "SELECT order_id, at, kind, text FROM order_event WHERE kind IN ('created', 'stage') ORDER BY order_id, id"):
        if order_id not in cards:
            continue
        name = analytics._entered(kind, text)
        if name is None:
            continue
        if name not in won:
            since[order_id] = None
        elif since.get(order_id) is None:
            since[order_id] = at
    return {k: (since.get(k) or entered or "")[:10] for k, entered in cards.items()}


def _chat_links(conn) -> Tuple[Dict[int, Tuple[int, int]], set]:
    """order → (account, ad) of its first chat with an ad (avito_chat.item_id); and the orders with a chat at all."""
    link: Dict[int, Tuple[int, int]] = {}
    for order_id, account_id, item_id in conn.execute(
            "SELECT ch.order_id, ch.account_id, ch.item_id FROM avito_chat ch WHERE ch.id = ("
            "SELECT MIN(x.id) FROM avito_chat x WHERE x.order_id = ch.order_id AND x.item_id <> '') AND ch.order_id IS NOT NULL"):
        try:
            link[order_id] = (account_id, int(item_id))
        except (TypeError, ValueError):
            continue
    chats = {r[0] for r in conn.execute("SELECT DISTINCT order_id FROM avito_chat WHERE order_id IS NOT NULL")}
    return link, chats


def sold_units(conn, since: str, until: str) -> List[dict]:
    """What was sold in the days, per order (or per manual sale): {order (None for a manual sale), pieces, revenue, margin (None: a
    purchase not typed), no_purchase}. An order with a sale in «Продажи» (order_card.sale_ids) is counted by its sales (not reversed, the
    day of the sale), once; an order without one, by its positions when it stands on a won stage («Выполнено», «Отправлен в ТК») and came
    to it in the days: pieces = qty, revenue = qty × sale_unit, the margin of economics.unit (the purchase with its terms, the commission and
    the promotion of the position: the formula of a sale, orders.totals). A cancelled order and a record of storage of «На ОТХ» (an order
    without a client, oth_storage) are no sale; an order that took tyres from «На ОТХ» for a new client is one."""
    from . import orders, oth_storage
    units: List[dict] = []
    by_sale: Dict[int, int] = {}
    with_sales = set()
    for card_id, sale_ids in conn.execute("SELECT id, sale_ids FROM order_card WHERE sale_ids NOT IN ('', '[]')"):
        try:
            ids = json.loads(sale_ids or "[]")
        except ValueError:
            continue
        with_sales.add(card_id)
        for sale_id in ids if isinstance(ids, list) else []:
            if isinstance(sale_id, int):
                by_sale[sale_id] = card_id
    grouped: Dict[object, dict] = {}
    for sale_id, pieces, revenue, margin in conn.execute(
            "SELECT id, pieces, sale_total, margin_total FROM sale WHERE day >= ? AND day <= ? AND reversed_at IS NULL", (since, until)):
        key = ("order", by_sale[sale_id]) if sale_id in by_sale else ("sale", sale_id)
        unit = grouped.setdefault(key, {"order": by_sale.get(sale_id), "pieces": 0, "revenue": 0.0, "margin": 0.0, "no_purchase": 0})
        unit["pieces"] += pieces
        unit["revenue"] += revenue
        unit["margin"] += margin
    units += grouped.values()
    records = oth_storage.record_ids(conn)
    cards = {card_id: entered for card_id, entered in conn.execute(
        "SELECT c.id, c.entered_at FROM order_card c JOIN order_stage s ON s.id = c.stage_id WHERE s.kind = 'won'")
        if card_id not in with_sales and card_id not in records}
    days = _won_days(conn, cards)
    cards = [k for k in cards if since <= days[k] <= until]
    for card_id, items in orders.items_of(conn, cards).items():
        items = [i for i in items if int(i["qty"]) >= 1]                     # a position emptied by «Взять с ОТХ»: nothing sold
        if not items:
            continue
        money = orders.totals(items)
        units.append({"order": card_id, "pieces": money["qty"], "revenue": money["sale"],
                      "margin": money["margin"] if money["margin"] is not None else
                      round(sum(i["money"]["margin"] for i in items if float(i["supplier_price"]) > 0), 2),
                      "no_purchase": 1 if money["margin"] is None else 0})
    return units


def _bucket() -> dict:
    return {"pieces": 0, "revenue": 0.0, "margin": 0.0, "count": 0, "no_purchase": 0}


def _add(bucket: dict, unit: dict) -> None:
    bucket["pieces"] += unit["pieces"]
    bucket["revenue"] += unit["revenue"]
    bucket["margin"] += unit["margin"]
    bucket["count"] += 1
    bucket["no_purchase"] += unit["no_purchase"]


def sales_by_ad(conn, since: str, until: str, feed: Iterable[int] = ()) -> Tuple[Dict[Tuple[int, int], dict], dict]:
    """The sales of the days (sold_units) by the ad they came from: the order → its first chat with an ad (avito_chat.item_id) → the ad and
    the account of the chat. The rest is «без объявления», with why: no chat, a chat without an ad, the chat of an account without a feed
    (`feed`: the ids of the feed accounts), a manual sale."""
    link, chats = _chat_links(conn)
    feed = set(feed)
    by_ad: Dict[Tuple[int, int], dict] = {}
    none = dict(_bucket(), reasons={NO_CHAT: 0, NO_ITEM: 0, NO_FEED: 0, MANUAL: 0})
    for unit in sold_units(conn, since, until):
        order_id = unit["order"]
        key = link.get(order_id) if order_id is not None else None
        if key is not None and key[0] in feed:
            _add(by_ad.setdefault(key, _bucket()), unit)
            continue
        _add(none, unit)
        none["reasons"][MANUAL if order_id is None else NO_FEED if key is not None else NO_ITEM if order_id in chats else NO_CHAT] += 1
    return by_ad, none


def bid_short(bid: dict) -> str:
    """The price of a view in a word: «авто», «своя 5,00 ₽», «5,00 ₽» (the mode not read), «не задана»."""
    if bid.get("mode") == "auto":
        return "авто"
    if bid.get("bid") is None:
        return "не задана"
    text = ("%.2f" % bid["bid"]).replace(".", ",") + "\u00a0₽"
    return "своя " + text if bid.get("mode") == "manual" else text


def _forecast(info: dict) -> str:
    low = next((v for k, v in info.items() if "forecast" in k.lower() and "min" in k.lower()), None)
    high = next((v for k, v in info.items() if "forecast" in k.lower() and "max" in k.lower()), None)
    one = next((v for k, v in info.items() if "forecast" in k.lower()), None)
    if low is not None and high is not None:
        return "%s–%s" % (low, high)
    if high is not None:
        return "до %s" % high
    if low is not None:
        return "от %s" % low
    return "" if one is None else str(one)


def near_options(options: List[dict], current: Optional[float]) -> List[dict]:
    """At most NEAR options of Avito, the nearest to the current price (or the first ones, without a price), in the order of the price;
    each with its forecast of views in a word («10–20») and the rest of its fields."""
    good = [o for o in options if isinstance(o, dict) and isinstance(o.get("bid"), (int, float))]
    if current is not None:
        good = sorted(sorted(good, key=lambda o: (abs(o["bid"] - current), o["bid"]))[:NEAR], key=lambda o: o["bid"])
    out = []
    for o in good[:NEAR]:
        info = o.get("info") if isinstance(o.get("info"), dict) else {}
        out.append({"bid": o["bid"], "forecast": _forecast(info), "info": info})
    return out


def _row(avito_id, title, ad_id, spend: dict, sold: dict, bid: Optional[dict], covered: int) -> dict:
    total = spend.get("spend", 0.0)
    views = spend.get("views", 0)
    revenue, margin = round(sold.get("revenue", 0.0), 2), round(sold.get("margin", 0.0), 2)
    row = {"avito_id": avito_id, "title": title, "ad_id": ad_id, "spend": round(total, 2), "presence": round(spend.get("presence", 0.0), 2),
           "promo": round(spend.get("promo", 0.0), 2), "views": views, "contacts": spend.get("contacts", 0),
           "view_cost": round(total / views, 2) if views else None, "pieces": sold.get("pieces", 0), "revenue": revenue, "margin": margin,
           "orders": sold.get("count", 0), "no_purchase": sold.get("no_purchase", 0),
           "drr": drr(total, revenue), "drr_margin": drr(total, margin),
           "per_day": round(total / covered, 2) if covered else None, "bid": None, "bid_fail": None, "bid_why": ""}
    if bid:
        try:
            options = json.loads(bid["options"] or "[]")
        except ValueError:
            options = []
        rec = bid["rec_bid"]
        row["bid"] = {"mode": bid["mode"], "bid": bid["bid"], "day_limit": bid["day_limit"], "rec_bid": rec, "min_bid": bid["min_bid"],
                      "max_bid": bid["max_bid"], "options": options, "taken_at": bid["taken_at"], "short": bid_short(bid),
                      "limit": bid["day_limit"], "near": near_options(options if isinstance(options, list) else [], bid["bid"] if bid["bid"] is not None else rec),
                      "rec_per_day": round(rec * views / covered, 2) if rec and covered else None}
    return row


def _sort(rows: List[dict], by: str) -> List[dict]:
    if by in ("drr", "drr_margin"):
        return sorted(rows, key=lambda r: (r[by] is None, -(r[by] or 0), -r["spend"]))
    return sorted(rows, key=lambda r: (-r["spend"], -r["revenue"], r["avito_id"]))


def _total(rows: Iterable[dict]) -> dict:
    rows = list(rows)
    spend, revenue, margin = (round(sum(r[k] for r in rows), 2) for k in ("spend", "revenue", "margin"))
    views, contacts = sum(r["views"] for r in rows), sum(r["contacts"] for r in rows)
    return {"ads": len(rows), "spend": spend, "views": views, "contacts": contacts,
            "view_cost": round(spend / views, 2) if views else None, "contact_cost": round(spend / contacts, 2) if contacts else None,
            "pieces": sum(r["pieces"] for r in rows), "revenue": revenue, "margin": margin, "drr": drr(spend, revenue),
            "drr_margin": drr(spend, margin), "no_purchase": sum(r.get("no_purchase", 0) for r in rows)}


def _filled(rows: List[dict]) -> set:
    """The columns of COLUMNS some row has a value in: a dash is no value, nor a zero of a sum (a ДРР of 0 % is one)."""
    shares = ("drr", "drr_margin", "view_cost")
    return {c for c in COLUMNS if any(r[c] is not None and (c in shares or r[c] != 0) for r in rows)}


def _names_out_of_the_list(conn, account_id: int, ids: Iterable[int]) -> Dict[int, Tuple[str, str]]:
    """The name and our Id of the ads the list of the account (avito_ad) has not: it holds the ads active since its first reading only, so
    an ad closed before, or between two readings, is not there. The name of a chat about the ad (avito_chat.item_title), our Id of the
    stock or the price sent to the ad (avito_stock, avito_price); the same account only (never another account's values)."""
    wanted = {int(i) for i in ids}
    out: Dict[int, List[str]] = {}
    if not wanted:
        return {}
    for item_id, title in conn.execute("SELECT item_id, item_title FROM avito_chat WHERE account_id = ? AND item_id <> '' AND item_title <> '' "
                                       "ORDER BY id DESC", (account_id,)):
        try:
            key = int(item_id)
        except (TypeError, ValueError):
            continue
        if key in wanted:
            out.setdefault(key, ["", ""])
            out[key][0] = out[key][0] or str(title)
    for table in ("avito_stock", "avito_price"):
        for avito_id, ad_id in conn.execute("SELECT avito_id, ad_id FROM %s WHERE account_id = ? AND ad_id <> ''" % table, (account_id,)):
            if avito_id in wanted:
                out.setdefault(avito_id, ["", ""])
                out[avito_id][1] = out[avito_id][1] or str(ad_id)
    return {k: (v[0], v[1]) for k, v in out.items()}


def _account(conn, account: dict, today: date, since: str, until: str, sold: dict, sort: str, only: str) -> dict:
    aid = account["id"]
    covered = conn.execute("SELECT COUNT(*) FROM ad_spend_day WHERE account_id = ? AND avito_id = ? AND day >= ? AND day <= ?",
                           (aid, MARK, since, until)).fetchone()[0]
    spend = {row[0]: {"spend": row[1], "presence": row[2], "promo": row[3], "views": row[4], "contacts": row[5]} for row in conn.execute(
        "SELECT avito_id, SUM(spend), SUM(presence), SUM(promo), SUM(views), SUM(contacts) FROM ad_spend_day "
        "WHERE account_id = ? AND avito_id <> ? AND day >= ? AND day <= ? GROUP BY avito_id", (aid, MARK, since, until))}
    ads = {row[0]: (row[1], row[2]) for row in conn.execute("SELECT avito_id, title, ad_id FROM avito_ad WHERE account_id = ?", (aid,))}
    cols = ("avito_id", "taken_at", "mode", "bid", "day_limit", "rec_bid", "min_bid", "max_bid", "options")
    bids = {row[0]: dict(zip(cols, row)) for row in conn.execute("SELECT %s FROM ad_bid WHERE account_id = ?" % ", ".join(cols), (aid,))}
    fails = bid_fails(conn, aid)
    error = store.get_state(conn, error_key(aid)) or ""
    queued = {row[0] for row in conn.execute(
        "SELECT avito_id FROM ad_spend_day WHERE account_id = ? AND avito_id <> ? AND day >= ? GROUP BY avito_id HAVING SUM(spend) > 0",
        (aid, MARK, (today - timedelta(days=BID_DAYS)).isoformat()))}
    keys = set(spend) | {k[1] for k in sold if k[0] == aid}
    ads.update(_names_out_of_the_list(conn, aid, keys - set(ads)))
    rows = [_row(k, ads.get(k, ("", ""))[0], ads.get(k, ("", ""))[1], spend.get(k, {}), sold.get((aid, k), {}), bids.get(k), covered)
            for k in keys]
    rows = [r for r in rows if r["spend"] or r["views"] or r["pieces"]]
    for r in rows:                                                      # why there is no price of a view, or why it is an old one
        r["bid_fail"] = fails.get(r["avito_id"])
        if r["bid"] is None and r["bid_fail"] is None:
            r["bid_why"] = ("нет расходов за последние %d дн., цена не запрашивается" % BID_DAYS if r["avito_id"] not in queued
                            else "последний сбор не удался (причина над таблицей)" if error else "очередь ещё не дошла")
    total = _total(rows)
    every = rows                                                        # all the ads of the period, uncut and unfiltered (ads_tree)
    due = [r for r in rows if r["avito_id"] in queued]
    strangers = _sort([r for r in rows if not r["title"] and not r["ad_id"]], "spend")
    rows = [r for r in rows if r["title"] or r["ad_id"]]
    shown = [r for r in rows if (only != "sales" or r["pieces"]) and (only != "spend" or r["spend"])]
    shown = _sort(shown, sort)
    return {"id": aid, "name": account["name"], "covered": covered, "rows": shown[:ROWS_MAX], "count": len(shown), "total": total,
            "error": error, "columns": _filled(shown), "every": every, "bids_of": len(due), "bids_read": sum(1 for r in due if r["bid"] is not None),
            "strangers": {"count": len(strangers), "spend": round(sum(r["spend"] for r in strangers), 2),
                          "views": sum(r["views"] for r in strangers), "rows": strangers[:ROWS_MAX]}}


def _summary(accounts: List[dict]) -> dict:
    """A line per account and the total: the account's totals and its share in the spending of all accounts."""
    lines = [dict(a["total"], id=a["id"], name=a["name"]) for a in accounts]
    total = dict(_total(lines), name="Итого")
    total["ads"] = sum(t["ads"] for t in lines)
    for line in lines + [total]:
        line["share"] = round(line["spend"] / total["spend"] * 100, 1) if total["spend"] > 0 else None
    return {"rows": lines, "total": total}


def report(conn, today: date, days: int, sort: str = "spend", account: Optional[int] = None, only: str = "") -> dict:
    """The page: the summary per feed account and the total; per account its ads (those with spending, views or sales in the period, `only`
    those with sales / with spending), their totals (whatever the filter), how many days of the period are read, how many prices of a view
    are read; `account` the one shown (the first by default); the sales without an ad apart, with why. Read only."""
    if days not in PERIODS:
        raise ValueError("period must be one of %s" % (PERIODS,))
    sort = sort if sort in SORTS else "spend"
    only = only if only in FILTERS else ""
    since, until = bounds(today, days)
    feed = connections.list_feed_accounts(conn)
    sold, none = sales_by_ad(conn, since, until, [a["id"] for a in feed])
    accounts = [_account(conn, a, today, since, until, sold, sort, only) for a in feed]
    chosen = next((a for a in accounts if a["id"] == account), accounts[0] if accounts else None)
    none = dict(none, revenue=round(none["revenue"], 2), margin=round(none["margin"], 2))
    return {"days": days, "since": since, "until": until, "sort": sort, "only": only, "accounts": accounts, "account": chosen,
            "summary": _summary(accounts), "none": none}
