"""The end of the reservation at the point and of the seven days «На ОТХ»: the responsible manager is told the day before and on the day itself, once each.

The reservation runs from the day the goods arrived at the point (`orders._start_reserve`) until `reserve_until`; the managers move the date
by hand when the client asks for more time. In «На ОТХ» (`orders._start_oth`) the same date is the end of the time to sell the goods; then they go
back to the supplier. Only cards still in one of these two stages are looked at.
On the last day of the reservation the client is asked once (the owner 2026-10-23, п. 17: «Срок резерва истекает…», the template
«Клиенту: срок резерва истекает»), in the working hours, in the order's Avito chat; once per stay at the point (a date moved later brings no second one).
The owner 2026-10-26 (a client got it after picking the order up): the point is asked first, in VK, at the first tick of the working hours of
that day («Заказ №… ещё на точке?»). Only its «на точке <номер>» the same day before the end of the working hours sends the client the message
(`here`); «выдал» closes the order as any «выдал»; silence, or a point that cannot be asked, sends nothing and leaves a yellow record for the
responsible. The state of the question is one row of app_state per stay at the point, moved from «asked» by one UPDATE only (`_move`).
The owner 2026-10-26: an order without a WebMIM number on that day asks the responsible once per order for the number (`_ask_number`, the
same alert as the end of the reservation: a point account's order goes to its point), also when no message to the client can go; the
client's text then has no «№…» (`_client_body`).
The lead 2026-10-26: a point on «Панель» is asked on its «Моя точка» instead of VK (the same state «asked», then the same answers and silence).
The owner 2026-10-26: an order the point gave back to the supplier in this stay («Возврат», order_manager.returned) stays «На точке» until the
manager fills the amount; nobody is asked about it, the client gets no reminder and no yellow record is left (`_returned`).
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Callable, Optional, Tuple

from . import chats, order_manager, order_templates, orders, store, vk_send
from .order_points import PANEL

FIRST_HOUR = 9                                   # no alert before 09:00 (Moscow), so that nobody is woken by the hourly tick
STAGE_CODES = ("at_point", "oth")
Notify = Callable[[dict, str], str]               # (card, text) -> how it went; never raises
CLIENT_TEMPLATE = order_templates.RESERVE_SAMPLE[0]
PointSend = Callable[..., object]                 # vk_send.sender_for: (peer_id, text, random_id=None, keyboard=None); raises when it did not go
ASK_KEY = "reserve_ask:%d:%s"                     # app_state, per order and stay at the point: «asking|<at>» → asked → answered / silent; failed, unasked
ASK = "Заказ №%s (%s) ещё на точке? Ответьте «выдал %s» или «на точке %s»."
HERE = "на точке"
ASK_NUMBER = "Укажите номер заказа WebMIM по заказу %d (%s)"
NO_NUMBER_MARK = "[резерв:нет-номера]"            # once per order, whatever its stays at the point
NUMBER = re.compile(r"[ \t\u00a0]*[№#N]?[ \t\u00a0]*\{webmim_number\}")


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


def _arrived_today(conn, card: dict, now: str) -> bool:
    arrived = conn.execute("SELECT at FROM chat_send_log WHERE token = ? AND status = 'sent'",
                           ("arrived-%d-%s" % (card["id"], re.sub(r"\D", "", card["entered_at"] or "")),)).fetchone()
    return bool(arrived) and str(arrived[0])[:10] == now[:10]


def tell_client(conn, card: dict, deliver: Optional[order_manager.Deliver], now: str) -> str:
    """«Срок резерва истекает» to the client, once per stay at the point (the mark in the journal and the send token both carry the moment the
    card reached «Товар на точке»). The outcome is a record of the journal: green when it went, red when somebody has to write by hand
    (never asked to send again when it is not known whether it went). Returns "sent", "failed", "unknown" or "" (told already)."""
    stay = card["entered_at"] or ""
    marker = "[резерв:клиенту:%s]" % stay
    if _done(conn, card["id"], marker):
        return ""
    token = "reserve-%d-%s" % (card["id"], re.sub(r"\D", "", stay))
    if _arrived_today(conn, card, now):                         # the owner 2026-10-25 (хвост З17): «можно забирать» went today, one message a day
        orders.add_event(conn, card["id"], now, "система", "reserve", "Сообщение клиенту «%s» не отправлено: сегодня клиенту уже ушло «можно "
                         "забирать» %s" % (CLIENT_TEMPLATE, marker), level="grey")
        conn.commit()
        return ""
    status, why, copy = order_manager.send_to_client(conn, card, _client_body(conn, card), token, deliver, now)
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
        conn.execute("INSERT OR IGNORE INTO manager_message (message_id, at, point_id, sender, text, status) SELECT ?,?,?,?,?,'new' "
                     + chats.NOT_SENT_MEANWHILE, (token, now, card.get("point_id"), "система", "Заказ №%s: клиенту не отправлено «%s» — %s" % (
                         card.get("webmim_number") or card["id"], CLIENT_TEMPLATE, said) + (order_manager.COPY_MARK + copy if copy else ""), token))
    conn.commit()
    return status


def _client_body(conn, card: dict) -> str:
    """The reminder's template; without a WebMIM number its «№{webmim_number}» is left out with the space before it (the owner 2026-10-26)."""
    body = order_manager.template_body(conn, order_templates.RESERVE_SAMPLE)
    return body if card.get("webmim_number") else NUMBER.sub("", body)


