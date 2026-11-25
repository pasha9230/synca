"""«Доход» of a point (ТЗ 80): the fee of an issued order, the bonus of a client the point handed over, the bonus of the tyres it sold from
«На ОТХ» — one count (order_report.build) for the point's page, the analytics and the report; the owner's switch hides the page from the points."""
from __future__ import annotations

import re
from datetime import datetime

import pytest

from panel_helpers import login, make_app, post
from test_mypoint import manager

from tires_sync import analytics, order_report, orders, sales, store, users
from tires_sync import order_points as points

OTH_SPLIT = True                     # as on the server: OTH is split into records of storage (HANDOFF §7)
NOW = "2026-12-15T12:00:00"
TODAY = "2026-12-15"
START, END = "2026-12-01", "2026-12-31"


def db(tmp_path):
    return store.connect(str(tmp_path / "x.db"))


def point(conn, name):
    return points.save_point(conn, {"name": name, "channel": ""})


def sell(conn, order_id, point_id, pieces=4, day="2026-12-10"):
    return sales.add_sale(conn, day=day, goods_id="g%d" % order_id, brand_key="nokian", season="winter", diameter=16, name="Nokian", ad_kind="single",
                          pieces=pieces, sale_total=5000.0 * pieces, supplier_price=4000, terms=None, fee_pct=0, promo_pct=0, account_id=None,
                          point="", point_id=point_id, note="Заказ №%d" % order_id, now=NOW, author="o", today=TODAY, source="order")


def event(conn, order_id, text, kind="point"):
    conn.execute("INSERT INTO order_event (order_id, at, author, kind, text) VALUES (?,?,?,?,?)", (order_id, NOW, "o", kind, text))
    conn.commit()


def handed(conn, order_id, point_id):
    conn.execute("INSERT INTO order_handoff (order_id, message_id, point_id, created_at) VALUES (?,?,?,?)", (order_id, "m%d" % order_id, point_id, NOW))
    conn.commit()


def base(conn):
    """Альфа: order 1 plain, order 3 sold from OTH by the window (2 tyres), order 4 sold the old way (4), order 5 the office's «Выполнено» of a
    record (no bonus), order 6 sold from OTH and reversed (nothing); Бета: order 2 of a client Альфа handed over."""
    a, b = point(conn, "Альфа"), point(conn, "Бета")
    sell(conn, 1, a)
    sell(conn, 2, b)
    handed(conn, 2, a)
    sell(conn, 3, a, pieces=2)
    event(conn, 3, order_report.OTH_SOLD_EVENT + ": 2 шт. (запись хранения №9)")
    sell(conn, 4, a)
    event(conn, 4, order_report.OTH_SOLD_EVENT)
    event(conn, 4, order_report.OTH_SOLD_EVENT)                  # pressed twice in the journal: one bonus
    sell(conn, 5, a)
    event(conn, 5, "Продано с ОТХ: запись хранения переведена в «Выполнено» в панели", kind="field")
    sale = sell(conn, 6, a)
    event(conn, 6, order_report.OTH_SOLD_EVENT)
    sales.reverse_sale(conn, sale, NOW, "o", from_order=True)    # cancelled: not counted at all
    return a, b


# ---- the count -------------------------------------------------------------------------------------------------------------------------

def test_the_fees_75_50_50_each_to_its_point(tmp_path):
    conn = db(tmp_path)
    a, b = base(conn)
    mine = order_report.point_income(conn, START, END, a)
    by = {e["order"]: e for e in mine["orders"]}
    assert sorted(by) == [1, 2, 3, 4, 5]
    assert (by[1]["issue"], by[1]["handoff"], by[1]["oth"]) == (75, 0, 0)
    assert (by[2]["issue"], by[2]["handoff"], by[2]["handoff_pieces"], by[2]["total"]) == (0, 200, 4, 200)   # issued by Бета: Альфа gets the bonus only
    # (BONUS_TO), 50 ₽ a tyre from HANDOFF_PER_TYRE_FROM (ТЗ 92)
    assert (by[3]["issue"], by[3]["oth"], by[3]["oth_pieces"], by[3]["total"]) == (75, 100, 2, 175)
    assert (by[4]["oth"], by[4]["oth_pieces"]) == (200, 4)                                # once, however many lines of the journal
    assert by[5]["oth"] == 0                                                              # the office's «Выполнено»: no sale of the point
    assert (mine["issue"], mine["handoff"], mine["oth"], mine["bonus"], mine["total"]) == (300, 200, 300, 500, 800)
    theirs = order_report.point_income(conn, START, END, b)
    assert [(e["order"], e["issue"], e["handoff"], e["oth"]) for e in theirs["orders"]] == [(2, 75, 0, 0)]
    assert theirs["total"] == 75


