"""The owner 2026-11-06 (a client who had written «забрал и поставил» got the end of the reservation the next day): the reminder of the last
day has the owner's new text; a client who wrote in the last 3 days gets nothing, the responsible gets «Клиент писал, проверьте заказ №…»;
a date in the client's answer moves the reservation (no answer to the client); no date, or one further than 30 days, is the responsible's;
a reminder that did not go (402) starts no 12 hours and is the responsible's; the sync's send again after a 402 never reaches an order
that left «Товар на точке». No real send: the transports are fakes."""
from __future__ import annotations

from datetime import date

import pytest

from test_order_reserve import PREVIOUS_TEXT, PointVk, card_at, chat_of, db, owner_text, stage
from test_reserve_noon import DAY, NEXT, Deliver, code, read_chats
from tires_sync import chats, order_intercity, order_manager as mgr, order_reserve as reserve, order_templates as tpl, orders, reply_date

NEW_TEXT = "Здравствуйте! Планируете забрать заказ? резерв истекает сегодня. Если планируете забрать, сообщите дату"


class Notify:
    """The tick's alerts to the order's responsible (order_notify.deliver): recorded, never sent."""

    def __init__(self):
        self.sent = []

    def __call__(self, card, text):
        self.sent.append((card["id"], text))
        return "ok"


def ready(tmp_path, error=None):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")                  # the reservation ends on DAY
    return conn, cid, chat_of(conn, cid), Deliver(conn, error), Notify()


def tick(conn, when, deliver, notify):
    deliver.at = when
    return reserve.check(conn, when, notify, deliver, PointVk())


def said(conn, chat, at, text="", kind="text", direction="in"):
    """A message of the chat as the sync keeps it (`at` Moscow time)."""
    conn.execute("INSERT INTO avito_message (chat_row, message_id, at, ts, direction, kind, text) VALUES (?, ?, ?, 0, ?, ?, ?)",
                 (chat, "m-%s-%s-%s" % (chat, direction, at), at, direction, kind, text))
    conn.commit()


def told(tmp_path, error=None):
    """The reminder went at 12:30 of DAY; the chats are read."""
    conn, cid, chat, deliver, notify = ready(tmp_path, error)
    tick(conn, DAY + "T12:30:00", deliver, notify)
    read_chats(conn, "2099-01-01T00:00:00")
    return conn, cid, chat, deliver, notify


def until(conn, cid):
    return orders.get_card(conn, cid)["reserve_until"]


def other_client_chat(conn, cid):
    """Another chat of the order's client (client_chat), not linked to the order."""
    client = conn.execute("SELECT client_id FROM client_order WHERE order_id = ?", (cid,)).fetchone()[0]
    account = conn.execute("SELECT account_id FROM avito_chat LIMIT 1").fetchone()[0]
    row = conn.execute("INSERT INTO avito_chat (account_id, chat_id) VALUES (?, 'c-other')", (account,)).lastrowid
    conn.execute("INSERT INTO client_chat (chat_row, client_id) VALUES (?, ?)", (row, client))
    conn.commit()
    return row


# ---- 1. the text --------------------------------------------------------------------------------------------------------------------

def test_the_last_day_brings_the_owners_new_text_word_for_word(tmp_path):
    conn, cid, chat, deliver, notify = told(tmp_path)
    assert owner_text() == NEW_TEXT
    assert [(c, t) for c, t, _ in deliver.sent] == [(chat, NEW_TEXT)]
    assert tpl.RESERVE_SAMPLE[1] == "client"


def test_the_previous_text_kept_word_for_word_is_replaced(tmp_path):
    conn = db(tmp_path)
    old = tpl.add_template(conn, tpl.RESERVE_SAMPLE[0], "client", PREVIOUS_TEXT)
    assert mgr.template_body(conn, tpl.RESERVE_SAMPLE) == NEW_TEXT
    tpl.add_samples(conn)
    assert tpl.get_template(conn, old)["body"] == NEW_TEXT


# ---- 2. the client wrote in the last 3 days ----------------------------------------------------------------------------------------