def _ask_number(conn, card: dict, notify: Notify, now: str) -> None:
    """No WebMIM number on the day of the reminder: the responsible is asked for it once per order (the record of the journal is the mark)."""
    if card.get("webmim_number") or _done(conn, card["id"], NO_NUMBER_MARK):
        return
    text = ASK_NUMBER % (card["id"], card["client_name"] or card["client_phone"] or "клиент")
    orders.add_event(conn, card["id"], now, "система", "reserve", "Менеджеру: «%s»: %s %s" % (text, notify(card, text), NO_NUMBER_MARK),
                     level="yellow")
    conn.commit()


def _returned(conn, card: dict) -> bool:
    """The point gave the goods back to the supplier in this stay at the point (the record of order_manager.returned): no question, no client."""
    return conn.execute("SELECT 1 FROM order_event WHERE order_id = ? AND kind = 'point' AND text = ? AND at >= ? LIMIT 1",
                        (card["id"], order_manager.RETURN_LINE, card["entered_at"] or "")).fetchone() is not None


def _ask_key(card: dict) -> str:
    return ASK_KEY % (card["id"], re.sub(r"\D", "", card["entered_at"] or ""))


def was_asked(conn, card: dict) -> bool:
    """The point was asked about the order in this stay at the point (whatever became of the question since)."""
    return (store.get_state(conn, _ask_key(card)) or "").split("|", 1)[0] in ("asked", "answered", "silent")     # «asking»: not yet


def _move(conn, key: str, was: str, to: str) -> bool:
    """The question moves on only from the state that was read: of the point's answer and the evening tick at the same time, one wins."""
    won = conn.execute("UPDATE app_state SET value = ? WHERE key = ? AND value = ?", (to, key, was)).rowcount == 1
    conn.commit()
    return won


def _late(was: str, now: str, hours) -> bool:
    """The point's day to answer is over: the working hours of the day it was asked have ended."""
    asked = was.split("|", 1)[1][:10]
    return now[:10] > asked or (now[:10] == asked and datetime.fromisoformat(now).hour >= hours[1])


def _point_name(card: dict) -> str:
    return (card.get("point") or {}).get("name") or "точка не указана"


