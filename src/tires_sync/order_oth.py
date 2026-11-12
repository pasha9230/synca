"""«На ОТХ»: the goods of an order the client refused are ours, kept in stock (the owner 2026-10-23).

Entering the stage unlinks the client: the link of the order (client_order) is put aside in order_oth_client, so the order is no longer the
client's (not in the card, not a purchase, never «заказал»), and the automatic joining never links it again (clients._detached). Leaving the
stage for an open one (the client came back) gives the link back; leaving it for a closed one («Выполнено», «Отмена», «Возврат», a refusal)
keeps the order unlinked. The positions of the orders standing in «На ОТХ» are our stock: the page «На ОТХ» of «Товар и фид» and a mark in
the catalog. «Вернуть поставщику» is a mark of ours (oth_return): nothing is written to WebMIM, the stage of the order does not change. The
feed is never touched: these goods are put on Avito by hand (the owner)."""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from . import orders


class OthError(ValueError):
    pass


def _audit(conn, now: str, author: str, action: str, detail: dict) -> None:
    """A record of the journal of actions saved with the caller's change (store.write_audit commits on its own)."""
    conn.execute("INSERT INTO audit (at, author, action, detail) VALUES (?,?,?,?)", (now, author, action, json.dumps(detail, ensure_ascii=False)))


def detach(conn, card: dict, now: str, author: str) -> Optional[int]:
    """The order has entered «На ОТХ»: its client (if any) is unlinked and kept aside; the marks «возвращено» and the agreed return of an
    earlier stay are dropped.
    Returns the client unlinked. No commit: saved with the move."""
    conn.execute("DELETE FROM oth_return WHERE order_id = ?", (card["id"],))
    conn.execute("DELETE FROM oth_return_plan WHERE order_id = ?", (card["id"],))     # a return agreed in an earlier stay is not this one's
    row = conn.execute("SELECT client_id, how, at FROM client_order WHERE order_id = ?", (card["id"],)).fetchone()
    if row is None:
        return None
    conn.execute("INSERT OR REPLACE INTO order_oth_client (order_id, client_id, how, linked_at, detached_at, detached_by) VALUES (?,?,?,?,?,?)",
                 (card["id"], row[0], row[1], row[2], now, author))
    conn.execute("DELETE FROM client_order WHERE order_id = ?", (card["id"],))
    orders.add_event(conn, card["id"], now, author, "field", "Клиент отвязан от заказа: отказ, товар на ОТХ (клиент №%d)" % row[0])
    _audit(conn, now, author, "oth_detach", {"order": int(card["id"]), "client": row[0]})
    return row[0]


def reattach(conn, card_id, now: str, author: str) -> Optional[int]:
    """The order has left «На ОТХ» for an open stage (the client came back): the client put aside is linked again — the one it was merged
    into, if it was. A client that is no more leaves the order to the usual joining by its phone. No commit."""
    row = conn.execute("SELECT client_id, how, linked_at FROM order_oth_client WHERE order_id = ?", (card_id,)).fetchone()
    if row is None:
        return None
    conn.execute("DELETE FROM order_oth_client WHERE order_id = ?", (card_id,))
    from . import clients
    from .inquiry import _final
    client_id = _final(conn, row[0])
    if client_id is None:
        orders.add_event(conn, card_id, now, author, "field", "Клиент №%d не найден: заказ привязывается по телефону" % row[0])
        return clients.attach_order(conn, card_id, now, author)
    conn.execute("INSERT OR IGNORE INTO client_order (order_id, client_id, how, at) VALUES (?, ?, ?, ?)", (card_id, client_id, row[1], row[2] or now))
    orders.add_event(conn, card_id, now, author, "field", "Клиент снова привязан к заказу: заказ ушёл с ОТХ (клиент №%d)" % client_id)
    _audit(conn, now, author, "oth_reattach", {"order": int(card_id), "client": client_id})
    return client_id


def leave(conn, card: dict, stage: dict, now: str, author: str) -> None:
    """A move of the order (orders.move_card): an unlinked order that goes into an open stage gives the client back — straight from «На ОТХ»
    or after a closed stage set by mistake (no other way to link it again); a closed stage keeps it unlinked. Back to work, an unsigned agreement of the
    return is over (review 2026-11-05: a later «На возврат» is a new one); a closed stage keeps it, its undo comes back."""
    if stage["code"] not in ("oth", orders.RETURN_GIVEN) and stage["kind"] == "open":      # «На возврат»: the goods go back, no client
        reattach(conn, card["id"], now, author)
        if conn.execute("DELETE FROM oth_return_plan WHERE order_id = ? AND signed_at IS NULL", (card["id"],)).rowcount:
            orders.add_event(conn, card["id"], now, author, "field", "Согласование возврата снято: заказ снова в работе")


_STOCK_SQL = ("SELECT i.id, i.order_id, i.goods_id, i.code, i.name, i.qty, c.point_id, COALESCE(p.name, ''), c.entered_at, c.reserve_until, "
              "r.at, r.author, COALESCE(r.qty, 0), s.code FROM order_item i JOIN order_card c ON c.id = i.order_id JOIN order_stage s ON s.id = c.stage_id "
              "LEFT JOIN point p ON p.id = c.point_id LEFT JOIN oth_return r ON r.item_id = i.id WHERE s.code IN ('oth', 'return_given') AND i.qty >= 1 "
              "AND NOT EXISTS (SELECT 1 FROM oth_return_plan rp WHERE rp.order_id = c.id AND rp.signed_at IS NOT NULL) ")
# «На возврат» (return_given, the owner 2026-11-03): the tyres lie at the point until the act МХ-3, ours; never on sale (oth_stock, goods_on_oth, the owner
# 2026-11-04: from «Согласуем» on); the act signed (a refund still to fill) — they are the supplier's. A mark «На возврат» of a position on «На ОТХ»
# is on sale until agreed (stock, the owner 2026-11-10)
_STOCK_KEYS = ("item_id", "order_id", "goods_id", "code", "name", "ordered", "point_id", "point", "since", "until", "returned_at", "returned_by",
               "returned_qty", "stage_code")
MAX_PIECES = 10000


def _with_left(row: dict) -> dict:
    """`ordered`: the pieces of the position in the order; `returned_qty`: marked «возвращено поставщику» so far (a return may be a part,
    the owner 2026-10-24); `left`: still ours, never below zero (the quantity of the position may be lowered after a return)."""
    row["left"] = max(int(row["ordered"]) - int(row["returned_qty"]), 0)
    return row


def stock(conn) -> Dict[str, List[dict]]:
    """The positions of the orders standing in «На ОТХ» or «На возврат», oldest first: {"stock": the pieces still ours, "returned": the pieces marked
    «возвращено поставщику»}. A position returned in part is in both; `qty` is the pieces of that list (left there, returned here).
    The owner 2026-11-10: the pieces of a mark «На возврат» on «На ОТХ» (`marked_qty`, «Ожидает согласование возврата») are on sale until the return
    is agreed: in the stock too (the feed, «Взять с ОТХ»); `left` stays what a real «Вернуть поставщику» may take.
    No price: the purchase price needs its own right, and the page is about where the goods are."""
    rows = [_with_left(dict(zip(_STOCK_KEYS, r))) for r in conn.execute(_STOCK_SQL + "ORDER BY c.entered_at, i.order_id, i.id")]
    since: Dict[int, str] = {}
    for r in rows:
        r["marked_qty"] = 0
        if r["stage_code"] == "oth" and r["returned_qty"] > 0:
            if r["order_id"] not in since:
                since[r["order_id"]] = _oth_since(conn, r["order_id"])
            r["marked_qty"] = _marked(conn, r["item_id"], int(r["returned_qty"]), since[r["order_id"]])
    return {"stock": [dict(r, qty=r["left"] + r["marked_qty"]) for r in rows if r["left"] + r["marked_qty"] > 0],
            "returned": [dict(r, qty=r["returned_qty"]) for r in rows if r["returned_qty"] > 0]}


def goods_on_oth(conn) -> Dict[str, int]:
    """goods_id → how many pieces of it are on «На ОТХ» and not returned: the mark of the catalog; with «Свой остаток (ОТХ)» (oth_manual)."""
    from .oth_stock import manual_rows
    found: Dict[str, int] = {}
    for row in [r for r in stock(conn)["stock"] if r["stage_code"] == "oth"] + manual_rows(conn):     # «На возврат»: not for sale (2026-11-04)
        if row["goods_id"]:
            found[row["goods_id"]] = found.get(row["goods_id"], 0) + int(row["qty"])
    return found


def _stock_item(conn, item_id) -> dict:
    row = conn.execute(_STOCK_SQL + "AND i.id = ?", (item_id,)).fetchone()
    if row is None:
        raise OthError("Этой позиции нет на ОТХ: заказ ушёл с этапа «На ОТХ» или позиция удалена.")
    return _with_left(dict(zip(_STOCK_KEYS, row)))


def _pieces(value) -> int:
    text = str(value if value is not None else "").strip()
    if not (text.isascii() and text.isdigit()) or len(text) > 5 or not 1 <= int(text) <= MAX_PIECES:
        raise OthError("Количество: целое число от 1.")
    return int(text)


def _add_returned(conn, item: dict, pieces: int, now: str, author: str) -> None:
    # the limit is in the statement itself: a mark made a moment ago in another tab never lets the total pass the pieces of the order
    if conn.execute("INSERT INTO oth_return (item_id, order_id, qty, at, author) SELECT i.id, i.order_id, ?, ?, ? FROM order_item i "
                    "WHERE i.id = ? AND i.qty >= ? ON CONFLICT(item_id) DO UPDATE SET qty = oth_return.qty + excluded.qty, at = excluded.at, "
                    "author = excluded.author WHERE oth_return.qty + excluded.qty <= (SELECT qty FROM order_item WHERE id = excluded.item_id)",
                    (pieces, now, author, item["item_id"], pieces)).rowcount != 1:
        conn.rollback()
        raise OthError("Столько уже не вернуть: позицию только что отметили в другой вкладке. Обновите страницу.")


def mark_returned(conn, item_id, now: str, author: str, qty=None) -> None:
    """«Вернуть поставщику»: `qty` pieces of the position (all that are left when None) are marked as gone back to the supplier, never more
    than the order had and never more than are not returned yet (the owner 2026-10-24). A mark of ours: nothing is sent to WebMIM."""
    item = _stock_item(conn, item_id)
    if item["left"] <= 0:
        raise OthError("Позиция уже вся возвращена поставщику.")
    pieces = item["left"] if qty is None else _pieces(qty)
    if pieces > item["left"]:
        raise OthError("Можно вернуть не больше %d шт.: в заказе %d шт., уже возвращено %d шт." % (item["left"], item["ordered"], item["returned_qty"]))
    _add_returned(conn, item, pieces, now, author)
    # a real return now: the seventh day leaves the order (_HELD_SQL: more pieces in oth_return than the mark); the mark «На возврат» stays,
    # its pieces are still at the point (review 2026-11-07)
    total = int(item["returned_qty"]) + pieces
    whole = not item["returned_qty"] and total == int(item["ordered"])
    orders.add_event(conn, item["order_id"], now, author, "field", "ОТХ: возвращено поставщику — %s, %d шт.%s" % (
        item["name"], pieces, "" if whole else " (всего возвращено %d из %d)" % (total, item["ordered"])))
    _audit(conn, now, author, "oth_return", {"order": item["order_id"], "item": item["item_id"], "qty": pieces, "returned": total})
    conn.commit()
    if not pieces_here(conn, orders.get_card(conn, item["order_id"])):              # all given back (a mark «На возврат» is still here): nothing to agree (2026-11-07)
        close_return_tasks(conn, item["order_id"], now, author, "шины возвращены поставщику")


def unmark_returned(conn, item_id, now: str, author: str, qty=None) -> None:
    """The mark «возвращено поставщику» was a mistake, for `qty` pieces (all the returned ones when None): they are our stock again."""
    item = _stock_item(conn, item_id)
    returned = int(item["returned_qty"])
    if not returned:
        raise OthError("Позиция не отмечена как возвращённая.")
    pieces = returned if qty is None else _pieces(qty)
    if pieces > returned:
        raise OthError("Отменить можно не больше %d шт.: столько отмечено возвращённым." % returned)
    if conn.execute("UPDATE oth_return SET qty = qty - ? WHERE item_id = ? AND qty >= ?", (pieces, item["item_id"], pieces)).rowcount != 1:
        conn.rollback()                                                                                        # taken off a moment ago
        raise OthError("Столько уже не отменить: отметку только что изменили в другой вкладке. Обновите страницу.")
    conn.execute("DELETE FROM oth_return WHERE item_id = ? AND qty <= 0", (item["item_id"],))
    rest = returned - pieces
    orders.add_event(conn, item["order_id"], now, author, "field", "ОТХ: отметка «возвращено поставщику» снята — %s, %d шт.%s" % (
        item["name"], pieces, " (возвращено %d из %d)" % (rest, item["ordered"]) if rest else ""))
    _audit(conn, now, author, "oth_return_undo", {"order": item["order_id"], "item": item["item_id"], "qty": pieces, "returned": rest})
    conn.commit()
    _item_undone(conn, item, rest, now, author)


# ---- the return to the supplier with the act МХ-3 (the owner 2026-10-29, the stage «На возврат» 2026-11-03) ---------------------------
# The seventh day of «На ОТХ» (ask_seventh_day), or a person by hand (moved_by_hand): the order goes to «На возврат» (code return_given), «Согласуем»;
# the tyres are off sale (oth_stock, the owner 2026-11-04) and the order's responsible has one task. They agree the return with the
# supplier in the order's card («Согласовано с поставщиком»; from «На ОТХ» too, the order then goes to «На возврат»): the operator enters the
# date of the return, a Monday, Wednesday or Friday (the owner 2026-11-04; the nearest one is offered); «Согласовано, отдать
# до ДД.ММ». The point sees «Возврат <дата>» and «Подпишите акт МХ-3» on «Рабочее место» (the act is paper: the
# panel makes nothing of it) and presses «Подписал акт и отдал»: the order goes to «Вернули поставщику» (closed) with the refund = the
# purchase of the tyres given back (purchase_sum). Nothing goes to WebMIM nor to the client.