def test_a_client_who_wrote_in_the_orders_chat_gets_nothing_and_the_responsible_a_task_once(tmp_path):
    conn, cid, chat, deliver, notify = ready(tmp_path)
    said(conn, chat, "2026-10-17T20:00:00", "забрал и поставил")
    for when in (DAY + "T12:30:00", DAY + "T13:07:00", DAY + "T19:07:00"):
        tick(conn, when, deliver, notify)
    assert deliver.sent == []
    assert [t for _c, t in notify.sent if "Клиент писал" in t] == ["Клиент писал, проверьте заказ №5200001"]
    [record] = [e for e in orders.list_events(conn, cid) if "клиент-писал" in e["text"]]
    assert record["level"] == "yellow"
    read_chats(conn, "2099-01-01T00:00:00")
    tick(conn, NEXT + "T01:07:00", deliver, notify)                     # nothing went: no 12 hours
    assert code(conn, cid) == "at_point"


def test_another_chat_of_the_same_client_counts_too(tmp_path):
    conn, cid, _chat, deliver, notify = ready(tmp_path)
    said(conn, other_client_chat(conn, cid), "2026-10-16T09:00:00", "а когда можно забрать?")
    tick(conn, DAY + "T12:30:00", deliver, notify)
    assert deliver.sent == [] and [t for _c, t in notify.sent if "Клиент писал" in t] == ["Клиент писал, проверьте заказ №5200001"]


@pytest.mark.parametrize("at, kind, direction, text", [
    ("2026-10-17T20:00:00", "call", "in", "Звонок через Авито: пропущен"),
    ("2026-10-17T20:00:00", "call", "in", "Звонок через Авито: отклонён"),
    ("2026-10-17T20:00:00", "text", "out", "мы вам писали"),
    ("2026-10-15T12:29:00", "text", "in", "больше трёх дней назад"),
])
def test_calls_in_the_chat_our_messages_and_older_ones_are_no_message_of_the_client(tmp_path, at, kind, direction, text):
    conn, _cid, chat, deliver, notify = ready(tmp_path)
    said(conn, chat, at, text, kind, direction)
    tick(conn, DAY + "T12:30:00", deliver, notify)
    assert [t for _c, t, _ in deliver.sent] == [NEW_TEXT] and not [t for _c, t in notify.sent if "Клиент писал" in t]


def test_a_chat_of_somebody_else_is_not_the_clients(tmp_path):
    conn, cid, _chat, deliver, notify = ready(tmp_path)
    stranger = conn.execute("INSERT INTO avito_chat (account_id, chat_id) VALUES (1, 'c-stranger')").lastrowid
    said(conn, stranger, "2026-10-17T20:00:00", "здравствуйте")
    tick(conn, DAY + "T12:30:00", deliver, notify)
    assert [t for _c, t, _ in deliver.sent] == [NEW_TEXT]


def test_an_answer_to_the_reminder_is_not_a_reason_for_the_task(tmp_path):
    conn, cid, chat, deliver, notify = told(tmp_path)
    said(conn, chat, DAY + "T12:50:00", "ок")
    tick(conn, DAY + "T13:07:00", deliver, notify)
    assert not [t for _c, t in notify.sent if "Клиент писал" in t]


# ---- 3. a date in the answer -------------------------------------------------------------------------------------------------------

def test_a_date_in_the_answer_moves_the_reservation_and_writes_who_what_and_the_date(tmp_path):
    conn, cid, chat, deliver, notify = told(tmp_path)
    said(conn, chat, DAY + "T14:00:00", "Заберу 20.10, спасибо")
    tick(conn, DAY + "T15:07:00", deliver, notify)
    assert until(conn, cid) == "2026-10-20"
    [record] = [e for e in orders.list_events(conn, cid) if "резерв:ответ" in e["text"]]
    assert "Иван" in record["text"] and "«Заберу 20.10, спасибо»" in record["text"] and "20.10.2026" in record["text"]
    assert len(deliver.sent) == 1                                       # the reminder only: no answer to the client
    tick(conn, NEXT + "T03:07:00", deliver, notify)
    assert code(conn, cid) == "at_point"                                # no «На ОТХ» after 12 hours
    tick(conn, NEXT + "T10:07:00", deliver, notify)
    assert sum("резерв:ответ" in e["text"] for e in orders.list_events(conn, cid)) == 1     # the message is read once


