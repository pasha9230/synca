"""The tails of the client's messages (lane tails-answers, 2026-10-27): a new press of a failed send under its own mark looks at the chat first
in every order of events (1); a line of «Входящие» about a send is answered once the send reached the client by a new press too, and never
written after it did (2); a line about a send the sync stopped for a closed order is answered too (3)."""
from __future__ import annotations

import pytest

from test_chat_send_refusal import Refusing
from test_send_settle import NOW_TS, at, out, refused402, row, settle
from test_send_settle4 import arrived, arrived_token, lines, refused403, refused_once
from test_stuck_sending import NOW, env  # noqa: F401  (env is a fixture)
from tires_sync import chats, order_manager

ARRIVED = "Можно забирать"


def refused_402s_to_the_end(conn, chat_row, sender, token="tok-1", text="Добрый день"):
    """A 402 and the sync's three tries refused too: «failed» and the sync's alert with the text to copy."""
    with pytest.raises(chats.ChatSendRetry):
        chats.send_reply(conn, sender, chat_row, text, token, NOW, "owner")
    for minute in (1, 2, 3):
        settle(conn, sender, at(61 * minute))
    assert row(conn, token)["status"] == "failed" and sender.sent == []


# --- 1: a new press of a failed send never makes a copy ---------------------------------------------------------------------------------

def test_a_second_arrived_after_the_402s_ended_finds_the_text_written_from_the_alert(env, monkeypatch):
    conn, chat_row, card = env
    sender = Refusing(*[refused402() for _ in range(4)])
    sent, _note = arrived(conn, card, sender, monkeypatch, NOW)
    token = arrived_token(conn)
    for minute in (1, 2, 3):
        settle(conn, sender, at(61 * minute))
    assert not sent and row(conn, token)["status"] == "failed"
    alert = [line for line in lines(conn) if line["message_id"].startswith("send-")]
    assert len(alert) == 1 and order_manager.COPY_MARK + ARRIVED in alert[0]["text"]
    sender.history = [out("m-hand", ARRIVED, created=NOW_TS + 600)]                             # the manager wrote it from the alert
    sent, _note = arrived(conn, card, sender, monkeypatch, at(700))
    assert sent and sender.sent == [] and row(conn, token)["status"] == "sent"
    assert all(line["status"] == "answered" for line in lines(conn))


def test_a_press_while_another_press_of_the_failed_send_is_sending_sends_nothing(env):
    conn, chat_row, card = env
    sender = Refusing(refused403())
    refused_once(conn, chat_row, sender)
    conn.execute("UPDATE chat_send_log SET status = 'sending', at = ? WHERE token = 'tok-1'", (at(100),))    # another window took it
    conn.commit()
    with pytest.raises(chats.ChatError, match="уже отправляется"):
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", at(110), "owner")
    assert sender.sent == []


def test_a_press_that_another_press_took_after_its_look_into_the_chat_sends_nothing(env, monkeypatch):
    conn, chat_row, card = env
    sender = Refusing(refused403())
    refused_once(conn, chat_row, sender)
    looked = chats._failed_is_there

    def and_another_press_took_it(*args):
        found = looked(*args)
        conn.execute("UPDATE chat_send_log SET status = 'sending', at = ? WHERE token = 'tok-1'", (at(101),))
        conn.commit()
        return found
    monkeypatch.setattr(chats, "_failed_is_there", and_another_press_took_it)
    with pytest.raises(chats.ChatError, match="уже отправляется"):
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", at(100), "owner")
    assert sender.sent == []


def test_a_third_press_looks_at_the_chat_since_the_second_one_that_was_refused_too(env):
    conn, chat_row, card = env
    sender = Refusing(refused403(), refused403())
    refused_once(conn, chat_row, sender)
    with pytest.raises(chats.ChatError):
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", at(100), "owner")      # nothing in the chat: tried, refused again
    sender.history = [out("m-hand", "Добрый день", created=NOW_TS + 300)]                    # written by hand after the second refusal
    assert chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", at(400), "owner") == "found"
    assert sender.sent == [] and row(conn)["status"] == "sent"


