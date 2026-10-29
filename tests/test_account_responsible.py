"""«Ответственный» of an Avito account (the owner 2026-10-29): the new orders of the account (a chat, a call, by hand with its chat) get the
person; the old ones stay; a client or a point is never chosen; «Чаты» opens the account the person answers for."""
from __future__ import annotations

import pytest

from test_chat_order_defaults import FakeClient, NOW, call, chat_of, setup
from tires_sync import calls, chat_deals as deals, connections, orders, users


def test_a_chat_of_the_account_gets_its_responsible_first(tmp_path):
    conn, acc, pt = setup(tmp_path)
    orders.set_default_responsible(conn, "worker2")
    users.add_user(conn, "yun", "pass-word-1", NOW, role="point", point_id=pt["yunona"])
    users.add_user(conn, "masha", "pass-word-1", NOW, role="chats")
    before = deals.create_deal(conn, chat_of(conn, acc["main"], [("in", "Здравствуйте")]), NOW, "pavel")
    staff = ["owner", "masha"]
    connections.set_responsible(conn, acc["main"], "masha", staff, NOW, "owner")
    connections.set_responsible(conn, acc["yunona"], "masha", staff, NOW, "owner")
    assert orders.get_card(conn, before)["responsible"] == "worker2"                       # the old orders stay as they are
    main = deals.create_deal(conn, chat_of(conn, acc["main"], [("in", "Есть шины?")], chat_id="c2"), NOW, "pavel")
    point = deals.create_deal(conn, chat_of(conn, acc["yunona"], [("in", "Здравствуйте")], chat_id="c3"), NOW, "pavel")
    assert orders.get_card(conn, main)["responsible"] == "masha"
    assert orders.get_card(conn, point)["responsible"] == "masha"                          # the account's first, then the point's manager


def test_a_call_of_the_account_gets_its_responsible(tmp_path):
    conn, acc, pt = setup(tmp_path)
    users.add_user(conn, "masha", "pass-word-1", NOW, role="chats")
    connections.set_responsible(conn, acc["main"], "masha", ["masha"], NOW, "owner")
    calls.sync_account(conn, FakeClient([[call(1)]]), acc["main"], NOW, lambda s: None)
    card = orders.get_card(conn, calls.create_order(conn, calls.list_calls(conn)[0]["id"], NOW, "pavel"))
    assert card["responsible"] == "masha"


def test_a_switched_off_person_or_one_become_a_point_is_not_given(tmp_path):
    conn, acc, pt = setup(tmp_path)
    orders.set_default_responsible(conn, "worker2")
    users.add_user(conn, "masha", "pass-word-1", NOW, role="chats")
    connections.set_responsible(conn, acc["main"], "masha", ["masha"], NOW, "owner")
    users.set_active(conn, "masha", False, NOW, "owner")
    assert connections.account_responsible(conn, acc["main"]) == ""
    card = deals.create_deal(conn, chat_of(conn, acc["main"], [("in", "Здравствуйте")]), NOW, "pavel")
    assert orders.get_card(conn, card)["responsible"] == "worker2"


@pytest.mark.parametrize("login", ["buyer", "yun", "nobody"])
def test_only_a_person_of_the_staff_list_is_saved(tmp_path, login):
    conn, acc, pt = setup(tmp_path)
    with pytest.raises(ValueError):
        connections.set_responsible(conn, acc["main"], login, ["owner", "masha"], NOW, "owner")
    assert connections.get_responsible(conn, acc["main"]) == ""


def test_the_chats_open_the_one_account_of_the_person(tmp_path):
    conn, acc, pt = setup(tmp_path)
    conn.execute("UPDATE account SET chats_enabled = 1")
    conn.commit()
    assert connections.chats_default_account(conn, "masha") is None
    connections.set_responsible(conn, acc["main"], "masha", ["masha"], NOW, "owner")
    assert connections.chats_default_account(conn, "masha") == acc["main"]
    connections.set_responsible(conn, acc["yunona"], "masha", ["masha"], NOW, "owner")
    assert connections.chats_default_account(conn, "masha") is None                        # several: all, as before
    assert connections.chats_default_account(conn, "other") is None
