"""The report of what the orders sold: the point fee, the split of the profit, who is counted."""
from __future__ import annotations

from datetime import datetime

from panel_helpers import login, make_app

from tires_sync import order_report, sales, store

NOW = "2026-10-20T12:00:00"


def sell(conn, order, point, sale_total, supplier, pieces=4, day="2026-10-10", source="order", name=None):
    return sales.add_sale(conn, day=day, goods_id="g%s" % order, brand_key="nokian", season="winter", diameter=16, name="Nokian 205/55 R16", ad_kind="single",
                          pieces=pieces, sale_total=sale_total, supplier_price=supplier, terms=None, fee_pct=0, promo_pct=0, account_id=None, point=point,
                          note=name or "Заказ №%s" % order, now=NOW, author="o", today="2026-10-20", source=source)


def db(tmp_path):
    return store.connect(str(tmp_path / "x.db"))


def test_per_point_fee_and_the_split_of_the_net_profit(tmp_path):
    conn = db(tmp_path)
    sell(conn, 1, "Бета", 20000, 4000, pieces=4)             # margin 4000
    sell(conn, 2, "Бета", 10000, 4000, pieces=2)             # margin 2000
    sell(conn, 3, "Альфа", 5000, 4000, pieces=1)          # margin 1000
    rep = order_report.build(conn, "2026-10-01", "2026-10-31")
    yun = next(r for r in rep["lines"] if r["point"] == "Бета")
    assert (yun["orders"], yun["pieces"], yun["sale"], yun["margin"], yun["fee"], yun["net"], yun["owner"], yun["partner"]) == (2, 6, 30000, 6000, 150, 5850, 2925, 2925)
    assert rep["lines"][0]["point"] == "Бета"
    t = rep["total"]
    assert (t["orders"], t["margin"], t["fee"], t["net"], t["owner"], t["partner"]) == (3, 7000, 225, 6775, 3387.5, 3387.5)


def test_one_order_with_several_positions_pays_the_fee_once(tmp_path):
    conn = db(tmp_path)
    sell(conn, 7, "Бета", 10000, 4000, pieces=2)
    sales.add_sale(conn, day="2026-10-10", goods_id="gx", brand_key="nokian", season="winter", diameter=17, name="Другая", ad_kind="single", pieces=1, sale_total=6000,
                   supplier_price=4000, terms=None, fee_pct=0, promo_pct=0, account_id=None, point="Бета", note="Заказ №7", now=NOW, author="o", today="2026-10-20", source="order")
    rep = order_report.build(conn, "2026-10-01", "2026-10-31")
    assert rep["lines"][0]["orders"] == 1 and rep["lines"][0]["fee"] == 75


def test_manual_reversed_and_out_of_period_sales_are_not_counted(tmp_path):
    conn = db(tmp_path)
    sell(conn, 1, "Бета", 20000, 4000)
    sell(conn, 2, "Бета", 20000, 4000, source="manual", name="вручную")
    sell(conn, 3, "Бета", 20000, 4000, day="2026-09-30")
    gone = sell(conn, 4, "Бета", 20000, 4000)
    sales.reverse_sale(conn, gone, NOW, "o", from_order=True)
    rep = order_report.build(conn, "2026-10-01", "2026-10-31")
    assert [r["orders"] for r in rep["lines"]] == [1]
    assert order_report.build(conn, "2026-10-01", "2026-10-31", point="Другая")["lines"] == []


def test_a_loss_after_the_fee_is_shown_as_it_is_and_the_cents_add_up(tmp_path):
    conn = db(tmp_path)
    sell(conn, 1, "Бета", 16100, 4000, pieces=4)             # margin 100, fee 75, net 25
    r = order_report.build(conn, "2026-10-01", "2026-10-31")["lines"][0]
    assert (r["net"], r["owner"], r["partner"]) == (25, 12.5, 12.5)
    sell(conn, 2, "Альфа", 16001, 4000, pieces=4)         # net -74: nothing is hidden
    r = next(x for x in order_report.build(conn, "2026-10-01", "2026-10-31")["lines"] if x["point"] == "Альфа")
    assert r["net"] == -74 and round(r["owner"] + r["partner"], 2) == -74


def test_the_page_and_the_csv(tmp_path):
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 10, 20, 9, 0, 0))
    client = app.test_client()
    assert client.get("/orders/report").status_code in (302, 401)
    login(client)
    conn = store.connect(app.config["DB_PATH"])
    html = client.get("/orders/report").get_data(as_text=True)
    assert "продаж по заказам нет" in html
    sell(conn, 1, "Бета", 20000, 4000)
    html = client.get("/orders/report?from=2026-10-01&to=2026-10-31").get_data(as_text=True)
    assert "Бета" in html and "3\u00a0925\u00a0₽" in html                       # the money filter (UX 4.5): whole rubles, no-break spaces
    csv_text = client.get("/orders/report/export.csv?from=2026-10-01&to=2026-10-31").get_data(as_text=True)
    assert csv_text.startswith("﻿Точка;") and "Бета;1;4;20000,00;16000,00;4000,00;75,00;3925,00;1962,50;1962,50" in csv_text
    assert client.get("/orders/report?from=bad&to=worse").status_code == 200           # bad dates fall back to the month
    assert "/orders/report" in client.get("/orders").get_data(as_text=True)


def test_odd_points_filter_and_the_unsafe_names(tmp_path):
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 10, 20, 9, 0, 0))
    client = app.test_client()
    assert client.get("/orders/report/export.csv").status_code in (302, 401)
    login(client)
    conn = store.connect(app.config["DB_PATH"])
    sell(conn, 1, "=HYPERLINK(1)", 20000, 4000)
    sell(conn, 2, "<b>x</b>", 20000, 4000)
    sell(conn, 3, "", 20000, 4000)                                           # an order without a point is shown, not lost
    html = client.get("/orders/report?from=2026-10-01&to=2026-10-31").get_data(as_text=True)
    assert "<b>x</b>" not in html and "&lt;b&gt;x&lt;/b&gt;" in html and "точка не указана" in html
    csv_text = client.get("/orders/report/export.csv?from=2026-10-01&to=2026-10-31").get_data(as_text=True)
    assert "'=HYPERLINK(1);" in csv_text and "\n=HYPERLINK" not in csv_text
    only = client.get("/orders/report?from=2026-10-01&to=2026-10-31&pt=%3Cb%3Ex%3C/b%3E").get_data(as_text=True)
    assert "точка не указана" not in only.split("<tbody>")[1]