def ask_point(conn, card: dict, point_send: Optional[PointSend], now: str) -> str:
    """The last day of the reservation: the point is asked in VK whether the order is still there, once per stay. A point that cannot be asked
    (no VK, no key of the community, VK refused) is a yellow record for the responsible: the client gets nothing. Returns "asked", "failed",
    "unasked", or "" (asked already)."""
    key, point = _ask_key(card), card.get("point") or {}
    marker = "[резерв:точке:%s]" % (card["entered_at"] or "")
    why = ""
    if not point:
        why = "у заказа нет точки"
    elif point.get("channel") != "vk" or not point.get("vk_peer_id"):
        why = "точке «%s» бот в ВК не пишет" % point["name"]
    elif point_send is None:
        why = "ВК не настроен: нет ключа сообщества"
    number = order_manager.label(card)
    text = ASK % (number, ", ".join(w for w in (card["client_name"], order_manager.short(card)) if w) or "без позиций", number, number)
    if point and point.get("channel") == PANEL:                 # the lead 2026-10-26: a point in the panel is asked on «Моя точка» (question_open)
        if conn.execute("INSERT OR IGNORE INTO app_state (key, value) VALUES (?, ?)", (key, "asked|" + now)).rowcount != 1:
            conn.commit()
            return ""
        orders.add_event(conn, card["id"], now, "система", "reserve", "Точке «%s» задан вопрос в «Моей точке»: «Клиент забрал заказ №%s?» %s"
                         % (point["name"], number, marker), level="grey")
        conn.commit()
        return "asked"
    state = ("unasked|" if why else "asking|") + now
    taken = conn.execute("INSERT OR IGNORE INTO app_state (key, value) VALUES (?, ?)", (key, state)).rowcount == 1
    conn.commit()
    if not taken:                                               # another tick is at it: one question
        return ""
    if not why:
        try:
            point_send(point["vk_peer_id"], order_manager.signed(text), random_id=vk_send.stable_id("reserve-ask|" + key))
        except Exception as err:                                # it did not go: never asked again, the responsible is told
            why = "ВК не принял вопрос: %s" % (str(err)[:200] or err.__class__.__name__)
            _move(conn, key, state, "failed|" + now)
        else:                                                   # only a question that went makes «на точке» count
            _move(conn, key, state, "asked|" + now)
            conn.execute("UPDATE app_state SET value = '' WHERE key = ?", (order_manager.ISSUE_ASK_KEY % point["vk_peer_id"],))
            orders.add_event(conn, card["id"], now, "система", "reserve", "Точке «%s» отправлен вопрос: «%s» %s" % (point["name"], text, marker),
                             level="grey")              # a bare «да» from now on is never the answer to «Это заказ №…?»
            conn.commit()
            return "asked"
    orders.add_event(conn, card["id"], now, "система", "reserve", "Клиенту «%s» не отправлено: точку не спросить, ещё ли заказ на точке (%s). "
                     "Узнайте у точки, забрал ли клиент заказ, и при необходимости напишите клиенту %s" % (CLIENT_TEMPLATE, why, marker), level="yellow")
    conn.commit()
    return "unasked" if state.startswith("unasked") else "failed"


def _silent(conn, card: dict, key: str, was: str, now: str) -> bool:
    """The point did not answer by the end of its working day: nothing to the client, a yellow record for the responsible (once)."""
    if not _move(conn, key, was, "silent|" + now):
        return False
    orders.add_event(conn, card["id"], now, "система", "reserve", "Точка «%s» не ответила до конца рабочего дня, ещё ли заказ на точке: клиенту «%s» "
                     "не отправлено. Узнайте у точки, забрал ли клиент заказ, и при необходимости напишите клиенту [резерв:точка-молчит:%s]"
                     % (_point_name(card), CLIENT_TEMPLATE, card["entered_at"] or ""), level="yellow")
    conn.commit()
    return True


