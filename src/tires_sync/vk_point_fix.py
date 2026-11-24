"""The client's name or phone of an order changed after the panel told the point's manager in VK (the owner 2026-11-24): the message the panel
sent is corrected in VK (messages.edit) and the point gets a short note of the bot (an edit in VK may come without a notice). When the
message cannot be corrected (too old for VK, no number of it — an order of before 2026-11-24 —, the old value not in its text, VK did not
answer), only the note goes. Only the manager of the order's point in VK, only with the key of the community; nothing goes to a client.
Never raises: the card is saved before and stays saved whatever VK says."""
from __future__ import annotations

import logging
import re
import sqlite3
from typing import Callable, List, Optional

from . import connections, order_points as points, orders, vk_send

log = logging.getLogger(__name__)
AUTHOR = "оповещение"
TASK = "task"                     # «Менеджеру: заказ оформлен» (order_engine.send_point_task)
OTH_HOLD = "oth_hold"             # «Точке: шины с ОТХ отложены под клиента» (order_notify.tell_point)
NOTE = "По заказу №%s изменены данные покупателя: имя %s, телефон %s"
SENT_TO_POINT = "отправлено точке в ВК"          # order_engine.SENT_TO_POINT: the result of a sent task (an order of before the numbers)


def _same_name(a, b) -> bool:
    return " ".join(str(a or "").split()).casefold() == " ".join(str(b or "").split()).casefold()


def _same_phone(a, b) -> bool:
    return connections.format_phone(str(a or "").strip()) == connections.format_phone(str(b or "").strip())


def _has(text: str, value: str) -> bool:
    value = str(value or "").strip()
    return bool(value) and re.search(r"(?<!\w)%s(?!\w)" % re.escape(value), text) is not None


def _replace(text: str, old: str, new: str) -> str:
    return re.sub(r"(?<!\w)%s(?!\w)" % re.escape(old.strip()), lambda _m: new, text)


def remember(conn, order_id, peer_id, message_id, text: str, kind: str, now: str) -> None:
    """Keep the number of a message just sent to a point in VK, with the client's name and phone it carries, in the caller's transaction
    (its commit saves it). Only a message that names the client; never fails the sending (the message has gone)."""
    if not isinstance(message_id, int) or isinstance(message_id, bool) or message_id <= 0:
        return
    try:
        row = conn.execute("SELECT client_name, client_phone FROM order_card WHERE id = ?", (order_id,)).fetchone()
        if row is None or not (_has(text, row[0]) or _has(text, row[1])):
            return
        conn.execute("SAVEPOINT vk_point_message")
        try:
            conn.execute("INSERT INTO vk_point_message (order_id, peer_id, message_id, kind, text, client_name, client_phone, sent_at) "
                         "VALUES (?,?,?,?,?,?,?,?)", (order_id, str(peer_id), message_id, kind, text, row[0] or "", row[1] or "", now))
            conn.execute("RELEASE vk_point_message")
        except sqlite3.Error:
            conn.execute("ROLLBACK TO vk_point_message")
            conn.execute("RELEASE vk_point_message")
            raise
    except Exception:                                              # noqa: BLE001  the message has gone: only its number is lost
        log.exception("the number of the VK message of order %s was not kept", order_id)


def client_changed(conn, card_id, before: dict, now: str, author: str, send: Optional[Callable], edit: Optional[Callable]) -> str:
    """After the save of the card (committed): the client's name or phone of order `card_id` is no longer `before` ({client_name, client_phone}).
    `send` / `edit`: vk_send.sender_for / editor_for (None without the key). Returns what was done ("" when nothing); never raises."""
    try:
        return _changed(conn, card_id, before, now, author, send, edit)
    except Exception:                                              # noqa: BLE001  never the save's failure
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        log.exception("the VK message of the point of order %s was not corrected", card_id)
        return ""


