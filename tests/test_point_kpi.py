"""KPI, the own rates of a point, the bonus of a handed client per tyre (ТЗ 92, 95): one count in order_report.build."""
from __future__ import annotations

import pytest

from test_point_income import db, handed, point

from tires_sync import order_report, sales

NOW = "2026-12-15T12:00:00"


def sell(conn, order_id, point_id, pieces=4, day="2026-12-10"):
    return sales.add_sale(conn, day=day, goods_id="g%d" % order_id, brand_key="nokian", season="winter", diameter=16, name="Nokian", ad_kind="single",
                          pieces=pieces, sale_total=5000.0 * pieces, supplier_price=4000, terms=None, fee_pct=0, promo_pct=0, account_id=None,
                          point="", point_id=point_id, note="Заказ №%d" % order_id, now=NOW, author="o", today="2027-12-31", source="order")


def clients(conn, to, n, first=1, pieces=4, day="2026-12-05", issued=None):
    """`n` orders of clients the point `to` handed over, issued (sold) by `issued` (else by `to`)."""
    for order_id in range(first, first + n):
        sell(conn, order_id, issued or to, pieces=pieces, day=day)
        handed(conn, order_id, to)


def test_the_handoff_bonus_is_per_tyre_from_its_day_and_per_order_before(tmp_path):
    conn = db(tmp_path)
    a, b = point(conn, "Альфа"), point(conn, "Бета")
    sell(conn, 1, b, pieces=4, day="2026-11-20")
    handed(conn, 1, a)
    sell(conn, 2, b, pieces=3, day=order_report.HANDOFF_PER_TYRE_FROM)
    handed(conn, 2, a)
    got = {e["order"]: (e["handoff"], e["handoff_pieces"]) for e in order_report.point_income(conn, "2026-11-01", "2026-11-30", a)["orders"]}
    assert got == {1: (50, 0), 2: (150, 3)}


def test_kpi_one_cycle_needs_both_thresholds(tmp_path):
    conn = db(tmp_path)
    a = point(conn, "Альфа")
    clients(conn, a, 10, pieces=3)                                           # 10 orders, 30 tyres: no premium
    mine = order_report.point_income(conn, "2026-12-01", "2026-12-31", a)
    assert mine["kpi"] == 0 and mine["premiums"] == []
    month = mine["progress"]["2026-12"]
    assert (month["orders"], month["tyres"], month["cycles"], month["need_orders"], month["need_tyres"]) == (10, 30, 0, 10, 38)
    clients(conn, a, 1, first=50, pieces=8, day="2026-12-20")               # 11 orders, 38 tyres: the cycle closes on the 20th
    mine = order_report.point_income(conn, "2026-12-01", "2026-12-31", a)
    assert mine["kpi"] == 1500 and [(p["day"], p["cycle"], p["item"]) for p in mine["premiums"]] == [("2026-12-20", 1, "kpi:2026-12:1")]
    assert mine["total"] == round(sum(e["total"] for e in mine["orders"]) + 1500, 2)


def test_kpi_cycles_are_not_limited(tmp_path):
    conn = db(tmp_path)
    a = point(conn, "Альфа")
    clients(conn, a, 20, pieces=4)                                           # 20 orders / 80 tyres → min(2, 2) = 2 premiums
    mine = order_report.point_income(conn, "2026-12-01", "2026-12-31", a)
    assert mine["kpi"] == 3000 and mine["progress"]["2026-12"]["cycles"] == 2
    clients(conn, a, 10, first=100, pieces=1)                                # 30 orders, 90 tyres → min(3, 2) = still 2
    assert order_report.point_income(conn, "2026-12-01", "2026-12-31", a)["kpi"] == 3000


def test_kpi_goes_to_the_handing_point_counts_its_month_and_reaches_the_report(tmp_path):
    conn = db(tmp_path)
    a, b = point(conn, "Альфа"), point(conn, "Бета")
    clients(conn, a, 10, pieces=4, issued=b)                                 # issued by Бета, handed by Альфа
    assert order_report.point_income(conn, "2026-12-01", "2026-12-31", b)["kpi"] == 0
    assert order_report.point_income(conn, "2026-12-01", "2026-12-31", a)["kpi"] == 1500
    # a period that starts mid-month still counts the month from its first day, and pays the premium only if it closed in the period
    assert order_report.point_income(conn, "2026-12-06", "2026-12-31", a)["kpi"] == 0
    assert order_report.point_income(conn, "2026-12-05", "2026-12-05", a)["kpi"] == 1500
    rep = order_report.build(conn, "2026-12-01", "2026-12-31")
    assert rep["kpi"]["fee"] == 1500 and rep["fees"]["kpi"] == 1500
    line = {r["point"]: r for r in rep["lines"]}["Альфа"]
    assert line["fee"] == 10 * 200 + 1500                                    # 50 ₽ a tyre × 40 + the premium
    assert {f["point"]: f["kpi"] for f in rep["fees"]["lines"]}["Альфа"] == 1500


