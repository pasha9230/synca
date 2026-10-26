"""The alert to the responsible manager of an order (the waiting list): who gets it and through what.
A panel user (the owner or a person with a login) gets it in Telegram, in the chat saved for them (the owner: the chat of the failure alerts),
except a point user: through their point («Моя точка» or its VK), never Telegram (the owner 2026-10-26: the bell with the sound is enough);
somebody who is not in the panel (the robot, a removed user) is answered through VK by the manager of the point of the order, or, when the
point works in the panel (channel «panel», the owner 2026-10-23), on its «Моя точка» (for the staff a grey record of the journal only);
when there is no way, the owner is told in Telegram that nobody could be written to. An order of the Avito account of a point (the owner
2026-10-26) goes only to that point: its user who answers for it, its VK or its «Моя точка»; with no way it is left on «Моя точка», never with
the owner. Every outcome is written in the feed of the order.
In VK the bot signs it «(бот) ». Nothing here is sent to a client."""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from . import connections, notify, order_points as points, orders, store, users, vk_send
from .secretbox import SecretBox, SecretKeyError
from .transport import Transport, urllib_transport

OWNER_LOGIN = "owner"                     # the owner's login unless the panel setting names another (panel.__init__: app.config AUTHOR, «owner»)
KIND = "telegram_users"                   # the panel setting of the alerts: {"chats": {login: chat_id}, "owner_login": login}
AUTHOR = "оповещение"
CHAT_ID = re.compile(r"^-?\d{1,20}$")


def user_chats(conn) -> Dict[str, str]:
    chats = connections.get_config(conn, KIND, 0).get("chats")
    return dict(chats) if isinstance(chats, dict) else {}


def set_user_chat(conn, login, chat_id, now: str, author: str) -> None:
    """Save (or, when empty, remove) the Telegram chat of a panel user."""
    login = str(login or "").strip()
    text = str(chat_id or "").strip()
    if not login or len(login) > 60:
        raise orders.OrderError("Логин пользователя: от 1 до 60 знаков.")
    if text and not CHAT_ID.match(text):
        raise orders.OrderError("Чат Telegram: число (у группы со знаком минус).")
    chats = user_chats(conn)
    if text:
        chats[login] = text
    else:
        chats.pop(login, None)
    connections.set_config(conn, KIND, 0, {**connections.get_config(conn, KIND, 0), "chats": chats}, now, author)


def get_owner_login(conn) -> str:
    """The login the alerts treat as the owner's (the owner's chat is the chat of the failure alerts): the panel setting, else «owner»."""
    login = connections.get_config(conn, KIND, 0).get("owner_login")
    return str(login).strip() if isinstance(login, str) and login.strip() else OWNER_LOGIN


def set_owner_login(conn, login, now: str, author: str) -> None:
    """Save the owner's login for the alerts; empty brings back «owner». Audited: what it was and what it is."""
    login = str(login or "").strip()
    if len(login) > 60:
        raise orders.OrderError("Логин владельца: до 60 знаков.")
    old = get_owner_login(conn)
    config = {k: v for k, v in connections.get_config(conn, KIND, 0).items() if k != "owner_login"}
    connections.set_config(conn, KIND, 0, {**config, "owner_login": login} if login else config, now, author)
    store.write_audit(conn, now, author, "owner_login", {"from": old, "to": login or OWNER_LOGIN})


def _panel_logins(conn, owner_login: str) -> set:
    return {owner_login} | {u["login"] for u in users.list_users(conn)}


