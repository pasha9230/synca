"""The data of an order the panel told the point's manager in VK changed (the owner 2026-11-24: the client's name or phone; 2026-11-25: the
positions, their pieces and prices, the dates, the numbers — every value of the template the message was made from): the message the panel
sent is corrected in VK (messages.edit) and the point gets a short note of the bot (an edit in VK may come without a notice).
A message kept with its template (from 2026-11-25) is built again by the same template with the order's values as they are now; an older one
gets only the client's name and phone put in its lines «Покупатель»/«Телефон». When the message cannot be corrected (too old for VK, no number
of it — an order of before 2026-11-24 —, a text written by hand, VK did not answer), only the note «По заказу №… изменено: …» goes.
Only the manager of the order's point in VK, only with the key of the community; nothing goes to a client. A new point of the order: the old
message stays as it was. Never raises: the card is saved before and stays saved whatever VK says."""
from __future__ import annotations

import functools
import inspect
import json
import logging
import re
import sqlite3
from typing import Callable, List, Optional

from . import connections, order_points as points, order_templates as templates, orders, vk_send

log = logging.getLogger(__name__)
AUTHOR = "оповещение"
TASK = "task"                     # «Менеджеру: заказ оформлен» (order_engine.send_point_task)
OTH_HOLD = "oth_hold"             # «Точке: шины с ОТХ отложены под клиента» (order_notify.tell_point)
NOTE = "По заказу №%s изменены данные покупателя: имя %s, телефон %s"
CHANGED = "По заказу №%s изменено: %s"
CLIENT = ("client_name", "client_phone")
ITEMS = ("items", "items_full", "items_lines", "total", "oth_source", "oth_orders")     # one «позиции» for the point
# what a message kept without its template may carry (the owner's samples): a change of it goes to the point as the note only
PLAIN = ("items_full", "delivery_date", "delivery_time", "hold_until", "webmim_number", "transfer_number")
WRAPS = ("", "bot", "bot_point")                  # how the template's text went: as it is, «(бот) …» (order_placed), «(бот) …» for a point (deliver)
SENT_TO_POINT = "отправлено точке в ВК"          # order_engine.SENT_TO_POINT: the result of a sent task (an order of before the numbers)
BOT_MARK = "(бот)"                                # order_manager.BOT_MARK, stripped
SHORT_TIMEOUT = 6                                 # seconds: VK is asked inside the save of the card (two workers of the panel)
DOWN = "ВК не ответил на правку"


def _same_name(a, b) -> bool:
    return " ".join(str(a or "").split()).casefold() == " ".join(str(b or "").split()).casefold()


def _same_phone(a, b) -> bool:
    return connections.format_phone(str(a or "").strip()) == connections.format_phone(str(b or "").strip())


def _has(text: str, value: str) -> bool:
    value = str(value or "").strip()
    return bool(value) and re.search(r"(?<!\w)%s(?!\w)" % re.escape(value), text) is not None


NAME_LINES = ("Покупатель",)                    # the lines of the owner's templates that carry the client (order_templates)
PHONE_LINES = ("Покупатель", "Телефон")


def _put(text: str, old: str, new: str, labels) -> Optional[str]:
    """`text` with the first whole-word `old` of the first line that begins with one of `labels` («Покупатель …», «Телефон: …») replaced by
    `new`; None when no such line has it (another word of the text is never touched)."""
    old = str(old or "").strip()
    if not old:
        return None
    pattern = re.compile(r"(?<!\w)%s(?!\w)" % re.escape(old))
    lines = text.split("\n")
    for i, line in enumerate(lines):
        head = line.strip()
        if head.startswith(BOT_MARK):
            head = head[len(BOT_MARK):].lstrip()
        if head.startswith(labels) and pattern.search(line):
            lines[i] = pattern.sub(lambda _m: new, line, count=1)
            return "\n".join(lines)
    return None


def short_http(http):
    """The transport with the short timeout (SHORT_TIMEOUT) when it takes one (transport.urllib_transport does); else as it is."""
    try:
        takes = "timeout" in inspect.signature(http).parameters
    except (TypeError, ValueError):
        takes = False
    return functools.partial(http, timeout=SHORT_TIMEOUT) if takes else http


