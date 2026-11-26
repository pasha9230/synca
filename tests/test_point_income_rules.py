"""The rules of the owner 2026-11-25 for what a point is paid: the bonuses and the KPI from 2026-12-01; the fee of the issue for the tyres the
point physically got (point_arrival: a cancel after the arrival and a return pay, an undone «На точке» and «шины не приезжали» do not); the
office's operations on every item (a sum set by hand, «Отменить начисление», «Вернуть в долг», whole points); one count everywhere — the
report, the analytics, the ledger, the point's page and the row of the Google table."""
from __future__ import annotations

import json
import re
import sqlite3

from panel_helpers import post
from test_mypoint import manager
from test_point_income import handed, panel, point, switch
from test_order_reserve import db as order_db, stage

from tires_sync import analytics, order_report, orders, point_arrival, point_payouts, sales

NOW = "2026-12-15T12:00:00"
DAY = "2026-12-10T10:00:00"
START, END = "2026-12-01", "2026-12-31"


def card(conn, pid, qty=4, phone="+7 900 000 00 01"):
    cid = orders.create_card(conn, dict(client_name="Иван", client_phone=phone, point_id=pid, delivery_date="2026-12-10"), DAY, "o", many_open=True)
    orders.add_item(conn, cid, goods_id="1", name="Nokian 205/55 R16", brand_key="nokian", season="winter", diameter=16, territory="1",
                    qty=qty, sale_unit=5000, supplier_price=4000, terms=None, fee_pct=0, promo_pct=0, now=DAY, author="o")
    return cid


def go(conn, cid, *codes, when=DAY):
    for code in codes:
        changes = {"cancel_reason": "отменил клиент"} if code == "cancel" else None
        assert orders.move_card(conn, cid, stage(conn, code), when, "o", changes, oth_split=False), code


def issue_of(conn, pid, start=START, end=END):
    return {e["order"]: (e["issue"], e.get("arrived", False)) for e in order_report.point_income(conn, start, end, pid)["orders"]}


# ---- 1. the dates ---------------------------------------------------------------------------------------------------------------------

def test_no_bonus_nor_kpi_before_the_first_of_december_whatever_the_rate(tmp_path):
    from test_point_kpi import clients, db
    conn = db(tmp_path)
    a = point(conn, "Альфа")
    order_report.set_rates(conn, "2026-11-25", "2026-11-25", NOW, "owner", oth_sale_fee=80, kpi_bonus=999)
    clients(conn, a, 10, pieces=4, day="2026-11-28")                       # a full cycle in November: no premium
    rep = order_report.build(conn, "2026-11-01", "2026-11-30")
    assert rep["handoff"]["fee"] == 0 and rep["kpi"]["fee"] == 0 and rep["fees"]["bonus"] == 0
    assert rep["fees"]["issue"] == 10 * 4 * 75                             # the issue: 75 ₽ a tyre from 2026-11-27
    clients(conn, a, 10, first=100, pieces=4, day="2026-12-05")
    dec = order_report.build(conn, START, END)
    assert dec["kpi"]["fee"] == 999 and dec["handoff"]["fee"] == 10 * 4 * 50


# ---- 4. the fee for the tyres the point got --------------------------------------------------------------------------------------------

def test_the_fee_follows_the_tyres_the_point_got(tmp_path):
    conn = order_db(tmp_path)
    a = point(conn, "Альфа")
    cancelled_after = card(conn, a, phone="+7 900 000 00 01")
    go(conn, cancelled_after, "placed", "moving", "at_point", "cancel")
    never = card(conn, a, phone="+7 900 000 00 02")
    go(conn, never, "placed", "moving", "cancel")
    undone = card(conn, a, phone="+7 900 000 00 03")
    go(conn, undone, "placed", "moving", "at_point", "moving", "cancel")
    returned = card(conn, a, qty=2, phone="+7 900 000 00 04")
    go(conn, returned, "placed", "moving", "at_point", "oth")
    waiting = card(conn, a, phone="+7 900 000 00 05")
    go(conn, waiting, "placed", "moving", "at_point")
    got = issue_of(conn, a)
    assert got == {cancelled_after: (300, True), returned: (150, True)}    # waiting: still in work; never / undone: nothing
    assert any(point_arrival.UNDONE in e["text"] for e in orders.list_events(conn, undone))
    go(conn, waiting, "cancel")
    assert issue_of(conn, a)[waiting] == (300, True)