def route(conn, card: dict, owner_login: Optional[str] = None) -> dict:
    """{"channel": "telegram"|"vk"|"panel"|"owner", "to": chat, peer or point, "why": the person, the point, or the reason nobody can be
    written to}.
    The owner's login is the panel setting unless one is given."""
    owner_login = owner_login or get_owner_login(conn)
    who = card["responsible"]                                      # whoever answers for the card (the maker unless another is named)
    own = None if card.get("to_staff") else points.of_account(conn, card.get("id"))   # to_staff: order_manager.staff_card
    if own is not None:                                            # an order of a point's Avito account: only that point (the owner 2026-10-26)
        return dict(_point_route(own), own_point=own["id"])
    mine = _users_point(conn, who)
    if mine is not None:                                           # a point user: through their point, never Telegram (the owner 2026-10-26)
        return _point_route(mine) if mine else {"channel": "owner", "to": "", "why": "точки пользователя «%s» больше нет" % who}
    if who in _panel_logins(conn, owner_login):
        chat = (connections.get_config(conn, "telegram", 0).get("chat_id") if who == owner_login else user_chats(conn).get(who)) or ""
        if chat:
            return {"channel": "telegram", "to": str(chat), "why": who}
        return {"channel": "owner", "to": "", "why": "у %s нет чата Telegram" % who}
    point = card.get("point")
    if not point:
        return {"channel": "owner", "to": "", "why": "ответственный «%s» не в панели, а у заказа нет точки" % who}
    if points.in_panel(point):
        return {"channel": "panel", "to": str(point["id"]), "why": point["name"]}
    if point["channel"] != "vk" or not point["vk_peer_id"]:
        return {"channel": "owner", "to": "", "why": "у точки «%s» не привязан ВК" % point["name"]}
    return {"channel": "vk", "to": str(point["vk_peer_id"]), "why": point["name"]}


def _users_point(conn, who: str) -> Optional[dict]:
    """The point of a point user (role «point»), whose news go through the point only: {} when it is gone (still never Telegram, review of
    lane 3); None for anybody else."""
    role = users.get_role(conn, who) if who and users.exists(conn, who) else None
    if not role or role["role"] != "point":
        return None
    try:
        return points.get_point(conn, role["point_id"]) if role["point_id"] else {}
    except points.PointError:
        return {}


def _point_route(point: dict) -> dict:
    """The way to a point: its «Моя точка» (the panel) or its VK; never the Telegram of a person of the point, even with a chat saved (the
    owner 2026-10-26, plan answer 8: the bell with the sound is enough). «owner» here only means there is no way (for an order of the point's
    account deliver then leaves it on «Моя точка», never with the owner)."""
    if points.in_panel(point):
        return {"channel": "panel", "to": str(point["id"]), "why": point["name"]}
    if point["channel"] != "vk" or not point["vk_peer_id"]:
        return {"channel": "owner", "to": "", "why": "у точки «%s» не привязан ВК" % point["name"]}
    return {"channel": "vk", "to": str(point["vk_peer_id"]), "why": point["name"]}


def _leave_on_point(conn, way: dict, card: dict, text: str, now: str, failure: str) -> str:
    """An alert of a point's account order that could not go: kept on the point's «Моя точка» and in the journal; the owner is not told."""
    try:
        points.add_note(conn, way["own_point"], "alert", text, now, card["id"])
        kept = "оставлено точке на «Моя точка»"
    except Exception as err:                                       # a busy base: the red record of the journal is what is left
        conn.rollback()
        kept = "на «Моя точка» не записано (%s)" % err.__class__.__name__
    _event(conn, card["id"], now, "Оповещение не отправлено: %s; %s (заказ аккаунта точки: владельцу не пишется)" % (failure, kept))
    return "не отправлено: %s; %s" % (failure, kept)


def _event(conn, card_id, now: str, text: str) -> None:
    orders.add_event(conn, card_id, now, AUTHOR, "notify", text)
    conn.commit()


