"""«№ Счета» of MIM's cabinet = our «Номер перемещения» (the owner 2026-11-09: right after «Оформить в WebMIM», without closing the window,
the order is checked against the cabinet and its number taken from there). API v2 never gives it (webmim_invoice looks there in vain); the
cabinet's list does: GET /api/v3/orders (webmim_orders.OrdersClient.cabinet_orders) with the session of the v3 login of the same API user
the button logs in with (the lead 2026-11-09: no login of its own): the order's `paymentAccount.number` («26R-1564565»,
webmim_invoice.PATTERN), there from the status «создан» on. Found by the exact `orderNumber` = the order's WebMIM number.

Where it is read (GETs only; the session is closed by the client's close(): sessions are limited):
- right after the press (webmim_create.place → after_place): the session of the press, before its logout, one quick attempt; the number is
  in the card before order_placed.on_placed renders the messages, so they go with it at once;
- the window «Подтверждение» (poll): every few seconds for about a minute while the number is not there, one client a poll, GAP between
  any two asks of the panel; a number in the card already: no login at all; a failed ask (the cabinet lets one session in: «код 429»)
  stops the asking until the person presses «Проверить ещё раз» (the owner 2026-11-10);
- hourly (webmim_match.run → safe_hourly): the open orders without a number, the session of the snapshot, one login a run;
- a WebMIM number typed by a person in the order window (after_save, the owner 2026-11-09: the partner places orders by hand in the
  cabinet): within that save, one quick login and read, its logout; the save never fails for it.
The live server only: an invoice of the test server is never ours (as webmim_invoice.after_place). No API user (an empty login): off,
everything as before. A list the login does not see: a grey record of the order, everything as before.

A number found is written by webmim_invoice.apply (an empty «Номер перемещения» only; a different one typed by a person is a yellow record,
nothing changed) in one IMMEDIATE transaction: two tabs, a second poll and the hourly reading write it once. Only that one write ('written')
lets the messages «заказ оформлен» that waited for it go, of any order, placed by the button or by hand (the owner 2026-11-09: «слать
всем»; order_placed.send_waiting: the same send as on_placed, the task's claim, the send mark: once). The login, the password and the
session's key never go to a journal, a line or the page."""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

from . import connections, orders, webmim_invoice, webmim_orders

SERVER = "prod"                    # the cabinet of the live server only
PER_PAGE = 100
HOURLY_PAGES = 3                   # the hourly reading: at most so many pages of the newest orders, one session
FRESH = 60                         # seconds: an answer older than this is never shown again in place of a new ask
GAP = 4                            # seconds: the window asks the cabinet at most once in so many (every order and tab together)
LAST = "webmim_cabinet_last"       # app_state: {"at": when the window asked last}
SEEN = "webmim_cabinet_seen:%d"    # app_state: the last answer for the window of an order (another tab is shown it, not asked again)
AUTHOR = webmim_invoice.AUTHOR

Send = Callable[[int], List[str]]  # (order) → the lines of order_placed.send_waiting with the senders of the caller


def enabled(conn) -> bool:
    """The API user's login is saved and the live server is chosen: the number is taken from the cabinet. Else off, as before."""
    from . import webmim_match
    config = connections.get_config(conn, "webmim_orders", connections.supplier_owner(1))
    return bool(str(config.get("login") or "").strip()) and webmim_match.current_server(conn) == SERVER


def _number(value) -> str:
    """An order's number as WebMIM's own digits: '' when it is not digits only (never a part of something else)."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value if value is not None else "").strip()
    return text if text.isascii() and text.isdigit() else ""


def _typed(value) -> str:
    """A WebMIM number as typed into the order (a «№» and spaces around the digits at most): its digits, else '' («5001/2» is never
    read as 50012)."""
    return _number(str(value or "").strip().lstrip("№").strip())


def _text(value) -> str:
    if isinstance(value, dict):
        for key in ("name", "title", "description", "text", "value", "code"):
            if value.get(key) not in (None, ""):
                return str(value[key])[:80]
        return ""
    return "" if value is None else str(value)[:80]


def _sum(value) -> Optional[float]:
    if isinstance(value, dict):
        value = value.get("value", value.get("amount"))
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        return round(float(str(value).replace(" ", "").replace(",", ".")), 2)
    except ValueError:
        return None


def info(row: dict) -> dict:
    """What the window shows of the cabinet's order. `invoice` '' unless it is the whole value of the form (webmim_invoice.PATTERN)."""
    account = row.get("paymentAccount")
    raw = str(account.get("number") or "").strip() if isinstance(account, dict) else ""
    good = bool(webmim_invoice.PATTERN.fullmatch(raw))
    return {"order_number": _number(row.get("orderNumber")), "invoice": raw if good else "", "odd_invoice": bool(raw) and not good,
            "invoice_date": _text(row.get("invoiceDate")), "status": _text(row.get("status")), "sum": _sum(row.get("sum")),
            "created": _text(row.get("createdDate"))}