def test_a_failed_send_pressed_twice_with_nothing_in_the_chat_goes_once(env):
    conn, chat_row, card = env
    sender = Refusing(refused403())
    refused_once(conn, chat_row, sender)
    assert chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", at(100), "owner") is None
    sender.history = []                                                                          # Avito has not shown it yet
    assert chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", at(110), "owner") is None
    assert sender.sent == ["Добрый день"]


# --- 2: the line of «Входящие» is answered once the message reached the client ---------------------------------------------------------

DELIVERED = "Итог: сообщение клиенту дошло (отправлено повторно). Делать ничего не нужно."


def test_a_second_arrived_that_goes_answers_the_line_with_the_text_to_copy_of_the_first(env, monkeypatch):
    conn, chat_row, card = env
    sender = Refusing(refused403())
    sent, _note = arrived(conn, card, sender, monkeypatch, NOW)
    [line] = lines(conn)
    assert not sent and line["status"] == "new" and order_manager.COPY_MARK + ARRIVED in line["text"]
    sent, _note = arrived(conn, card, sender, monkeypatch, at(1000))                            # nothing in the chat: it goes
    assert sent and sender.sent == [ARRIVED]
    [line] = lines(conn)
    assert line["status"] == "answered" and line["answer"] == DELIVERED
    assert order_manager.new_count(conn) == 0


def test_a_new_press_after_the_402s_ended_answers_the_alert_of_the_sync(env):
    conn, chat_row, card = env
    sender = Refusing(*[refused402() for _ in range(4)])
    refused_402s_to_the_end(conn, chat_row, sender)
    conn.execute("INSERT INTO manager_message (message_id, at, point_id, sender, text, status) VALUES ('send-tok-10-1', ?, NULL, 'система', "
                 "'чужая', 'new')", (NOW,))
    conn.commit()
    assert chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", at(1000), "owner") is None
    assert sender.sent == ["Добрый день"]
    shown = {line["message_id"][:11]: (line["status"], line["answer"]) for line in lines(conn)}
    assert shown["send-tok-1-"] == ("answered", DELIVERED) and shown["send-tok-10"] == ("new", "")


def test_a_new_press_of_a_refused_photo_that_goes_answers_its_lines(env):
    from test_chat_send_refusal import picture
    conn, chat_row, card = env
    sender = Refusing(refused403())
    with pytest.raises(chats.ChatError):
        chats.send_image(conn, sender, chat_row, picture(), "img-1", NOW, "owner")
    conn.execute("INSERT INTO manager_message (message_id, at, point_id, sender, text, status) VALUES ('img-1-1', ?, NULL, 'система', 'фото', 'new')",
                 (NOW,))
    conn.commit()
    chats.send_image(conn, sender, chat_row, picture(), "img-1", at(1000), "owner")
    assert sender.sent == ["img-1"]
    [line] = lines(conn)
    assert line["status"] == "answered" and line["answer"] == DELIVERED


def test_the_first_press_of_a_send_answers_no_line(env):
    conn, chat_row, card = env
    conn.execute("INSERT INTO manager_message (message_id, at, point_id, sender, text, status) VALUES ('tok-1-1', ?, NULL, 'система', 'x', 'new')",
                 (NOW,))
    conn.commit()
    chats.send_reply(conn, Refusing(), chat_row, "Добрый день", "tok-1", NOW, "owner")
    assert lines(conn)[0]["status"] == "new"                                                    # a first send has no line of its own


def test_a_line_about_a_send_that_went_meanwhile_is_never_written(env, monkeypatch):
    """The race of two «приехал» of one arrival: the first one refused, the second one delivered it before the first wrote its line."""
    conn, chat_row, card = env
    sender = Refusing(refused403())
    real = order_manager.orders.add_event

    def and_the_other_one_goes(conn_, card_, now, who, kind, text, level="grey"):
        if level == "red" and not sender.sent:
            chats.send_reply(conn, sender, chat_row, ARRIVED, arrived_token(conn), at(5), "система")
        return real(conn_, card_, now, who, kind, text, level=level)
    monkeypatch.setattr(order_manager.orders, "add_event", and_the_other_one_goes)
    sent, _note = arrived(conn, card, sender, monkeypatch, NOW)
    assert not sent and sender.sent == [ARRIVED] and row(conn, arrived_token(conn))["status"] == "sent"
    assert lines(conn) == []