def here(conn, card: dict, who: str, now: str, deliver: Optional[order_manager.Deliver]) -> Tuple[str, str]:
    """The point's «на точке <номер>» about an order at the point (order_manager checks the point and the stage): the client gets «срок резерва
    истекает» only when the point was asked in this stay at the point, the same day, before the end of the working hours, and the reservation
    still ends that day. (outcome for the log, answer for the point)."""
    number = order_manager.label(card)
    if _returned(conn, card):                                   # the owner 2026-10-26: the goods went back, the question is over
        return ("«на точке» после возврата: клиенту не отправлено",
                "Заказ №%s: отмечен возврат поставщику, клиенту не пишем. Заказ уйдёт с точки, когда менеджер внесёт сумму." % number)
    key = _ask_key(card)
    was = store.get_state(conn, key) or ""
    if was.startswith("asked|") and _late(was, now, order_manager.duty_settings(conn)["hours"]):
        _silent(conn, card, key, was, now)
        was = store.get_state(conn, key) or ""
    if not was.startswith("asked|"):
        kind = was.split("|", 1)[0]
        if kind == "answered":
            return "уже отмечено", "Заказ №%s уже отмечен: на точке." % number
        if kind == "silent":
            orders.add_event(conn, card["id"], now, who, "point", "Точка ответила после конца рабочего дня: заказ ещё на точке (клиенту не отправлено)",
                             level="yellow")
            conn.commit()
            return ("«на точке» после конца рабочего дня: клиенту не отправлено",
                    "Заказ №%s: ответ пришёл после конца рабочего дня, клиенту не написали — с ним свяжется менеджер." % number)
        return ("«на точке» без вопроса бота: ничего не изменено",
                "Про заказ №%s бот не спрашивал, ничего не изменено. Если клиент забрал заказ, напишите «выдал %s»." % (number, number))
    if not _move(conn, key, was, "answered|" + now):            # another message of the point got it first
        return "уже отмечено", "Заказ №%s уже отмечен: на точке." % number
    orders.add_event(conn, card["id"], now, who, "point", "Точка ответила: заказ ещё на точке")
    conn.commit()
    if card["reserve_until"] != was.split("|", 1)[1][:10]:      # the date was moved since the question: the reservation does not end today
        return ("на точке; резерв продлён: клиенту не отправлено",
                "Заказ №%s — отмечено: на точке. Резерв уже продлён, клиенту не пишем." % number)
    status = tell_client(conn, card, deliver, now)
    if status == "sent":
        return "на точке; клиенту отправлено", "Заказ №%s — отмечено: на точке. Клиенту отправлено напоминание о сроке хранения." % number
    if not status:
        return "на точке; клиенту уже писали", "Заказ №%s — отмечено: на точке. Клиенту сегодня уже писали, второе сообщение не отправляем." % number
    return "на точке; клиенту не отправлено", "Заказ №%s — отмечено: на точке. Клиенту сообщение не отправлено: менеджер разберётся." % number


def question_open(conn, card: dict, now: str) -> bool:
    """«Моя точка» shows the question of the last day («Клиент забрал?») while the point can still answer it as in VK: asked in this stay at
    the point, the reservation still ends on the day it was asked, and that day's working hours are not over. It only reads: the answers
    (`here`, «выдал») and the tick move the question."""
    if (card.get("stage") or {}).get("code") != "at_point":
        return False
    was = store.get_state(conn, _ask_key(card)) or ""
    if not was.startswith("asked|") or _returned(conn, card):
        return False
    return card["reserve_until"] == was.split("|", 1)[1][:10] and not _late(was, now, order_manager.duty_settings(conn)["hours"])