ACT = "акт МХ-3"
SIGNED_LINE = "Точка «%s»: %s подписан, шины отданы поставщику (возврат с ОТХ)."
SIGNED_HEAD = "Точка отдала шины с ОТХ поставщику по акту МХ-3: "     # routes_alerts.is_return_ask (the old records too)
SIGNED_ASK = SIGNED_HEAD + "заполните сумму возврата и переведите заказ в «%s»."   # the current name of «return» (orders.stage_name)
RETURN_REASON = "возврат с ОТХ поставщику (%s)" % ACT
RETURNING = ("oth", orders.RETURN_GIVEN)        # the stages a return is agreed and signed from (the orders agreed before 2026-11-03 are on oth)
AGREE, AGREED, SIGNED = "agree", "agreed", "signed"
LABELS = {AGREE: "Ожидает согласование возврата", AGREED: "Согласовано, отдать до %s", SIGNED: "Отдано, нужна сумма возврата"}
POINT_LABELS = {AGREE: LABELS[AGREE], AGREED: "Согласовано на возврат, %s", SIGNED: LABELS[SIGNED]}     # «Рабочее место» (2026-11-04)
RETURN_DAYS = (0, 2, 4)                         # the supplier takes the tyres on Monday, Wednesday and Friday (the owner 2026-11-04)
FILTERS = {AGREE: LABELS[AGREE], AGREED: "Согласовано"}          # the filter «На возврат» of the board (SIGNED is «Согласовано» there)


def return_date(today) -> str:
    """The nearest day of the supplier from `today` (a date), `today` included, ISO: what the form of the agreement offers."""
    while today.weekday() not in RETURN_DAYS:
        today += timedelta(days=1)
    return today.isoformat()


def check_return_day(value, today) -> str:
    """The date the operator entered: a real date, not in the past, Monday, Wednesday or Friday. ISO; OthError says what is wrong."""
    try:
        day = date.fromisoformat(str(value or "").strip())
    except ValueError:
        raise OthError("Укажите дату возврата.") from None
    if day < today:
        raise OthError("Дата возврата уже прошла: укажите понедельник, среду или пятницу не раньше сегодняшнего дня.")
    if day.weekday() not in RETURN_DAYS:
        raise OthError("Возврат поставщику только в понедельник, среду или пятницу: ближайший день — %s." % _short(return_date(day)))
    return day.isoformat()


def _short(iso: str) -> str:
    return "%s.%s" % (iso[8:10], iso[5:7])


def return_plan(conn, order_id) -> Optional[dict]:
    row = conn.execute("SELECT agreed_at, agreed_by, return_date, signed_at, signed_by FROM oth_return_plan WHERE order_id = ?", (order_id,)).fetchone()
    return None if row is None else dict(zip(("agreed_at", "agreed_by", "return_date", "signed_at", "signed_by"), row))


def state_of(plan: Optional[dict]) -> str:
    """The label's state of an order on «На возврат»: AGREE (no agreement), AGREED (the act not signed), SIGNED (the act signed, the refund
    could not be counted: a position without the purchase)."""
    return AGREE if plan is None else (SIGNED if plan["signed_at"] else AGREED)


def label_of(plan: Optional[dict]) -> str:
    state = state_of(plan)
    return LABELS[state] % _short(plan["return_date"]) if state == AGREED else LABELS[state]


def point_label_of(plan: Optional[dict]) -> str:
    """The same for the point's «Рабочее место»."""
    state = state_of(plan)
    return POINT_LABELS[state] % _short(plan["return_date"]) if state == AGREED else POINT_LABELS[state]


def plans(conn) -> Dict[int, dict]:
    """order → its plan, every one (the board reads them at once)."""
    return {r[0]: dict(zip(("agreed_at", "agreed_by", "return_date", "signed_at", "signed_by"), r[1:])) for r in conn.execute(
        "SELECT order_id, agreed_at, agreed_by, return_date, signed_at, signed_by FROM oth_return_plan")}


def purchase_sum(card: dict) -> Optional[float]:
    """The refund of a return to the supplier (the owner 2026-11-03): the purchase of the tyres given back — each position's supplier price ×
    its pieces now (the pieces taken by «Взять с ОТХ» left the order), as the window suggests it (routes_orders._return_amount, the owner
    2026-10-27: no supplier's discount). Only the supplier's tyres (the owner 2026-11-07: «сумма закупки всегда от поставщика»): the point's own tyre
    is not counted. None when a supplier's position has no purchase yet or no piece is left: never a made-up sum."""
    items = [i for i in card["items"] if int(i["qty"]) >= 1 and i["goods_id"] != orders.POINT_GOODS]     # the point's own tyre: no purchase of the supplier (2026-11-07)
    if not items or any(not float(i["supplier_price"]) > 0 for i in items):
        return None
    return round(sum(float(i["supplier_price"]) * int(i["qty"]) for i in items), 2)


def return_changes(card: dict, reason: str) -> dict:
    """The fields of «Вернули поставщику» a return fills when empty: the type, the reason and the refund (purchase_sum); a manager's own kept."""
    changes = {k: v for k, v in (("return_kind", "supplier"), ("return_reason", reason)) if not card[k]}
    amount = purchase_sum(card)
    if not float(card["return_amount"] or 0) > 0 and amount:
        changes["return_amount"] = amount
    return changes


REFUND_LINE = "Сумма возврата: закупка возвращённых шин"     # the journal names no amount (the point reads it; the purchase is money)


def apply_refund(conn, card_id, changes: dict, now: str, author: str) -> dict:
    """The refund of return_changes is written apart, before the move (no commit): the order's journal says where it came from and names no
    amount (a point reads its order's journal: the purchase is the right «money», routes_orders._return_amount), the audit keeps the sum.
    Returns the other changes, for move_card."""
    rest = dict(changes)
    amount = rest.pop("return_amount", None)
    if amount:
        conn.execute("UPDATE order_card SET return_amount = ? WHERE id = ?", (amount, card_id))
        orders.add_event(conn, card_id, now, author, "field", REFUND_LINE)
        _audit(conn, now, author, "oth_return_amount", {"order": int(card_id), "amount": amount})
    return rest


def _returning_stage(conn) -> dict:
    orders._ensure_return_given_stage(conn)                                           # a pipeline nobody opened since the stage came
    return next(s for s in orders.list_stages(conn) if s["code"] == orders.RETURN_GIVEN)


def _oth_since(conn, order_id) -> str:
    """The moment the order last entered «На ОТХ» (its record orders.OTH_ENTERED): oth_return and the marks of an earlier stay are not this one's."""
    row = conn.execute("SELECT MAX(at) FROM order_event WHERE order_id = ? AND kind = 'field' AND text = ?", (order_id, orders.OTH_ENTERED)).fetchone()
    return str(row[0] or "") if row else ""


def _marked(conn, item_id, returned: int, since: str = "") -> int:
    """The pieces of oth_return of a position that are a mark of «На возврат» (item_to_return), not a real «Вернуть поставщику»: the newest mark
    of the position made since the order last entered «На ОТХ» (`since`; on «На возврат» the order has moved on since), never more than oth_return holds."""
    best, qty = "", 0
    for (value,) in conn.execute("SELECT value FROM app_state WHERE key LIKE ?", ("oth_item_return:%d:%%" % int(item_id),)):
        try:
            mark = json.loads(value)
        except ValueError:
            continue
        if str(mark.get("at") or "") < since:                                       # a mark of an earlier stay (detach dropped its oth_return)
            continue
        if str(mark.get("at") or "") >= best:
            best, qty = str(mark.get("at") or ""), int(mark.get("qty") or 0)
    return max(min(qty, returned), 0)


SOLD_EARLY = "Продано до согласования возврата"
ISSUED_NO_RETURN = "Заказ выдан, возврат не нужен"         # «Выдали» before the agreement: no sale, a neutral record (the owner 2026-11-10)


MARKS_KEY = "oth_dropped_marks:%d"     # app_state, per order: {slot: {item: {"qty", "by"}}} — the marks «На возврат» drop_marks took off, until «Отменить»
MOVE = "move"                          # (slot MOVE: the sale or «Выдали» that closed the order) or «Вернуть на ОТХ» (slot take_slot) brings them back


def take_slot(order_id, from_item) -> str:
    """The slot of the marks a «Взять с ОТХ» of the order `order_id` took from the position `from_item`."""
    return "take:%d:%d" % (int(order_id), int(from_item))


def _dropped(conn, order_id) -> dict:
    try:
        kept = json.loads(store_value(conn, MARKS_KEY % int(order_id)) or "{}")
    except ValueError:
        kept = {}
    return kept if isinstance(kept, dict) else {}


def _keep_dropped(conn, order_id, kept: dict) -> None:
    kept = {k: v for k, v in kept.items() if v}
    if kept:
        conn.execute("INSERT OR REPLACE INTO app_state (key, value) VALUES (?, ?)", (MARKS_KEY % int(order_id), json.dumps(kept, ensure_ascii=False)))
    else:
        conn.execute("DELETE FROM app_state WHERE key = ?", (MARKS_KEY % int(order_id),))


def forget_dropped(conn, order_id, slot: str = MOVE) -> None:
    """A new sale or «Выдали»: the marks an earlier one took off are not this one's to bring back. No commit."""
    kept = _dropped(conn, order_id)
    if kept.pop(slot, None) is not None:
        _keep_dropped(conn, order_id, kept)


def _mark_of(value) -> Optional[dict]:
    """A saved mark «На возврат», None when the value is broken: skipped, as _marked does (it has counted no pieces of it)."""
    try:
        mark = json.loads(value)
    except (TypeError, ValueError):
        return None
    return mark if isinstance(mark, dict) else None


def drop_marks(conn, order_id, now: str, who: str, item_id=None, pieces=None, why: str = SOLD_EARLY, slot: str = MOVE) -> int:
    """The tyres of a mark «На возврат» were sold (or given to the client, `why` ISSUED_NO_RETURN) before the return was agreed (the owner
    2026-11-10): the mark of this stay goes and its pieces leave oth_return (a real «Вернуть поставщику» stays) — of every position (`item_id`
    None) or `pieces` of one («Взять с ОТХ»: the mark keeps the rest). What went is kept under `slot` (MARKS_KEY) for restore_marks. One record
    in the journal. No commit. Returns the pieces released."""
    since = _oth_since(conn, order_id)
    names = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM order_item WHERE order_id = ?", (order_id,))}
    done = []
    for item, qty in conn.execute("SELECT item_id, qty FROM oth_return WHERE order_id = ?" + ("" if item_id is None else " AND item_id = %d" % int(item_id)),
                                  (order_id,)).fetchall():
        marked = _marked(conn, item, int(qty), since)
        off = marked if pieces is None else min(marked, int(pieces))
        if off <= 0:
            continue
        by = ""
        for (value,) in conn.execute("SELECT value FROM app_state WHERE key LIKE ?", ("oth_item_return:%d:%%" % int(item),)).fetchall():
            mark = _mark_of(value)
            if mark is not None and str(mark.get("at") or "") >= since:
                by = str(mark.get("by") or "")
        conn.execute("UPDATE oth_return SET qty = qty - ? WHERE item_id = ?", (off, item))
        if off < marked:                                                             # a part taken: the mark holds the rest
            for key, value in conn.execute("SELECT key, value FROM app_state WHERE key LIKE ?", ("oth_item_return:%d:%%" % int(item),)).fetchall():
                mark = _mark_of(value)
                if mark is not None and str(mark.get("at") or "") >= since:
                    conn.execute("UPDATE app_state SET value = ? WHERE key = ?", (json.dumps(dict(mark, qty=marked - off), ensure_ascii=False), key))
        else:
            conn.execute("DELETE FROM app_state WHERE key LIKE ?", ("oth_item_return:%d:%%" % int(item),))
        done.append((item, off, by))
    conn.execute("DELETE FROM oth_return WHERE order_id = ? AND qty <= 0", (order_id,))
    if done:
        kept = _dropped(conn, order_id)
        place = {} if slot == MOVE else kept.get(slot) or {}                         # MOVE: one sale's own; a take adds to its slot
        for item, off, by in done:
            was = place.get(str(item)) or {}
            place[str(item)] = {"qty": int(was.get("qty") or 0) + off, "by": by or was.get("by") or who}
        kept[slot] = place
        _keep_dropped(conn, order_id, kept)
        orders.add_event(conn, order_id, now, who, "field", "%s: метка «На возврат» снята — %s" % (why, "; ".join(
            "%s, %d шт." % (names.get(i, "позиция"), n) for i, n, _by in done)))
        _audit(conn, now, who, "oth_sold_early" if why == SOLD_EARLY else "oth_issued_early", {"order": int(order_id), "items": {str(i): n for i, n, _by in done}})
    return sum(n for _, n, _by in done)


