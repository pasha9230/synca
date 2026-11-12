"""«Изменить цену просмотра» of one ad in «Аналитика рекламы» (the owner 2026-11-13, step 2b): a write to Avito that spends money.

The window reads getBids and getPromotionsByItemIds fresh (read_fresh) and shows the price, the limit, the options and the ДРР of 30 days;
change() checks the value against Avito's bounds, wants the box «Подтверждаю» for a jump of more than half or a daily limit above the
threshold, writes once (setManual, never repeated), reads the promotion back and keeps in ad_bid what Avito answered. Every write, done or
refused by Avito, is a line of the journal (AUDIT, Moscow time: panel.MOSCOW_AUDIT): who, the ad, the account, was, wanted, what Avito
returned. «Вернуть прежнюю» (undo_values) takes «было» of the last change from the journal and goes through the same change().

A form is written once: its token is taken in app_state before the request (INSERT OR IGNORE: a second press finds it taken), and the
same values of the same ad within a minute of a write are not sent again. Before a write the reading must be fresh (FRESH seconds: the
price in ad_bid and actionTypeID of the last read_fresh), else change() reads getBids and getPromotionsByItemIds again and checks by them;
the form carries «было» the person saw, and a price or a limit changed since (another tab, the cabinet) refuses the write (review 2026-11-13). The panel's own cpxpromo requests are counted per account in
app_state: at most CALLS_PER_MINUTE a minute (Avito's limit); a 429 is told, never repeated."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Callable, List, Optional

from . import ads_spend, store

AUDIT = "ads_bid_set"
THRESHOLD_AUDIT = "ads_bid_threshold"
THRESHOLD_KEY = "ads_bid_limit_warn"         # app_state: the daily limit (rubles) above which «Подтверждаю» is wanted
DEFAULT_THRESHOLD = 1000.0
JUMP = 0.5                                    # a change of the price of more than this share either way wants «Подтверждаю»
CALLS_PER_MINUTE = 20                         # cpxpromo, the reads and the write together (Avito)
SAME_WINDOW = 60                              # seconds: the same values of the same ad again within them are not sent
TOKEN_PREFIX = "ads_bid_token:"
TOKEN_DAYS = 2                                # the tokens of the forms kept
DRR_DAYS = 30
FRESH = 600                                   # seconds: a reading older than this is read again before a write
WAIT_TEXT = "Авито просит подождать: запросов к цене просмотра слишком много. Попробуйте через минуту."


class BidError(Exception):
    """A refusal to show in the window: nothing was sent to Avito (or Avito refused)."""


class Wait(BidError):
    def __init__(self, text: str = WAIT_TEXT):
        super().__init__(text)


# ---- the threshold ---------------------------------------------------------------------------------------------------------------------

def threshold(conn) -> float:
    try:
        value = float(store.get_state(conn, THRESHOLD_KEY) or DEFAULT_THRESHOLD)
    except ValueError:
        return DEFAULT_THRESHOLD
    return value if value > 0 else DEFAULT_THRESHOLD


def set_threshold(conn, value: float, at: str, author: str) -> bool:
    if not value or value <= 0 or value > 1_000_000:
        raise BidError("Порог: нужно число больше нуля.")
    old = threshold(conn)
    if round(old, 2) == round(value, 2):
        return False
    conn.execute("INSERT OR REPLACE INTO app_state VALUES (?, ?)", (THRESHOLD_KEY, str(round(value, 2))))
    store.write_audit(conn, at, author, THRESHOLD_AUDIT, {"was": old, "now": round(value, 2)})
    return True


# ---- Avito's limit -------------------------------------------------------------------------------------------------------------------

def _calls_key(account_id: int) -> str:
    return "ads_bid_calls:%d" % account_id


def take_calls(conn, account_id: int, now: float, count: int) -> None:
    """Count `count` cpxpromo requests of the account now; Wait when the minute already has CALLS_PER_MINUTE (nothing counted then)."""
    try:
        times = [float(t) for t in json.loads(store.get_state(conn, _calls_key(account_id)) or "[]")]
    except (ValueError, TypeError):
        times = []
    times = [t for t in times if now - 60 < t <= now]
    if len(times) + count > CALLS_PER_MINUTE:
        raise Wait()
    store.set_state(conn, _calls_key(account_id), json.dumps(times + [now] * count))


# ---- reading -------------------------------------------------------------------------------------------------------------------------

def _action_key(account_id: int, avito_id: int) -> str:
    return "ads_bid_action:%d:%d" % (account_id, avito_id)


def action_type(avito_id: int, *answers) -> Optional[int]:
    """actionTypeID of the ad in Avito's answers (getBids at the top or under «manual», getPromotionsByItemIds in the entry of the ad)."""
    keys = ("actionTypeID", "actionTypeId", "action_type_id")

    def found(entry):
        if isinstance(entry, dict):
            for key in keys:
                value = entry.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                    return value
        return None

    for data in answers:
        if not isinstance(data, dict):
            continue
        for place in (data, data.get("manual")):
            if found(place):
                return found(place)
        for entry in ads_spend._list_in(data, "items", "promotions"):
            if ads_spend._item_id(entry) == avito_id:
                for place in (entry, entry.get("manualPromotion")):
                    if found(place):
                        return found(place)
    return None