@pytest.mark.parametrize("text, expected", [
    ("12.11", "2026-11-12"), ("12/11", "2026-11-12"), ("12.11.2026", "2026-11-12"), ("12 ноября", "2026-11-12"), ("12-го ноября", "2026-11-12"),
    ("завтра заберу", "2026-11-07"), ("Послезавтра", "2026-11-08"), ("сегодня вечером", "2026-11-06"), ("в пятницу", "2026-11-13"),
    ("в понедельник", "2026-11-09"), ("Во вторник после обеда", "2026-11-10"), ("6 декабря", "2026-12-06"), ("2.01", None),
])
def test_the_dates_a_client_writes(text, expected):
    status, found = reply_date.parse(text, date(2026, 11, 6))           # a Friday
    assert (found.isoformat() if status == "ok" else None) == expected


@pytest.mark.parametrize("text, status", [
    ("забрал и поставил", "none"), ("ок", "none"), ("", "none"), ("15.11 или 16.11", "many"), ("сегодня не смогу, в пятницу", "refused"),
    ("заберу 25.12", "far"), ("05.11", "far"), ("в 12.30 подъеду", "none"), ("8 900 123 45 67", "none"), ("31.02", "none"),
    ("Сегодня не успею", "refused"), ("завтра не смогу", "refused"), ("перезвоню завтра", "refused"), ("Отказываюсь, завтра не надо", "refused"),
    ("нет, 20.11", "refused"), ("отмените заказ", "refused"), ("Передумал", "refused"),
])
def test_no_date_two_dates_and_a_far_one(text, status):
    assert reply_date.parse(text, date(2026, 11, 6))[0] == status


def test_the_date_of_the_answer_counts_from_the_day_it_was_written(tmp_path):
    conn, cid, chat, deliver, notify = told(tmp_path)
    said(conn, chat, DAY + "T23:30:00", "завтра")
    tick(conn, NEXT + "T00:07:00", deliver, notify)
    assert until(conn, cid) == "2026-10-19"


# ---- 4. no date, or further than 30 days ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["ок, понял", "заберу 30.11", "Сегодня не успею", "завтра не смогу"])
def test_no_date_or_a_far_one_is_the_responsibles_and_moves_nothing(tmp_path, text):
    conn, cid, chat, deliver, notify = told(tmp_path)
    said(conn, chat, DAY + "T14:00:00", text)
    tick(conn, DAY + "T15:07:00", deliver, notify)
    [alert] = [t for _c, t in notify.sent if "Клиент ответил" in t]
    assert "№5200001" in alert and "«%s»" % text in alert
    assert until(conn, cid) == DAY
    tick(conn, DAY + "T16:07:00", deliver, notify)
    tick(conn, NEXT + "T03:07:00", deliver, notify)
    assert code(conn, cid) == "at_point" and until(conn, cid) == DAY
    assert len([t for _c, t in notify.sent if "Клиент ответил" in t]) == 1 and len(deliver.sent) == 1


# ---- the night -----------------------------------------------------------------------------------------------------------------------

def test_at_night_a_date_moves_the_reservation_and_no_date_waits_for_the_alert_hours(tmp_path):
    conn, cid, chat, deliver, notify = told(tmp_path)
    said(conn, chat, NEXT + "T01:00:00", "подъеду 21.10")
    tick(conn, NEXT + "T02:07:00", deliver, notify)
    assert until(conn, cid) == "2026-10-21" and notify.sent == [n for n in notify.sent if "Клиент ответил" not in n[1]]
    (tmp_path / "b").mkdir()
    conn, cid, chat, deliver, notify = told(tmp_path / "b")
    said(conn, chat, NEXT + "T01:00:00", "а можно подольше?")
    tick(conn, NEXT + "T02:07:00", deliver, notify)
    assert not [t for _c, t in notify.sent if "Клиент ответил" in t] and code(conn, cid) == "at_point"
    tick(conn, NEXT + "T10:07:00", deliver, notify)
    assert len([t for _c, t in notify.sent if "Клиент ответил" in t]) == 1 and until(conn, cid) == DAY


