"""The owner 2026-11-02: on the last day of the reservation the client gets «срок резерва истекает» by itself from 12:30, whatever the point
answered (the point is still asked as before); when the client writes nothing in that chat for 12 hours after it, the order goes to «На ОТХ»
by itself — not when the point marked «выдан», the order left «Товар на точке», the date was moved, or the reminder did not go."""
from __future__ import annotations

from test_order_reserve import PointVk, card_at, chat_of, db, ok, stage
from tires_sync import chats, order_reserve as reserve, orders

DAY = "2026-10-18"                                     # the last day of a card that came to the point on 2026-10-15
NEXT = "2026-10-19"


class Deliver:
    """chats.send_reply as order_reserve.check gets it: records the message and its send mark at the moment of the tick (`at`)."""

    def __init__(self, conn, error=None):
        self.conn, self.error, self.sent, self.at = conn, error, [], ""

    def __call__(self, chat_row, text, token):
        if self.error is not None:
            raise self.error
        self.sent.append((chat_row, text, token))
        self.conn.execute("INSERT INTO chat_send_log (token, chat_row, at, author, status) VALUES (?, ?, ?, 'система', 'sent')", (token, chat_row, self.at))


def ready(tmp_path, error=None):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    return conn, cid, chat_of(conn, cid), Deliver(conn, error), PointVk()


def tick(conn, when, deliver, vk=None):
    deliver.at = when
    return reserve.check(conn, when, ok, deliver, vk if vk is not None else PointVk())


def code(conn, cid):
    return orders.get_card(conn, cid)["stage"]["code"]


def wrote(conn, chat, at, direction="in", kind="text"):
    """A message of the chat as the sync keeps it (chats._store_message: `at` is Moscow time)."""
    conn.execute("INSERT INTO avito_message (chat_row, message_id, at, ts, direction, kind, text) VALUES (?, ?, ?, 0, ?, ?, 'продлите, пожалуйста')",
                 (chat, "m-%s-%s" % (direction, at), at, direction, kind))
    conn.commit()


# ---- 1. the reminder at 12:30 -----------------------------------------------------------------------------------------------------

def test_the_reminder_goes_at_12_30_not_at_12_29_and_once(tmp_path):
    conn, cid, chat, deliver, vk = ready(tmp_path)
    tick(conn, DAY + "T10:07:00", deliver, vk)
    assert len(vk.sent) == 1 and deliver.sent == []                                     # the point is asked as before
    tick(conn, DAY + "T12:29:00", deliver, vk)
    assert deliver.sent == []
    tick(conn, DAY + "T12:30:00", deliver, vk)
    assert [(c, t.split(".")[0]) for c, t, _ in deliver.sent] == [(chat, "Добрый день")]  # nobody answered: it goes all the same
    for when in (DAY + "T13:07:00", DAY + "T19:07:00"):
        tick(conn, when, deliver, vk)
    assert len(deliver.sent) == 1 and len(vk.sent) == 1


def test_no_reminder_after_the_working_hours_nor_the_next_day(tmp_path):
    conn, _cid, _chat, deliver, vk = ready(tmp_path)
    tick(conn, DAY + "T20:07:00", deliver, vk)                                          # the working hours are 10–20
    tick(conn, NEXT + "T12:30:00", deliver, vk)                                         # «истекает» is no longer true
    assert deliver.sent == []


def test_issued_before_12_30_tells_the_client_nothing(tmp_path):
    conn, cid, _chat, deliver, vk = ready(tmp_path)
    tick(conn, DAY + "T10:07:00", deliver, vk)
    orders.move_card(conn, cid, stage(conn, "done"), DAY + "T11:00:00", "точка")
    tick(conn, DAY + "T12:30:00", deliver, vk)
    assert deliver.sent == []


def test_the_point_saying_here_sends_nothing_and_the_tick_sends_it(tmp_path):
    conn, cid, _chat, deliver, vk = ready(tmp_path)
    tick(conn, DAY + "T10:07:00", deliver, vk)
    assert reserve.here(conn, orders.get_card(conn, cid), "менеджер Бета", DAY + "T10:30:00", deliver) == (
        "на точке", "Заказ №5200001 — отмечено: на точке.")
    assert deliver.sent == []
    tick(conn, DAY + "T12:30:00", deliver, vk)
    assert len(deliver.sent) == 1


def test_a_reminder_that_did_not_go_is_alerted_as_before(tmp_path):
    conn, cid, _chat, deliver, vk = ready(tmp_path, chats.ChatError("У аккаунта не заданы ключи Авито."))
    tick(conn, DAY + "T12:30:00", deliver, vk)
    [red] = [e for e in orders.list_events(conn, cid) if e["level"] == "red"]
    assert red["text"].startswith("Сообщение клиенту не отправлено («Клиенту: срок резерва истекает»)") and "не заданы ключи" in red["text"]
    assert [r[0] for r in conn.execute("SELECT text FROM manager_message")][0].startswith("Заказ №5200001: клиенту не отправлено")


