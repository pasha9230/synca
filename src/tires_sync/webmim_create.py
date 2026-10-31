"""«Оформить в WebMIM»: an order of the panel made in WebMIM by a person's button (the owner and the partner; docs/integrations/webmim-orders-api-v2.md).
Delivery to the address of the order's point, the fields as the owner fills WebMIM's form by hand (2026-10-29: «безнал», «Отсрочка», contact
«Админ», the point's phone and time, «Грузим на ОТХ»; fields()), `externalNumber` = the order's number in the panel.
No approve, no rule of a stage presses it. The first full success moves an order standing before «Заказ оформлен» there the ordinary way
(the owner 2026-10-23: its checks, journal and rules of the stage); then the route calls order_placed.on_placed (the client's message
«Клиенту: заказ оформлен», the owner 2026-10-23): the move first, so the hook sends the message the rule of the stage has just prepared, never a
second one (order_placed._earlier).

No doubles: the order is taken («оформляется») in one IMMEDIATE transaction before WebMIM is asked; a lost answer to the order is never sent again
blindly: the next press looks for our number in WebMIM's list first; positions are read before any is added (the same position again adds to its
quantity in WebMIM), only the difference goes. A part made (the order without some positions) keeps the order's id and number: the next press
adds what lacks. The state of the presses: the table webmim_create (store.py)."""
from __future__ import annotations

import hashlib
import logging
import re
import socket
import time
from datetime import date, datetime
from typing import Callable, Dict, List, Optional, Tuple

from . import orders, oth_sale, reliability, webmim_match, webmim_orders
from .order_points import PointError, get_point

# The servers the button may write to. The live one only by the owner's word: add "prod" here (a change of the code, reviewed and deployed),
# after the owner has confirmed the values below against the live dictionaries.
CREATE_SERVERS = ("test",)
# What is taken from WebMIM's dictionaries (names, compared without case), as the owner fills the form by hand (2026-10-29): the type «безнал»,
# the terms «Отсрочка», the shipment «Доставка». Nothing fits: the button refuses and lists the values.
SHIPMENT = "доставка"
PAYMENT_TERMS = "отсрочка"
PAYMENT_TYPES = ("безнал",)
CONTACT = "Админ"                    # the contact person of the delivery (the owner 2026-10-29); the phone is the point's
NOTE_FOR_SUPPLIER = "Грузим на ОТХ"  # «Примечание для поставщика», always (the owner 2026-10-29)
LATE_HOUR = 16                       # WebMIM: an order for today goes to work before 16:00 (its own warning in the form)

NO_ADDRESS = "не указан адрес доставки WebMIM"   # the refusal that lists the server's addresses (_addresses_hint)
ALL_OTH = "Все позиции взяты с ОТХ: заказ в WebMIM не нужен."   # oth_sale: the tyres «с ОТХ» are at the point already
TEST_TAG = "тест"            # the test server makes only the orders with this tag (the team lead's R2 2026-10-23: the owner's own test orders)
STALE = 600                 # seconds: a «running» mark older than this is of a killed worker (order_engine.RUNNING_TIMEOUT)
BUDGET = 45.0               # seconds a press may spend on positions (nginx gives the page 60)
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
    """What keeps the order from WebMIM, before WebMIM is asked (the order's own data). `mode`: «new»; «lost» (an earlier press may have made it:
    a date passed since is no reason not to look for it); «made» (the order is in WebMIM: only the positions to add are checked)."""
    return [text for _key, text in checks(conn, card, today, mode)]


