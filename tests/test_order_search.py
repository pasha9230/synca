"""A position of an order is found by its name: Russian or Latin letters, a usual misspelling, the wrong keyboard layout; only with a stock, with its price."""
from __future__ import annotations

import re
from datetime import datetime

from helpers import catalog_xml, commodity_xml
import panel_helpers
from panel_helpers import login, make_app, post
from tires_sync import order_search, orders, store

UNLOAD = catalog_xml(
    commodity_xml(NNOMMODIF="100", SMODIFNAME="IKON 205/55/16 T 94 Autograph Snow 3", SMODIFNAME_ORIG="Ikon Tyres 205/55/16 T 94 Autograph Snow 3",
                  SMODEL="Autograph Snow 3", NREST="8"),
    commodity_xml(NNOMMODIF="101", SMODIFNAME="IKON 195/65/15 T 91 Autograph Ice 9", SMODIFNAME_ORIG="Ikon Tyres 195/65/15 T 91 Autograph Ice 9",
                  SMODEL="Autograph Ice 9", SDIAMETR="15", SWIDTH="195", SHEIGHT="65", NREST="20"),
    commodity_xml(NNOMMODIF="102", SMODIFNAME="IKON 205/55/16 T 95 Autograph Aqua", SMODIFNAME_ORIG="Ikon Tyres 205/55/16 T 95 Autograph Aqua",
                  SMODEL="Autograph Aqua", NREST="0"),
    commodity_xml(NNOMMODIF="103", SMARKA="Pirelli", SMODIFNAME="PIRELLI 205/55/16 Ice Zero", SMODIFNAME_ORIG="Pirelli 205/55/16 Ice Zero", SMODEL="Ice Zero"),
)
REF = ('<Shiny>\n<make name="Ikon Tyres">\n<model name="Autograph Snow 3"/>\n<model name="Autograph Ice 9"/>\n<model name="Autograph Aqua"/>\n</make>\n'
       '<make name="Pirelli">\n<model name="Ice Zero"/>\n</make>\n</Shiny>')


def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(panel_helpers, "UNLOAD", UNLOAD)
    monkeypatch.setattr(panel_helpers, "REF_TEXT", REF)
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 10, 12, 9, 0, 0))
    client = app.test_client()
    tok = login(client)
    return app, client, tok, store.connect(app.config["DB_PATH"])


def found(client, q):
    return client.get("/orders/items/find?q=" + q).get_json()["items"]


def test_the_sound_is_the_same_for_russian_and_latin_spelling():
    assert order_search.sound("Autograph") == order_search.sound("автограф") == "tgrf"
    assert order_search.sound("Ikon") == order_search.sound("icon") == order_search.sound("Айкон")
    assert order_search.sound("Tyres") == order_search.sound("tires") == order_search.sound("тайерс")
    assert order_search.sound("Pirelli") == order_search.sound("пирелли")
    assert order_search.sound("Kumho") == order_search.sound("кумхо")


def test_the_name_finds_positions_with_a_stock_with_their_price(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    items = found(client, "icon tires автограф")
    assert {i["goods_id"] for i in items} == {"100", "101"}                                    # no stock (102) is not offered, Pirelli does not match
    assert items[0]["goods_id"] == "101"                                                       # the larger stock first
    assert all(i["price"] and i["price"] > 0 and i["stock"] > 0 and i["size"] for i in items)
    assert [i["goods_id"] for i in found(client, "айкон автограф снов")] == ["100"]
    assert [i["goods_id"] for i in found(client, "Autograph Ice")] == ["101"]
    assert [i["goods_id"] for i in found(client, "фгещпкфзр шсу")] == ["101"]                 # «autograph ice» in the Russian layout
    assert [i["goods_id"] for i in found(client, "205/55 R16 ikon")] == ["100"]
    assert {i["goods_id"] for i in found(client, "ice")} == {"101", "103"}
    assert found(client, "мишлен") == [] and found(client, "a") == []
    assert app.test_client().get("/orders/items/find?q=ikon").status_code == 302             # only after login


def test_the_price_is_that_of_the_catalog_and_the_text_adds_the_only_match(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    r = post(client, "/orders/new", tok, client_name="Иван")
    cid = int(re.search(r"/orders/(\d+)", r.headers["Location"]).group(1))
    price = found(client, "autograph snow")[0]["price"]
    post(client, "/orders/%d/items/add" % cid, tok, goods_id="", query="айкон автограф снов", qty="4")    # found by sound only: choose in the list
    assert orders.get_card(conn, cid)["items"] == []
    assert "нашлось позиций: 1" in client.get("/orders/%d?tab=items" % cid).get_data(as_text=True)
    post(client, "/orders/%d/items/add" % cid, tok, goods_id="", query="Autograph Snow · 205/55 R16", qty="4")   # every word exact: added
    items = orders.get_card(conn, cid)["items"]
    assert [(i["goods_id"], i["qty"], i["sale_unit"]) for i in items] == [("100", 4, price)]       # the same price as the order puts itself
    assert "Добавлено по тексту" in client.get("/orders/%d?tab=items" % cid).get_data(as_text=True)
    post(client, "/orders/%d/items/add" % cid, tok, goods_id="101", query="Autograph Ice 9 · 195/65 R15", qty="2", sale_unit="4800")
    assert orders.get_card(conn, cid)["items"][1]["sale_unit"] == 4800.0                     # the price typed by hand wins
    post(client, "/orders/%d/items/add" % cid, tok, query="ikon autograph", qty="1")              # two positions match: nothing is added
    post(client, "/orders/%d/items/add" % cid, tok, query="мишлен", qty="1")
    post(client, "/orders/%d/items/add" % cid, tok, goods_id="999", query="Autograph Snow", qty="1")   # a chosen code no longer in the unload
    post(client, "/orders/%d/items/add" % cid, tok, query="Autograph Aqua", qty="1")              # exact, but no stock
    assert len(orders.get_card(conn, cid)["items"]) == 2
    post(client, "/orders/%d/items/add" % cid, tok, query="ICE ZERO", qty="1")                    # exact and the only one, any case
    assert orders.get_card(conn, cid)["items"][2]["goods_id"] == "103"
    html = client.get("/orders/%d?tab=items" % cid).get_data(as_text=True)
    assert 'data-item-find="/orders/items/find?order=%d"' % cid in html                       # the list is priced by the account of the order


def test_a_text_is_added_only_when_exact_and_alone(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    from tires_sync.panel.routes_main import context
    with app.test_request_context():
        _c, _s, parsed, ref = context(app)
        assert order_search.typed(conn, parsed, ref, "ikon autograph") == (None, 2)
        assert order_search.typed(conn, parsed, ref, "айкон автограф айс")[0] is None             # by sound: never alone
        assert order_search.typed(conn, parsed, ref, "autograph ice 9")[0].goods_id == "101"
        assert order_search.typed(conn, parsed, ref, "101")[0].goods_id == "101"
        assert order_search.typed(conn, parsed, ref, "фгещпкфзр шсу 9")[0].goods_id == "101"      # wrong layout, every word exact once switched