def _why(err: Exception) -> str:
    if isinstance(err, webmim_orders.OrdersError):
        return str(err).rstrip(".")
    return "кабинет недоступен (%s)" % type(err).__name__


def _find(client, number: str, quick: bool = False) -> Optional[dict]:
    """The row of exactly this `orderNumber` on the first page of the newest orders (an order just made is among them)."""
    for row in client.cabinet_orders(1, PER_PAGE, quick=quick):
        if _number(row.get("orderNumber")) == number:
            return row
    return None


# ---- the write ---------------------------------------------------------------------------------------------------------------------------

def _day(text: str) -> str:
    value = str(text or "").strip()
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")[:19]).strftime("%d.%m.%Y")
    except ValueError:
        return value[:20]


def write(conn, card_id: int, seen: dict, now: str) -> str:
    """The invoice of `seen` into the order (webmim_invoice.apply) in one IMMEDIATE transaction: written / same / differs. Commits."""
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        when = _day(seen["invoice_date"])
        text = "Номер перемещения из кабинета MIM: %s (%s)." % (seen["invoice"], ("счёт от %s" % when) if when else
                                                               "заказ WebMIM №%s" % seen["order_number"])
        result = webmim_invoice.apply(conn, card_id, seen["invoice"], "№%s" % seen["order_number"], now, written_text=text)
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    return result


NO_SUM = ("Сумма заказа в кабинете MIM не указана: номер перемещения %s вписан, сообщения «заказ оформлен» сами не отправлены — сверьте "
          "заказ и отправьте их в окне «Сообщения по заказу».")
SUM_DIFFERS = ("Сумма заказа в кабинете MIM (%s ₽) не сходится с закупкой заказа (%s ₽): номер перемещения %s вписан, сообщения «заказ "
               "оформлен» сами не отправлены — сверьте и отправьте их в окне «Сообщения по заказу».")


def sum_differs(conn, card_id: int, seen: dict) -> bool:
    """The cabinet's sum of the order against its purchase (the window's own check): a strange row is never a reason to tell the client."""
    return seen.get("sum") is not None and abs(float(seen["sum"]) - purchase(conn, card_id)) >= 0.5


def take(conn, card_id: int, seen: dict, now: str, send: Optional[Send]) -> Tuple[str, List[str]]:
    """Write the number found and, when this was the write of it, send what waited for it (of any order: the owner 2026-11-09 «слать
    всем»; nothing new is made, order_placed.send_waiting) — unless the cabinet's sum differs from the order's purchase (the review
    2026-11-09: a yellow record, the messages wait for a person). (apply's result, the lines of the send)."""
    result = write(conn, card_id, seen, now)
    lines: List[str] = []
    if result == "written" and send is not None:
        if not typed_since_release(conn, card_id):                        # the owner 2026-11-09: only new orders, the old ones are said
            from . import order_placed
            lines = order_placed.send_waiting(conn, card_id, now, AUTHOR, None, None, hold=True)
        elif seen.get("sum") is None or sum_differs(conn, card_id, seen):    # no sum to check the row by: never a reason to tell the client
            text = (NO_SUM % seen["invoice"] if seen.get("sum") is None else
                    SUM_DIFFERS % (_money(seen["sum"]), _money(purchase(conn, card_id)), seen["invoice"]))
            orders.add_event(conn, card_id, now, AUTHOR, "webmim", text, "yellow")
            conn.commit()
            lines = [text]                                                   # shown to the person who saved it (never counted as sent)
        else:
            lines = send(card_id)
    return result, lines


