"""A new client of all the tyres of an order on «На ОТХ» (the plan agreed by the owner 2026-11-06): the order A on «На ОТХ» is a purchase of WebMIM
(its number, transfer, invoice), its client changes. When every tyre of A goes to the new client (none marked «возвращено поставщику», no tyre
of the point, no agreed return, no sale), A itself takes the new client and goes to «Товар на точке» — no new order:
- from the card on «На ОТХ» («Новый клиент», oth_sale.to_new_client): the name and the phone are the typed ones, the client of the phone is joined;
- by «Взять с ОТХ» of an order B holding nothing else (take_target): the client, the chats and the calls of B go to A, B goes to «Отмена»
  (CLIENT_TOOK, no message, no rule of the stage).
The old client is unlinked for good: the link put aside (order_oth_client) is erased, kept whole in the journal of actions (SWAP); his chats are
unlinked from A, the history and the calls stay. The fee of a handed client (order_handoff) of the old client is never paid (handed_fee); the fee
of B goes with its client into A. A part of the tyres: a new order takes them as before (oth_sale.take). No table of its own: the journal of
actions keeps the swaps."""
from __future__ import annotations

import json
from typing import Dict, Optional, Set, Tuple

from . import orders, oth_stock

SWAP = "oth_client_swap"
CLIENT_TOOK = "другое: Клиент взял шины заказа №%d, заказ продолжается там"     # a reason of «Отмена» as the window writes «другое»
FEE_OFF = "Сбор за передачу клиента точкой снят: клиент сменён"


def _error(text: str):
    from .oth_sale import OthSaleError
    return OthSaleError(text)


def why_not(conn, card: dict) -> Optional[str]:
    """Why the order on «На ОТХ» cannot give all its tyres to a new client in place; None: it can."""
    if card["stage"]["code"] != "oth":
        return "Заказ не на «На ОТХ»."
    if card["sale_ids"]:
        return "По заказу записана продажа."
    if conn.execute("SELECT 1 FROM oth_return_plan WHERE order_id = ?", (card["id"],)).fetchone():
        return "По заказу согласован возврат поставщику."
    if conn.execute("SELECT 1 FROM oth_return WHERE order_id = ? AND qty > 0", (card["id"],)).fetchone():
        return "Часть шин отмечена «возвращено поставщику»."
    held = {i["id"]: int(i["qty"]) for i in card["items"] if int(i["qty"]) >= 1}
    stock = {r["item_id"]: int(r["qty"]) for r in oth_stock._rows(conn, manual=False) if r["order_id"] == card["id"]}
    if not held:
        return "В заказе нет шин."
    if held != stock:
        return "В заказе есть шины, которые не переходят (шина точки или тестовая точка)."
    return None


def _swap(conn, card: dict, name: str, phone: str, now: str, author: str, from_order: Optional[int] = None) -> dict:
    """The old client leaves A for good, the new name and phone are written. No commit: saved with the move of the caller."""
    a = card["id"]
    aside = conn.execute("SELECT order_id, client_id, how, linked_at, detached_at, detached_by FROM order_oth_client WHERE order_id = ?", (a,)).fetchone()
    old_link = dict(zip(("order_id", "client_id", "how", "linked_at", "detached_at", "detached_by"), aside)) if aside else None
    linked = conn.execute("SELECT client_id, how, at FROM client_order WHERE order_id = ?", (a,)).fetchone()     # an order of before the stage
    old_client = old_link["client_id"] if old_link else (linked[0] if linked else None)
    conn.execute("DELETE FROM order_oth_client WHERE order_id = ?", (a,))
    conn.execute("DELETE FROM client_order WHERE order_id = ?", (a,))
    chats = [r[0] for r in conn.execute("SELECT id FROM avito_chat WHERE order_id = ? ORDER BY id", (a,))]
    conn.execute("UPDATE avito_chat SET order_id = NULL, unlinked = 1 WHERE order_id = ?", (a,))
    for row in chats:
        orders.add_event(conn, a, now, author, "chat", "Отвязан чат Ч-%d: клиент сменён" % row)
    fee = handed_fee(conn, a)
    old_name, old_phone = card["client_name"], card["client_phone"]
    orders.update_card(conn, a, {"client_name": name, "client_phone": phone, "avito_chat": ""}, now, author, commit=False, link_client=False)
    who = "№%d (%s)" % (old_client, ", ".join(v for v in (old_name, old_phone) if v) or "без имени") if old_client else \
        "без карточки клиента (%s)" % (", ".join(v for v in (old_name, old_phone) if v) or "без имени")
    orders.add_event(conn, a, now, author, "field", "Клиент сменён: %s → %s" % (who, ", ".join(v for v in (name, phone) if v)))
    if fee:
        orders.add_event(conn, a, now, author, "field", FEE_OFF)
    detail = {"order": a, "old_client": old_client, "old_link": old_link, "old_order_link": list(linked) if linked else None,
              "old_name": old_name, "old_phone": old_phone, "name": name, "phone": phone, "chats": chats, "fee_off": fee, "from_order": from_order}
    return detail