def _read_key(account_id: int, avito_id: int) -> str:
    return "ads_bid_read_at:%d:%d" % (account_id, avito_id)


def _epoch_of(stamp) -> Optional[float]:
    """UTC epoch of a stamp of ad_bid / app_state (UTC ISO, naive or with an offset); None when it is not one."""
    try:
        value = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def is_fresh(conn, state: Optional[dict], account_id: int, avito_id: int, now: float) -> bool:
    """The price in ad_bid and actionTypeID (of the last read_fresh) are both read less than FRESH seconds ago."""
    if not state:
        return False
    taken = _epoch_of(state.get("taken_at"))
    read = _epoch_of(store.get_state(conn, _read_key(account_id, avito_id)))
    return taken is not None and read is not None and all(0 <= now - t < FRESH for t in (taken, read))


def current(conn, account_id: int, avito_id: int) -> Optional[dict]:
    row = conn.execute("SELECT taken_at, mode, bid, day_limit, rec_bid, min_bid, max_bid, options FROM ad_bid WHERE account_id = ? AND avito_id = ?",
                       (account_id, avito_id)).fetchone()
    if not row:
        return None
    try:
        options = json.loads(row[7] or "[]")
    except ValueError:
        options = []
    action = store.get_state(conn, _action_key(account_id, avito_id))
    return {"taken_at": row[0], "mode": row[1], "bid": row[2], "day_limit": row[3], "rec_bid": row[4], "min_bid": row[5], "max_bid": row[6],
            "options": options if isinstance(options, list) else [], "action_type": int(action) if action and action.isdigit() else None}


def read_fresh(conn, client, account_id: int, avito_id: int, stamp: str, now: float) -> dict:
    """getBids and getPromotionsByItemIds of the ad, once each (no repeats in a press), kept in ad_bid as the collection keeps them;
    actionTypeID kept for the write. Wait on Avito's 429 or the panel's own limit; BidError on a refusal."""
    from .avito import AvitoError, RateLimited
    take_calls(conn, account_id, now, 2)
    try:
        bids_raw = client.item_bids(avito_id, retry=False)
        promo_raw = client.promotions_by_items([avito_id], retry=False)
    except RateLimited:
        raise Wait()
    except AvitoError as err:
        raise BidError("Авито не отдал цену просмотра: %s" % ads_spend.refusal(err)[0])
    bids = ads_spend.parse_bids(bids_raw)
    promo = ads_spend.parse_promotions(promo_raw).get(avito_id, {})
    bid = promo.get("bid") if promo.get("bid") is not None else bids["bid"]
    conn.execute("INSERT OR REPLACE INTO ad_bid (account_id, avito_id, taken_at, mode, bid, day_limit, rec_bid, min_bid, max_bid, options) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (account_id, avito_id, stamp, promo.get("mode", ""), bid, promo.get("day_limit"), bids["rec_bid"], bids["min_bid"],
                  bids["max_bid"], json.dumps(bids["options"], ensure_ascii=False)))
    action = action_type(avito_id, bids_raw, promo_raw)
    if action:
        conn.execute("INSERT OR REPLACE INTO app_state VALUES (?, ?)", (_action_key(account_id, avito_id), str(action)))
        conn.execute("INSERT OR REPLACE INTO app_state VALUES (?, ?)", (_read_key(account_id, avito_id), stamp))
    else:
        conn.execute("DELETE FROM app_state WHERE key = ?", (_read_key(account_id, avito_id),))
    conn.commit()
    return current(conn, account_id, avito_id)