def checks(conn, card: dict, today: date, mode: str = "new") -> List[Tuple[str, str]]:
    """problems() with the line of «Сверка» each one belongs to (order_supplier): items, territory, point, address, phone, time, date."""
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
        if not (card.get("delivery_time") or point["default_time"]):
            out.append(("time", "Не указано время доставки (в заказе или время точки по умолчанию)."))
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
    (what the person saw is what goes). The card passed problems() (a point, its address and phone, a date)."""
    point = get_point(conn, card["point_id"])
    return {"date": card["delivery_date"], "address_id": point["webmim_address_id"], "point": point["name"],
            "time": (card.get("delivery_time") or point["default_time"])[:200], "person": CONTACT,
            "phone": webmim_phone(point["manager_phone"]), "note": NOTE_FOR_SUPPLIER, "external": str(card["id"]),
            "self_note": "Tires Sync, заказ №%d" % card["id"], "kind": PAYMENT_TYPES[0], "terms": PAYMENT_TERMS.capitalize(),
            "shipment": SHIPMENT.capitalize()}


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


def _claim(conn, card_id, server: str, now: str, today: date, author: str, seen: Optional[str] = None) -> Tuple[dict, str, bool]:
    """Take the order for this press, or raise OrderError (nothing asked of WebMIM). Returns (the card, the id of the WebMIM order the button made
    and the card still holds, whether an earlier press may have made one whose answer was lost)."""
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        card = orders.get_card(conn, card_id)
        before = get_state(conn, card_id)
        if before and before["state"] == "running" and _age(now, before["started_at"]) < STALE:
            raise orders.OrderError("Заказ уже оформляется в WebMIM (нажал %s в %s): дождитесь итога." % (before["author"], before["started_at"][11:16]))
        made = before["webmim_id"] if before else ""
        if made and not card["webmim_id"] and not card["webmim_number"]:
            made, before = "", None                          # the person cleared the WebMIM fields (a rollback): a press starts anew
        if made:
            if before["server"] != server:
                raise orders.OrderError("Заказ оформлен в WebMIM на другом сервере (%s): выбран %s, продолжать нельзя."
                                        % (webmim_orders.SERVER_NAMES[before["server"]], webmim_orders.SERVER_NAMES[server]))
            if str(card["webmim_id"]) != made:
                raise orders.OrderError("ID заказа WebMIM в карточке изменён вручную: оформление кнопкой не продолжается.")
            if before["state"] == "done":
                raise orders.OrderError("Заказ уже оформлен в WebMIM (№%s)." % (card["webmim_number"] or made))
        else:
            if card["webmim_id"] or card["webmim_number"]:
                raise orders.OrderError("У заказа уже есть номер WebMIM: оформление не нужно.")
            if before and before["state"] in ("unknown", "running") and before["server"] != server:
                raise orders.OrderError("Прошлое нажатие было на другом сервере (%s) и его итог неизвестен: верните тот сервер."
                                        % webmim_orders.SERVER_NAMES[before["server"]])
        lost = bool(before and not made and before["state"] in ("unknown", "running"))     # the order of an earlier press may be made
        if server == "test" and not _tagged(card):
            raise orders.OrderError("На тестовом сервере WebMIM оформляются только заказы с тегом «%s»." % TEST_TAG)
        found = problems(conn, card, today, "made" if made else "lost" if lost else "new")
        if found:
            raise orders.OrderError(" ".join(found))
        if seen == "" and not made:                          # the page's press of a new order comes from the window only
            raise orders.OrderError("Откройте окно подтверждения («Заказать в МИМ») и подтвердите заказ оттуда: в WebMIM ничего не отправлено.")
        if seen is not None and not made and seen != fingerprint(conn, card):
            raise orders.OrderError("Данные заказа изменились после открытия окна подтверждения: проверьте их снова и подтвердите ещё раз. "
                                    "В WebMIM ничего не отправлено.")
        first_at = before["first_at"] if (lost or made) else now
        conn.execute("INSERT INTO webmim_create (order_id, server, state, first_at, started_at, author, webmim_id, result, updated_at) "
                     "VALUES (?, ?, 'running', ?, ?, ?, ?, '', ?) ON CONFLICT (order_id) DO UPDATE SET server = excluded.server, "
                     "state = 'running', first_at = excluded.first_at, started_at = excluded.started_at, author = excluded.author, "
                     "webmim_id = excluded.webmim_id, result = '', updated_at = excluded.updated_at", (card_id, server, first_at, now, author, made, now))
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    return card, made, lost


class _OutOfTime(Exception):
    """The press has spent its BUDGET: the positions left wait for the next press."""


class _Incomplete(Exception):
    """The list of WebMIM's orders is longer than the pages read (webmim_match.MAX_PAGES): our number is not looked for everywhere."""


