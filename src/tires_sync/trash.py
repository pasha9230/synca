"""«Удалённые»: an order or a chat deleted in the panel leaves our base whole (with everything that hangs on it) and is kept here, so it can be
put back. Only our base changes: nothing is written to Avito or WebMIM. A deleted chat stays deleted: the reading of Avito skips it."""
from __future__ import annotations

import json
from typing import List, Optional

from . import orders
from .orders import OrderError

# what hangs on an order: (table, the column that points at the order)
ORDER_TABLES = (("order_item", "order_id"), ("order_event", "order_id"), ("order_field_value", "order_id"), ("order_task", "order_id"),
                ("order_want", "order_id"), ("order_oth_client", "order_id"), ("oth_return", "order_id"))   # «На ОТХ»: the client put aside, the
                                                                                                          # marks «возвращено» (хвост З16)
KINDS = {"order": "Заказ", "chat": "Чат"}


def _rows(conn, sql: str, args=()) -> List[dict]:
    cur = conn.execute(sql, args)
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def _insert(conn, table: str, row: dict) -> None:
    """Back with the same id. A column added after the deletion takes its default; one dropped since is left out."""
    columns = [c for c in (r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)) if c in row]
    conn.execute("INSERT INTO %s (%s) VALUES (%s)" % (table, ", ".join(columns), ", ".join("?" for _ in columns)), [row[c] for c in columns])


def _begin(conn) -> None:
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")                     # the reading of chats runs in another process: no one writes between our read and our delete


def _audit(conn, now: str, author: str, action: str, detail: dict) -> None:
    conn.execute("INSERT INTO audit (at, author, action, detail) VALUES (?,?,?,?)", (now, author, action, json.dumps(detail, ensure_ascii=False)))


def _record(conn, record_id) -> dict:
    rows = _rows(conn, "SELECT * FROM deleted_record WHERE id = ?", (record_id,))
    if not rows:
        raise OrderError("Такой удалённой записи нет.")
    return rows[0]


def order_title(card: dict) -> str:
    return "Заказ №%d%s" % (card["id"], " · " + (card["client_name"] or card["client_phone"]) if (card["client_name"] or card["client_phone"]) else "")


def chat_title(chat: dict) -> str:
    return "Чат Ч-%d · %s%s" % (chat["id"], chat["peer_name"] or "без имени", " · " + chat["item_title"] if chat["item_title"] else "")


def delete_order(conn, card_id, now: str, author: str, only_if=None) -> int:
    """The order with its positions, history, tasks and wait list goes to «Удалённые». Its chats stay and lose the link
    (they are not linked again on their own). An order with a recorded sale is not deleted: «Продажи» must keep matching the orders.
    `only_if(conn, card)` is asked inside the transaction, so nobody changes the card between the check and the delete; a reason it gives stops it."""
    _begin(conn)
    try:
        card = _rows(conn, "SELECT * FROM order_card WHERE id = ?", (card_id,))
        if not card:
            raise OrderError("Такого заказа нет.")
        card = card[0]
        if json.loads(card["sale_ids"] or "[]"):
            raise OrderError("По заказу записана продажа: сначала сторнируйте её, потом удаляйте заказ.")
        reason = only_if(conn, card) if only_if else None
        if reason:
            raise OrderError(reason)
        data = {"card": card, "chats": _rows(conn, "SELECT id, order_id, unlinked FROM avito_chat WHERE order_id = ?", (card_id,))}
        for table, column in ORDER_TABLES:
            data[table] = _rows(conn, "SELECT * FROM %s WHERE %s = ? ORDER BY rowid" % (table, column), (card_id,))
        title = order_title(card)
        cur = conn.execute("INSERT INTO deleted_record (kind, record_id, title, data, deleted_at, deleted_by) VALUES ('order',?,?,?,?,?)",
                           (card_id, title, json.dumps(data, ensure_ascii=False), now, author))
        for table, column in ORDER_TABLES:
            conn.execute("DELETE FROM %s WHERE %s = ?" % (table, column), (card_id,))
        conn.execute("DELETE FROM order_card WHERE id = ?", (card_id,))
        conn.execute("UPDATE avito_chat SET order_id = NULL, unlinked = 1 WHERE order_id = ?", (card_id,))
        _audit(conn, now, author, "order_delete", {"order": card_id, "record": cur.lastrowid, "chats": [c["id"] for c in data["chats"]]})
        conn.commit()
        return cur.lastrowid
    except BaseException:
        conn.rollback()
        raise


