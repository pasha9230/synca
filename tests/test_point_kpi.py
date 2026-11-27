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


def test_the_handoff_bonus_is_per_tyre_from_the_first_of_december_and_none_before(tmp_path):
    """The owner 2026-11-25: the bonuses start on 2026-12-01 whatever rate is saved (the time before is paid by a one-time correction)."""
    conn = db(tmp_path)
    a, b = point(conn, "Альфа"), point(conn, "Бета")
    sell(conn, 1, b, pieces=4, day="2026-11-28")
    handed(conn, 1, a)
    sell(conn, 2, b, pieces=3, day=order_report.HANDOFF_PER_TYRE_FROM)
    handed(conn, 2, a)
    order_report.set_point_rates(conn, a, "2026-11-25", "2026-11-25", NOW, "owner", {"handoff_fee": 100})   # a point's own rate earlier: no bonus
    got = {e["order"]: (e["handoff"], e["handoff_pieces"]) for e in order_report.point_income(conn, "2026-11-01", "2026-12-31", a)["orders"]}
    assert order_report.HANDOFF_PER_TYRE_FROM == order_report.BONUS_FROM == "2026-12-01"
    assert got == {1: (0, 0), 2: (300, 3)}
    rep = order_report.build(conn, "2026-11-01", "2026-12-31")
    assert rep["handoff"]["clients"] == 2 and rep["handoff"]["fee"] == 300              # both clients counted, the bonus of December only


def test_kpi_by_orders_or_by_tyres(tmp_path):
    """The owner 2026-11-27: «ИЛИ» — a premium for every full kpi_orders orders OR every full kpi_tyres tyres, whichever gives more."""
    conn = db(tmp_path)
    a = point(conn, "Альфа")
    assert order_report.KPI_TYRES == 36 and order_report.DEFAULTS["kpi_tyres"] == 36
    clients(conn, a, 9, pieces=3)                                            # 9 orders, 27 tyres: no premium
    mine = order_report.point_income(conn, "2026-12-01", "2026-12-31", a)
    assert mine["kpi"] == 0 and mine["premiums"] == []
    month = mine["progress"]["2026-12"]
    assert (month["orders"], month["tyres"], month["cycles"], month["need_orders"], month["need_tyres"]) == (9, 27, 0, 10, 36)
    clients(conn, a, 1, first=50, pieces=1, day="2026-12-20")               # 10 orders, 28 tyres: by the orders, on the 20th
    mine = order_report.point_income(conn, "2026-12-01", "2026-12-31", a)
    assert mine["kpi"] == 1500 and [(p["day"], p["cycle"], p["item"]) for p in mine["premiums"]] == [("2026-12-20", 1, "kpi:2026-12:1")]
    assert mine["total"] == round(sum(e["total"] for e in mine["orders"]) + 1500, 2)


@pytest.mark.parametrize("pieces, cycles", [(45, 1), (72, 2), (36, 1), (35, 0), (100, 2)])
def test_kpi_by_the_tyres_of_one_order(tmp_path, pieces, cycles):
    """One order of 45 tyres → 1 premium and 9 toward the next; 72 → 2; exactly 36 → 1; not limited."""
    conn = db(tmp_path)
    a = point(conn, "Альфа")
    clients(conn, a, 1, pieces=pieces)
    mine = order_report.point_income(conn, "2026-12-01", "2026-12-31", a)
    assert mine["kpi"] == 1500 * cycles and mine["progress"]["2026-12"]["cycles"] == cycles


def test_kpi_tyres_threshold_of_the_settings(tmp_path):
    conn = db(tmp_path)
    a = point(conn, "Альфа")
    order_report.set_rates(conn, "2026-12-01", "2026-11-27", NOW, "owner", kpi_tyres=40)
    clients(conn, a, 1, pieces=39)
    assert order_report.point_income(conn, "2026-12-01", "2026-12-31", a)["kpi"] == 0
    clients(conn, a, 1, first=2, pieces=1)                                    # 40 tyres
    mine = order_report.point_income(conn, "2026-12-01", "2026-12-31", a)
    assert mine["kpi"] == 1500 and mine["progress"]["2026-12"]["need_tyres"] == 40


def test_kpi_progress_toward_the_next_premium(tmp_path):
    from tires_sync.panel import income_view
    conn = db(tmp_path)
    a = point(conn, "Альфа")
    clients(conn, a, 1, pieces=45)
    report = order_report.build(conn, "2026-12-01", "2026-12-31")
    k = income_view._kpi(report, conn, a, "2026-12", "2026-12-15")
    assert (k["cycles"], k["cycle_tyres"], k["tyres_left"], k["cycle_orders"], k["orders_left"]) == (1, 9, 27, 0, 19)
    b = point(conn, "Бета")
    clients(conn, b, 10, first=100, pieces=2)                                # 10 orders, 20 tyres: 1 premium by the orders
    report = order_report.build(conn, "2026-12-01", "2026-12-31")
    k = income_view._kpi(report, conn, b, "2026-12", "2026-12-15")
    assert (k["cycles"], k["orders_left"], k["cycle_orders"], k["tyres_left"], k["cycle_tyres"]) == (1, 10, 0, 52, 0)


def test_kpi_cycles_are_not_limited(tmp_path):
    conn = db(tmp_path)
    a = point(conn, "Альфа")
    clients(conn, a, 20, pieces=4)                                           # 20 orders / 80 tyres → max(2, 2) = 2 premiums
    mine = order_report.point_income(conn, "2026-12-01", "2026-12-31", a)
    assert mine["kpi"] == 3000 and mine["progress"]["2026-12"]["cycles"] == 2
    clients(conn, a, 10, first=100, pieces=1)                                # 30 orders, 90 tyres → max(3, 2) = 3
    assert order_report.point_income(conn, "2026-12-01", "2026-12-31", a)["kpi"] == 4500


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
    clients(conn, a, 9, first=100, pieces=3)
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


def test_the_tabs_say_the_day_of_a_change_up_to_the_day_after_it(tmp_path):
    """The owner 2026-11-27: «Изменения с ДД.ММ.ГГГГ» on the tab whose rates change; up to the end of the day after, then gone."""
    from tires_sync.panel import income_view
    conn = db(tmp_path)
    a = point(conn, "Альфа")
    assert income_view.changes_day(conn, a, "income", "2026-11-28") == ""   # the start of the bonuses: not on «Доход» (review #96)
    for tab in ("bonus", "kpi"):                                              # the start of the bonuses and KPI
        assert income_view.changes_day(conn, a, tab, "2026-11-28") == "01.12.2026"
        assert income_view.changes_day(conn, a, tab, "2026-12-02") == "01.12.2026"
        assert income_view.changes_day(conn, a, tab, "2026-12-03") == ""
    order_report.set_rates(conn, "2027-01-10", "2026-12-15", NOW, "owner", kpi_tyres=40, point_fee=80)
    assert income_view.changes_day(conn, a, "income", "2026-12-15") == "10.01.2027"
    assert income_view.changes_day(conn, a, "kpi", "2026-12-15") == "10.01.2027"
    assert income_view.changes_day(conn, a, "bonus", "2026-12-15") == ""
    order_report.set_point_rates(conn, a, "2027-02-01", "2026-12-15", NOW, "owner", {"handoff_fee": 60})
    assert income_view.changes_day(conn, a, "bonus", "2026-12-15") == "01.02.2027"
    assert income_view.changes_day(conn, a, "kpi", "2027-01-12") == ""