def _finish(conn, card_id, state: str, text: str, now: str, author: str, level: str) -> dict:
    conn.execute("UPDATE webmim_create SET state = ?, result = ?, updated_at = ? WHERE order_id = ?", (state, text[:1000], now, card_id))
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
            "deliveryPerson": data["person"], "deliveryPhone": data["phone"], "externalNumber": data["external"],
            "noteForSupplier": data["note"], "noteForSelf": data["self_note"]}
    chosen = "оплата: %s (id %s), признак оплаты: %s (id %s), отгрузка: %s (id %s), адрес %s" % (
        kind[1], kind[0], terms[1], terms[0], shipment[1], shipment[0], data["address_id"])
    return body, chosen


def _ours(conn, client, card: dict, now: str, sleep, out_of_time: Callable[[], bool] = lambda: False) -> List[dict]:
    """Every order of WebMIM with our number on every page of the list (up to webmim_match.MAX_PAGES; the rows are not by the creation date,
    so no date stops the reading); «taken»: the order is in another order of the panel."""
    taken = {r[0] for r in conn.execute("SELECT webmim_id FROM order_card WHERE webmim_id != '' AND id != ?", (card["id"],))}
    found, counts, read, last = {}, {}, 0, 0
    for rows in webmim_match._every_page(client, sleep, counts):  # every page: WebMIM's rows are not by date (2026-10-28)
        if (counts.get("pages_total") or 0) > webmim_match.MAX_PAGES:
            raise _Incomplete()                             # known from the first page: no need to read the rest
        if out_of_time():                                   # the page must answer before nginx gives up (60 s)
            raise _OutOfTime()
        read, last = read + 1, len(rows)
        for row in rows:
            order = webmim_match._order(row)
            if order and order["external_number"] == str(card["id"]):               # one order once: the pages may shift while read
                found[order["id"]] = dict(order, status_text=order["status_text"] or order["status_code"], taken=order["id"] in taken)
    total = counts.get("pages_total") or 0
    if total > webmim_match.MAX_PAGES or (not total and read >= webmim_match.MAX_PAGES and last >= webmim_match.PER_PAGE):
        raise _Incomplete()                                 # our number may be on a page not read: nothing is made
    return list(found.values())


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
        line, refused = _to_placed(conn, card_id, now, author)
        if line:
            result["text"] += " " + line
        result["stage_error"] = refused
    return result


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
        raise orders.OrderError("Выбран боевой сервер WebMIM: оформление заказа кнопкой на нём только по слову владельца.")
    if client is None:
        raise orders.OrderError("API-пользователь WebMIM не настроен (Подключения → WebMIM → Заказы в WebMIM).")
    try:
        card, webmim_id, lost = _claim(conn, card_id, server, now, today, author, seen)
    except orders.OrderError as err:
        raise orders.OrderError(str(err) + _addresses_hint(client, server, str(err))) from None
    where = "тестовый сервер" if server == "test" else "боевой сервер"
    start = clock()
    created, chosen = False, ""
    try:
        try:
            if not webmim_id:
                found = _ours(conn, client, card, now, sleep, lambda: clock() - start > BUDGET)                 # first: an order an earlier press made is never made again
                if found and (len(found) > 1 or found[0]["taken"] or not lost):
                    numbers = ", ".join("№%s (id %s%s)" % (o["number"], o["id"], ", уже в другом заказе панели" if o["taken"] else "") for o in found)
                    return _finish(conn, card_id, "unknown" if lost else "refused", "В WebMIM уже есть заказ с номером панели %d: %s. Ничего не "
                                   "создано: сверьте и сопоставьте вручную (Сверка с WebMIM или поля карточки)." % (card_id, numbers), now, author, "red")
                if found:
                    order = found[0]
                    line = "Найден заказ WebMIM №%s (id %s), созданный прошлым нажатием (%s): повторно не создаётся." % (order["number"], order["id"], where)
                else:
                    body, chosen = _payload(conn, card, client)
                    try:
                        made = client.create_order(body)
                    except (OSError, webmim_orders.OrdersError) as err:
                        if isinstance(err, webmim_orders.OrdersRefused):
                            raise
                        return _finish(conn, card_id, "unknown", "WebMIM не ответил на создание заказа (%s): заказ мог создаться. Нажмите ещё раз через "
                                       "минуту: панель сначала поищет его в WebMIM по номеру %d и не создаст второй." % (_why(err), card_id),
                                       now, author, "red")
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
        except _Incomplete as err:                           # before any order is made (webmim_id is empty): a repeat does not help
            return _finish(conn, card_id, "unknown" if lost else "refused", "Заказ в WebMIM не создан: %s. Сверьте в кабинете WebMIM по номеру "
                           "панели %d и оформите вручную%s." % (_why(err), card_id, " (итог прошлого нажатия неизвестен: заказ мог создаться)" if lost else ""),
                           now, author, "red")
        except (OSError, webmim_orders.OrdersError, _OutOfTime) as err:
            if webmim_id:
                return _finish(conn, card_id, "partial", "Заказ WebMIM недосоздан (%s). Нажмите «Досоздать в WebMIM»: панель прочитает, что уже есть "
                               "в заказе WebMIM, и добавит только недостающее." % _why(err), now, author, "red")
            if lost:                                         # an earlier press may have made it: the next press must still look for it first
                return _finish(conn, card_id, "unknown", "Итог прошлого нажатия всё ещё неизвестен (%s): нажмите ещё раз позже, панель сначала "
                               "поищет заказ в WebMIM по номеру %d." % (_why(err), card_id), now, author, "red")
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
    if isinstance(err, _Incomplete):
        return "прочитаны не все страницы списка заказов WebMIM (больше %d): свой номер найден бы не наверняка" % webmim_match.MAX_PAGES
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