def test_the_office_says_the_tyres_never_came(tmp_path):
    conn = order_db(tmp_path)
    a = point(conn, "Альфа")
    cid = card(conn, a)
    go(conn, cid, "placed", "moving", "at_point", "cancel")
    assert point_arrival.mark_none(conn, cid, NOW, "owner")
    assert cid not in issue_of(conn, a)
    assert any(point_arrival.NONE in e["text"] for e in orders.list_events(conn, cid))
    assert conn.execute("SELECT COUNT(*) FROM audit WHERE action = 'order_no_arrival'").fetchone()[0] == 1
    assert point_arrival.unmark_none(conn, cid, NOW, "owner") and issue_of(conn, a)[cid] == (300, True)


def test_a_sale_counts_once_and_the_old_days_are_never_recounted(tmp_path):
    conn = order_db(tmp_path)
    a = point(conn, "Альфа")
    sold = card(conn, a)
    go(conn, sold, "placed", "moving", "at_point", "done")
    orders.record_sales(conn, sold, "2026-12-11", NOW, "owner", "2026-12-11")
    old = card(conn, a, phone="+7 900 000 00 09")
    go(conn, old, "placed", "moving", "at_point", "cancel", when="2026-11-26T10:00:00")     # before ARRIVAL_FROM: as before, 0
    got = issue_of(conn, a, "2026-11-01", END)
    assert got == {sold: (300, False)}


def test_the_cancel_window_asks_and_both_ways_write_the_journal(tmp_path):
    app, owner, tok, conn, a, b = panel(tmp_path)
    from tires_sync import orders as o
    o.ensure_pipeline(conn, DAY)
    cid = card(conn, a)
    go(conn, cid, "placed", "moving", "at_point")
    conn.commit()
    page = owner.get("/orders/%d" % cid).get_data(as_text=True)
    assert "data-no-arrival" in page and 'name="no_arrival" value="no" checked' in page
    post(owner, "/orders/%d/move" % cid, tok, stage_id=str(stage(conn, "cancel")), cancel_kind="отменил клиент", no_arrival="yes")
    assert orders.get_card(conn, cid)["stage"]["code"] == "cancel"
    assert not point_arrival.arrived(conn, cid) and any(point_arrival.NONE in e["text"] for e in orders.list_events(conn, cid))
    other = card(conn, a, phone="+7 900 000 00 07")
    go(conn, other, "placed", "moving", "at_point")
    conn.commit()
    post(owner, "/orders/%d/move" % other, tok, stage_id=str(stage(conn, "cancel")), cancel_kind="отменил клиент", no_arrival="no",
         oth_answer="no")
    assert point_arrival.arrived(conn, other)                                # «нет» (the default): the fee stays


# ---- the review 2026-11-26 -------------------------------------------------------------------------------------------------------------

def _take_from(conn, b, a, qty):
    """The position of order `b` is `qty` tyres taken from «На ОТХ» of order `a` (as oth_sale writes it)."""
    item = conn.execute("SELECT id FROM order_item WHERE order_id = ?", (b,)).fetchone()[0]
    src = conn.execute("SELECT id FROM order_item WHERE order_id = ?", (a,)).fetchone()[0]
    conn.execute("INSERT INTO order_item_oth (item_id, order_id, from_item, from_order, qty, at, author) VALUES (?,?,?,?,?,?,'o')",
                 (item, b, src, a, qty, DAY))
    conn.commit()


def test_the_tyres_from_oth_are_paid_once(tmp_path):
    conn = order_db(tmp_path)
    a = point(conn, "Альфа")
    first = card(conn, a, phone="+7 900 000 00 11")
    go(conn, first, "placed", "moving", "at_point", "cancel")                  # came, the client refused: the issue is paid (300)
    taken = card(conn, a, phone="+7 900 000 00 12")
    _take_from(conn, taken, first, 4)
    go(conn, taken, "placed", "moving", "at_point", "oth")                     # the same tyres from «На ОТХ», refused again: 0
    assert issue_of(conn, a) == {first: (300, True)}
    conn.execute("UPDATE order_item_oth SET undone_at = ? WHERE order_id = ?", (DAY, taken))   # given back to «На ОТХ»
    conn.commit()
    sold = card(conn, a, phone="+7 900 000 00 13")
    _take_from(conn, sold, first, 4)
    go(conn, sold, "placed", "moving", "at_point", "done")
    orders.record_sales(conn, sold, "2026-12-12", NOW, "owner", "2026-12-12")
    conn.execute("INSERT INTO order_event (order_id, at, author, kind, text) VALUES (?,?,?,?,?)", (sold, NOW, "o", "point", order_report.OTH_SOLD_EVENT))
    conn.commit()
    got = {e["order"]: e for e in order_report.point_income(conn, START, END, a)["orders"]}
    assert (got[sold]["issue"], got[sold]["oth"]) == (0, 200)                 # no second fee of the issue, the bonus of «На ОТХ» only
    assert got[first]["issue"] == 300


