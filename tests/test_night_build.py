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
