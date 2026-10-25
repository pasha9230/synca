"""The purchase of WebMIM in the profit (the owner 2026-10-22: «только для аналитики»): an order of the panel paired with an order of WebMIM
(server prod) is counted at WebMIM's price of the position; nothing is written anywhere, the test server never counts."""
from __future__ import annotations

import json
from datetime import datetime

import pytest
from panel_helpers import login, make_app

from tires_sync import analytics, connections, economics, order_report, orders, sales, store, users, webmim_match

NOW = "2026-10-20T12:00:00"
TERMS = {"discount_pct": 10, "bonus_pct": 2, "monthly_pct": 1}


def db(tmp_path):
    return store.connect(str(tmp_path / "x.db"))


def card(conn, webmim_id="", webmim_number="") -> int:
    cid = conn.execute("INSERT INTO order_card (stage_id, webmim_id, webmim_number, created_at, created_by, entered_at, updated_at) "
                       "VALUES (1,?,?,?, 'o', ?, ?)", (webmim_id, webmim_number, NOW, NOW, NOW)).lastrowid
    conn.commit()
    return cid


def wm(conn, order_id, items, number="", server="prod"):
    conn.execute("INSERT INTO webmim_order (server, id, number, items, seen_at, items_at) VALUES (?,?,?,?,?,?)",
                 (server, order_id, number, None if items is None else json.dumps(items, ensure_ascii=False), NOW, NOW))
    conn.commit()


def sell(conn, cid, goods="g1", sale_total=20000, supplier=4000, pieces=4, terms=None, fee_pct=0, promo_pct=0, point="Бета", day="2026-10-23"):
    return sales.add_sale(conn, day=day, goods_id=goods, brand_key="nokian", season="winter", diameter=16, name="Nokian 205/55 R16",
                          ad_kind="single", pieces=pieces, sale_total=sale_total, supplier_price=supplier, terms=terms, fee_pct=fee_pct,
                          promo_pct=promo_pct, account_id=None, point=point, note="Заказ №%d" % cid, now=NOW, author="o", today="2026-10-31",
                          source="order")


def build(conn):
    return order_report.build(conn, "2026-10-01", "2026-10-31")


def test_a_paired_order_is_counted_at_the_price_of_webmim(tmp_path):
    conn = db(tmp_path)
    cid = card(conn, webmim_id="555")
    wm(conn, "555", [{"goods_id": "g1", "name": "x", "qty": 4, "price": 3500, "sum": 14000}])
    sell(conn, cid)                                                           # written: cost 16000, margin 4000
    rep = build(conn)
    line = rep["lines"][0]
    assert (line["sale"], line["cost"], line["margin"], line["fee"], line["net"]) == (20000, 14000, 6000, 75, 5925)
    assert rep["webmim"] == {"count": 1, "positions": 1, "by_point": [{"point": "Бета", "count": 1, "positions": 1}]}
    assert (rep["total"]["owner"], rep["total"]["partner"]) == (2962.5, 2962.5)


def test_the_terms_apply_to_the_price_of_webmim_as_to_the_unload_price(tmp_path):
    conn = db(tmp_path)
    cid = card(conn, webmim_id="555")
    wm(conn, "555", [{"goods_id": "g1", "name": "x", "qty": 4, "price": 3333.33, "sum": 13333.32}])
    sell(conn, cid, sale_total=17777.77, supplier=4111.11, terms=TERMS, fee_pct=5, promo_pct=2)
    want = economics.unit(3333.33, 17777.77 / 4, TERMS, 5, 2, 4)
    line = build(conn)["lines"][0]
    assert line["cost"] == pytest.approx(want["cost"], abs=0.02) and line["margin"] == pytest.approx(want["margin"], abs=0.02)
    assert line["sale"] == 17777.77