def restore_order(conn, record_id, now: str, author: str) -> int:
    _begin(conn)
    try:
        record = _record(conn, record_id)
        if record["kind"] != "order" or record["restored_at"]:
            raise OrderError("Эта запись уже восстановлена." if record["restored_at"] else "Это не заказ.")
        data = json.loads(record["data"])
        card = dict(data["card"])
        if conn.execute("SELECT 1 FROM order_card WHERE id = ?", (card["id"],)).fetchone():
            raise OrderError("Заказ №%d уже есть." % card["id"])
        notes, moved_to = [], None
        if not conn.execute("SELECT 1 FROM order_stage WHERE id = ?", (card["stage_id"],)).fetchone():
            stage = moved_to = orders._first_stage(conn)                        # its stage was deleted meanwhile: back on the first one
            card["stage_id"], card["entered_at"] = stage["id"], now
            notes.append("этап удалён, заказ на этапе «%s»" % stage["name"])
        if card.get("point_id") and not conn.execute("SELECT 1 FROM point WHERE id = ?", (card["point_id"],)).fetchone():
            card["point_id"] = None
            notes.append("точка удалена")
        linked = orders.CHAT_REF.match(card.get("avito_chat") or "")
        if linked:                                                              # its chat was deleted or went to another deal meanwhile
            owner = conn.execute("SELECT order_id FROM avito_chat WHERE id = ?", (int(linked.group(1)),)).fetchone()
            if owner is None or owner[0] not in (None, card["id"]):
                card["avito_chat"] = ""
                notes.append("чат %s не вернулся к заказу" % linked.group(0))
        _insert(conn, "order_card", card)
        for table, _column in ORDER_TABLES:
            for row in data.get(table, []):
                _insert(conn, table, row)
        relinked = 0
        for chat in data.get("chats", []):                                    # the chat comes back to the order only if nobody took it since
            cur = conn.execute("UPDATE avito_chat SET order_id = ?, unlinked = ? WHERE id = ? AND order_id IS NULL",
                               (card["id"], chat["unlinked"], chat["id"]))
            relinked += cur.rowcount
        if moved_to is not None:                                              # «На ОТХ» was deleted: an open stage gives the client back
            from . import order_oth
            order_oth.leave(conn, card, moved_to, now, author)
        orders.add_event(conn, card["id"], now, author, "restored", "Заказ восстановлен из удалённых" + (" (%s)" % ", ".join(notes) if notes else ""))
        conn.execute("UPDATE deleted_record SET restored_at = ?, restored_by = ? WHERE id = ?", (now, author, record_id))
        _audit(conn, now, author, "order_restore", {"order": card["id"], "record": record_id, "chats": relinked})
        conn.commit()
        return card["id"]
    except BaseException:
        conn.rollback()
        raise


def is_deleted_chat(conn, account_id: int, chat_id: str) -> bool:
    return conn.execute("SELECT 1 FROM deleted_record WHERE kind = 'chat' AND account_id = ? AND chat_id = ? AND restored_at IS NULL LIMIT 1",
                        (account_id, str(chat_id))).fetchone() is not None