def deliver(conn, box: Optional[SecretBox], card: dict, text: str, now: str, tg_http: Transport = urllib_transport,
            vk_http: Transport = urllib_transport, owner_login: Optional[str] = None) -> str:
    """Write the alert; never raises. Returns «отправлено …» or «не отправлено …» (the owner is told in the second case when possible)."""
    way = route(conn, card, owner_login)
    failure = way["why"] if way["channel"] == "owner" else ""
    if way["channel"] == "telegram":
        key = "order|%s|%s|%s" % (card["id"], way["to"], text)
        if notify.sent_recently(conn, key, now):                  # a repeat of the same alert (its caller failed after it): Telegram has no key
            _event(conn, card["id"], now, "Оповещение уже отправлено в течение часа: Telegram → %s (повтор не отправлен)" % way["why"])
            return "отправлено в Telegram (%s; повтор не отправлен)" % way["why"]
        try:
            token = connections.get_secret(conn, box, "telegram", 0, "bot_token") if box is not None else ""
            notify.send_message(token, way["to"], text, http=tg_http)
            notify.remember_sent(conn, key, now)                   # in the commit of the event: both, or neither
            _event(conn, card["id"], now, "Оповещение отправлено: Telegram → %s" % way["why"])
            return "отправлено в Telegram (%s)" % way["why"]
        except (notify.NotifyError, SecretKeyError) as err:
            failure = "Telegram → %s: %s" % (way["why"], err)
    elif way["channel"] == "vk":
        try:
            sender = vk_send.sender_for(conn, box, http=vk_http)
            if sender is None:
                raise vk_send.VkError("ВК не настроен: нет ключа сообщества.")
            from .order_manager import signed
            body = signed(text)                                    # the bot writes it: «(бот) » (the owner 2026-10-24)
            sender(way["to"], body, random_id=vk_send.stable_id("notify|%s|%s|%s|%s" % (card["id"], way["to"], body, now[:13])))
            _event(conn, card["id"], now, "Оповещение отправлено: ВК → точка «%s»" % way["why"])
            return "отправлено в ВК (%s)" % way["why"]
        except vk_send.VkError as err:
            failure = "ВК → точка «%s»: %s" % (way["why"], err)
    elif way["channel"] == "panel":                                # the point's own «Моя точка»; the staff get no alert of it (grey)
        try:
            points.add_note(conn, int(way["to"]), "alert", text, now, card["id"])
            _event(conn, card["id"], now, "Оповещение показано точке «%s» в панели" % way["why"])
            return "показано точке в панели (%s)" % way["why"]
        except Exception as err:                                   # a busy base: the owner is told, as when nobody can be written to
            conn.rollback()
            failure = "панель → точка «%s»: %s" % (way["why"], err.__class__.__name__)
    if way.get("own_point") is not None:
        return _leave_on_point(conn, way, card, text, now, failure)
    told = ""
    key = "owner|%s|%s|%s" % (card["id"], failure, text)
    try:
        owner = notify.notifier_for(conn, box, http=tg_http)
        if owner is not None and notify.sent_recently(conn, key, now):
            told = "; владельцу уже написано в Telegram (повтор не отправлен)"
        elif owner is not None:
            owner("Некому отправить оповещение по заказу №%d (%s).\n%s" % (card["id"], failure, text))
            notify.remember_sent(conn, key, now)                   # in the commit of the event below
            told = "; владельцу написано в Telegram"
    except (notify.NotifyError, SecretKeyError):
        told = "; владельцу написать не удалось"
    _event(conn, card["id"], now, "Оповещение не отправлено: %s%s" % (failure, told))
    return "не отправлено: %s%s" % (failure, told)


def missing_order_people(conn) -> List[str]:
    """Who is told of a number a point wrote that the panel does not know (the owner 2026-10-24: the partner adds such orders): every active
    partner; with no active partner (the panel has no roles yet) every active owner and the owner's login of the alerts, so that it is never lost."""
    people = [u for u in users.list_users(conn) if u["active"]]
    partners = sorted(u["login"] for u in people if u["role"] == "partner")
    if partners:
        return partners
    return sorted({u["login"] for u in people if u["role"] == "owner"} | {get_owner_login(conn)})


