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
