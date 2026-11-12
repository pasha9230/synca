"""The owner 2026-11-12: an unclear answer to the reminder of the reservation brings the client one question («Уточните, пожалуйста…»,
«Шаблоны сообщений»). Unclear: a word of the lists right after «не» (it did not count for that), or words of two different outcomes in one
message. Only in the 8 hours after the reminder, only for a message after it, once per stay; the 8 hours stay as they were. The answer to the
question is read by the same lists. A question that did not go: a red record and a line in «Входящие», never sent again. Nothing goes to VK.
No real send: the transports are fakes, the data is made up."""
from __future__ import annotations

import pytest

from test_order_reserve import card_at, chat_of, db
from test_reserve_answers import END, events, run
from test_reserve_date import Notify, said, told
from test_reserve_noon import DAY, Deliver, code, read_chats
from tires_sync import chats, message_texts as mt, order_reserve as reserve, orders, vk_send

QUESTION = mt.BY_KEY[mt.RESERVE_CLARIFY]["default"]


@pytest.fixture(autouse=True)
def no_vk(monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("в ВК ничего не уходит")
    monkeypatch.setattr(vk_send, "send_message", refuse, raising=False)
    monkeypatch.setattr(vk_send, "sender_for", refuse)


def questions(deliver):
    return [(chat, text) for chat, text, token in deliver.sent if token.startswith("reserve-clarify-")]


def records(conn, cid):
    return events(conn, cid, "[резерв:уточнение:")


def test_the_text_is_the_owners_and_lives_in_the_templates(tmp_path):
    assert QUESTION == ("Уточните, пожалуйста: вы уже забрали заказ? Напишите «забрал», если забрали, или «продлить», если нужно ещё два дня.")
    item = mt.BY_KEY[mt.RESERVE_CLARIFY]
    assert item["to"] == "client" and item["audience"] == "client" and mt.check(item, item["default"])[1] == []


@pytest.mark.parametrize("text", ["не забрал", "Не, забрал", "пока не взял", "купил бы, но продлите", "Забрал. Или продлите, не знаю"])
def test_an_unclear_answer_brings_one_question(tmp_path, text):
    conn, cid, chat, deliver, notify = told(tmp_path)
    said(conn, chat, DAY + "T13:00:00", text)
    run(conn, DAY + "T13:01:00", deliver, notify)
    assert questions(deliver) == [(chat, QUESTION)]
    [record] = records(conn, cid)
    assert record["level"] == "green" and "Клиенту отправлено" in record["text"] and text[:20] in record["text"]
    assert code(conn, cid) == "at_point" and orders.get_card(conn, cid)["reserve_until"] == DAY


@pytest.mark.parametrize("text", ["ок", "спасибо", "подумаю", "не знаю пока"])
def test_an_answer_without_words_of_the_lists_brings_no_question(tmp_path, text):
    conn, cid, chat, deliver, notify = told(tmp_path)
    said(conn, chat, DAY + "T13:00:00", text)
    run(conn, DAY + "T13:01:00", deliver, notify)
    assert questions(deliver) == [] and records(conn, cid) == []


@pytest.mark.parametrize("text", ["не забрал, продлите", "не нужно, отказ"])
def test_a_clear_outcome_beside_a_negated_word_is_an_answer_not_a_question(tmp_path, text):
    conn, cid, chat, deliver, notify = told(tmp_path)
    said(conn, chat, DAY + "T13:00:00", text)
    run(conn, DAY + "T13:01:00", deliver, notify)
    assert questions(deliver) == []


def test_unclear_words_are_told_apart():
    lists = reserve.WORDS_DEFAULT
    assert reserve.unclear("не забрал", lists) and reserve.unclear("купил бы, но продлите", lists)
    assert not reserve.unclear("забрал", lists) and not reserve.unclear("ок", lists) and not reserve.unclear("не забрал, продлите", lists)


def test_the_question_goes_once_per_stay(tmp_path):
    conn, cid, chat, deliver, notify = told(tmp_path)
    said(conn, chat, DAY + "T13:00:00", "не забрал")
    run(conn, DAY + "T13:01:00", deliver, notify)
    said(conn, chat, DAY + "T14:00:00", "купил бы, но продлите")
    run(conn, DAY + "T14:01:00", deliver, notify)
    run(conn, DAY + "T14:02:00", deliver, notify)                                     # the same tick again
    assert len(questions(deliver)) == 1 and len(records(conn, cid)) == 1


def test_the_clients_took_after_the_question_counts(tmp_path):
    conn, cid, chat, deliver, notify = told(tmp_path)
    said(conn, chat, DAY + "T13:00:00", "пока не взял")
    run(conn, DAY + "T13:01:00", deliver, notify)
    said(conn, chat, DAY + "T13:02:00", QUESTION, direction="out")                   # our question as the sync keeps it
    said(conn, chat, DAY + "T13:30:00", "забрал")
    run(conn, DAY + "T13:31:00", deliver, notify)
    assert reserve.took_open(conn, orders.get_card(conn, cid))
    run(conn, END, deliver, notify)
    assert code(conn, cid) == "at_point"                                             # «забрал»: no move by itself


def test_the_clients_extend_after_the_question_counts(tmp_path):
    conn, cid, chat, deliver, notify = told(tmp_path)
    said(conn, chat, DAY + "T13:00:00", "не забрал")
    run(conn, DAY + "T13:01:00", deliver, notify)
    said(conn, chat, DAY + "T13:30:00", "продлить")
    run(conn, DAY + "T13:31:00", deliver, notify)
    assert orders.get_card(conn, cid)["reserve_until"] == "2026-10-20"


def test_the_eight_hours_do_not_move_after_the_question(tmp_path):
    conn, cid, chat, deliver, notify = told(tmp_path)
    said(conn, chat, DAY + "T20:00:00", "не забрал")
    run(conn, DAY + "T20:01:00", deliver, notify)
    assert len(questions(deliver)) == 1
    run(conn, DAY + "T20:29:59", deliver, notify)
    assert code(conn, cid) == "at_point"
    run(conn, END, deliver, notify)
    assert code(conn, cid) == "oth" and orders.get_card(conn, cid)["cancel_reason"] == reserve.NO_ANSWER_REASON


def test_no_question_after_20_30(tmp_path):
    conn, cid, chat, deliver, notify = told(tmp_path)
    said(conn, chat, DAY + "T20:20:00", "не забрал")                                  # written in the 8 hours, read only after them
    run(conn, DAY + "T20:35:00", deliver, notify)
    assert questions(deliver) == [] and records(conn, cid) == []
    assert code(conn, cid) == "oth"
    said(conn, chat, DAY + "T21:00:00", "не забрал")
    run(conn, DAY + "T21:01:00", deliver, notify)
    assert questions(deliver) == []


def test_a_message_before_the_reminder_brings_no_question(tmp_path):
    conn, cid, chat, deliver, notify = told(tmp_path)
    said(conn, chat, DAY + "T12:29:00", "не забрал")
    run(conn, DAY + "T13:01:00", deliver, notify)
    assert questions(deliver) == []


def test_a_question_that_did_not_go_is_recorded_put_in_the_inbox_and_never_sent_again(tmp_path):
    conn, cid, chat, deliver, notify = told(tmp_path)
    deliver.error = RuntimeError("сеть недоступна")
    said(conn, chat, DAY + "T13:00:00", "не забрал")
    run(conn, DAY + "T13:01:00", deliver, notify)
    [record] = records(conn, cid)
    assert record["level"] == "red" and "не отправлено" in record["text"] and "сеть недоступна" in record["text"]
    inbox = conn.execute("SELECT text, status FROM manager_message WHERE message_id LIKE 'reserve-clarify-%'").fetchall()
    assert len(inbox) == 1 and inbox[0][1] == "new" and "Отправьте вручную" in inbox[0][0] and QUESTION in inbox[0][0]
    deliver.error = None
    said(conn, chat, DAY + "T14:00:00", "купил бы, но продлите")
    run(conn, DAY + "T14:01:00", deliver, notify)
    run(conn, DAY + "T15:00:00", deliver, notify)
    assert questions(deliver) == [] and len(records(conn, cid)) == 1
    assert conn.execute("SELECT COUNT(*) FROM manager_message WHERE message_id LIKE 'reserve-clarify-%'").fetchone()[0] == 1


def test_the_question_goes_with_the_owners_text(tmp_path):
    conn, cid, chat, deliver, notify = told(tmp_path)
    mt.save(conn, mt.RESERVE_CLARIFY, "Забрали заказ или продлить резерв?", "owner", DAY + "T12:00:00")
    said(conn, chat, DAY + "T13:00:00", "не забрал")
    run(conn, DAY + "T13:01:00", deliver, notify)
    assert questions(deliver) == [(chat, "Забрали заказ или продлить резерв?")]


def test_an_order_without_a_chat_gets_no_question(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    deliver, notify = Deliver(conn), Notify()
    run(conn, DAY + "T12:30:00", deliver, notify)
    read_chats(conn, "2099-01-01T00:00:00")
    run(conn, DAY + "T13:01:00", deliver, notify)
    assert questions(deliver) == [] and records(conn, cid) == []
    assert conn.execute("SELECT COUNT(*) FROM chat_send_log WHERE token LIKE 'reserve-clarify-%'").fetchone()[0] == 0


class Retry(Deliver):
    """Avito refused with a 402: the sync sends it again by itself (chat_send_log «retry»), nobody sends it by hand."""

    def __call__(self, chat_row, text, token):
        if token.startswith("reserve-clarify-"):
            self.conn.execute("INSERT INTO chat_send_log (token, chat_row, at, author, status) VALUES (?, ?, ?, 'система', 'retry')",
                              (token, chat_row, self.at))
            raise chats.ChatSendRetry("Авито отказал (402), система повторит сама.")
        return super().__call__(chat_row, text, token)


def test_a_402_question_is_left_to_the_sync_and_never_asked_by_hand(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    chat, deliver, notify = chat_of(conn, cid), Retry(conn), Notify()
    run(conn, DAY + "T12:30:00", deliver, notify)
    read_chats(conn, "2099-01-01T00:00:00")
    said(conn, chat, DAY + "T13:00:00", "не забрал")
    run(conn, DAY + "T13:01:00", deliver, notify)
    [record] = records(conn, cid)
    assert record["level"] == "yellow" and "402" in record["text"] and "вручную" not in record["text"]
    [(line,)] = conn.execute("SELECT text FROM manager_message WHERE message_id LIKE 'reserve-clarify-%'").fetchall()
    assert "402" in line and "Отправьте вручную" not in line
