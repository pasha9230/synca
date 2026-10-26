"""The price a position of an order takes by itself is the price of the ad of the Avito account the client came from (the owner 2026-10-26:
«Нужна калькуляция с объявления аккаунта авито, по которому идет обращение»): the account of the chat, of the order's chat, of the order's
point; the main account otherwise. The same calculation as the feed of that account (its markup and its brand's, once), never another's."""
from __future__ import annotations

import re

from test_order_search import setup, found
from panel_helpers import post
from tires_sync import connections, orders
from tires_sync import order_points as points
from tires_sync.models import Season
from tires_sync.pricing import final_price
from tires_sync.settings_store import active_settings

NOW = "2026-10-12T09:00:00"
BASE = {"100": (5000.0, 16), "101": (5000.0, 15)}                                 # NPRICE_DELAYED and diameter of the test unload


def accounts(conn):
    """Two feed accounts with different markups: the main one +1 point, the second +3 and +0.5 more for Ikon."""
    orders.ensure_pipeline(conn, NOW)
    main = connections.create_account(conn, "Основной", NOW, "o")
    other = connections.create_account(conn, "Второй", NOW, "o")
    connections.set_account_pricing(conn, main, 1.0, None, NOW, "o")
    connections.set_account_pricing(conn, other, 3.0, None, NOW, "o")
    connections.set_brand_deltas(conn, other, {"IKON TYRES": 0.5}, NOW, "o")
    conn.commit()
    return main, other


def feed_price(conn, account_id, goods_id):
    """What the feed of this account asks for one tyre: the rule of the account's own settings, rounded as the feed rounds it."""
    base, diameter = BASE[goods_id]
    rule = connections.effective_settings(conn, account_id, active_settings(conn)).pricing[("IKON TYRES", Season.WINTER)]
    return float(final_price(base, rule, diameter))


def chat_of(conn, account_id, goods_id="100", avito_id=7001):
    """A chat of the account about our ad of the position."""
    conn.execute("INSERT INTO avito_ad (account_id, avito_id, ad_id, title, seen_at) VALUES (?,?,?,?,?)", (account_id, avito_id, goods_id, "t", NOW))
    cur = conn.execute("INSERT INTO avito_chat (account_id, chat_id, item_id, item_title, peer_name) VALUES (?,?,?,?,?)",
                       (account_id, "c%d" % avito_id, str(avito_id), "Ikon 205/55 R16", "Клиент"))
    conn.commit()
    return cur.lastrowid


def created(r) -> int:
    assert r.status_code == 302, r.get_data(as_text=True)[:300]
    return int(re.search(r"/orders/(\d+)", r.headers["Location"]).group(1))


def prices(conn, card_id):
    return [(i["goods_id"], i["sale_unit"]) for i in orders.get_card(conn, card_id)["items"]]


