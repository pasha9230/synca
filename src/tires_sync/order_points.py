"""Points of pickup: who works there, how to reach them, which delivery address the supplier knows."""
from __future__ import annotations

import json
import re
import secrets
import urllib.parse
from datetime import date, timedelta
from typing import List, Optional

PANEL = "panel"                                           # the owner 2026-10-23: the point works in the panel, the bot writes nothing to it
CHANNELS = ("", "vk", "telegram", PANEL)
CHANNEL_NAMES = {"": "не выбран", "vk": "ВК", "telegram": "Telegram", PANEL: "Панель (бот в ВК не пишет)"}
FIELDS = ("id, name, avito_address_id, manager_name, manager_phone, channel, webmim_address_id, default_time, bind_code, vk_peer_id, "
          "address, hours, map_url, avito_account_id")
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"        # no 0, 1, O, I, L


class PointError(ValueError):
    pass


def _row(row) -> dict:
    return dict(zip([f.strip() for f in FIELDS.split(",")], row))


def list_points(conn) -> List[dict]:
    return [_row(r) for r in conn.execute("SELECT %s FROM point ORDER BY name" % FIELDS)]


def get_point(conn, point_id) -> dict:
    row = conn.execute("SELECT %s FROM point WHERE id = ?" % FIELDS, (point_id,)).fetchone()
    if row is None:
        raise PointError("Такой точки нет.")
    return _row(row)


def point_by_account(conn, account_id) -> Optional[dict]:
    """The point whose Avito account it is (an order of its chat is of the point); None for an account of no point or of several (the
    owner 2026-10-22: then the order stays common)."""
    if account_id is None:
        return None
    rows = conn.execute("SELECT %s FROM point WHERE avito_account_id = ? LIMIT 2" % FIELDS, (account_id,)).fetchall()
    return _row(rows[0]) if len(rows) == 1 else None


POINT_ACCOUNTS = "SELECT avito_account_id FROM point WHERE avito_account_id IS NOT NULL GROUP BY avito_account_id HAVING COUNT(*) = 1"
ACCOUNT_ORDERS = ("SELECT s.order_id FROM (SELECT order_id, account_id FROM avito_chat WHERE order_id > 0 UNION ALL "
                  "SELECT order_id, account_id FROM avito_call WHERE order_id > 0) s "
                  "JOIN order_card c ON c.id = s.order_id JOIN point p ON p.id = c.point_id "
                  "LEFT JOIN (" + POINT_ACCOUNTS + ") u ON u.avito_account_id = s.account_id %s "
                  "GROUP BY s.order_id HAVING SUM(u.avito_account_id IS NULL OR s.account_id != p.avito_account_id) = 0 "
                  "AND NOT EXISTS (SELECT 1 FROM order_handoff h WHERE h.order_id = s.order_id)")


def account_orders(conn) -> set:
    """The orders of the Avito accounts of the points (the owner 2026-10-26: their alerts go only to the point): every chat and call of the
    order is of the account of its own point (order_card.point_id; an account of one point only), so the point told is the point whose bell
    shows it. Any other order is as before and never lost for the staff: a chat or a call of an account of no point or of another point, no
    point, no chat nor call, or a client the point handed over to the staff (order_handoff: the point asks them)."""
    return {r[0] for r in conn.execute(ACCOUNT_ORDERS % "")}


def account_chats(conn) -> set:
    """The chats of the Avito accounts of the points."""
    return {r[0] for r in conn.execute("SELECT id FROM avito_chat WHERE account_id IN (" + POINT_ACCOUNTS + ")")}


def of_account(conn, order_id) -> Optional[dict]:
    """The point of an order of a point's Avito account (account_orders); None for any other order."""
    if not order_id or not conn.execute(ACCOUNT_ORDERS % "WHERE s.order_id = ?", (order_id,)).fetchone():
        return None
    return get_point(conn, conn.execute("SELECT point_id FROM order_card WHERE id = ?", (order_id,)).fetchone()[0])


def _single(value, limit: int, label: str) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        raise PointError("%s: не длиннее %d знаков." % (label, limit))
    return text