# ---- an answer not about the order, an order that left -----------------------------------------------------------------------------

def test_an_answer_in_a_chat_not_of_the_order_moves_nothing(tmp_path):
    conn, cid, _chat, deliver, notify = told(tmp_path)
    other = card_at(conn, "at_point", "2026-10-16T10:00:00")
    said(conn, chat_of(conn, other), DAY + "T14:00:00", "заберу 20.10")
    said(conn, other_client_chat(conn, cid), DAY + "T14:05:00", "заберу 21.10")
    tick(conn, DAY + "T15:07:00", deliver, notify)
    assert until(conn, cid) == DAY and not [t for c, t in notify.sent if c == cid and "Клиент ответил" in t]


@pytest.mark.parametrize("to", ["done", "cancel"])
def test_an_order_that_left_the_point_before_the_answer_is_not_touched(tmp_path, to):
    conn, cid, chat, deliver, notify = told(tmp_path)
    orders.move_card(conn, cid, stage(conn, to), DAY + "T13:00:00", "точка", {"cancel_reason": "не выкупил"} if to == "cancel" else None)
    said(conn, chat, DAY + "T14:00:00", "заберу 20.10")
    tick(conn, DAY + "T15:07:00", deliver, notify)
    card = orders.get_card(conn, cid)
    assert card["stage"]["code"] == to and not [e for e in orders.list_events(conn, cid) if "резерв:ответ" in e["text"]]
    assert not [t for _c, t in notify.sent if "Клиент ответил" in t]


@pytest.mark.parametrize("to", ["done", "cancel"])
def test_an_issued_or_closed_order_gets_no_reminder(tmp_path, to):
    conn, cid, chat, deliver, notify = ready(tmp_path)
    said(conn, chat, "2026-10-17T20:00:00", "забрал")
    orders.move_card(conn, cid, stage(conn, to), DAY + "T11:00:00", "точка", {"cancel_reason": "не выкупил"} if to == "cancel" else None)
    tick(conn, DAY + "T12:30:00", deliver, notify)
    assert deliver.sent == [] and not [t for _c, t in notify.sent if "Клиент писал" in t]


def test_an_order_to_another_city_and_the_points_own_sale_are_outside(tmp_path):
    conn, cid, chat, deliver, notify = ready(tmp_path)
    said(conn, chat, "2026-10-17T20:00:00", "здравствуйте")
    tpl.add_samples(conn)
    order_intercity.save(conn, cid, dict(enabled="1", city="Псков", carrier="ПЭК", track=""), DAY + "T09:00:00", "o")
    conn.execute("UPDATE order_card SET reserve_until = ? WHERE id = ?", (DAY, cid))
    conn.commit()
    tick(conn, DAY + "T12:30:00", deliver, notify)
    assert deliver.sent == [] and not [t for _c, t in notify.sent if "Клиент писал" in t]



def test_the_points_own_sale_at_the_point_is_outside(tmp_path):
    """The office may put the point's own sale (its own tyres, «Из наличия точки») on «Товар на точке»: no reminder, no task, no date."""
    from test_order_reserve import PLACED
    from tires_sync import order_points
    conn, cid, _chat, deliver, notify = ready(tmp_path)
    pid = orders.get_card(conn, cid)["point_id"]
    own = orders.create_card(conn, dict(client_name="Пётр", client_phone="+7 900 555 00 11", point_id=pid, deal_kind="stock", delivery_date="2026-10-13"), PLACED, "o")
    orders.add_item(conn, own, goods_id=orders.POINT_GOODS, name="Шина точки", brand_key="", season="winter", diameter=16, territory="1",
                    qty=4, sale_unit=5000, supplier_price=0, terms=None, fee_pct=0, promo_pct=0, now=PLACED, author="o")
    orders.move_card(conn, own, stage(conn, "at_point"), "2026-10-15T10:00:00", "o")
    assert own in order_points.own_sales(conn, pid) and until(conn, own) == DAY
    own_chat = conn.execute("INSERT INTO avito_chat (account_id, chat_id, order_id) VALUES (1, 'c-own', ?)", (own,)).lastrowid
    said(conn, own_chat, "2026-10-17T20:00:00", "здравствуйте")
    tick(conn, DAY + "T12:30:00", deliver, notify)
    assert [c for c, _t, _ in deliver.sent if c == own_chat] == [] and not [t for c, t in notify.sent if c == own and "Клиент писал" in t]


