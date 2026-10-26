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