def failed_with_an_alert_then_pressed_again(conn, chat_row, monkeypatch, sender, card):
    """The review of tails-answers, D1: «можно забирать» refused by 402 to the end (the sync's alert with the text to copy), then a second
    «приехал» under the same mark gets a 402 again or no answer: the sync settles it later."""
    sent, _note = arrived(conn, card, sender, monkeypatch, NOW)
    for minute in (1, 2, 3):
        settle(conn, sender, at(61 * minute))
    token = arrived_token(conn)
    assert row(conn, token)["status"] == "failed"
    [alert] = [line for line in lines(conn) if line["message_id"].startswith("send-")]
    assert alert["status"] == "new" and order_manager.COPY_MARK + ARRIVED in alert["text"]
    sent, _note = arrived(conn, card, sender, monkeypatch, at(600))
    assert not sent
    return token


def test_the_alert_of_an_earlier_try_is_answered_when_the_sync_sends_the_new_press_again(env, monkeypatch):
    conn, chat_row, card = env
    sender = Refusing(*[refused402() for _ in range(5)])
    token = failed_with_an_alert_then_pressed_again(conn, chat_row, monkeypatch, sender, card)
    assert row(conn, token)["status"] == "retry"
    assert settle(conn, sender, at(661)) == {"sent": 1} and sender.sent == [ARRIVED]
    assert all(line["status"] == "answered" for line in lines(conn))
    assert order_manager.new_count(conn) == 0


def test_the_alert_of_an_earlier_try_is_answered_when_the_sync_finds_the_new_press_in_the_chat(env, monkeypatch):
    conn, chat_row, card = env
    sender = Refusing(*[refused402() for _ in range(4)], TimeoutError())
    token = failed_with_an_alert_then_pressed_again(conn, chat_row, monkeypatch, sender, card)
    assert row(conn, token)["status"] == "unknown"
    sender.history = [out("m-late", ARRIVED, created=NOW_TS + 605)]                               # it had gone after all
    assert settle(conn, sender, at(661)) == {"sent": 1} and sender.sent == []
    assert all(line["status"] == "answered" for line in lines(conn))


# --- 3: a send the sync stopped for a closed order answers its caller's line ------------------------------------------------------------

STOPPED = "Итог: система больше не повторяет: заказ в «Отмена / невыкуп» — писать клиенту не нужно."


def test_a_402_of_an_arrived_whose_order_was_cancelled_answers_its_line(env, monkeypatch):
    from test_send_settle2 import to_stage
    conn, chat_row, card = env
    sender = Refusing(refused402())
    sent, _note = arrived(conn, card, sender, monkeypatch, NOW)
    [line] = lines(conn)
    assert not sent and line["status"] == "new" and "повтор" in line["text"]                 # «…система повторит сама»
    to_stage(conn, card, "cancel")
    assert settle(conn, sender, at(61)) == {"stopped": 1} and sender.sent == []
    [line] = lines(conn)
    assert line["status"] == "answered" and line["answer"] == STOPPED
    assert order_manager.new_count(conn) == 0


def test_an_unknown_arrived_not_in_the_chat_whose_order_was_cancelled_answers_its_line(env, monkeypatch):
    from test_send_settle2 import to_stage
    conn, chat_row, card = env
    sender = Refusing(TimeoutError())
    sent, _note = arrived(conn, card, sender, monkeypatch, NOW)
    assert not sent and lines(conn)[0]["status"] == "new"
    to_stage(conn, card, "cancel")
    settle(conn, sender, at(61))
    settle(conn, sender, at(301))
    assert sender.sent == [] and row(conn, arrived_token(conn))["status"] == "failed"
    [line] = lines(conn)
    assert line["status"] == "answered" and line["answer"] == STOPPED


def test_a_402_of_an_arrived_of_an_open_order_keeps_its_line_until_it_goes(env, monkeypatch):
    conn, chat_row, card = env
    sender = Refusing(refused402(), refused402())
    arrived(conn, card, sender, monkeypatch, NOW)
    assert settle(conn, sender, at(61)) == {"retry": 1}
    assert lines(conn)[0]["status"] == "new"
    assert settle(conn, sender, at(122)) == {"sent": 1} and sender.sent == [ARRIVED]
    [line] = lines(conn)
    assert line["status"] == "answered" and line["answer"].startswith("Итог: сообщение клиенту дошло (повтор после отказа Авито")
