"""The morning summary in Telegram: a few lines with the figures of the day for the owner and the partner. Off until the owner switches it
on; sent once a day after 9:00 Moscow time from the hourly tick (`orders-tick`), the day is marked before sending so it never goes twice.
The figures are those of the day dashboard (`analytics.day_numbers`); nothing is sent to a client."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable, List, Optional

from . import analytics, connections, notify, order_notify, store, users
from .secretbox import SecretKeyError
from .transport import Transport, urllib_transport

ON_KEY, SENT_KEY = "daily_summary_on", "daily_summary_sent"
SEND_FROM_HOUR, SEND_UNTIL_HOUR = 9, 12           # Moscow time: a summary switched on in the evening waits for the next morning


def is_on(conn) -> bool:
    return store.get_state(conn, ON_KEY) == "1"


def set_on(conn, on: bool, now: str, author: str) -> None:
    store.set_state(conn, ON_KEY, "1" if on else "")
    store.write_audit(conn, now, author, "daily_summary", {"on": bool(on)})


def _rub(value) -> str:
    return "{:,.0f}".format(float(value or 0)).replace(",", " ") + " ₽"


def text(conn, now: datetime) -> str:
    """3–6 lines: the orders, yesterday's and the week's money, the chats."""
    day = analytics.day_numbers(conn, now)
    yesterday = now.date() - timedelta(days=1)
    money_y = analytics.money(conn, yesterday.isoformat(), yesterday.isoformat())
    week = analytics.money(conn, (yesterday - timedelta(days=6)).isoformat(), yesterday.isoformat())     # seven whole days up to yesterday
    lines = ["Доброе утро! Сводка на %s." % now.strftime("%d.%m"),
             "Заказы: в работе %d, просрочено %d, новых сегодня %d." % (day["orders_open"], day["orders_overdue"], day["orders_new_today"]),
             "Вчера: выручка %s, прибыль %s." % (_rub(money_y["revenue"]), _rub(money_y["profit"])),
             "За 7 дней по вчера: выручка %s, прибыль %s." % (_rub(week["revenue"]), _rub(week["profit"])),
             "Чаты: непрочитанных %d, ждут ответа %d." % (day["chats_unread"], len(day["waiting"]))]
    return "\n".join(lines)


def _recipients(conn, box, http) -> List[Callable[[str], None]]:
    """The owner (the chat of the failure alerts) and every partner with a saved Telegram chat."""
    found = []
    owner = notify.notifier_for(conn, box, http=http)
    seen = set()
    if owner is not None:
        found.append(owner)
        seen.add(str(connections.get_config(conn, "telegram", 0).get("chat_id") or ""))
    chats = order_notify.user_chats(conn)
    partners = [u["login"] for u in users.list_users(conn) if u["role"] == "partner" and u["active"]]
    token = connections.get_secret(conn, box, "telegram", 0, "bot_token") if box is not None and any(p in chats for p in partners) else ""
    for login in partners:
        if login in chats and token and chats[login] not in seen:         # a chat shared with the owner (a group) gets it once
            seen.add(chats[login])
            found.append(lambda body, chat=chats[login]: notify.send_message(token, chat, body, http=http))
    return found


def maybe_send(conn, box, now: datetime, http: Transport = urllib_transport) -> Optional[str]:
    """Send the summary when it is on, it is between 9:00 and 12:00 in Moscow and it has not gone today. Returns what happened (None: not its time).
    The day is marked first: a second tick of the same day sends nothing, even when this one failed."""
    if not is_on(conn) or not SEND_FROM_HOUR <= now.hour < SEND_UNTIL_HOUR:
        return None
    today = now.date().isoformat()
    if store.get_state(conn, SENT_KEY) == today:
        return None
    store.set_state(conn, SENT_KEY, today)
    body = text(conn, now)
    sent, failed = 0, 0
    try:
        recipients = _recipients(conn, box, http)
    except SecretKeyError:
        recipients = []
    for send in recipients:
        try:
            send(body)
            sent += 1
        except notify.NotifyError:
            failed += 1
    return "сводка: отправлено %d, не ушло %d" % (sent, failed)
