"""«Продать» of the point on «На ОТХ» with the window of the sale (the ТЗ «Невыкуп, ОТХ, возвраты» of 2026-11-12, part D; over «only whole»
of 2026-11-11, cancelled by the owner).

A record of storage (oth_storage) on «На ОТХ» sells some or all of its tyres to a buyer at the point. The window asks the buyer's name and
phone (both needed, the panel's check of a phone), the WebMIM number and the transfer number (the record's, may be corrected), the pieces of
each position (1 … what lies on OTH); the price is the record's (the order's own, or the one corrected in it), the sum counts itself, the
moment is the press's. «Клиент отказался предоставлять имя и телефон»: no name, no phone, no client in the CRM.

One transaction: a new order of the panel B (its client found or made by the phone: orders.create_card) takes the pieces as «Взять с ОТХ»
does (oth_sale._take_order: the purchase of the record, «с ОТХ»), gets the numbers and stands on «Выполнено» at once; the rules of
«Выполнено» record its sale, nothing is written to anybody. The record keeps what is left on «На ОТХ» with its dates (the seventh day
does not move); emptied, it goes to «Отмена» «Шины переданы в заказ №B». The client's order A it was made from stays in «Отмена» with
« О» on both numbers. The Google table gets no row of B: the rows of A say «Выполнено», «Реализация» «О» (order_sheet.common_of).
The stock of OTH in the feed goes down by the pieces sold and by nothing else (oth_stock reads the record).

The orders put on «На ОТХ» the old way (no record) keep the whole «Продать» of order_oth.sell."""
from __future__ import annotations

import json
import logging
from typing import Dict, List, Optional, Set

from . import order_oth, orders, oth_sale, oth_stock, oth_storage

SOLD_OF = "oth_point_sold:%d"           # A → [{"order": B, "record": Х, "pieces", "at", "by"}]: the point sold tyres of A's record
NAME_MAX, NUMBER_MAX, TRANSFER_MAX = 200, 60, 40
BONUS = "Бонус точке: +%s ₽ за каждую проданную шину."     # the rate «oth_sale_fee» of today (bonus_text; order_report.accruals counts it)
NO_CONTACT = "Клиент отказался предоставлять имя и телефон"
SOLD_NOTE = "Продано с ОТХ %d шт. в заказ №%d (%s)."
SKIP = "продажа с ОТХ на точке: покупателю сообщения не отправляются"


class SaleError(orders.OrderError):
    pass


def _state(conn, key: str):
    row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    try:
        return json.loads(row[0]) if row else None
    except ValueError:
        return None


def sold_of(conn, order_id) -> List[dict]:
    """The sales of the point from the record of the client's order `order_id` (A); [] for any other order."""
    found = _state(conn, SOLD_OF % int(order_id))
    return found if isinstance(found, list) else []


def all_sold(conn, order_id) -> bool:
    """Every tyre of the records A's sales came from is sold (none left in them): the table says «Выполнено» / «О» for A only then (the owner
    13.11: a part sold, the rest stays on «На ОТХ», A's row as it was with the note in «Примечание»)."""
    records = sorted({int(s["record"]) for s in sold_of(conn, order_id) if s.get("record")})
    if not records:
        return False
    left = conn.execute("SELECT COALESCE(SUM(qty), 0) FROM order_item WHERE order_id IN (%s)" % ",".join("?" * len(records)), records).fetchone()[0]
    return int(left or 0) == 0


def bonus_text(conn, day: str) -> str:
    from . import order_report
    rate = order_report.rate_on(order_report.list_rates(conn), "oth_sale_fee", day)
    return BONUS % (("%.2f" % rate).replace(".", ",").replace(",00", ""))


def sale_orders(conn) -> Set[int]:
    """The orders B the window «Продать» made (the audit of the sale, written in its transaction): the tyres of their sales bring the point
    the bonus for a sale from OTH (order_report.accruals)."""
    found: Set[int] = set()
    for (detail,) in conn.execute("SELECT detail FROM audit WHERE action = 'oth_point_sale'"):
        try:
            order = json.loads(detail or "{}").get("order")
        except (ValueError, AttributeError):
            continue
        if isinstance(order, int):
            found.add(order)
    return found


def why_not(conn, card: dict) -> Optional[str]:
    """Why the point cannot open the window for `card`; None: it can (a record on «На ОТХ», no return agreed or on agreement, nothing
    given back to the supplier)."""
    if not card.get("oth_record") and not oth_storage.is_record(conn, card["id"]):
        return "Это не запись хранения: продажа идёт кнопкой «Продать» целиком."
    if card["stage"]["code"] != "oth":
        return "Заказ №%s уже ушёл с «На ОТХ»: обновите страницу." % _label(card)
    if order_oth.return_plan(conn, card["id"]) is not None:
        return order_oth.AGREED_ANSWER % _label(card)
    if order_oth.agreeing(conn, card):
        return order_oth.AGREEING_ANSWER
    if order_oth.given_back(conn, card["id"]):
        return "Заказ №%s: часть шин уже возвращена поставщику. Продать нельзя: напишите офису." % _label(card)
    return None