def typed_since_release(conn, card_id: int) -> bool:
    """The order's WebMIM number was saved after the release of 2026-11-09 (orders.NUMBER_AT, any save: the button, a person, the hourly
    match) and is still that one: only then the messages «заказ оформлен» go by themselves when the transfer number comes (the owner
    2026-11-09: only new orders; a number typed before has no mark)."""
    mark = _state(conn, orders.NUMBER_AT % card_id)
    return bool(mark) and str(mark.get("number", "")) == str(orders._row(conn, card_id)["webmim_number"] or "")


def _money(value) -> str:
    return ("%.2f" % float(value)).rstrip("0").rstrip(".").replace(".", ",")


def note(conn, card_id: int, text: str, now: str) -> None:
    """A grey record of a failure, once per text (a poll every few seconds never repeats it). Commits."""
    line = "Номер перемещения из кабинета MIM не прочитан: %s. Впишите его сами или дождитесь почасового чтения." % text.rstrip(".")
    if conn.execute("SELECT 1 FROM order_event WHERE order_id = ? AND author = ? AND text = ?", (card_id, AUTHOR, line)).fetchone() is None:
        orders.add_event(conn, card_id, now, AUTHOR, "webmim", line, "grey")
    conn.commit()


# ---- right after the press -------------------------------------------------------------------------------------------------------------

def after_place(conn, card_id: int, client, now: str, server: str) -> str:
    """Within the press, its own session before the logout: one quick read of the cabinet's list. The number goes in before on_placed
    renders the messages (so they go with it, nothing waits). Never raises for WebMIM's sake. Returns what became of it."""
    from . import webmim_match
    if client is None or server != SERVER or not enabled(conn):
        return "off"
    number = webmim_match.own_number(orders._row(conn, card_id)["webmim_number"])
    if not number:
        return "no-number"
    try:
        row = _find(client, number, quick=True)
    except (OSError, webmim_orders.OrdersError) as err:
        note(conn, card_id, _why(err), now)
        return "failed"
    seen = info(row) if row is not None else None
    if seen is None or not seen["invoice"]:
        return "wait"
    if seen.get("sum") is None:                                           # the owner 2026-11-09: no sum, no message anywhere; as below
        return "no-sum"
    if sum_differs(conn, card_id, seen):                                  # not before on_placed: the window's poll writes it, nothing is sent
        return "sum-differs"
    return write(conn, card_id, seen, now)


# ---- a number typed by a person ----------------------------------------------------------------------------------------------------------

LATER = ("Номер перемещения заказа WebMIM №%s в кабинете MIM пока не найден: возьмём его при почасовой проверке (или впишите сами).")


def later(conn, card_id: int, number: str, now: str) -> None:
    """A grey record, once per number: not in the cabinet yet (or no invoice yet), the hourly reading takes it. Commits."""
    line = LATER % number
    if conn.execute("SELECT 1 FROM order_event WHERE order_id = ? AND author = ? AND text = ?", (card_id, AUTHOR, line)).fetchone() is None:
        orders.add_event(conn, card_id, now, AUTHOR, "webmim", line, "grey")
    conn.commit()


BUSY = "Кабинет MIM только что спрашивали по другому заказу: номер перемещения возьмём при почасовой проверке (или впишите сами)."


def busy(conn, card_id: int, now: str) -> None:
    """A grey record, once: the cabinet was not asked (the GAP of the window). Commits."""
    if conn.execute("SELECT 1 FROM order_event WHERE order_id = ? AND author = ? AND text = ?", (card_id, AUTHOR, BUSY)).fetchone() is None:
        orders.add_event(conn, card_id, now, AUTHOR, "webmim", BUSY, "grey")
    conn.commit()


MOVED = ("Номер заказа WebMIM исправлен (%s → %s), а номер перемещения %s вписан по прежнему номеру MIM: проверьте его.")


