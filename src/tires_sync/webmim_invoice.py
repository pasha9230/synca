"""The number of the invoice of a WebMIM order («№ Счета» of WebMIM's cabinet, «Номер перемещения» of the panel's order, e.g. «26R-1554147»),
taken from WebMIM's own answers (the owner 2026-11-07: «Ты можешь сам брать по API номер счёта», «сразу забирать № счёта после оформления»).
The described API v2 has no such field; the live answers may have one not described. So every answer is searched (look) and the names of its
fields (never their values, but the number found) go once to the journal of the paired order of the panel: the team lead sees what WebMIM gives.

Where it is read (GET only, nothing is sent to WebMIM):
- hourly (webmim_match.run): the rows of GET /api/v2/orders of the live server; then GET /api/v2/orders/{id} for at most MAX_DETAILS open
  orders a run whose row had no number and whose «Номер перемещения» is empty (the ones asked longest ago first);
- once right after «Оформить» / «Досоздать в WebMIM» (webmim_create.place): GET /api/v2/orders/{id}, one attempt of webmim_orders.QUICK seconds
  with the token of the press, outside the breaker; the number is written on the live server only;
  a failure is a grey line only, the press stays a success.
A number found goes into an empty «Номер перемещения» (and into the empty transfer of a second order of WebMIM, order_mim_extra, of that
number). A number typed by a person is never overwritten: when it differs, a yellow record asks the office to look (once)."""
from __future__ import annotations

import json
import logging
import re
from typing import Callable, Dict, List, Optional, Tuple

from . import orders, webmim_orders

AUTHOR = "система"
# «26R-1554147»: two digits, one capital letter (Latin or Cyrillic), a hyphen, 4 to 10 digits: the whole value, never a part of it.
# An order's number (digits only), a date, a phone or a code of goods never fits.
PATTERN = re.compile(r"[0-9]{2}[A-ZА-ЯЁ]-[0-9]{4,10}")
STRONG = re.compile(r"invoice|bill|sch[eo]t|schyot|shchet|account|сч[её]т", re.I)   # a field named so is taken wherever it is in the answer
TYPED = re.compile(r"external|note|comment|remark|descr", re.I)   # a field of the order a person types freely: taken only when named STRONG
MAX_DETAILS = 10                 # GET /api/v2/orders/{id} at most so many a run (hourly)
ASKED = "webmim_invoice_asked"   # app_state: {WebMIM id: when it was asked by id last}, the open orders only
LIST = "список заказов"
BY_ID = "заказ по id"
FIELDS_PREFIX = "Поля ответа WebMIM (%s, без значений): "
SKIPPED = (200, 400, 404, 410, 422)  # this order only is refused (gone, not parsed): the next one is asked


def _walk(data, path: str, depth: int, strong: bool, out: List[Tuple[str, str, bool, int]]) -> None:
    """(path, value, under a STRONG name, depth) of every value of the invoice's form. Depth 1: a plain field of the order itself (not one a
    person types freely, TYPED); a list is a level deeper (its values are not the order's own field)."""
    if depth > 5:
        return
    if isinstance(data, dict):
        for key, value in data.items():
            name = str(key)[:60]
            here = path + ("." if path else "") + name
            _walk(value, here, depth + 1 + (1 if depth == 0 and TYPED.search(name) else 0), strong or bool(STRONG.search(name)), out)
    elif isinstance(data, list):
        for value in data[:50]:
            _walk(value, path + "[]", depth + 1, strong, out)
    elif isinstance(data, str) and PATTERN.fullmatch(data.strip()):
        out.append((path, data.strip(), strong, depth))


def look(data) -> dict:
    """{fields: the names of the answer's fields, number: the invoice ('' none or not sure), where: its field, doubt: why not taken}.
    Taken: a value of the invoice's form in a field named like one (STRONG) anywhere, or in a field of the order itself (the top level). The
    same form deeper under another name (a delivery's document, say) is only named in `doubt`. Two different values: none taken."""
    from .webmim_create import field_names
    found: List[Tuple[str, str, bool, int]] = []
    _walk(data, "", 0, False, found)
    taken = [(p, v) for p, v, strong, depth in found if strong or depth == 1]
    values = sorted({v for _, v in taken})
    out = {"fields": field_names(data) if isinstance(data, (dict, list)) else [], "number": "", "where": "", "doubt": ""}
    if len(values) == 1:
        out["number"] = values[0]
        out["where"] = next(p for p, v in taken if v == values[0])
    elif len(values) > 1:
        out["doubt"] = "похожие значения в нескольких полях (%s): не вписано" % ", ".join(sorted({p for p, _ in taken}))
    others = sorted({p for p, v, strong, depth in found if not (strong or depth == 1)})
    if others and not out["number"] and not out["doubt"]:
        out["doubt"] = "значение вида номера счёта во вложенном поле %s: не взято" % ", ".join(others)
    return out