def drr30(conn, account_id: int, avito_id: int, today: date, feed: List[int]) -> dict:
    """The spending, the revenue and the ДРР of the ad over the last DRR_DAYS closed days (the figures of «Аналитика рекламы»)."""
    since, until = ads_spend.bounds(today, DRR_DAYS)
    spend = conn.execute("SELECT COALESCE(SUM(spend), 0), COALESCE(SUM(views), 0) FROM ad_spend_day WHERE account_id = ? AND avito_id = ? "
                         "AND day >= ? AND day <= ?", (account_id, avito_id, since, until)).fetchone()
    sold = ads_spend.sales_by_ad(conn, since, until, feed)[0].get((account_id, avito_id), {})
    revenue = round(sold.get("revenue", 0.0), 2)
    return {"spend": round(spend[0], 2), "views": spend[1], "revenue": revenue, "drr": ads_spend.drr(spend[0], revenue)}


# ---- checks --------------------------------------------------------------------------------------------------------------------------

def _rub(value: float) -> str:
    return ("%.2f" % value).replace(".", ",") + " ₽"


def confirm_reasons(state: dict, bid: float, limit: float, warn: float) -> List[str]:
    """Why the change wants «Подтверждаю»: a jump of the price of more than JUMP from the current one (the recommended one without it),
    a daily limit above the threshold."""
    out = []
    base = state.get("bid") if state.get("mode") != "auto" and state.get("bid") else state.get("rec_bid")
    if base and abs(bid - base) / base > JUMP:
        out.append("цена меняется больше чем на %d %%: %s → %s" % (JUMP * 100, _rub(base), _rub(bid)))
    if limit > warn:
        out.append("дневной лимит %s выше порога %s" % (_rub(limit), _rub(warn)))
    return out


def check(state: Optional[dict], bid, limit, confirmed: bool, warn: float) -> None:
    """BidError when the values cannot be sent: the bounds of Avito not read, the price outside them, no limit, «Подтверждаю» missing."""
    if not state or state.get("min_bid") is None or state.get("max_bid") is None:
        raise BidError("Границы цены от Авито не прочитаны: откройте окно заново.")
    if bid is None or bid <= 0:
        raise BidError("Цена просмотра: нужно число больше нуля.")
    if not state["min_bid"] <= bid <= state["max_bid"]:
        raise BidError("Цена %s вне границ Авито: от %s до %s." % (_rub(bid), _rub(state["min_bid"]), _rub(state["max_bid"])))
    if limit is None or limit <= 0:
        raise BidError("Сколько тратить в день: нужно число больше нуля.")
    reasons = confirm_reasons(state, bid, limit, warn)
    if reasons and not confirmed:
        raise BidError("Нужна галочка «Подтверждаю»: " + "; ".join(reasons) + ".")


# ---- the write -----------------------------------------------------------------------------------------------------------------------

def _take_token(conn, token: str, at: str) -> bool:
    old = (datetime.fromisoformat(at) - timedelta(days=TOKEN_DAYS)).isoformat(timespec="seconds")
    conn.execute("DELETE FROM app_state WHERE key LIKE ? AND value < ?", (TOKEN_PREFIX + "%", old))
    taken = conn.execute("INSERT OR IGNORE INTO app_state VALUES (?, ?)", (TOKEN_PREFIX + token, at)).rowcount == 1
    conn.commit()
    return taken


def history(conn, account_id: int, avito_id: int, limit: int = 20) -> List[dict]:
    """The journal's lines of the ad, newest first: {"at", "author", **detail}."""
    out = []
    for at, author, detail in conn.execute(
            "SELECT at, author, detail FROM audit WHERE action = ? AND json_extract(detail, '$.account_id') = ? "
            "AND json_extract(detail, '$.avito_id') = ? ORDER BY id DESC LIMIT ?", (AUDIT, account_id, avito_id, limit)):
        try:
            data = json.loads(detail)
        except ValueError:
            continue
        if data.get("account_id") == account_id and data.get("avito_id") == avito_id:
            out.append(dict(data, at=at, author=author))
            if len(out) >= limit:
                break
    return out


