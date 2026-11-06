"""«Ожидает наличие», the owner 2026-11-05: «Клиент молчит» also after the words a manager sent from the form of the task; an answer of the
client in another chat of the order or of the client, or a call of the client, is an answer; «Клиент ответил» closes the task; the price of a
tire in the message is never below the price of its ad in the account of the chat; one dot in the body of the manager's task."""
from __future__ import annotations

import pytest

from test_order_wait_auto import NOW, WANT, Sender, env, price_of, run, said, silent_tasks, tasks, tire, told, unread, waiting  # noqa: F401
from tires_sync import chats, order_engine as engine, order_wait, orders, store


def manual(env):
    """The client waits for an answer in the chat: the message does not go by itself, the task with the form does; the manager sends its words."""
    conn, chat_row = env
    card = waiting(env)
    unread(conn, chat_row)
    run(conn, Sender(), [tire()])
    [task] = tasks(conn, card)
    assert task["title"] == order_wait.TITLE_EXACT
    chats.send_reply(conn, Sender(), chat_row, "Есть ваши шины, 8 500 ₽ за штуку.", "o%dt%d" % (card, task["id"]), "2026-11-04T12:30:00", "worker2")
    return card


def account_of(conn, chat_row):
    return conn.execute("SELECT account_id FROM avito_chat WHERE id = ?", (chat_row,)).fetchone()[0]


def published(conn, chat_row, goods_id, price):
    conn.execute("INSERT INTO published_state (account_id, goods_id, price, base_price, point, snapshot) VALUES (?, ?, ?, 0, 'x', '')",
                 (account_of(conn, chat_row), goods_id, price))
    conn.commit()


def other_chat(conn, chat_row, order_id=None):
    row = conn.execute("INSERT INTO avito_chat (account_id, chat_id, order_id) VALUES (?, 'c2', ?)", (account_of(conn, chat_row), order_id)).lastrowid
    conn.commit()
    return row


# --- 1. «Клиент молчит» after the words sent by hand -----------------------------------------------------------------------------------------

def test_the_words_sent_from_the_form_start_the_three_days_too(env):
    conn, _row = env
    card = manual(env)
    assert order_wait.silent(conn, "2026-11-06T12:00:00") == 0
    assert order_wait.silent(conn, "2026-11-07T12:00:00") == 1
    [task] = silent_tasks(conn, card)
    assert "вручную" in task["body"] and "04.11.2026" in task["body"]
    assert "не придержаны" not in task["body"] and "придержаны за этим заказом, пока менеджер не снимет" in task["body"]   # the owner 2026-11-06
    assert task["arg"] == order_wait.BY_HAND and "«Клиент отказался»" in task["body"]
    assert order_wait.silent(conn, "2026-11-08T12:00:00") == 0                                  # once per stay


def test_the_words_by_hand_answered_in_the_chat_give_no_task(env):
    conn, chat_row = env
    card = manual(env)
    said(conn, chat_row, "r1", "2026-11-05T09:00:00")
    assert order_wait.silent(conn, "2026-11-07T12:00:00") == 0 and silent_tasks(conn, card) == []


def test_a_form_not_sent_starts_nothing(env):
    conn, chat_row = env
    card = waiting(env)
    unread(conn, chat_row)
    run(conn, Sender(), [tire()])
    assert order_wait.silent(conn, "2026-11-07T12:00:00") == 0 and silent_tasks(conn, card) == []


# --- 2. an answer elsewhere -------------------------------------------------------------------------------------------------------------------

def test_an_answer_in_another_chat_of_the_order_is_an_answer(env):
    conn, chat_row = env
    card, _sender = told(env)
    said(conn, other_chat(conn, chat_row, card), "r1", "2026-11-05T09:00:00")
    assert order_wait.silent(conn, "2026-11-07T12:00:00") == 0 and silent_tasks(conn, card) == []