def _vk_number(value) -> str:
    """The number of the manager in VK: digits, or the address of the page (vk.com/id123, vk.ru/id123)."""
    text = str(value or "").strip()
    found = re.fullmatch(r"(?:https?://)?(?:m\.)?vk\.(?:com|ru)/id(\d{1,15})/?", text, re.I)
    if found:
        text = found.group(1)
    if text and not (text.isascii() and text.isdigit() and text.lstrip("0")):
        raise PointError("ВК менеджера: номер из цифр или адрес страницы вида vk.com/id123.")
    return text


MAP_HOSTS = ("yandex.ru", "yandex.com", "ya.ru")


def _map_url(value) -> str:
    """The point on Yandex Maps: an https link of Yandex, as the client gets it in «можно забирать»."""
    text = str(value or "").strip()
    if not text:
        return ""
    parts = urllib.parse.urlsplit(text)
    host = (parts.hostname or "").lower()
    if (parts.scheme != "https" or len(text) > 300 or any(ch.isspace() for ch in text) or not text.isascii()
            or not any(host == h or host.endswith("." + h) for h in MAP_HOSTS)):
        raise PointError("Ссылка на Яндекс Карты: адрес вида https://yandex.ru/maps/… (кнопка «Поделиться» на карте).")
    return text


def _kept(conn, data: dict, point_id, key: str, clean):
    """A field of the form, or what is stored when the caller does not send it."""
    if key in data:
        return clean(data.get(key))
    return get_point(conn, point_id)[key] if point_id else ''


def _account(conn, value) -> Optional[int]:
    """«Аккаунт Авито точки»: an existing account or nothing (its manager then sees no chat)."""
    text = str(value or "").strip()
    if not text:
        return None
    if not (text.isascii() and text.isdigit()) or conn.execute("SELECT 1 FROM account WHERE id = ?", (int(text),)).fetchone() is None:
        raise PointError("Аккаунт Авито точки выбран неверно.")
    return int(text)


def save_point(conn, data: dict, point_id: Optional[int] = None) -> int:
    name = _single(data.get("name"), 60, "Название точки")
    if not name:
        raise PointError("Название точки обязательно.")
    manager = _single(data.get("manager_name"), 80, "Менеджер")
    phone = _single(data.get("manager_phone"), 30, "Телефон менеджера")
    if phone and not 10 <= len(re.sub(r"\D", "", phone)) <= 15:
        raise PointError("Телефон менеджера: от 10 до 15 цифр.")
    channel = str(data.get("channel") or "")
    if channel not in CHANNELS:
        raise PointError("Канал связи: ВК, Telegram или панель.")
    webmim = _single(data.get("webmim_address_id"), 40, "Адрес доставки WebMIM")
    if webmim and (not webmim.isascii() or " " in webmim):
        raise PointError("Адрес доставки WebMIM: идентификатор без пробелов.")
    default_time = _single(data.get("default_time"), 60, "Время доставки")
    if "avito_address_id" in data:
        avito = _single(data.get("avito_address_id"), 40, "Адрес Авито")
    else:                                                   # the page form does not show it: keep what is stored
        avito = get_point(conn, point_id)["avito_address_id"] if point_id else ''
    if "vk_peer_id" in data:
        vk = _vk_number(data.get("vk_peer_id"))
    else:                                                   # a caller that does not send it keeps what is stored
        vk = get_point(conn, point_id)["vk_peer_id"] if point_id else ''
    address = _kept(conn, data, point_id, "address", lambda v: _single(v, 200, "Адрес точки"))
    hours = _kept(conn, data, point_id, "hours", lambda v: _single(v, 120, "График работы"))
    map_url = _kept(conn, data, point_id, "map_url", _map_url)
    account = _kept(conn, data, point_id, "avito_account_id", lambda v: _account(conn, v)) if point_id or "avito_account_id" in data else None
    taken = conn.execute("SELECT name FROM point WHERE vk_peer_id = ? AND id != ?", (vk, point_id or 0)).fetchone() if vk else None
    if taken:                                                   # one VK, one point: the bot must know whose message it is
        raise PointError("Этот ВК уже указан у точки «%s»." % taken[0])
    clash = conn.execute("SELECT id FROM point WHERE name = ?", (name,)).fetchone()
    if clash and clash[0] != point_id:
        raise PointError("Точка с таким названием уже есть.")
    values = (name, avito, manager, phone, channel, webmim, default_time, vk, address, hours, map_url, account)
    if point_id is None:
        cur = conn.execute("INSERT INTO point (name, avito_address_id, manager_name, manager_phone, channel, webmim_address_id, default_time, vk_peer_id,"
                           " address, hours, map_url, avito_account_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", values)
        point_id = cur.lastrowid
    else:
        get_point(conn, point_id)
        conn.execute("UPDATE point SET name = ?, avito_address_id = ?, manager_name = ?, manager_phone = ?, channel = ?, webmim_address_id = ?,"
                     " default_time = ?, vk_peer_id = ?, address = ?, hours = ?, map_url = ?, avito_account_id = ? WHERE id = ?", values + (point_id,))
    conn.commit()
    return point_id


