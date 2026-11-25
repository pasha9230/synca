"""ТЗ 94 (the owner 2026-11-25): the office (operator, admin) orders by hand any position of the base — without a stock, out of the feed, of a brand
or season without a percent, one the unload has no more — from the search of the window, the card, the chat and the catalog. The list marks
what it is not and the page warns, nothing blocks; the price is the panel's, else typed by hand. Nothing of the feed, prices or settings changes."""
from __future__ import annotations

import dataclasses
import json

from helpers import catalog_xml, commodity_xml
import panel_helpers
from panel_helpers import login, make_app, post
from test_order_account_price import accounts, chat_of
from test_order_window import created
from tires_sync import catalog_archive, order_search, orders, store
from tires_sync import order_points as points
from tires_sync.settings_store import get_active
from tires_sync.webmim import ParseResult
from datetime import datetime

UNLOAD = catalog_xml(
    commodity_xml(NNOMMODIF="100", SMODIFNAME="IKON 205/55/16 T 94 Autograph Snow 3", SMODIFNAME_ORIG="Ikon Tyres 205/55/16 T 94 Autograph Snow 3",
                  SMODEL="Autograph Snow 3", NREST="8"),
    commodity_xml(NNOMMODIF="104", SSEASON="Летняя", SMODIFNAME="IKON 205/55/16 V 94 Autograph Eco", SMODIFNAME_ORIG="Ikon Tyres 205/55/16 V 94 Autograph Eco",
                  SMODEL="Autograph Eco", NREST="12"),                                   # a summer Ikon: no percent, not enabled
    commodity_xml(NNOMMODIF="105", SMARKA="Kumho", SMODIFNAME="KUMHO 205/55/16 WS-31", SMODIFNAME_ORIG="Kumho 205/55/16 WS-31", SMODEL="WS-31",
                  NREST="0"),                                                            # a brand without settings, no stock
)
REF = ('<Shiny>\n<make name="Ikon Tyres">\n<model name="Autograph Snow 3"/>\n<model name="Autograph Eco"/>\n</make>\n'
       '<make name="Kumho">\n<model name="WS-31"/>\n</make>\n</Shiny>')
GONE = "Ikon Tyres 205/55/16 T 99 Autograph Gone"


def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(panel_helpers, "UNLOAD", UNLOAD)
    monkeypatch.setattr(panel_helpers, "REF_TEXT", REF)
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 11, 25, 9, 0, 0))
    client = app.test_client()
    tok = login(client)
    return app, client, tok, store.connect(app.config["DB_PATH"])


def gone(app, conn):
    """A position the supplier took away: kept in the permanent base of positions, marked gone."""
    from tires_sync.panel.routes_main import context
    with app.test_request_context():
        _c, _s, parsed, _ref = context(app)
    old = dataclasses.replace(parsed.offers["100"], goods_id="x-gone", name=GONE.upper(), name_orig=GONE, stock=3)
    catalog_archive.record(conn, ParseResult(offers={"x-gone": old}), "2026-11-01T10:00:00")
    conn.execute("UPDATE catalog_item SET gone_since = ? WHERE goods_id = ?", ("2026-11-10T10:00:00", "x-gone"))
    conn.commit()


def found(client, q):
    return {i["goods_id"]: i for i in client.get("/orders/items/find?q=" + q).get_json()["items"]}


def frozen(conn):
    """What a manual order must never change: the settings, the published feed, the base of positions, the manual overrides."""
    return (json.dumps(get_active(conn)[1], sort_keys=True), conn.execute("SELECT COUNT(*) FROM published_state").fetchone()[0],
            conn.execute("SELECT goods_id, gone_since, last_stock FROM catalog_item ORDER BY goods_id").fetchall())


def journal(conn, card_id):
    return [(e["kind"], e["text"]) for e in orders.list_events(conn, card_id)]