def test_the_report_the_analytics_and_the_page_are_one_count(tmp_path):
    conn = db(tmp_path)
    a, b = base(conn)
    rep = order_report.build(conn, START, END)
    fee = {r["point"]: r["fee"] for r in rep["lines"]}
    assert fee == {"Альфа": 800, "Бета": 75}
    assert {f["point"]: (f["issue"], f["bonus"]) for f in rep["fees"]["lines"]} == {"Альфа": (300, 500), "Бета": (75, 0)}
    assert (rep["fees"]["issue"], rep["fees"]["bonus"]) == (375, 500)
    assert (rep["oth"]["orders"], rep["oth"]["pieces"], rep["oth"]["fee"]) == (2, 6, 300)
    money = analytics.money(conn, START, END)
    assert (money["point_fees"], money["issue_fees"], money["bonus_fees"], money["handoff_fees"], money["oth_fees"]) == (875, 375, 500, 200, 300)
    assert {r["point"]: (r["issue_fee"], r["bonus_fee"], r["point_fee"]) for r in money["by_point"]} == {"Альфа": (300, 500, 800), "Бета": (75, 0, 75)}
    assert order_report.point_income(conn, START, END, a)["total"] + order_report.point_income(conn, START, END, b)["total"] == money["point_fees"]


def test_the_bonus_of_oth_follows_the_dated_rate_and_is_zero_before_its_start(tmp_path):
    conn = db(tmp_path)
    a = point(conn, "Альфа")
    assert order_report.rate_on([], "oth_sale_fee", "2026-11-24") == 0 and order_report.rate_on([], "oth_sale_fee", order_report.OTH_SALE_FEE_FROM) == 50
    for order_id, day in ((1, "2026-11-20"), (2, "2026-12-05"), (3, "2026-12-12")):
        sell(conn, order_id, a, pieces=1, day=day)
        event(conn, order_id, order_report.OTH_SOLD_EVENT)
    assert order_report.set_rates(conn, "2026-12-10", "2026-12-01", NOW, "owner", oth_sale_fee=80) == ["oth_sale_fee"]
    with pytest.raises(ValueError):
        order_report.set_rates(conn, "2026-12-10", "2026-12-01", NOW, "owner", oth_sale_fee=-1)
    got = {e["order"]: e["oth"] for e in order_report.point_income(conn, "2026-11-01", END, a)["orders"]}
    assert got == {1: 0, 2: 50, 3: 80}


def test_the_month_is_its_days_only(tmp_path):
    conn = db(tmp_path)
    a = point(conn, "Альфа")
    sell(conn, 1, a, day="2026-11-30")
    sell(conn, 2, a, day="2026-12-01")
    assert [e["order"] for e in order_report.point_income(conn, START, END, a)["orders"]] == [2]
    assert [e["order"] for e in order_report.point_income(conn, "2026-11-01", "2026-11-30", a)["orders"]] == [1]


def test_the_count_does_not_ask_the_base_once_an_order(tmp_path):
    conn = db(tmp_path)
    a = point(conn, "Альфа")

    def queries(n, first):
        for order_id in range(first, first + n):
            sell(conn, order_id, a)
            event(conn, order_id, order_report.OTH_SOLD_EVENT)
        seen = []
        conn.set_trace_callback(seen.append)
        order_report.point_income(conn, START, END, a)
        conn.set_trace_callback(None)
        return len([q for q in seen if q.lstrip().upper().startswith("SELECT")])
    few = queries(2, 1)
    assert queries(10, 100) == few


def test_a_real_sale_of_the_window_gives_the_bonus_once(tmp_path, monkeypatch):
    from test_oth_point_sale import sale_rule, sell as window_sell
    from test_order_reserve import db as order_db
    from test_oth_storage import split
    monkeypatch.setitem(order_report.STARTS, "oth_sale_fee", "2026-01-01")
    conn = order_db(tmp_path)
    sale_rule(conn)
    cid, x = split(conn)
    b = window_sell(conn, x, qty=3)
    point_id = orders.get_card(conn, b)["point_id"]
    [entry] = [e for e in order_report.point_income(conn, "2026-10-01", "2026-10-31", point_id)["orders"] if e["order"] == b]
    assert (entry["issue"], entry["oth"], entry["oth_pieces"]) == (75, 150, 3)
    assert not [e for e in order_report.point_income(conn, "2026-10-01", "2026-10-31", point_id)["orders"] if e["order"] in (cid, x)]


# ---- the pages -------------------------------------------------------------------------------------------------------------------------

def panel(tmp_path):
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 12, 15, 9, 0, 0))
    owner = app.test_client()
    tok = login(owner)
    conn = store.connect(app.config["DB_PATH"])
    from test_oth_sale import T
    orders.ensure_pipeline(conn, T)
    a, b = base(conn)
    return app, owner, tok, conn, a, b


def switch(owner, tok, on):
    return post(owner, "/orders/report/rates", tok, what="point_income", **({"on": "1"} if on else {}))