def delete_point(conn, point_id) -> None:
    get_point(conn, point_id)
    if conn.execute("SELECT COUNT(*) FROM order_card WHERE point_id = ?", (point_id,)).fetchone()[0]:
        raise PointError("Точка указана в заказах: удалить её нельзя.")
    if conn.execute("SELECT COUNT(*) FROM order_rule WHERE point_id = ?", (point_id,)).fetchone()[0]:
        raise PointError("Точка указана в правилах воронки: сначала уберите их.")
    conn.execute("DELETE FROM point WHERE id = ?", (point_id,))
    conn.commit()


def new_bind_code(conn, point_id) -> str:
    """A one-time code for binding the manager's VK to the point: the manager sends it to the community (bind_vk)."""
    get_point(conn, point_id)
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
    conn.execute("UPDATE point SET bind_code = ? WHERE id = ?", (code, point_id))
    conn.commit()
    return code


def bind_vk(conn, code: str, sender) -> dict:
    """The manager sent the point's code to the community: their VK becomes the point's (channel VK; a point in the panel stays in the panel,
    the owner 2026-10-23), the code is used up. One VK, one point."""
    code, sender = str(code or "").strip().upper(), str(sender or "").strip()
    if not (sender.isascii() and sender.isdigit() and sender.lstrip("0")):
        raise PointError("Не получилось привязать: неизвестный отправитель.")
    row = conn.execute("SELECT id FROM point WHERE bind_code != '' AND bind_code = ?", (code,)).fetchone() if len(code) == 8 else None
    if row is None:
        raise PointError("Код не подходит: попросите у владельца новый код точки.")
    taken = conn.execute("SELECT name FROM point WHERE vk_peer_id = ? AND id != ?", (sender, row[0])).fetchone()
    if taken:
        raise PointError("Этот ВК уже привязан к точке «%s»." % taken[0])
    cur = conn.execute("UPDATE point SET vk_peer_id = ?, channel = CASE channel WHEN 'panel' THEN 'panel' ELSE 'vk' END, bind_code = '' "
                       "WHERE id = ? AND bind_code = ?", (sender, row[0], code))
    conn.commit()
    if cur.rowcount != 1:                                     # the same code used at the same moment by somebody else
        raise PointError("Код не подходит: попросите у владельца новый код точки.")
    return get_point(conn, row[0])


# ---- a point in the panel (channel «panel»): what the bot would have written to its VK is kept for its «Моя точка» ------------------------

def in_panel(point: Optional[dict]) -> bool:
    return bool(point) and point.get("channel") == PANEL


def panel_point_of(conn, sender) -> Optional[dict]:
    """The point in the panel whose VK this is (it still writes commands from VK); None for any other sender."""
    sender = str(sender or "").strip()
    row = conn.execute("SELECT %s FROM point WHERE vk_peer_id != '' AND vk_peer_id = ? AND channel = ?" % FIELDS, (sender, PANEL)).fetchone() \
        if sender else None
    return _row(row) if row else None


NOTE_KINDS = {"answer": "Ответ бота", "alert": "Оповещение"}
NOTES_SHOWN = 10
NOTES_KEPT_DAYS = 14


def _note_key(point_id, at: str = "") -> str:
    return "point-note:%d:%s" % (int(point_id), at)


