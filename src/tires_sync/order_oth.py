"""«На ОТХ»: the goods of an order the client refused are ours, kept in stock (the owner 2026-10-23).

Entering the stage unlinks the client: the link of the order (client_order) is put aside in order_oth_client, so the order is no longer the
client's (not in the card, not a purchase, never «заказал»), and the automatic joining never links it again (clients._detached). Leaving the
stage for an open one (the client came back) gives the link back; leaving it for a closed one («Выполнено», «Отмена», «Возврат», a refusal)
keeps the order unlinked. The positions of the orders standing in «На ОТХ» are our stock: the page «На ОТХ» of «Товар и фид» and a mark in
the catalog. «Вернуть поставщику» is a mark of ours (oth_return): nothing is written to WebMIM, the stage of the order does not change. The
feed is never touched: these goods are put on Avito by hand (the owner)."""
from __future__ import annotations

import json
from typing import Dict, List, Optional

from . import orders


class OthError(ValueError):
    pass


def _audit(conn, now: str, author: str, action: str, detail: dict) -> None:
    """A record of the journal of actions saved with the caller's change (store.write_audit commits on its own)."""
    conn.execute("INSERT INTO audit (at, author, action, detail) VALUES (?,?,?,?)", (now, author, action, json.dumps(detail, ensure_ascii=False)))


def detach(conn, card: dict, now: str, author: str) -> Optional[int]:
    """The order has entered «На ОТХ»: its client (if any) is unlinked and kept aside; the marks «возвращено» of an earlier stay are dropped.
    Returns the client unlinked. No commit: saved with the move."""
    conn.execute("DELETE FROM oth_return WHERE order_id = ?", (card["id"],))
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
    or after a closed stage set by mistake (no other way to link it again); a closed stage keeps it unlinked."""
    if stage["code"] != "oth" and stage["kind"] == "open":
        reattach(conn, card["id"], now, author)


_STOCK_SQL = ("SELECT i.id, i.order_id, i.goods_id, i.code, i.name, i.qty, c.point_id, COALESCE(p.name, ''), c.entered_at, c.reserve_until, "
              "r.at, r.author, COALESCE(r.qty, 0) FROM order_item i JOIN order_card c ON c.id = i.order_id JOIN order_stage s ON s.id = c.stage_id "
              "LEFT JOIN point p ON p.id = c.point_id LEFT JOIN oth_return r ON r.item_id = i.id WHERE s.code = 'oth' AND i.qty >= 1 ")
_STOCK_KEYS = ("item_id", "order_id", "goods_id", "code", "name", "ordered", "point_id", "point", "since", "until", "returned_at", "returned_by",
               "returned_qty")
MAX_PIECES = 10000


def _with_left(row: dict) -> dict:
    """`ordered`: the pieces of the position in the order; `returned_qty`: marked «возвращено поставщику» so far (a return may be a part,
    the owner 2026-10-24); `left`: still ours, never below zero (the quantity of the position may be lowered after a return)."""
    row["left"] = max(int(row["ordered"]) - int(row["returned_qty"]), 0)
    return row


def stock(conn) -> Dict[str, List[dict]]:
    """The positions of the orders standing in «На ОТХ», oldest first: {"stock": the pieces still ours, "returned": the pieces marked
    «возвращено поставщику»}. A position returned in part is in both; `qty` is the pieces of that list (left there, returned here).
    No price: the purchase price needs its own right, and the page is about where the goods are."""
    rows = [_with_left(dict(zip(_STOCK_KEYS, r))) for r in conn.execute(_STOCK_SQL + "ORDER BY c.entered_at, i.order_id, i.id")]
    return {"stock": [dict(r, qty=r["left"]) for r in rows if r["left"] > 0],
            "returned": [dict(r, qty=r["returned_qty"]) for r in rows if r["returned_qty"] > 0]}


def goods_on_oth(conn) -> Dict[str, int]:
    """goods_id → how many pieces of it are on «На ОТХ» and not returned: the mark of the catalog."""
    found: Dict[str, int] = {}
    for row in stock(conn)["stock"]:
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


def mark_returned(conn, item_id, now: str, author: str, qty=None) -> None:
    """«Вернуть поставщику»: `qty` pieces of the position (all that are left when None) are marked as gone back to the supplier, never more
    than the order had and never more than are not returned yet (the owner 2026-10-24). A mark of ours: nothing is sent to WebMIM."""
    item = _stock_item(conn, item_id)
    if item["left"] <= 0:
        raise OthError("Позиция уже вся возвращена поставщику.")
    pieces = item["left"] if qty is None else _pieces(qty)
    if pieces > item["left"]:
        raise OthError("Можно вернуть не больше %d шт.: в заказе %d шт., уже возвращено %d шт." % (item["left"], item["ordered"], item["returned_qty"]))
    # the limit is in the statement itself: a mark made a moment ago in another tab never lets the total pass the pieces of the order
    if conn.execute("INSERT INTO oth_return (item_id, order_id, qty, at, author) SELECT i.id, i.order_id, ?, ?, ? FROM order_item i "
                    "WHERE i.id = ? AND i.qty >= ? ON CONFLICT(item_id) DO UPDATE SET qty = oth_return.qty + excluded.qty, at = excluded.at, "
                    "author = excluded.author WHERE oth_return.qty + excluded.qty <= (SELECT qty FROM order_item WHERE id = excluded.item_id)",
                    (pieces, now, author, item["item_id"], pieces)).rowcount != 1:
        conn.rollback()
        raise OthError("Столько уже не вернуть: позицию только что отметили в другой вкладке. Обновите страницу.")
    total = int(item["returned_qty"]) + pieces
    whole = not item["returned_qty"] and total == int(item["ordered"])
    orders.add_event(conn, item["order_id"], now, author, "field", "ОТХ: возвращено поставщику — %s, %d шт.%s" % (
        item["name"], pieces, "" if whole else " (всего возвращено %d из %d)" % (total, item["ordered"])))
    _audit(conn, now, author, "oth_return", {"order": item["order_id"], "item": item["item_id"], "qty": pieces, "returned": total})
    conn.commit()


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