def test_the_arrival_keeps_its_point_and_tyres_and_the_day_it_left(tmp_path):
    conn = order_db(tmp_path)
    a, b = point(conn, "Альфа"), point(conn, "Бета")
    cid = card(conn, a)
    go(conn, cid, "placed", "moving", "at_point")
    conn.execute("UPDATE order_card SET point_id = ? WHERE id = ?", (b, cid))
    conn.execute("UPDATE order_item SET qty = 1 WHERE order_id = ?", (cid,))
    conn.commit()
    go(conn, cid, "cancel", when="2026-12-20T10:00:00")
    assert issue_of(conn, b) == {}
    [entry] = order_report.point_income(conn, START, END, a)["orders"]
    assert (entry["order"], entry["issue"], entry["issue_pieces"], entry["day"]) == (cid, 300, 4, "2026-12-20")
    assert not order_report.point_income(conn, START, "2026-12-19", a)["orders"]          # the day it left, not the day it came


def test_old_rows_of_the_table_keep_their_sums(tmp_path):
    from test_order_sheet import placed, stage as sheet_stage
    from tires_sync import order_sheet as sheet
    conn = order_db(tmp_path)
    pid = point(conn, "Альфа")
    mine = placed(conn, goods=(("111", 4),))
    conn.execute("UPDATE order_card SET point_id = ? WHERE id = ?", (pid, mine))
    handed(conn, mine, pid)
    orders.move_card(conn, mine, sheet_stage(conn, "done")["id"], NOW, "owner")
    orders.record_sales(conn, mine, "2026-11-28", NOW, "owner", "2026-11-28")
    rows = sheet.rows_of(conn, mine, NOW)
    report = order_report.build(conn, "2026-11-28", "2026-11-28")
    assert report["handoff"]["fee"] == 0                                                   # the report: no bonus before 01.12
    assert round(sum(r["Прибыль ₽"] for r in rows.values()), 2) == report["total"]["net"]   # a row going there the first time: the new rules
    conn.execute("INSERT OR IGNORE INTO sheet_order (order_id, placed_at) VALUES (?, ?)", (mine, NOW))
    conn.execute("UPDATE sheet_order SET rows = ? WHERE order_id = ?", (json.dumps({m: dict(r, **{"Прибыль ₽": 1.0}) for m, r in rows.items()}), mine))
    conn.commit()                                                                          # the row was sent before with its profit
    rows = list(sheet.rows_of(conn, mine, NOW).values())
    assert round(sum(r["Прибыль ₽"] for r in rows), 2) == round(report["total"]["net"] - 200, 2)   # it keeps the old sums (50 ₽ a tyre)
    point_payouts.fix_items(conn, [(pid, "order:%d" % mine)], "issue", 0, "ошибка", NOW, "owner", "2026-11-01", "2026-11-30")
    rows = list(sheet.rows_of(conn, mine, NOW).values())
    assert round(sum(r["Прибыль ₽"] for r in rows), 2) == order_report.build(conn, "2026-11-28", "2026-11-28")["total"]["net"]


def test_only_the_money_right_says_the_tyres_never_came_and_takes_it_back(tmp_path):
    from tires_sync import store, users
    app, owner, tok, conn, a, b = panel(tmp_path)
    orders.ensure_pipeline(conn, DAY)
    cid = card(conn, a)
    go(conn, cid, "placed", "moving", "at_point")
    users.add_user(conn, "chats1", "Secret-pass-123", NOW, role="chats")
    conn.commit()
    other = app.test_client()
    with other.session_transaction() as session:
        session["user"], session["epoch"], session["csrf"] = "chats1", store.get_session_epoch(conn, "chats1"), "tok"
    page = other.get("/orders/%d" % cid).get_data(as_text=True)
    assert "data-no-arrival disabled" in page and "data-no-arrival-locked" in page
    post(other, "/orders/%d/move" % cid, "tok", stage_id=str(stage(conn, "cancel")), cancel_kind="отменил клиент", no_arrival="yes", oth_answer="no")
    assert orders.get_card(conn, cid)["stage"]["code"] == "cancel" and point_arrival.arrived(conn, cid)     # «да» without the right: not taken
    point_arrival.mark_none(conn, cid, NOW, "owner")
    conn.commit()
    assert "data-arrival-back" in owner.get("/orders/%d" % cid).get_data(as_text=True)
    assert "data-arrival-back" not in other.get("/orders/%d" % cid).get_data(as_text=True)
    post(other, "/orders/%d/arrival-back" % cid, "tok")
    assert not point_arrival.arrived(conn, cid)
    post(owner, "/orders/%d/arrival-back" % cid, tok)
    assert point_arrival.arrived(conn, cid)


