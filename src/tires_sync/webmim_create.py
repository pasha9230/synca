"""«Оформить в WebMIM»: an order of the panel made in WebMIM by a person's button (the owner and the partner), on the test and the live server
(the owner 2026-11-06: «Сделай так, чтобы можно было Заказ МИМ кнопкой»). A real purchase on the live server: the money goes.
Delivery to the address of the order's point, the fields as the owner fills WebMIM's form by hand (2026-10-29: «безнал», «Отсрочка», contact
«Админ», the point's phone; 2026-11-06: the working hours 10:00-17:00 for every point, both notes «грузим на ОТХ», «Собственный номер» empty —
no `externalNumber`; fields()). No approve, no rule of a stage, no timer presses it. The first full success moves an order standing before «Заказ оформлен» there the ordinary way
(the owner 2026-10-23: its checks, journal and rules of the stage); then the route calls order_placed.on_placed (the client's message
«Клиенту: заказ оформлен», the owner 2026-10-23): the move first, so the hook sends the message the rule of the stage has just prepared, never a
second one (order_placed._earlier).

No doubles: the order is taken («оформляется») in one IMMEDIATE transaction before WebMIM is asked. The WebMIM order carries no number of the
panel, so a lost answer to the order (state «unknown», or a worker killed meanwhile) is never looked for nor sent again: the button stays shut
(held_back) until the owner or the partner checks WebMIM's cabinet and says which (resolve: the order found — its id goes to the card and
«Досоздать» adds what lacks; or no order there — a new press is open again), with a record of the journal. Positions are read before any is
added (the same position again adds to its quantity in WebMIM), only the difference goes. A part made (the order without some positions) keeps
the order's id and number: the next press adds what lacks. The state of the presses: the table webmim_create (store.py)."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import socket
import time
from datetime import date, datetime
from typing import Callable, Dict, List, Optional, Tuple

from . import orders, oth_sale, reliability, webmim_match, webmim_orders
from .order_points import PointError, get_point

# The servers the button may write to: the live one by the owner's word 2026-11-06 (he checked the values below against WebMIM's form).
CREATE_SERVERS = ("test", "prod")
# What is taken from WebMIM's dictionaries (names, compared without case), as the owner fills the form by hand (2026-10-29): the type «безнал»,
# the terms «Отсрочка», the shipment «Доставка». Nothing fits: the button refuses and lists the values.
SHIPMENT = "доставка"
PAYMENT_TERMS = "отсрочка"
PAYMENT_TYPES = ("безнал",)
CONTACT = "Админ"                    # the contact person of the delivery (the owner 2026-10-29); the phone is the point's
NOTE_FOR_SUPPLIER = "Грузим на ОТХ"  # the note of the text for 4 Точки (order_supplier.tochki_text; the owner 2026-10-29)
MIM_NOTE = "грузим на ОТХ"           # WebMIM: «Примечание для поставщика» and «Примечание к заказу», as written (the owner 2026-11-06)
MIM_TIME = "10:00-17:00"             # WebMIM: «Время работы клиента» of every point; the point's and the order's own time never go there (2026-11-06)
LATE_HOUR = 16                       # WebMIM: an order for today goes to work before 16:00 (its own warning in the form)

NO_ADDRESS = "не указан адрес доставки WebMIM"   # the refusal that lists the server's addresses (_addresses_hint)
ALL_OTH = "Все позиции взяты с ОТХ: заказ в WebMIM не нужен."   # oth_sale: the tyres «с ОТХ» are at the point already
TEST_TAG = "тест"            # the test server makes only the orders with this tag (the team lead's R2 2026-10-23: the owner's own test orders)
STALE = 600                 # seconds: a «running» mark older than this is of a killed worker (order_engine.RUNNING_TIMEOUT)
BUDGET = 45.0               # seconds a press may spend on positions (nginx gives the page 60)
EARLY = 600                 # seconds: a WebMIM order created so long before the press is not its order (a warning for the person)
RESOLVE_LEN = 20            # digits of an id or a number of WebMIM typed by resolve()
STATE_NAMES = {"running": "оформляется", "unknown": "ответ WebMIM потерян", "refused": "не оформлен", "partial": "недосоздан", "done": "оформлен"}
FIELDS = ("order_id", "server", "state", "first_at", "started_at", "author", "webmim_id", "result", "updated_at")


def get_state(conn, card_id) -> Optional[dict]:
    row = conn.execute("SELECT %s FROM webmim_create WHERE order_id = ?" % ", ".join(FIELDS), (card_id,)).fetchone()
    return dict(zip(FIELDS, row)) if row else None


def webmim_phone(raw) -> str:
    """+7(000)000-00-00, the form WebMIM asks for; '' when the number is not a Russian one."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) == 11 and digits[0] in "78":
        digits = digits[1:]
    if len(digits) != 10:
        return ""
    return "+7(%s)%s-%s-%s" % (digits[:3], digits[3:6], digits[6:8], digits[8:])


def _goods_ok(goods_id: str) -> bool:
    return goods_id.isascii() and goods_id.isdigit() and len(goods_id) <= 20


def problems(conn, card: dict, today: date, mode: str = "new") -> List[str]:
    """What keeps the order from WebMIM, before WebMIM is asked (the order's own data). `mode`: «new»; «made» (the order is in WebMIM: only the
    positions to add are checked). The working hours are MIM_TIME: neither the point's time nor the order's is needed."""
    return [text for _key, text in checks(conn, card, today, mode)]