def restore_marks(conn, order_id, now: str, who: str, slot: str, why: str, pieces=None) -> int:
    """«Отменить» of the sale or «Выдали» (slot MOVE), «Вернуть на ОТХ» of a take (take_slot, at most `pieces`): the marks «На возврат» drop_marks took
    off are back on the positions — in oth_return, «Ожидает согласование возврата», on sale until agreed — and the task of the return with them, as
    item_to_return sets it: the open one names them, else a new one (the order's responsible, the term of the setting from now). Only on «На ОТХ» /
    «На возврат» not agreed. No commit: the caller saves it with the rest of its undo. Returns the pieces marked again."""
    kept = _dropped(conn, order_id)
    place = kept.pop(slot, None) or {}
    card = orders.get_card(conn, order_id)
    if not place or card["stage"]["code"] not in RETURNING or return_plan(conn, order_id) is not None:
        _keep_dropped(conn, order_id, kept)
        return 0
    since = _oth_since(conn, order_id)
    items = {i["id"]: i for i in card["items"]}
    left = None if pieces is None else int(pieces)
    back, rest = [], {}
    for key, entry in place.items():
        item, wanted = int(key), int(entry.get("qty") or 0)
        held = int((conn.execute("SELECT qty FROM oth_return WHERE item_id = ?", (item,)).fetchone() or (0,))[0])
        n = min(wanted, int(items[item]["qty"]) - held if item in items else 0, wanted if left is None else left)
        if wanted - max(n, 0) > 0 and item in items:
            rest[key] = dict(entry, qty=wanted - max(n, 0))                         # another take's pieces, still in another order
        if n <= 0:
            continue
        if left is not None:
            left -= n
        conn.execute("INSERT INTO oth_return (item_id, order_id, qty, at, author) VALUES (?, ?, ?, ?, ?) ON CONFLICT(item_id) DO UPDATE SET "
                     "qty = oth_return.qty + excluded.qty, at = excluded.at, author = excluded.author", (item, order_id, n, now, who))
        back.append((item, n, str(entry.get("by") or who)))
    if rest:
        kept[slot] = rest
    _keep_dropped(conn, order_id, kept)
    if not back:
        return 0
    what = "; ".join("%s, %d шт." % (items[i]["name"], n) for i, n, _by in back)
    orders.add_event(conn, order_id, now, who, "field", "Метка «На возврат» возвращена (%s): %s. Метка «%s», шины в продаже до согласования возврата." % (
        why, what, LABELS[AGREE]))
    _audit(conn, now, who, "oth_marks_back", {"order": int(order_id), "items": {str(i): n for i, n, _by in back}})
    marks = []
    for item, n, by in back:
        key = _item_key(item, card["entered_at"])
        old = json.loads(store_value(conn, key) or "null") or {}
        same = str(old.get("at") or "") >= since and old.get("qty")                  # a part left by a take: it holds these pieces too
        mark = {"qty": int(old.get("qty") or 0) + n if same else n, "at": old["at"] if same else now, "by": old.get("by") if same else by, "task": 0}
        if same and old.get("waiting"):
            mark["waiting"] = old["waiting"]
        marks.append((key, mark))
    found = conn.execute("SELECT id FROM callback_task WHERE order_id = ? AND title IN (?, ?) AND done_at IS NULL AND cancelled_at IS NULL ORDER BY id LIMIT 1",
                         (order_id,) + RETURN_TITLES).fetchone()
    if found:                                                                        # one open task per order: it names these too
        from . import tasks
        conn.execute("UPDATE callback_task SET note = substr(note || ', ' || ?, 1, ?) WHERE id = ?", ("позиция: " + what, tasks.NOTE_MAX, found[0]))
        task = found[0]
    else:
        task = _return_task(conn, card, now, who, "позиция: " + what, commit=False)
    for key, mark in marks:
        conn.execute("INSERT OR REPLACE INTO app_state (key, value) VALUES (?, ?)", (key, json.dumps(dict(mark, task=task), ensure_ascii=False)))
    return sum(n for _i, n, _by in back)


def pieces_here(conn, card: dict) -> Dict[int, int]:
    """Position → the pieces still at the point: the order's minus those really given back to the supplier («Вернуть поставщику»); the pieces
    marked «На возврат» are here (review 2026-11-07)."""
    returned = {r[0]: int(r[1]) for r in conn.execute("SELECT item_id, qty FROM oth_return WHERE order_id = ?", (card["id"],))}
    since = _oth_since(conn, card["id"])
    out = {}
    for i in card["items"]:
        real = returned.get(i["id"], 0) - _marked(conn, i["id"], returned.get(i["id"], 0), since)
        if int(i["qty"]) - real >= 1:
            out[i["id"]] = int(i["qty"]) - real
    return out


def return_parts(conn, card: dict) -> Dict[int, int]:
    """What «Согласовано» sends back by default (the owner 2026-11-07, «по позиции»): on «На ОТХ» with positions moved to the return by hand
    (item_to_return, their task open) those pieces only; else every piece still at the point (pieces_here)."""
    held = pieces_here(conn, card)
    if card["stage"]["code"] == "oth":
        marks = item_marks(conn)
        marked = {i: min(int((item_mark(marks, i, card["entered_at"]) or {}).get("qty") or 0), n) for i, n in held.items()}
        if any(marked.values()):
            return {i: n for i, n in marked.items() if n}
    return held


def _checked_parts(conn, card: dict, qty: Dict) -> Dict[int, int]:
    held = pieces_here(conn, card)
    parts = {}
    for key, value in qty.items():
        item = int(key)
        if str(value).strip().lstrip("0") == "":                                     # 0 (or empty): this position does not go back
            continue
        if item not in held:
            raise OthError("Такой позиции на точке нет: обновите страницу.")
        pieces = _pieces(value)
        if pieces > held[item]:
            raise OthError("На возврат не больше %d шт. позиции: столько на точке." % held[item])
        if pieces:
            parts[item] = pieces
    if not parts:
        raise OthError("Укажите, сколько шин примет поставщик: хотя бы одну.")
    return parts


def split_of(conn, order_id) -> List[int]:
    """The orders split off this one by «Согласовано» (_split_off), oldest first."""
    found = []
    for (text,) in conn.execute("SELECT detail FROM audit WHERE action = 'oth_split' ORDER BY id"):
        try:
            detail = json.loads(text)
        except ValueError:
            continue
        if int(detail.get("order") or 0) == int(order_id):
            found.append(int(detail["new"]))
    return found


def _split_off(conn, card: dict, parts: Dict[int, int], stage: dict, now: str, who: str, why: str) -> int:
    """A new order of the same point on `stage` takes `parts` (position → pieces) of `card` at their purchase, as «Взять с ОТХ» does (order_item_oth:
    the chain, the numbers of the purchase, the table follows); the pieces marked off sale for the return (oth_return, item_to_return) go with them.
    No client, no message, no rule. No commit. Returns the new order."""
    from . import oth_sale, order_sheet
    label = card.get("webmim_number") or str(card["id"])
    new = orders.create_card(conn, {"point_id": card["point_id"], "responsible": card.get("responsible") or "", "deal_kind": card.get("deal_kind") or "webmim",
                                    "comment": "Выделен из заказа №%s: %s" % (label, why)}, now, who, require_contact=False, commit=False, stage=stage)
    items = {i["id"]: i for i in card["items"]}
    names = []
    for item_id, pieces in parts.items():
        item = items[item_id]
        if conn.execute("UPDATE order_item SET qty = qty - ? WHERE id = ? AND order_id = ? AND qty >= ?", (pieces, item_id, card["id"], pieces)).rowcount != 1:
            conn.rollback()
            raise OthError("Позицию только что изменили: обновите страницу.")
        cur = conn.execute(
            "INSERT INTO order_item (order_id, goods_id, code, name, brand_key, season, diameter, territory, qty, supplier_price, sale_unit, terms, "
            "fee_pct, promo_pct, purchase_manual) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
            (new, item["goods_id"], item["code"], item["name"], item["brand_key"], item["season"], item["diameter"], item["territory"], pieces,
             float(item["supplier_price"]), float(item["sale_unit"]), json.dumps(item["terms"] or {}, ensure_ascii=False), float(item["fee_pct"]),
             float(item["promo_pct"])))
        conn.execute("INSERT INTO order_item_oth (item_id, order_id, from_item, from_order, qty, at, author) VALUES (?,?,?,?,?,?,?)",
                     (cur.lastrowid, new, item_id, card["id"], pieces, now, who))
        off = conn.execute("SELECT qty FROM oth_return WHERE item_id = ?", (item_id,)).fetchone()
        marked = _marked(conn, item_id, int(off[0]), _oth_since(conn, card["id"])) if off else 0
        if marked:                                                                    # the mark «На возврат» is decided now: its pieces go with the
            conn.execute("UPDATE oth_return SET qty = qty - ? WHERE item_id = ?", (marked, item_id))     # split or are on sale again; a real
            conn.execute("DELETE FROM oth_return WHERE item_id = ? AND qty <= 0", (item_id,))           # «Вернуть поставщику» stays as it is
        conn.execute("DELETE FROM app_state WHERE key LIKE ?", ("oth_item_return:%d:%%" % int(item_id),))
        names.append("%s × %d" % (item["name"], pieces))
    oth_sale._sync_numbers(conn, new, [], now, who)
    orders.add_event(conn, card["id"], now, who, "field", "%s: выделено в заказ №%d на «%s» — %s" % (why[:1].upper() + why[1:], new, stage["name"], "; ".join(names)))
    orders.add_event(conn, new, now, who, "field", "Выделен из заказа №%s (%s): %s" % (label, why, "; ".join(names)))
    _audit(conn, now, who, "oth_split", {"order": int(card["id"]), "new": new, "items": {str(k): v for k, v in parts.items()}, "stage": stage["code"]})
    order_sheet.enroll(conn, new, now)
    return new


def agree(conn, card: dict, today, now: str, author: str, day=None, qty: Optional[Dict] = None, seen: Optional[Dict] = None) -> str:
    """«Согласовано с поставщиком» for an order on «На возврат» (or still on «На ОТХ»: it goes to «На возврат» with it, in one transaction):
    `day`, the date of the return the operator entered (check_return_day; None: the nearest one). Once: a second press changes nothing and
    says the date. Returns the date (ISO).
    The owner 2026-11-07: what goes back is `qty` (position → pieces the supplier takes; None: return_parts). Not all of the order: on «На ОТХ» the
    pieces going back are split off into an order of their own on «На возврат», agreed, and the rest stays on sale; on «На возврат» the rest is split
    off into an order on «На ОТХ» (our stock again) and the journal says fewer tyres go back than were bought."""
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")                              # the checks and the split under the write lock: a second press waits, then sees it
    try:
        card = orders.get_card(conn, card["id"])                 # the stage as it is now (review 2026-11-03: a stale page)
        if card["stage"]["code"] not in RETURNING:
            raise OthError("Согласовать возврат можно только для заказа на этапе «%s» или «%s»." % (
                orders.stage_name(conn, "oth"), orders.stage_name(conn, orders.RETURN_GIVEN)))
        if not card["point_id"]:
            raise OthError("У заказа не указана точка: некому подписать акт МХ-3. Укажите точку в заказе.")
        agreed = return_plan(conn, card["id"])
        if agreed is not None:
            conn.rollback()
            return agreed["return_date"]
        day = return_date(today) if day is None else check_return_day(day, today)
        held = pieces_here(conn, card)
        if seen is not None and {int(k): int(v) for k, v in seen.items()} != held:
            raise OthError("Заказ только что изменили (вторая вкладка или второе нажатие): обновите страницу, ничего не согласовано.")
        default = return_parts(conn, card)
        parts = default if qty is None else _checked_parts(conn, card, qty)
    except Exception:
        conn.rollback()
        raise
    if parts != held:
        return _agree_part(conn, card, held, parts, day, now, author, sum(default.values()))
    return _agree_whole(conn, card, day, now, author)


def _agree_part(conn, card: dict, held: Dict[int, int], parts: Dict[int, int], day: str, now: str, author: str, offered: int) -> str:
    """agree() of a part of the order: the split and the agreement in one transaction. `offered`: the pieces return_parts sent back."""
    sent = sum(parts.values())
    note = "на возврат едет меньше шин, чем в закупке: %d из %d" % (sent, offered) if sent < offered else ""
    try:
        if card["stage"]["code"] == "oth":                                            # «Согласовано» по позиции: the returning part leaves
            new = _split_off(conn, card, parts, _returning_stage(conn), now, author, note or "согласован возврат части заказа")
            back = _agree_whole(conn, orders.get_card(conn, new), day, now, author, commit=False)
            target = new
        else:                                                                         # the rest is our stock again
            rest = {i: held[i] - parts.get(i, 0) for i in held if held[i] - parts.get(i, 0) > 0}
            oth = next(s for s in orders.list_stages(conn) if s["code"] == "oth")
            new = _split_off(conn, card, rest, oth, now, author, "поставщик примет не все шины, остаток на ОТХ")
            conn.execute("INSERT OR IGNORE INTO app_state (key, value) VALUES (?, ?)", (NO_RETURN_KEY % (new, re.sub(r"\D", "", now)), json.dumps(
                {"at": now, "by": author, "why": "поставщик не принял"})))      # the supplier refused these: the 7th day never sends them back again
            if note:
                orders.add_event(conn, card["id"], now, author, "field", "На возврат: %s; остаток выделен в заказ №%d на «%s»." % (note, new, oth["name"]))
            back = _agree_whole(conn, orders.get_card(conn, card["id"]), day, now, author, commit=False)
            target = card["id"]
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    left = orders.get_card(conn, card["id"])
    if not (left["stage"]["code"] == "oth" and any(item_mark(item_marks(conn), i["id"], left["entered_at"]) for i in left["items"])):
        close_return_tasks(conn, card["id"], now, author, "согласовано с поставщиком")     # another position still waits: its task stays
    if target != card["id"]:
        close_return_tasks(conn, target, now, author, "согласовано с поставщиком")
    return back


def _agree_whole(conn, card: dict, day: str, now: str, author: str, commit: bool = True) -> str:
    stage = _returning_stage(conn) if card["stage"]["code"] == "oth" else None
    if conn.execute("INSERT OR IGNORE INTO oth_return_plan (order_id, agreed_at, agreed_by, return_date) VALUES (?,?,?,?)",
                    (card["id"], now, author, day)).rowcount != 1:
        conn.rollback()
        return return_plan(conn, card["id"])["return_date"]
    orders.add_event(conn, card["id"], now, author, "field", "Возврат с ОТХ согласован с поставщиком: передать на возврат %s (%s)." % (_short(day), ACT))
    _audit(conn, now, author, "oth_return_agreed", {"order": int(card["id"]), "date": day})
    conn.execute("INSERT OR REPLACE INTO app_state (key, value) VALUES (?, ?)", (AGREED_KEY % card["id"], day))     # a VK point hears it (tell_agreed)
    if not commit:                                                                    # a part (_agree_part): an order already on «На возврат», saved by the caller
        return day
    if stage is None:
        conn.commit()
    else:
        try:
            moved = orders.move_card(conn, card["id"], stage["id"], now, author)           # commits the agreement with the move
        except orders.OrderError:
            conn.rollback()
            raise
        if not moved:                                                                 # moved meanwhile: rolled back, nothing agreed
            raise OthError("Заказ только что перенесли: обновите страницу.")
    close_return_tasks(conn, card["id"], now, author, "согласовано с поставщиком")
    return day