def _audit(conn, now: str, author: str, detail: dict) -> None:
    conn.execute("INSERT INTO audit (at, author, action, detail) VALUES (?,?,?,?)", (now, author, SWAP, json.dumps(detail, ensure_ascii=False)))


def _at_point(conn) -> dict:
    found = next((s for s in orders.list_stages(conn) if s["code"] == "at_point"), None)
    if found is None:
        raise _error("В воронке нет этапа «Товар на точке».")
    return found


def to_client(conn, card_id, name: str, phone: str, now: str, author: str) -> None:
    """«Новый клиент» of all the tyres of A: A takes the client and goes to «Товар на точке» by the ordinary move (its rules; the client of the
    phone is joined there, orders.move_card). One transaction; OthSaleError says why nothing changed. The messages are the caller's."""
    card = orders.get_card(conn, card_id)
    why = why_not(conn, card)
    if why:
        raise _error(why)
    busy = orders.open_order_conflict(conn, phone, card["point_id"], besides=card["id"])
    if busy is not None:
        raise _error("У клиента с телефоном %s уже есть открытый заказ №%d этой точки: откройте его." % (phone, busy["id"]))
    stage = _at_point(conn)
    try:
        _audit(conn, now, author, _swap(conn, card, name, phone, now, author))
        moved = orders.move_card(conn, card["id"], stage["id"], now, author)
    except orders.OrderError as err:
        conn.rollback()
        raise _error("Клиент не сменён: %s" % err) from None
    if not moved:
        raise _error("Клиент не сменён: заказ только что изменили. Обновите страницу.")


def take_target(conn, card_id, line_id, from_item) -> Optional[int]:
    """The order A whose tyres «Взять с ОТХ» of the position `line_id` of B would take all of, while B holds nothing else: A takes the client
    of B instead (take_client). None: the take goes as before (oth_sale.take)."""
    from . import oth_sale
    from .order_merge import placed_ids
    b = orders.get_card(conn, card_id)
    if oth_sale.why_not(conn, b) or b["sale_ids"]:
        return None
    line = next((i for i in b["items"] if i["id"] == int(line_id)), None)
    if line is None or line["id"] in oth_sale.taken_ids(conn, b["id"]) or line["id"] in placed_ids(conn, b["id"]):
        return None
    if [i["id"] for i in b["items"] if int(i["qty"]) >= 1] != [line["id"]]:
        return None
    source = next((r for r in oth_stock._rows(conn, manual=False) if r["item_id"] == int(from_item)), None)
    if source is None or source["order_id"] == b["id"] or str(source["goods_id"]) != str(line["goods_id"]):
        return None
    if b["point_id"] and b["point_id"] != source["point_id"]:
        return None
    a = orders.get_card(conn, source["order_id"])
    if why_not(conn, a) or [i["id"] for i in a["items"] if int(i["qty"]) >= 1] != [source["item_id"]] or int(source["qty"]) != int(line["qty"]):
        return None
    return a["id"]


