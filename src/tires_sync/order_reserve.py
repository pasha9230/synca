"""The end of the reservation at the point: the responsible manager is told the day before and on the day itself, once each.

The reservation runs from the day the goods arrived at the point (`orders._start_reserve`) until `reserve_until`; the managers move the date
by hand when the client asks for more time. Only cards still «Товар на точке» are looked at: «На ОТХ» has no time to sell (the owner 2026-10-30,
whether the goods go back to the supplier is the owner's call), a date left there by the seven days before is never read.
On the last day of the reservation the client is asked once (the owner 2026-10-23, п. 17: «Срок резерва истекает…», the template
«Клиенту: срок резерва истекает»), in the working hours, in the order's Avito chat; once per stay at the point (a date moved later brings no second one).
The owner 2026-10-26 (a client got it after picking the order up): the point is asked first, in VK, at the first tick of the working hours of
that day («Заказ №… ещё на точке?»). Its «на точке <номер>» the same day before the end of the working hours marks the answer (`here`; it
told the client until 2026-11-02, see below); «выдал» closes the order as any «выдал»; silence, or a point that cannot be asked, leaves a
yellow record for the responsible. The state of the question is one row of app_state per stay at the point, moved from «asked» by one UPDATE only (`_move`).
The owner 2026-10-26: an order without a WebMIM number on that day asks the responsible once per order for the number (`_ask_number`, the
same alert as the end of the reservation: a point account's order goes to its point), also when no message to the client can go; the
client's text then has no «№…» (`_client_body`).
The lead 2026-10-26: a point on «Панель» is asked on its «Рабочее место» instead of VK (the same state «asked», then the same answers and silence).
The owner 2026-10-26: an order the point gave back to the supplier in this stay («Возврат», order_manager.returned) stays «На точке» until the
manager fills the amount; nobody is asked about it, the client gets no reminder and no yellow record is left (`_returned`).
The owner 2026-11-02: the client no longer waits for the point's answer: on the last day of the reservation from 12:30 (CLIENT_AT) the
reminder goes by itself (`_client_step`); the point is still asked as before («на точке» / «выдан» on its «Рабочее место»), its «на точке»
only marks the answer. When the reminder went and the client wrote nothing in that chat for 12 hours (NO_ANSWER), the order goes to «На ОТХ»
by itself (`_no_answer_step`): not when the date was moved since, the order left «Товар на точке» or the reminder did not go.
The owner 2026-11-04: that move tells the order's responsible «Позвонить клиенту» with a line of the order's data (order_oth.call_line).
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Callable, Optional, Tuple

from . import chats, message_texts, order_manager, order_templates, orders, store, vk_send
from .order_points import PANEL

FIRST_HOUR = 9                                   # no alert before 09:00 (Moscow), so that nobody is woken by the hourly tick
STAGE_CODES = ("at_point",)
Notify = Callable[[dict, str], str]               # (card, text) -> how it went; never raises
CLIENT_TEMPLATE = order_templates.RESERVE_SAMPLE[0]
PointSend = Callable[..., object]                 # vk_send.sender_for: (peer_id, text, random_id=None, keyboard=None); raises when it did not go
ASK_KEY = "reserve_ask:%d:%s"                     # app_state, per order and stay at the point: «asking|<at>» → asked → answered / silent; failed, unasked
HERE = "на точке"
NO_NUMBER_MARK = "[резерв:нет-номера]"            # once per order, whatever its stays at the point
NUMBER = re.compile(r"[ \t\u00a0]*[№#N]?[ \t\u00a0]*\{webmim_number\}")
CLIENT_AT = (12, 30)                              # the owner 2026-11-02: the reminder goes on the last day from 12:30 (Moscow), by itself
NO_ANSWER = timedelta(hours=12)                   # no message of the client in the chat this long after the reminder: «На ОТХ»
NO_ANSWER_REASON = "Клиент не ответил на напоминание о сроке резерва"
OTH_MARK = "[резерв:на-отх:%s]"                   # once per stay at the point: moved, refused, or the client wrote
NO_ANSWER_MAX = timedelta(hours=48)               # an older reminder moves nothing (review 2026-11-02: no mass move of forgotten orders)
CHATS_FRESH = timedelta(minutes=30)               # the chats of the account read this lately, or «no answer» is not known (they are read every minute)
STALE_AFTER = timedelta(hours=24)                 # the move held up this long after its window by chats not read: one yellow record (the lead 2026-11-02)
STALE_MARK = "[резерв:чаты-не-читаются:%s]"


def _marker(kind: str, until: str) -> str:
    return "[резерв:%s:%s]" % (kind, until)


def _done(conn, card_id: int, marker: str) -> bool:
    return conn.execute("SELECT 1 FROM order_event WHERE order_id = ? AND kind = 'reserve' AND text LIKE ? LIMIT 1", (card_id, "%" + marker + "%")).fetchone() is not None


def _text(card: dict, kind: str, until: str, today: str) -> str:
    shown = date.fromisoformat(until).strftime("%d.%m.%Y")
    who = card["client_name"] or card["client_phone"] or "клиент"
    point = card["point"]["name"] if card.get("point") else "точка не указана"
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
    from . import order_merge, oth_sale
    if ((oth_sale.taken_ids(conn, card["id"]) or order_merge.placed_ids(conn, card["id"]))
            and not [i for i in oth_sale.supplier_items(conn, card) if int(i["qty"]) >= 1]):
        return                                                  # every tyre taken from «На ОТХ» or ordered by the sources of a merge: no number to ask
    text = message_texts.text(conn, message_texts.ASK_NUMBER, order_no=card["id"], client=card["client_name"] or card["client_phone"] or "клиент")
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
    text = message_texts.text(conn, message_texts.RESERVE_ASK, order_label=number,      # the owner's words («Шаблоны сообщений»)
                              order_summary=", ".join(w for w in (card["client_name"], order_manager.short(card)) if w) or "без позиций")
    if point and point.get("channel") == PANEL:                 # the lead 2026-10-26: a point in the panel is asked on «Рабочее место» (question_open)
        if conn.execute("INSERT OR IGNORE INTO app_state (key, value) VALUES (?, ?)", (key, "asked|" + now)).rowcount != 1:
            conn.commit()
            return ""
        orders.add_event(conn, card["id"], now, "система", "reserve", "Точке «%s» задан вопрос на «Рабочем месте»: «Клиент забрал заказ №%s?» %s"
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
    orders.add_event(conn, card["id"], now, "система", "reserve", "Точку не спросить, ещё ли заказ на точке (%s). Узнайте у точки, забрал ли клиент "
                     "заказ %s" % (why, marker), level="yellow")
    conn.commit()
    return "unasked" if state.startswith("unasked") else "failed"


def _silent(conn, card: dict, key: str, was: str, now: str) -> bool:
    """The point did not answer by the end of its working day: nothing to the client, a yellow record for the responsible (once)."""
    if not _move(conn, key, was, "silent|" + now):
        return False
    orders.add_event(conn, card["id"], now, "система", "reserve", "Точка «%s» не ответила до конца рабочего дня, ещё ли заказ на точке. Узнайте у "
                     "точки, забрал ли клиент заказ [резерв:точка-молчит:%s]" % (_point_name(card), card["entered_at"] or ""), level="yellow")
    conn.commit()
    return True


def here(conn, card: dict, who: str, now: str, deliver: Optional[order_manager.Deliver]) -> Tuple[str, str]:
    """The point's «на точке <номер>» about an order at the point (order_manager checks the point and the stage): the answer to the question of
    this stay, the same day, before the end of the working hours, is marked; the client is never written to from here (the owner 2026-11-02:
    the reminder goes by itself at 12:30, `_client_step`). `deliver` is kept for the callers. (outcome for the log, answer for the point)."""
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
            orders.add_event(conn, card["id"], now, who, "point", "Точка ответила после конца рабочего дня: заказ ещё на точке", level="yellow")
            conn.commit()
            return ("«на точке» после конца рабочего дня",
                    "Заказ №%s: ответ пришёл после конца рабочего дня — отмечено, менеджер увидит." % number)
        return ("«на точке» без вопроса бота: ничего не изменено",
                "Про заказ №%s бот не спрашивал, ничего не изменено. Если клиент забрал заказ, напишите «выдал %s»." % (number, number))
    if not _move(conn, key, was, "answered|" + now):            # another message of the point got it first
        return "уже отмечено", "Заказ №%s уже отмечен: на точке." % number
    orders.add_event(conn, card["id"], now, who, "point", "Точка ответила: заказ ещё на точке")
    conn.commit()
    if card["reserve_until"] != was.split("|", 1)[1][:10]:      # the date was moved since the question: the reservation does not end today
        return ("на точке; резерв продлён: клиенту не отправлено",
                "Заказ №%s — отмечено: на точке. Резерв уже продлён, клиенту не пишем." % number)
    return "на точке", "Заказ №%s — отмечено: на точке." % number         # the reminder goes by itself at 12:30 (the owner 2026-11-02)


def question_open(conn, card: dict, now: str) -> bool:
    """«Рабочее место» shows the question of the last day («Клиент забрал?») while the point can still answer it as in VK: asked in this stay at
    the point, the reservation still ends on the day it was asked, and that day's working hours are not over. It only reads: the answers
    (`here`, «выдал») and the tick move the question."""
    if (card.get("stage") or {}).get("code") != "at_point" or (card.get("intercity") or {}).get("on"):
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
            orders.add_event(conn, card["id"], now, "система", "reserve", "Неизвестно, ушёл ли вопрос точке, ещё ли заказ на точке (отправка "
                             "прервалась). Узнайте у точки, забрал ли клиент заказ", level="yellow")
            conn.commit()
        return
    if was or not working or card["reserve_until"] != today.isoformat():
        return
    try:
        _ask_number(conn, card, notify, now)                    # also when no client message can go (the owner 2026-10-26)
    except Exception:                                           # the notice is a help: the question to the point still goes
        conn.rollback()
        logging.getLogger("tires_sync.orders").exception("the WebMIM number notice of card %s failed", card["id"])
    if deliver is None:                                         # nothing could be sent to the client: the point is not asked
        return
    if _returned(conn, card):                                   # the owner 2026-10-26: given back, waits for the amount: nobody is asked
        return
    if _arrived_today(conn, card, now):                         # «можно забирать» went today: nobody is asked (the reminder's grey record
        return                                                  # comes at 12:30, _client_step)
    ask_point(conn, card, point_send, now)


def _client_step(conn, card: dict, today: date, now: str, hours, deliver) -> None:
    """The owner 2026-11-02: the last day of the reservation, from 12:30 to the end of the working hours, «срок резерва истекает» goes to the
    client by itself, whatever the point answered (tell_client: once per stay; not on the day «можно забирать» went). Not after a return."""
    moment = datetime.fromisoformat(now)
    if card["stage"]["code"] not in STAGE_CODES:                # a «выдал» since the card was read: nothing to remind of
        return
    if deliver is None or card["reserve_until"] != today.isoformat() or (moment.hour, moment.minute) < CLIENT_AT or moment.hour >= hours[1]:
        return
    if _returned(conn, card):
        return
    tell_client(conn, card, deliver, now)


def _tell_moved(conn, card: dict, call: str, notify: Optional[Notify], now: str) -> None:
    """The owner 2026-11-02: the move to «На ОТХ» is told to the point (its «Рабочее место» or VK, as the alerts of the order go to a point)
    and to its responsible (order_alerts.told_people, the owner 2026-11-03), through the tick's own alerts; each says how it went in the journal (order_notify).
    The owner 2026-11-04: the responsible's alert is `call` («Позвонить клиенту»); the point's stays the note."""
    if notify is None:
        return
    from . import order_alerts, order_oth
    moved = message_texts.text(conn, message_texts.MOVED_NOTE, order_label=order_manager.label(card))
    for who in [""] + order_alerts.told_people(conn, card["id"], order_oth.office_logins(conn)):
        # "": nobody of the staff answers, so the alert goes to the point; then its responsible only (2026-11-03)
        try:
            notify(dict(card, responsible=who, to_staff=bool(who)), call if who else moved)
        except Exception:                                       # noqa: BLE001 - the move is done; the journal has its record
            conn.rollback()
            logging.getLogger("tires_sync.orders").exception("the alert of the move to OTH of card %s failed", card["id"])


def _no_answer_step(conn, card: dict, now: str, notify: Optional[Notify] = None) -> bool:
    """The owner 2026-11-02: the reminder went (its send mark «sent») and the client wrote nothing in that chat for 12 hours: the order goes
    from «Товар на точке» to «На ОТХ», a record in its journal. Once per stay (OTH_MARK). Nothing when the date was moved since the reminder,
    the point marked a return, or the client wrote (a grey record). Returns True when the order moved."""
    stay = card["entered_at"] or ""
    marker = OTH_MARK % stay
    sent = conn.execute("SELECT at, chat_row FROM chat_send_log WHERE token = ? AND status = 'sent'",
                        ("reserve-%d-%s" % (card["id"], re.sub(r"\D", "", stay)),)).fetchone()
    if sent is None or now < (datetime.fromisoformat(sent[0]) + NO_ANSWER).isoformat(timespec="seconds") or _done(conn, card["id"], marker):
        return False
    if now > (datetime.fromisoformat(sent[0]) + NO_ANSWER_MAX).isoformat(timespec="seconds"):
        return False                                            # a reminder of days ago: the order was forgotten, not left by the client
    if card["reserve_until"] != sent[0][:10] or _returned(conn, card):
        return False                                            # extended since (a new reminder may come), or the goods went back
    fresh = (datetime.fromisoformat(now) - CHATS_FRESH).isoformat(timespec="seconds")
    if not conn.execute("SELECT 1 FROM avito_chat WHERE account_id = (SELECT account_id FROM avito_chat WHERE id = ?) AND synced_at >= ? LIMIT 1",
                        (sent[1], fresh)).fetchone():           # our copy of the chats may be behind: tried again at the next tick
        late = now >= (datetime.fromisoformat(sent[0]) + NO_ANSWER + STALE_AFTER).isoformat(timespec="seconds")
        if late and not _done(conn, card["id"], STALE_MARK % stay):
            orders.add_event(conn, card["id"], now, "система", "reserve", "Чаты аккаунта Авито не читаются: не узнать, ответил ли клиент на напоминание "
                             "о сроке резерва, на «На ОТХ» заказ пока не переведён. Свяжитесь с клиентом %s" % (STALE_MARK % stay), level="yellow")
            conn.commit()
        return False
    chats_of = "(SELECT id FROM avito_chat WHERE id = ? OR order_id = ?)"     # the chat of the reminder and any other chat of the order
    if conn.execute("SELECT 1 FROM avito_message WHERE chat_row IN %s AND direction = 'in' AND kind NOT IN ('system', 'deleted') AND at > ? "
                    "UNION ALL SELECT 1 FROM avito_chat WHERE id IN %s AND last_dir = 'in' AND last_at > ? LIMIT 1" % (chats_of, chats_of),
                    (sent[1], card["id"], sent[0], sent[1], card["id"], sent[0])).fetchone():
        orders.add_event(conn, card["id"], now, "система", "reserve", "Клиент ответил на напоминание о сроке резерва: на «На ОТХ» не переводим %s"
                         % marker, level="grey")
        conn.commit()
        return False
    oth = next((s for s in orders.list_stages(conn) if s["code"] == "oth"), None)
    if oth is None:
        return False
    try:
        moved = orders.move_card(conn, card["id"], oth["id"], now, "система", changes=None if card["cancel_reason"] else {"cancel_reason": NO_ANSWER_REASON},
                                 journal=False)                 # the refusal is the one red record below
    except orders.OrderError as err:
        orders.add_event(conn, card["id"], now, "система", "reserve", "Клиент не ответил за 12 часов на напоминание о сроке резерва, но на «%s» "
                         "заказ не перевести: %s %s" % (oth["name"], err, marker), level="red")
        conn.commit()
        return False
    if not moved:
        return False
    orders.add_event(conn, card["id"], now, "система", "reserve", "Клиент не ответил за 12 часов на напоминание о сроке резерва: заказ переведён на "
                     "«%s» %s" % (oth["name"], marker), level="grey")         # the one record of the alerts is «Позвонить клиенту»
    from . import order_oth
    moved = orders.get_card(conn, card["id"])
    line = order_oth.call_line(moved, card["reserve_until"])                  # the date as it was: the move clears it
    orders.add_event(conn, card["id"], now, "система", "reserve", "Позвонить клиенту: %s" % line, level="yellow")
    conn.commit()
    call = message_texts.text(conn, message_texts.CALL_CLIENT, order_label=order_manager.label(moved), line=line, link="/orders/%d" % card["id"])
    _tell_moved(conn, moved, call, notify, now)
    return True


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
    try:
        from . import order_oth
        order_oth.ask_seventh_day(conn, now)                  # the owner 2026-11-02: «На ОТХ» 6 days, then the point is asked
    except Exception:
        conn.rollback()
        logging.getLogger("tires_sync.orders").exception("the question of the seventh day on OTH failed")
    try:
        order_oth.tell_agree_due(conn, now, notify)           # the owner 2026-11-04: 6 days 12 hours on «На ОТХ», the responsible is told
    except Exception:
        conn.rollback()
        logging.getLogger("tires_sync.orders").exception("the alert of 6 days 12 hours on OTH failed")
    for (card_id,) in conn.execute("SELECT c.id FROM order_card c JOIN order_stage s ON s.id = c.stage_id WHERE s.code IN (%s) AND c.reserve_until != ''"
                                   % ",".join("?" * len(STAGE_CODES)), STAGE_CODES).fetchall():
        try:
            card = orders.get_card(conn, card_id)
            if (card.get("intercity") or {}).get("on"):      # to another city (order_intercity): no reservation, no reminder, no «На ОТХ»
                continue
            until = date.fromisoformat(card["reserve_until"])
            try:
                if _no_answer_step(conn, card, now, notify):
                    continue                                  # on «На ОТХ» now: no alert of the end of the reservation
            except Exception:
                conn.rollback()
                logging.getLogger("tires_sync.orders").exception("the move to OTH of card %s after the reminder failed", card_id)
            try:
                _point_step(conn, card, today, now, hours, working, deliver, point_send, notify)
            except Exception:                                 # the alert of the manager still goes
                conn.rollback()
                logging.getLogger("tires_sync.orders").exception("the reserve question to the point of card %s failed", card_id)
            try:
                _client_step(conn, orders.get_card(conn, card_id), today, now, hours, deliver)
            except Exception:
                conn.rollback()
                logging.getLogger("tires_sync.orders").exception("the reserve reminder to the client of card %s failed", card_id)
            kind = "due" if until <= today else ("before" if until - timedelta(days=1) == today else "")
            if not kind:
                continue
            marker = _marker(kind, card["reserve_until"])
            if _done(conn, card_id, marker):
                continue
            result = notify(card, _text(card, kind, card["reserve_until"], today.isoformat()))
            orders.add_event(conn, card_id, now, "система", "reserve", "Оповещение о конце резерва: %s %s" % (result, marker))
            conn.commit()
            made += 1
        except Exception:                                     # one broken card must not stop the others
            conn.rollback()
            logging.getLogger("tires_sync.orders").exception("reserve check failed for card %s", card_id)
    return made