def test_kpi_not_before_its_start_and_not_for_reversed_or_plain_orders(tmp_path):
    conn = db(tmp_path)
    a = point(conn, "Альфа")
    clients(conn, a, 10, pieces=4, day="2026-11-27")                          # November: before KPI_FROM
    assert order_report.point_income(conn, "2026-11-01", "2026-11-30", a)["kpi"] == 0
    clients(conn, a, 9, first=100, pieces=4)
    for order_id in range(200, 205):
        sell(conn, order_id, a, pieces=4)                                    # its own plain orders: not handed, not counted
    sale = sell(conn, 300, a, pieces=4)
    handed(conn, 300, a)
    sales.reverse_sale(conn, sale, NOW, "o", from_order=True)               # reversed: not closed
    mine = order_report.point_income(conn, "2026-12-01", "2026-12-31", a)
    assert mine["kpi"] == 0 and mine["progress"]["2026-12"]["orders"] == 9


def test_the_own_rates_of_a_point(tmp_path):
    conn = db(tmp_path)
    a, b = point(conn, "Альфа"), point(conn, "Бета")
    assert order_report.set_point_rates(conn, a, "2026-12-01", "2026-12-01", NOW, "owner",
                                        {"point_fee": 100, "handoff_fee": 60, "kpi_orders": 5, "kpi_tyres": 20, "kpi_bonus": 2000}) == \
        ["point_fee", "handoff_fee", "kpi_orders", "kpi_tyres", "kpi_bonus"]
    clients(conn, a, 5, pieces=4)
    clients(conn, b, 5, first=50, pieces=4)
    mine = order_report.point_income(conn, "2026-12-01", "2026-12-31", a)
    assert {e["issue"] for e in mine["orders"]} == {400} and {e["handoff"] for e in mine["orders"]} == {240}
    assert mine["kpi"] == 2000
    theirs = order_report.point_income(conn, "2026-12-01", "2026-12-31", b)
    assert {e["issue"] for e in theirs["orders"]} == {300} and theirs["kpi"] == 0
    assert order_report.point_rates_now(conn, a, "2026-12-15")["point_fee"] == 100
    # back to the general rate from a later day; a past day keeps its own
    assert order_report.set_point_rates(conn, a, "2026-12-20", "2026-12-15", NOW, "owner", {"point_fee": None}) == ["point_fee"]
    assert order_report.point_rates_now(conn, a, "2026-12-21")["point_fee"] == 75
    assert order_report.point_rates_now(conn, a, "2026-12-15")["point_fee"] == 100
    assert conn.execute("SELECT COUNT(*) FROM audit WHERE action = 'point_rate'").fetchone()[0] == 6
    for bad in ({"kpi_orders": 0}, {"kpi_tyres": 2.5}, {"kpi_bonus": -1}, {"owner_share": 0.3}):
        with pytest.raises(ValueError):
            order_report.set_point_rates(conn, a, "2026-12-20", "2026-12-15", NOW, "owner", bad)
    with pytest.raises(ValueError):                                          # never in the past
        order_report.set_point_rates(conn, a, "2026-12-01", "2026-12-15", NOW, "owner", {"point_fee": 1})


def test_the_general_kpi_rates_are_dated(tmp_path):
    conn = db(tmp_path)
    a = point(conn, "Альфа")
    assert order_report.set_rates(conn, "2027-01-01", "2026-12-15", NOW, "owner", kpi_orders=2, kpi_tyres=8, kpi_bonus=500) == \
        ["kpi_orders", "kpi_tyres", "kpi_bonus"]
    clients(conn, a, 4, pieces=4, day="2026-12-10")
    clients(conn, a, 4, first=50, pieces=4, day="2027-01-10")
    assert order_report.point_income(conn, "2026-12-01", "2026-12-31", a)["kpi"] == 0
    assert order_report.point_income(conn, "2027-01-01", "2027-01-31", a)["kpi"] == 1000
    with pytest.raises(ValueError):
        order_report.set_rates(conn, "2027-01-01", "2026-12-15", NOW, "owner", kpi_orders=0)


def test_a_client_given_by_phone_counts_as_handed(tmp_path):
    conn = db(tmp_path)
    a, b = point(conn, "Альфа"), point(conn, "Бета")
    sell(conn, 1, b, pieces=4)
    conn.execute("INSERT INTO order_source_point (order_id, point_id, at, author) VALUES (1, ?, ?, 'o')", (a, NOW))
    conn.commit()
    [entry] = order_report.point_income(conn, "2026-12-01", "2026-12-31", a)["orders"]
    assert (entry["order"], entry["handoff"], entry["issue"]) == (1, 200, 0)