def take_client(conn, card_id, line_id, from_item, now: str, author: str) -> int:
    """«Взять с ОТХ» of all the tyres of A into B holding nothing else: the client, the chats and the calls of B go to A, B goes to «Отмена»
    (CLIENT_TOOK: no message, no rule), A to «Товар на точке». One transaction. Returns A; the messages are the caller's."""
    a_id = take_target(conn, card_id, line_id, from_item)
    if a_id is None:
        raise _error("Шины уже не те: обновите страницу.")
    a, b = orders.get_card(conn, a_id), orders.get_card(conn, card_id)
    cancel = next((s for s in orders.list_stages(conn) if s["code"] == "cancel"), None)
    if cancel is None:
        raise _error("В воронке нет этапа «Отмена».")
    stage = _at_point(conn)
    try:
        detail = _swap(conn, a, b["client_name"], b["client_phone"], now, author, from_order=b["id"])
        link = conn.execute("SELECT client_id, how, at FROM client_order WHERE order_id = ?", (b["id"],)).fetchone()
        if link:
            conn.execute("DELETE FROM client_order WHERE order_id = ?", (b["id"],))
            conn.execute("INSERT INTO client_order (order_id, client_id, how, at) VALUES (?,?,?,?)", (a_id, link[0], link[1], link[2] or now))
        chats = [r[0] for r in conn.execute("SELECT id FROM avito_chat WHERE order_id = ? ORDER BY id", (b["id"],))]
        conn.execute("UPDATE avito_chat SET order_id = ? WHERE order_id = ?", (a_id, b["id"]))
        calls = conn.execute("UPDATE avito_call SET order_id = ? WHERE order_id = ?", (a_id, b["id"])).rowcount
        if b["avito_chat"]:
            orders.update_card(conn, a_id, {"avito_chat": b["avito_chat"]}, now, author, commit=False, link_client=False)
            orders.update_card(conn, b["id"], {"avito_chat": ""}, now, author, commit=False, link_client=False)
        detail.update(client=link[0] if link else None, moved_chats=chats, moved_calls=calls)
        _audit(conn, now, author, detail)
        orders.add_event(conn, a_id, now, author, "field", "Клиент заказа №%d взял все шины этого заказа: клиент, чаты (%d) и звонки (%d) перенесены сюда"
                         % (b["id"], len(chats), calls))
        orders.add_event(conn, b["id"], now, author, "field", "Клиент взял шины заказа №%d с ОТХ: клиент, чаты и звонки перенесены туда, заказ продолжается там"
                         % a_id)
        if not orders.move_card(conn, b["id"], cancel["id"], now, author, changes={"cancel_reason": CLIENT_TOOK % a_id}, fire_rules=False,
                                journal=False, commit=False):
            raise _error("заказ №%d только что изменили" % b["id"])
        moved = orders.move_card(conn, a_id, stage["id"], now, author)
    except orders.OrderError as err:
        conn.rollback()
        raise _error("Шины не взяты: %s. Обновите страницу." % err) from None
    if not moved:
        raise _error("Шины не взяты: заказ №%d только что изменили. Обновите страницу." % a_id)
    return a_id


def swaps(conn) -> Dict[int, dict]:
    """Order → its last swap of the client (the journal of actions)."""
    out: Dict[int, dict] = {}
    for (text,) in conn.execute("SELECT detail FROM audit WHERE action = ? ORDER BY id", (SWAP,)):
        try:
            detail = json.loads(text)
            out[int(detail["order"])] = detail
        except (TypeError, ValueError, KeyError):
            continue
    return out


def fee_handed(conn) -> Tuple[Set[int], Dict[int, int]]:
    """(the orders whose own fee of a handed client is off: the client changed, the orders that pay the fee of the order B whose client came
    to them: A → B, when B was handed by a point)."""
    found = swaps(conn)
    handed = {r[0] for r in conn.execute("SELECT order_id FROM order_handoff")}
    return set(found), {a: int(d["from_order"]) for a, d in found.items() if d.get("from_order") and int(d["from_order"]) in handed}


def handed_fee(conn, order_id) -> bool:
    """The order pays the fee of a handed client (order_report, order_sheet): its own handoff (or a merged source's), unless its client changed;
    the handoff of the order B whose client came to it."""
    from . import order_merge
    off, moved = fee_handed(conn)
    if order_id in moved:
        return True
    if order_id in off:
        return False
    return bool(conn.execute("SELECT 1 FROM order_handoff WHERE order_id = ?", (order_id,)).fetchone()) or order_id in order_merge.handed(conn)