def unagree(conn, card: dict, now: str, author: str) -> bool:
    """«Снять согласование» (the owner and the partner, before the act МХ-3 is signed): the agreement goes, the order stays on «На возврат»
    with «Согласуем», the tyres stay off sale (the owner 2026-11-04), the point no longer sees the date. One statement: a signed plan, or one cancelled a moment ago, is left as it is. False: nothing to cancel (or the order left)."""
    if card["stage"]["code"] not in RETURNING:
        return False
    if conn.execute("DELETE FROM oth_return_plan WHERE order_id = ? AND signed_at IS NULL", (card["id"],)).rowcount != 1:
        conn.rollback()
        return False
    orders.add_event(conn, card["id"], now, author, "field", "Согласование возврата с поставщиком отменено: «%s», шины не в продаже." % LABELS[AGREE])
    _audit(conn, now, author, "oth_return_unagreed", {"order": int(card["id"])})
    conn.commit()
    if card["stage"]["code"] == orders.RETURN_GIVEN:                                  # «Согласуем» again: agree closed the task, somebody has to agree it again
        _ensure_task(conn, card["id"], now, author)
    return True


def act_orders(conn, order_id) -> str:
    """The orders the act МХ-3 of a return is likely made by (the owner 2026-10-30: the supplier's driver brings it for the order the tyres
    were bought by): those of the positions of the order taken from «На ОТХ», the first order of a chain, by the number the point knows
    (WebMIM's, else the panel's) — the same as the mark «Шины с ОТХ, заказ №…» of its card. '' for an ordinary order on OTH."""
    from .order_templates import _oth_orders
    return _oth_orders(conn, order_id)


def to_return(conn, point_id) -> List[dict]:
    """The orders of the point agreed for the return and not yet given: (card, plan), the earliest date first."""
    rows = conn.execute("SELECT c.id FROM order_card c JOIN order_stage s ON s.id = c.stage_id JOIN oth_return_plan r ON r.order_id = c.id "
                        "WHERE c.point_id = ? AND s.code IN (?, ?) AND r.signed_at IS NULL ORDER BY r.return_date, c.id", (point_id,) + RETURNING).fetchall()
    return [{"card": orders.get_card(conn, r[0]), "plan": return_plan(conn, r[0])} for r in rows]


def sign_act(conn, card: dict, point_name: str, now: str, author: str) -> str:
    """«Подписал акт и отдал»: the plan is marked signed (one statement: a second press, another tab, signs nothing more) and the order goes
    to «Вернули поставщику» with the refund filled (return_changes), in one transaction. A refund that cannot be counted (a position without
    the purchase) keeps the order on «На возврат» («Отдано, нужна сумма возврата») with a yellow record for the responsible.
    Returns "return" (closed), "signed" (waits for the amount), "" (nothing to sign: no agreement, signed already, the order left)."""
    card = orders.get_card(conn, card["id"])                     # the stage and the refund as they are now (review 2026-11-03)
    if card["stage"]["code"] not in RETURNING:
        return ""
    returning = _returning_stage(conn) if card["stage"]["code"] == "oth" else None       # read first: making it commits
    if conn.execute("UPDATE oth_return_plan SET signed_at = ?, signed_by = ? WHERE order_id = ? AND signed_at IS NULL",
                    (now, author, card["id"])).rowcount != 1:
        conn.rollback()
        return ""
    orders.add_event(conn, card["id"], now, author, "point", SIGNED_LINE % (point_name, ACT))     # saved with the move (move_card commits it all)
    _audit(conn, now, author, "oth_act_signed", {"order": int(card["id"])})
    closed = next((s for s in orders.list_stages(conn) if s["code"] == "return"), None)
    changes = return_changes(card, RETURN_REASON)
    whole = closed is not None and not orders.missing_for(dict(card, **changes), closed)
    if not whole:
        orders.add_event(conn, card["id"], now, author, "point", SIGNED_ASK % orders.stage_name(conn, "return"), level="yellow")
        if card["stage"]["code"] == orders.RETURN_GIVEN:
            conn.commit()
            return "signed"
    target = closed if whole else returning
    try:
        moved = orders.move_card(conn, card["id"], target["id"], now, author,
                                 changes=apply_refund(conn, card["id"], changes, now, author) if whole else None)
    except orders.OrderError:
        conn.rollback()
        raise
    if not moved:                                                                     # moved meanwhile: move_card rolled it all back
        conn.rollback()
        return ""
    return "return" if whole else "signed"


# ---- the owner 2026-11-02: «На ОТХ» is held six days. The owner 2026-11-04: on the seventh from 12:30 the order goes to «На возврат» by itself and
# the point is asked at the same time «Заказ выдали или он на точке?» on its «Рабочее место» («мало ли они продали его»), once per stay (app_state ASK_KEY of
# the stay on «На возврат», moved by one UPDATE only). «Выдали»: the client came after all, the order is linked to its client again and closed as
# issued, the agreement (if any) and the task go. «На точке» only marks the answer; silence changes nothing: the return goes on. The order's
# responsible has one task, RETURN_TASK; «Согласовано с поставщиком» (agree) starts the act МХ-3.
HOLD_DAYS = 6
ALERT_FROM = 9                                   # order_reserve.FIRST_HOUR: no alert before 09:00
PENDING_KEY = "oth_moved_pending:%d"             # app_state, saved with the move to «На возврат»: {"since", "entered", "auto", "who", "task"}, what follows it (finish_moved)
TOLD_KEY = "oth_point_told:%d:%s"                # per order and stay on «На возврат»: a VK point has its status
AGREED_KEY = "oth_agreed_tell:%d"                # a VK point is to hear this date (tell_agreed)
ASK_AT = "12:30"
ASK_KEY = "oth_ask:%d:%s"                        # per order and stay on «На ОТХ»: asked|<at> → issued|<at> / here|<at>
RETURN_TASK = "Написать поставщику и согласовать возврат, узнать дату забора груза"     # kind «Вернуть», the responsible's (the owner 2026-11-04)
DROPPED_KEY = "oth_dropped_plan:%d"              # app_state: the agreement «Выдали» dropped, until «Отменить» brings it back
RETURN_TITLES = (RETURN_TASK, "Согласовать возврат")     # with the task of before 2026-11-04 still open
SINCE_KEY = "oth_ask_since"                      # app_state: the first tick of this rule; an order that entered «На ОТХ» earlier is never asked


def _ask_key(card: dict) -> str:
    return ASK_KEY % (card["id"], re.sub(r"\D", "", card["entered_at"] or ""))


def ask_due(card: dict, now: str) -> bool:
    """The seventh day on «На ОТХ» (the day it entered is the first) from 12:30, or any later moment."""
    entered = str(card.get("entered_at") or "")[:10]
    try:
        day = date.fromisoformat(entered) + timedelta(days=HOLD_DAYS)
    except ValueError:
        return False
    return now >= "%sT%s" % (day.isoformat(), ASK_AT)


_HELD_SQL = ("SELECT c.id, c.entered_at FROM order_card c JOIN order_stage s ON s.id = c.stage_id WHERE s.code = 'oth' "
             "AND c.entered_at >= ? AND NOT EXISTS (SELECT 1 FROM oth_return_plan r WHERE r.order_id = c.id) "
             # a real «Вернуть поставщику» keeps the order; the pieces of a mark «На возврат» of this stay (ITEM_KEY, its qty) do not (the owner
             # 2026-11-05): kept while oth_return holds more than the mark
             "AND NOT EXISTS (SELECT 1 FROM oth_return r WHERE r.order_id = c.id AND r.qty > COALESCE((SELECT CAST(json_extract(a.value, '$.qty') AS INTEGER) "
             "FROM app_state a WHERE a.key = 'oth_item_return:' || r.item_id || ':' || replace(replace(replace(c.entered_at, '-', ''), ':', ''), 'T', '')), 0)) "
             "AND NOT EXISTS (SELECT 1 FROM order_intercity i WHERE i.order_id = c.id AND i.enabled = 1) ")


def ask_seventh_day(conn, now: str, notify=None) -> int:
    """The tick, at any hour: the orders on «На ОТХ» whose seventh day has come go to «На возврат» and their point is asked (the owner 2026-11-04), the question
    and PENDING_KEY saved with the move; what follows the move (finish_moved) is repaired by the next tick if it fails. Only those that entered «На ОТХ» since
    the first tick of this rule (the owner 2026-11-02: no mass move of the old ones; SINCE_KEY, kept by the first tick). Not one whose return is agreed already
    (the act МХ-3 is on its way), nor one with tyres marked «возвращено поставщику» (the lead 2026-11-02), nor a point's own sale, nor an order to another
    city (order_intercity, the owner 2026-11-02), nor one without a point. Returns how many moved."""
    from . import order_points as points
    conn.execute("INSERT OR IGNORE INTO app_state (key, value) VALUES (?, ?)", (SINCE_KEY, now))
    conn.commit()
    since = store_value(conn, SINCE_KEY)
    moved = 0
    for card_id, entered in conn.execute(_HELD_SQL + "AND c.point_id IS NOT NULL ORDER BY c.id", (since,)).fetchall():
        try:
            card = orders.get_card(conn, card_id)                                    # as it is now: an order that left since the list is left alone
            if card["stage"]["code"] != "oth" or card["entered_at"] != entered or not ask_due(card, now) or card_id in points.own_sales(conn, card["point_id"]):
                continue
            if _kept(conn, card):                                                    # «не возвращаем» (the owner 2026-11-07)
                continue
            moved += _move_on_day7(conn, card, now)
        except Exception:                                                            # noqa: BLE001 - one order must not stop the others
            conn.rollback()
            logging.getLogger("tires_sync.orders").exception("the seventh day of order %s failed", card_id)
    finish_moved(conn, now, notify)
    return moved


def _move_on_day7(conn, card: dict, now: str) -> int:
    card_id = card["id"]
    stage = _returning_stage(conn)                                                   # read first: making it commits
    point = card.get("point") or {}
    conn.execute("INSERT OR IGNORE INTO app_state (key, value) VALUES (?, ?)", (ASK_KEY % (card_id, re.sub(r"\D", "", now)), "asked|" + now))
    conn.execute("INSERT OR REPLACE INTO app_state (key, value) VALUES (?, ?)", (PENDING_KEY % card_id, json.dumps(
        {"since": card["entered_at"], "entered": now, "auto": True, "who": "система"}, ensure_ascii=False)))
    orders.add_event(conn, card_id, now, "система", "point", "Точке «%s» задан вопрос: «%s»" % (point.get("name") or "", question(conn, card, now)), level="grey")
    try:
        if not orders.move_card(conn, card_id, stage["id"], now, "система", journal=False):
            conn.rollback()
            return 0
    except orders.OrderError as err:                                                 # rolled back: tried again at the next tick; told once
        _stuck(conn, card, now, str(err))
        return 0
    return 1


def in_alert_hours(conn, now: str) -> bool:
    """Alerts to people wait for these hours (the lead 2026-11-05): from ALERT_FROM to the end of the working hours; the moves do not."""
    from . import order_manager
    return ALERT_FROM <= datetime.fromisoformat(now).hour < order_manager.duty_settings(conn)["hours"][1]


def finish_moved(conn, now: str, notify) -> None:
    """Every tick: what follows a move to «На возврат» (PENDING_KEY): the task at any hour; in the alert hours the responsible's alert (seventh day only) and a
    VK point's status with the question. The key goes last: a crash or a failure is repaired by the next tick, each alert once. An order that left: nothing."""
    for key, value in conn.execute("SELECT key, value FROM app_state WHERE key LIKE 'oth_moved_pending:%'").fetchall():
        try:
            _finish_one(conn, key, value, now, notify)
        except Exception:                                                            # noqa: BLE001 - one order must not stop the others
            conn.rollback()
            logging.getLogger("tires_sync.orders").exception("what follows the move of %s failed", key)


def _finish_one(conn, key: str, value: str, now: str, notify) -> None:
    from . import message_texts, order_manager, order_points as points
    held = json.loads(value)
    card_id = int(key.rsplit(":", 1)[1])
    try:
        card = orders.get_card(conn, card_id)
    except orders.OrderError:
        card = None
    if (card is None or card["stage"]["code"] != orders.RETURN_GIVEN or card["entered_at"] != held["entered"]
            or return_plan(conn, card_id) is not None):                              # agreed while the alerts waited: tell_agreed says what is true now
        conn.execute("DELETE FROM app_state WHERE key = ?", (key,))
        conn.commit()
        return
    made = held.get("task") or _ensure_task(conn, card_id, now, held.get("who") or "система")
    if made and not held.get("task"):                                                # once: a person may close it before the alerts go
        conn.execute("UPDATE app_state SET value = ? WHERE key = ?", (json.dumps(dict(held, task=True), ensure_ascii=False), key))
        conn.commit()
    if notify is None or not in_alert_hours(conn, now):
        return
    if held.get("auto"):
        _tell_agree(conn, dict(card, entered_at=held["since"]), now, notify)
    point = card.get("point") or {}
    if point and point.get("channel") != points.PANEL and not (conn.execute("SELECT 1 FROM app_state WHERE key = ?", (TOLD_KEY % (card_id, re.sub(r"\D", "", held["entered"])),)).fetchone()):
        label = order_manager.label(card)
        text = message_texts.text(conn, message_texts.RETURN_STATUS, order_label=label)
        if question_open(conn, card):                                                # a record of storage: never «выдал» (the owner 2026-11-10)
            text += " " + (RECORD_ASK % (label, label) if card.get("oth_record") else message_texts.text(conn, message_texts.RETURN_ASK, order_label=label))
        conn.execute("INSERT INTO app_state (key, value) VALUES (?, ?)", (TOLD_KEY % (card_id, re.sub(r"\D", "", held["entered"])), now))
        conn.commit()
        notify(dict(card, responsible="", to_staff=False), text)                     # "": the alert goes to the point (order_notify.route, as tell_point)
    if made:
        conn.execute("DELETE FROM app_state WHERE key = ?", (key,))
        conn.commit()