def checks(conn, card: dict, today: date, mode: str = "new") -> List[Tuple[str, str]]:
    """problems() with the line of «Сверка» each one belongs to (order_supplier): items, territory, point, address, phone, date."""
    out = []
    if card.get("deal_kind") == "stock":
        return [("items", "Сделка из наличия точки: заказ в WebMIM для неё не нужен.")]
    stage = card.get("stage") or {}
    if stage.get("kind", "open") != "open":                 # cancelled (the client may cancel the own order, shop_cart.cancel), done, closed:
        return [("items", "Заказ закрыт («%s»): в WebMIM его не оформить." % stage.get("name", ""))]   # _claim checks it under its lock
    items = [i for i in oth_sale.supplier_items(conn, card) if int(i["qty"] or 0) > 0]      # the tyres taken from «На ОТХ» are ours already
    if not items and any(int(i["qty"] or 0) > 0 for i in card.get("items") or []):
        return [("items", ALL_OTH)]
    if not items:
        out.append(("items", "В заказе нет позиций с количеством."))
    for item in items:
        if str(item.get("brand_key") or "").startswith("S") and ":" in str(item.get("brand_key") or ""):
            out.append(("items", "Позиция «%s» другого поставщика: в WebMIM её не заказать." % item["name"]))
        elif not _goods_ok(str(item["goods_id"])):
            out.append(("items", "У позиции «%s» нет кода WebMIM." % item["name"]))
    territories = {str(i.get("territory") or "") for i in items}
    if "" in territories:
        out.append(("territory", "У позиции не указана территория WebMIM: добавьте позицию заново из выгрузки."))
    elif len(territories) > 1:
        out.append(("territory", "Позиции с разных территорий WebMIM: оформите их отдельными заказами."))
    if mode == "made":
        return out
    point = get_point(conn, card["point_id"]) if card.get("point_id") else None
    if point is None:
        out.append(("point", "Не выбрана точка выдачи."))
    else:
        if not point["webmim_address_id"]:
            out.append(("address", "У точки «%s» %s (Подключения → Точки)." % (point["name"], NO_ADDRESS)))
        if not webmim_phone(point["manager_phone"]):
            out.append(("phone", "У точки «%s» телефон не российский мобильный или не указан: он контактный телефон доставки." % point["name"]))
    if not card.get("delivery_date"):
        out.append(("date", "Не указана дата доставки."))
    elif card["delivery_date"] < today.isoformat() and mode == "new":
        out.append(("date", "Дата доставки в прошлом: WebMIM не принимает заказ задним числом."))
    return out

def late(card: dict, now: str) -> bool:
    """The delivery is today and it is LATE_HOUR or later: WebMIM's own warning (an order for today goes to work before 16:00)."""
    try:
        moment = datetime.fromisoformat(now)
    except (TypeError, ValueError):
        return False
    return card.get("delivery_date") == moment.date().isoformat() and moment.hour >= LATE_HOUR


def fields(conn, card: dict) -> dict:
    """What the order of WebMIM gets from the panel, the dictionaries aside: the same for the window «Подтверждение» and for POST /orders
    (what the person saw is what goes). The card passed problems() (a point, its address and phone, a date). No number of the panel goes
    («Собственный номер» empty, the owner 2026-11-06); the time and both notes are the same for every order."""
    point = get_point(conn, card["point_id"])
    return {"date": card["delivery_date"], "address_id": point["webmim_address_id"], "point": point["name"], "address": point["address"] or "",
            "time": MIM_TIME, "person": CONTACT, "phone": webmim_phone(point["manager_phone"]), "note": MIM_NOTE, "self_note": MIM_NOTE,
            "kind": PAYMENT_TYPES[0], "terms": PAYMENT_TERMS.capitalize(), "shipment": SHIPMENT.capitalize()}


def fingerprint(conn, card: dict) -> str:
    """The fields and the positions the window showed: a press with another one (the order changed meanwhile) is refused (_claim)."""
    items = sorted((str(i["goods_id"]), str(i.get("territory") or ""), int(i["qty"] or 0))      # what goes to WebMIM: never the tyres «с ОТХ»
                   for i in oth_sale.supplier_items(conn, card) if int(i["qty"] or 0) > 0)
    try:
        data = fields(conn, card) if card.get("point_id") else {}
    except PointError:                                                  # the point is gone: problems() refuses the press anyway
        data = {}
    raw = repr((webmim_match.current_server(conn), sorted(data.items()), items))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _tagged(card: dict) -> bool:
    return TEST_TAG in {str(t).casefold() for t in card.get("tags") or []}


def _age(now: str, then: str) -> float:
    try:
        return (datetime.fromisoformat(now) - datetime.fromisoformat(then)).total_seconds()
    except (TypeError, ValueError):
        return STALE + 1


def held_back(conn, card: dict, state: Optional[dict], now: Optional[str] = None) -> str:
    """Why a new press might buy twice: '' when it may not. The WebMIM order carries no number of the panel, so the button never looks for it:
    an earlier press whose answer was lost («unknown»; «running» older than STALE: a worker killed) or an order the button made whose id was
    cleared from the card leaves the button shut until a person says what is in WebMIM's cabinet (resolve). `now` None: a «running» mark counts
    as at work."""
    if not state:
        return ""
    if state["webmim_id"]:
        if card.get("webmim_id") or card.get("webmim_number"):
            return ""
        return ("Кнопка уже создала заказ WebMIM (id %s, %s), а поля WebMIM в карточке очищены: новое нажатие создало бы второй заказ у "
                "поставщика. Проверьте кабинет WebMIM: заказ есть — впишите его ID ниже и снимите отметку; удалён — подтвердите «Заказа в "
                "WebMIM нет»." % (state["webmim_id"], webmim_orders.SERVER_NAMES.get(state["server"], state["server"])))
    stale = state["state"] == "running" and now is not None and _age(now, state["started_at"]) >= STALE
    if state["state"] != "unknown" and not stale:
        return ""
    return ("Итог нажатия %s (%s, %s) неизвестен: заказ мог создаться в WebMIM. Повторное нажатие заблокировано, чтобы не купить дважды. "
            "Проверьте кабинет WebMIM: заказ на %s, адрес %s, позиций %d. Заказ есть — впишите его ID ниже и снимите отметку; нет — подтвердите "
            "«Заказа в WebMIM нет»." % (str(state["started_at"])[:16].replace("T", " "), state["author"],
                                         webmim_orders.SERVER_NAMES.get(state["server"], state["server"]), _day(card.get("delivery_date")),
                                         _address_of(conn, card), len([i for i in oth_sale.supplier_items(conn, card) if int(i["qty"] or 0) > 0])))


def _day(value) -> str:
    try:
        return date.fromisoformat(str(value or "")).strftime("%d.%m.%Y")
    except ValueError:
        return "(дата не указана)"


def _address_of(conn, card: dict) -> str:
    try:
        point = get_point(conn, card["point_id"]) if card.get("point_id") else None
    except PointError:
        point = None
    return "WebMIM %s (точка «%s»)" % (point["webmim_address_id"] or "?", point["name"]) if point else "(точка не выбрана)"