def test_a_sum_is_set_only_for_an_article_the_item_has(tmp_path):
    from test_point_income import db
    conn = db(tmp_path)
    a, b = base(conn)
    for part, amount in (("handoff", 10), ("kpi", 10), ("issue", -5)):                    # order 1: the issue only; no minus there
        try:
            point_payouts.fix_items(conn, [(a, "order:1")], part, amount, "x", NOW, "owner")
            raise AssertionError(part)
        except point_payouts.PayoutError:
            pass
    try:
        point_payouts.fix_items(conn, [(a, "order:2")], "issue", 10, "x", NOW, "owner")    # Альфа gets the bonus of order 2, Бета the issue
        raise AssertionError("issue of another point")
    except point_payouts.PayoutError:
        pass
    assert point_payouts.fix_items(conn, [(a, "order:2")], "handoff", 100, "x", NOW, "owner") == 1
    adj = point_payouts.adjust(conn, a, 400, "разовый бонус", "2026-12-05", NOW, "owner")
    assert point_payouts.fix_items(conn, [(a, "adj:%d" % adj)], "adj", -50, "удержание", NOW, "owner") == 1


def test_a_merged_order_pays_the_issue_once(tmp_path):
    from tires_sync import order_merge
    conn = order_db(tmp_path)
    a = point(conn, "Альфа")
    one, two = card(conn, a, qty=2, phone="+7 900 000 00 21"), card(conn, a, qty=2, phone="+7 900 000 00 21")
    go(conn, one, "placed", "moving", "at_point")
    go(conn, two, "placed", "moving", "at_point")
    new = order_merge.merge(conn, [one, two], stage(conn, "at_point"), NOW, "o", client_from=one)
    assert point_arrival._row(conn, new)["pieces"] == 4                       # the tyres moved in, counted after the move
    assert issue_of(conn, a) == {}                                           # the sources cancelled by the merge: nothing
    go(conn, new, "done")
    orders.record_sales(conn, new, "2026-12-15", NOW, "owner", "2026-12-15")
    assert issue_of(conn, a) == {new: (300, False)}                           # the issue once, by the sale


def test_a_part_sold_leaves_the_rest_unpaid_for_the_next_order(tmp_path):
    conn = order_db(tmp_path)
    a = point(conn, "Альфа")
    first = card(conn, a, phone="+7 900 000 00 31")
    go(conn, first, "placed", "moving", "at_point")
    sales.add_sale(conn, day="2026-12-11", goods_id="1", brand_key="nokian", season="winter", diameter=16, name="Nokian", ad_kind="single",
                   pieces=2, sale_total=10000, supplier_price=4000, terms=None, fee_pct=0, promo_pct=0, account_id=None, point="", point_id=a,
                   note="Заказ №%d" % first, now=NOW, author="o", today="2026-12-11", source="order")        # 2 of 4 sold
    go(conn, first, "oth")                                                    # the other 2 on «На ОТХ»
    later = card(conn, a, qty=2, phone="+7 900 000 00 32")
    _take_from(conn, later, first, 2)
    assert point_arrival.paid_oth_pieces(conn, [later]) == {}                  # not paid by the first order: the fee is the later one's
    got = issue_of(conn, a)
    assert got[first] == (150, False)                                         # the first: the 2 tyres it sold


def test_the_first_version_of_the_arrival_table_gets_its_columns(tmp_path):
    from tires_sync import store
    path = str(tmp_path / "old.db")
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE order_arrival (order_id INTEGER PRIMARY KEY, arrived_at TEXT NOT NULL DEFAULT '', author TEXT NOT NULL DEFAULT '', "
                "undone_at TEXT, undone_by TEXT, none_at TEXT, none_by TEXT)")
    raw.execute("INSERT INTO order_arrival (order_id, arrived_at) VALUES (1, '2026-11-28T10:00:00')")
    raw.commit()
    raw.close()
    conn = store.connect(path)
    columns = {r[1] for r in conn.execute("PRAGMA table_info(order_arrival)")}
    assert {"point_id", "pieces", "left_at"} <= columns
    assert conn.execute("SELECT pieces, point_id, left_at FROM order_arrival WHERE order_id = 1").fetchone() == (0, None, None)