def tell_agreed(conn, now: str, notify) -> None:
    """In the alert hours: a VK point hears the agreed date (AGREED_KEY, kept by agree); nothing when the agreement is gone or the order left."""
    from . import message_texts, order_manager, order_points as points
    for key, day in conn.execute("SELECT key, value FROM app_state WHERE key LIKE 'oth_agreed_tell:%'").fetchall():
        try:
            conn.execute("DELETE FROM app_state WHERE key = ?", (key,))
            conn.commit()
            card = orders.get_card(conn, int(key.rsplit(":", 1)[1]))
            plan = return_plan(conn, card["id"])
            point = card.get("point") or {}
            if plan and not plan["signed_at"] and plan["return_date"] == day and card["stage"]["code"] in RETURNING and point and point.get("channel") != points.PANEL:
                notify(dict(card, responsible="", to_staff=False), message_texts.text(
                    conn, message_texts.RETURN_AGREED, order_label=order_manager.label(card), date=_short(day)))
        except Exception:                                                            # noqa: BLE001 - one order must not stop the others
            conn.rollback()
            logging.getLogger("tires_sync.orders").exception("the agreed date of %s was not told", key)


def _stuck(conn, card: dict, now: str, why: str) -> None:
    """The seventh day could not move the order (a recorded sale): one yellow record per stay."""
    key = "oth_move_stuck:%d:%s" % (card["id"], re.sub(r"\D", "", card["entered_at"] or ""))
    if conn.execute("INSERT OR IGNORE INTO app_state (key, value) VALUES (?, ?)", (key, now)).rowcount == 1:
        orders.add_event(conn, card["id"], now, "система", "reserve", "7-й день на ОТХ: заказ не переведён на возврат: %s" % why, level="yellow")
    conn.commit()


RECORD_QUESTION = "Шины на точке?"     # a record of storage (oth_storage): no client to give them to, they are sold by «Продать» (the owner 2026-11-10)
RECORD_ASK = "Шины заказа №%s на точке? Ответьте «на точке %s». Если их купили, нажмите «Продать» в кабинете точки."
RECORD_ISSUED = ("Заказ №%s: шины на ОТХ без клиента, «Выдали» для них не отмечается (выдают с «На точке»). Если шины купили, нажмите «Продать» "
                 "в кабинете точки.")


def question(conn, card: dict, now: str) -> str:
    """The words of the question (the owner's template «Точке: ОТХ 7-й день» if there is one, else the sample); a record of storage is
    asked only whether its tyres are there (RECORD_QUESTION)."""
    if card.get("oth_record"):
        return RECORD_QUESTION
    from .order_templates import OTH_ASK_SAMPLE, render
    row = conn.execute("SELECT body FROM order_template WHERE name = ? AND audience = ? ORDER BY id LIMIT 1", OTH_ASK_SAMPLE[:2]).fetchone()
    return render(conn, card, row[0] if row else OTH_ASK_SAMPLE[2], now)[0].strip()


def question_open(conn, card: dict) -> bool:
    """«Рабочее место» shows the question: asked in this stay and not answered, the act МХ-3 not signed."""
    if card["stage"]["code"] not in RETURNING or not (store_value(conn, _ask_key(card)) or "").startswith("asked|"):
        return False
    return not (return_plan(conn, card["id"]) or {}).get("signed_at")


def to_agree(conn, card: dict) -> bool:
    """«На возврат» and the return not agreed yet («Согласуем»): the point's «На возврат», waiting for the office."""
    return card["stage"]["code"] == orders.RETURN_GIVEN and return_plan(conn, card["id"]) is None


def agreeing(conn, card: dict) -> bool:
    """The order is on the agreement of its return (the owner 2026-11-12, «Согласование возврата» of «Рабочее место»): on «На возврат» and not
    agreed yet, or on «На ОТХ» with a position marked for the return (item_to_return) — the whole order then: a record is sold or returned
    whole (the owner 2026-11-11). Agreed (a plan with its date): no longer."""
    code = card["stage"]["code"]
    if code not in RETURNING or return_plan(conn, card["id"]) is not None:
        return False
    if code == orders.RETURN_GIVEN:
        return True
    since = _oth_since(conn, card["id"])
    return any(_marked(conn, item_id, int(qty), since) > 0
               for item_id, qty in conn.execute("SELECT item_id, qty FROM oth_return WHERE order_id = ? AND qty > 0", (card["id"],)).fetchall())


def store_value(conn, key: str) -> str:
    row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else ""


def _answer(conn, card: dict, to: str, now: str) -> str:
    """The question moves from «asked» to `to` (no commit): the state read, '' when it was not open (answered, or never asked)."""
    key = _ask_key(card)
    was = store_value(conn, key)
    if not was.startswith("asked|") or conn.execute("UPDATE app_state SET value = ? WHERE key = ? AND value = ?",
                                                    (to + "|" + now, key, was)).rowcount != 1:
        return ""
    return was


def office_logins(conn) -> list:
    """The owner (the login of the alerts) and every active partner (Заман's role; never a login written here)."""
    from . import order_notify, users
    owner = order_notify.get_owner_login(conn)
    return [owner] + sorted(u["login"] for u in users.list_users(conn) if u["active"] and u["role"] == "partner" and u["login"] != owner)


def answer_issued(conn, card: dict, who: str, now: str) -> tuple:
    """«Выдали», from «На ОТХ» or «На возврат» (the owner 2026-11-04): the client is linked again, the agreement of the return (if any) goes and the
    order goes to «Выполнено», with the answer, in one transaction; the task of the return closes. Not agreed yet (the owner 2026-11-10): the mark
    «На возврат» goes and the task is done as not needed («Заказ выдан, возврат не нужен»), never «Продано до согласования». After the act МХ-3:
    refused. (outcome, answer, done).
    A record of storage has no «Выдали» (the owner 2026-11-10: given out from «На точке», sold from «На ОТХ» by «Продать»): refused, the
    question stays open for «На точке»."""
    card = orders.get_card(conn, card["id"])                     # the stage as it is now
    label = card.get("webmim_number") or str(card["id"])
    if card.get("oth_record"):
        conn.rollback()
        return "не подходит: запись хранения, нужно «Продать»", RECORD_ISSUED % label, False
    if (return_plan(conn, card["id"]) or {}).get("signed_at"):
        conn.rollback()
        return ("не выполнено: акт МХ-3 подписан", "Заказ №%s: акт МХ-3 уже подписан, шины отданы поставщику. Отметить «Выдали» нельзя: напишите офису." % label, False)
    if card["stage"]["code"] not in RETURNING or not _answer(conn, card, "issued", now):
        conn.rollback()
        return "уже отмечено", "Заказ №%s уже отмечен или ушёл с «На ОТХ»: обновите страницу." % label, False
    done = next((s for s in orders.list_stages(conn) if s["code"] == "done"), None)
    if done is None:
        conn.rollback()
        return "не выполнено: нет этапа «Выполнено»", "Не получилось: в воронке нет этапа «Выполнено».", False
    plan = return_plan(conn, card["id"])                         # read before it goes: agreed or not decides what left_return writes
    conn.execute("DELETE FROM app_state WHERE key = ?", (DROPPED_KEY % card["id"],))
    forget_dropped(conn, card["id"])                             # the marks an earlier sale or «Выдали» took off: not this one's
    if plan is None:                                             # not agreed: the client took the tyres, the return is not needed (the owner 2026-11-10)
        drop_marks(conn, card["id"], now, who, why=ISSUED_NO_RETURN)
        _end_tasks(conn, card["id"], True, ISSUED_NO_RETURN[:1].lower() + ISSUED_NO_RETURN[1:], now, who)
    if conn.execute("DELETE FROM oth_return_plan WHERE order_id = ? AND signed_at IS NULL", (card["id"],)).rowcount:
        held = conn.execute("SELECT item_id, qty, at, author FROM oth_return WHERE order_id = ?", (card["id"],)).fetchall()     # the undo's detach drops them
        conn.execute("INSERT INTO app_state (key, value) VALUES (?, ?)", (DROPPED_KEY % card["id"], json.dumps(dict(plan, held=[list(r) for r in held]),
                                                                                                         ensure_ascii=False)))     # for «Отменить» (point_log.undo)
        orders.add_event(conn, card["id"], now, who, "field", "Согласование возврата снято: точка ответила, что заказ выдали")
    reattach(conn, card["id"], now, who)
    orders.add_event(conn, card["id"], now, who, "point", "Точка ответила на 7-й день ОТХ: заказ выдали")
    try:
        moved = orders.move_card(conn, card["id"], done["id"], now, who)
    except orders.OrderError as err:                                                 # rolled back with the answer: the question stays
        return "не выполнено: %s" % err, "Не получилось: %s" % err, False
    if not moved:
        conn.rollback()
        return "не перенесён: заказ уже на другом этапе", "Заказ №%s уже переместили, проверьте его в панели." % label, False
    return "заказ перенесён на «%s»" % done["name"], "Заказ №%s — отмечено: выдан (теперь на этапе «%s»)." % (label, done["name"]), True


SOLD_SKIP = "шины проданы с ОТХ другому покупателю: прежнему клиенту не пишем"
OFFICE_SOLD = "Продано с ОТХ: запись хранения переведена в «Выполнено» в панели"
AGREED_REFUSED = "не выполнено: возврат согласован"
AGREEING_REFUSED = "не выполнено: шины на согласовании возврата"
AGREEING_ANSWER = "Шины на согласовании возврата: продать можно после отмены согласования оператором"     # the owner 2026-11-12, word for word
AGREED_ANSWER = "Заказ №%s: возврат уже согласован с поставщиком, шины едут на возврат. Отметить «Продать» нельзя: напишите офису."


def given_back(conn, order_id) -> bool:
    """Some tyres of the order really went back to the supplier («Вернуть поставщику»): the point does not sell such an order by one press (review
    2026-11-10). The pieces of oth_return that are a mark «На возврат» (_marked, «Ожидает согласование возврата») lie at the point: not given back."""
    since = _oth_since(conn, order_id)
    return any(int(qty) - _marked(conn, item_id, int(qty), since) > 0
               for item_id, qty in conn.execute("SELECT item_id, qty FROM oth_return WHERE order_id = ? AND qty > 0", (order_id,)).fetchall())


def _record_part(conn, card: dict) -> Optional[tuple]:
    """(pieces in it, pieces of the split) of a record of storage with a mark «На возврат» that no longer holds every tyre it was made with
    (the owner 2026-11-11: such a record is sold or returned whole: «если на ОТХ 4, то и продадим 4 или вернём 4»); None: it holds them all, it
    has no mark, or it is no record (the orders put on «На ОТХ» the old way are sold as before)."""
    from . import oth_storage
    mark = oth_storage.record(conn, card["id"])
    if mark is None or card["stage"]["code"] != "oth" or not oth_storage.returning(conn, card["id"]):
        return None
    have, made = sum(int(i["qty"]) for i in card["items"]), int(mark.get("pieces") or 0)
    return (have, made) if have < made else None


def sell(conn, card: dict, who: str, now: str, office: bool = False) -> tuple:
    """«Продать» of «Рабочее место» (the owner 2026-11-10): the tyres on «На ОТХ» or «На возврат» went to a buyer from the street, the whole order goes
    to «Выполнено» in one transaction. Not answer_issued: no question of the seventh day is needed (an open one closes: «sold»), and the client put
    aside is NOT linked again (the buyer is another person: the order stays without a client). Only until the return is agreed (the owner
    2026-11-10): agreed, refused; the mark «На возврат» and the task go with the move (left_return). The owner 2026-11-12: the point sells on
    «На ОТХ» only — an order on the agreement of the return (`agreeing`) is refused to it, the office (`office`) sells it as before. The rules of «Выполнено» run as for any order
    (the sale is theirs), but their messages to the client are skipped: the order's chat is the old client's. After the act МХ-3: refused. (outcome, answer, done).
    `office`: a record of storage the office moved to «Выполнено» (orders.move_card → _sell_record, the owner 2026-11-10): the same sale, its
    record in the journal says so; the refusal is the caller's to write."""
    card = orders.get_card(conn, card["id"])                     # the stage as it is now
    label = card.get("webmim_number") or str(card["id"])
    if (return_plan(conn, card["id"]) or {}).get("signed_at"):
        conn.rollback()
        return ("не выполнено: акт МХ-3 подписан", "Заказ №%s: акт МХ-3 уже подписан, шины отданы поставщику. Отметить «Продать» нельзя: напишите офису." % label, False)
    if card["stage"]["code"] not in RETURNING:
        conn.rollback()
        return "уже отмечено", "Заказ №%s уже отмечен или ушёл с «На ОТХ»: обновите страницу." % label, False
    if return_plan(conn, card["id"]) is not None:                # agreed: the tyres go back to the supplier (the owner 2026-11-10)
        conn.rollback()
        return AGREED_REFUSED, AGREED_ANSWER % label, False
    if not office and agreeing(conn, card):                      # the owner 2026-11-12 (over 2026-11-10): the point sells on «На ОТХ» only; on the
        conn.rollback()                                          # agreement of the return the office sells, or takes the agreement off first
        return AGREEING_REFUSED, AGREEING_ANSWER, False
    if given_back(conn, card["id"]):                             # the sale is of the whole order: its quantity would count them too
        conn.rollback()
        return ("не выполнено: часть шин возвращена поставщику", "Заказ №%s: часть шин уже возвращена поставщику. Отметить «Продать» нельзя: "
                "напишите офису, сколько шин продано." % label, False)
    part = _record_part(conn, card)
    if part:                                                     # the owner 2026-11-11: a record with a mark «На возврат» is sold whole only
        conn.rollback()
        return ("не выполнено: запись хранения не целиком", "Заказ №%s: запись на возврат поставщику продаётся только целиком, в ней %d из %d шт. "
                "Отметить «Продать» нельзя: напишите офису." % ((label,) + part), False)
    done = next((s for s in orders.list_stages(conn) if s["code"] == "done"), None)
    if done is None:
        conn.rollback()
        return "не выполнено: нет этапа «Выполнено»", "Не получилось: в воронке нет этапа «Выполнено».", False
    conn.execute("DELETE FROM app_state WHERE key = ?", (DROPPED_KEY % card["id"],))      # an agreement an earlier «Выдали» dropped: not this sale's
    forget_dropped(conn, card["id"])                                                          # nor the marks it took off
    if return_plan(conn, card["id"]) is not None:                # agreed (or the act signed) between the check above and the write: nothing is sold
        conn.rollback()
        return AGREED_REFUSED, AGREED_ANSWER % label, False
    _answer(conn, card, "sold", now)                             # an open question of the seventh day is answered by the sale
    if office:
        orders.add_event(conn, card["id"], now, who, "field", OFFICE_SOLD)
    else:
        orders.add_event(conn, card["id"], now, who, "point", "Точка продала шины с ОТХ")
    try:
        moved = orders.move_card(conn, card["id"], done["id"], now, who, journal=not office, oth_sold=True)
    except orders.OrderError as err:                                                 # rolled back with the rest
        return "не выполнено: %s" % err, "Не получилось: %s" % err, False
    if not moved:
        conn.rollback()
        return "не перенесён: заказ уже на другом этапе", "Заказ №%s уже переместили, проверьте его в панели." % label, False
    try:                                                                             # nothing is sent by itself on «Выполнено»: the prepared texts
        from . import order_engine                                                   # to the old client are closed before a person can press them
        for task in order_engine.list_tasks(conn, card["id"], ("pending", "error")):
            if task["kind"] == "message" and task["stage_id"] == done["id"] and order_engine.audience(conn, task) == "client":
                order_engine._finish(conn, task, "skipped", SOLD_SKIP, now, who)
    except Exception:                                                                # noqa: BLE001 - the sale stands
        conn.rollback()
        logging.getLogger("tires_sync.orders").exception("the client's messages of order %s sold from «На ОТХ» were not skipped", card["id"])
    return "заказ перенесён на «%s»" % done["name"], "Заказ №%s — отмечено: продан с ОТХ (теперь на этапе «%s»)." % (label, done["name"]), True