def _claim(conn, card_id, server: str, now: str, today: date, author: str, seen: Optional[str] = None) -> Tuple[dict, str]:
    """Take the order for this press, or raise OrderError (nothing asked of WebMIM). Returns (the card, the id of the WebMIM order the button made
    and the card still holds)."""
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        card = orders.get_card(conn, card_id)
        before = get_state(conn, card_id)
        if before and before["state"] == "running" and _age(now, before["started_at"]) < STALE:
            raise orders.OrderError("Заказ уже оформляется в WebMIM (нажал %s в %s): дождитесь итога." % (before["author"], before["started_at"][11:16]))
        held = held_back(conn, card, before, now)
        if held:
            raise orders.OrderError(held)
        made = before["webmim_id"] if before else ""
        if made:
            if before["server"] != server:
                raise orders.OrderError("Заказ оформлен в WebMIM на другом сервере (%s): выбран %s, продолжать нельзя."
                                        % (webmim_orders.SERVER_NAMES[before["server"]], webmim_orders.SERVER_NAMES[server]))
            if str(card["webmim_id"]) != made:
                raise orders.OrderError("ID заказа WebMIM в карточке изменён вручную: оформление кнопкой не продолжается.")
            if before["state"] == "done":
                raise orders.OrderError("Заказ уже оформлен в WebMIM (№%s)." % (card["webmim_number"] or made))
        elif card["webmim_id"] or card["webmim_number"]:
            raise orders.OrderError("У заказа уже есть номер WebMIM: оформление не нужно.")
        if server == "test" and not _tagged(card):
            raise orders.OrderError("На тестовом сервере WebMIM оформляются только заказы с тегом «%s»." % TEST_TAG)
        found = problems(conn, card, today, "made" if made else "new")
        if found:
            raise orders.OrderError(" ".join(found))
        if seen == "" and not made:                          # the page's press of a new order comes from the window only
            raise orders.OrderError("Откройте окно подтверждения («Заказать в МИМ») и подтвердите заказ оттуда: в WebMIM ничего не отправлено.")
        if seen is not None and not made and seen != fingerprint(conn, card):
            raise orders.OrderError("Данные заказа изменились после открытия окна подтверждения: проверьте их снова и подтвердите ещё раз. "
                                    "В WebMIM ничего не отправлено.")
        first_at = before["first_at"] if made else now
        conn.execute("INSERT INTO webmim_create (order_id, server, state, first_at, started_at, author, webmim_id, result, updated_at) "
                     "VALUES (?, ?, 'running', ?, ?, ?, ?, '', ?) ON CONFLICT (order_id) DO UPDATE SET server = excluded.server, "
                     "state = 'running', first_at = excluded.first_at, started_at = excluded.started_at, author = excluded.author, "
                     "webmim_id = excluded.webmim_id, result = '', updated_at = excluded.updated_at", (card_id, server, first_at, now, author, made, now))
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    return card, made


def _typed(webmim_id, number) -> Tuple[str, str]:
    webmim_id, number = str(webmim_id or "").strip(), str(number or "").strip()
    for label, value in (("ID", webmim_id), ("Номер", number)):
        if value and not (value.isascii() and value.isdigit() and len(value) <= RESOLVE_LEN):
            raise orders.OrderError("%s заказа WebMIM: только цифры, не больше %d." % (label, RESOLVE_LEN))
    return webmim_id, number


def _may_resolve(conn, card: dict, state: Optional[dict], now: str) -> None:
    if state and state["state"] == "running" and _age(now, state["started_at"]) < STALE:
        raise orders.OrderError("Заказ сейчас оформляется в WebMIM: дождитесь итога.")
    if not held_back(conn, card, state, now):
        raise orders.OrderError("Кнопка не заблокирована: снимать нечего.")


def _may_take(conn, card: dict, webmim_id: str, number: str = "") -> None:
    typed = str(card["webmim_id"] or "")
    if typed and typed != webmim_id:
        raise orders.OrderError("В карточке уже вписан другой ID заказа WebMIM (%s): поправьте его в карточке." % typed)
    other = conn.execute("SELECT id FROM order_card WHERE webmim_id = ? AND id != ?", (webmim_id, card["id"])).fetchone()
    if other:
        raise orders.OrderError("Заказ WebMIM с ID %s уже у заказа №%d: проверьте ID." % (webmim_id, other[0]))
    other = conn.execute("SELECT id FROM order_card WHERE webmim_number = ? AND id != ?", (number, card["id"])).fetchone() if number else None
    if other:                                                     # a number typed by hand on another order, without its id
        raise orders.OrderError("Заказ WebMIM №%s уже у заказа №%d: проверьте цифры." % (number, other[0]))


def _cabinet_order(client, webmim_id: str, number: str, sleep: Callable[[float], None]) -> Optional[dict]:
    """The order of WebMIM's cabinet with this id (or, without an id, this number), read from GET /api/v2/orders (the pages the snapshot
    reads, webmim_match._every_page); None: not there."""
    for rows in webmim_match._every_page(client, sleep, {}):
        for raw in rows:
            order = webmim_match._order(raw)
            if order and (order["id"] == webmim_id if webmim_id else order["number"] == number):
                return order
    return None


CANCELLED = ("отменен", "отменено", "аннулирован", "аннулировано")   # the whole status text of an order surely cancelled (no codes known here)


def _status(order: dict) -> str:
    return " ".join(str(order.get("status_text") or "").casefold().replace("ё", "е").split())


def cancelled(order: dict) -> bool:
    """A WebMIM order whose status speaks of a cancel at all («Отменён», «отменен клиентом», «Запрошена отмена», «Аннулирован»): never bound
    by «Заказ есть» (review 2026-11-06). Refusing is the safe side, so any such word stops it. The status codes of WebMIM are not known here."""
    text = _status(order)
    return "отмен" in text or "аннулир" in text


def surely_cancelled(order: dict) -> bool:
    """Cancelled for good (the whole status is one of CANCELLED): only this lets «Заказа нет» through while the order is in the cabinet —
    «Запрошена отмена» or «частично отменён» may still be a purchase."""
    return _status(order) in CANCELLED


class _Unread(Exception):
    """The cabinet was not read in full: «not found» would not mean «not there»."""