def _label(card: dict) -> str:
    return str(card.get("webmim_number") or card["id"])


def lines(conn, card: dict) -> List[dict]:
    """The positions of the record the window sells: item, name, the pieces on OTH (the most that can be sold), the price of one."""
    on_oth = {r["item_id"]: int(r["qty"]) for r in oth_stock._rows(conn, manual=False) if r["order_id"] == card["id"]}
    out = []
    for item in card["items"]:
        have = on_oth.get(item["id"], 0)
        if have < 1:
            continue
        unit = float(item["sale_unit"] or 0)
        out.append({"item_id": item["id"], "name": item["name"], "goods_id": str(item["goods_id"]), "max": have, "unit": unit,
                    "sum": unit * have})
    return out


def _clean_contact(name, phone, anonymous: bool) -> tuple:
    if anonymous:                                                    # the buyer gave nothing: no client, whatever was typed
        return "", ""
    name = " ".join(str(name or "").split())
    phone = str(phone or "").strip()
    if not name:
        raise SaleError("Укажите имя покупателя или отметьте «%s»." % NO_CONTACT)
    if len(name) > NAME_MAX:
        raise SaleError("Имя покупателя не длиннее %d знаков." % NAME_MAX)
    if not phone:
        raise SaleError("Укажите телефон покупателя или отметьте «%s»." % NO_CONTACT)
    orders._clean("client_phone", phone, None)                       # the panel's check: 10–15 digits (OrderError)
    return name, phone


def _clean_number(value, limit: int, label: str) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        raise SaleError("%s не длиннее %d знаков." % (label, limit))
    return text


def sell(conn, record_id, pieces: Dict[int, int], name, phone, number, transfer, anonymous: bool, now: str, who: str) -> int:
    """The window's «Продать»: B made on «Выполнено» with the pieces, as the module says. Returns B; SaleError (or OrderError) says why
    nothing was done. Its own transaction: an open one of the caller is a RuntimeError."""
    if conn.in_transaction:
        raise RuntimeError("oth_point_sale.sell: открыта транзакция вызывающего, продажа идёт своей транзакцией")
    name, phone = _clean_contact(name, phone, anonymous)
    conn.execute("BEGIN IMMEDIATE")
    try:
        b = _sell(conn, int(record_id), pieces, name, phone, number, transfer, now, who)
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    _rules(conn, b, now, who)
    return b