def _same(a: str, b: str) -> bool:
    return "".join(str(a).split()).upper() == "".join(str(b).split()).upper()


def _once(conn, card_id: int, text: str) -> bool:
    return conn.execute("SELECT 1 FROM order_event WHERE order_id = ? AND author = ? AND text = ?", (card_id, AUTHOR, text)).fetchone() is None


def note_fields(conn, card_id: int, source: str, seen: dict, now: str) -> bool:
    """The grey record of the answer's field names, once per order of the panel and source (LIST, BY_ID). No commit."""
    prefix = FIELDS_PREFIX % source
    if conn.execute("SELECT 1 FROM order_event WHERE order_id = ? AND author = ? AND kind = 'webmim' AND substr(text, 1, ?) = ?",
                    (card_id, AUTHOR, len(prefix), prefix)).fetchone():
        return False
    names = ", ".join(seen["fields"]) or "(пусто)"
    if seen["number"]:
        tail = "номер счёта %s (поле %s)" % (seen["number"], seen["where"])
    else:
        tail = "номера счёта нет" + ("; " + seen["doubt"] if seen["doubt"] else "")
    orders.add_event(conn, card_id, now, AUTHOR, "webmim", (prefix + names)[:1500] + ". " + tail + ".", "grey")
    return True


def apply(conn, card_id: int, number: str, label: str, now: str, written_text: str = "") -> str:
    """The invoice `number` of the WebMIM order `label` for the order of the panel: written into an empty «Номер перемещения» ('written'),
    the same as typed ('same'), another typed by a person (a yellow record once, nothing changed: 'differs'). `written_text`: the grey
    record of the write when the number came from elsewhere (webmim_cabinet). No commit."""
    current = orders._row(conn, card_id)["transfer_number"]
    if not current:
        orders.update_card(conn, card_id, {"transfer_number": number}, now, AUTHOR, commit=False)
        orders.add_event(conn, card_id, now, AUTHOR, "webmim", written_text or "Номер перемещения из WebMIM: %s (счёт заказа WebMIM %s)." % (number, label), "grey")
        return "written"
    if _same(current, number):
        return "same"
    text = ("Номер перемещения в заказе «%s», а WebMIM даёт счёт %s (заказ WebMIM %s): сверьте, панель ничего не меняла."
            % (current, number, label))
    if _once(conn, card_id, text):
        orders.add_event(conn, card_id, now, AUTHOR, "webmim", text, "yellow")
    return "differs"


def apply_extra(conn, card_id: int, item_id: int, number: str, label: str, now: str) -> str:
    """The same for the second order of WebMIM of the pieces added to a position (order_mim_extra), with the audit of the manual way
    (order_webmim.set_extra_numbers). No commit."""
    from . import store
    row = conn.execute("SELECT transfer_number FROM order_mim_extra WHERE order_id = ? AND item_id = ?", (card_id, item_id)).fetchone()
    if row is None:
        return ""
    name = conn.execute("SELECT name FROM order_item WHERE id = ?", (item_id,)).fetchone()
    name = name[0] if name else str(item_id)
    if not row[0]:
        conn.execute("UPDATE order_mim_extra SET transfer_number = ?, at = ?, author = ? WHERE order_id = ? AND item_id = ?",
                     (number, now, AUTHOR, card_id, item_id))
        store.write_audit(conn, now, AUTHOR, "order_mim_extra", {"order": card_id, "item": item_id, "transfer": number, "from": "webmim"})
        orders.add_event(conn, card_id, now, AUTHOR, "webmim", "Номер перемещения второго заказа WebMIM %s (добавленные шины «%s») из WebMIM: %s."
                         % (label, name, number), "grey")
        return "written"
    if _same(row[0], number):
        return "same"
    text = ("Номер перемещения второго заказа WebMIM %s (добавленные шины «%s») «%s», а WebMIM даёт счёт %s: сверьте, панель ничего не меняла."
            % (label, name, row[0], number))
    if _once(conn, card_id, text):
        orders.add_event(conn, card_id, now, AUTHOR, "webmim", text, "yellow")
    return "differs"