def _in_cabinet(client, webmim_id: str, sleep: Callable[[float], None]) -> Optional[dict]:
    """The order with this id among every order of WebMIM's cabinet (GET /api/v2/orders), read strictly for «Заказа нет»: unlike the
    snapshot (webmim_match._every_page) an answer that is not a list or an empty first page is OrdersError, and a cabinet of more pages
    than webmim_match.MAX_PAGES (or no count and a full last page) is _Unread. None: read in full, not there."""
    page, total = 1, 0
    while True:
        if page > 1:
            sleep(webmim_match.PAUSE)
        data, headers = client.get_with_headers("/api/v2/orders", {"page": page, "per-page": webmim_match.PER_PAGE})
        if not isinstance(data, list):
            raise webmim_orders.OrdersError("/api/v2/orders: ответ не список заказов.", 200)
        if page == 1:
            if not data:
                raise webmim_orders.OrdersError("/api/v2/orders: кабинет прочитан пустым.", 200)
            total = webmim_orders._int_header(headers, "x-pagination-page-count")
            if total > webmim_match.MAX_PAGES:
                raise _Unread()
        for raw in data:
            order = webmim_match._order(raw)
            if order and order["id"] == webmim_id:
                return order
        if (page >= total) if total else (len(data) < webmim_match.PER_PAGE):
            return None
        if page >= webmim_match.MAX_PAGES:
            raise _Unread()
        page += 1


def _seen_text(order: dict, where: str) -> str:
    return "%s(id %s, %s), создан %s%s" % ("№%s " % order["number"] if order["number"] else "", order["id"], where,
                                         order["create_date"][:16].replace("T", " ") or "(дата не указана)",
                                         ", статус «%s»" % order["status_text"] if order["status_text"] else "")


def check(conn, card_id, client, now: str, webmim_id: str = "", number: str = "", sleep: Callable[[float], None] = time.sleep,
          other: bool = False) -> dict:
    """«Заказ есть»: the order typed is read from WebMIM's cabinet (GETs only, nothing written anywhere) before the mark is taken off, so a
    wrong digit never sends «Досоздать» into somebody else's order. OrderError: not there, not read, cancelled, or it does not fit — nothing
    changes. The panel knows the id of the order its button made (review 2026-11-06): another order is taken only with `other` (the person's
    own confirmation that it is another order). Returns what the person confirms: {"id", "number", "created", "status", "positions", "add",
    "more", "server", "token", "early", "other"}; `token` binds the confirmation to this reading (resolve reads again and compares)."""
    webmim_id, number = _typed(webmim_id, number)
    if not webmim_id and not number:
        raise orders.OrderError("Впишите ID или номер заказа WebMIM из кабинета.")
    card = orders.get_card(conn, card_id)
    state = get_state(conn, card_id)
    _may_resolve(conn, card, state, now)
    if client is None:
        raise orders.OrderError("API-пользователь WebMIM не настроен (Подключения → WebMIM → Заказы в WebMIM): заказ не проверить, отметка не снята.")
    where = webmim_orders.SERVER_NAMES.get(state["server"], state["server"])
    typed = "ID %s" % webmim_id if webmim_id else "№%s" % number
    try:
        order = _cabinet_order(client, webmim_id, number, sleep)
        if order is None:
            raise orders.OrderError("Заказа WebMIM с %s нет в кабинете (%s): проверьте цифры. Отметка не снята, ничего не изменено." % (typed, where))
        if webmim_id and number and order["number"] != number:
            raise orders.OrderError("Заказ WebMIM с ID %s имеет №%s, а вписан №%s: проверьте цифры. Отметка не снята." % (webmim_id, order["number"], number))
        if cancelled(order):
            raise orders.OrderError("Заказ WebMIM %s отменён: отменённый заказ не привязывается и не досоздаётся. Отметка не снята. Если "
                                    "действующего заказа в кабинете нет, подтвердите «Заказа в WebMIM нет»." % _seen_text(order, where))
        known = state["webmim_id"]
        if known and order["id"] != known and not other:
            raise orders.OrderError("Кнопка создала заказ WebMIM id %s, а вписан другой: %s. Ошибка в цифре привязала бы чужой заказ, и "
                                    "«Досоздать» дописал бы в него все позиции. Проверьте цифры; если это правда другой заказ, отметьте «Это "
                                    "другой заказ» и проверьте снова. Отметка не снята." % (known, _seen_text(order, where)))
        rows = client.items(order["id"])
        held = _held(rows)
    except (OSError, webmim_orders.OrdersError) as err:
        raise orders.OrderError("Заказ WebMIM %s не прочитан (%s, %s): отметка не снята, ничего не изменено." % (typed, where, _why(err))) from None
    _may_take(conn, card, order["id"], order["number"])
    wanted, names, _ = _wanted(conn, card)
    given = {str(r["goodsId"]).strip(): str(r.get("goodsName") or "").strip() for r in rows}
    positions = ["«%s» — %d шт" % (given.get(g) or names.get(g) or g, q) for g, q in held.items()]
    add = ["«%s» — %d шт" % (names[g], q - held.get(g, 0)) for g, q in wanted.items() if q > held.get(g, 0)]
    more = ["«%s»: в WebMIM %d шт, в заказе %d" % (names[g], held[g], q) for g, q in wanted.items() if held.get(g, 0) > q]
    created = order["create_date"][:16].replace("T", " ")
    early = bool(order["create_date"]) and _age(state["first_at"], order["create_date"]) > EARLY
    differs = bool(known) and order["id"] != known
    token = hashlib.sha256(json.dumps([card_id, order["id"], order["number"], order["create_date"], sorted(held.items()), sorted(wanted.items()),
                                       known if differs else ""]).encode("utf-8")).hexdigest()[:20]
    return {"id": order["id"], "number": order["number"], "created": created, "status": order["status_text"], "positions": positions,
            "add": add, "more": more, "server": where, "token": token,
            "early": ("Внимание: заказ создан раньше нажатия кнопки (%s): похоже, это другой, старый заказ." % state["first_at"][:16].replace("T", " "))
            if early else "",
            "other": ("Внимание: кнопка создала заказ WebMIM id %s, а это другой заказ (id %s). «Досоздать» допишет позиции в него."
                      % (known, order["id"])) if differs else ""}


def _found_text(seen: dict) -> str:
    return "%s(id %s, %s), создан %s%s; в нём: %s. «Досоздать» добавит: %s.%s%s" % (
        "№%s " % seen["number"] if seen["number"] else "", seen["id"], seen["server"], seen["created"] or "(дата не указана)",
        ", %s" % seen["status"] if seen["status"] else "", "; ".join(seen["positions"]) or "позиций нет", "; ".join(seen["add"]) or "ничего",
        (" В WebMIM больше, чем в заказе: %s." % "; ".join(seen["more"])) if seen["more"] else "",
        "".join(" " + seen[k] for k in ("early", "other") if seen.get(k)))