def _sell(conn, x: int, pieces: Dict[int, int], name: str, phone: str, number, transfer, now: str, who: str) -> int:
    card = orders.get_card(conn, x)
    why = why_not(conn, card)
    if why:
        raise SaleError(why)
    sellable = {line["item_id"]: line for line in lines(conn, card)}
    wanted = {}
    for item_id, qty in (pieces or {}).items():
        try:
            item_id, qty = int(item_id), int(qty)
        except (TypeError, ValueError):
            raise SaleError("Количество: целое число.") from None
        if qty == 0:
            continue
        line = sellable.get(item_id)
        if line is None:
            raise SaleError("Этих шин на ОТХ уже нет: обновите страницу.")
        if not 1 <= qty <= line["max"]:
            raise SaleError("%s: продать можно от 1 до %d шт." % (line["name"], line["max"]))
        if line["unit"] <= 0:
            raise SaleError("%s: в заказе не указана цена продажи. Напишите офису." % line["name"])
        wanted[item_id] = qty
    if not wanted:
        raise SaleError("Укажите количество: хотя бы 1 шт.")
    number = _clean_number(number if number is not None else card["webmim_number"], NUMBER_MAX, "Номер заказа")
    transfer = _clean_number(transfer if transfer is not None else card["transfer_number"], TRANSFER_MAX, "Номер перемещения")
    a = oth_storage.source_of(conn, x)
    done = next((s for s in orders.list_stages(conn) if s["code"] == "done"), None)
    if done is None:
        raise SaleError("В воронке нет этапа «Выполнено».")
    b = orders.create_card(conn, {"client_name": name, "client_phone": phone, "point_id": card["point_id"], "responsible": card.get("responsible") or "",
                                  "deal_kind": card.get("deal_kind") or "webmim",
                                  "comment": "Продажа с ОТХ на точке: шины записи хранения №%d" % x + (" (заказ №%d)" % a if a else "")},
                           now, who, require_contact=False, commit=False, many_open=True)
    items = {i["id"]: i for i in card["items"]}
    sold = 0
    for item_id, qty in wanted.items():
        item = items[item_id]
        line = orders.add_item(conn, b, goods_id=item["goods_id"], name=item["name"], brand_key=item["brand_key"], season=item["season"],
                               diameter=item["diameter"], territory=item["territory"], qty=qty, sale_unit=float(item["sale_unit"]),
                               supplier_price=float(item["supplier_price"]), terms=item["terms"] or {}, fee_pct=item["fee_pct"],
                               promo_pct=item["promo_pct"], code=item["code"] or "", now=now, author=who, commit=False)
        if oth_sale._take_order(conn, b, line, item_id, now, who, commit=False) != qty:
            raise SaleError("Столько шин на ОТХ уже нет: обновите страницу.")
        sold += qty
    row = conn.execute("SELECT webmim_number, transfer_number FROM order_card WHERE id = ?", (b,)).fetchone()
    if (number, transfer) != tuple(row):                                       # what the window says is the order's (a correction stays)
        conn.execute("UPDATE order_card SET webmim_number = ?, transfer_number = ?, updated_at = ? WHERE id = ?", (number, transfer, now, b))
        orders.add_event(conn, b, now, who, "field", "Номер WebMIM: %s, номер перемещения: %s (из окна продажи)" % (number or "не указан", transfer or "не указан"))
    created = orders.get_card(conn, b)["stage"]
    if conn.execute("UPDATE order_card SET stage_id = ?, entered_at = ?, updated_at = ? WHERE id = ? AND stage_id = ?",
                    (done["id"], now, now, b, created["id"])).rowcount != 1:
        raise SaleError("Заказ продажи только что изменили: обновите страницу.")
    orders.add_event(conn, b, now, who, "stage", "Этап: «%s» → «%s»" % (created["name"], done["name"]))
    orders.add_event(conn, b, now, who, "point", "Точка продала шины с ОТХ: %d шт. (запись хранения №%d)%s" % (
        sold, x, "; %s" % NO_CONTACT.lower() if not (name or phone) else ""))
    left = orders.get_card(conn, x)
    mark = oth_storage.record(conn, x) or {}
    if left["stage"]["code"] == "cancel":                                       # emptied: closed «Шины переданы в заказ №B» by the take
        oth_storage._put(conn, oth_storage.MARK % x, dict(mark, state=oth_storage.TAKEN, to=b))
        order_oth._answer(conn, card, "sold", now)                              # an open question of the seventh day is answered by the sale
    orders.add_event(conn, x, now, who, "point", "Точка продала с ОТХ %d шт.: заказ №%d" % (sold, b))
    if a:
        _source(conn, a, x, b, sold, now, who)
    oth_storage._audit(conn, now, who, "oth_point_sale", {"record": x, "order": b, "source": a, "pieces": wanted, "sold": sold, "client": bool(name or phone)})
    return b


def _source(conn, a: int, x: int, b: int, sold: int, now: str, who: str) -> None:
    """The client's order A stays in «Отмена» with « О» on both numbers; its journal says what was sold (the Google table reads it again)."""
    row = orders._row(conn, a)
    marked = (oth_storage.with_letter(row["webmim_number"]), oth_storage.with_letter(row["transfer_number"]))
    if marked != (row["webmim_number"], row["transfer_number"]):
        conn.execute("UPDATE order_card SET webmim_number = ?, transfer_number = ?, updated_at = ? WHERE id = ?", marked + (now, a))
    orders.add_event(conn, a, now, who, "field", "Шины с ОТХ (запись №%d) продала точка: заказ №%d, %d шт." % (x, b, sold))
    note = SOLD_NOTE % (sold, b, "%s.%s" % (now[8:10], now[5:7]))       # the owner 13.11: the comment of A says it (and its «Примечание» in the table)
    conn.execute("UPDATE order_card SET comment = TRIM(COALESCE(comment, '') || ' ' || ?), updated_at = ? WHERE id = ?", (note, now, a))
    oth_storage._put(conn, SOLD_OF % a, sold_of(conn, a) + [{"order": b, "record": x, "pieces": sold, "at": now, "by": who}])


def _rules(conn, b: int, now: str, who: str) -> None:
    """The rules of «Выполнено» for B (its sale); the messages they prepare are closed: nobody is written by the sale."""
    orders._fire_rules(conn, b, now, ())
    try:
        from . import order_engine
        done = orders.get_card(conn, b)["stage_id"]
        for task in order_engine.list_tasks(conn, b, ("pending", "error")):
            if task["kind"] == "message" and task["stage_id"] == done and order_engine.audience(conn, task) == "client":
                order_engine._finish(conn, task, "skipped", SKIP, now, who)
    except Exception:                                                            # noqa: BLE001 - the sale stands
        conn.rollback()
        logging.getLogger("tires_sync.orders").exception("the messages of order %s sold from «На ОТХ» were not skipped", b)
