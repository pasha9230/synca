"""The search of a position by the supplier's data on the pages (the owner 2026-11-24): the card's row and a new order take a position of the base
of positions; a point never reads its purchase; a deal of a chat whose ad code moved keeps the price of that ad; a tyre without a stock is said."""
from __future__ import annotations

import dataclasses

from test_order_account_price import accounts, chat_of, feed_price, prices, published
from test_order_search import setup
from test_order_window import created
from panel_helpers import post
from tires_sync import catalog_archive, orders
from tires_sync import order_points as points
from tires_sync.webmim import ParseResult

T1 = "2026-10-01T10:00:00"
OLD = "Ikon Tyres 205/55/16 T 94 Autograph Snow 3 Old"          # a name today's unload has not
SNOW = "Ikon Tyres 205/55/16 T 94 Autograph Snow 3"              # the exact supplier's name of 100
AQUA = "Ikon Tyres 205/55/16 T 95 Autograph Aqua"                # 102: no stock today


def based(app, conn):
    """The position 100 as the base of positions kept it under an older code and name (the supplier rewrote it since)."""
    from tires_sync.panel.routes_main import context
    with app.test_request_context():
        _c, _s, parsed, _ref = context(app)
    old = dataclasses.replace(parsed.offers["100"], goods_id="x-old", name=OLD.upper(), name_orig=OLD)
    catalog_archive.record(conn, ParseResult(offers={"x-old": old}), T1)


def journal(conn, card_id, kind=None):
    return [e["text"] for e in orders.list_events(conn, card_id) if kind is None or e["kind"] == kind]


def test_a_row_of_the_card_takes_a_position_of_the_base_by_its_exact_name(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    based(app, conn)
    cid = created(post(client, "/orders/new", tok, client_name="Иван"))
    post(client, "/orders/%d/items/add" % cid, tok, goods_id="", query=OLD, qty="2")
    assert [i["goods_id"] for i in orders.get_card(conn, cid)["items"]] == ["x-old"]
    [note] = [t for t in journal(conn, cid, "webmim") if t.startswith("Позиция взята из базы позиций")]
    assert "Цена закупки из базы позиций" in note and "Искали: название «%s»" % OLD in note


def test_a_new_order_takes_a_position_of_the_base_and_a_point_reads_no_purchase(tmp_path, monkeypatch):
    from test_roles import person
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    based(app, conn)
    point = points.save_point(conn, {"name": "Альфа", "manager_name": "А", "channel": "vk"})
    cid = created(post(client, "/orders/new", tok, client_name="Антон", point_id=str(point), deal_kind="stock", item_goods_id=[""],
                       item_query=[OLD], item_qty=["4"]))
    assert [i["goods_id"] for i in orders.get_card(conn, cid)["items"]] == ["x-old"]
    office = client.get("/orders/%d" % cid).get_data(as_text=True)
    assert "Позиция взята из базы позиций" in office and "Цена закупки из базы позиций" in office
    manager, _ = person(app, "point", point)
    page = manager.get("/orders/%d" % cid).get_data(as_text=True)
    assert "Добавлено:" in page                                                              # the point reads its own order
    assert "Цена закупки" not in page and "взята из базы позиций" not in page
    assert "взята из базы позиций" not in manager.get("/alerts/feed").get_data(as_text=True)


def test_an_exact_name_without_a_stock_is_added_and_said(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    cid = created(post(client, "/orders/new", tok, client_name="Иван"))
    post(client, "/orders/%d/items/add" % cid, tok, goods_id="", query=AQUA, qty="1")
    assert [i["goods_id"] for i in orders.get_card(conn, cid)["items"]] == ["102"]
    [note] = [t for t in journal(conn, cid, "item") if t.startswith("Позиция найдена по названию поставщика")]
    assert "(код 102), остаток сегодня 0 шт." in note
    assert "остаток у поставщика сегодня 0 шт." in client.get("/orders/%d" % cid).get_data(as_text=True)


def test_a_chat_ad_whose_code_moved_keeps_the_price_of_that_ad(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    main, other = accounts(conn)
    chat = chat_of(conn, other, goods_id="x-old")
    conn.execute("UPDATE avito_chat SET item_title = ? WHERE id = ?", (SNOW, chat))
    conn.commit()
    published(conn, other, "x-old", 7770.0)
    post(client, "/chats/%d/create-deal" % chat, tok)
    card_id = conn.execute("SELECT order_id FROM avito_chat WHERE id = ?", (chat,)).fetchone()[0]
    assert prices(conn, card_id) == [("100", 7770.0)]
    assert not any("расчётная" in t for t in journal(conn, card_id))


def test_a_chat_ad_whose_code_moved_without_a_published_price_says_the_price_is_calculated(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    main, other = accounts(conn)
    chat = chat_of(conn, other, goods_id="x-old")
    conn.execute("UPDATE avito_chat SET item_title = ? WHERE id = ?", (SNOW, chat))
    conn.commit()
    post(client, "/chats/%d/create-deal" % chat, tok)
    card_id = conn.execute("SELECT order_id FROM avito_chat WHERE id = ?", (chat,)).fetchone()[0]
    assert prices(conn, card_id) == [("100", feed_price(conn, other, "100"))]
    [note] = [t for t in journal(conn, card_id, "chat") if t.startswith("Позиция найдена по названию поставщика")]
    assert "Цена продажи расчётная, не из объявления" in note