def test_paired_by_the_number_and_only_its_own_positions(tmp_path):
    conn = db(tmp_path)
    cid = card(conn, webmim_number="WM-777")
    wm(conn, "9001", [{"goods_id": "g1", "name": "x", "qty": 4, "price": 3500, "sum": 14000}], number="777")
    sell(conn, cid, goods="g1")
    sell(conn, cid, goods="g2", sale_total=5000, supplier=4000, pieces=1)     # not in WebMIM: as written (margin 1000)
    rep = build(conn)
    line = rep["lines"][0]
    assert (line["cost"], line["margin"], rep["webmim"]["count"], rep["webmim"]["positions"]) == (18000, 7000, 1, 2)


@pytest.mark.parametrize("server, items", [
    ("test", [{"goods_id": "g1", "qty": 4, "price": 3500, "sum": 14000}]),                 # the test server's orders are made up
    ("prod", None),                                                                          # the positions are not read yet
    ("prod", []),
    ("prod", [{"goods_id": "g1", "qty": 4, "price": None, "sum": None}]),
    ("prod", [{"goods_id": "g1", "qty": 4, "price": 0, "sum": 0}]),
    ("prod", [{"goods_id": "g1", "qty": 4, "price": -5, "sum": -20}]),
    ("prod", [{"goods_id": "g1", "qty": 4, "price": 0.001, "sum": 0.004}]),                 # rounds to 0 kopecks: no price
    ("prod", [{"goods_id": "g1", "qty": 2, "price": 3500, "sum": 7000}, {"goods_id": "g1", "qty": 2, "price": 3600, "sum": 7200}]),
    ("prod", [{"goods_id": "g1", "qty": 2, "price": 3500, "sum": 7000}, {"goods_id": "g1", "qty": 2, "price": None, "sum": None}]),
    ("prod", [{"goods_id": "g9", "qty": 4, "price": 3500, "sum": 14000}]),
])
def test_without_a_price_of_webmim_the_report_is_as_before(tmp_path, server, items):
    conn = db(tmp_path)
    cid = card(conn, webmim_id="555")
    wm(conn, "555", items, server=server)
    sell(conn, cid)
    rep = build(conn)
    line = rep["lines"][0]
    assert (line["cost"], line["margin"], line["net"], rep["webmim"]["count"], rep["webmim"]["positions"]) == (16000, 4000, 3925, 0, 1)
    assert "WebMIM" not in order_report.export_csv(rep)                     # nothing of WebMIM: the CSV is the old one


def test_the_same_price_twice_is_one_price(tmp_path):
    conn = db(tmp_path)
    cid = card(conn, webmim_id="555")
    wm(conn, "555", [{"goods_id": "g1", "qty": 2, "price": 3500, "sum": 7000}, {"goods_id": "g1", "qty": 2, "price": 3500, "sum": 7000}])
    sell(conn, cid)
    assert build(conn)["lines"][0]["margin"] == 6000


def test_one_webmim_order_on_two_orders_of_the_panel_gives_no_price(tmp_path):
    """A number typed twice (webmim_match.shared): whose order it is, is not known, so neither takes its price."""
    conn = db(tmp_path)
    wm(conn, "555", [{"goods_id": "g1", "qty": 4, "price": 3500, "sum": 14000}], number="777")
    first, second = card(conn, webmim_id="555"), card(conn, webmim_number="777")
    sell(conn, first)
    sell(conn, second, point="Альфа")
    rep = build(conn)
    assert [(r["cost"], r["margin"]) for r in rep["lines"]] == [(16000, 4000), (16000, 4000)] and rep["webmim"]["count"] == 0


def test_the_prod_orders_count_whatever_server_is_chosen(tmp_path):
    conn = db(tmp_path)
    connections.set_config(conn, "webmim_orders", connections.supplier_owner(1), {"server": "test"}, NOW, "o")
    cid = card(conn, webmim_id="555")
    wm(conn, "555", [{"goods_id": "g1", "qty": 4, "price": 3500, "sum": 14000}])
    sell(conn, cid)
    assert webmim_match.current_server(conn) == "test" and build(conn)["lines"][0]["margin"] == 6000


