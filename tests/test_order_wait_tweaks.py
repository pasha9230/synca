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


# --- 4. one dot ----------------------------------------------------------------------------------------------------------------------------------

def test_the_body_of_the_task_has_no_double_dot(env):
    conn, _row = env
    card = waiting(env, with_chat=False)
    run(conn, Sender(), [tire()])
    [task] = tasks(conn, card)
    assert "4 шт. В наличии" in task["body"] and ".." not in task["body"]