# ---- 2. the office's operations on every item ---------------------------------------------------------------------------------------

def base(conn):
    from test_point_income import base as income_base
    return income_base(conn)


def test_a_sum_set_by_hand_counts_everywhere_and_comes_back(tmp_path):
    from test_point_income import db
    conn = db(tmp_path)
    a, b = base(conn)
    before = order_report.build(conn, START, END)
    point_payouts.fix_items(conn, [(a, "order:1")], "issue", 100, "шины с дефектом", NOW, "owner")
    rep = order_report.build(conn, START, END)
    fees = {f["point"]: f for f in rep["fees"]["lines"]}
    assert fees["Альфа"]["issue"] == 1050 - 300 + 100
    assert rep["total"]["net"] == round(before["total"]["net"] + 200, 2)                          # the net of the analytics follows
    assert analytics.money(conn, START, END)["point_fees"] == rep["total"]["fee"]
    [entry] = [e for e in point_payouts.items(conn, START, END, a) if e["item"] == "order:1"]
    assert entry["total"] == 100 and entry["fixed"]["issue"]["reason"] == "шины с дефектом" and entry["counted"]["issue"] == 300
    assert order_report.point_income(conn, START, END, a)["total"] == point_payouts.balance(conn, a, "2026-12-31")["total"]
    point_payouts.fix_items(conn, [(a, "order:3"), (a, "order:4")], "all", 0, "ошибка", NOW, "owner")          # «Отменить начисление», mass
    assert {e["item"]: e["total"] for e in point_payouts.items(conn, START, END, a)}["order:3"] == 0
    point_payouts.unfix_items(conn, [(a, "order:1"), (a, "order:3"), (a, "order:4")], NOW, "owner")
    assert order_report.build(conn, START, END)["total"] == before["total"]
    assert conn.execute("SELECT COUNT(*) FROM audit WHERE action = 'point_item_fix'").fetchone()[0] == 3


def test_a_paid_item_changed_owes_the_difference_and_goes_back_to_the_debt(tmp_path):
    from test_point_income import db
    conn = db(tmp_path)
    a, b = base(conn)
    point_payouts.settle(conn, [(a, "order:1"), (a, "order:3")], START, END, NOW, "owner")
    point_payouts.fix_items(conn, [(a, "order:1")], "issue", 200, "уценка", NOW, "owner")
    found = {e["item"]: e for e in point_payouts.items(conn, START, END, a)}
    assert (found["order:1"]["status"], found["order:1"]["due"]) == ("over", -100)
    assert point_payouts.unsettle_many(conn, [(a, "order:1"), (a, "order:3"), (a, "order:5")], NOW, "owner") == {"count": 2, "amount": 550}
    assert {e["item"]: e["status"] for e in point_payouts.items(conn, START, END, a)}["order:3"] == "unpaid"


def test_a_kpi_premium_and_a_correction_can_be_set_and_cancelled(tmp_path):
    from test_point_kpi import clients, db
    conn = db(tmp_path)
    a = point(conn, "Альфа")
    clients(conn, a, 10, pieces=4, day="2026-12-05")
    adj = point_payouts.adjust(conn, a, 400, "разовый бонус за 23.10–25.11", "2026-12-05", NOW, "owner")
    rep = order_report.build(conn, START, END)
    assert rep["kpi"]["fee"] == 1500 and rep["fees"]["adj"] == 400
    point_payouts.fix_items(conn, [(a, "kpi:2026-12:1"), (a, "adj:%d" % adj)], "all", 0, "не положено", NOW, "owner")
    rep = order_report.build(conn, START, END)
    assert rep["kpi"]["fee"] == 0 and rep["fees"]["adj"] == 0
    assert {e["item"] for e in point_payouts.items(conn, START, END, a)} >= {"kpi:2026-12:1", "adj:%d" % adj}   # in sight, to take back


