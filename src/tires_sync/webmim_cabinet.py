"""«№ Счета» of MIM's cabinet = our «Номер перемещения» (the owner 2026-11-09: right after «Оформить в WebMIM», without closing the window,
the order is checked against the cabinet and its number taken from there). API v2 never gives it (webmim_invoice looks there in vain); the
cabinet's list does: GET /api/v3/orders (webmim_orders.OrdersClient.cabinet_orders) with the session of the v3 login of the same API user
the button logs in with (the lead 2026-11-09: no login of its own): the order's `paymentAccount.number` («26R-1564565»,
webmim_invoice.PATTERN), there from the status «создан» on. Found by the exact `orderNumber` = the order's WebMIM number.

Where it is read (GETs only; the session is closed by the client's close(): sessions are limited):
- right after the press (webmim_create.place → after_place): the session of the press, before its logout, one quick attempt; the number is
  in the card before order_placed.on_placed renders the messages, so they go with it at once;
- the window «Подтверждение» (poll): every few seconds for about a minute while the number is not there, one client a poll, GAP between
  any two asks of the panel;
- hourly (webmim_match.run → safe_hourly): the open orders without a number, the session of the snapshot, one login a run.
The live server only: an invoice of the test server is never ours (as webmim_invoice.after_place). No API user (an empty login): off,
everything as before. A list the login does not see: a grey record of the order, everything as before.

A number found is written by webmim_invoice.apply (an empty «Номер перемещения» only; a different one typed by a person is a yellow record,
nothing changed) in one IMMEDIATE transaction: two tabs, a second poll and the hourly reading write it once. Only that one write ('written')
of an order placed in full lets the messages «заказ оформлен» that waited for it go (order_placed.send_waiting: the same send as on_placed,
the task's claim, the send mark: once). The login, the password and the session's key never go to a journal, a line or the page."""
from __future__ import annotations

import json
import logging
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


def placed(conn, card_id: int) -> bool:
    """Placed in full by the button (webmim_create «done»): only then the messages «заказ оформлен» are due (order_placed.on_placed)."""
    from . import webmim_create
    state = webmim_create.get_state(conn, card_id)
    return bool(state and state["state"] == "done")


def take(conn, card_id: int, seen: dict, now: str, send: Optional[Send]) -> Tuple[str, List[str]]:
    """Write the number found and, when this was the write of it, send what waited for it. (apply's result, the lines of the send)."""
    result = write(conn, card_id, seen, now)
    lines: List[str] = []
    if result == "written" and send is not None and placed(conn, card_id):
        lines = send(card_id)
    return result, lines


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
    number = webmim_match.digits(orders._row(conn, card_id)["webmim_number"])
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
    return write(conn, card_id, seen, now)


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
            return {k: v for k, v in seen.items() if k != "at"} if fresh else {"state": "wait", "final": False, "text": "проверяю…"}
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


def purchase(conn, card_id: int) -> float:
    """The order's purchase as the window «Подтверждение» shows it (order_supplier.view: the supplier's positions, the panel's price)."""
    from . import oth_sale
    card = orders.get_card(conn, card_id)
    return round(sum(float(i["supplier_price"] or 0) * int(i["qty"] or 0) for i in oth_sale.supplier_items(conn, card) if int(i["qty"] or 0) > 0), 2)


def poll(conn, card_id: int, client_of: Callable[[], object], now: str, send: Optional[Send]) -> dict:
    """One poll of the window: {state, text, final, order_number, invoice, invoice_date, status, sum, purchase, sum_differs, transfer, result,
    lines}. state: off (no API user / not the live server), none (no WebMIM number), wait (not in the list yet, no invoice yet), found,
    error (the login, the list, the network; `final` when the login does not see the list). `client_of()`: a new OrdersClient or None;
    closed here (its logout). Never raises for the cabinet's sake."""
    from . import webmim_create, webmim_match
    card = orders._row(conn, card_id)
    out = {"state": "wait", "text": "", "final": False, "order_number": "", "invoice": "", "invoice_date": "", "status": "", "sum": None,
           "purchase": None, "sum_differs": False, "transfer": card["transfer_number"], "result": "", "lines": []}
    state = webmim_create.get_state(conn, card_id)
    if not enabled(conn) or not state or state["server"] != SERVER or state["state"] != "done":
        return dict(out, state="off", final=True, text="сверка с кабинетом MIM — для заказа, оформленного кнопкой на боевом сервере")
    number = webmim_match.digits(card["webmim_number"])
    if not number:
        return dict(out, state="none", final=True, text="у заказа нет номера WebMIM")
    if not _own_number(conn, card_id, number):
        return dict(out, state="off", final=True, text="номер WebMIM заказа не только его (взят с ОТХ или вписан в другой заказ): номер перемещения впишите сами")
    if card["transfer_number"]:                               # in already (by the press, a poll, a person): no login for it
        seen = _state(conn, SEEN % card_id)
        if seen.get("state") == "found":
            return dict(out, **dict({k: v for k, v in seen.items() if k != "at"}, transfer=card["transfer_number"], lines=[]))
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
        return _remember(conn, card_id, dict(out, state="error", final=seen_not or refused, text=_why(err)), now)
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
               if webmim_match.digits(typed) == number)


# ---- hourly -----------------------------------------------------------------------------------------------------------------------------

def _targets(conn, server: str) -> Dict[str, int]:
    """WebMIM number → the open order of the panel without a transfer number (not one of another server, nor a borrowed number; a number
    of two orders is left alone: a person sorts it)."""
    from . import oth_sale, webmim_match
    kinds = {s["id"]: s["kind"] for s in orders.list_stages(conn)}
    elsewhere = webmim_match.made_elsewhere(conn, server) | oth_sale.borrowed_ids(conn)
    seen: Dict[str, List[int]] = {}
    for card_id, number, transfer, stage in conn.execute(
            "SELECT id, webmim_number, transfer_number, stage_id FROM order_card WHERE webmim_number != ''"):
        key = webmim_match.digits(number)
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
        counts["sent"] += sum(1 for line in lines if line.startswith("Клиенту отправлено") or line.startswith("Точке «"))
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