def add_note(conn, point_id, kind: str, text: str, now: str, order_id=None, asked: str = "") -> str:
    """Keep for the point what the bot did not write to its VK: the answer to its message (`asked`: what it wrote), an alert about an order.
    One row of app_state per note (no read-and-write of a shared list: the VK thread and the panel write at once); older than two weeks
    goes. Returns the note's id."""
    if kind not in NOTE_KINDS:
        raise PointError("Неизвестная запись для точки.")
    key = _note_key(point_id, "%s:%s" % (now, secrets.token_hex(4)))
    value = {"at": now, "kind": kind, "text": str(text or "").strip()[:2000], "asked": str(asked or "").strip()[:300],
             "order_id": int(order_id) if order_id else None}
    conn.execute("INSERT OR REPLACE INTO app_state (key, value) VALUES (?, ?)", (key, json.dumps(value, ensure_ascii=False)))
    try:
        border = (date.fromisoformat(str(now)[:10]) - timedelta(days=NOTES_KEPT_DAYS)).isoformat()
    except ValueError:
        border = ""
    if border:
        conn.execute("DELETE FROM app_state WHERE key > ? AND key < ?", (_note_key(point_id), _note_key(point_id, border)))
    conn.commit()
    return key


def notes(conn, point_id, limit: int = NOTES_SHOWN) -> List[dict]:
    """The point's notes, newest first: {id, at, kind, kind_name, text, asked, order_id}."""
    found = []
    for key, value in conn.execute("SELECT key, value FROM app_state WHERE key > ? AND key < ? ORDER BY key DESC LIMIT ?",
                                   (_note_key(point_id), "point-note:%d;" % int(point_id), int(limit))):
        try:
            note = json.loads(value)
        except ValueError:
            continue
        if isinstance(note, dict) and note.get("kind") in NOTE_KINDS:
            found.append(dict(note, id=key, kind_name=NOTE_KINDS[note["kind"]]))
    return found


# ---- the safety net of a point in the panel (the lead 2026-10-23): an alert nobody of the point opened goes to the owner -----------------

def _seen_key(point_id) -> str:
    return "point-seen:%d" % int(point_id)


def mark_seen(conn, point_id, up_to: str) -> None:
    """A person of the point opened its «Моя точка»: every note up to the newest one shown (`up_to`) is seen (the sound of notify.js is not
    a look). The mark never goes back."""
    conn.execute("INSERT INTO app_state (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = MAX(value, excluded.value)",
                 (_seen_key(point_id), str(up_to)))
    conn.commit()


def seen_at(conn, point_id) -> str:
    row = conn.execute("SELECT value FROM app_state WHERE key = ?", (_seen_key(point_id),)).fetchone()
    return row[0] if row else ""


def unseen_alerts(conn, point_id, since: str) -> List[dict]:
    """The alerts of the point written since `since` that nobody of the point has opened and that were not passed to the owner yet."""
    seen = seen_at(conn, point_id)
    found = []
    for key, value in conn.execute("SELECT key, value FROM app_state WHERE key > ? AND key < ? ORDER BY key",
                                   (_note_key(point_id, since), "point-note:%d;" % int(point_id))):
        try:
            note = json.loads(value)
        except ValueError:
            continue
        if isinstance(note, dict) and note.get("kind") == "alert" and not note.get("escalated_at") and str(note.get("at") or "") > seen:
            found.append(dict(note, id=key))
    return found


def opened_order(conn, point_id, order_id, since: str) -> bool:
    """A person of the point opened the order's card since `since` (the bell leads there, not to «Моя точка»): its alert is seen too."""
    return bool(order_id) and conn.execute(
        "SELECT 1 FROM order_event_seen s JOIN panel_user_role r ON r.login = s.login WHERE s.order_id = ? AND s.at >= ? "
        "AND r.role = 'point' AND r.point_id = ? LIMIT 1", (int(order_id), str(since or ""), int(point_id))).fetchone() is not None


def mark_escalated(conn, note_id: str, now: str, outcome: str) -> bool:
    """The note was passed to the owner (once): kept in the note itself. False when it is gone or was passed already."""
    row = conn.execute("SELECT value FROM app_state WHERE key = ?", (note_id,)).fetchone()
    try:
        note = json.loads(row[0]) if row else None
    except ValueError:
        note = None
    if not isinstance(note, dict) or note.get("escalated_at"):
        return False
    note.update(escalated_at=now, escalated=str(outcome)[:200])
    cur = conn.execute("UPDATE app_state SET value = ? WHERE key = ? AND value = ?", (json.dumps(note, ensure_ascii=False), note_id, row[0]))
    conn.commit()
    return cur.rowcount == 1                                  # another look passed it at the same moment
