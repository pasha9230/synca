"""The rates of the report of the orders (75 ₽ to the point per order, 50 % of the net profit to the owner) have dates: the main check is that
with the default rates the report is the same to the kopeck as the report before the rates got dates (GOLDEN was taken from the old code)."""
from __future__ import annotations

from datetime import datetime

import pytest

import order_report_fixture as fixture
from panel_helpers import login, make_app
from tires_sync import order_report, store

NOW = fixture.NOW

GOLDEN = {
    ('2026-08-01', '2026-10-31', ''): {'lines': [{'point': 'Бета', 'orders': 5, 'pieces': 17, 'sale': 84050.96, 'cost': 71100.1, 'margin': 12950.86, 'fee': 375.0, 'net': 12575.86, 'owner': 6287.93, 'partner': 6287.93}, {'point': 'Альфа', 'orders': 4, 'pieces': 17, 'sale': 77203.86, 'cost': 71051.57, 'margin': 6152.29, 'fee': 300.0, 'net': 5852.29, 'owner': 2926.14, 'partner': 2926.15}, {'point': 'Гамма', 'orders': 2, 'pieces': 8, 'sale': 32345.66, 'cost': 29377.76, 'margin': 2967.9, 'fee': 150.0, 'net': 2817.9, 'owner': 1408.95, 'partner': 1408.95}, {'point': 'точка не указана', 'orders': 2, 'pieces': 3, 'sale': 13100.0, 'cost': 12000.0, 'margin': 1100.0, 'fee': 150.0, 'net': 950.0, 'owner': 475.0, 'partner': 475.0}], 'total': {'point': 'Итого', 'orders': 13, 'pieces': 45, 'sale': 206700.48, 'cost': 183529.43, 'margin': 23171.05, 'fee': 975.0, 'net': 22196.05, 'owner': 11098.02, 'partner': 11098.03}},
    ('2026-08-01', '2026-08-31', ''): {'lines': [{'point': 'Бета', 'orders': 2, 'pieces': 7, 'sale': 36000.95, 'cost': 28100.1, 'margin': 7900.85, 'fee': 150.0, 'net': 7750.85, 'owner': 3875.43, 'partner': 3875.42}, {'point': 'точка не указана', 'orders': 1, 'pieces': 2, 'sale': 9000.0, 'cost': 8000.0, 'margin': 1000.0, 'fee': 75.0, 'net': 925.0, 'owner': 462.5, 'partner': 462.5}, {'point': 'Альфа', 'orders': 1, 'pieces': 4, 'sale': 16101.37, 'cost': 16000.0, 'margin': 101.37, 'fee': 75.0, 'net': 26.37, 'owner': 13.19, 'partner': 13.18}], 'total': {'point': 'Итого', 'orders': 4, 'pieces': 13, 'sale': 61102.32, 'cost': 52100.1, 'margin': 9002.22, 'fee': 300.0, 'net': 8702.22, 'owner': 4351.11, 'partner': 4351.11}},
    ('2026-09-01', '2026-09-30', ''): {'lines': [{'point': 'Альфа', 'orders': 2, 'pieces': 9, 'sale': 51001.99, 'cost': 48001.32, 'margin': 3000.67, 'fee': 150.0, 'net': 2850.67, 'owner': 1425.34, 'partner': 1425.33}, {'point': 'Гамма', 'orders': 1, 'pieces': 4, 'sale': 12345.67, 'cost': 11600.0, 'margin': 745.67, 'fee': 75.0, 'net': 670.67, 'owner': 335.33, 'partner': 335.34}, {'point': 'Бета', 'orders': 1, 'pieces': 4, 'sale': 16050.0, 'cost': 16000.0, 'margin': 50.0, 'fee': 75.0, 'net': -25.0, 'owner': -12.5, 'partner': -12.5}], 'total': {'point': 'Итого', 'orders': 4, 'pieces': 17, 'sale': 79397.66, 'cost': 75601.32, 'margin': 3796.34, 'fee': 300.0, 'net': 3496.34, 'owner': 1748.17, 'partner': 1748.17}},
    ('2026-10-01', '2026-10-31', ''): {'lines': [{'point': 'Бета', 'orders': 2, 'pieces': 6, 'sale': 32000.01, 'cost': 27000.0, 'margin': 5000.01, 'fee': 150.0, 'net': 4850.01, 'owner': 2425.01, 'partner': 2425.0}, {'point': 'Альфа', 'orders': 1, 'pieces': 4, 'sale': 10100.5, 'cost': 7050.25, 'margin': 3050.25, 'fee': 75.0, 'net': 2975.25, 'owner': 1487.62, 'partner': 1487.63}, {'point': 'Гамма', 'orders': 1, 'pieces': 4, 'sale': 19999.99, 'cost': 17777.76, 'margin': 2222.23, 'fee': 75.0, 'net': 2147.23, 'owner': 1073.62, 'partner': 1073.61}, {'point': 'точка не указана', 'orders': 1, 'pieces': 1, 'sale': 4100.0, 'cost': 4000.0, 'margin': 100.0, 'fee': 75.0, 'net': 25.0, 'owner': 12.5, 'partner': 12.5}], 'total': {'point': 'Итого', 'orders': 5, 'pieces': 15, 'sale': 66200.5, 'cost': 55828.01, 'margin': 10372.49, 'fee': 375.0, 'net': 9997.49, 'owner': 4998.74, 'partner': 4998.75}},
    ('2026-08-01', '2026-10-31', 'Альфа'): {'lines': [{'point': 'Альфа', 'orders': 4, 'pieces': 17, 'sale': 77203.86, 'cost': 71051.57, 'margin': 6152.29, 'fee': 300.0, 'net': 5852.29, 'owner': 2926.14, 'partner': 2926.15}], 'total': {'point': 'Итого', 'orders': 4, 'pieces': 17, 'sale': 77203.86, 'cost': 71051.57, 'margin': 6152.29, 'fee': 300.0, 'net': 5852.29, 'owner': 2926.14, 'partner': 2926.15}},
    ('2026-09-15', '2026-10-10', ''): {'lines': [{'point': 'Бета', 'orders': 2, 'pieces': 6, 'sale': 32000.01, 'cost': 27000.0, 'margin': 5000.01, 'fee': 150.0, 'net': 4850.01, 'owner': 2425.01, 'partner': 2425.0}, {'point': 'Альфа', 'orders': 1, 'pieces': 5, 'sale': 35000.99, 'cost': 32001.32, 'margin': 2999.67, 'fee': 75.0, 'net': 2924.67, 'owner': 1462.34, 'partner': 1462.33}, {'point': 'Гамма', 'orders': 1, 'pieces': 4, 'sale': 12345.67, 'cost': 11600.0, 'margin': 745.67, 'fee': 75.0, 'net': 670.67, 'owner': 335.33, 'partner': 335.34}], 'total': {'point': 'Итого', 'orders': 4, 'pieces': 15, 'sale': 79346.67, 'cost': 70601.32, 'margin': 8745.35, 'fee': 300.0, 'net': 8445.35, 'owner': 4222.68, 'partner': 4222.67}},
    ('2026-07-01', '2026-07-31', ''): {'lines': [], 'total': {'point': 'Итого', 'orders': 0, 'pieces': 0, 'sale': 0, 'cost': 0, 'margin': 0, 'fee': 0, 'net': 0, 'owner': 0.0, 'partner': 0.0}},
}
# the two last columns are named by the panel's users (test_share_labels.py): with none named, «Владельцу» / «Партнёру»
GOLDEN_CSV = '\ufeffТочка;Заказов;Шин;Выручка;Закупка;Прибыль;Выплата точке;Чистая прибыль;Владельцу;Партнёру\nБета;5;17;84050,96;71100,10;12950,86;375,00;12575,86;6287,93;6287,93\nАльфа;4;17;77203,86;71051,57;6152,29;300,00;5852,29;2926,14;2926,15\nГамма;2;8;32345,66;29377,76;2967,90;150,00;2817,90;1408,95;1408,95\nточка не указана;2;3;13100,00;12000,00;1100,00;150,00;950,00;475,00;475,00\nИтого;13;45;206700,48;183529,43;23171,05;975,00;22196,05;11098,02;11098,03\n'