def _label(order: dict) -> str:
    return ("№%s (id %s)" % (order["number"], order["id"])) if order.get("number") else "id %s" % order["id"]


# ---- right after a press -------------------------------------------------------------------------------------------------------------

def after_place(conn, card_id: int, client, now: str, server: str = "prod") -> str:
    """One GET /api/v2/orders/{id} of the order the press made (webmim_orders.QUICK, no repeat). Never raises for WebMIM's sake: a failure is
    a grey line, the hourly reading takes the number later. The number goes into the order on the live server only (a test order's invoice is
    never ours: it would stand in the way of the real one); on the test server only the names of the fields. Commits. Returns what became of it."""
    from . import webmim_match
    row = orders._row(conn, card_id)
    webmim_id = webmim_match.digits(row["webmim_id"])
    if not webmim_id or len(webmim_id) > 20:
        return "no-id"
    try:
        data = client.order(webmim_id, quick=True)
    except (OSError, webmim_orders.OrdersError) as err:
        why = ("код %s" % err.status) if isinstance(err, webmim_orders.OrdersError) and err.status else type(err).__name__
        orders.add_event(conn, card_id, now, AUTHOR, "webmim", "Номер счёта WebMIM сразу после оформления не прочитан (%s): его возьмёт почасовое "
                         "чтение." % why, "grey")
        conn.commit()
        return "failed"
    seen = look(data)
    note_fields(conn, card_id, BY_ID, seen, now)
    result = "none"
    if seen["number"] and server != "prod":
        result = "test"
    elif seen["number"]:
        result = apply(conn, card_id, seen["number"], _label({"id": webmim_id, "number": webmim_match.digits(row["webmim_number"])}), now)
    conn.commit()
    return result


# ---- hourly ----------------------------------------------------------------------------------------------------------------------------

def _targets(conn, server: str, rows: Dict[str, dict]) -> Dict[str, dict]:
    """WebMIM id → {order, cards: [(id, transfer, open)], extras: [(card, item, transfer, open)]}: the orders of the panel paired with it the way
    the snapshot pairs (webmim_match.paired: the id first, else the number; not the ones of another server nor the borrowed numbers), and the
    second orders typed with its number. A WebMIM order paired with two orders of the panel is left out (a number typed twice: a person sorts it)."""
    from . import oth_sale, webmim_match
    by_id = {o["id"]: o for o in rows.values()}
    by_number = {webmim_match.digits(o["number"]): o for o in rows.values() if webmim_match.digits(o["number"])}
    kinds = {s["id"]: s["kind"] for s in orders.list_stages(conn)}
    elsewhere = webmim_match.made_elsewhere(conn, server) | oth_sale.borrowed_ids(conn)
    snap = {o["id"]: o for o in webmim_match.snapshot(conn, server)}
    elsewhere |= webmim_match.foreign_cards(conn, list(snap.values()))   # the number of another customer's order: not theirs
    out: Dict[str, dict] = {}
    for card_id, webmim_id, number, transfer, stage in conn.execute(
            "SELECT id, webmim_id, webmim_number, transfer_number, stage_id FROM order_card WHERE webmim_id != '' OR webmim_number != ''"):
        if card_id in elsewhere:
            continue
        order = webmim_match.paired({"webmim_id": webmim_id, "webmim_number": number}, by_id, by_number)
        if order is not None:
            out.setdefault(order["id"], {"order": order, "cards": [], "extras": []})["cards"].append((card_id, transfer, kinds.get(stage) == "open"))
    for card_id, item_id, number, transfer, stage, created_at, goods_id in conn.execute(
            "SELECT e.order_id, e.item_id, e.webmim_number, e.transfer_number, c.stage_id, c.created_at, i.goods_id FROM order_mim_extra e "
            "JOIN order_card c ON c.id = e.order_id LEFT JOIN order_item i ON i.id = e.item_id WHERE e.webmim_number != ''"):
        order = by_number.get(webmim_match.digits(number))         # a typed number: never looked up among the ids (webmim_match.paired)
        if order is not None and webmim_match.foreign({"created_at": created_at}, {str(goods_id)} if goods_id is not None else set(),
                                                      snap.get(order["id"])):
            continue                                               # another customer's order of the same number (webmim_match.foreign)
        if order is not None:
            out.setdefault(order["id"], {"order": order, "cards": [], "extras": []})["extras"].append((card_id, item_id, transfer, kinds.get(stage) == "open"))
    return {k: v for k, v in out.items() if len(v["cards"]) <= 1}