def _gone(conn, card_id, client, now: str, sleep: Callable[[float], None]) -> Tuple[str, str]:
    """«Заказа нет» while the panel knows the id the button made (review 2026-11-06): that order is read from WebMIM's cabinet first (GETs
    only). Returns (the id read, a note of the journal); OrderError (nothing changes): it is there and not cancelled, not read, no client."""
    card = orders.get_card(conn, card_id)
    state = get_state(conn, card_id)
    _may_resolve(conn, card, state, now)
    known = state["webmim_id"]
    if not known:
        return "", ""
    where = webmim_orders.SERVER_NAMES.get(state["server"], state["server"])
    if client is None:
        raise orders.OrderError("Кабинет WebMIM недоступен панели (не настроен API-пользователь в Подключения → WebMIM → Заказы в WebMIM, "
                                "не читаются секреты или сервер закрыт для кнопки): заказ id %s, созданный кнопкой, не проверить. Отметка не "
                                "снята." % known)
    try:
        order = _in_cabinet(client, known, sleep)
    except (OSError, webmim_orders.OrdersError) as err:
        raise orders.OrderError("Заказ WebMIM id %s, созданный кнопкой, не прочитан (%s, %s): отметка не снята, ничего не изменено."
                                % (known, where, _why(err))) from None
    except _Unread:
        raise orders.OrderError("Кабинет WebMIM (%s) не прочитан целиком (больше %d заказов): нельзя убедиться, что заказа id %s, созданного "
                                "кнопкой, там нет. Отметка не снята; если он есть, нажмите «Заказ есть»." % (
                                    where, webmim_match.MAX_PAGES * webmim_match.PER_PAGE, known)) from None
    if order is None:
        return known, ""
    if surely_cancelled(order):
        return known, " (прежний заказ %s отменён в кабинете)" % _seen_text(order, where)
    raise orders.OrderError("Заказ WebMIM %s, созданный кнопкой, есть в кабинете: отметка не снята, новый заказ не откроется. Это он — "
                            "нажмите «Заказ есть: проверить в WebMIM» (его ID уже вписан). Удалите или отмените его в кабинете, если он не "
                            "нужен, и подтвердите «Заказа нет» снова." % _seen_text(order, where))


def resolve(conn, card_id, found: bool, now: str, author: str, webmim_id: str = "", number: str = "", client=None, confirm: str = "",
            sleep: Callable[[float], None] = time.sleep, other: bool = False) -> str:
    """A person's way out of held_back (the owner and the partner: the route's right «supplier»), after a look at WebMIM's cabinet. `found`:
    the order is there — it is read from WebMIM again (check) and must be what the person confirmed (`confirm`: the token check gave);
    its id and number go to the card, the mark becomes «недосоздан» with that id, so «Досоздать» reads what WebMIM's order holds and adds
    only what lacks; an id other than the one the button made needs `other` (check). Not found: when the panel knows the id the button made,
    that order is read from the cabinet first (_gone: there and not cancelled — refused); then the mark becomes «не оформлен» without an id,
    a new press makes a new order. A record of the journal either way; OrderError: nothing written."""
    seen, read, note = None, "", ""
    if found:
        seen = check(conn, card_id, client, now, webmim_id, number, sleep, other)   # GETs only, before the write lock
        if not confirm or str(confirm) != seen["token"]:
            raise orders.OrderError("Заказ WebMIM %s изменился после проверки или не был показан: проверьте его снова и подтвердите. Отметка не снята."
                                    % ("№" + seen["number"] if seen["number"] else "id " + seen["id"]))
    else:
        read, note = _gone(conn, card_id, client, now, sleep)                       # GETs only, before the write lock
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        card = orders.get_card(conn, card_id)
        state = get_state(conn, card_id)
        _may_resolve(conn, card, state, now)
        where = webmim_orders.SERVER_NAMES.get(state["server"], state["server"])
        if seen is not None:
            _may_take(conn, card, seen["id"], seen["number"])
            change = {"webmim_id": seen["id"]}
            if seen["number"] and not card["webmim_number"]:
                change["webmim_number"] = seen["number"]
            orders.update_card(conn, card_id, change, now, author, commit=False)
            text = ("Отметка снята вручную (%s): заказ WebMIM %s проверен в кабинете. Если в нём не все позиции, нажмите «Досоздать в WebMIM»: "
                    "панель прочитает его снова и добавит только недостающее." % (author, _found_text(seen)))
            conn.execute("UPDATE webmim_create SET state = 'partial', webmim_id = ?, result = ?, updated_at = ? WHERE order_id = ?",
                         (seen["id"], text[:1000], now, card_id))
        else:
            if card["webmim_id"] or card["webmim_number"]:
                raise orders.OrderError("В карточке вписан номер WebMIM: заказ есть. Впишите его ID ниже или очистите поля карточки.")
            if state["webmim_id"] != read:                                  # the mark changed after the cabinet was read
                raise orders.OrderError("Отметка изменилась, пока читался кабинет WebMIM: обновите страницу и проверьте снова.")
            text = ("Отметка снята вручную (%s): в кабинете WebMIM (%s) заказа нет%s. Кнопка снова создаст новый заказ у поставщика."
                    % (author, where, note or (" (прежний id %s не найден в кабинете)" % read if read else "")))
            conn.execute("UPDATE webmim_create SET state = 'refused', webmim_id = '', result = ?, updated_at = ? WHERE order_id = ?", (text, now, card_id))
        orders.add_event(conn, card_id, now, author, "webmim", text, "grey")
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    return text


class _OutOfTime(Exception):
    """The press has spent its BUDGET: the positions left wait for the next press."""


def _finish(conn, card_id, state: str, text: str, now: str, author: str, level: str) -> dict:
    conn.execute("UPDATE webmim_create SET state = ?, result = ?, updated_at = ? WHERE order_id = ?", (state, text[:1000], now, card_id))
    if state == "done":                                               # «Досоздать» too: everything in the order is in its order of WebMIM now
        from . import order_webmim
        order_webmim.fresh_snapshot(conn, card_id, now)
    orders.add_event(conn, card_id, now, author, "webmim", text, level)
    conn.commit()
    return {"state": state, "text": text, "ok": state == "done"}