def after_save(conn, card_id: int, client_of: Callable[[], object], now: str, send: Optional[Send],
               before: str = "") -> Tuple[str, List[str]]:
    """A WebMIM number has just been typed into the order window by a person (the partner places the order by hand in the cabinet): when
    the order is one the hourly reading would ask for (_targets: open, no transfer number, not of the test server, its number its own), one
    quick read of the cabinet's list right in that save (a new client of `client_of()`, its quick login, its logout), and take() of what is
    found: the number written, the messages that waited for it sent once. Only a number typed as WebMIM's digits (a «№» and spaces
    around them at most: «5001/2» is never read as 50012). Not there yet, no invoice yet: a grey record once, the hourly reading takes it;
    a failure: a grey record once (note). The window's GAP holds here too (sessions are limited): within it, a grey record of its own
    and the hourly reading. `before`: the WebMIM number before this save; corrected while a transfer number is in already: a yellow
    record once, the cabinet is not asked (the review 2026-11-09). (what became of it, the lines of the send)."""
    if not enabled(conn):
        return "off", []
    card = orders._row(conn, card_id)
    was, typed = str(before or "").strip(), str(card["webmim_number"] or "").strip()
    if was and was != typed and card["transfer_number"]:
        text = MOVED % (was[:40], typed[:40] or "(пусто)", card["transfer_number"])
        if conn.execute("SELECT 1 FROM order_event WHERE order_id = ? AND text = ?", (card_id, text)).fetchone() is None:
            orders.add_event(conn, card_id, now, AUTHOR, "webmim", text, "yellow")
        conn.commit()
        return "moved", [text]
    number = _typed(card["webmim_number"])
    if not number:
        return "no-number", []
    if _targets(conn, SERVER).get(number) != card_id:
        return "off", []
    client = client_of()
    if client is None:
        return "off", []
    if _turn(conn, card_id, now) is not None:                           # no login made yet: nothing to close
        busy(conn, card_id, now)
        return "busy", []
    try:
        with client:
            row = _find(client, number, quick=True)
    except (OSError, webmim_orders.OrdersError) as err:
        note(conn, card_id, _why(err), now)
        return "failed", []
    seen = info(row) if row is not None else None
    if seen is None or not seen["invoice"]:
        later(conn, card_id, number, now)
        return "wait", []
    return take(conn, card_id, seen, now, send)


# ---- the window «Подтверждение» -------------------------------------------------------------------------------------------------------