def test_the_two_accounts_have_different_feed_prices(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    main, other = accounts(conn)
    assert feed_price(conn, main, "100") != feed_price(conn, other, "100")


def test_a_deal_made_from_a_chat_of_the_second_account_takes_the_price_of_its_ad(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    main, other = accounts(conn)
    chat = chat_of(conn, other)
    post(client, "/chats/%d/create-deal" % chat, tok)
    card_id = conn.execute("SELECT order_id FROM avito_chat WHERE id = ?", (chat,)).fetchone()[0]
    assert prices(conn, card_id) == [("100", feed_price(conn, other, "100"))]


def test_a_deal_of_a_chat_of_the_main_account_takes_its_markup_once(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    main, other = accounts(conn)
    chat = chat_of(conn, main)
    post(client, "/chats/%d/create-deal" % chat, tok)
    card_id = conn.execute("SELECT order_id FROM avito_chat WHERE id = ?", (chat,)).fetchone()[0]
    assert prices(conn, card_id) == [("100", feed_price(conn, main, "100"))]


def test_a_position_added_to_the_order_of_a_chat_is_priced_by_the_chats_account(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    main, other = accounts(conn)
    chat = chat_of(conn, other)
    post(client, "/chats/%d/create-deal" % chat, tok)
    card_id = conn.execute("SELECT order_id FROM avito_chat WHERE id = ?", (chat,)).fetchone()[0]
    hint = client.get("/orders/items/find?q=autograph ice&order=%d" % card_id).get_json()["items"]
    assert [(i["goods_id"], i["price"]) for i in hint] == [("101", feed_price(conn, other, "101"))]      # the price the window puts in the field
    post(client, "/orders/%d/items/add" % card_id, tok, goods_id="101", query="Autograph Ice 9 · 195/65 R15", qty="2")
    post(client, "/chats/%d/items/add" % chat, tok, order_id=str(card_id), goods_id="101", qty="2")
    assert prices(conn, card_id) == [("100", feed_price(conn, other, "100")), ("101", feed_price(conn, other, "101")),
                                     ("101", feed_price(conn, other, "101"))]
    hint = client.get("/orders/items/find?q=autograph ice&chat=%d" % chat).get_json()["items"]          # the search of the chat's page
    assert hint[0]["price"] == feed_price(conn, other, "101")


def test_a_new_order_takes_the_account_of_the_chat_field_or_of_the_point_and_the_main_one_otherwise(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    main, other = accounts(conn)
    chat = chat_of(conn, other)
    by_chat = created(post(client, "/orders/new", tok, client_name="Анна", avito_chat="Ч-%d" % chat,
                           item_goods_id=["100"], item_qty=["4"], item_sale_unit=[""]))
    assert prices(conn, by_chat) == [("100", feed_price(conn, other, "100"))]
    point = points.save_point(conn, {"name": "Гамма", "avito_account_id": other})
    by_point = created(post(client, "/orders/new", tok, client_name="Борис", point_id=str(point), item_goods_id=["100"], item_qty=["4"], item_sale_unit=[""]))
    assert prices(conn, by_point) == [("100", feed_price(conn, other, "100"))]
    plain = created(post(client, "/orders/new", tok, client_name="Вера", item_goods_id=["100"], item_qty=["4"], item_sale_unit=[""]))
    assert prices(conn, plain) == [("100", feed_price(conn, main, "100"))]
    assert found(client, "autograph ice")[0]["price"] == feed_price(conn, main, "101")                 # a window of no account: the main one
    assert client.get("/orders/items/find?q=autograph ice&avito_chat=Ч-%d" % chat).get_json()["items"][0]["price"] == feed_price(conn, other, "101")
    assert client.get("/orders/items/find?q=autograph ice&point_id=%d" % point).get_json()["items"][0]["price"] == feed_price(conn, other, "101")
    page = client.get("/orders/new?goods_id=100").get_data(as_text=True)                                  # «Оформить заказ» from the catalog
    assert 'value="%d"' % feed_price(conn, main, "100") in page
    post(client, "/orders/%d/items/add" % by_point, tok, goods_id="101", query="Autograph Ice 9 · 195/65 R15", qty="2")   # an order of the point, no chat
    assert prices(conn, by_point)[-1] == ("101", feed_price(conn, other, "101"))


def test_a_chat_of_an_account_without_a_feed_is_priced_by_the_main_account(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    main, other = accounts(conn)
    reader = connections.create_account(conn, "Только чаты", NOW, "o")
    connections.set_account_pricing(conn, reader, 5.0, None, NOW, "o")
    connections.set_modes(conn, reader, True, False, NOW, "o")
    conn.commit()
    chat = chat_of(conn, reader)
    post(client, "/chats/%d/create-deal" % chat, tok)
    card_id = conn.execute("SELECT order_id FROM avito_chat WHERE id = ?", (chat,)).fetchone()[0]
    assert prices(conn, card_id) == [("100", feed_price(conn, main, "100"))]


def test_the_prices_of_positions_already_in_an_order_never_change(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    main, other = accounts(conn)
    chat = chat_of(conn, other)
    post(client, "/chats/%d/create-deal" % chat, tok)
    card_id = conn.execute("SELECT order_id FROM avito_chat WHERE id = ?", (chat,)).fetchone()[0]
    post(client, "/orders/%d/items/add" % card_id, tok, goods_id="101", query="Autograph Ice 9 · 195/65 R15", qty="2", sale_unit="4800")
    before = prices(conn, card_id)
    assert before[-1] == ("101", 4800.0)                                                                   # the price typed by hand wins
    connections.set_account_pricing(conn, other, -2.0, None, NOW, "o")                                     # the markup changes later
    conn.commit()
    client.get("/orders/%d" % card_id)
    post(client, "/orders/%d/save" % card_id, tok, comment="перезвонить")
    assert prices(conn, card_id) == before


def published(conn, account_id, goods_id, price):
    """The ad of the position as the last published feed of the account carries it (store.commit_pending writes it)."""
    conn.execute("INSERT INTO published_state (account_id, goods_id, price, base_price, point, location, snapshot) VALUES (?,?,?,?,?,?,?)",
                 (account_id, goods_id, price, 5000.0, "", "supplier", "<Ad/>"))
    conn.commit()


def test_the_price_of_the_published_ad_of_the_account_comes_before_the_calculation(tmp_path, monkeypatch):
    """The lead 2026-10-26: «Создать заказ» in a chat of the second account takes the price of that tyre's ad in the published feed of the
    second account (what the client saw), the calculation only when the account has not published it."""
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    main, other = accounts(conn)
    published(conn, main, "100", 4700)                                                                     # the main account's ad: not this client's
    published(conn, other, "100", 5130)
    chat = chat_of(conn, other)
    post(client, "/chats/%d/create-deal" % chat, tok)
    card_id = conn.execute("SELECT order_id FROM avito_chat WHERE id = ?", (chat,)).fetchone()[0]
    assert prices(conn, card_id) == [("100", 5130.0)]
    assert client.get("/orders/items/find?q=autograph snow&order=%d" % card_id).get_json()["items"][0]["price"] == 5130
    post(client, "/orders/%d/items/add" % card_id, tok, goods_id="101", query="Autograph Ice 9 · 195/65 R15", qty="2")   # not published: calculated
    assert prices(conn, card_id)[-1] == ("101", feed_price(conn, other, "101"))
    plain = created(post(client, "/orders/new", tok, client_name="Вера", item_goods_id=["100"], item_qty=["4"], item_sale_unit=[""]))
    assert prices(conn, plain) == [("100", 4700.0)]


def feed_of(conn, tmp_path, account_id) -> dict:
    """{position: price} of the feed the cycle really builds for the account (the plan it promotes: the <Price> of its ads)."""
    import json
    from datetime import date
    import panel_helpers
    from tires_sync import cycle
    from tires_sync.catalog import parse_reference
    cycle.run_for_accounts(conn, panel_helpers.UNLOAD, parse_reference(panel_helpers.REF_TEXT), date(2026, 10, 12), tmp_path / "feed")
    row = conn.execute("SELECT payload FROM pending_state WHERE account_id = ?", (account_id,)).fetchone()
    return {i["goods_id"]: i["price"] for i in json.loads(row[0]) if i["action"] in ("publish", "update")}


def test_a_deal_of_a_chat_of_the_second_account_costs_what_the_feed_of_that_account_asks(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    main, other = accounts(conn)
    feed = feed_of(conn, tmp_path, other)
    assert feed["100"] != feed_of(conn, tmp_path, main)["100"]
    chat = chat_of(conn, other)
    post(client, "/chats/%d/create-deal" % chat, tok)
    card_id = conn.execute("SELECT order_id FROM avito_chat WHERE id = ?", (chat,)).fetchone()[0]
    assert prices(conn, card_id) == [("100", float(feed["100"]))]


def test_a_price_the_list_put_in_the_field_is_priced_again_by_the_account_of_the_window(tmp_path, monkeypatch):
    """The review 2026-10-26 (D1): the list priced the row before «Чат Авито» or the point was chosen; kept as it is, it is the panel's price,
    so the account of the window prices it when saved. A price changed by hand stays."""
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    main, other = accounts(conn)
    chat = chat_of(conn, other)
    listed = "%d" % feed_price(conn, main, "100")
    kept = created(post(client, "/orders/new", tok, client_name="Анна", avito_chat="Ч-%d" % chat, item_goods_id=["100"], item_qty=["4"],
                        item_sale_unit=[listed], item_sale_auto=[listed]))
    assert prices(conn, kept) == [("100", feed_price(conn, other, "100"))]
    typed = created(post(client, "/orders/new", tok, client_name="Борис", avito_chat="Ч-%d" % chat, item_goods_id=["100"], item_qty=["4"],
                         item_sale_unit=["4800"], item_sale_auto=[listed]))
    assert prices(conn, typed) == [("100", 4800.0)]
    page = client.get("/orders/new?goods_id=100").get_data(as_text=True)                                  # «Оформить заказ» from the catalog
    assert '<input type="hidden" name="item_sale_auto" value="%s">' % listed in page
    point = points.save_point(conn, {"name": "Гамма", "avito_account_id": other})
    card_id = created(post(client, "/orders/new", tok, client_name="Вера"))
    r = post(client, "/orders/%d/items/add" % card_id, tok, was="", client_name="Вера", point_id=str(point), goods_id="100",
             query="Autograph Snow 3 · 205/55 R16", qty="4", sale_unit=listed, sale_auto=listed)                # the point chosen with «+»
    assert r.status_code == 302 and prices(conn, card_id) == [("100", feed_price(conn, other, "100"))]


def test_the_row_saved_with_a_chat_typed_in_the_same_press_is_priced_by_that_chat(tmp_path, monkeypatch):
    """The review 2026-10-26 (D2): «Сохранить» binds the chat typed in «Чат Авито» before the row is priced."""
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    main, other = accounts(conn)
    chat = chat_of(conn, other)
    card_id = created(post(client, "/orders/new", tok, client_name="Анна"))
    post(client, "/orders/%d/save" % card_id, tok, avito_chat="Ч-%d" % chat, goods_id="100", query="Autograph Snow 3 · 205/55 R16", qty="4")
    assert conn.execute("SELECT order_id FROM avito_chat WHERE id = ?", (chat,)).fetchone()[0] == card_id
    assert prices(conn, card_id) == [("100", feed_price(conn, other, "100"))]


def test_a_mirror_account_is_priced_by_the_calculation_not_by_a_stored_row(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    main, other = accounts(conn)
    conn.execute("UPDATE account SET feed_mode = 'mirror' WHERE id = ?", (other,))
    published(conn, other, "100", 5130)                                                                    # a row of an older state of the mirror
    chat = chat_of(conn, other)
    post(client, "/chats/%d/create-deal" % chat, tok)
    card_id = conn.execute("SELECT order_id FROM avito_chat WHERE id = ?", (chat,)).fetchone()[0]
    assert prices(conn, card_id) == [("100", feed_price(conn, other, "100"))]


def test_a_window_back_with_an_error_keeps_the_mark_of_the_listed_price(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    main, other = accounts(conn)
    r = post(client, "/orders/new", tok, client_name="Анна", item_goods_id=["100", "999"], item_query=["", ""], item_qty=["4", "1"],
             item_sale_unit=["4860", ""], item_sale_auto=["4860", ""])                                     # 999 is not in the unload
    assert r.status_code == 200 and '<input type="hidden" name="item_sale_auto" value="4860">' in r.get_data(as_text=True)
    card_id = created(post(client, "/orders/new", tok, client_name="Вера"))
    r = post(client, "/orders/%d/save" % card_id, tok, client_phone="12", goods_id="100", query="Autograph Snow 3 · 205/55 R16", qty="4",
             sale_unit="4860", sale_auto="4860")
    assert 'name="sale_auto" data-find-auto value="4860"' in r.get_data(as_text=True)
