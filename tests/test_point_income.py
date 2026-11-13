"""The income of a point (order_report.accruals): 75 ₽ for an issued order, the bonus for a handed-over client and — new — the bonus for
every tyre the point sold from «На ОТХ» (oth_point_sale: the window «Продать»), by the day of the sale, to the point of the sale; a sale
undone takes its sums away. «Доход точки» shows a point its own sums only; «Переданные клиенты» shows the bonus of each client; the report
of the orders and the analytics split the fees into the issue and the bonuses. Made-up data only."""
from __future__ import annotations

from test_mypoint import manager
from test_order_reserve import db, stage
from test_oth_point_sale import BUYER, PHONE, SOLD, item, sale_rule, sell
from test_oth_sale import panel
from test_oth_storage import MOVED, at_point, record
from test_point_new_view import flag
from tires_sync import analytics, order_points as points, order_report, orders, oth_point_sale

OTH_SPLIT = True                       # records of storage on «На ОТХ» (conftest)
DAY = SOLD[:10]


def split_at(conn, name=None):
    """A record of storage on «На ОТХ» at the point `name` (made if missing), else at the point of card_at (Бета)."""
    cid = at_point(conn)
    if name:
        pid = next((p["id"] for p in points.list_points(conn) if p["name"] == name), None) or points.save_point(conn, dict(name=name, manager_name="Ольга"))
        conn.execute("UPDATE order_card SET point_id = ? WHERE id = ?", (pid, cid))
        conn.commit()
    assert orders.move_card(conn, cid, stage(conn, "oth"), MOVED, "o", changes={"cancel_reason": "Клиент не пришёл"})
    return cid, record(conn, cid)


def point_of(conn, x):
    return orders.get_card(conn, x)["point_id"]


def oth(entries):
    return [(e["order"], e["day"], e["pieces"], e["amount"]) for e in entries if e["kind"] == "oth"]


def test_one_tyre_sold_from_oth_brings_the_point_50_and_four_bring_200(tmp_path):
    conn = db(tmp_path)
    sale_rule(conn)
    cid, x = split_at(conn)
    one = sell(conn, x, qty=1)
    _, y = split_at(conn)
    four = sell(conn, y, qty=4)
    got = order_report.accruals(conn, DAY, DAY, point_of(conn, x))
    assert oth(got) == [(one, DAY, 1, 50.0), (four, DAY, 4, 200.0)]
    summary = order_report.income(got)
    assert (summary["oth"], summary["oth_pieces"], summary["issue"], summary["handoff"]) == (250.0, 5, 150.0, 0.0)   # 75 ₽ for each order as before
    assert (summary["bonus"], summary["total"]) == (250.0, 400.0)


def test_a_sale_undone_takes_its_bonus_away(tmp_path):
    conn = db(tmp_path)
    sale_rule(conn)
    cid, x = split_at(conn)
    b = sell(conn, x, qty=2)
    assert oth(order_report.accruals(conn, DAY, DAY)) == [(b, DAY, 2, 100.0)]
    orders.reverse_sales(conn, b, SOLD, "o")
    assert order_report.accruals(conn, DAY, DAY) == []


def test_each_point_gets_the_bonus_of_its_own_sales(tmp_path):
    conn = db(tmp_path)
    sale_rule(conn)
    _, x = split_at(conn)
    _, y = split_at(conn, "Гамма")
    bx, by = sell(conn, x, qty=1), sell(conn, y, qty=3)
    assert point_of(conn, x) != point_of(conn, y)
    assert oth(order_report.accruals(conn, DAY, DAY, point_of(conn, x))) == [(bx, DAY, 1, 50.0)]
    assert oth(order_report.accruals(conn, DAY, DAY, point_of(conn, y))) == [(by, DAY, 3, 150.0)]
    report = order_report.build(conn, DAY, DAY)
    lines = {r["point"]: r for r in report["payouts"]["lines"]}
    assert (lines["Бета"]["issue"], lines["Бета"]["oth"], lines["Бета"]["total"]) == (75.0, 50.0, 125.0)
    assert (lines["Гамма"]["issue"], lines["Гамма"]["oth"], lines["Гамма"]["total"]) == (75.0, 150.0, 225.0)
    total = report["payouts"]["total"]
    assert (total["issue"], total["bonus"], total["total"]) == (150.0, 200.0, 350.0)
    fees = {r["point"]: r["fee"] for r in report["lines"]}                       # the bonus is a part of «Выплата точке» and of the net
    assert fees == {"Бета": 125.0, "Гамма": 225.0} and report["total"]["fee"] == 350.0
    assert report["total"]["net"] == round(report["total"]["margin"] - 350.0, 2)
    money = analytics.money(conn, DAY, DAY)
    assert money["point_fees"] == 350.0 and money["payouts"]["total"]["bonus"] == 200.0