# ---- 2. no answer of the client for 12 hours: «На ОТХ» -----------------------------------------------------------------------------

def told(tmp_path, error=None):
    conn, cid, chat, deliver, vk = ready(tmp_path, error)
    tick(conn, DAY + "T12:30:00", deliver, vk)
    return conn, cid, chat, deliver, vk


def test_twelve_hours_of_silence_move_the_order_to_oth_not_a_second_earlier(tmp_path):
    conn, cid, _chat, _deliver, _vk = told(tmp_path)
    assert not reserve._no_answer_step(conn, orders.get_card(conn, cid), NEXT + "T00:29:59")
    assert code(conn, cid) == "at_point"
    assert reserve._no_answer_step(conn, orders.get_card(conn, cid), NEXT + "T00:30:00")
    card = orders.get_card(conn, cid)
    assert card["stage"]["code"] == "oth" and card["cancel_reason"] == reserve.NO_ANSWER_REASON
    [record] = [e for e in orders.list_events(conn, cid) if "не ответил за 12 часов" in e["text"]]
    assert record["text"].startswith("Клиент не ответил за 12 часов на напоминание о сроке резерва: заказ переведён на «На ОТХ»")
    assert record["author"] == "система"


def test_the_tick_moves_it_the_next_morning_once(tmp_path):
    conn, cid, _chat, deliver, vk = told(tmp_path)
    tick(conn, NEXT + "T08:07:00", deliver, vk)                                         # before 09:00 the tick does nothing
    assert code(conn, cid) == "at_point"
    tick(conn, NEXT + "T09:07:00", deliver, vk)
    assert code(conn, cid) == "oth"
    tick(conn, NEXT + "T10:07:00", deliver, vk)
    assert sum("не ответил за 12 часов" in e["text"] for e in orders.list_events(conn, cid)) == 1


def test_a_message_of_the_client_after_the_reminder_keeps_the_order_at_the_point(tmp_path):
    conn, cid, chat, deliver, vk = told(tmp_path)
    wrote(conn, chat, DAY + "T23:59:00")
    tick(conn, NEXT + "T09:07:00", deliver, vk)
    tick(conn, NEXT + "T10:07:00", deliver, vk)
    assert code(conn, cid) == "at_point"
    assert sum("Клиент ответил на напоминание" in e["text"] for e in orders.list_events(conn, cid)) == 1


def test_our_own_messages_and_the_clients_before_the_reminder_do_not_count(tmp_path):
    conn, cid, chat, deliver, vk = told(tmp_path)
    wrote(conn, chat, DAY + "T11:00:00")                                                # before the reminder
    wrote(conn, chat, DAY + "T14:00:00", direction="out")                               # ours
    wrote(conn, chat, DAY + "T15:00:00", kind="system")                                 # Avito's
    tick(conn, NEXT + "T09:07:00", deliver, vk)
    assert code(conn, cid) == "oth"


def test_a_reminder_that_did_not_go_never_moves_the_order(tmp_path):
    conn, cid, _chat, deliver, vk = told(tmp_path, chats.ChatError("Авито не принял сообщение (403)."))
    tick(conn, NEXT + "T09:07:00", deliver, vk)
    tick(conn, "2026-10-20T12:07:00", deliver, vk)
    assert code(conn, cid) == "at_point"


def test_a_moved_date_or_an_issue_after_the_reminder_moves_nothing(tmp_path):
    conn, cid, _chat, deliver, vk = told(tmp_path)
    orders.update_card(conn, cid, {"reserve_until": "2026-10-21"}, DAY + "T15:00:00", "o")      # the client asked for more time by phone
    tick(conn, NEXT + "T09:07:00", deliver, vk)
    assert code(conn, cid) == "at_point"
    (tmp_path / "b").mkdir()
    conn, cid, _chat, deliver, vk = told(tmp_path / "b")
    orders.move_card(conn, cid, stage(conn, "done"), DAY + "T16:00:00", "точка")        # «выдан» after the reminder
    tick(conn, NEXT + "T09:07:00", deliver, vk)
    assert code(conn, cid) == "done"


def test_a_refused_move_is_one_red_record(tmp_path, monkeypatch):
    conn, cid, _chat, deliver, vk = told(tmp_path)

    def refuse(*a, **k):
        raise orders.OrderError("нельзя")
    monkeypatch.setattr(reserve.orders, "move_card", refuse)
    tick(conn, NEXT + "T09:07:00", deliver, vk)
    tick(conn, NEXT + "T10:07:00", deliver, vk)
    red = [e for e in orders.list_events(conn, cid) if e["level"] == "red" and "не перевести" in e["text"]]
    assert len(red) == 1 and "нельзя" in red[0]["text"] and code(conn, cid) == "at_point"