# ---- 5. a reminder that did not go ----------------------------------------------------------------------------------------------------

class Refused(Deliver):
    """Avito answers 402: the send mark «retry» (chats._deliver), the sync sends it again later by itself."""

    def __call__(self, chat_row, text, token):
        self.conn.execute("INSERT INTO chat_send_log (token, chat_row, at, author, status, next_at, text) VALUES (?, ?, ?, 'система', 'retry', ?, ?)",
                          (token, chat_row, self.at, self.at, text))
        raise chats.ChatSendRetry("Авито отказал (402), система повторит сама.")


def test_a_refused_reminder_starts_no_twelve_hours_and_is_the_responsibles(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    chat_of(conn, cid)
    deliver, notify = Refused(conn), Notify()
    tick(conn, DAY + "T12:30:00", deliver, notify)
    assert [t for c, t in notify.sent if "не ушло" in t] == ["Напоминание о сроке резерва по заказу №5200001 клиенту не ушло (Авито отказал (402), "
                                                              "система повторит сама.). Свяжитесь с клиентом."]
    assert any("Сообщение клиенту «Клиенту: срок резерва истекает»" in e["text"] for e in orders.list_events(conn, cid))
    conn.execute("UPDATE chat_send_log SET status = 'sent' WHERE token LIKE 'reserve-%'")    # the sync's send again went later
    read_chats(conn, "2099-01-01T00:00:00")
    tick(conn, DAY + "T13:07:00", deliver, notify)
    tick(conn, NEXT + "T03:07:00", deliver, notify)
    assert code(conn, cid) == "at_point"                                # no 12 hours from a send that did not go by itself
    assert len([t for c, t in notify.sent if "не ушло" in t]) == 1


@pytest.mark.parametrize("to", ["done", "cancel", "oth"])
def test_the_send_again_after_a_402_stops_for_an_order_that_left_the_point(tmp_path, to):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    row = chat_of(conn, cid)
    chat = {"id": row, "order_id": cid, "account_id": 1}
    stay = orders.get_card(conn, cid)["entered_at"]
    token = "reserve-%d-%s" % (cid, "".join(ch for ch in stay if ch.isdigit()))
    assert chats._stopped(conn, chat, token) == ""                      # still at the point: it may go
    orders.move_card(conn, cid, stage(conn, to), DAY + "T13:00:00", "точка", {"cancel_reason": "не выкупил"} if to != "done" else None)
    assert chats._stopped(conn, chat, token) != ""


def test_a_later_refusal_or_far_date_in_the_same_batch_wins_over_an_earlier_date(tmp_path):
    conn, cid, chat, deliver, notify = told(tmp_path)
    said(conn, chat, DAY + "T14:00:00", "завтра")
    said(conn, chat, DAY + "T14:01:00", "ой, лучше 20.12")
    said(conn, chat, DAY + "T14:02:00", "спасибо")                     # says nothing: the far date before it decides
    tick(conn, DAY + "T15:07:00", deliver, notify)
    assert until(conn, cid) == DAY and len([t for _c, t in notify.sent if "Клиент ответил" in t]) == 1


def test_after_the_date_moved_the_answers_are_the_managers(tmp_path):
    conn, cid, chat, deliver, notify = told(tmp_path)
    said(conn, chat, DAY + "T14:00:00", "заберу 20.10")
    tick(conn, DAY + "T15:07:00", deliver, notify)
    said(conn, chat, DAY + "T16:00:00", "спасибо")
    said(conn, chat, DAY + "T16:01:00", "сегодня")
    tick(conn, DAY + "T17:07:00", deliver, notify)
    assert until(conn, cid) == "2026-10-20" and not [t for _c, t in notify.sent if "Клиент ответил" in t]