def delete_chat(conn, chat_row, now: str, author: str) -> int:
    """The chat and its messages go to «Удалённые». The reading of Avito skips it from now on: new messages in it are not loaded
    (they are read again when it is put back). Its deal stays; the deal's chat field is emptied if it named this chat."""
    _begin(conn)
    try:
        chat = _rows(conn, "SELECT * FROM avito_chat WHERE id = ?", (chat_row,))
        if not chat:
            raise OrderError("Такого чата нет.")
        chat = chat[0]
        if chat["order_id"] == -1:
            raise OrderError("Для этого чата сейчас создаётся сделка: повторите удаление чуть позже.")
        ref = "Ч-%d" % chat["id"]
        cards = [r[0] for r in conn.execute("SELECT id FROM order_card WHERE avito_chat = ?", (ref,))]
        data = {"chat": chat, "messages": _rows(conn, "SELECT * FROM avito_message WHERE chat_row = ? ORDER BY id", (chat_row,)), "cards": cards}
        title = chat_title(chat)
        cur = conn.execute("INSERT INTO deleted_record (kind, record_id, account_id, chat_id, title, data, deleted_at, deleted_by) VALUES ('chat',?,?,?,?,?,?,?)",
                           (chat_row, chat["account_id"], chat["chat_id"], title, json.dumps(data, ensure_ascii=False), now, author))
        for card_id in cards:
            conn.execute("UPDATE order_card SET avito_chat = '', updated_at = ? WHERE id = ?", (now, card_id))
            orders.add_event(conn, card_id, now, author, "chat", "Чат %s удалён" % ref)
        conn.execute("DELETE FROM avito_message WHERE chat_row = ?", (chat_row,))
        conn.execute("DELETE FROM avito_chat WHERE id = ?", (chat_row,))
        _audit(conn, now, author, "chat_delete", {"chat": chat_row, "record": cur.lastrowid, "order": chat["order_id"]})
        conn.commit()
        return cur.lastrowid
    except BaseException:
        conn.rollback()
        raise


def restore_chat(conn, record_id, now: str, author: str) -> int:
    _begin(conn)
    try:
        record = _record(conn, record_id)
        if record["kind"] != "chat" or record["restored_at"]:
            raise OrderError("Эта запись уже восстановлена." if record["restored_at"] else "Это не чат.")
        data = json.loads(record["data"])
        chat = dict(data["chat"])
        if conn.execute("SELECT 1 FROM avito_chat WHERE id = ? OR (account_id = ? AND chat_id = ?)", (chat["id"], chat["account_id"], chat["chat_id"])).fetchone():
            raise OrderError("Чат Ч-%d уже есть." % chat["id"])
        chat["updated_ts"] = 0                                                  # the next reading loads what was written while it was deleted
        if chat.get("order_id") and (chat["order_id"] < 0 or not conn.execute("SELECT 1 FROM order_card WHERE id = ?", (chat["order_id"],)).fetchone()):
            chat["order_id"] = None                                             # its deal is gone (or was being made): no deal
        _insert(conn, "avito_chat", chat)
        for message in data.get("messages", []):
            _insert(conn, "avito_message", message)
        for card_id in data.get("cards", []):
            cur = conn.execute("UPDATE order_card SET avito_chat = ?, updated_at = ? WHERE id = ? AND avito_chat = ''", ("Ч-%d" % chat["id"], now, card_id))
            if cur.rowcount:
                orders.add_event(conn, card_id, now, author, "chat", "Чат Ч-%d восстановлен" % chat["id"])
        conn.execute("UPDATE deleted_record SET restored_at = ?, restored_by = ? WHERE id = ?", (now, author, record_id))
        _audit(conn, now, author, "chat_restore", {"chat": chat["id"], "record": record_id})
        conn.commit()
        return chat["id"]
    except BaseException:
        conn.rollback()
        raise


def list_deleted(conn, kind: Optional[str] = None) -> List[dict]:
    sql, args = "SELECT id, kind, record_id, title, deleted_at, deleted_by FROM deleted_record WHERE restored_at IS NULL", ()
    if kind:
        sql, args = sql + " AND kind = ?", (kind,)
    return _rows(conn, sql + " ORDER BY id DESC", args)