def test_no_pair_reversed_and_manual_sales_are_as_before(tmp_path):
    conn = db(tmp_path)
    wm(conn, "555", [{"goods_id": "g1", "qty": 4, "price": 3500, "sum": 14000}])
    lone = card(conn)                                                         # no WebMIM number at all
    sell(conn, lone)
    other = card(conn, webmim_id="404")                                       # a number not in the snapshot
    sell(conn, other, point="Альфа")
    paired = card(conn, webmim_id="555")
    gone = sell(conn, paired, point="Гамма")
    sales.reverse_sale(conn, gone, NOW, "o", from_order=True)
    sales.add_sale(conn, day="2026-10-23", goods_id="g1", brand_key="nokian", season="winter", diameter=16, name="n", ad_kind="single", pieces=4,
                   sale_total=20000, supplier_price=4000, terms=None, fee_pct=0, promo_pct=0, account_id=None, point="Бета",
                   note="Заказ №%d" % paired, now=NOW, author="o", today="2026-10-31", source="manual")
    rep = build(conn)
    assert sorted((r["point"], r["cost"], r["margin"]) for r in rep["lines"]) == [("Альфа", 16000, 4000), ("Бета", 16000, 4000)]
    assert rep["webmim"]["count"] == 0
    assert rep["manual"]["cost"] == 16000 and rep["manual"]["margin"] == 4000


def test_only_the_sales_from_the_day_of_the_release_take_the_price_of_webmim(tmp_path):
    """The owner 2026-10-22 «с даты выкладки»: a sale of 2026-10-22 keeps its purchase, one of 2026-10-23 takes WebMIM's."""
    conn = db(tmp_path)
    wm(conn, "555", [{"goods_id": "g1", "qty": 4, "price": 3500, "sum": 14000}])
    wm(conn, "556", [{"goods_id": "g1", "qty": 4, "price": 3500, "sum": 14000}])
    sell(conn, card(conn, webmim_id="555"), day="2026-10-22")
    sell(conn, card(conn, webmim_id="556"), day="2026-10-23", point="Альфа")
    assert order_report.WEBMIM_FROM == "2026-10-23"
    assert {r["point"]: (r["cost"], r["margin"]) for r in build(conn)["lines"]} == {"Бета": (16000, 4000), "Альфа": (14000, 6000)}
    assert order_report.build(conn, "2026-10-01", "2026-10-22")["webmim"]["count"] == 0


def test_the_order_page_function_is_the_report_rule(tmp_path):
    conn = db(tmp_path)
    wm(conn, "555", [{"goods_id": "g1", "qty": 4, "price": 3500, "sum": 14000}])
    wm(conn, "556", [{"goods_id": "g1", "qty": 4, "price": 3500, "sum": 14000}])
    paired = card(conn, webmim_id="555")
    assert order_report.order_webmim(conn, paired) is None                    # no sale written yet: nothing in the analytics
    sell(conn, paired)
    sell(conn, paired, goods="g2", sale_total=5000, supplier=4000, pieces=1)
    assert order_report.order_webmim(conn, paired) == {"cost": 14000, "count": 1, "positions": 2}
    early = card(conn, webmim_id="556")
    sell(conn, early, day="2026-10-22")
    assert order_report.order_webmim(conn, early) is None and order_report.order_webmim(conn, card(conn)) is None
    gone = card(conn, webmim_id="557")
    wm(conn, "557", [{"goods_id": "g1", "qty": 4, "price": 3500, "sum": 14000}], server="test")
    sell(conn, gone)
    assert order_report.order_webmim(conn, gone) is None                      # the test server


def test_the_order_page_shows_the_purchase_of_the_analytics_to_the_money_right_only(tmp_path):
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 10, 23, 9, 0, 0))
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, NOW)
    paired = orders.create_card(conn, {"client_name": "Иван", "client_phone": "+7 (911) 111-22-33", "webmim_id": "555"}, NOW, "owner")
    wm(conn, "555", [{"goods_id": "g1", "qty": 4, "price": 3500, "sum": 14000}])
    owner = app.test_client()
    login(owner)
    assert "data-webmim-analytics" not in owner.get("/orders/%d" % paired).get_data(as_text=True)      # not sold yet
    sell(conn, paired)
    html = owner.get("/orders/%d" % paired).get_data(as_text=True)
    assert "data-webmim-analytics" in html and "В аналитике закупка по WebMIM: <b>14\u00a0000\u00a0₽</b> (1 из 1 поз." in html
    assert conn.execute("SELECT cost_total FROM sale").fetchone()[0] == 16000
    users.add_user(conn, "chatter", "pw-long-enough-123", NOW, role="chats")
    chats = app.test_client()
    with chats.session_transaction() as s:
        s["user"], s["epoch"], s["csrf"] = "chatter", store.get_session_epoch(conn, "chatter"), "tok"
    html = chats.get("/orders/%d" % paired).get_data(as_text=True)
    assert "Иван" in html and "data-webmim-analytics" not in html and "14\u00a0000" not in html