def _pick(values, wanted, label: str) -> Tuple[str, str]:
    """(id, name) of a dictionary {id: name} whose name is one of `wanted`; refusal (OrdersRefused) with the values when none or several fit."""
    if not isinstance(values, dict) or not values:
        raise webmim_orders.OrdersRefused("%s: справочник WebMIM пуст или не разобран." % label)
    names = {str(k): str(v).strip() for k, v in values.items()}
    found = [(k, v) for k, v in names.items() if v.casefold() in wanted]
    if len(found) != 1:
        listed = ", ".join("%s (id %s)" % (v, k) for k, v in names.items())
        raise webmim_orders.OrdersRefused("%s: в справочнике WebMIM %s, есть: %s." % (
            label, "несколько подходящих" if found else "нет подходящего", listed))
    return found[0]


def _number(value: str):
    return int(value) if value.isascii() and value.isdigit() else value


def addresses(client) -> List[dict]:
    """The delivery addresses of the client's server (GET /addresses, a read only): [{"id", "address"}]."""
    rows = client.get("/api/v2/addresses")
    return [{"id": str(a.get("id", "")).strip(), "address": str(a.get("address", ""))} for a in rows if isinstance(a, dict)] if isinstance(rows, list) else []


def _listed(rows: List[dict]) -> str:
    return "; ".join("%s — %s" % (a["id"], a["address"][:80]) for a in rows[:10]) or "адресов нет"


def _addresses_hint(client, server: str, refusal: str) -> str:
    """The server's addresses for a refusal that names a point without its WebMIM address (the owner 2026-10-23: no roundabout search);
    '' for any other refusal (a press at work, «Досоздать»: WebMIM is not asked)."""
    if NO_ADDRESS not in refusal:
        return ""
    try:
        return " Адреса доставки на сервере «%s»: %s." % (webmim_orders.SERVER_NAMES[server], _listed(addresses(client)))
    except (OSError, webmim_orders.OrdersError) as err:
        return " Список адресов WebMIM получить не удалось (%s)." % _why(err)


def _payload(conn, card: dict, client) -> Tuple[dict, str]:
    """(the body of POST /orders, the line of what was chosen). Reads the dictionaries; OrdersRefused when one lacks the value."""
    data = fields(conn, card)
    shipment = _pick(client.get("/api/v2/account/shipment-types"), (SHIPMENT,), "Вид отгрузки «Доставка»")
    terms = _pick(client.get("/api/v2/account/payment-terms"), (PAYMENT_TERMS,), "Признак оплаты «Отсрочка»")
    kind = _pick(client.get("/api/v2/account/payment-types"), PAYMENT_TYPES, "Вид оплаты «безнал»")
    rows = addresses(client)
    if not any(a["id"] == data["address_id"] for a in rows):
        raise webmim_orders.OrdersRefused("Адреса доставки %s (точка «%s») нет на этом сервере WebMIM. Доступные: %s."
                                          % (data["address_id"], data["point"], _listed(rows)))
    day = date.fromisoformat(data["date"])
    body = {"orderDate": day.strftime("%d.%m.%y"), "paymentType": _number(kind[0]), "paymentTerms": _number(terms[0]),
            "shipmentType": _number(shipment[0]), "deliveryAddressId": data["address_id"], "deliveryTime": data["time"],
            "deliveryPerson": data["person"], "deliveryPhone": data["phone"],
            "noteForSupplier": data["note"], "noteForSelf": data["self_note"]}
    chosen = "оплата: %s (id %s), признак оплаты: %s (id %s), отгрузка: %s (id %s), адрес %s" % (
        kind[1], kind[0], terms[1], terms[0], shipment[1], shipment[0], data["address_id"])
    return body, chosen


def _write_order(conn, card_id, order: dict, now: str, author: str, line: str) -> bool:
    """The made order's id, number and status go to the order (and to webmim_create) before any position: a part made keeps them.
    A number typed into the card while the press ran is not overwritten: False (the positions then wait for a person; the next press stops)."""
    conn.execute("UPDATE webmim_create SET webmim_id = ?, state = 'partial', updated_at = ? WHERE order_id = ?", (order["id"], now, card_id))
    row = orders._row(conn, card_id)
    if row["webmim_id"] or row["webmim_number"]:
        conn.commit()
        return False
    orders.update_card(conn, card_id, {"webmim_id": order["id"], "webmim_number": order["number"], "webmim_status": order["status_text"][:60]},
                       now, author, commit=False)
    from . import order_webmim                                        # the pieces this order of WebMIM was made with (2026-11-07)
    order_webmim.fresh_snapshot(conn, card_id, now)
    orders.add_event(conn, card_id, now, author, "webmim", line, "green")
    conn.commit()
    return True


def place(conn, card_id, client, server: str, now: str, today: date, author: str,
          sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic, seen: Optional[str] = None) -> dict:
    """One press of «Оформить в WebMIM» / «Досоздать в WebMIM». Raises OrderError when nothing is asked of WebMIM (the server, a check, the mark);
    otherwise returns {"state", "text", "ok", "stage_error"}: the text is a record of the order's journal, and after the first full success
    what became of the stage (the move has its own record); a stage that did not change is `stage_error`, shown apart from the green result.
    `client`: webmim_orders.OrdersClient of `server`. `seen`: the fingerprint the window «Подтверждение» showed ('' from the page: none was
    shown, a new order is refused; «Досоздать» has no window and needs none; None: not checked, the callers of the code)."""
    result = _press(conn, card_id, client, server, now, today, author, sleep, clock, seen)
    result["stage_error"] = ""
    if result["ok"]:                                    # «done» once: a done order refuses a press (_claim)
        _invoice(conn, card_id, client, now, server)
        line, refused = _to_placed(conn, card_id, now, author)
        if line:
            result["text"] += " " + line
        result["stage_error"] = refused
    return result


def _invoice(conn, card_id, client, now: str, server: str) -> None:
    """The invoice's number at once (the owner 2026-11-07: «сразу забирать № счёта после оформления»): one quick GET of the order made
    (webmim_invoice.after_place). Never fails the press: the order is made already."""
    try:
        from . import webmim_invoice
        webmim_invoice.after_place(conn, card_id, client, now, server)
    except Exception:                                                 # noqa: BLE001
        conn.rollback()
        logging.getLogger("tires_sync.orders").exception("the invoice number of order %s after WebMIM", card_id)