def _use(conn, target: dict, source: str, seen: dict, now: str, counts: dict) -> None:
    label = _label(target["order"])
    for card_id, _, _ in target["cards"]:
        counts["noted"] += note_fields(conn, card_id, source, seen, now)
        if seen["number"]:
            counts[apply(conn, card_id, seen["number"], label, now)] += 1
    for card_id, item_id, _, _ in target["extras"]:
        counts["noted"] += note_fields(conn, card_id, source, seen, now)       # once per order of the panel: a second call is a no-op
        if seen["number"]:
            counts[apply_extra(conn, card_id, item_id, seen["number"], label, now)] += 1
    conn.commit()


def _asked(conn) -> Dict[str, str]:
    row = conn.execute("SELECT value FROM app_state WHERE key = ?", (ASKED,)).fetchone()
    try:
        data = json.loads(row[0]) if row else {}
    except ValueError:
        data = {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def hourly(conn, client, server: str, raws: Dict[str, dict], now: str, sleep: Callable[[float], None]) -> dict:
    """After the snapshot (webmim_match.run): the rows of the list searched, then GET by id for the open orders still without a number. The live
    server only (a test order is never ours). Commits each order. A failure of a GET by id ends this part only (the snapshot stands)."""
    from . import webmim_match
    counts = {"noted": 0, "written": 0, "same": 0, "differs": 0, "": 0, "asked": 0, "error": ""}
    if server != "prod" or not raws:
        return counts
    rows = {}
    for raw in raws.values():
        order = webmim_match._order(raw)
        if order is not None:
            rows[order["id"]] = order
    targets = _targets(conn, server, rows)
    wanted = []
    for webmim_id, target in targets.items():
        seen = look(raws.get(webmim_id))
        _use(conn, target, LIST, seen, now, counts)
        if not seen["number"] and (any(open_ and not transfer for _, transfer, open_ in target["cards"])
                                   or any(open_ and not transfer for _, _, transfer, open_ in target["extras"])):
            wanted.append(webmim_id)
    before = _asked(conn)
    wanted.sort(key=lambda i: (before.get(i, ""), i))
    asked = {k: v for k, v in before.items() if k in wanted}
    try:
        for webmim_id in wanted[:MAX_DETAILS]:
            sleep(webmim_match.PAUSE)
            asked[webmim_id] = now
            counts["asked"] += 1
            try:
                data = client.order(webmim_id)
            except webmim_orders.OrdersError as err:
                if err.status in SKIPPED:
                    continue
                raise
            _use(conn, targets[webmim_id], BY_ID, look(data), now, counts)
    except (OSError, webmim_orders.OrdersError) as err:
        counts["error"] = ("код %s" % err.status) if isinstance(err, webmim_orders.OrdersError) and err.status else type(err).__name__
    finally:
        conn.execute("INSERT INTO app_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (ASKED, json.dumps(asked)))
        conn.commit()
    return counts


def line(counts: dict) -> str:
    """The tail of the hourly line ('' when nothing happened)."""
    parts = []
    if counts["written"]:
        parts.append("вписано %d" % counts["written"])
    if counts["differs"]:
        parts.append("расходится с вписанным %d" % counts["differs"])
    if counts["asked"]:
        parts.append("запросов по id %d" % counts["asked"])
    if counts["error"]:
        parts.append("чтение по id прервано (%s)" % counts["error"])
    return ("; номер счёта: " + ", ".join(parts)) if parts else ""


def safe_hourly(conn, client, server: str, raws: Dict[str, dict], now: str, sleep: Callable[[float], None]) -> str:
    """hourly() whose own failure never fails the snapshot: (the tail of the line)."""
    try:
        return line(hourly(conn, client, server, raws, now, sleep))
    except Exception:                                                  # noqa: BLE001
        conn.rollback()
        logging.getLogger("tires_sync.orders").exception("the invoice numbers of WebMIM")
        return "; номер счёта: сбой панели"