def _penny(rub: float) -> int:
    return int(round(rub * 100))


def _short(answer) -> str:
    text = answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)
    return text[:500]


def _same(a, b) -> bool:
    return (a is None and b is None) or (a is not None and b is not None and round(a, 2) == round(b, 2))


def _fresh_state(conn, account_id: int, avito_id: int, stamp: str, now: float, reader: Optional[Callable[[], object]]) -> Optional[dict]:
    """The state of the ad for a write: ad_bid when fresh, else a new read_fresh; BidError / Wait (nothing written) when it fails."""
    state = current(conn, account_id, avito_id)
    if is_fresh(conn, state, account_id, avito_id, now):
        return state
    if reader is None:
        raise BidError("Цена прочитана давно, а прочитать её заново нельзя: цена не записана. Откройте окно заново.")
    try:
        return read_fresh(conn, reader(), account_id, avito_id, stamp, now)
    except Wait:
        raise
    except BidError as err:
        raise BidError("Свежую цену с Авито прочитать не удалось (%s): цена не записана. Попробуйте позже." % err)
    except Exception as err:                                                # noqa: BLE001 — a network failure: nothing is written
        raise BidError("Авито не ответил на чтение цены (%s): цена не записана. Попробуйте позже." % type(err).__name__)


def change(conn, account: dict, avito_id: int, bid, limit, *, confirmed: bool, token: str, at: str, stamp: str, now: float, author: str,
           writer: Callable[[], object], kind: str = "set", seen: Optional[dict] = None,
           reader: Optional[Callable[[], object]] = None) -> dict:
    """One write of the price of a view and the daily limit of one ad. `at`: Moscow time of the journal; `stamp`: UTC of ad_bid; `writer`:
    makes the client with allow_write (only here); `reader`: a read-only client for the fresh read when ad_bid is older than FRESH;
    `seen`: {"bid", "limit"} the form showed («было»), refused when Avito has other ones now. The result: {"ok", "text", "mismatch",
    "state", "duplicate"}; BidError / Wait when nothing was sent."""
    from .avito import AvitoError, RateLimited
    from .reliability import ServiceUnavailable
    account_id = account["id"]
    if not token or len(token) > 64:
        raise BidError("Форма устарела: откройте окно заново.")
    bid = round(bid, 2) if bid is not None else None                       # checked as sent: kopecks
    limit = round(limit, 2) if limit is not None else None
    if not isinstance(seen, dict) or "bid" not in seen or "limit" not in seen:
        raise BidError("Форма устарела: откройте окно заново.")
    state = _fresh_state(conn, account_id, avito_id, stamp, now, reader)
    check(state, bid, limit, confirmed, threshold(conn))
    if not state.get("action_type"):
        raise BidError("Авито не назвал тип продвижения объявления (actionTypeID): записать нельзя. Откройте окно заново.")
    last = next(iter(history(conn, account_id, avito_id, 1)), None)
    if last and last.get("ok") and last.get("want") == {"bid": bid, "limit": limit} and \
            (datetime.fromisoformat(at) - datetime.fromisoformat(last["at"])).total_seconds() < SAME_WINDOW:
        return {"ok": True, "duplicate": True, "mismatch": False, "state": state,
                "text": "Эти же значения уже отправлены меньше минуты назад: второй раз не отправляю."}
    if not _take_token(conn, token, at):
        return {"ok": True, "duplicate": True, "mismatch": False, "state": state, "text": "Эта форма уже отправлена: второй раз не отправляю."}
    if not (_same(seen["bid"], state["bid"]) and _same(seen["limit"], state["day_limit"])):
        raise BidError("Цену уже изменили (сейчас %s, лимит %s). Обновите окно." % (
            _rub(state["bid"]) if state["bid"] is not None else "не задана", _rub(state["day_limit"]) if state["day_limit"] is not None else "нет"))
    take_calls(conn, account_id, now, 2)                                    # the write and the control read
    was = {"mode": state["mode"], "bid": state["bid"], "day_limit": state["day_limit"]}
    detail = {"account_id": account_id, "account": account.get("name", ""), "avito_id": avito_id, "kind": kind, "was": was,
              "want": {"bid": bid, "limit": limit}, "action_type": state["action_type"], "token": token, "sent": True}
    unknown = False
    client = writer()                                                       # the client that may write: made here, once
    try:
        answer = client.set_manual_bid(avito_id, state["action_type"], _penny(bid), _penny(limit))
    except RateLimited as err:
        store.write_audit(conn, at, author, AUDIT, dict(detail, ok=False, answer="429 " + _short(err.body or err.message)))
        raise Wait()
    except AvitoError as err:
        store.write_audit(conn, at, author, AUDIT, dict(detail, ok=False, answer="%s %s" % (err.status, _short(err.body or err.message))))
        raise BidError("Авито отказал: %s. Цена не изменена." % ads_spend.refusal(err)[0])
    except (OSError, ServiceUnavailable) as err:                           # sent, the answer lost: the control read tells what is there
        answer, unknown = "нет ответа: %s" % type(err).__name__, True
    got = None
    try:
        promo = ads_spend.parse_promotions(client.promotions_by_items([avito_id], retry=False)).get(avito_id)   # the control read, once
    except Exception as err:                                                # noqa: BLE001 — the write is done; the reading is told
        promo, read_error = None, str(getattr(err, "message", "") or type(err).__name__)
    else:
        read_error = "" if promo else "объявления нет в ответе"
    if promo:
        got = {"mode": promo["mode"], "bid": promo["bid"], "day_limit": promo["day_limit"]}
        conn.execute("UPDATE ad_bid SET taken_at = ?, mode = ?, bid = ?, day_limit = ? WHERE account_id = ? AND avito_id = ?",
                     (stamp, promo["mode"], promo["bid"], promo["day_limit"], account_id, avito_id))
    else:                                                                   # what Avito has now is unknown: the next write reads it first
        conn.execute("DELETE FROM app_state WHERE key = ?", (_read_key(account_id, avito_id),))
    mismatch = got is not None and (got["bid"] != bid or got["day_limit"] != limit)
    ok = got is not None and not mismatch
    store.write_audit(conn, at, author, AUDIT, dict(detail, ok=ok, answer=_short(answer), got=got, read_error=read_error))
    if got is None:
        text = ("Запрос отправлен, но ответа Авито нет" if unknown else "Авито принял запрос") + \
               ", а контрольное чтение не удалось (%s): проверьте цену в кабинете Авито." % read_error
    elif mismatch:
        text = "Авито показывает не то, что ставили: стало %s, лимит %s (ставили %s, лимит %s)." % (
            _rub(got["bid"]) if got["bid"] is not None else got["mode"] or "не задана",
            _rub(got["day_limit"]) if got["day_limit"] is not None else "нет", _rub(bid), _rub(limit))
    else:
        text = "Цена просмотра на Авито: %s, лимит %s в день." % (_rub(got["bid"]), _rub(got["day_limit"]))
    return {"ok": ok, "duplicate": False, "mismatch": mismatch, "state": current(conn, account_id, avito_id), "text": text}


# ---- «Вернуть прежнюю» ---------------------------------------------------------------------------------------------------------------

def undo_values(conn, account_id: int, avito_id: int) -> Optional[dict]:
    """What «Вернуть прежнюю» puts back: «было» of the last change made through the panel that Avito took (none once it was put back). {"bid", "limit", "mode", "at",
    "author", "note"}; bid None when there was no price to put back (the window says so); None without a change."""
    last = next((h for h in history(conn, account_id, avito_id, 50) if h.get("sent") and (h.get("ok") or h.get("got") is not None)), None)
    if not last or last.get("kind") == "undo":                              # already put back: nothing more to return
        return None
    was = last.get("was") or {}
    note = ""
    if was.get("mode") == "auto":
        note = "До изменения был режим «авто»: через API Авито его не вернуть, включите его в кабинете Авито."
        if was.get("bid") is not None:
            note += " Кнопка поставит цену, прочитанную тогда."
    elif was.get("bid") is None:
        note = "До изменения цена не была задана: возвращать нечего, поставьте цену сами или снимите её в кабинете Авито."
    return {"bid": was.get("bid"), "limit": was.get("day_limit"), "mode": was.get("mode", ""), "at": last["at"], "author": last["author"],
            "note": note}