def test_the_bonus_is_of_the_day_of_the_sale_and_of_its_rate(tmp_path):
    conn = db(tmp_path)
    sale_rule(conn)
    _, x = split_at(conn)
    sell(conn, x, qty=1)
    assert order_report.accruals(conn, "2026-10-23", "2026-10-31") == []          # another period: nothing
    assert order_report.accruals(conn, "2026-10-01", "2026-10-21") == []
    assert oth(order_report.accruals(conn, "2026-10-01", "2026-10-31"))[0][3] == 50.0
    order_report.set_rates(conn, "2026-10-23", "2026-10-22", SOLD, "o", oth_sale_fee=70)
    conn.commit()
    _, y = split_at(conn)
    b = oth_point_sale.sell(conn, y, {item(conn, y): 2}, BUYER, PHONE, None, None, False, "2026-10-23T12:00:00", "менеджер Бета")
    assert oth(order_report.accruals(conn, "2026-10-23", "2026-10-23")) == [(b, "2026-10-23", 2, 140.0)]
    assert oth(order_report.accruals(conn, DAY, DAY))[0][3] == 50.0                # the earlier day keeps its rate


def test_tyres_taken_from_oth_by_the_office_bring_no_bonus(tmp_path):
    conn = db(tmp_path)
    sale_rule(conn)
    _, x = split_at(conn)
    b = sell(conn, x, qty=1)
    conn.execute("DELETE FROM audit WHERE action = 'oth_point_sale'")              # as an order of the office: not made by the window
    conn.commit()
    got = order_report.accruals(conn, DAY, DAY)
    assert oth(got) == [] and [e["kind"] for e in got] == ["issue"] and got[0]["order"] == b


# ---- the panel -------------------------------------------------------------------------------------------------------------------------

def test_the_point_sees_its_own_income_only(tmp_path):
    app, owner, tok, conn = panel(tmp_path)
    sale_rule(conn)
    _, x = split_at(conn)
    _, y = split_at(conn, "Гамма")
    sell(conn, x, qty=1)
    sell(conn, y, qty=3)
    for view in ("old", "new"):
        if view == "new":
            flag(conn, "all")
        mine = manager(app, conn, point_of(conn, x), "gamma_" + view)
        page = mine.get("/my-point/income?from=%s&to=%s&p=%d" % (DAY, DAY, point_of(conn, y))).get_data(as_text=True)   # ?p= is not the point's
        assert "Доход точки" in page and "Бета" in page and "Гамма" not in page
        assert "за выдачу" in page and "бонусы" in page and "итого" in page
        assert 'data-income-issue>75' in page and 'data-income-bonus>50' in page and 'data-income-total>125' in page
        assert "бонус за продажу с ОТХ, 1 шт." in page and "3 шт." not in page
        assert 'href="/my-point/income"' in page
    seen = owner.get("/my-point/income?from=%s&to=%s&p=%d" % (DAY, DAY, point_of(conn, y))).get_data(as_text=True)
    assert "Гамма" in seen and 'data-income-bonus>150' in seen


def test_without_the_right_to_the_money_the_income_is_closed(tmp_path, monkeypatch):
    app, owner, tok, conn = panel(tmp_path)
    from tires_sync.panel import routes_mypoint
    monkeypatch.setattr(routes_mypoint, "has_right", lambda right: False)
    assert owner.get("/my-point/income").status_code == 403


def test_the_report_and_the_analytics_show_the_split_of_the_fees(tmp_path):
    app, owner, tok, conn = panel(tmp_path)
    sale_rule(conn)
    _, x = split_at(conn)
    sell(conn, x, qty=2)
    page = owner.get("/orders/report?from=%s&to=%s" % (DAY, DAY)).get_data(as_text=True)
    assert "Выплаты точкам" in page and "Бонус за ОТХ" in page and "data-payouts" in page and "(2 шт.)" in page
    page = owner.get("/analytics?from=%s&to=%s" % (DAY, DAY)).get_data(as_text=True)
    assert "Выплаты точкам" in page and "бонусы 100" in page


def test_the_handed_clients_show_their_bonus(tmp_path):
    from tires_sync import sales
    app, owner, tok, conn = panel(tmp_path)
    now = "2026-10-07T12:00:00"                                                              # the panel's clock in the tests (panel_helpers)
    order_report.set_rates(conn, now[:10], now[:10], now, "o", handoff_fee=50)              # before the 24th the default is 0
    beta = points.save_point(conn, dict(name="Бета", manager_name="Алексей"))
    gamma = points.save_point(conn, dict(name="Гамма", manager_name="Ольга"))
    issued, waits = (orders.create_card(conn, dict(client_name="Иван", client_phone=phone, point_id=gamma), now, "o")
                     for phone in ("+7 900 000 00 01", "+7 900 000 00 02"))
    for n, order_id in enumerate((issued, waits)):
        conn.execute("INSERT INTO order_handoff (order_id, message_id, point_id, phone, created_at) VALUES (?, ?, ?, ?, ?)",
                     (order_id, "m%d" % n, beta, "+79000000000%d" % n, now))
    sales.add_sale(conn, day=now[:10], goods_id="1", brand_key="nokian", season="winter", diameter=16, name="Nokian", ad_kind="single", pieces=4,
                   sale_total=20000, supplier_price=4000, terms=None, fee_pct=0, promo_pct=0, account_id=None, point="Гамма",
                   note="Заказ №%d" % issued, now=now, author="o", today=now[:10], source="order", point_id=gamma)
    conn.commit()
    for view in ("old", "new"):
        if view == "new":
            flag(conn, "all")
        page = manager(app, conn, beta, "beta_" + view).get("/my-point/handed").get_data(as_text=True)
        assert 'data-bonus="%d">Бонус: <b>+50' % issued in page
        assert 'data-bonus-wait="%d">Бонус после выдачи: +50' % waits in page and 'data-bonus="%d"' % waits not in page