def test_an_answer_in_another_chat_of_the_client_is_an_answer(env):
    conn, chat_row = env
    card, _sender = told(env)
    other = other_chat(conn, chat_row)
    client = conn.execute("INSERT INTO client (name, note, created_at, created_by) VALUES ('Иван', '', ?, 'x')", (NOW,)).lastrowid
    conn.execute("INSERT INTO client_order (order_id, client_id) VALUES (?, ?)", (card, client))
    conn.execute("INSERT INTO client_chat (chat_row, client_id) VALUES (?, ?)", (other, client))
    conn.commit()
    said(conn, other, "r1", "2026-11-05T09:00:00")
    assert order_wait.silent(conn, "2026-11-07T12:00:00") == 0 and silent_tasks(conn, card) == []


def test_an_answer_in_a_chat_of_somebody_else_is_not_an_answer(env):
    conn, chat_row = env
    card, _sender = told(env)
    said(conn, other_chat(conn, chat_row), "r1", "2026-11-05T09:00:00")
    assert order_wait.silent(conn, "2026-11-07T12:00:00") == 1


def call(conn, chat_row, at, phone="+79990000001", order_id=None, call_id=1, talk=60):
    """A call as calls._keep writes it: `answered` is a talk that took place (talkDuration > 0); a missed call has 0."""
    row = conn.execute("INSERT INTO avito_call (account_id, call_id, call_at, buyer_phone, order_id, talk_seconds, answered) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (account_of(conn, chat_row), call_id, at, phone, order_id, talk, 1 if talk > 0 else 0)).lastrowid
    conn.commit()
    return row


@pytest.mark.parametrize("how", ("phone", "order", "client"))
def test_a_call_of_the_client_after_the_message_is_an_answer(env, how):
    conn, chat_row = env
    card, _sender = told(env)
    conn.execute("UPDATE order_card SET client_phone = '+7 999 000-00-01' WHERE id = ?", (card,))
    conn.commit()
    if how == "phone":
        call(conn, chat_row, "2026-11-05T10:00:00", phone="89990000001")
    elif how == "order":
        call(conn, chat_row, "2026-11-05T10:00:00", phone="+79995550000", order_id=card)
    else:
        row = call(conn, chat_row, "2026-11-05T10:00:00", phone="+79995550000")
        client = conn.execute("INSERT INTO client (name, note, created_at, created_by) VALUES ('Иван', '', ?, 'x')", (NOW,)).lastrowid
        conn.execute("INSERT INTO client_order (order_id, client_id) VALUES (?, ?)", (card, client))
        conn.execute("INSERT INTO client_call (call_row, client_id) VALUES (?, ?)", (row, client))
        conn.commit()
    assert order_wait.silent(conn, "2026-11-07T12:00:00") == 0 and silent_tasks(conn, card) == []


def test_a_call_before_the_message_or_of_another_number_is_not_an_answer(env):
    conn, chat_row = env
    card, _sender = told(env)
    conn.execute("UPDATE order_card SET client_phone = '+79990000001' WHERE id = ?", (card,))
    conn.commit()
    call(conn, chat_row, "2026-11-04T11:00:00", call_id=1)                                      # before the message
    call(conn, chat_row, "2026-11-05T10:00:00", phone="+79995550000", call_id=2)                 # somebody else
    assert order_wait.silent(conn, "2026-11-07T12:00:00") == 1


@pytest.mark.parametrize("how", ("phone", "order", "client"))
def test_a_missed_call_of_the_client_is_not_an_answer(env, how):
    """The owner 2026-11-06: only a talk that took place is an answer; a missed call is not."""
    conn, chat_row = env
    card, _sender = told(env)
    conn.execute("UPDATE order_card SET client_phone = '+79990000001' WHERE id = ?", (card,))
    conn.commit()
    if how == "phone":
        call(conn, chat_row, "2026-11-05T10:00:00", talk=0)
    elif how == "order":
        call(conn, chat_row, "2026-11-05T10:00:00", phone="+79995550000", order_id=card, talk=0)
    else:
        row = call(conn, chat_row, "2026-11-05T10:00:00", phone="+79995550000", talk=0)
        client = conn.execute("INSERT INTO client (name, note, created_at, created_by) VALUES ('Иван', '', ?, 'x')", (NOW,)).lastrowid
        conn.execute("INSERT INTO client_order (order_id, client_id) VALUES (?, ?)", (card, client))
        conn.execute("INSERT INTO client_call (call_row, client_id) VALUES (?, ?)", (row, client))
        conn.commit()
    assert order_wait.silent(conn, "2026-11-07T12:00:00") == 1 and len(silent_tasks(conn, card)) == 1


def test_the_button_client_answered_closes_the_task_writes_the_journal_and_no_task_comes_again(env):
    conn, _row = env
    card, _sender = told(env)
    assert order_wait.silent(conn, "2026-11-07T12:00:00") == 1
    [task] = silent_tasks(conn, card)
    order_wait.client_answered(conn, task["id"], "2026-11-07T12:10:00", "worker2")
    task = engine.get_task(conn, task["id"])
    assert task["status"] == "done" and "клиент ответил" in task["result"].lower()
    assert [e for e in orders.list_events(conn, card) if "Клиент ответил" in e["text"] and e["author"] == "worker2"]
    assert order_wait.silent(conn, "2026-11-08T12:00:00") == 0 and len(silent_tasks(conn, card)) == 1
    with pytest.raises(orders.OrderError):                                                      # a second press: nothing twice
        order_wait.client_answered(conn, task["id"], "2026-11-07T12:11:00", "worker2")


def test_the_button_is_only_for_the_task_of_the_silent_client(env):
    conn, _row = env
    card = waiting(env, with_chat=False)
    run(conn, Sender(), [tire()])
    [task] = tasks(conn, card)
    with pytest.raises(orders.OrderError):
        order_wait.client_answered(conn, task["id"], "2026-11-07T12:10:00", "worker2")
    assert engine.get_task(conn, task["id"])["status"] == "pending"


def test_the_card_shows_the_button_and_the_press_closes_the_task(tmp_path):
    from panel_helpers import post
    from test_order_wait_pages import WANT as PAGE_WANT, new_card, setup, text
    _app, client, tok, conn = setup(tmp_path)
    card = new_card(conn)
    order_wait.add_want(conn, card, PAGE_WANT, "2026-10-15T12:00:00", "owner")
    orders.move_card(conn, card, {s["code"]: s["id"] for s in orders.list_stages(conn)}["wait"], "2026-10-15T12:00:00", "owner")
    c = orders.get_card(conn, card)
    task_id = conn.execute("INSERT INTO order_task (order_id, rule_id, entered_at, kind, arg, stage_id, status, title, body, result, created_at) "
                           "VALUES (?, ?, ?, 'remind', '', ?, 'pending', ?, 'тело', '', ?)",
                           (card, order_wait.TASK_SILENT, c["entered_at"], c["stage_id"], order_wait.TITLE_SILENT, "2026-10-15T12:00:00")).lastrowid
    conn.commit()
    page = text(client, "/orders/%d" % card)
    assert 'name="answered"' in page and "Клиент ответил</button>" in page
    assert post(client, "/orders/%d/task/%d/run" % (card, task_id), tok, answered="1").status_code == 302
    assert engine.get_task(conn, task_id)["status"] == "done" and "ответил" in engine.get_task(conn, task_id)["result"]
    assert [e for e in orders.list_events(conn, card) if "Клиент ответил" in e["text"]]


# --- 3. the price is never below the ad -------------------------------------------------------------------------------------------------------

def test_a_price_below_the_ad_names_the_price_of_the_ad(env):
    conn, chat_row = env
    card, sender = waiting(env), Sender()
    published(conn, chat_row, "100", 9100)
    run(conn, sender, [tire()])
    assert sender.sent and "9 100 ₽" in sender.sent[0] and "8 500" not in sender.sent[0]
    assert order_wait.form_text(conn, orders.get_card(conn, card), [tire()], price_of, chat_row).count("9 100 ₽") == 1


def test_a_price_above_the_ad_stays(env):
    conn, chat_row = env
    card, sender = waiting(env), Sender()
    published(conn, chat_row, "100", 7000)
    run(conn, sender, [tire()])
    assert "8 500 ₽" in sender.sent[0]


def test_the_ad_of_another_position_of_the_same_tire_counts_and_held_tires_do_not_stop_the_form(env):
    conn, chat_row = env
    published(conn, chat_row, "100", 9300)                                                      # the WebMIM position the client saw
    first = waiting(env)
    run(conn, Sender(), [tire()])                                                               # the WebMIM tires go to the first order
    second = waiting(env)
    conn.execute("UPDATE avito_chat SET order_id = ? WHERE id = ?", (second, chat_row))
    conn.commit()
    words = order_wait.form_text(conn, orders.get_card(conn, second), [tire(), s2()], price_of, chat_row)
    assert "9 300 ₽" in words and orders.get_card(conn, first)["stage"]["code"] == "wait"


def s2(**changes):
    return tire(**{"goods_id": "s2-200", "brand_key": "S2:IKON TYRES", "code": "TS2", "name_orig": "S2 Ikon 205/55 R16", **changes})


def test_without_the_price_of_the_ad_the_price_stays_and_the_journal_says_so(env):
    conn, chat_row = env
    card, sender = waiting(env), Sender()
    run(conn, sender, [tire()])
    assert "8 500 ₽" in sender.sent[0]
    notes = [e["text"] for e in orders.list_events(conn, card) if "цена объявления" in e["text"].lower()]
    assert len(notes) == 1 and "8 500" in notes[0]


def test_the_note_of_an_unknown_ad_price_is_not_written_when_the_send_failed(env):
    """The owner 2026-11-06: the note goes to the journal after the message went, not before."""
    conn, _row = env
    card = waiting(env)

    def refuse(row, text, token):
        raise chats.ChatError("Авито отказал")
    order_wait.check(conn, [tire()], NOW, deliver=refuse, price_of=price_of)
    assert not [e for e in orders.list_events(conn, card) if "цена объявления" in e["text"].lower()]


def test_the_note_of_an_unknown_ad_price_comes_after_the_message(env):
    conn, _row = env
    card, sender = waiting(env), Sender()
    run(conn, sender, [tire()])
    texts = [e["text"] for e in sorted(orders.list_events(conn, card), key=lambda e: e["id"])]
    told_at = next(i for i, t in enumerate(texts) if t.startswith(order_wait.TOLD))
    note_at = next(i for i, t in enumerate(texts) if "цена объявления" in t.lower())
    assert told_at < note_at


def test_the_note_of_an_unknown_ad_price_waits_for_an_unknown_send_and_comes_with_its_delivery(env):
    """The send may have gone (a timeout): no note yet; the sync finds the words in the chat: the note comes after the message, once."""
    from datetime import datetime

    from test_chat_send_refusal import Refusing
    from test_send_settle import out
    conn, _row = env
    card = waiting(env)
    sender = Refusing(TimeoutError())
    order_wait.check(conn, [tire()], NOW, deliver=lambda row, text, token: chats.send_reply(conn, sender, row, text, token, NOW, "система"), price_of=price_of)
    assert not [e for e in orders.list_events(conn, card) if "цена объявления" in e["text"].lower()]
    words = conn.execute("SELECT text FROM chat_send_log WHERE token LIKE 'wait-%'").fetchone()[0]
    sender.history = [out("m9", words, int(datetime.fromisoformat(NOW).timestamp()) + 5)]
    chats.settle_sends(conn, sender, lambda: sender, 1, "2026-11-04T12:02:00")
    texts = [e["text"] for e in sorted(orders.list_events(conn, card), key=lambda e: e["id"])]
    notes = [i for i, t in enumerate(texts) if "цена объявления" in t.lower()]
    assert len(notes) == 1 and "8 500" in texts[notes[0]]
    assert next(i for i, t in enumerate(texts) if t.startswith(order_wait.TOLD)) < notes[0]
    token = order_wait._token(card, orders.get_card(conn, card)["entered_at"])
    order_wait.delivered(conn, token, "слова", "2026-11-04T12:03:00", "повтор")                  # a second delivery: nothing twice
    assert len([e for e in orders.list_events(conn, card) if "цена объявления" in e["text"].lower()]) == 1


def test_the_ad_of_webmim_only_when_the_message_names_webmim(env):
    """The owner 2026-11-06: «работаем по МИМ, бери только их цену»: the ad of the other supplier's position of the tire is not the floor."""
    conn, chat_row = env
    card, sender = waiting(env), Sender()
    published(conn, chat_row, "100", 9100)                                                      # WebMIM
    published(conn, chat_row, "s2-200", 12000)                                                  # the second supplier
    run(conn, sender, [tire(), s2()])
    assert sender.sent and "9 100 ₽" in sender.sent[0] and "12 000" not in sender.sent[0]
    assert "9 100 ₽" in order_wait.form_text(conn, orders.get_card(conn, card), [tire(), s2()], price_of, chat_row)


def test_webmim_named_without_its_ad_the_other_ad_is_not_taken(env):
    conn, chat_row = env
    card, sender = waiting(env), Sender()
    published(conn, chat_row, "s2-200", 12000)
    run(conn, sender, [tire(), s2()])
    assert "8 500 ₽" in sender.sent[0] and "12 000" not in sender.sent[0]
    notes = [e["text"] for e in orders.list_events(conn, card) if "цена объявления" in e["text"].lower()]
    assert len(notes) == 1 and "TS1" in notes[0]


def test_the_ad_floor_leaves_the_files_of_the_feed_alone(env):
    conn, chat_row = env
    published(conn, chat_row, "100", 9100)
    before = conn.execute("SELECT * FROM published_state").fetchall()
    card = waiting(env)
    run(conn, Sender(), [tire()])
    assert conn.execute("SELECT * FROM published_state").fetchall() == before


# --- 4. one dot ----------------------------------------------------------------------------------------------------------------------------------

def test_the_body_of_the_task_has_no_double_dot(env):
    conn, _row = env
    card = waiting(env, with_chat=False)
    run(conn, Sender(), [tire()])
    [task] = tasks(conn, card)
    assert "4 шт. В наличии" in task["body"] and ".." not in task["body"]


# --- 4. a call in the chat is not an answer; «Клиент отказался» lets the tires go (the owner 2026-11-06) ---------------------------------------

def call_in_chat(conn, chat_row, mid, at, status):
    """A call as Avito puts it in the chat too: a message of type «call» from the client, how it went in content.call.status."""
    from datetime import datetime
    chats._store_message(conn, chat_row, {"id": mid, "created": int(datetime.fromisoformat(at + "+03:00").timestamp()), "direction": "in", "type": "call",
                                          "content": {"call": {"status": status}}, "isRead": False})
    conn.commit()


@pytest.mark.parametrize("how", ("auto", "manual"))
@pytest.mark.parametrize("status", ("missed", "rejected"))
def test_a_missed_or_rejected_call_in_the_chat_is_not_an_answer(env, how, status):
    conn, chat_row = env
    card = told(env)[0] if how == "auto" else manual(env)
    call_in_chat(conn, chat_row, "k1", "2026-11-05T10:00:00", status)
    assert order_wait.replies(conn, "2026-11-05T10:01:00") == 0                                 # not «Клиент ответил на сообщение» either
    assert order_wait.silent(conn, "2026-11-07T12:00:00") == 1 and len(silent_tasks(conn, card)) == 1


@pytest.mark.parametrize("how", ("auto", "manual"))
def test_a_call_that_took_place_is_an_answer_by_the_calls(env, how):
    conn, chat_row = env
    card = told(env)[0] if how == "auto" else manual(env)
    call_in_chat(conn, chat_row, "k1", "2026-11-05T10:00:00", "finished")
    call(conn, chat_row, "2026-11-05T10:00:00", order_id=card)                                  # the panel's calls have the talk
    assert order_wait.silent(conn, "2026-11-07T12:00:00") == 0 and silent_tasks(conn, card) == []


def second_order(env):
    """An order younger than the first for the same tires, with its own chat."""
    conn, chat_row = env
    card = waiting(env)
    other_chat(conn, chat_row, card)
    return card


def test_the_client_refused_lets_the_tires_go_to_the_next_order(env):
    conn, chat_row = env
    first = manual(env)
    second = second_order(env)
    sender = Sender()
    run(conn, sender, [tire()], "2026-11-05T12:00:00")
    assert sender.sent == [] and order_wait.QUEUED_TASK % first in [e["text"] for e in orders.list_events(conn, second)]   # the first holds them
    assert order_wait.silent(conn, "2026-11-07T12:00:00") == 1
    [task] = silent_tasks(conn, first)
    stage_before, tags_before = orders.get_card(conn, first)["stage_id"], orders.get_card(conn, first)["tags"]
    order_wait.client_refused(conn, task["id"], "2026-11-07T12:10:00", "worker2")
    task = engine.get_task(conn, task["id"])
    assert task["status"] == "done" and task["result"] == order_wait.REFUSED
    assert [e for e in orders.list_events(conn, first) if "Клиент отказался" in e["text"] and e["author"] == "worker2"]
    card = orders.get_card(conn, first)
    assert (card["stage_id"], card["tags"]) == (stage_before, tags_before) and sender.sent == []   # nothing else moves, nothing goes
    run(conn, sender, [tire()], "2026-11-07T13:00:00")
    assert len(sender.sent) == 1                                                                # the next order is told at the next pass
    assert [e for e in orders.list_events(conn, second) if e["text"].startswith(order_wait.TOLD)]
    assert [e for e in orders.list_events(conn, first) if e["text"].startswith(order_wait.TOLD)] == []
    run(conn, sender, [tire()], "2026-11-07T14:00:00")
    assert len(sender.sent) == 1                                                                # and the first is not told after it
    with pytest.raises(orders.OrderError):                                                      # a second press: nothing twice
        order_wait.client_refused(conn, task["id"], "2026-11-07T12:11:00", "worker2")


def test_the_client_refused_is_only_after_the_words_sent_by_hand(env):
    conn, _row = env
    card, _sender = told(env)                                                                   # went by itself: held while in the stage
    assert order_wait.silent(conn, "2026-11-07T12:00:00") == 1
    [task] = silent_tasks(conn, card)
    assert task["arg"] != order_wait.BY_HAND and "«Клиент отказался»" not in task["body"]
    with pytest.raises(orders.OrderError):
        order_wait.client_refused(conn, task["id"], "2026-11-07T12:10:00", "worker2")
    assert engine.get_task(conn, task["id"])["status"] == "pending"


def test_the_card_shows_the_button_client_refused_and_the_press_closes_the_task(tmp_path):
    from panel_helpers import post
    from test_order_wait_pages import WANT as PAGE_WANT, new_card, setup, text
    _app, client, tok, conn = setup(tmp_path)
    card = new_card(conn)
    order_wait.add_want(conn, card, PAGE_WANT, "2026-10-15T12:00:00", "owner")
    orders.move_card(conn, card, {s["code"]: s["id"] for s in orders.list_stages(conn)}["wait"], "2026-10-15T12:00:00", "owner")
    c = orders.get_card(conn, card)
    ids = [conn.execute("INSERT INTO order_task (order_id, rule_id, entered_at, kind, arg, stage_id, status, title, body, result, created_at) "
                        "VALUES (?, ?, ?, 'remind', ?, ?, 'pending', ?, 'тело', '', ?)",
                        (card, order_wait.TASK_SILENT, entered, arg, c["stage_id"], order_wait.TITLE_SILENT, "2026-10-15T12:00:00")).lastrowid
           for entered, arg in ((c["entered_at"], order_wait.BY_HAND), ("2026-10-01T00:00:00", ""))]
    conn.commit()
    page = text(client, "/orders/%d" % card)
    assert page.count('name="refused"') == 1 and "Клиент отказался</button>" in page            # only the task after the words sent by hand
    assert post(client, "/orders/%d/task/%d/run" % (card, ids[0]), tok, refused="1").status_code == 302
    assert engine.get_task(conn, ids[0])["result"] == order_wait.REFUSED
    assert [e for e in orders.list_events(conn, card) if "Клиент отказался" in e["text"]]