def _dump(conn):
    tables = ("sale", "order_card", "order_item", "webmim_order", "audit")
    return {t: conn.execute("SELECT * FROM %s ORDER BY rowid" % t).fetchall() for t in tables}


def test_nothing_is_written(tmp_path):
    conn = db(tmp_path)
    cid = card(conn, webmim_id="555")
    wm(conn, "555", [{"goods_id": "g1", "qty": 4, "price": 3500, "sum": 14000}])
    sell(conn, cid)
    before = _dump(conn)
    build(conn)
    analytics.money(conn, "2026-10-01", "2026-10-31")
    order_report.export_csv(build(conn))
    assert _dump(conn) == before
    assert conn.execute("SELECT cost_total, margin_total FROM sale").fetchone() == (16000, 4000)


def test_the_money_of_the_analytics_and_its_source(tmp_path):
    conn = db(tmp_path)
    cid = card(conn, webmim_id="555")
    wm(conn, "555", [{"goods_id": "g1", "qty": 4, "price": 3500, "sum": 14000}])
    sell(conn, cid)
    sell(conn, card(conn), point="Альфа")
    got = analytics.money(conn, "2026-10-01", "2026-10-31")
    assert (got["cost"], got["profit"], got["net"]) == (30000, 10000, 9850)
    assert got["webmim"] == {"count": 1, "of": 2}


def test_the_page_the_csv_and_the_right(tmp_path):
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 10, 20, 9, 0, 0))
    client = app.test_client()
    login(client)
    conn = store.connect(app.config["DB_PATH"])
    cid = card(conn, webmim_id="555")
    wm(conn, "555", [{"goods_id": "g1", "qty": 4, "price": 3500, "sum": 14000}])
    sell(conn, cid)
    html = client.get("/orders/report?from=2026-10-01&to=2026-10-31").get_data(as_text=True)
    assert "14 000 ₽" in html and "data-webmim-cost" in html and "1 из 1" in html
    sell(conn, card(conn), point="<b>Точка</b>")
    html = client.get("/orders/report?from=2026-10-01&to=2026-10-31").get_data(as_text=True)
    assert "1 из 2 поз." in html and "&lt;b&gt;Точка&lt;/b&gt; 0 из 1; Бета 1 из 1" in html and "<b>Точка</b>" not in html
    html = client.get("/analytics?from=2026-10-01&to=2026-10-31").get_data(as_text=True)
    assert "data-webmim-cost" in html
    csv_text = client.get("/orders/report/export.csv?from=2026-10-01&to=2026-10-31").get_data(as_text=True)
    assert "Бета;1;4;20000,00;14000,00;6000,00;75,00;5925,00;2962,50;2962,50\n" in csv_text
    assert "\nЗакупка из заказов WebMIM (боевой сервер), позиций;1 из 2;" in csv_text
    users.add_user(conn, "watcher", "pw-long-enough-123", NOW, role="viewer")
    other = app.test_client()
    with other.session_transaction() as s:
        s["user"], s["epoch"], s["csrf"] = "watcher", store.get_session_epoch(conn, "watcher"), "tok"
    for url in ("/orders/report", "/orders/report/export.csv"):
        assert other.get(url).status_code == 403
    html = other.get("/analytics?from=2026-10-01&to=2026-10-31").get_data(as_text=True)     # the funnel without the money
    assert "data-webmim-cost" not in html and "<h3>Продажи и прибыль</h3>" not in html
