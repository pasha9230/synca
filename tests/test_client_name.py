"""The client's name on a deal made from a chat is the Avito name only, the ad stays in the comment."""
from __future__ import annotations

import pytest

from tires_sync import chat_deals as deals, chats, orders
from test_chat_deals import NOW, setup, sync


def test_a_deal_from_a_chat_is_named_with_the_client_only(tmp_path):
    conn, account, fake = setup(tmp_path)
    fake.add("c1", "Татьяна", [("in", "Здравствуйте, это актуально?")])
    sync(conn, account, fake)
    [chat] = chats.list_chats(conn)
    card = orders.get_card(conn, chat["order_id"])
    assert card["client_name"] == "Татьяна" and "·" not in card["client_name"]
    assert card["comment"].startswith("Объявление: Ikon 185/65 R15 https://www.avito.ru/x_111")


def test_a_chat_without_a_name_makes_a_nameless_deal_and_the_ad_is_not_used_as_the_name(tmp_path):
    conn, account, fake = setup(tmp_path)
    fake.add("c1", "Татьяна", [("in", "Здравствуйте")])
    sync(conn, account, fake)
    [chat] = chats.list_chats(conn)
    conn.execute("UPDATE avito_chat SET order_id = NULL, peer_name = '' WHERE id = ?", (chat["id"],))
    conn.commit()
    card = orders.get_card(conn, deals.create_deal(conn, chat["id"], NOW))
    assert card["client_name"] == "" and "Ikon" in card["comment"]


def test_an_order_from_a_chat_with_only_an_ad_title_still_asks_for_a_name(tmp_path):
    from test_order_wait_chat import chat_row, FORM
    conn, row = chat_row(tmp_path)
    conn.execute("UPDATE avito_chat SET peer_name = '', item_title = 'Ikon 185/65 R15' WHERE id = ?", (row,))
    conn.commit()
    with pytest.raises(orders.OrderError, match="нет имени клиента"):
        deals.wait_from_chat(conn, row, FORM, [], NOW, "pavel")
    assert conn.execute("SELECT COUNT(*) FROM order_card").fetchone()[0] == 0
