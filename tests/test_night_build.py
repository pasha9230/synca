"""The night build (lane/night-build): what two branches only do together. The swap of the client on «На ОТХ» (oth_swap, lane/oth-client-swap) and
«Автосообщение клиенту» (order_autosend, lane/autotask-date): a message set for the old client never goes to the new one."""
from __future__ import annotations

from test_oth_client_swap import NEW_PHONE, _b, chat
from test_oth_sale import LATER, T
from tests.test_order_reserve import card_at, db
from tires_sync import order_autosend, orders, oth_sale, oth_swap


def _set_for_the_old_client(conn, a):
    orders.move_card(conn, a, next(s["id"] for s in orders.list_stages(conn) if s["code"] == "at_point"), T, "o")     # set while at the point
    autosend = order_autosend.add(conn, a, "deal", "2026-10-30T10:00", "Иван, ждём вас завтра", T, "o")
    orders.move_card(conn, a, next(s["id"] for s in orders.list_stages(conn) if s["code"] == "oth"), T, "o")          # then refused
    return autosend


def test_the_new_client_from_the_card_never_gets_the_message_of_the_old_one(tmp_path):
    conn = db(tmp_path)
    a = card_at(conn, "oth", T)
    autosend = _set_for_the_old_client(conn, a)
    assert oth_sale.to_new_client(conn, a, "Пётр", NEW_PHONE, LATER, "o") == a
    got = order_autosend.get(conn, autosend)
    assert got["status"] == "cancelled" and "клиент сменён" in got["reason"]


def test_the_client_of_b_never_gets_the_message_of_the_old_one(tmp_path):
    conn = db(tmp_path)
    a = card_at(conn, "oth", T)
    autosend = _set_for_the_old_client(conn, a)
    b, _ = _b(conn)
    line = orders.get_card(conn, b)["items"][0]
    oth_swap.take_client(conn, b, line["id"], orders.get_card(conn, a)["items"][0]["id"], LATER, "o")
    assert order_autosend.get(conn, autosend)["status"] == "cancelled"



def test_a_send_that_failed_for_the_old_client_leaves_no_task_to_write_to_the_new_one(tmp_path):
    conn = db(tmp_path)
    a = card_at(conn, "oth", T)
    autosend = _set_for_the_old_client(conn, a)
    task = conn.execute("INSERT INTO callback_task (order_id, due_at, created_at, created_by, kind, title) VALUES (?, ?, ?, 'o', 'other', 'Автосообщение не ушло')",
                        (a, T, T)).lastrowid
    conn.execute("UPDATE order_autosend SET status = 'waiting', task_id = ? WHERE id = ?", (task, autosend))
    conn.commit()
    oth_sale.to_new_client(conn, a, "Пётр", NEW_PHONE, LATER, "o")
    assert order_autosend.get(conn, autosend)["status"] == "cancelled"
    assert conn.execute("SELECT cancelled_at FROM callback_task WHERE id = ?", (task,)).fetchone()[0] == LATER


def test_a_send_with_its_mark_is_left_to_its_own_run_one_without_is_cancelled(tmp_path):
    conn = db(tmp_path)
    a = card_at(conn, "oth", T)
    started = _set_for_the_old_client(conn, a)
    fresh = order_autosend.add(conn, a, "deal", "2026-10-30T11:00", "Иван, напоминаем", T, "o")
    conn.execute("UPDATE order_autosend SET status = 'sending' WHERE id IN (?, ?)", (started, fresh))
    row = chat(conn, a)
    conn.execute("INSERT INTO chat_send_log (token, chat_row, at, author, status) VALUES (?, ?, ?, 'o', 'sent')",
                 (order_autosend.get(conn, started)["token"], row, T))
    conn.commit()
    oth_sale.to_new_client(conn, a, "Пётр", NEW_PHONE, LATER, "o")
    assert order_autosend.get(conn, started)["status"] == "sending"           # it went to the old chat already: its own run settles it
    assert order_autosend.get(conn, fresh)["status"] == "cancelled"


def test_a_run_that_lost_its_message_to_the_swap_sends_nothing_and_makes_no_task(tmp_path):
    conn = db(tmp_path)
    a = card_at(conn, "oth", T)
    autosend = _set_for_the_old_client(conn, a)
    item = order_autosend.get(conn, autosend)
    sent = []
    real_get = order_autosend.get
    def swap_in_between(c, i):                                                # the swap commits right after the run claimed it
        got = real_get(c, i)
        if got["status"] == "sending" and not sent:
            sent.append("swapped")
            oth_sale.to_new_client(c, a, "Пётр", NEW_PHONE, LATER, "o")
            return real_get(c, i)
        return got
    import pytest
    mp = pytest.MonkeyPatch()
    mp.setattr(order_autosend, "get", swap_in_between)
    try:
        assert order_autosend._send(conn, item, lambda *a, **k: (_ for _ in ()).throw(AssertionError("sent")), "2026-10-30T10:00:00", False) == ""
    finally:
        mp.undo()
    assert order_autosend.get(conn, autosend)["status"] == "cancelled"
    assert not conn.execute("SELECT 1 FROM callback_task WHERE order_id = ? AND kind = 'other' AND cancelled_at IS NULL", (a,)).fetchone()