def test_the_switch_is_off_by_default_the_point_has_no_page_nor_menu(tmp_path):
    app, owner, tok, conn, a, b = panel(tmp_path)
    assert not order_report.income_shown(conn)
    mine = manager(app, conn, a)
    assert mine.get("/my-point/income").status_code == 404
    for url in ("/my-point", "/my-point/handed"):
        page = mine.get(url).get_data(as_text=True)
        assert 'href="/my-point/income"' not in page and "Ваш бонус" not in page
    office = owner.get("/my-point/income?p=%d" % a)                                   # the owner checks the sums before switching on
    assert office.status_code == 200 and "800" in office.get_data(as_text=True)
    money = owner.get("/orders/settings/money").get_data(as_text=True)
    assert "Показывать точкам страницу дохода" in money and "скрыта от точек" in money


def test_only_the_owner_switches(tmp_path):
    app, owner, tok, conn, a, b = panel(tmp_path)
    users.add_user(conn, "partner1", "Secret-pass-123", NOW, role="partner")
    conn.commit()
    partner = app.test_client()
    with partner.session_transaction() as session:
        session["user"], session["epoch"], session["csrf"] = "partner1", store.get_session_epoch(conn, "partner1"), "tok"
    assert switch(partner, "tok", True).status_code == 403 and not order_report.income_shown(conn)
    assert switch(owner, tok, True).status_code == 302 and order_report.income_shown(conn)
    assert conn.execute("SELECT COUNT(*) FROM audit WHERE action = 'point_income_shown'").fetchone()[0] == 1
    assert switch(owner, tok, False).status_code == 302 and not order_report.income_shown(conn)


@pytest.mark.parametrize("view", ["old", "new"])
def test_switched_on_the_point_sees_its_own_sums_only(tmp_path, view):
    app, owner, tok, conn, a, b = panel(tmp_path)
    if view == "new":
        from test_point_new_view import flag
        flag(conn, "all")
    switch(owner, tok, True)
    mine = manager(app, conn, a)
    page = mine.get("/my-point/income").get_data(as_text=True)
    assert 'href="/my-point/income"' in page
    total = re.search(r'data-income="total">([^<]+)<', page).group(1)
    assert total.replace("\xa0", " ").startswith("800")
    assert 'data-income="issue">300' in page.replace("\xa0", " ") and 'data-income="bonus">500' in page.replace("\xa0", " ")
    rows = re.findall(r'<tr data-order="(\d+)">', page)
    assert sorted(map(int, rows)) == [1, 2, 3, 4, 5]
    for secret in ("Закупка", "Прибыль", "20 000", "4 000"):                       # no purchase, no profit, no prices
        assert secret not in page, secret
    other = mine.get("/my-point/income?p=%d" % b).get_data(as_text=True)          # ?p= is never the point's choice
    assert re.findall(r'<tr data-order="(\d+)">', other) == rows
    theirs = manager(app, conn, b, login_="delta").get("/my-point/income").get_data(as_text=True)
    assert re.findall(r'<tr data-order="(\d+)">', theirs) == ["2"] and "800" not in theirs
    november = mine.get("/my-point/income?m=2026-11").get_data(as_text=True)
    assert "За этот месяц выплат нет." in november
    assert mine.get("/my-point/income?m=junk").status_code == 200
    assert "за каждую шину, проданную с ОТХ, +50" in page.replace("\xa0", " ")
    october = mine.get("/my-point/income?m=2026-10").get_data(as_text=True)          # before OTH_SALE_FEE_FROM: no «+0 ₽» in the hint
    assert "проданную с ОТХ" not in october


def test_the_handed_page_shows_the_bonus_of_each_client(tmp_path):
    app, owner, tok, conn, a, b = panel(tmp_path)
    card = orders.create_card(conn, {"client_name": "Тест", "client_phone": "+7 900 111 22 33", "point_id": b}, NOW, "o")
    handed(conn, card, a)
    sell(conn, card, b, day="2026-12-14")
    mine = manager(app, conn, a)
    assert "Ваш бонус" not in mine.get("/my-point/handed").get_data(as_text=True)
    switch(owner, tok, True)
    page = mine.get("/my-point/handed").get_data(as_text=True).replace("\xa0", " ")
    assert 'data-handoff-bonus="%d">Ваш бонус: +200 ₽' % card in page


def test_the_analytics_shows_the_fees_and_the_bonuses(tmp_path):
    app, owner, tok, conn, a, b = panel(tmp_path)
    page = owner.get("/analytics?from=%s&to=%s" % (START, END)).get_data(as_text=True).replace("\xa0", " ")
    assert "Выплаты точкам" in page and "за выдачу 375 ₽, бонусы 500 ₽" in page
    assert re.search(r"<td>Альфа</td><td>4</td><td>300 ₽</td><td>500 ₽</td><td>800 ₽</td>", page)
    assert "продажи с ОТХ 300 ₽ (6 шт.)" in page