def close_return_tasks(conn, order_id, now: str, who: str, result: str) -> int:
    """The open tasks of the return of the order are done by themselves (the lead 2026-11-02): agreed. Never raises: the answer stands; returns how many were closed."""
    return _close_tasks(conn, order_id, RETURN_TITLES, "done", now, who, result)


def sold_early_tasks(conn, order_id, now: str, who: str) -> int:
    """drop_marks of a part («Взять с ОТХ») left the order on «На ОТХ» with no mark «На возврат» of this stay: nothing waits for the agreement,
    its task is cancelled. Commits."""
    card = orders.get_card(conn, order_id)
    if card["stage"]["code"] != "oth" or return_plan(conn, order_id) is not None:
        return 0
    since = _oth_since(conn, order_id)
    if any(str(m.get("at") or "") >= since for key, m in item_marks(conn).items() if int(key.split(":")[1]) in {i["id"] for i in card["items"]}):
        return 0
    return _close_tasks(conn, order_id, RETURN_TITLES, "cancel", now, who, SOLD_EARLY.lower())


def _close_tasks(conn, order_id, titles: tuple, how: str, now: str, who: str, result: str = "") -> int:
    from . import tasks
    closed = 0
    for (task_id,) in conn.execute("SELECT id FROM callback_task WHERE order_id = ? AND title IN (%s) AND done_at IS NULL AND cancelled_at IS NULL"
                                   % ",".join("?" * len(titles)), (order_id,) + titles).fetchall():
        try:
            tasks.close(conn, task_id, how, now, who, result)
            closed += 1
        except Exception:                                                            # noqa: BLE001 - closed meanwhile, or a busy base
            conn.rollback()
            logging.getLogger("tires_sync.orders").exception("the return task %s of order %s was not closed", task_id, order_id)
    return closed


def restore_plan(conn, order_id) -> None:
    """«Отменить» of «Выдали»: the agreement it dropped is back as it was, with the pieces of oth_return it held (the marks «На возврат»). No commit."""
    plan = json.loads(store_value(conn, DROPPED_KEY % order_id) or "null")
    conn.execute("DELETE FROM app_state WHERE key = ?", (DROPPED_KEY % order_id,))
    if plan:
        conn.execute("INSERT OR REPLACE INTO oth_return_plan (order_id, agreed_at, agreed_by, return_date, signed_at, signed_by) VALUES (?,?,?,?,NULL,'')",
                     (order_id, plan["agreed_at"], plan["agreed_by"], plan["return_date"]))
        for item, qty, at, author in plan.get("held") or []:
            conn.execute("INSERT OR REPLACE INTO oth_return (item_id, order_id, qty, at, author) VALUES (?, ?, ?, ?, ?)", (item, order_id, qty, at, author))


def left_return(conn, card_id, stage: dict, now: str, who: str) -> None:
    """The order left «На ОТХ» / «На возврат» by any path, or went from «На возврат» back on sale on «На ОТХ» (orders.move_card, in its transaction: no commit, the
    owner 2026-11-04): the open tasks of the return close by themselves — done when the order closes (won, closed), cancelled when it goes back to work or is lost.
    Won with no agreement (sold before it, the owner 2026-11-10): the mark «На возврат» goes (drop_marks) and the task is cancelled."""
    done = stage["kind"] in ("won", "closed")
    early = (stage["kind"] == "won" and return_plan(conn, card_id) is None      # sold before the return was agreed (the owner 2026-11-10); an agreement
             and not store_value(conn, DROPPED_KEY % card_id))                  # «Выдали» dropped just before the move was there: nothing «до согласования»
    if early:
        drop_marks(conn, card_id, now, who)
        done = False                                                             # nothing was agreed: the task is cancelled, not done
    _end_tasks(conn, card_id, done, SOLD_EARLY.lower() if early else "заказ на этапе «%s»" % stage["name"], now, who)


def _end_tasks(conn, card_id, done: bool, why: str, now: str, who: str) -> None:
    """The open tasks of the return of the order close, done or cancelled, with `why` in the journal. No commit."""
    from . import tasks
    for task_id, kind, title in conn.execute("SELECT id, kind, title FROM callback_task WHERE order_id = ? AND title IN (?, ?) AND done_at IS NULL AND cancelled_at IS NULL",
                                             (card_id,) + RETURN_TITLES).fetchall():
        conn.execute("UPDATE callback_task SET %s = ?, done_by = ? WHERE id = ?" % ("done_at" if done else "cancelled_at"), (now, who, task_id))
        orders.add_event(conn, card_id, now, who, "task", "Задача %s: %s — %s" % ("выполнена" if done else "отменена", tasks._what(kind, title), why), level="grey")
        _audit(conn, now, who, "callback_done" if done else "callback_cancel", {"task": int(task_id)})


NO_RETURN_KEY = "oth_no_return:%d:%s"            # per order and stay on «На ОТХ»: the task of the return closed «не возвращаем» (the owner 2026-11-07)
NO_RETURN = re.compile(r"(?<![^\W\d_])не\s+(?:будем\s+)?возвра(?:щаем|щать|тим)(?![^\W\d_])", re.I)     # «не возвращаем»; never «ещё не возвращали»


WAITING = re.compile(r"(?<![^\W\d_])(?:пока|ждем)(?![^\W\d_])", re.I)     # «пока», «ждём/ждем» (the owner 2026-11-08): never a refusal
BACK = "Шины снова в продаже"


def task_closed(conn, task: dict, how: str, result: str, who: str, now: str) -> None:
    """tasks.close: the task of the return of an order done with «не возвращаем» in its result (the owner 2026-11-07, 2026-11-08): the tyres taken off
    sale for the return are on sale again by themselves — the marks «На возврат» of the positions of this stay on «На ОТХ» (_back_on_sale), the order on
    «На возврат» not agreed yet («Согласуем») goes back to «На ОТХ» (_back_to_oth); the seventh day leaves that stay alone (ask_seventh_day). «пока»,
    «ждём» in the result: not a refusal, the order still waits for the agreement (_still_waiting). Saved here (commits): the close itself first."""
    if how != "done" or task.get("title") not in RETURN_TITLES or not task.get("order_id"):
        return
    text = str(result or "").replace("ё", "е").replace("Ё", "Е")
    waiting, refused = WAITING.search(text), NO_RETURN.search(text)
    if not (waiting or refused):
        return
    conn.commit()                                                                    # the task is closed whatever follows
    card = orders.get_card(conn, task["order_id"])
    if waiting:
        _still_waiting(conn, card, task)
    elif card["stage"]["code"] == "oth":
        if conn.execute("INSERT OR IGNORE INTO app_state (key, value) VALUES (?, ?)", (NO_RETURN_KEY % (card["id"], re.sub(r"\D", "", card["entered_at"] or "")),
                                                                                      json.dumps({"at": now, "by": who, "task": task["id"]}))).rowcount == 1:
            orders.add_event(conn, card["id"], now, who, "field", "Задача возврата закрыта «не возвращаем»: на 7-й день заказ не перейдёт на «%s»"
                             % orders.stage_name(conn, orders.RETURN_GIVEN), level="grey")
        _back_on_sale(conn, card, now, who)
    elif card["stage"]["code"] == orders.RETURN_GIVEN:
        _back_to_oth(conn, card, task, now, who)
    conn.commit()


def _still_waiting(conn, card: dict, task: dict) -> None:
    """«пока», «ждём»: the marks «На возврат» of the positions of this stay closed with the task keep «Ожидает согласование возврата» (item_marks)
    until the agreement or the seventh day. No commit."""
    if card["stage"]["code"] != "oth":
        return                                                                       # «На возврат»: the label is the stage's, nothing to keep
    for item in card["items"]:
        key = _item_key(item["id"], card["entered_at"])
        mark = json.loads(store_value(conn, key) or "null")
        if mark and mark.get("task") == task["id"]:
            conn.execute("UPDATE app_state SET value = ? WHERE key = ?", (json.dumps(dict(mark, waiting=True), ensure_ascii=False), key))


def _back_on_sale(conn, card: dict, now: str, who: str) -> None:
    """The marks «На возврат» of the positions of this stay are gone and their pieces leave oth_return: on sale again. A real «Вернуть поставщику»
    (the pieces beyond the mark) stays. No commit."""
    for item in card["items"]:
        key = _item_key(item["id"], card["entered_at"])
        mark = json.loads(store_value(conn, key) or "null")
        if not mark:
            continue
        row = conn.execute("SELECT qty FROM oth_return WHERE item_id = ?", (item["id"],)).fetchone()
        pieces = min(int(mark.get("qty") or 0), int(row[0]) if row else 0)
        if pieces > 0:
            conn.execute("UPDATE oth_return SET qty = qty - ? WHERE item_id = ?", (pieces, item["id"]))
            conn.execute("DELETE FROM oth_return WHERE item_id = ? AND qty <= 0", (item["id"],))
        conn.execute("DELETE FROM app_state WHERE key = ?", (key,))
        if pieces > 0:
            orders.add_event(conn, card["id"], now, who, "field", "%s: %s, %d шт. — задача возврата закрыта «не возвращаем»" % (BACK, item["name"], pieces))
            _audit(conn, now, who, "oth_back_on_sale", {"order": card["id"], "item": item["id"], "qty": pieces})


def _back_to_oth(conn, card: dict, task: dict, now: str, who: str) -> None:
    """The order on «На возврат» with no agreement: back to «На ОТХ», on sale (orders.move_card: the marks of the stay go, the tasks of the return close;
    no rule of the stage, nothing to the client), and the seventh day of the new stay leaves it alone. Not when tyres were really given back to the
    supplier (the move would forget them) or the move is refused: one yellow record, a person decides. An agreed return is never undone."""
    if return_plan(conn, card["id"]) is not None:
        return
    held, ordered = sum(pieces_here(conn, card).values()), sum(int(i["qty"]) for i in card["items"] if int(i["qty"]) >= 1)
    oth = next((s for s in orders.list_stages(conn) if s["code"] == "oth"), None)
    why = "часть шин уже отдана поставщику" if held < ordered else ("в воронке нет этапа «На ОТХ»" if oth is None else "")
    if not why:
        conn.execute("INSERT OR IGNORE INTO app_state (key, value) VALUES (?, ?)", (NO_RETURN_KEY % (card["id"], re.sub(r"\D", "", now)),
                                                                                   json.dumps({"at": now, "by": who, "task": task["id"]})))
        try:
            moved = orders.move_card(conn, card["id"], oth["id"], now, who, fire_rules=False)     # commits the key with the move
        except orders.OrderError as err:                                             # rolled back with the key
            moved, why = False, str(err)
        if moved:
            orders.add_event(conn, card["id"], now, who, "field", "%s: задача возврата закрыта «не возвращаем», заказ снова на «%s»; на 7-й день не перейдёт на «%s»"
                             % (BACK, oth["name"], card["stage"]["name"]))
            _audit(conn, now, who, "oth_back_on_sale", {"order": card["id"], "from": orders.RETURN_GIVEN})
            return
        if not why:
            return                                                                   # moved meanwhile by somebody: theirs stands
    orders.add_event(conn, card["id"], now, who, "field", "Задача возврата закрыта «не возвращаем», но заказ остался на «%s»: %s. Верните шины в продажу вручную."
                     % (card["stage"]["name"], why), level="yellow")


def _kept(conn, card: dict) -> bool:
    return conn.execute("SELECT 1 FROM app_state WHERE key = ?", (NO_RETURN_KEY % (card["id"], re.sub(r"\D", "", card["entered_at"] or "")),)).fetchone() is not None


EXTEND_DAYS = 2                                  # «Продлить» of the task of the return: 2 working days, Monday to Friday (the owner 2026-11-07)


def can_extend(conn, task: dict) -> bool:
    """«Продлить на 2 рабочих дня» of an open task of the return, while the point has not given the tyres to the driver (no act МХ-3 signed)."""
    if task.get("title") not in RETURN_TITLES or task.get("done_at") or task.get("cancelled_at") or not task.get("order_id"):
        return False
    row = conn.execute("SELECT s.code FROM order_card c JOIN order_stage s ON s.id = c.stage_id WHERE c.id = ?", (task["order_id"],)).fetchone()
    return bool(row) and row[0] in RETURNING and not (return_plan(conn, task["order_id"]) or {}).get("signed_at")


def extend_task(conn, task_id, now: str, who: str) -> str:
    """The task of the return gets EXTEND_DAYS working days more, from its due date or from now when it is late. Returns the new due date."""
    from . import tasks
    task = tasks.get(conn, task_id)
    if not can_extend(conn, task):
        raise OthError("Продлить можно только открытую задачу возврата, пока точка не отдала шины водителю.")
    start = max(str(task["due_at"])[:16], now[:16])
    due = working_due(start + ":00" if len(start) == 16 else start, EXTEND_DAYS)[:16]
    if conn.execute("UPDATE callback_task SET due_at = ? WHERE id = ? AND due_at = ? AND done_at IS NULL AND cancelled_at IS NULL",
                    (due, task_id, task["due_at"])).rowcount != 1:
        conn.rollback()
        raise OthError("Задачу только что изменили: обновите страницу.")
    orders.add_event(conn, task["order_id"], now, who, "task", "Задача продлена: %s — до %s.%s %s (точка ещё не отдала шины водителю)" % (
        tasks._what(task["kind"], task["title"]), due[8:10], due[5:7], due[11:16]), level="grey")
    _audit(conn, now, who, "callback_extend", {"task": int(task_id), "from": task["due_at"], "to": due})
    conn.commit()
    return due