def _wrap(how: str, text: str) -> str:
    from .order_manager import signed
    from .order_notify import for_point
    if how == "bot":
        return signed(text)
    if how == "bot_point":
        return signed(for_point(text))
    return text


def _used(template: str, data: dict) -> dict:
    """The values of the variables the template has ({key: text}), in the order of the text."""
    return {k: str(data.get(k) or "") for k in dict.fromkeys(templates.PLACEHOLDER.findall(template)) if k in templates.VARIABLES}


def _source(conn, card: dict, text: str, kind: str, now: str, template):
    """(template, values as JSON, wrap) of a message just sent when its text is exactly the template's with the order's values (a text the
    person changed in the window has none: it is never rebuilt); (None, None, None) else."""
    if callable(template):
        template = template(card)
    if template is None and kind == OTH_HOLD:
        from .oth_sale import point_template
        template = point_template(conn)
    if not template:
        return None, None, None
    used = _used(template, templates.values(conn, card, now))
    body = templates.fill(template, used)[0]
    for how in (("bot_point",) if kind == OTH_HOLD else ("", "bot")):     # deliver writes a point's alert; send_point_task a task
        if _wrap(how, body).strip() == text.strip():
            return template, json.dumps(used, ensure_ascii=False), how
    return None, None, None


def remember(conn, order_id, peer_id, message_id, text: str, kind: str, now: str, template=None) -> None:
    """Keep the number of a message just sent to a point in VK, with the client's name and phone it carries and the template it was made from
    (`template`: its text, or a function of the card that gives it; OTH_HOLD: oth_sale.point_template), in the caller's transaction (its
    commit saves it). Only a message of the template or one that names the client; never fails the sending (the message has gone)."""
    if not isinstance(message_id, int) or isinstance(message_id, bool) or message_id <= 0:
        return
    try:
        row = conn.execute("SELECT client_name, client_phone FROM order_card WHERE id = ?", (order_id,)).fetchone()
        if row is None:
            return
        try:
            source = _source(conn, orders.get_card(conn, order_id), text, kind, now, template)
        except Exception:                                          # noqa: BLE001  kept as a message without its template
            log.exception("the template of the VK message of order %s", order_id)
            source = (None, None, None)
        if not (source[0] or _has(text, row[0]) or _has(text, row[1])):
            return
        conn.execute("SAVEPOINT vk_point_message")
        try:
            conn.execute("INSERT INTO vk_point_message (order_id, peer_id, message_id, kind, text, client_name, client_phone, sent_at, template, "
                         "vals, wrap) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (order_id, str(peer_id), message_id, kind, text, row[0] or "", row[1] or "", now) + tuple(source))
            conn.execute("RELEASE vk_point_message")
        except sqlite3.Error:
            conn.execute("ROLLBACK TO vk_point_message")
            conn.execute("RELEASE vk_point_message")
            raise
    except Exception:                                              # noqa: BLE001  the message has gone: only its number is lost
        log.exception("the number of the VK message of order %s was not kept", order_id)


def _told(point) -> str:
    """The VK of the order's point the panel writes to; '' for none (no point, no VK, a point working in the panel)."""
    peer = str((point or {}).get("vk_peer_id") or "").strip()
    return "" if not point or not peer or points.in_panel(point) else peer


def snapshot(conn, card_id, now: str) -> dict:
    """The values the point's messages are made from (order_templates.values) and the order's point, taken before a change of the order
    (order_changed compares them); {} when the point has no VK or the panel never told it of this order. Never raises."""
    try:
        card = orders.get_card(conn, card_id)
        if not _told(card.get("point")):
            return {}
        told = conn.execute("SELECT 1 FROM vk_point_message WHERE order_id = ? LIMIT 1", (card_id,)).fetchone() or conn.execute(
            "SELECT 1 FROM order_task WHERE order_id = ? AND kind = 'message' AND status = 'done' AND result LIKE ? LIMIT 1",
            (card_id, "%" + SENT_TO_POINT + "%")).fetchone()
        if not told:
            return {}
        return dict(templates.values(conn, card, now), _point_id=card["point"]["id"])
    except Exception:                                              # noqa: BLE001  nothing is corrected, the save goes on
        log.exception("the values of order %s for the point's VK", card_id)
        return {}


def order_changed(conn, card_id, before: dict, now: str, author: str, send: Optional[Callable], edit: Optional[Callable]) -> str:
    """After the save of the card (committed): the order `card_id` was `before` (snapshot; or only {client_name, client_phone}). Its messages
    to the point in VK are made right. `send` / `edit`: vk_send.sender_for / editor_for (None without the key). Returns what was done ("" when
    nothing); never raises."""
    try:
        return _changed(conn, card_id, before or {}, now, author, send, edit)
    except Exception:                                              # noqa: BLE001  never the save's failure
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        log.exception("the VK message of the point of order %s was not corrected", card_id)
        return ""


client_changed = order_changed                                     # the name of 2026-11-24


def _plain(before: dict, after: dict) -> List[str]:
    """The values of PLAIN that changed (both known and not empty: a value the point was never told is no change to it)."""
    return [k for k in PLAIN if str(before.get(k) or "").strip() and str(after.get(k) or "").strip() and before[k] != after[k]]


def _client_keys(old_name, old_phone, card: dict) -> List[str]:
    return ([] if _same_name(old_name, card["client_name"]) else ["client_name"]) + \
           ([] if _same_phone(old_phone, card["client_phone"]) else ["client_phone"])


def _add(keys: List[str], more) -> None:
    for k in more:
        if k not in keys:
            keys.append(k)


def _merge(old: dict, fresh: dict) -> dict:
    """The values a message is built again with: the new ones; the old where the order has none now (a line is never emptied: «На ОТХ» has no
    «резерв до») or the client's name or phone is only written otherwise."""
    out = {}
    for k, v in old.items():
        new = str(fresh.get(k) or "")
        if not new.strip() or (k == "client_name" and _same_name(v, new)) or (k == "client_phone" and _same_phone(v, new)):
            out[k] = v
        else:
            out[k] = new
    return out


def _try(edit, peer, message_id, text: str) -> str:
    """'' when the message is corrected, DOWN when VK did not answer, else why not."""
    if len(text.strip()) > vk_send.VK_LIMIT:
        return "текст длиннее лимита ВК"
    try:
        edit(peer, message_id, text)
    except vk_send.VkError as err:
        if str(err).startswith("ВК недоступен"):                   # VK hangs: no more tries in this save (the panel's workers wait)
            return DOWN
        return "срок правки вышел" if err.code in vk_send.TOO_OLD else "ВК не принял правку"
    return ""


def _changed(conn, card_id, before: dict, now: str, author: str, send, edit) -> str:
    card = orders.get_card(conn, card_id)
    point = card.get("point")
    peer = _told(point)
    if not peer or send is None or edit is None:
        return ""                                                  # no VK of the point or no key: quietly nothing
    if "_point_id" in before and before["_point_id"] != point["id"]:
        return ""                                                  # another point: the old one's message stays, the new one is told as ever
    name, phone = card["client_name"] or "", card["client_phone"] or ""
    after = templates.values(conn, card, now) if any(k in before for k in PLAIN) else {}
    plain = _plain(before, after)
    rows = [{"id": r[0], "message_id": r[1], "text": r[2], "name": r[3], "phone": r[4], "template": r[5], "vals": r[6], "wrap": r[7] or "",
             "sent_at": r[8]} for r in conn.execute(
        "SELECT id, message_id, text, client_name, client_phone, template, vals, wrap, sent_at FROM vk_point_message "
        "WHERE order_id = ? AND peer_id = ? ORDER BY id", (card_id, peer))]
    if not rows:
        others = [{"name": r[0], "phone": r[1]} for r in conn.execute(
            "SELECT client_name, client_phone FROM vk_point_message WHERE order_id = ?", (card_id,))]
        if others:                                                 # told another manager (the point's VK changed): not this one's message
            if not plain and all(_same_name(o["name"], name) and _same_phone(o["phone"], phone) for o in others):
                return ""
            return _event(conn, card, now, author, "Менеджер точки «%s» в ВК изменился: сообщение прежнему менеджеру не исправлено, "
                                                   "новому не отправлено" % point["name"], "grey")
        return _legacy(conn, card, peer, before, plain, now, author, send)
    edited, why, keys = 0, "", []
    for row in rows:
        built = _rebuilt(conn, card, row)
        if built is not None:                                      # a message of its template: built again with the values of now
            text, merged, changed = built
            if not changed:
                continue
            _add(keys, changed)
            failure = _try(edit, peer, row["message_id"], text)
            if failure == DOWN:
                return _down(conn, card, now, author, edited, keys)
            if failure:
                why = why or failure
                continue
            conn.execute("UPDATE vk_point_message SET text = ?, vals = ?, client_name = ?, client_phone = ?, edited_at = ? WHERE id = ?",
                         (text, json.dumps(merged, ensure_ascii=False), name, phone, now, row["id"]))
            conn.commit()
            edited += 1
            continue
        if plain:                                                  # an old message: its other values cannot be put in, the note says them
            _add(keys, plain)
            why = why or "старое сообщение не пересобрать"
        client = _client_keys(row["name"], row["phone"], card)
        if not client:
            continue
        _add(keys, client)
        text = row["text"]
        for old, new, same, labels in ((row["name"], name, _same_name, NAME_LINES), (row["phone"], phone, _same_phone, PHONE_LINES)):
            if text is not None and not same(old, new):
                text = _put(text, old, new, labels)
        if text is None:
            why = why or "старое сообщение не найдено"
            continue
        failure = _try(edit, peer, row["message_id"], text)
        if failure == DOWN:
            return _down(conn, card, now, author, edited, keys)
        if failure:
            why = why or failure
            continue
        conn.execute("UPDATE vk_point_message SET text = ?, client_name = ?, client_phone = ?, edited_at = ? WHERE id = ?",
                     (text, name, phone, now, row["id"]))
        conn.commit()
        edited += 1
    if not edited and not why:
        return ""                                                  # nothing the point was told changed (or only written otherwise)
    return _note(conn, card, peer, now, author, send, edited, why, keys)


def _rebuilt(conn, card: dict, row: dict):
    """(text, values, the keys changed) of a message kept with its template, built again with the order's values of now; None when it has no
    template or its text in VK is no longer the template's (an old correction of the client's lines)."""
    if not row["template"] or not row["vals"]:
        return None
    try:
        old = json.loads(row["vals"])
    except ValueError:
        return None
    if not isinstance(old, dict) or _wrap(row["wrap"], templates.fill(row["template"], old)[0]).strip() != str(row["text"] or "").strip():
        return None
    merged = _merge(old, templates.values(conn, card, row["sent_at"]))     # its own time: «дата оформления» of an order never placed stays
    changed = [k for k in old if merged[k] != old[k]]
    return _wrap(row["wrap"], templates.fill(row["template"], merged)[0]).strip(), merged, changed


def _client_only(keys) -> bool:
    return bool(keys) and all(k in CLIENT for k in keys)


def _subject(keys) -> str:
    return "покупателя" if not keys or _client_only(keys) else "заказа"


def _lower(label: str) -> str:
    return label[:1].lower() + label[1:]


def _what(keys) -> str:
    """What changed, for the journal (never the client's name or phone themselves)."""
    words: List[str] = []
    for k in keys:
        word = "имя и телефон покупателя" if k in CLIENT else "позиции" if k in ITEMS else _lower(templates.VARIABLES.get(k, k))
        if word not in words:
            words.append(word)
    return ", ".join(words)


def _parts(keys, data: dict, card: dict) -> str:
    """What changed with the new values, for the point's note."""
    parts: List[str] = []
    for k in keys:
        if k == "client_name":
            part = "имя %s" % (card["client_name"] or "(пусто)")
        elif k == "client_phone":
            part = "телефон %s" % (card["client_phone"] or "(пусто)")
        elif k in ITEMS:
            part = "позиции: %s" % data["items"] if data.get("items") else "позиции"
        else:
            part = "%s %s" % (_lower(templates.VARIABLES.get(k, k)), data.get(k) or "(пусто)")
        if part not in parts:
            parts.append(part)
    return "; ".join(parts)


def _event(conn, card: dict, now: str, author: str, text: str, level: str) -> str:
    orders.add_event(conn, card["id"], now, author or AUTHOR, "notify", text, level=level)
    conn.commit()
    return text


def _down(conn, card: dict, now: str, author: str, edited: int, keys=()) -> str:
    """VK did not answer: one red record, nothing more is sent in this save."""
    done = " (до этого исправлено сообщений: %d)" % edited if edited else ""
    return _event(conn, card, now, author, "Точке «%s» в ВК не отправлено об изменении %s: ВК недоступен%s"
                  % (card["point"]["name"], _subject(keys), done), "red")


def _legacy(conn, card: dict, peer: str, before: dict, plain: List[str], now: str, author: str, send) -> str:
    """An order without a kept number: the note only, when the order is open and a sent message of it named the old name or phone (a new
    client) or the point was told the order at all (its other values)."""
    old_name, old_phone = str(before.get("client_name") or ""), str(before.get("client_phone") or "")
    client = _client_keys(old_name, old_phone, card) if ("client_name" in before or "client_phone" in before) else []
    if not client and not plain:
        return ""
    if (card.get("stage") or {}).get("kind") != "open":
        return ""                                                  # a closed or cancelled order: the point is not bothered
    bodies: List[str] = [r[0] or "" for r in conn.execute(
        "SELECT body FROM order_task WHERE order_id = ? AND kind = 'message' AND status = 'done' AND result LIKE ?",
        (card["id"], "%" + SENT_TO_POINT + "%"))]
    if client and not any(_has(b, old_name) or _has(b, old_phone) for b in bodies):
        client = []                                                # the point was never told the client by the panel
    keys = client + (plain if bodies else [])
    if not keys:
        return ""
    return _note(conn, card, peer, now, author, send, 0, "номера сообщения нет", keys)


def _note(conn, card: dict, peer: str, now: str, author: str, send, edited: int, why: str, keys=CLIENT) -> str:
    from .order_manager import label, signed
    from .order_notify import for_point
    name, phone = card["client_name"] or "", card["client_phone"] or ""
    if _client_only(keys):
        body = signed(NOTE % (label(card), name or "(пусто)", phone or "(пусто)"))
    else:                                                          # no WebMIM to a point (order_notify.for_point)
        body = signed(for_point(CHANGED % (label(card), _parts(keys, templates.values(conn, card, now), card))))
    point_name, subject = card["point"]["name"], _subject(keys)
    try:
        send(peer, body, random_id=vk_send.stable_id("client-fix|%s|%s|%s|%s" % (card["id"], peer, body, now)))
        sent, failure = True, ""
    except vk_send.VkError as err:
        if str(err).startswith("ВК недоступен"):
            return _down(conn, card, now, author, edited, keys)
        sent, failure = False, "ВК не принял сообщение"
    if edited:
        text = "Сообщение точке «%s» в ВК исправлено: %s" % (
            point_name, "новые имя и телефон покупателя" if _client_only(keys) else "изменились " + _what(keys))
        text += "; точке отправлена заметка об изменении" if sent else "; заметка точке не отправлена (%s)" % failure
        level = "grey"                                             # the point's message itself is right: no alert
        if why:
            text += "; не все сообщения исправлены (%s)" % why
    elif sent:
        text, level = "Точке «%s» отправлено новое сообщение об изменении %s: %s" % (point_name, subject, why), "grey"
    else:
        text, level = "Точке «%s» в ВК не отправлено об изменении %s (%s): %s" % (point_name, subject, why, failure), "red"
    return _event(conn, card, now, author, text, level)