def test_the_ledger_page_operations(tmp_path):
    app, owner, tok, conn, a, b = panel(tmp_path)
    page = owner.get("/points/payouts?from=%s&to=%s&pay=all" % (START, END)).get_data(as_text=True)
    assert 'data-bulk="fix"' in page and 'data-bulk="cancel"' in page and 'data-bulk="unsettle"' in page and "разовый бонус" in page
    item = "%d|order:1" % a
    keep = {"from": START, "to": END, "pay": "all"}
    post(owner, "/points/payouts/bulk", tok, action="fix", item=item, part="issue", amount="50", reason="", **keep)
    assert not conn.execute("SELECT COUNT(*) FROM point_item_fix").fetchone()[0]                   # a reason is required
    post(owner, "/points/payouts/bulk", tok, action="fix", item=item, part="issue", amount="50", reason="скидка", **keep)
    page = owner.get("/points/payouts?from=%s&to=%s&pay=all" % (START, END)).get_data(as_text=True)
    assert 'data-fixed="issue"' in page and "скидка" in page
    post(owner, "/points/payouts/settle", tok, item=item, **keep)
    post(owner, "/points/payouts/bulk", tok, action="unsettle", item=item, **keep)
    assert not point_payouts.paid_items(conn, [a])
    post(owner, "/points/payouts/bulk", tok, action="unfix", item=item, **keep)
    assert {e["item"]: e["total"] for e in point_payouts.items(conn, START, END, a)}["order:1"] == 300
    mine = manager(app, conn, a)
    post(mine, "/points/payouts/bulk", "tok", action="cancel", item=item, reason="x", **keep)
    assert not conn.execute("SELECT COUNT(*) FROM point_item_fix WHERE undone_at IS NULL").fetchone()[0]     # a point never writes


# ---- 3. one count: the row of the Google table ---------------------------------------------------------------------------------------

def test_the_row_of_the_table_follows_a_sum_set_by_hand(tmp_path):
    from test_order_sheet import placed, stage as sheet_stage
    from tires_sync import order_sheet as sheet
    conn = order_db(tmp_path)
    pid = point(conn, "Альфа")
    mine = placed(conn, goods=(("111", 4),))
    conn.execute("UPDATE order_card SET point_id = ? WHERE id = ?", (pid, mine))
    handed(conn, mine, pid)
    orders.move_card(conn, mine, sheet_stage(conn, "done")["id"], NOW, "owner")
    orders.record_sales(conn, mine, "2026-12-10", NOW, "owner", "2026-12-10")
    point_payouts.fix_items(conn, [(pid, "order:%d" % mine)], "handoff", 10, "часть бонуса", NOW, "owner")
    rows = list(sheet.rows_of(conn, mine, NOW).values())
    report = order_report.build(conn, "2026-12-10", "2026-12-10")
    assert report["handoff"]["fee"] == 10
    assert round(sum(r["Прибыль ₽"] for r in rows), 2) == report["total"]["net"]


# ---- 5, 6. the point's pages -------------------------------------------------------------------------------------------------------

def test_the_income_page_folds_the_filters_and_tells_the_rule(tmp_path):
    app, owner, tok, conn, a, b = panel(tmp_path)
    switch(owner, tok, True)
    mine = manager(app, conn, a)
    page = mine.get("/my-point/income").get_data(as_text=True).replace("\xa0", " ")
    assert "data-income-filters" in page and "data-filters-toggle" in page and "data-income-more open" not in page
    assert "data-income-about" in page and "показать полностью" in page and "физически получила" in page
    assert "отменённые заказы не входят" not in page and "75 ₽ за каждую шину" in page
    opened = mine.get("/my-point/income?pay=all").get_data(as_text=True)
    assert re.search(r"data-income-more\s*open|open data-income-more|<details class=\"inc-f-more\" open", opened)
    assert "data-income-filter-on" in opened
    bonus = mine.get("/my-point/income?tab=bonus").get_data(as_text=True)
    assert "data-income-filters" in bonus and "data-income-about" not in bonus


def test_the_handed_page_has_its_banner_and_the_list_below(tmp_path):
    app, owner, tok, conn, a, b = panel(tmp_path)
    mine = manager(app, conn, a)
    page = mine.get("/my-point/handed").get_data(as_text=True).replace("\xa0", " ")
    assert "data-handed-banner" in page and "«Чаты»" in page and "«Звонки»" in page and "+200 ₽ за комплект" in page
    assert "1 заказ к выполнению KPI" in page
    assert page.index("mp-handoff-h") < page.index("mp-handed-h")                                # the form, then the list
    assert "закупк" not in page.lower()