def _open_return_task(conn, order_id) -> bool:
    return conn.execute("SELECT 1 FROM callback_task WHERE order_id = ? AND title IN (?, ?) AND done_at IS NULL AND cancelled_at IS NULL LIMIT 1",
                        (order_id,) + RETURN_TITLES).fetchone() is not None


def answer_here(conn, card: dict, who: str, now: str) -> tuple:
    """«На точке» (the owner 2026-11-04): only marks the answer; the order stays where it is, no second task. (outcome, answer, done)."""
    card = orders.get_card(conn, card["id"])                     # the stage as it is now (review 2026-11-03)
    label = card.get("webmim_number") or str(card["id"])
    if card["stage"]["code"] not in RETURNING or not _answer(conn, card, "here", now):
        conn.rollback()
        return "уже отмечено", "Заказ №%s уже отмечен или ушёл с «На ОТХ»: обновите страницу." % label, False
    orders.add_event(conn, card["id"], now, who, "point", "Точка ответила на 7-й день ОТХ: шины на точке", level="grey")
    conn.commit()
    return "отмечено: шины на точке", "Заказ №%s — отмечено: шины на точке. Возврат поставщику идёт дальше." % label, True


# ---- the owner 2026-11-04: the dates of «На ОТХ», the alert of 6 days 12 hours, «На возврат» by hand ----------------------------------------
AGREE_AFTER = timedelta(days=6, hours=12)
AGREE_KEY = "oth_agree_due:%d:%s"                # per order and stay on «На ОТХ»: told|<at>
TASK_DAYS_KEY = "orders.return_task_days"        # «Срок задачи на возврат», working days; the owner sets it (orders_settings)
TASK_DAYS_DEFAULT, TASK_DAYS_MAX = 2, 30      # the owner 2026-11-04: 2 working days from the move
ITEM_KEY = "oth_item_return:%d:%s"               # per position and stay on «На ОТХ»: {"qty", "at", "by", "task"} — a position moved by hand, «Согласуем» while its task is open
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def reserve_dates(conn) -> Dict[int, dict]:
    """The orders on «На ОТХ» and «На возврат» → {"arrived": the day the goods came to the point, "until": the last date of the reservation, "oth_since": the day
    they last entered «На ОТХ»}, ISO, '' when unknown. From the journal: entering «На ОТХ» clears the date (orders._start_oth) and no column keeps the arrival."""
    found: Dict[int, dict] = {}
    edited = orders.LABELS["reserve_until"] + ": "
    for order_id, at, text in conn.execute(
            "SELECT e.order_id, e.at, e.text FROM order_event e JOIN order_card c ON c.id = e.order_id JOIN order_stage s ON s.id = c.stage_id "
            "WHERE s.code IN ('oth', 'return_given') AND e.kind = 'field' AND (e.text LIKE 'Резерв%' OR e.text LIKE ? OR e.text = ?) ORDER BY e.id", (edited + "%", orders.OTH_ENTERED)):
        row = found.setdefault(order_id, {"arrived": "", "until": "", "oth_since": ""})
        if text == orders.OTH_ENTERED:
            row["oth_since"] = str(at or "")[:10]
            continue
        day = _DATE.search(text)
        if text.startswith("Резерв: товар на точке"):
            row.update(arrived=str(at or "")[:10], until=day.group(0) if day else "")
        elif text.startswith((orders.RESERVE_WAS % "", edited)):
            row["until"] = day.group(0) if day else ""
    for order_id, until in conn.execute("SELECT c.id, c.reserve_until FROM order_card c JOIN order_stage s ON s.id = c.stage_id "
                                        "WHERE s.code IN ('oth', 'return_given') AND c.reserve_until != ''"):
        found.setdefault(order_id, {"arrived": "", "until": "", "oth_since": ""})["until"] = until      # moved here from the point past «На ОТХ»: the date stands
    return found


def _day(iso: str) -> str:
    return "%s.%s.%s" % (iso[8:10], iso[5:7], iso[:4]) if iso else "нет"


def call_line(card: dict, until: str) -> str:
    """The line of «Позвонить клиенту» (the owner 2026-11-04): the positions with their pieces, the sum, the day of the order, the end of the
    reservation, the point, the tags."""
    items = [i for i in card["items"] if int(i["qty"]) >= 1]
    return "%s; сумма %s ₽; заказ от %s; резерв до %s; точка «%s»; теги: %s" % (
        ", ".join("%s, %d шт." % (i["name"], int(i["qty"])) for i in items) or "позиций нет",
        "{:,.0f}".format(orders.totals(items)["sale"]).replace(",", " "), _day(str(card["created_at"] or "")[:10]), _day(until),
        (card.get("point") or {}).get("name") or "не указана", ", ".join(card["tags"]) or "нет")


def order_link(conn, card_id) -> str:
    """The link of an alert: the full address when the panel's public address is set (as order_arrived._link), else the path."""
    base = (os.environ.get("TIRES_PUBLIC_BASE") or store_value(conn, "public_base") or "").rstrip("/")
    return "%s/orders/%d" % (base, int(card_id))     # no base: the path, as before


def agree_due(card: dict, now: str) -> bool:
    """6 days 12 hours since the order entered «На ОТХ», to the second."""
    try:
        return datetime.fromisoformat(now) >= datetime.fromisoformat(card["entered_at"]) + AGREE_AFTER
    except (TypeError, ValueError):
        return False


def tell_agree_due(conn, now: str, notify) -> int:
    """The tick: an order 6 days 12 hours on «На ОТХ» tells its responsible once per stay «Нужно согласовать на возврат» (the stage stays). The same orders as
    the seventh day (`_HELD_SQL`: since its first tick, no return agreed or marked, no other city, no point's own sale), and one without a point too:
    no seventh day moves it, the responsible still has to decide. Not a stay whose task of the return was closed «не возвращаем» (_kept, the owner
    2026-11-07): nothing is left to agree. The seventh day usually comes first (an order enters «На ОТХ» after 00:30): it tells
    with the move, and the stay is told once all the same (`_tell_agree`). Returns how many were told."""
    from . import order_points as points
    since = store_value(conn, SINCE_KEY)
    told = 0
    for card_id, entered in conn.execute(_HELD_SQL + "ORDER BY c.id", (since,)).fetchall() if since else ():
        try:
            card = orders.get_card(conn, card_id)
            if card["stage"]["code"] != "oth" or card["entered_at"] != entered:
                continue
            if _kept(conn, card):
                continue                                                     # the task of the return closed «не возвращаем»: nothing to agree
            if agree_due(card, now) and not (card["point_id"] and card_id in points.own_sales(conn, card["point_id"])):
                told += _tell_agree(conn, card, now, notify)
        except Exception:                                                            # noqa: BLE001 - one order must not stop the others
            conn.rollback()
            logging.getLogger("tires_sync.orders").exception("the alert of 6 days 12 hours of order %s failed", card_id)
    return told


def _tell_agree(conn, card: dict, now: str, notify) -> int:
    """«Нужно согласовать на возврат» of the stay on «На ОТХ» of `card` (read before any move), once: 1 when told. The state is kept first: a failed alert leaves its record."""
    from . import message_texts, order_alerts, order_manager
    card_id, since = card["id"], str(card["entered_at"] or "")
    people = order_alerts.told_people(conn, card_id, office_logins(conn))            # read first: a failure here leaves it to the next tick
    if conn.execute("INSERT OR IGNORE INTO app_state (key, value) VALUES (?, ?)", (AGREE_KEY % (card_id, re.sub(r"\D", "", since)), "told|" + now)).rowcount != 1:
        conn.rollback()
        return 0
    orders.add_event(conn, card_id, now, "система", "reserve", "Нужно согласовать на возврат: заказ на ОТХ с %s" % _day(since[:10]), level="yellow")
    conn.commit()
    text = message_texts.text(conn, message_texts.AGREE_DUE, order_label=order_manager.label(card), link=order_link(conn, card_id), since=_day(since[:10]),
                              point=(card.get("point") or {}).get("name") or "не указана")
    for who in people:
        try:
            notify(dict(card, responsible=who, to_staff=True), text)
        except Exception:                                                            # noqa: BLE001 - the journal has its record
            conn.rollback()
            logging.getLogger("tires_sync.orders").exception("the alert of the return of order %s failed", card_id)
    return 1


HANDOVER_AT = "08:59"                            # the owner 2026-11-04: on the next day of the supplier the point is told again
REMIND_KEY = "oth_handover_remind:%d:%s"         # per order and day


def remind_handover(conn, now: str, notify) -> int:
    """The point did not press «Подписал акт и отдал» by the agreed date: on every later Monday, Wednesday and Friday from 08:59 to the end of the working
    hours it is told again, once a day, until it presses. Returns how many were told."""
    from . import message_texts, order_manager
    today = date.fromisoformat(now[:10])
    if today.weekday() not in RETURN_DAYS or now[11:16] < HANDOVER_AT or int(now[11:13]) >= order_manager.duty_settings(conn)["hours"][1]:
        return 0                                                                     # a late catch-up of the timer never tells at night
    told = 0
    for card_id, day in conn.execute("SELECT c.id, r.return_date FROM oth_return_plan r JOIN order_card c ON c.id = r.order_id JOIN order_stage s ON s.id = c.stage_id "
                                     "WHERE r.signed_at IS NULL AND r.return_date < ? AND c.point_id IS NOT NULL AND s.code IN (?, ?) ORDER BY c.id",
                                     (today.isoformat(),) + RETURNING).fetchall():
        if conn.execute("INSERT OR IGNORE INTO app_state (key, value) VALUES (?, ?)", (REMIND_KEY % (card_id, today.isoformat()), now)).rowcount != 1:
            continue
        conn.commit()
        try:
            card = orders.get_card(conn, card_id)
            plan = return_plan(conn, card_id)
            if card["stage"]["code"] not in RETURNING or not plan or plan["signed_at"]:  # pressed during the others' alerts
                continue
            notify(dict(card, responsible="", to_staff=False),                       # "": the alert goes to the point
                   message_texts.text(conn, message_texts.HANDOVER_REMIND, order_label=order_manager.label(card), date=_short(day)))
            told += 1
        except Exception:                                                            # noqa: BLE001 - the others still go
            conn.rollback()
            logging.getLogger("tires_sync.orders").exception("the reminder of the return of order %s failed", card_id)
    return told


def task_days(conn) -> int:
    """«Срок задачи на возврат», working days."""
    text = store_value(conn, TASK_DAYS_KEY)
    return int(text) if text.isascii() and text.isdigit() and 1 <= int(text) <= TASK_DAYS_MAX else TASK_DAYS_DEFAULT


def set_task_days(conn, value) -> int:
    text = str(value if value is not None else "").strip()
    if not (text.isascii() and text.isdigit()) or not 1 <= int(text) <= TASK_DAYS_MAX:
        raise OthError("Срок задачи на возврат: целое число рабочих дней от 1 до %d." % TASK_DAYS_MAX)
    conn.execute("INSERT OR REPLACE INTO app_state (key, value) VALUES (?, ?)", (TASK_DAYS_KEY, str(int(text))))
    conn.commit()
    return int(text)


def working_due(now: str, days: int) -> str:
    """`days` working days from `now`, at the same time of the day. A working day: Monday to Friday; no calendar of holidays."""
    moment = datetime.fromisoformat(now)
    while days > 0:
        moment += timedelta(days=1)
        days -= moment.weekday() < 5
    return moment.isoformat(timespec="minutes")


def _return_task(conn, card: dict, now: str, who: str, what: str = "", commit: bool = True) -> int:
    """The one task of a return, by hand or on the seventh day: kind «Вернуть», to the order's responsible (nobody of the staff answers for it: the owner's login), due by
    the setting. tasks.add saves the caller's changes with it (its audit commits); `commit` False: the caller saves it."""
    from . import order_alerts, order_notify, tasks
    owner = order_notify.get_owner_login(conn)
    kinds = order_alerts.people_kinds(conn)
    person = order_alerts.responsibles(conn, kinds, card["id"]).get(card["id"])      # a user of a point cannot write to the supplier: the owner's (the lead 2026-11-05)
    return tasks.add(conn, kind="return", title=RETURN_TASK, due_at=working_due(now, task_days(conn)), now=now, author=who, owner_login=owner,
                     assignee=person if person and kinds.get(person) == "staff" else owner, order_id=card["id"], commit=commit,
                     note="Заказ №%s, точка «%s»%s, /orders/%d" % (card.get("webmim_number") or card["id"], (card.get("point") or {}).get("name") or "не указана",
                                                                  ", " + what if what else "", card["id"]))


def moved_by_hand(conn, before: dict, now: str, who: str) -> None:
    """A person moved the order in the panel (`before`: the card before the move; the board, the window, the chat, the button of the card).
    Into «На возврат»: the task of the return, unless the return is agreed already or that task is open: no double; the point is asked
    «Заказ выдали или он на точке?» as on the seventh day (the owner 2026-11-07). Never raises: the move stands."""
    try:
        card = orders.get_card(conn, before["id"])
    except orders.OrderError:                                                        # removed meanwhile
        return
    if card["stage"]["code"] == orders.RETURN_GIVEN and before["stage"]["code"] != orders.RETURN_GIVEN:
        made = _ensure_task(conn, card["id"], now, who)
        try:                                                                         # a VK point hears the status, a failed task is made again (finish_moved)
            if card.get("point_id") and conn.execute("INSERT OR IGNORE INTO app_state (key, value) VALUES (?, ?)",
                                                     (_ask_key(card), "asked|" + now)).rowcount == 1:     # the question of the 7th day by hand too (2026-11-07)
                orders.add_event(conn, card["id"], now, who, "point", "Точке «%s» задан вопрос: «%s»" % (
                    (card.get("point") or {}).get("name") or "", question(conn, card, now)), level="grey")
            conn.execute("INSERT OR REPLACE INTO app_state (key, value) VALUES (?, ?)", (PENDING_KEY % card["id"], json.dumps(
                {"since": before["entered_at"], "entered": card["entered_at"], "auto": False, "who": who, "task": made}, ensure_ascii=False)))
            conn.commit()
        except Exception:                                                            # noqa: BLE001 - the move stands
            conn.rollback()
            logging.getLogger("tires_sync.orders").exception("the status of order %s moved by hand was not kept", card["id"])