def _to_placed(conn, card_id, now: str, author: str) -> Tuple[str, str]:
    """The order placed in WebMIM goes to «Заказ оформлен» by the ordinary move when it stands on an open stage before it («Новый заказ»,
    «Ожидает наличие», the owner's own ones); at it, after it or on a closed one it stays. Returns (the move, why the stage did not change).
    Never raises: the WebMIM order is made already."""
    current = None
    try:
        placed = orders._placed_stage(conn)
        current = orders.get_card(conn, card_id)["stage"]
        if placed is None or current["kind"] != "open" or current["position"] >= placed["position"]:
            return "", ""
        if not orders.move_card(conn, card_id, placed["id"], now, author):
            return "", ""                               # somebody moved it meanwhile
        return "Этап: «%s» → «%s»." % (current["name"], placed["name"]), ""
    except orders.OrderError as err:                    # the stage's checks: the refusal is a red record of the journal already
        return "", "Этап не сменился (остался «%s»): %s Переведите заказ сами." % ((current or {}).get("name", ""), err)
    except Exception:
        conn.rollback()
        logging.getLogger("tires_sync.orders").exception("the move of order %s after WebMIM", card_id)
        return "", "Этап не сменился: сбой панели. Переведите заказ сами."


def _press(conn, card_id, client, server: str, now: str, today: date, author: str, sleep: Callable[[float], None], clock: Callable[[], float],
           seen: Optional[str] = None) -> dict:
    if server not in CREATE_SERVERS:
        raise orders.OrderError("Сервер WebMIM «%s» закрыт для кнопки." % webmim_orders.SERVER_NAMES.get(server, server))
    if client is None:
        raise orders.OrderError("API-пользователь WebMIM не настроен (Подключения → WebMIM → Заказы в WebMIM).")
    try:
        card, webmim_id = _claim(conn, card_id, server, now, today, author, seen)
    except orders.OrderError as err:
        raise orders.OrderError(str(err) + _addresses_hint(client, server, str(err))) from None
    where = "тестовый сервер" if server == "test" else "боевой сервер"
    start = clock()
    created, chosen = False, ""
    try:
        try:
            if not webmim_id:
                body, chosen = _payload(conn, card, client)
                try:
                    made = client.create_order(body)
                except (OSError, webmim_orders.OrdersError) as err:
                    if isinstance(err, (webmim_orders.OrdersRefused, reliability.ServiceUnavailable)):
                        raise                                # refused, or not asked at all (the pause after failures): nothing made
                    return _finish(conn, card_id, "unknown", "WebMIM не ответил на создание заказа (%s): заказ мог создаться. Не нажимайте снова: "
                                   "проверьте кабинет WebMIM (заказ на %s, адрес %s) и скажите панели, есть ли он (блок «WebMIM» заказа)."
                                   % (_why(err), _day(card.get("delivery_date")), _address_of(conn, card)), now, author, "red")
                if server == "test":                     # which fields WebMIM answers with (names only): is the transfer number among them
                    orders.add_event(conn, card_id, now, author, "webmim", "Поля ответа WebMIM на создание заказа (тестовый сервер, без значений): %s."
                                     % ", ".join(field_names(made)), "grey")
                order = {"id": str(made["id"]), "number": str(made.get("number") or ""),
                         "status_text": str(made.get("statusDescription") or made.get("statusCode") or "")}
                line = "Создан заказ WebMIM №%s (id %s, %s); %s." % (order["number"], order["id"], where, chosen)
                if not _write_order(conn, card_id, order, now, author, line):
                    return _finish(conn, card_id, "partial", "%s Но в карточке уже вписан другой номер WebMIM (вручную, пока шло нажатие): позиции не "
                                   "добавлены, кнопка остановлена. Сверьте: созданный заказ №%s (id %s)." % (line, order["number"], order["id"]),
                                   now, author, "red")
                webmim_id, created = order["id"], True
            return _positions(conn, card, client, webmim_id, where, chosen, now, author, start, clock)
        except (OSError, webmim_orders.OrdersError, _OutOfTime) as err:
            if webmim_id:
                return _finish(conn, card_id, "partial", "Заказ WebMIM недосоздан (%s). Нажмите «Досоздать в WebMIM»: панель прочитает, что уже есть "
                               "в заказе WebMIM, и добавит только недостающее." % _why(err), now, author, "red")
            if isinstance(err, webmim_orders.OrdersRefused):
                return _finish(conn, card_id, "refused", "Заказ в WebMIM не оформлен: %s" % err, now, author, "red")
            return _finish(conn, card_id, "refused", "WebMIM %s (%s): ничего не создано." % ("не ответил вовремя" if _slow(err) else "недоступен", _why(err)),
                           now, author, "red")
    except BaseException:
        conn.rollback()
        state = "partial" if webmim_id or created else "unknown"
        conn.execute("UPDATE webmim_create SET state = ?, updated_at = ? WHERE order_id = ? AND state = 'running'", (state, now, card_id))
        conn.commit()
        raise


def field_names(data, prefix: str = "", depth: int = 0) -> List[str]:
    """The names of the fields of a JSON answer, nested ones as «parent.child» («parent[].child» in a list), never a value; at most 100."""
    out: List[str] = []
    rows = [data] if isinstance(data, dict) else [d for d in data if isinstance(d, dict)][:1] if isinstance(data, list) else []
    for row in rows:
        for key, value in row.items():
            name = prefix + str(key)[:60]
            out.append(name)
            if depth < 3 and isinstance(value, dict):
                out += field_names(value, name + ".", depth + 1)
            elif depth < 3 and isinstance(value, list) and any(isinstance(v, dict) for v in value):
                out += field_names(value, name + "[].", depth + 1)
    return out[:100]


def _slow(err) -> bool:
    """WebMIM answered too slowly (the press's own time, the socket's timeout as such or inside urllib's URLError), not «unreachable»."""
    timeouts = (_OutOfTime, socket.timeout, TimeoutError)
    if isinstance(err, webmim_orders.OrdersError) and err.status in (408, 504):         # the request's or the gateway's timeout
        return True
    return isinstance(err, timeouts) or isinstance(getattr(err, "reason", None), timeouts)


def _why(err) -> str:
    if isinstance(err, _OutOfTime):
        return "время нажатия вышло"
    if _slow(err):
        status = getattr(err, "status", None) if isinstance(err, webmim_orders.OrdersError) else None
        return "время ожидания ответа вышло" + (" (код %s)" % status if status else "")
    if isinstance(err, webmim_orders.OrdersRefused):
        return "WebMIM отказал: %s" % str(err)[:200]
    if isinstance(err, reliability.ServiceUnavailable):           # not asked at all: the pause after failures in a row, and until when
        return str(err)[:200]
    if isinstance(err, OSError):
        return "нет ответа сети"
    return str(err)[:200] or "ошибка"