def tell_people(conn, box: Optional[SecretBox], logins: List[str], text: str, tg_http: Transport = urllib_transport, now: str = "") -> str:
    """The same text in Telegram to each of these panel users who has a chat (the owner's login: the chat of the failure alerts), once per chat.
    With `now`, the same text to the same chat within the hour is a repeat and is not sent again. Never raises; returns how it went."""
    owner = get_owner_login(conn)
    owner_chat = str(connections.get_config(conn, "telegram", 0).get("chat_id") or "")
    mine = user_chats(conn)
    chats: Dict[str, List[str]] = {}
    for login in logins:
        chat = owner_chat if login == owner else str(mine.get(login) or "")
        if chat:
            chats.setdefault(chat, []).append(login)
    if not chats:
        return "Telegram: ни у кого нет чата (%s)" % ", ".join(logins)
    try:
        token = connections.get_secret(conn, box, "telegram", 0, "bot_token") if box is not None else ""
    except SecretKeyError as err:
        return "Telegram не отправлен: %s" % err
    sent, failed = [], []
    for chat, who in sorted(chats.items()):
        key = "people|%s|%s" % (chat, text)
        if now and notify.sent_recently(conn, key, now):                  # the same news to the same chat within the hour: a repeat, not sent
            sent += who
            continue
        try:
            notify.send_message(token, chat, text, http=tg_http)
            if now:
                notify.remember_sent(conn, key, now)
                conn.commit()                                      # kept even when the caller's own record of it fails afterwards
            sent += who
        except notify.NotifyError as err:
            failed.append("%s: %s" % (", ".join(who), err))
    parts = ["Telegram: отправлено (%s)" % ", ".join(sent)] if sent else []
    if failed:
        parts.append("Telegram не отправлен (%s)" % "; ".join(failed))
    return "; ".join(parts)


UNSEEN_MINUTES = 15
UNSEEN_START_KEY = "point_unseen_start"           # app_state: the notes written before the safety net came are the past


def escalate_unseen(conn, box: Optional[SecretBox], now: str, tg_http: Transport = urllib_transport) -> int:
    """The safety net of a point in the panel (the lead 2026-10-23, as the old «no VK → the owner»): an alert on its «Моя точка» that nobody of
    the point opened within 15 minutes of working time (the hours of the handoffs) goes to the owner in Telegram, once. Opened: its «Моя точка»
    or, since the alert, the order's card (lane 3: the owner is never told what the point already saw). Returns how many."""
    from datetime import datetime
    from . import order_manager
    start = store.get_state(conn, UNSEEN_START_KEY)
    if not start:
        store.set_state(conn, UNSEEN_START_KEY, now)
        return 0
    hours = order_manager.duty_settings(conn)["hours"]
    moment = datetime.fromisoformat(now[:19])
    passed = 0
    for point in points.list_points(conn):
        if not points.in_panel(point):
            continue
        for note in points.unseen_alerts(conn, point["id"], start):
            if order_manager.take_deadline(note["at"], hours, UNSEEN_MINUTES) > moment:
                continue
            if points.of_account(conn, note.get("order_id")) is not None:     # an order of a point's account: the point's only (2026-10-26)
                points.mark_escalated(conn, note["id"], now, "заказ аккаунта точки: владельцу не передаётся")
                continue
            if points.opened_order(conn, point["id"], note.get("order_id"), note.get("at")):   # the point opened the order: it is seen
                points.mark_escalated(conn, note["id"], now, "точка открыла заказ: владельцу не передаётся")
                continue
            text = "Точка «%s» не открыла оповещение%s за %d минут рабочего времени.\n%s" % (
                point["name"], " по заказу №%d" % note["order_id"] if note.get("order_id") else "", UNSEEN_MINUTES, note.get("text") or "")
            key = "unseen|%s" % note["id"]
            try:
                owner = notify.notifier_for(conn, box, http=tg_http)
                if owner is None:
                    raise notify.NotifyError("Telegram не настроен")
                if not notify.sent_recently(conn, key, now):       # sent, but the mark below failed (a busy base): not sent twice
                    owner(text)
                    notify.remember_sent(conn, key, now)
                    conn.commit()                                  # kept even when the mark below fails
                outcome = "владельцу написано в Telegram"
            except (notify.NotifyError, SecretKeyError) as err:
                outcome = "владельцу написать не удалось: %s" % err
            if not points.mark_escalated(conn, note["id"], now, outcome):
                continue
            passed += 1
            if note.get("order_id") and conn.execute("SELECT 1 FROM order_card WHERE id = ?", (note["order_id"],)).fetchone():
                orders.add_event(conn, note["order_id"], now, AUTHOR, "notify", "Точка «%s» не открыла оповещение за %d минут: %s." % (
                    point["name"], UNSEEN_MINUTES, outcome), level="yellow")
                conn.commit()
    return passed
