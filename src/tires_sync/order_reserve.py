"""The end of the reservation at the point and of the seven days «На ОТХ»: the responsible manager is told the day before and on the day itself, once each.

The reservation runs from the day the goods arrived at the point (`orders._start_reserve`) until `reserve_until`; the managers move the date
by hand when the client asks for more time. In «На ОТХ» (`orders._start_oth`) the same date is the end of the time to sell the goods; then they go
back to the supplier. Only cards still in one of these two stages are looked at.
On the last day of the reservation the client is asked once (the owner 2026-10-23, п. 17: «Срок резерва истекает…», the template
«Клиенту: срок резерва истекает»), in the working hours, in the order's Avito chat; once per stay at the point (a date moved later brings no second one).
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Callable, Optional

from . import order_manager, order_templates, orders

FIRST_HOUR = 9                                   # no alert before 09:00 (Moscow), so that nobody is woken by the hourly tick
STAGE_CODES = ("at_point", "oth")
Notify = Callable[[dict, str], str]               # (card, text) -> how it went; never raises
CLIENT_TEMPLATE = order_templates.RESERVE_SAMPLE[0]


def _marker(kind: str, until: str, stage: str) -> str:
    return "[%s:%s:%s]" % ("отх" if stage == "oth" else "резерв", kind, until)


def _done(conn, card_id: int, marker: str) -> bool:
    return conn.execute("SELECT 1 FROM order_event WHERE order_id = ? AND kind = 'reserve' AND text LIKE ? LIMIT 1", (card_id, "%" + marker + "%")).fetchone() is not None


def _text(card: dict, kind: str, until: str, today: str, stage: str = "at_point") -> str:
    shown = date.fromisoformat(until).strftime("%d.%m.%Y")
    who = card["client_name"] or card["client_phone"] or "клиент"
    point = card["point"]["name"] if card.get("point") else "точка не указана"
    if stage == "oth":
        when = "закончился %s" % shown if until < today else ("заканчивается завтра (%s)" % shown if kind == "before" else "заканчивается сегодня (%s)" % shown)
        return ("Заказ №%d: срок продажи на ОТХ %s.\n%s, %s. Продайте товар или верните поставщику; если нужно больше времени, поставьте новую дату в заказе."
                % (card["id"], when, who, point))
    if kind == "before":
        head = "Завтра (%s) заканчивается резерв по заказу №%d" % (shown, card["id"])
    else:
        head = "Резерв по заказу №%d закончился %s" % (card["id"], shown) if until < today else "Сегодня (%s) заканчивается резерв по заказу №%d" % (shown, card["id"])
    return "%s.\n%s, %s. Свяжитесь с клиентом: продлить (поставить новую крайнюю дату в заказе) или отказ." % (head, who, point)


def tell_client(conn, card: dict, deliver: Optional[order_manager.Deliver], now: str) -> str:
    """«Срок резерва истекает» to the client, once per stay at the point (the mark in the journal and the send token both carry the moment the
    card reached «Товар на точке»). The outcome is a record of the journal: green when it went, red when somebody has to write by hand
    (never asked to send again when it is not known whether it went). Returns "sent", "failed", "unknown" or "" (told already)."""
    stay = card["entered_at"] or ""
    marker = "[резерв:клиенту:%s]" % stay
    if _done(conn, card["id"], marker):
        return ""
    token = "reserve-%d-%s" % (card["id"], re.sub(r"\D", "", stay))
    arrived = conn.execute("SELECT at FROM chat_send_log WHERE token = ? AND status = 'sent'",
                           ("arrived-%d-%s" % (card["id"], re.sub(r"\D", "", stay)),)).fetchone()
    if arrived and str(arrived[0])[:10] == now[:10]:            # the owner 2026-10-25 (хвост З17): «можно забирать» went today, one message a day
        orders.add_event(conn, card["id"], now, "система", "reserve", "Сообщение клиенту «%s» не отправлено: сегодня клиенту уже ушло «можно "
                         "забирать» %s" % (CLIENT_TEMPLATE, marker), level="grey")
        conn.commit()
        return ""
    status, why, copy = order_manager.send_to_client(conn, card, order_manager.template_body(conn, order_templates.RESERVE_SAMPLE), token, deliver, now)
    mark = conn.execute("SELECT status FROM chat_send_log WHERE token = ?", (token,)).fetchone() if status == "unknown" else None
    own = bool(mark) and mark[0] in ("retry", "lost")              # a 402 the sync sends again, a lost one: their own words only (L3)
    if status == "sent":
        text, level = "Клиенту отправлено «%s» %s" % (CLIENT_TEMPLATE, marker), "green"
    elif own:
        text, level = "Сообщение клиенту «%s»: %s %s" % (CLIENT_TEMPLATE, why, marker), "yellow" if mark[0] == "retry" else "red"
    elif status == "unknown":
        text, level = ("Сообщение клиенту «%s»: %s. Проверьте чат Авито заказа: если сообщения там нет, напишите клиенту вручную, повторно "
                       "не отправляйте %s" % (CLIENT_TEMPLATE, why, marker)), "red"
    else:
        text, level = "Сообщение клиенту не отправлено («%s»): %s. Напишите клиенту вручную %s" % (CLIENT_TEMPLATE, why, marker), "red"
    orders.add_event(conn, card["id"], now, "система", "reserve", text, level=level)
    if status != "sent":                                         # хвост З17: «Входящие» get the line and the text to send by hand, as «можно забирать»
        by_hand = "Проверьте чат Авито заказа: если сообщение там есть, повторно не отправляйте." if status == "unknown" else "Отправьте вручную."
        said = why if own else "%s. %s" % (why, by_hand)
        conn.execute("INSERT OR IGNORE INTO manager_message (message_id, at, point_id, sender, text, status) VALUES (?,?,?,?,?,'new')",
                     (token, now, card.get("point_id"), "система", "Заказ №%s: клиенту не отправлено «%s» — %s" % (
                         card.get("webmim_number") or card["id"], CLIENT_TEMPLATE, said) + (order_manager.COPY_MARK + copy if copy else "")))
    conn.commit()
    return status


def _all_returned(conn, card_id: int) -> bool:
    """Every piece of every position of the order on «На ОТХ» is marked «Вернуть поставщику» (order_oth.mark_returned: a return may be a
    part, then the rest is still to sell); an order without positions is not."""
    left = conn.execute("SELECT COUNT(*) FROM order_item i LEFT JOIN oth_return r ON r.item_id = i.id WHERE i.order_id = ? AND i.qty >= 1 "
                        "AND (r.item_id IS NULL OR r.qty < i.qty)", (card_id,)).fetchone()[0]
    return not left and conn.execute("SELECT 1 FROM order_item WHERE order_id = ? AND qty >= 1 LIMIT 1", (card_id,)).fetchone() is not None


def check(conn, now: str, notify: Optional[Notify], deliver: Optional[order_manager.Deliver] = None) -> int:
    """Alert for the cards whose reservation ends tomorrow or has ended. Returns how many alerts were made. Without `notify` nothing happens
    (an alert that cannot be sent must not be marked as made). With `deliver` (chats.send_reply of the chat's account) the client of a card at
    the point is asked on the last day of the reservation, in the working hours (the hours of the handoffs); a date that is already past asks nobody."""
    if notify is None:
        return 0
    moment = datetime.fromisoformat(now)
    if moment.hour < FIRST_HOUR:
        return 0
    today = moment.date()
    hours = order_manager.duty_settings(conn)["hours"]
    working = hours[0] <= moment.hour < hours[1]
    made = 0
    for card_id, stage in conn.execute("SELECT c.id, s.code FROM order_card c JOIN order_stage s ON s.id = c.stage_id WHERE s.code IN (?, ?) AND c.reserve_until != ''", STAGE_CODES).fetchall():
        try:
            card = orders.get_card(conn, card_id)
            if stage == "oth" and not conn.execute("SELECT 1 FROM order_event WHERE order_id = ? AND kind = 'field' AND text LIKE 'На ОТХ: срок продажи до %' LIMIT 1", (card_id,)).fetchone():
                continue                                      # stood in «На ОТХ» before the seven days existed: the date is the old reserve date
            if stage == "oth" and _all_returned(conn, card_id):
                continue                                      # every position went back to the supplier: nothing is left to sell (хвост З14)
            until = date.fromisoformat(card["reserve_until"])
            if stage == "at_point" and until == today and working and deliver is not None:
                try:
                    tell_client(conn, card, deliver, now)
                except Exception:                             # the alert of the manager still goes
                    conn.rollback()
                    logging.getLogger("tires_sync.orders").exception("the reserve message to the client of card %s failed", card_id)
            kind = "due" if until <= today else ("before" if until - timedelta(days=1) == today else "")
            if not kind:
                continue
            marker = _marker(kind, card["reserve_until"], stage)
            if _done(conn, card_id, marker):
                continue
            result = notify(card, _text(card, kind, card["reserve_until"], today.isoformat(), stage))
            orders.add_event(conn, card_id, now, "система", "reserve", "Оповещение о конце %s: %s %s" % ("срока на ОТХ" if stage == "oth" else "резерва", result, marker))
            conn.commit()
            made += 1
        except Exception:                                     # one broken card must not stop the others
            conn.rollback()
            logging.getLogger("tires_sync.orders").exception("reserve check failed for card %s", card_id)
    return made