def _wanted(conn, card: dict) -> Tuple[Dict[str, int], Dict[str, str], str]:
    """(goods id → our quantity, goods id → name, the territory) of the supplier's positions: the positions «с ОТХ» are never ordered."""
    wanted: Dict[str, int] = {}
    names: Dict[str, str] = {}
    territory = ""
    for item in oth_sale.supplier_items(conn, card):
        if int(item["qty"] or 0) > 0:
            goods = str(item["goods_id"])
            wanted[goods] = wanted.get(goods, 0) + int(item["qty"])
            names.setdefault(goods, item["name"])
            territory = str(item["territory"])
    return wanted, names, territory


def _held(rows: list) -> Dict[str, int]:
    """goods id → the quantity WebMIM's order holds. A row not read (no goodsId, a quantity not a number) is OrdersError, never skipped:
    counted as none, «Досоздать» would send it again and WebMIM adds it to what is there (4 become 8)."""
    held: Dict[str, int] = {}
    for row in rows:
        goods = str(row.get("goodsId", "") if isinstance(row, dict) else "").strip()
        if not goods:
            raise webmim_orders.OrdersError("позиции заказа WebMIM: строка без кода товара (goodsId) не разобрана, ничего не добавлено")
        qty = row.get("quantity")
        if isinstance(qty, str) and qty.strip().isascii() and qty.strip().isdigit():
            qty = int(qty.strip())
        if not isinstance(qty, (int, float)) or isinstance(qty, bool):      # unread, it would count as none and be sent again: WebMIM adds it
            raise webmim_orders.OrdersError("позиции заказа WebMIM: количество не разобрано")
        held[goods] = held.get(goods, 0) + int(qty)
    return held


def _positions(conn, card: dict, client, webmim_id: str, where: str, chosen: str, now: str, author: str, start: float, clock) -> dict:
    """Add what lacks: our quantity of each position less what WebMIM's order holds. More in WebMIM than ours is left as it is and said."""
    wanted, names, territory = _wanted(conn, card)
    held = _held(client.items(webmim_id))
    refused, more = [], []
    for goods, qty in wanted.items():
        need = qty - held.get(goods, 0)
        if need < 0:
            more.append("«%s»: в WebMIM %d шт, в заказе %d" % (names[goods], held[goods], qty))
        if need <= 0:
            continue
        if clock() - start > BUDGET:
            raise _OutOfTime()
        try:
            client.add_item(webmim_id, goods, territory, need)
        except webmim_orders.OrdersRefused as err:
            refused.append("«%s» (%d шт): %s" % (names[goods], need, err))
    number = orders._row(conn, card["id"])["webmim_number"] or webmim_id
    tail = (" В WebMIM больше, чем в заказе (не тронуто): %s." % "; ".join(more)) if more else ""
    if refused:
        return _finish(conn, card["id"], "partial", "Заказ WebMIM №%s недосоздан (%s): позиций %d из %d. WebMIM не добавил: %s.%s Нажмите «Досоздать в WebMIM»."
                       % (number, where, len(wanted) - len(refused), len(wanted), "; ".join(refused), tail), now, author, "red")
    text = "Оформлен в WebMIM (%s): заказ №%s (id %s), позиций %d из %d%s.%s" % (
        where, number, webmim_id, len(wanted), len(wanted), "; " + chosen if chosen else "", tail)
    return _finish(conn, card["id"], "done", text, now, author, "green")


def dosozdat_text(conn, card: dict, state: dict) -> str:
    """The window of «Досоздать»: which WebMIM order, and what goes into it (our quantity of each position; WebMIM's own is read first)."""
    wanted, names, _ = _wanted(conn, card)
    lines = "; ".join("«%s» до %d шт" % (names[g], q) for g, q in wanted.items()) or "позиций поставщика нет"
    return ("Новый заказ не создаётся: позиции добавятся в заказ WebMIM %s(id %s, %s). Панель сначала прочитает этот заказ в WebMIM и "
            "добавит только недостающее до количества этого заказа: %s. Что в заказе WebMIM уже есть, не добавляется второй раз; если "
            "WebMIM вернёт позиции в непонятном виде, ничего не добавится." % (
                "№%s " % card["webmim_number"] if card.get("webmim_number") else "", state["webmim_id"],
                webmim_orders.SERVER_NAMES.get(state["server"], state["server"]), lines))


def button(conn, card: dict, server: str, now: Optional[str] = None) -> Optional[dict]:
    """What the card shows next to the block «WebMIM»: the button's label and the last result; None when the button has nothing to do
    (a number typed by hand, a deal from the point's stock, an order made). «held»: why the button is shut (held_back) — the person's way out
    is resolve() (the form of the block «WebMIM»)."""
    state = get_state(conn, card["id"])
    if card.get("deal_kind") == "stock" or (server == "test" and not _tagged(card)):
        return None
    typed = bool(card.get("webmim_id") or card.get("webmim_number"))
    where = "в тестовом WebMIM" if server == "test" else "в WebMIM"
    held = held_back(conn, card, state, now)
    out = {"label": "", "state": state, "state_name": STATE_NAMES.get(state["state"], "") if state else "", "live": server in CREATE_SERVERS,
           "server": webmim_orders.SERVER_NAMES[server], "test": server == "test", "held": held,
           "held_id": str(card.get("webmim_id") or (state["webmim_id"] if state and held else "") or ""),
           "held_number": str(card.get("webmim_number") or ""), "known_id": state["webmim_id"] if state and held else ""}
    if held:
        return out
    if state and state["webmim_id"]:
        if state["state"] != "done" and str(card.get("webmim_id") or "") == state["webmim_id"] and (card.get("stage") or {}).get("kind", "open") == "open":
            out["label"] = "Досоздать " + where
            out["confirm"] = dosozdat_text(conn, card, state)
        return out
    if typed:
        return out if state and state["result"] else None               # a number typed by hand: no button
    if (card.get("stage") or {}).get("kind", "open") != "open":
        return out if state and state["result"] else None               # a closed order (cancelled, done): nothing to make
    out["label"] = "Оформить " + where
    return out