def _changed(conn, card_id, before: dict, now: str, author: str, send, edit) -> str:
    card = orders.get_card(conn, card_id)
    point = card.get("point")
    peer = str((point or {}).get("vk_peer_id") or "").strip()
    if not point or not peer or points.in_panel(point) or send is None or edit is None:
        return ""                                                  # no VK of the point or no key: quietly nothing
    name, phone = card["client_name"] or "", card["client_phone"] or ""
    rows = [{"id": r[0], "message_id": r[1], "text": r[2], "name": r[3], "phone": r[4]} for r in conn.execute(
        "SELECT id, message_id, text, client_name, client_phone FROM vk_point_message WHERE order_id = ? AND peer_id = ? ORDER BY id",
        (card_id, peer))]
    if not rows:
        if conn.execute("SELECT 1 FROM vk_point_message WHERE order_id = ? LIMIT 1", (card_id,)).fetchone():
            return ""                                              # told another point (the point changed): not this one's message
        return _legacy(conn, card, peer, before, now, author, send)
    edited, why = 0, ""
    for row in rows:
        if _same_name(row["name"], name) and _same_phone(row["phone"], phone):
            continue
        text, missing = row["text"], False
        for old, new, same in ((row["name"], name, _same_name), (row["phone"], phone, _same_phone)):
            if same(old, new):
                continue
            if not _has(text, old):
                missing = True
                break
            text = _replace(text, old, new)
        if missing:
            why = why or "старое сообщение не найдено"
            continue
        if len(text.strip()) > vk_send.VK_LIMIT:
            why = why or "текст длиннее лимита ВК"
            continue
        try:
            edit(peer, row["message_id"], text)
        except vk_send.VkError as err:
            if err.code in vk_send.TOO_OLD:
                why = why or "срок правки вышел"
            elif str(err).startswith("ВК недоступен"):
                why = why or "ВК не ответил на правку"
            else:
                why = why or "ВК не принял правку"
            continue
        conn.execute("UPDATE vk_point_message SET text = ?, client_name = ?, client_phone = ?, edited_at = ? WHERE id = ?",
                     (text, name, phone, now, row["id"]))
        conn.commit()
        edited += 1
    if not edited and not why:
        return ""                                                  # the same name and phone, only written otherwise: nothing to VK
    return _note(conn, card, peer, now, author, send, edited, why)


def _legacy(conn, card: dict, peer: str, before: dict, now: str, author: str, send) -> str:
    """An order without a kept number: the note only, when a sent message of the order named the old name or phone."""
    old_name, old_phone = str(before.get("client_name") or ""), str(before.get("client_phone") or "")
    if _same_name(old_name, card["client_name"]) and _same_phone(old_phone, card["client_phone"]):
        return ""
    bodies: List[str] = [r[0] or "" for r in conn.execute(
        "SELECT body FROM order_task WHERE order_id = ? AND kind = 'message' AND status = 'done' AND result LIKE ?",
        (card["id"], "%" + SENT_TO_POINT + "%"))]
    if not any(_has(b, old_name) or _has(b, old_phone) for b in bodies):
        return ""                                                  # the point was never told the client by the panel
    return _note(conn, card, peer, now, author, send, 0, "номера сообщения нет")


def _note(conn, card: dict, peer: str, now: str, author: str, send, edited: int, why: str) -> str:
    from .order_manager import label, signed
    name, phone = card["client_name"] or "", card["client_phone"] or ""
    body = signed(NOTE % (label(card), name or "(пусто)", phone or "(пусто)"))
    point_name = card["point"]["name"]
    try:
        send(peer, body, random_id=vk_send.stable_id("client-fix|%s|%s|%s|%s" % (card["id"], peer, body, now)))
        sent, failure = True, ""
    except vk_send.VkError as err:
        sent, failure = False, "ВК недоступен" if str(err).startswith("ВК недоступен") else "ВК не принял сообщение"
    if edited:
        text = "Сообщение точке «%s» в ВК исправлено: новые имя и телефон покупателя" % point_name
        text += "; точке отправлена заметка об изменении" if sent else "; заметка точке не отправлена (%s)" % failure
        level = "grey"                                             # the point's message itself is right: no alert
        if why:
            text += "; не все сообщения исправлены (%s)" % why
    elif sent:
        text, level = "Точке «%s» отправлено новое сообщение об изменении покупателя: %s" % (point_name, why), "grey"
    else:
        text, level = "Точке «%s» в ВК не отправлено об изменении покупателя (%s): %s" % (point_name, why, failure), "red"
    orders.add_event(conn, card["id"], now, author or AUTHOR, "notify", text, level=level)
    conn.commit()
    return text