def _positions(conn, card: dict, client, webmim_id: str, where: str, chosen: str, now: str, author: str, start: float, clock) -> dict:
    """Add what lacks: our quantity of each position less what WebMIM's order holds. More in WebMIM than ours is left as it is and said."""
    wanted: Dict[str, int] = {}
    names: Dict[str, str] = {}
    territory = ""
    for item in oth_sale.supplier_items(conn, card):              # only what lacks: the positions «с ОТХ» are never ordered
        if int(item["qty"] or 0) > 0:
            goods = str(item["goods_id"])
            wanted[goods] = wanted.get(goods, 0) + int(item["qty"])
            names.setdefault(goods, item["name"])
            territory = str(item["territory"])
    held: Dict[str, int] = {}
    for row in client.items(webmim_id):
        goods = str(row.get("goodsId", "")).strip()
        if not goods:
            continue
        qty = row.get("quantity")
        if isinstance(qty, str) and qty.strip().isascii() and qty.strip().isdigit():
            qty = int(qty.strip())
        if not isinstance(qty, (int, float)) or isinstance(qty, bool):      # unread, it would count as none and be sent again: WebMIM adds it
            raise webmim_orders.OrdersError("позиции заказа WebMIM: количество не разобрано")
        held[goods] = held.get(goods, 0) + int(qty)
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


def button(conn, card: dict, server: str) -> Optional[dict]:
    """What the card shows next to the block «WebMIM»: the button's label and the last result; None when the button has nothing to do
    (a number typed by hand, a deal from the point's stock, an order made)."""
    state = get_state(conn, card["id"])
    if card.get("deal_kind") == "stock" or (server == "test" and not _tagged(card)):
        return None
    typed = bool(card.get("webmim_id") or card.get("webmim_number"))
    if state and state["webmim_id"] and not typed:
        state = None                                                    # the fields were cleared: a press starts anew (_claim)
    where = "в тестовом WebMIM" if server == "test" else "в WebMIM"
    out = {"label": "", "state": state, "state_name": STATE_NAMES.get(state["state"], "") if state else "", "live": server in CREATE_SERVERS,
           "server": webmim_orders.SERVER_NAMES[server], "test": server == "test"}
    if state and state["webmim_id"]:
        if state["state"] != "done" and str(card.get("webmim_id") or "") == state["webmim_id"] and (card.get("stage") or {}).get("kind", "open") == "open":
            out["label"] = "Досоздать " + where
        return out
    if typed:
        return out if state and state["result"] else None               # a number typed by hand: no button
    if (card.get("stage") or {}).get("kind", "open") != "open":
        return out if state and state["result"] else None               # a closed order (cancelled, done): nothing to make
    out["label"] = "Оформить " + where
    return out