def _ensure_task(conn, card_id, now: str, who: str) -> bool:
    """The one task of the return of an order on «Согласуем»: made unless agreed or open. Never raises: what was done stands; a failure is one yellow record, and
    the next tick tries again (finish_moved). False: not made."""
    try:
        if return_plan(conn, card_id) is None and not _open_return_task(conn, card_id):
            _return_task(conn, orders.get_card(conn, card_id), now, who)
            conn.commit()
        return True
    except Exception:                                                                # noqa: BLE001
        conn.rollback()
        logging.getLogger("tires_sync.orders").exception("the return task of order %s failed", card_id)
        try:
            text = "Задача возврата не поставлена: поставьте её вручную — %s" % RETURN_TASK
            if not conn.execute("SELECT 1 FROM order_event WHERE order_id = ? AND text = ? AND at >= (SELECT entered_at FROM order_card WHERE id = ?) LIMIT 1",
                                (card_id, text, card_id)).fetchone():
                orders.add_event(conn, card_id, now, "система", "reserve", text, level="yellow")
                conn.commit()
        except Exception:                                                            # noqa: BLE001 - a busy base: the log above is what is left
            conn.rollback()
        return False


def _item_key(item_id, entered: str) -> str:
    return ITEM_KEY % (int(item_id), re.sub(r"\D", "", entered or ""))


def item_to_return(conn, item_id, now: str, who: str, qty=None) -> bool:
    """«На возврат» of one position (the owner 2026-11-04): a part of an order has no stage of its own, so the order stays on «На ОТХ»; its pieces are
    marked in oth_return (the block «Возвращено поставщику» of the page, with its «Отменить») and stay on sale until the return is agreed (stock, the
    owner 2026-11-10), the position shows «Согласуем» while the responsible's task is open. False: moved already."""
    item = _stock_item(conn, item_id)
    card = orders.get_card(conn, item["order_id"])
    if card["stage"]["code"] != "oth":
        raise OthError("Весь заказ уже на этапе «%s»." % card["stage"]["name"])
    if return_plan(conn, card["id"]) is not None:
        raise OthError("Возврат по заказу уже согласован с поставщиком.")
    if item["left"] <= 0:
        raise OthError("Позиция уже вся возвращена поставщику.")
    pieces = item["left"] if qty is None else _pieces(qty)
    if pieces > item["left"]:
        raise OthError("На возврат можно не больше %d шт.: столько осталось на ОТХ." % item["left"])
    key = _item_key(item["item_id"], item["since"])
    if key in item_marks(conn):
        return False
    if item["returned_qty"] and not store_value(conn, key):                          # one row of oth_return: _HELD_SQL would take the real return for a mark
        raise OthError("У позиции уже есть отметка «возвращено поставщику»: снимите её или верните остаток той же кнопкой.")
    old = json.loads(store_value(conn, key) or "null") or {}                         # a task closed earlier in this stay: its pieces are still marked
    kept = min(int(old.get("qty") or 0), int(item["returned_qty"]))                  # in oth_return — the new mark takes them in (review 2026-11-07)
    conn.execute("DELETE FROM app_state WHERE key = ?", (key,))
    mark = {"qty": pieces + kept, "at": now, "by": who, "task": 0}
    if conn.execute("INSERT OR IGNORE INTO app_state (key, value) VALUES (?, ?)", (key, json.dumps(mark, ensure_ascii=False))).rowcount != 1:
        conn.rollback()
        return False
    _add_returned(conn, item, pieces, now, who)
    orders.add_event(conn, card["id"], now, who, "field", "На возврат: %s, %d шт. Метка «%s», шины в продаже до согласования возврата, заказ остаётся на «%s»." % (
        item["name"], pieces, LABELS[AGREE], card["stage"]["name"]))
    _audit(conn, now, who, "oth_item_to_return", {"order": card["id"], "item": item["item_id"], "qty": pieces})
    what = "позиция: %s, %d шт." % (item["name"], pieces)
    found = conn.execute("SELECT id FROM callback_task WHERE order_id = ? AND title IN (?, ?) AND done_at IS NULL AND cancelled_at IS NULL ORDER BY id LIMIT 1",
                         (card["id"],) + RETURN_TITLES).fetchone()
    try:
        if found:                                                                    # one open task per order (the owner 2026-11-05): it names this position too
            from . import tasks
            conn.execute("UPDATE callback_task SET note = substr(note || ', ' || ?, 1, ?) WHERE id = ?", (what, tasks.NOTE_MAX, found[0]))
            mark["task"] = found[0]
        else:
            mark["task"] = _return_task(conn, card, now, who, what)                  # commits it all
    except Exception:
        conn.rollback()
        raise
    conn.execute("UPDATE app_state SET value = ? WHERE key = ?", (json.dumps(mark, ensure_ascii=False), key))
    conn.commit()
    return True


def item_marks(conn) -> Dict[str, dict]:
    """The positions moved by hand whose task is open, or closed with «пока», «ждём» (task_closed), by their key (a key of an old stay is never
    read: `item_mark`)."""
    live = {r[0] for r in conn.execute("SELECT id FROM callback_task WHERE title IN (?, ?) AND done_at IS NULL AND cancelled_at IS NULL", RETURN_TITLES)}
    found = {}
    for key, value in conn.execute("SELECT key, value FROM app_state WHERE key LIKE 'oth_item_return:%'"):
        try:
            mark = json.loads(value)
        except ValueError:
            continue
        if mark.get("task") in live or mark.get("waiting"):                          # «пока», «ждём» (task_closed): still waits
            found[key] = mark
    return found


def item_mark(marks: Dict[str, dict], item_id, entered: str) -> Optional[dict]:
    return marks.get(_item_key(item_id, entered))


def _item_undone(conn, item: dict, rest: int, now: str, who: str) -> None:
    """«Отменить» took the mark of the pieces off: nothing left of a position moved by hand, its label and task go. Never raises: the undo stands."""
    from . import tasks
    key = _item_key(item["item_id"], item["since"])
    try:
        mark = json.loads(store_value(conn, key) or "null")
        if not mark:
            return
        if rest > 0:                                                                 # a part undone: the mark holds what is left (review 2026-11-07)
            if int(mark.get("qty") or 0) > rest:
                conn.execute("UPDATE app_state SET value = ? WHERE key = ?", (json.dumps(dict(mark, qty=rest), ensure_ascii=False), key))
                conn.commit()
            return
        conn.execute("DELETE FROM app_state WHERE key = ?", (key,))
        conn.commit()
        if mark.get("task") and not mark.get("waiting") and not any(m.get("task") == mark["task"] for m in item_marks(conn).values()):     # another position's too: it stays
            tasks.close(conn, mark["task"], "cancel", now, who)
    except Exception:                                                                # noqa: BLE001 - closed by a person meanwhile
        conn.rollback()
        logging.getLogger("tires_sync.orders").exception("the position %s moved by hand was not undone", item["item_id"])


# ---- the owner 2026-11-03: «На возврат» is a stage; the orders standing in the old flow are moved once (store._migrate) -----------------
FLOW_KEY = "return_flow_2026_11_03"             # app_state: the move was done (a second process finds it)
OLD_RETURN_NAME = "Возврат передан"             # the old default name of return_given: renamed «На возврат» only while it is still so
MIGRATED = "Схема возврата 2026-11-03: "


def migrated_since(conn) -> Dict[int, str]:
    """The owner 2026-11-06: an order the migration moved to «На возврат» (and not moved since) shows the day its stay really began, not
    the migration's: {order id: the moment of its stage record before the migration's}. Such an order: on return_given, its entered_at is
    the migration's moment (FLOW_KEY), it has the migration's grey record (MIGRATED) of that moment, and its last stage record is the
    migration's. Display only: entered_at stays, the timers of this module count from it."""
    row = conn.execute("SELECT value FROM app_state WHERE key = ?", (FLOW_KEY,)).fetchone()
    if row is None:
        return {}
    moment, out = row[0], {}
    for (card_id,) in conn.execute(
            "SELECT c.id FROM order_card c JOIN order_stage s ON s.id = c.stage_id WHERE s.code = ? AND c.entered_at = ? AND EXISTS ("
            "SELECT 1 FROM order_event f WHERE f.order_id = c.id AND f.kind = 'field' AND f.at = ? AND f.author = 'система' AND f.text LIKE ?)",
            (orders.RETURN_GIVEN, moment, moment, MIGRATED + "%")).fetchall():
        stages = conn.execute("SELECT at, author FROM order_event WHERE order_id = ? AND kind = 'stage' ORDER BY id DESC LIMIT 2",
                              (card_id,)).fetchall()
        if len(stages) == 2 and tuple(stages[0]) == (moment, "система"):
            out[card_id] = stages[1][0]
    return out


def migrate(conn, now: str) -> Dict[str, int]:
    """Once (FLOW_KEY), in one IMMEDIATE transaction: return_given still called «Возврат передан» is renamed «На возврат»; the orders on it
    (the old meaning: the act МХ-3 signed, the tyres given, the refund awaited) go to «Вернули поставщику» with the refund of the purchase
    (return_changes) — one whose refund cannot be counted, or that misses a field of the stage, stays with a signed plan («Отдано, нужна сумма
    возврата») and a yellow record; then the orders on «На ОТХ» the point said «На точке» of in this stay, or agreed for the return, go to «На
    возврат». No rule of a stage fires, nothing is sent. Returns the counts (the journal of actions keeps them)."""
    done = {"renamed": 0, "closed": 0, "waiting_amount": 0, "to_return_given": 0, "skipped": 0}
    try:
        if conn.execute("SELECT 1 FROM app_state WHERE key = ?", (FLOW_KEY,)).fetchone():
            return done
        conn.execute("SELECT 1 FROM oth_return_plan LIMIT 1")
    except Exception:                                                                 # noqa: BLE001 - a base without the orders' tables
        return done
    try:
        return _migrate(conn, now, done)
    except Exception:                                                                 # noqa: BLE001 - a busy base, a card the move refuses:
        conn.rollback()                                                               # nothing kept, the next start tries again; the panel and
        logging.getLogger("tires_sync.orders").exception("the return flow migration failed")     # the cycle still open the base
        return dict.fromkeys(done, 0)


def _migrate(conn, now: str, done: Dict[str, int]) -> Dict[str, int]:
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("SELECT 1 FROM app_state WHERE key = ?", (FLOW_KEY,)).fetchone():     # another process was first
            conn.rollback()
            return done
        stages = {s["code"]: s for s in orders.list_stages(conn) if s["code"]}
        given, closed = stages.get(orders.RETURN_GIVEN), stages.get("return")
        if given is not None and given["name"] == OLD_RETURN_NAME:
            new = next(d[1] for d in orders.DEFAULT_STAGES if d[0] == orders.RETURN_GIVEN)
            conn.execute("UPDATE order_stage SET name = ? WHERE id = ?", (new, given["id"]))
            given = dict(given, name=new)
            done["renamed"] = 1
        if given is not None:
            for (card_id,) in conn.execute("SELECT id FROM order_card WHERE stage_id = ? ORDER BY id", (given["id"],)).fetchall():
                card = orders.get_card(conn, card_id)
                changes = return_changes(card, RETURN_REASON)
                gaps = ["этап «Вернули поставщику»"] if closed is None else orders.missing_for(dict(card, **changes), closed)
                if not gaps:
                    orders.move_card(conn, card_id, closed["id"], now, "система", changes=apply_refund(conn, card_id, changes, now, "система"),
                                     fire_rules=False, journal=False, commit=False)
                    orders.add_event(conn, card_id, now, "система", "field", MIGRATED + "шины уже отданы поставщику, заказ закрыт, сумма возврата "
                                     "— закупка шин.", level="grey")
                    done["closed"] += 1
                    continue
                conn.execute("INSERT INTO oth_return_plan (order_id, agreed_at, agreed_by, return_date, signed_at, signed_by) VALUES (?,?,?,?,?,?) "
                             "ON CONFLICT(order_id) DO UPDATE SET signed_at = excluded.signed_at, signed_by = excluded.signed_by "
                             "WHERE oth_return_plan.signed_at IS NULL", (card_id, now, "система", now[:10], now, "система"))
                orders.add_event(conn, card_id, now, "система", "point", SIGNED_HEAD + "заполните: %s — и переведите заказ в «%s» (%s)." % (
                    ", ".join(gaps), closed["name"] if closed else "Вернули поставщику", MIGRATED.strip(": ")), level="yellow")   # the staff's (is_return_ask)
                done["waiting_amount"] += 1
            oth = stages.get("oth")
            for (card_id,) in conn.execute("SELECT c.id FROM order_card c WHERE c.stage_id = ? ORDER BY c.id", (oth["id"] if oth else -1,)).fetchall():
                card = orders.get_card(conn, card_id)
                plan = return_plan(conn, card_id)
                if not (store_value(conn, _ask_key(card)).startswith("here|") or (plan is not None and not plan["signed_at"])):
                    continue
                if card["sale_ids"]:                                                  # move_card refuses an open stage with a sale
                    done["skipped"] += 1
                    continue
                orders.move_card(conn, card_id, given["id"], now, "система", fire_rules=False, journal=False, commit=False)
                orders.add_event(conn, card_id, now, "система", "field", MIGRATED + "заказ на «%s» — %s." % (given["name"], label_of(plan)),
                                 level="grey")
                done["to_return_given"] += 1
        if any(done.values()):                                                        # a new base: nothing to say
            _audit(conn, now, "system", "return_flow_migrated", done)
        conn.execute("INSERT INTO app_state (key, value) VALUES (?, ?)", (FLOW_KEY, now))
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    return done