def _point_step(conn, card: dict, today: date, now: str, hours, working: bool, deliver, point_send, notify: Notify) -> None:
    """A card at the point: the question of the last day (only where a client could be written to), the silence after it."""
    key = _ask_key(card)
    was = store.get_state(conn, key) or ""
    if was.startswith(("asked|", "asking|")) and _returned(conn, card):    # the owner 2026-10-26: given back after the question
        if _move(conn, key, was, "silent|" + now):
            orders.add_event(conn, card["id"], now, "система", "reserve", "Вопрос точке снят: точка отметила возврат поставщику, клиенту «%s» "
                             "не отправлено" % CLIENT_TEMPLATE, level="grey")
            conn.commit()
        return
    if was.startswith("asked|"):
        if _late(was, now, hours) and card["reserve_until"] == was.split("|", 1)[1][:10]:
            _silent(conn, card, key, was, now)
        elif _late(was, now, hours) and _move(conn, key, was, "silent|" + now):   # the date was moved since: nothing to wait for
            orders.add_event(conn, card["id"], now, "система", "reserve", "Точка не ответила на вопрос о заказе, но резерв уже продлён", level="grey")
            conn.commit()
        return
    if was.startswith("asking|"):                               # the work was cut while the question was sent: maybe it went, maybe not
        if _late(was, now, hours) and _move(conn, key, was, "failed|" + now):
            orders.add_event(conn, card["id"], now, "система", "reserve", "Клиенту «%s» не отправлено: неизвестно, ушёл ли вопрос точке (отправка "
                             "прервалась). Узнайте у точки, забрал ли клиент заказ, и при необходимости напишите клиенту" % CLIENT_TEMPLATE, level="yellow")
            conn.commit()
        return
    if was or not working or card["reserve_until"] != today.isoformat():
        return
    if _done(conn, card["id"], "[резерв:клиенту:%s]" % (card["entered_at"] or "")):
        return                                                  # the client was told in this stay already
    try:
        _ask_number(conn, card, notify, now)                    # also when no client message can go (the owner 2026-10-26)
    except Exception:                                           # the notice is a help: the question to the point still goes
        conn.rollback()
        logging.getLogger("tires_sync.orders").exception("the WebMIM number notice of card %s failed", card["id"])
    if deliver is None:                                         # nothing could be sent to the client: the point is not asked
        return
    if _returned(conn, card):                                   # the owner 2026-10-26: given back, waits for the amount: nobody is asked
        return
    if _arrived_today(conn, card, now):
        tell_client(conn, card, deliver, now)                   # «можно забирать» went today: the grey record only, nobody is asked
        return
    ask_point(conn, card, point_send, now)


def _all_returned(conn, card_id: int) -> bool:
    """Every piece of every position of the order on «На ОТХ» is marked «Вернуть поставщику» (order_oth.mark_returned: a return may be a
    part, then the rest is still to sell); an order without positions is not."""
    left = conn.execute("SELECT COUNT(*) FROM order_item i LEFT JOIN oth_return r ON r.item_id = i.id WHERE i.order_id = ? AND i.qty >= 1 "
                        "AND (r.item_id IS NULL OR r.qty < i.qty)", (card_id,)).fetchone()[0]
    return not left and conn.execute("SELECT 1 FROM order_item WHERE order_id = ? AND qty >= 1 LIMIT 1", (card_id,)).fetchone() is not None


def check(conn, now: str, notify: Optional[Notify], deliver: Optional[order_manager.Deliver] = None,
          point_send: Optional[PointSend] = None) -> int:
    """Alert for the cards whose reservation ends tomorrow or has ended. Returns how many alerts were made. Without `notify` nothing happens
    (an alert that cannot be sent must not be marked as made). With `deliver` (chats.send_reply of the chat's account) the point of a card at
    the point is asked on the last day of the reservation, in the working hours (the hours of the handoffs), through `point_send` (the VK of
    the community); its «на точке» tells the client (`here`). A date that is already past asks nobody."""
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
            if stage == "at_point":
                try:
                    _point_step(conn, card, today, now, hours, working, deliver, point_send, notify)
                except Exception:                             # the alert of the manager still goes
                    conn.rollback()
                    logging.getLogger("tires_sync.orders").exception("the reserve question to the point of card %s failed", card_id)
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