def _state(conn, key: str) -> dict:
    row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    try:
        data = json.loads(row[0]) if row else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def _put(conn, key: str, value: dict) -> None:
    conn.execute("INSERT INTO app_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, json.dumps(value, ensure_ascii=False)))


def _seconds(now: str, then: str) -> float:
    try:
        return (datetime.fromisoformat(now) - datetime.fromisoformat(then)).total_seconds()
    except (TypeError, ValueError):
        return 1e9


def _turn(conn, card_id: int, now: str) -> Optional[dict]:
    """May the window ask the cabinet now (GAP since the last ask of anybody)? Takes the turn (None); else the last answer of this order."""
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        last = _state(conn, LAST)
        if 0 <= _seconds(now, str(last.get("at", ""))) < GAP:
            conn.commit()
            seen = _state(conn, SEEN % card_id)
            fresh = seen and 0 <= _seconds(now, str(seen.get("at", ""))) < FRESH
            wait = max(1, math.ceil(GAP - _seconds(now, str(last.get("at", "")))))     # «Проверить ещё раз» pressed too early says so
            return dict({k: v for k, v in seen.items() if k != "at"} if fresh else {"state": "wait", "final": False, "text": "проверяю…"}, retry_in=wait)
        _put(conn, LAST, {"at": now})
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    return None


def _remember(conn, card_id: int, result: dict, now: str) -> dict:
    _put(conn, SEEN % card_id, dict(result, lines=[], at=now))
    conn.commit()
    return result


WRITTEN = "Номер перемещения %s вписан"
UNREACHABLE = "Кабинет MIM занят или не отвечает. Номер подтянется сам при ежечасной проверке."


def messages(conn, card_id: int) -> List[str]:
    """What became of the messages «заказ оформлен» of the order (order_placed's tasks), for the window: read only."""
    from . import order_engine
    tasks = [t for t in order_engine.list_tasks(conn, card_id) if t["kind"] == "message" and t["rule_id"] == order_engine.PLACED_RULE]
    sent, waits = sum(t["status"] == "done" for t in tasks), sum(t["status"] in ("pending", "error", "running") for t in tasks)
    return ((["Сообщения «заказ оформлен» отправлены: %d." % sent] if sent else [])
            + (["Сообщения «заказ оформлен» ждут в окне «Сообщения по заказу»: %d." % waits] if waits else []))


def purchase(conn, card_id: int) -> float:
    """The order's purchase as the window «Подтверждение» shows it (order_supplier.view: the supplier's positions, the panel's price)."""
    from . import oth_sale
    card = orders.get_card(conn, card_id)
    return round(sum(float(i["supplier_price"] or 0) * int(i["qty"] or 0) for i in oth_sale.supplier_items(conn, card) if int(i["qty"] or 0) > 0), 2)


def poll(conn, card_id: int, client_of: Callable[[], object], now: str, send: Optional[Send]) -> dict:
    """One poll of the window: {state, text, final, order_number, invoice, invoice_date, status, sum, purchase, sum_differs, transfer, result,
    lines; retry: the ask failed, the page stops asking and shows «Проверить ещё раз»; retry_in: asked within GAP — seconds to wait}. A
    number in the card already (the owner 2026-11-10): the answer found with what became of the messages, never a login. state: off (no API user / not the live server), none (no WebMIM number), wait (not in the list yet, no invoice yet), found,
    error (the login, the list, the network; `final` when the login does not see the list). `client_of()`: a new OrdersClient or None;
    closed here (its logout). Never raises for the cabinet's sake."""
    from . import webmim_create, webmim_match
    card = orders._row(conn, card_id)
    out = {"state": "wait", "text": "", "final": False, "order_number": "", "invoice": "", "invoice_date": "", "status": "", "sum": None,
           "purchase": None, "sum_differs": False, "transfer": card["transfer_number"], "result": "", "lines": []}
    state = webmim_create.get_state(conn, card_id)
    if not enabled(conn) or not state or state["server"] != SERVER or state["state"] != "done":
        return dict(out, state="off", final=True, text="сверка с кабинетом MIM — для заказа, оформленного кнопкой на боевом сервере")
    number = webmim_match.own_number(card["webmim_number"])
    if not number:
        return dict(out, state="none", final=True, text="у заказа нет номера WebMIM")
    if not _own_number(conn, card_id, number):
        return dict(out, state="off", final=True, text="номер WebMIM заказа не только его (взят с ОТХ или вписан в другой заказ): номер перемещения впишите сами")
    if card["transfer_number"]:                               # in already (by the press, a poll, a person): never a login for it
        seen = _state(conn, SEEN % card_id)
        known = {k: v for k, v in seen.items() if k not in ("at", "retry", "retry_in")} if seen.get("state") == "found" else {}
        text = WRITTEN % card["transfer_number"]
        if known.get("invoice") and known["invoice"] != card["transfer_number"]:
            text += ": в кабинете MIM «№ Счета» другой (%s), сверьте" % known["invoice"]
        return dict(out, **dict(known, state="found", final=True, result=known.get("result") or "in", transfer=card["transfer_number"],
                                text=text, lines=messages(conn, card_id)))
    cached = _turn(conn, card_id, now)
    if cached is not None:
        return dict(out, **dict(cached, transfer=orders._row(conn, card_id)["transfer_number"], lines=[]))
    client = client_of()
    if client is None:
        return dict(out, state="off", final=True, text="API-пользователь WebMIM не задан")
    try:
        with client:
            row = _find(client, number)
    except (OSError, webmim_orders.OrdersError) as err:
        note(conn, card_id, _why(err), now)
        seen_not = isinstance(err, webmim_orders.OrdersError) and err.status in (401, 403, 404)
        refused = isinstance(err, webmim_orders.OrdersError) and webmim_orders.WRONG_PASSWORD in str(err)
        if seen_not or refused:                                   # the login never sees the list: a new ask changes nothing
            return _remember(conn, card_id, dict(out, state="error", final=True, text=_why(err)), now)
        return _remember(conn, card_id, dict(out, state="error", retry=True, text=UNREACHABLE, why=_why(err)), now)
    if row is None:
        return _remember(conn, card_id, dict(out, text="заказа №%s ещё нет в кабинете MIM" % number), now)
    seen = info(row)
    out.update({k: seen[k] for k in ("order_number", "invoice", "invoice_date", "status", "sum")})
    out["purchase"] = purchase(conn, card_id)
    out["sum_differs"] = seen["sum"] is not None and abs(seen["sum"] - out["purchase"]) >= 0.5
    if not seen["invoice"]:
        return _remember(conn, card_id, dict(out, text="№ счёта ещё не выдан" + (" (в кабинете значение не того вида)" if seen["odd_invoice"] else "")), now)
    result_of, lines = take(conn, card_id, seen, now, send)
    transfer = orders._row(conn, card_id)["transfer_number"]
    texts = {"written": "номер перемещения вписан: %s" % seen["invoice"], "same": "номер перемещения совпадает с кабинетом",
             "differs": "в заказе вписан другой номер перемещения (%s): панель ничего не меняла, сверьте" % transfer}
    _remember(conn, card_id, dict(out, state="found", final=True, result=result_of, transfer=transfer, text=texts.get(result_of, "")), now)
    return dict(out, state="found", final=True, result=result_of, transfer=transfer, text=texts.get(result_of, ""), lines=lines)


def _own_number(conn, card_id: int, number: str) -> bool:
    """The WebMIM number is this order's own: not borrowed with tyres «с ОТХ», not typed into another order too (as _targets)."""
    from . import oth_sale, webmim_match
    if card_id in oth_sale.borrowed_ids(conn):
        return False
    return all(other == card_id for other, typed in conn.execute("SELECT id, webmim_number FROM order_card WHERE webmim_number != ''")
               if webmim_match.own_number(typed) == number)


# ---- hourly -----------------------------------------------------------------------------------------------------------------------------

def _targets(conn, server: str) -> Dict[str, int]:
    """WebMIM number → the open order of the panel without a transfer number (not one of another server, nor a borrowed number; a number
    of two orders is left alone: a person sorts it). The key as after_save reads it (_typed): «5001/2» is no number of WebMIM's."""
    from . import oth_sale, webmim_match
    kinds = {s["id"]: s["kind"] for s in orders.list_stages(conn)}
    elsewhere = webmim_match.made_elsewhere(conn, server) | oth_sale.borrowed_ids(conn)
    seen: Dict[str, List[int]] = {}
    for card_id, number, transfer, stage in conn.execute(
            "SELECT id, webmim_number, transfer_number, stage_id FROM order_card WHERE webmim_number != ''"):
        key = _typed(number)
        if key:
            seen.setdefault(key, []).append(card_id if (not transfer and kinds.get(stage) == "open" and card_id not in elsewhere) else 0)
    return {k: v[0] for k, v in seen.items() if len(v) == 1 and v[0]}


def hourly(conn, client, server: str, now: str, send: Optional[Send]) -> dict:
    """The open orders without a number get it from the cabinet: the session of the snapshot's client (one login a run, none when nothing
    waits), the newest HOURLY_PAGES pages. What was read before a failure is still written."""
    counts = {"written": 0, "same": 0, "differs": 0, "sent": 0, "error": ""}
    if server != SERVER or not enabled(conn):
        return counts
    targets = _targets(conn, server)
    if not targets:
        return counts
    found: Dict[str, dict] = {}
    try:
        for page in range(1, HOURLY_PAGES + 1):
            rows = client.cabinet_orders(page, PER_PAGE)
            for row in rows:
                seen = info(row)
                if seen["order_number"] in targets and seen["invoice"]:
                    found[seen["order_number"]] = seen
            if len(rows) < PER_PAGE or len(found) == len(targets):
                break
    except (OSError, webmim_orders.OrdersError) as err:
        counts["error"] = _why(err)
    for number, seen in found.items():
        result, lines = take(conn, targets[number], seen, now, send)
        counts[result] = counts.get(result, 0) + 1
        counts["sent"] += sum(1 for line in lines if line.startswith("Клиенту отправлено") or (line.startswith("Точке «") and "» отправлено «" in line))
    return counts


def line(counts: dict) -> str:
    parts = []
    if counts["written"]:
        parts.append("вписано %d" % counts["written"])
    if counts["differs"]:
        parts.append("расходится с вписанным %d" % counts["differs"])
    if counts["sent"]:
        parts.append("сообщений отправлено %d" % counts["sent"])
    if counts["error"]:
        parts.append(counts["error"])
    return ("; кабинет MIM: " + ", ".join(parts)) if parts else ""


def safe_hourly(conn, client, server: str, now: str, send: Optional[Send]) -> str:
    """hourly() whose own failure never fails the snapshot: the tail of the line."""
    try:
        return line(hourly(conn, client, server, now, send))
    except Exception:                                                  # noqa: BLE001
        conn.rollback()
        logging.getLogger("tires_sync.orders").exception("the invoice numbers of MIM's cabinet")
        return "; кабинет MIM: сбой панели"