def test_the_office_finds_every_position_of_the_base_marked(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    gone(app, conn)
    items = found(client, "autograph")
    assert set(items) == {"100", "104", "x-gone"}
    assert items["100"]["marks"] == [] and not items["100"]["warn"] and items["100"]["price"]
    assert items["104"]["price"] is None and items["104"]["warn"] and any(m.startswith("вне фида") for m in items["104"]["marks"])
    assert items["x-gone"]["marks"][:2] == ["нет в выгрузке поставщика", "нет в наличии"] and items["x-gone"]["stock"] == 0
    kumho = found(client, "кумхо")["105"]                                                  # any brand, no stock
    assert "нет в наличии" in kumho["marks"] and kumho["price"] is None


def test_the_default_search_stays_with_a_stock_only(tmp_path, monkeypatch):
    """What a point or any other caller gets without `every`: as before, positions with a stock of today's unload."""
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    gone(app, conn)
    from tires_sync.panel.routes_main import context
    with app.test_request_context():
        _c, settings, parsed, ref = context(app)
        items = order_search.suggestions(conn, settings, parsed, ref, "autograph", "2026-11-25")
    assert {i["goods_id"] for i in items} == {"100", "104"} and all("marks" not in i for i in items)


def test_a_position_without_a_price_asks_for_it_then_takes_the_typed_one_with_a_warning(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    before = frozen(conn)
    cid = created(post(client, "/orders/new", tok, client_name="Иван"))
    r = post(client, "/orders/%d/items/add" % cid, tok, goods_id="104", query="Ikon Tyres 205/55/16 V 94 Autograph Eco · 205/55 R16", qty="2")
    assert orders.get_card(conn, cid)["items"] == []                                       # no price, nothing typed: said, not added
    assert "впишите цену за шт. вручную" in client.get(r.headers["Location"]).get_data(as_text=True)
    r = post(client, "/orders/%d/items/add" % cid, tok, goods_id="104", query="Ikon Tyres 205/55/16 V 94 Autograph Eco · 205/55 R16", qty="2",
             sale_unit="6100")
    [item] = orders.get_card(conn, cid)["items"]
    assert (item["goods_id"], item["qty"], item["sale_unit"]) == ("104", 2, 6100.0)
    assert any(k == "webmim" and "вписана вручную" in t for k, t in journal(conn, cid))  # the office's record only
    assert "взята цена, вписанная вручную" in client.get(r.headers["Location"]).get_data(as_text=True)
    post(client, "/orders/%d/items/add" % cid, tok, goods_id="105", query="Kumho 205/55/16 WS-31 · 205/55 R16", qty="4", sale_unit="5500")
    assert [i["goods_id"] for i in orders.get_card(conn, cid)["items"]] == ["104", "105"]  # another brand, no stock: a warning, no block
    assert frozen(conn) == before                                                          # the feed, prices, settings untouched


def test_a_gone_position_is_ordered_from_the_list_by_its_code(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    gone(app, conn)
    before = frozen(conn)
    cid = created(post(client, "/orders/new", tok, client_name="Пётр", item_goods_id=["x-gone"], item_query=[GONE + " · 205/55 R16"],
                       item_qty=["1"], item_sale_unit=["7000"]))
    [item] = orders.get_card(conn, cid)["items"]
    assert item["goods_id"] == "x-gone" and item["sale_unit"] > 0
    assert any(t.startswith("Позиция взята из базы позиций") for _k, t in journal(conn, cid))
    assert frozen(conn) == before


def test_the_catalog_opens_a_window_for_a_position_without_a_price(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    gone(app, conn)
    page = client.get("/orders/new?goods_id=104").get_data(as_text=True)
    assert 'value="104"' in page and "заказ оформить можно, впишите цену" in page          # a warning, the row is there
    page = client.get("/orders/new?goods_id=x-gone").get_data(as_text=True)
    assert 'value="x-gone"' in page
    cid = created(post(client, "/orders/new", tok, client_name="Олег", item_goods_id=["104"], item_query=["Ikon Tyres 205/55/16 V 94 Autograph Eco"],
                       item_qty=["1"], item_sale_unit=["6000"], item_sale_auto=[""]))
    assert [(i["goods_id"], i["sale_unit"]) for i in orders.get_card(conn, cid)["items"]] == [("104", 6000.0)]


def test_the_catalog_offers_an_order_for_a_gone_position(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    gone(app, conn)
    page = client.get("/catalog?st=gone").get_data(as_text=True)
    assert 'href="/orders/new?goods_id=x-gone"' in page


def test_the_chat_adds_a_position_out_of_the_feed(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    main, _other = accounts(conn)
    chat = chat_of(conn, main)
    post(client, "/chats/%d/create-deal" % chat, tok)
    card_id = conn.execute("SELECT order_id FROM avito_chat WHERE id = ?", (chat,)).fetchone()[0]
    before = len(orders.get_card(conn, card_id)["items"])
    post(client, "/chats/%d/items/add" % chat, tok, order_id=str(card_id), goods_id="104", qty="1")
    assert len(orders.get_card(conn, card_id)["items"]) == before                           # no price: said, not added
    post(client, "/chats/%d/items/add" % chat, tok, order_id=str(card_id), goods_id="104", qty="1", sale_unit="6200")
    assert orders.get_card(conn, card_id)["items"][-1]["goods_id"] == "104"
    assert orders.get_card(conn, card_id)["items"][-1]["sale_unit"] == 6200.0


def test_positions_with_a_stock_come_before_those_without_whatever_the_match():
    """The review of 2026-11-25: the list of 10 never loses a position with a stock to better matches without one."""
    from helpers import make_offer
    exact_none = make_offer(goods_id="a", name="AUTOGRAPH A", name_orig="Autograph A", brand_key="X", brand_raw="X", model_raw="Autograph", stock=0)
    sound_some = make_offer(goods_id="b", name="AVTOGRAF B", name_orig="Avtograf B", brand_key="X", brand_raw="X", model_raw="Avtograf", stock=5)
    pool = ParseResult(offers={"a": exact_none, "b": sound_some})
    ranked = order_search._find(pool, {}, "autograph", every=True)
    assert ranked[0][0] < ranked[1][0]                                                     # the one with a stock matches worse (by sound)
    assert [offer.goods_id for _s, _e, offer in ranked] == ["b", "a"]


def test_the_gone_positions_are_read_once_until_the_base_changes(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    gone(app, conn)
    calls = []
    real = catalog_archive.gone_offers
    monkeypatch.setattr(catalog_archive, "gone_offers", lambda c: calls.append(1) or real(c))
    order_search._GONE_CACHE.clear()
    assert [o.goods_id for o in order_search.gone_cached(conn)] == ["x-gone"]
    found(client, "autograph")
    found(client, "autogr")
    assert len(calls) == 1                                                                 # typing reads the JSON of the base once
    [held] = order_search.gone_cached(conn)
    other = dataclasses.replace(held, goods_id="x-two")
    catalog_archive.record(conn, ParseResult(offers={"x-two": other}), "2026-11-02T10:00:00")
    conn.execute("UPDATE catalog_item SET gone_since = ? WHERE goods_id = ?", ("2026-11-20T10:00:00", "x-two"))   # another one went away
    conn.commit()
    assert sorted(o.goods_id for o in order_search.gone_cached(conn)) == ["x-gone", "x-two"] and len(calls) == 2


def test_the_manual_price_is_said_by_offer_args_itself(tmp_path, monkeypatch):
    from tires_sync import order_offers
    from tires_sync.panel.routes_main import context
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    with app.test_request_context():
        _c, settings, parsed, ref = context(app)
        no_price = order_search.locate(conn, parsed, "104")
        args = order_search.located_args(conn, settings, parsed, ref, no_price, "2026-11-25", manual_sale="6100")
        assert no_price.manual_price and order_offers.MANUAL_PRICE not in args and args["sale_unit"] == "6100"
        priced = order_search.locate(conn, parsed, "100")
        args = order_search.located_args(conn, settings, parsed, ref, priced, "2026-11-25", manual_sale="6100")
        assert not priced.manual_price and args["sale_unit"] != "6100"                    # the panel's price: the typed one is not «manual»
        assert order_offers.MANUAL_PRICE not in order_offers.item_args(conn, settings, parsed, ref, "100", "2026-11-25")


def test_the_catalog_asks_to_type_a_price_only_when_the_panel_has_none(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    gone(app, conn)
    based = client.get("/orders/new?goods_id=x-gone").get_data(as_text=True)               # the base has its price
    assert "цена из базы позиций, проверьте наличие" in based and "впишите цену за шт." not in based
    assert "впишите цену за шт." in client.get("/orders/new?goods_id=104").get_data(as_text=True)


def test_the_chat_says_a_position_of_the_base_is_not_in_the_unload(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    gone(app, conn)
    main, _other = accounts(conn)
    chat = chat_of(conn, main)
    post(client, "/chats/%d/create-deal" % chat, tok)
    card_id = conn.execute("SELECT order_id FROM avito_chat WHERE id = ?", (chat,)).fetchone()[0]
    r = post(client, "/chats/%d/items/add" % chat, tok, order_id=str(card_id), goods_id="x-gone", qty="1")
    assert orders.get_card(conn, card_id)["items"][-1]["goods_id"] == "x-gone"
    assert "нет в выгрузке поставщика сегодня: проверьте наличие" in client.get(r.headers["Location"]).get_data(as_text=True)


def test_a_point_has_no_search_of_positions(tmp_path, monkeypatch):
    from test_roles import person
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    point = points.save_point(conn, {"name": "Альфа", "manager_name": "А", "channel": "vk"})
    manager, _ = person(app, "point", point)
    assert manager.get("/orders/items/find?q=autograph").status_code in (302, 403)