def db(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    fixture.fill(conn)
    return conn


@pytest.mark.parametrize("period", fixture.PERIODS)
def test_with_the_default_rates_the_report_is_the_old_one_to_the_kopeck(tmp_path, period):
    rep = order_report.build(db(tmp_path), *period)
    assert {"lines": rep["lines"], "total": rep["total"]} == GOLDEN[period]


def test_with_the_default_rates_the_csv_is_the_old_one_for_the_orders(tmp_path):
    text = order_report.export_csv(order_report.build(db(tmp_path), "2026-08-01", "2026-10-31"))
    assert text.startswith(GOLDEN_CSV)


def test_a_new_rate_from_a_date_changes_only_the_orders_from_that_date(tmp_path):
    conn = db(tmp_path)
    assert order_report.set_rates(conn, "2026-10-01", "2026-10-01", NOW, "owner", point_fee=100, owner_share=0.6) == ["point_fee", "owner_share"]
    for period in fixture.PERIODS:
        if period[1] < "2026-10-01":                                       # wholly before the date: the old report to the kopeck
            rep = order_report.build(conn, *period)
            assert {"lines": rep["lines"], "total": rep["total"]} == GOLDEN[period]
    october = order_report.build(conn, "2026-10-01", "2026-10-31")
    old = GOLDEN[("2026-10-01", "2026-10-31", "")]
    assert october["total"]["orders"] == old["total"]["orders"] == 5 and october["total"]["fee"] == 500
    assert october["total"]["owner"] == round(october["total"]["net"] * 0.6, 2) and october["rates_used"] == [(100.0, 0.6)]
    summer_and_autumn = order_report.build(conn, "2026-08-01", "2026-10-31")["total"]
    assert summer_and_autumn["fee"] == 75 * 8 + 100 * 5


def test_a_rate_change_inside_the_period_counts_each_order_by_its_own_day(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    sell = fixture._sale
    sell(conn, "2026-10-05", "Бета", "Заказ №1", "order", 20000, 4000, 4, 1)          # margin 4000, before the date
    sell(conn, "2026-10-15", "Бета", "Заказ №2", "order", 20000, 4000, 4, 2)          # margin 4000, after it
    order_report.set_rates(conn, "2026-10-10", "2026-10-01", NOW, "owner", point_fee=100, owner_share=0.6)
    line = order_report.build(conn, "2026-10-01", "2026-10-31")["lines"][0]
    assert (line["fee"], line["net"], line["owner"], line["partner"]) == (175, 7825, 1962.5 + 2340, 7825 - 4302.5)
    early = order_report.build(conn, "2026-10-01", "2026-10-09")["lines"][0]
    assert (early["fee"], early["owner"]) == (75, 1962.5)
    assert order_report.build(conn, "2026-10-01", "2026-10-31")["total"]["owner"] == 4302.5


def test_the_rates_are_checked_saved_once_and_audited(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    with pytest.raises(ValueError, match="прошлые отчёты"):
        order_report.set_rates(conn, "2026-10-19", "2026-10-20", NOW, "owner", point_fee=100)
    for bad in (dict(point_fee=-1), dict(point_fee=100001), dict(owner_share=1.01), dict(owner_share=-0.1)):
        with pytest.raises(ValueError):
            order_report.set_rates(conn, "2026-10-20", "2026-10-20", NOW, "owner", **bad)
    with pytest.raises(ValueError, match="ГГГГ"):
        order_report.set_rates(conn, "завтра", "2026-10-20", NOW, "owner", point_fee=100)
    assert order_report.set_rates(conn, "2026-10-20", "2026-10-20", NOW, "owner", point_fee=75, owner_share=0.5) == []   # the defaults: nothing new
    assert order_report.list_rates(conn) == []
    assert order_report.set_rates(conn, "2026-11-01", "2026-10-20", NOW, "pavel", point_fee=90) == ["point_fee"]
    assert order_report.set_rates(conn, "2026-11-01", "2026-10-20", NOW, "pavel", point_fee=95) == ["point_fee"]       # the same date: replaced
    assert [(r["name"], r["valid_from"], r["value"]) for r in order_report.list_rates(conn)] == [("point_fee", "2026-11-01", 95.0)]
    assert order_report.rate_on(order_report.list_rates(conn), "point_fee", "2026-10-31") == 75
    audit = [a for a in store.list_audit(conn) if a["action"] == "order_rate"]
    assert [a["detail"] for a in reversed(audit)] == [{"name": "point_fee", "valid_from": "2026-11-01", "from": 75.0, "to": 90.0},
                                                       {"name": "point_fee", "valid_from": "2026-11-01", "from": 90.0, "to": 95.0}]
    assert audit[0]["author"] == "pavel"


def test_the_manual_sales_are_a_line_of_their_own_outside_the_total(tmp_path):
    conn = db(tmp_path)
    rep = order_report.build(conn, "2026-08-01", "2026-10-31")
    assert rep["manual"] == {"point": "Продажи вручную", "sales": 3, "pieces": 12, "sale": 43778.27, "cost": 34000.0, "margin": 9778.27}
    assert rep["total"] == GOLDEN[("2026-08-01", "2026-10-31", "")]["total"]
    assert order_report.build(conn, "2026-08-01", "2026-10-31", "Альфа")["manual"]["sales"] == 1
    assert order_report.build(conn, "2026-07-01", "2026-07-31")["manual"] is None
    text = order_report.export_csv(rep)
    assert text.endswith("Продажи вручную (не входят в итог);;12;43778,27;34000,00;9778,27;;;;\n")


def test_the_page_shows_money_the_manual_line_and_saves_the_rates(tmp_path):
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 10, 20, 9, 0, 0))
    client = app.test_client()
    tok = login(client)
    conn = store.connect(app.config["DB_PATH"])
    fixture.fill(conn)
    html = client.get("/orders/report?from=2026-08-01&to=2026-10-31").get_data(as_text=True)
    assert "12 576 ₽" in html and "12 575,86" not in html                      # the money filter: whole rubles, no-break spaces
    assert "Продажи вручную" in html and "9 778 ₽" in html
    assert "Ставки не менялись" in html and 'value="75"' in html and 'value="50"' in html
    assert client.post("/orders/report/rates", data={"csrf": tok, "point_fee": "100", "owner_pct": "60", "valid_from": "2026-10-19"}).status_code == 302
    assert order_report.list_rates(conn) == []                                            # yesterday: refused
    client.post("/orders/report/rates", data={"csrf": tok, "point_fee": "1 000", "owner_pct": "60", "valid_from": "2026-10-21"})
    assert [(r["name"], r["value"], r["author"]) for r in order_report.list_rates(conn)] == [("owner_share", 0.6, "owner"), ("point_fee", 1000.0, "owner")]
    html = client.get("/orders/report?from=2026-08-01&to=2026-10-31").get_data(as_text=True)
    assert "2026-10-21" in html and "1000\u00a0₽" in html and "60 %" in html
    assert GOLDEN_CSV in client.get("/orders/report/export.csv?from=2026-08-01&to=2026-10-31").get_data(as_text=True)   # tomorrow's rate: today's report is the same
    client.post("/orders/report/rates", data={"csrf": tok, "point_fee": "сто", "owner_pct": "60", "valid_from": "2026-10-21"})
    assert len(order_report.list_rates(conn)) == 2
    assert client.post("/orders/report/rates", data={"point_fee": "1", "owner_pct": "1", "valid_from": "2026-10-21"}).status_code in (400, 403)


def test_changing_one_rate_in_the_form_keeps_a_future_rate_of_the_other(tmp_path):
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 10, 20, 9, 0, 0))
    client = app.test_client()
    tok = login(client)
    conn = store.connect(app.config["DB_PATH"])
    order_report.set_rates(conn, "2026-11-01", "2026-10-20", NOW, "owner", point_fee=100)
    html = client.get("/orders/report").get_data(as_text=True)
    assert 'name="point_fee_was" value="75"' in html                                     # the form is filled with today's rates
    client.post("/orders/report/rates", data={"csrf": tok, "point_fee": "75", "point_fee_was": "75", "owner_pct": "60", "owner_pct_was": "50",
                                              "valid_from": "2026-12-01"})
    rates = order_report.list_rates(conn)
    assert order_report.rate_on(rates, "point_fee", "2026-12-15") == 100 and order_report.rate_on(rates, "owner_share", "2026-12-15") == 0.6
    page = client.post("/orders/report/rates", data={"csrf": tok, "point_fee": "75", "point_fee_was": "75", "owner_pct": "60", "owner_pct_was": "60",
                                                     "valid_from": "2027-01-01"}, follow_redirects=True).get_data(as_text=True)
    assert "Ничего не записано: поля не менялись" in page and order_report.rate_on(order_report.list_rates(conn), "point_fee", "2027-01-01") == 100
    order_report.set_rates(conn, "2026-10-21", "2026-10-20", NOW, "owner", point_fee=75.1234)
    assert order_report.rate_on(order_report.list_rates(conn), "point_fee", "2026-10-21") == 75.12                # kopecks, as the form shows them
