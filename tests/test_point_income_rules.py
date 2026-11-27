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
    # the owner 2026-11-26: one fee for the end of the chain — on «На ОТХ» the tyres wait; waiting: in work; never / undone: nothing
    assert got == {cancelled_after: (300, True)}
    assert any(point_arrival.UNDONE in e["text"] for e in orders.list_events(conn, undone))
    _give_back(conn, returned, "2026-12-14T10:00:00")
    assert issue_of(conn, a)[returned] == (150, True)                     # given back to the supplier: the end, paid once
    go(conn, waiting, "cancel")
    assert issue_of(conn, a)[waiting] == (300, True)


def _before_the_table(conn, *ids):
    """The arrivals of before order_arrival (2026-11-26): only the journal of the order says them."""
    conn.execute("DELETE FROM order_arrival WHERE order_id IN (%s)" % ",".join("?" * len(ids)), ids)
    conn.commit()


def test_the_arrivals_before_the_table_are_read_from_the_journal(tmp_path, monkeypatch):
    conn = order_db(tmp_path)
    a = point(conn, "Альфа")
    nov = "2026-11-10T10:00:00"
    cancelled = card(conn, a, phone="+7 900 000 00 91")
    go(conn, cancelled, "placed", "moving", "at_point", when=nov)
    go(conn, cancelled, "cancel", when="2026-11-12T10:00:00")
    undone = card(conn, a, phone="+7 900 000 00 92")
    go(conn, undone, "placed", "moving", "at_point", "moving", "cancel", when=nov)
    returned = card(conn, a, qty=2, phone="+7 900 000 00 93")
    go(conn, returned, "placed", "moving", "at_point", "oth", when=nov)
    _give_back(conn, returned, "2026-11-20T10:00:00")
    waiting = card(conn, a, phone="+7 900 000 00 94")
    go(conn, waiting, "placed", "moving", "at_point", when=nov)
    sold = card(conn, a, phone="+7 900 000 00 95")
    go(conn, sold, "placed", "moving", "at_point", "done", when=nov)
    orders.record_sales(conn, sold, "2026-11-11", NOW, "owner", "2026-11-11")
    never = card(conn, a, phone="+7 900 000 00 96")
    go(conn, never, "placed", "moving", "cancel", when=nov)
    old = card(conn, a, phone="+7 900 000 00 97")
    go(conn, old, "placed", "moving", "at_point", "cancel", when="2026-10-20T10:00:00")     # settled with the points up to 22.10
    came_early = card(conn, a, phone="+7 900 000 00 89")                        # came before 23.10, the end after it: paid on the end
    go(conn, came_early, "placed", "moving", "at_point", when="2026-10-20T10:00:00")
    go(conn, came_early, "cancel", when="2026-10-25T10:00:00")
    still = card(conn, a, phone="+7 900 000 00 88")                             # came before 23.10, no end yet
    go(conn, still, "placed", "moving", "at_point", when="2026-10-20T10:00:00")
    _before_the_table(conn, cancelled, undone, returned, waiting, sold, never, old, came_early, still)
    got = {e["order"]: (e["day"], e["issue"], e["issue_pieces"]) for e in order_report.point_income(conn, "2026-10-01", "2026-11-30", a)["orders"]}
    assert got == {cancelled: ("2026-11-12", 300, 4), returned: ("2026-11-20", 150, 2), sold: ("2026-11-11", 300, 4),
                   came_early: ("2026-10-25", 300, 4)}                                      # old: its end before 23.10 was settled
    assert sorted(w["order"] for w in order_report.waiting(conn, "2026-11-26", a)) == sorted([waiting, still])
    first, x = _on_oth(conn, monkeypatch, a, nov)                               # cancelled into a record of storage, given back
    _give_back(conn, x, "2026-11-18T10:00:00")
    _before_the_table(conn, first)
    got = {e["order"]: (e["day"], e["issue"]) for e in order_report.point_income(conn, "2026-11-01", "2026-11-30", a)["orders"]}
    assert got[first] == ("2026-11-18", 300)


def test_an_order_made_on_the_point_before_the_table_is_read_from_its_creation(tmp_path):
    from tires_sync import order_merge
    conn = order_db(tmp_path)
    a = point(conn, "Альфа")
    nov = "2026-11-10T10:00:00"
    one, two = card(conn, a, qty=2, phone="+7 900 000 00 98"), card(conn, a, qty=2, phone="+7 900 000 00 98")
    go(conn, one, "placed", "moving", "at_point", when=nov)
    go(conn, two, "placed", "moving", "at_point", when=nov)
    new = order_merge.merge(conn, [one, two], stage(conn, "at_point"), nov, "o", client_from=one)    # «Заказ создан на этапе «На точке»»
    go(conn, new, "cancel", when="2026-11-12T10:00:00")
    late = card(conn, a, phone="+7 900 000 00 99")
    go(conn, late, "placed", "moving", "at_point", "cancel", when="2026-11-26T03:00:00")   # 26.11 before the deploy of the table
    _before_the_table(conn, one, two, new, late)
    got = {e["order"]: (e["day"], e["issue"], e["issue_pieces"]) for e in order_report.point_income(conn, "2026-11-01", "2026-11-30", a)["orders"]}
    assert got == {new: ("2026-11-12", 300, 4), late: ("2026-11-26", 300, 4)}             # the sources of the merge: nothing


def _give_back(conn, cid, when):
    """«На возврат»: the tyres go back to the supplier (the agreement of the return is the office's, not of this test)."""
    assert orders.move_card(conn, cid, stage(conn, "return_given"), when, "o", internal_return_restore=True)


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


def test_a_sale_counts_once_and_the_rate_a_tyre_holds_from_the_twenty_third_of_october(tmp_path):
    conn = order_db(tmp_path)
    a = point(conn, "Альфа")
    sold = card(conn, a)
    go(conn, sold, "placed", "moving", "at_point", "done")
    orders.record_sales(conn, sold, "2026-12-11", NOW, "owner", "2026-12-11")
    old = card(conn, a, phone="+7 900 000 00 09")
    go(conn, old, "placed", "moving", "at_point", "cancel", when="2026-11-26T10:00:00")     # the owner 2026-11-26: before 27.11 too
    early = card(conn, a, phone="+7 900 000 00 08")
    go(conn, early, "placed", "moving", "at_point", "done")
    orders.record_sales(conn, early, "2026-11-20", NOW, "owner", "2026-11-20")
    settled = card(conn, a, phone="+7 900 000 00 07")
    go(conn, settled, "placed", "moving", "at_point", "done")
    orders.record_sales(conn, settled, "2026-10-22", NOW, "owner", "2026-10-22")
    got = issue_of(conn, a, "2026-10-01", END)
    # the owner 2026-11-26: 75 ₽ a tyre from 23.10 (the points are settled up to 22.10: that day keeps the old count, once an order)
    assert got == {sold: (300, False), old: (300, True), early: (300, False), settled: (75, False)}


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
    go(conn, taken, "placed", "moving", "at_point", "oth")                     # the same tyres from «На ОТХ», refused again: they wait
    assert issue_of(conn, a) == {}
    conn.execute("UPDATE order_item_oth SET undone_at = ? WHERE order_id = ?", (DAY, taken))   # given back to «На ОТХ»
    conn.commit()
    sold = card(conn, a, phone="+7 900 000 00 13")
    _take_from(conn, sold, first, 4)
    go(conn, sold, "placed", "moving", "at_point", "done")
    orders.record_sales(conn, sold, "2026-12-12", NOW, "owner", "2026-12-12")
    conn.execute("INSERT INTO order_event (order_id, at, author, kind, text) VALUES (?,?,?,?,?)", (sold, NOW, "o", "point", order_report.OTH_SOLD_EVENT))
    conn.commit()
    got = {e["order"]: e for e in order_report.point_income(conn, START, END, a)["orders"]}
    # the owner 2026-11-26: one fee a chain — the sale ends it (its fee and the bonus of «На ОТХ»); the arrival pays nothing more
    assert (got[sold]["issue"], got[sold]["oth"]) == (300, 200)
    assert first not in got and taken not in got


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
    report = order_report.build(conn, "2026-11-28", "2026-11-28")
    assert report["handoff"]["fee"] == 0                                                   # the report: no bonus before 01.12
    for _ in range(2):                                                                     # sent twice: the same profit, the report's
        rows = sheet.rows_of(conn, mine, NOW)
        assert round(sum(r["Прибыль ₽"] for r in rows.values()), 2) == report["total"]["net"]
        conn.execute("INSERT OR IGNORE INTO sheet_order (order_id, placed_at) VALUES (?, ?)", (mine, NOW))
        conn.execute("UPDATE sheet_order SET rows = ? WHERE order_id = ?", (json.dumps(rows), mine))
        conn.commit()
    old = placed(conn, goods=(("222", 4),))                                                # sold 20.11: the old sums, for ever
    conn.execute("UPDATE order_card SET point_id = ? WHERE id = ?", (pid, old))
    handed(conn, old, pid)
    orders.move_card(conn, old, sheet_stage(conn, "done")["id"], NOW, "owner")
    orders.record_sales(conn, old, "2026-11-20", NOW, "owner", "2026-11-20")
    margin = order_report.build(conn, "2026-11-20", "2026-11-20")["total"]["margin"]
    for _ in range(2):
        rows = sheet.rows_of(conn, old, NOW)
        assert round(sum(r["Прибыль ₽"] for r in rows.values()), 2) == round(margin - 75 - 50, 2)  # 75 + 50 ₽ an order, as written
        conn.execute("INSERT OR IGNORE INTO sheet_order (order_id, placed_at) VALUES (?, ?)", (old, NOW))
        conn.execute("UPDATE sheet_order SET rows = ? WHERE order_id = ?", (json.dumps(rows), old))
        conn.commit()
    point_payouts.fix_items(conn, [(pid, "order:%d" % old)], "issue", 0, "ошибка", NOW, "owner", "2026-11-01", "2026-11-30")
    rows = list(sheet.rows_of(conn, old, NOW).values())                                    # a sum set by the office: as everywhere
    assert round(sum(r["Прибыль ₽"] for r in rows), 2) == order_report.build(conn, "2026-11-20", "2026-11-20")["total"]["net"]


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


def test_an_unmerge_gives_the_arrival_back_to_the_source(tmp_path):
    from tires_sync import order_merge
    conn = order_db(tmp_path)
    a = point(conn, "Альфа")
    one, two = card(conn, a, qty=2, phone="+7 900 000 00 41"), card(conn, a, qty=2, phone="+7 900 000 00 41")
    go(conn, one, "placed", "moving", "at_point", when="2026-12-05T10:00:00")
    go(conn, two, "placed", "moving", "at_point")
    new = order_merge.merge(conn, [one, two], stage(conn, "at_point"), NOW, "o", client_from=one)
    assert point_arrival._row(conn, new)["arrived_at"] == "2026-12-05T10:00:00"         # the earliest arrival of the sources
    order_merge.unmerge(conn, new, NOW, "o")
    conn.commit()
    assert point_arrival._row(conn, new) is None                                         # no tyres of its own left: no arrival
    assert orders.get_card(conn, one)["stage"]["code"] == "at_point" and not point_arrival._row(conn, one)["left_at"]
    assert issue_of(conn, a) == {}                                                       # back on the point: in work
    go(conn, one, "cancel", when="2026-12-20T10:00:00")
    assert issue_of(conn, a) == {one: (150, True)}                                       # paid once, the merged order nothing


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
    got = issue_of(conn, a)
    assert got[first] == (150, False)                                         # the first: the 2 tyres it sold
    go(conn, later, "placed", "moving", "at_point", "done")
    orders.record_sales(conn, later, "2026-12-12", NOW, "owner", "2026-12-12")
    got = issue_of(conn, a)
    assert (got[first], got[later]) == ((150, False), (150, False))           # the later order: the fee of its 2 tyres, once


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
    assert "data-income-about" in page and "показать полностью" in page and "Отмены по вине точки не начисляются на баланс точки." in page
    assert "причина отмены «Точка не передала статус»" in page and "не были в статусе «На точке»" in page
    assert "продажа тех же шин другому клиенту оплачивается как обычно" in page and "Заказ отменён после приезда шин (шины не на ОТХ) — тоже итог, платим." in page
    # the owner 2026-11-27: the phrases nobody understood are gone
    assert "Заказ отменили, а шины на ОТХ не оставались" not in page and "Дата в списке" not in page
    assert "отменённые заказы не входят" not in page and "300 ₽ за комплект (4 шт.)" in page and "за итог: шины выданы клиенту или ушли поставщику возвратом" in page
    assert "27.11" not in page and "мы платим" not in page and "ваша точка" not in page      # the owner 2026-11-26: one team, no date of the rate
    opened = mine.get("/my-point/income?pay=all").get_data(as_text=True)
    assert re.search(r"data-income-more\s*open|open data-income-more|<details class=\"inc-f-more\" open", opened)
    assert "data-income-filter-on" in opened
    bonus = mine.get("/my-point/income?tab=bonus").get_data(as_text=True)
    assert "data-income-filters" in bonus and "data-income-about" not in bonus


def test_a_chain_with_no_end_is_shown_to_be_counted_and_not_in_the_sums(tmp_path):
    app, owner, tok, conn, a, b = panel(tmp_path)
    switch(owner, tok, True)
    mine = manager(app, conn, a)
    total = re.compile(r'data-income="total">([^<]*)<')
    before = total.search(mine.get("/my-point/income?pay=all").get_data(as_text=True)).group(1)
    for n in range(9):                                                               # past the orders the fixture sold (№1–6)
        card(conn, b, phone="+7 900 000 01 %02d" % n)
    cid = card(conn, a)
    go(conn, cid, "placed", "moving", "at_point")
    returned = card(conn, a, qty=2, phone="+7 900 000 00 62")
    go(conn, returned, "placed", "moving", "at_point", "oth")
    conn.commit()
    assert [(w["order"], w["expected"]) for w in order_report.waiting(conn, "2026-12-15", a)] == [(cid, 300), (returned, 150)]
    page = mine.get("/my-point/income?pay=all").get_data(as_text=True)
    assert "data-income-waiting" in page and "к начислению" in page and "450" in page
    assert total.search(page).group(1) == before                                    # not in «Всего» before the end
    office = owner.get("/points/payouts?from=%s&to=%s&pay=all" % (START, END)).get_data(as_text=True)
    assert "data-payout-waiting" in office and 'data-waiting="%d|%d"' % (a, returned) in office
    _give_back(conn, returned, "2026-12-14T10:00:00")
    conn.commit()
    assert [w["order"] for w in order_report.waiting(conn, "2026-12-15", a)] == [cid]
    assert issue_of(conn, a)[returned] == (150, True) and cid not in issue_of(conn, a)     # the return: the same fee as an issue


def test_the_handed_page_has_its_banner_and_the_list_below(tmp_path):
    app, owner, tok, conn, a, b = panel(tmp_path)
    mine = manager(app, conn, a)
    page = mine.get("/my-point/handed").get_data(as_text=True).replace("\xa0", " ")
    assert "data-handed-banner" in page and "«Чаты»" in page and "«Звонки»" in page and "+200 ₽ за комплект" in page
    assert "1 заказ к выполнению KPI" in page
    assert page.index("mp-handoff-h") < page.index("mp-handed-h")                                # the form, then the list
    assert "закупк" not in page.lower()


# ---- the owner 2026-11-26: a sale from «На ОТХ» is an issue; its bonus is the point's own sale only ------------------------------------

def _on_oth(conn, monkeypatch, pid, day):
    """A client's order came to the point on `day` and was refused: the record of storage holds its 4 tyres (as production splits)."""
    from tires_sync import oth_storage
    monkeypatch.setattr(oth_storage, "SPLIT", True)
    first = card(conn, pid, phone="+7 900 000 00 51")
    go(conn, first, "placed", "moving", "at_point", when=day)
    assert orders.move_card(conn, first, stage(conn, "oth"), day, "o", {"cancel_reason": "отменил клиент"})
    return first, oth_storage.record_of(conn, first)


def _point_sells(conn, x, day, who, seller=""):
    from tires_sync import oth_point_sale
    item = orders.get_card(conn, x)["items"][0]["id"]
    b = oth_point_sale.sell(conn, x, {item: 4}, "Глеб", "+7 900 333 44 55", None, None, False, day + "T15:00:00", who, seller=seller)
    orders.record_sales(conn, b, day, NOW, "owner", day)
    return b


def _income(conn, pid, start, end):
    return {e["order"]: (e["issue"], e["oth"]) for e in order_report.point_income(conn, start, end, pid)["orders"]}


def _sheet_matches(conn, b, start, end):
    from tires_sync import order_sheet as sheet
    rows = sheet.rows_of(conn, b, NOW)
    margin = sum(r["margin_total"] for r in order_report._counted(conn, sales.list_sales(conn, start, end)) if r["note"] == "Заказ №%d" % b)
    return round(sum(r["Прибыль ₽"] for r in rows.values()), 2), round(margin, 2)


def test_the_point_sold_from_oth_after_the_first_of_december_gets_the_issue_and_the_bonus(tmp_path, monkeypatch):
    conn = order_db(tmp_path)
    a = point(conn, "Альфа")
    first, x = _on_oth(conn, monkeypatch, a, DAY)
    assert _income(conn, a, START, END) == {}                                     # on «На ОТХ»: the chain waits
    assert [(w["order"], w["pieces"], w["expected"]) for w in order_report.waiting(conn, "2026-12-11", a)] == [(first, 4, 300)]
    b = _point_sells(conn, x, "2026-12-12", "менеджер Альфа")
    assert _income(conn, a, START, END) == {b: (300, 200)}                       # one issue for the chain and the bonus
    assert order_report.waiting(conn, "2026-12-12", a) == []                     # the chain ended: nothing «к начислению»
    rep = order_report.build(conn, START, END)
    assert rep["oth"]["pieces"] == 4 and rep["oth"]["fee"] == 200 and rep["fees"]["issue"] == 300
    profit, margin = _sheet_matches(conn, b, START, END)
    assert profit == round(margin - 300 - 200, 2)                                 # the row of the table: the same sums as the report


def test_the_point_sold_from_oth_before_the_first_of_december_gets_the_issue_only(tmp_path, monkeypatch):
    conn = order_db(tmp_path)
    a = point(conn, "Альфа")
    first, x = _on_oth(conn, monkeypatch, a, "2026-11-28T10:00:00")
    b = _point_sells(conn, x, "2026-11-29", "менеджер Альфа")
    assert _income(conn, a, "2026-11-01", "2026-11-30") == {b: (300, 0)}         # the bonus of «На ОТХ» from 01.12
    profit, margin = _sheet_matches(conn, b, "2026-11-01", "2026-11-30")
    assert profit == round(margin - 300, 2)


def test_the_office_sold_from_oth_gets_the_point_the_issue_without_the_bonus(tmp_path, monkeypatch):
    conn = order_db(tmp_path)
    a = point(conn, "Альфа")
    first, x = _on_oth(conn, monkeypatch, a, DAY)
    b = _point_sells(conn, x, "2026-12-12", "Павел за точку «Альфа»", seller="owner")      # the owner working as the point
    assert any(e["kind"] == "point" and e["text"].startswith(order_report.OTH_SOLD_EVENT) for e in orders.list_events(conn, b))
    assert _income(conn, a, START, END) == {b: (300, 0)}                         # one issue, no bonus
    assert order_report.build(conn, START, END)["oth"]["fee"] == 0
    profit, margin = _sheet_matches(conn, b, START, END)
    assert profit == round(margin - 300, 2)
    other, y = _on_oth(conn, monkeypatch, a, DAY)                                 # the office moves a record to «Выполнено»
    assert orders.move_card(conn, y, stage(conn, "done"), "2026-12-13T10:00:00", "owner")
    orders.record_sales(conn, y, "2026-12-13", NOW, "owner", "2026-12-13")
    got = _income(conn, a, START, END)
    assert other not in got and got[y] == (300, 0)


def test_the_old_way_sold_from_oth_is_one_item_on_the_day_of_the_sale(tmp_path):
    """An order on «На ОТХ» itself (no record of storage: a merged order, a second number of WebMIM, no client) sold by «Продать»: nothing
    is paid while it waits (no item to settle), the sale is the one item «order:N» of the chain: the issue and the bonus."""
    from tires_sync import order_oth
    conn = order_db(tmp_path)
    a = point(conn, "Альфа")
    cid = card(conn, a)
    go(conn, cid, "placed", "moving", "at_point", "oth")                       # the old way: the order itself on «На ОТХ»
    assert _income(conn, a, START, END) == {} and not point_payouts.items(conn, START, END, a)
    outcome, answer, done = order_oth.sell(conn, orders.get_card(conn, cid), "менеджер Альфа", "2026-12-12T15:00:00")
    assert done, answer
    orders.record_sales(conn, cid, "2026-12-12", NOW, "owner", "2026-12-12")
    got = order_report.point_income(conn, START, END, a)["orders"]
    assert [(e["item"], e["day"], e["issue"], e["oth"]) for e in got] == [("order:%d" % cid, "2026-12-12", 300, 200)]
    assert order_report.build(conn, START, END)["fees"]["issue"] == 300
    profit, margin = _sheet_matches(conn, cid, START, END)
    assert profit == round(margin - 300 - 200, 2)                                 # the row of the table: the report's sums
    assert [e["status"] for e in point_payouts.items(conn, START, END, a)] == ["unpaid"]


def test_the_tyres_that_came_and_went_back_are_paid_once_on_the_day_of_the_return(tmp_path, monkeypatch):
    conn = order_db(tmp_path)
    a = point(conn, "Альфа")
    first, x = _on_oth(conn, monkeypatch, a, DAY)
    assert orders.move_card(conn, x, stage(conn, "return_agree"), "2026-12-14T10:00:00", "o")      # agreeing: the tyres still wait
    assert _income(conn, a, START, END) == {}
    _give_back(conn, x, "2026-12-16T10:00:00")                                   # «На возврат»: the end of the chain
    [entry] = order_report.point_income(conn, START, END, a)["orders"]
    assert (entry["order"], entry["day"], entry["issue"], entry["issue_pieces"]) == (first, "2026-12-16", 300, 4)
    conn.execute("UPDATE order_card SET return_kind = ?, return_reason = 'брак', return_amount = 1000 WHERE id = ?", ("supplier", x))
    assert orders.move_card(conn, x, stage(conn, "return"), "2026-12-18T10:00:00", "o")             # «Вернули поставщику»: the same day
    assert [e["day"] for e in order_report.point_income(conn, START, END, a)["orders"]] == ["2026-12-16"]


def test_a_day_written_on_oth_by_the_rule_before_gives_way_to_the_return(tmp_path):
    conn = order_db(tmp_path)
    a = point(conn, "Альфа")
    cid = card(conn, a)
    go(conn, cid, "placed", "moving", "at_point", "oth")
    conn.execute("UPDATE order_arrival SET left_at = ? WHERE order_id = ?", (DAY, cid))     # written on «На ОТХ» before the deploy
    conn.commit()
    assert _income(conn, a, START, END) == {}                                    # still on «На ОТХ»: waits whatever is written
    _give_back(conn, cid, "2026-12-17T10:00:00")
    assert [(e["order"], e["day"]) for e in order_report.point_income(conn, START, END, a)["orders"]] == [(cid, "2026-12-17")]


def test_a_part_sold_by_the_point_the_rest_given_back(tmp_path, monkeypatch):
    from tires_sync import oth_point_sale
    conn = order_db(tmp_path)
    a = point(conn, "Альфа")
    first, x = _on_oth(conn, monkeypatch, a, DAY)
    item = orders.get_card(conn, x)["items"][0]["id"]
    b = oth_point_sale.sell(conn, x, {item: 2}, "Глеб", "+7 900 333 44 55", None, None, False, "2026-12-12T15:00:00", "менеджер Альфа")
    orders.record_sales(conn, b, "2026-12-12", NOW, "owner", "2026-12-12")
    assert _income(conn, a, START, END) == {b: (150, 100)}                       # 2 sold; the other 2 wait on «На ОТХ»
    _give_back(conn, x, "2026-12-16T10:00:00")
    assert _income(conn, a, START, END) == {b: (150, 100), first: (150, 0)}      # the rest given back: the arrival pays the 2 left


def test_the_bonuses_never_add_up_on_the_tyres_from_the_points_own_oth(tmp_path, monkeypatch):
    conn = order_db(tmp_path)
    a, q = point(conn, "Альфа"), point(conn, "Бета")
    first, x = _on_oth(conn, monkeypatch, a, DAY)
    b = _point_sells(conn, x, "2026-12-12", "менеджер Альфа")
    handed(conn, b, a)                                                           # the client is Альфа's own: the bonus of «На ОТХ» only
    [entry] = order_report.point_income(conn, START, END, a)["orders"]
    assert (entry["order"], entry["issue"], entry["oth"], entry["handoff"]) == (b, 300, 200, 0)
    rep = order_report.build(conn, START, END)
    assert rep["handoff"]["fee"] == 0 and rep["oth"]["fee"] == 200
    profit, margin = _sheet_matches(conn, b, START, END)
    assert profit == round(margin - 300 - 200, 2)
    conn.execute("UPDATE order_handoff SET point_id = ? WHERE order_id = ?", (q, b))     # handed by another point: its bonus stays
    conn.commit()
    assert {e["order"]: e["handoff"] for e in order_report.point_income(conn, START, END, q)["orders"]} == {b: 200}
    assert order_report.point_income(conn, START, END, a)["orders"][0]["oth"] == 200


def test_the_handed_bonus_stays_for_a_sale_not_from_oth(tmp_path):
    conn = order_db(tmp_path)
    a = point(conn, "Альфа")
    cid = card(conn, a)
    handed(conn, cid, a)
    go(conn, cid, "placed", "moving", "at_point", "done")
    orders.record_sales(conn, cid, "2026-12-11", NOW, "owner", "2026-12-11")
    [entry] = order_report.point_income(conn, START, END, a)["orders"]
    assert (entry["issue"], entry["handoff"], entry["oth"]) == (300, 200, 0)


def _takes_record(conn, b, x, when):
    """Order `b` takes the whole record of storage `x` by «Взять с ОТХ» (oth_sale.take)."""
    from tires_sync import oth_sale
    line = orders.get_card(conn, b)["items"][0]["id"]
    item = orders.get_card(conn, x)["items"][0]["id"]
    assert oth_sale.take(conn, b, line, item, when, "o") == 4


def test_a_return_at_the_end_of_a_long_chain_pays_the_first_arrival(tmp_path, monkeypatch):
    """The review 2026-11-26: A came, refused → record X1; B took X1 whole and came (no tyres of its own), refused → X2 → «На возврат»."""
    conn = order_db(tmp_path)
    a = point(conn, "Альфа")
    first, x1 = _on_oth(conn, monkeypatch, a, DAY)
    b = card(conn, a, phone="+7 900 000 00 71")
    _takes_record(conn, b, x1, "2026-12-11T10:00:00")
    go(conn, b, "placed", "moving", "at_point", when="2026-12-11T12:00:00")
    assert orders.move_card(conn, b, stage(conn, "oth"), "2026-12-12T10:00:00", "o", {"cancel_reason": "отменил клиент"})
    from tires_sync import oth_storage
    x2 = oth_storage.record_of(conn, b)
    assert x2 and [w["order"] for w in order_report.waiting(conn, "2026-12-13", a)] == [first]
    assert _income(conn, a, START, END) == {}
    _give_back(conn, x2, "2026-12-16T10:00:00")
    [entry] = order_report.point_income(conn, START, END, a)["orders"]
    assert (entry["order"], entry["day"], entry["issue"]) == (first, "2026-12-16", 300)       # one fee, for the first arrival
    assert order_report.waiting(conn, "2026-12-16", a) == []


def _own_and_taken(conn, monkeypatch, a):
    """A came (4), refused → X1; B took X1 and has 4 tyres of its own, came, refused → X2 holds 8 tyres of two arrivals."""
    from tires_sync import oth_storage
    first, x1 = _on_oth(conn, monkeypatch, a, DAY)
    b = card(conn, a, phone="+7 900 000 00 81")
    orders.add_item(conn, b, goods_id="2", name="Nokian 215/55 R17", brand_key="nokian", season="winter", diameter=17, territory="1",
                    qty=4, sale_unit=5000, supplier_price=4000, terms=None, fee_pct=0, promo_pct=0, now=DAY, author="o")
    _takes_record(conn, b, x1, "2026-12-11T10:00:00")
    go(conn, b, "placed", "moving", "at_point", when="2026-12-11T12:00:00")
    assert point_arrival._row(conn, b)["pieces"] == 4
    assert orders.move_card(conn, b, stage(conn, "oth"), "2026-12-12T10:00:00", "o", {"cancel_reason": "отменил клиент"})
    return first, b, oth_storage.record_of(conn, b)


def test_a_record_of_two_arrivals_given_back_pays_both(tmp_path, monkeypatch):
    conn = order_db(tmp_path)
    a = point(conn, "Альфа")
    first, b, x2 = _own_and_taken(conn, monkeypatch, a)
    assert sorted((w["order"], w["pieces"]) for w in order_report.waiting(conn, "2026-12-13", a)) == [(first, 4), (b, 4)]
    _give_back(conn, x2, "2026-12-16T10:00:00")
    assert _income(conn, a, START, END) == {first: (300, 0), b: (300, 0)}


def test_a_sold_tyre_of_a_shared_record_is_taken_off_one_arrival_only(tmp_path, monkeypatch):
    """The review 2026-11-26 (T2): X2 holds 8 tyres of A and B; C took 6 and sold them; the other 2 go back: 450 + 150 = 600 ₽."""
    conn = order_db(tmp_path)
    a = point(conn, "Альфа")
    first, b, x2 = _own_and_taken(conn, monkeypatch, a)
    c = card(conn, a, qty=6, phone="+7 900 000 00 82")
    go(conn, c, "placed")
    _take_from(conn, c, x2, 6)
    go(conn, c, "moving", "at_point", "done", when="2026-12-14T10:00:00")
    orders.record_sales(conn, c, "2026-12-14", NOW, "owner", "2026-12-14")
    assert [(w["order"], w["pieces"]) for w in order_report.waiting(conn, "2026-12-14", a)] == [(first, 2)]   # B's own 4 sold first
    _give_back(conn, x2, "2026-12-16T10:00:00")
    got = order_report.point_income(conn, START, END, a)["orders"]
    assert sorted((e["order"], e["issue"], e["issue_pieces"]) for e in got) == [(first, 150, 2), (c, 450, 6)]
    assert order_report.waiting(conn, "2026-12-16", a) == []


def test_tyres_taken_by_an_order_in_work_keep_the_chain_waiting(tmp_path, monkeypatch):
    conn = order_db(tmp_path)
    a = point(conn, "Альфа")
    first, x = _on_oth(conn, monkeypatch, a, DAY)
    b = card(conn, a, qty=2, phone="+7 900 000 00 72")
    go(conn, b, "placed")
    _take_from(conn, b, x, 2)                                                    # 2 of the 4 taken by an order not sold yet
    _give_back(conn, x, "2026-12-14T10:00:00")                                   # the other 2 go back
    assert _income(conn, a, START, END) == {}                                    # no temporary overpay: the chain waits for B
    assert [w["order"] for w in order_report.waiting(conn, "2026-12-14", a)] == [first]
    go(conn, b, "moving", "at_point", "done", when="2026-12-15T10:00:00")
    orders.record_sales(conn, b, "2026-12-15", NOW, "owner", "2026-12-15")
    got = order_report.point_income(conn, START, END, a)["orders"]
    assert sorted((e["order"], e["day"], e["issue"]) for e in got) == [(first, "2026-12-15", 150), (b, "2026-12-15", 150)]


def test_a_new_client_by_take_gives_no_handed_bonus_on_the_own_oth(tmp_path):
    from tires_sync import oth_swap
    conn = order_db(tmp_path)
    a = point(conn, "Альфа")
    cid = card(conn, a)
    go(conn, cid, "placed", "moving", "at_point", "oth")                        # the old way: the order itself on «На ОТХ»
    b = card(conn, a, phone="+7 900 000 00 73")
    handed(conn, b, a)
    line, item = orders.get_card(conn, b)["items"][0]["id"], orders.get_card(conn, cid)["items"][0]["id"]
    assert oth_swap.take_target(conn, b, line, item) == cid
    oth_swap.take_client(conn, b, line, item, "2026-12-12T10:00:00", "owner")
    go(conn, cid, "done", when="2026-12-13T10:00:00")
    orders.record_sales(conn, cid, "2026-12-13", NOW, "owner", "2026-12-13")
    [entry] = order_report.point_income(conn, START, END, a)["orders"]
    assert (entry["order"], entry["issue"], entry["handoff"], entry["oth"]) == (cid, 300, 0, 0)     # the office sold: no bonus at all


def test_the_handed_client_stays_counted_before_the_bonuses(tmp_path, monkeypatch):
    conn = order_db(tmp_path)
    a = point(conn, "Альфа")
    first, x = _on_oth(conn, monkeypatch, a, "2026-11-28T10:00:00")
    b = _point_sells(conn, x, "2026-11-29", "менеджер Альфа")
    handed(conn, b, a)
    rep = order_report.build(conn, "2026-11-01", "2026-11-30")
    assert rep["handoff"]["clients"] == 1 and rep["handoff"]["fee"] == 0       # the counts of a past period do not change


def test_the_expected_handed_bonus_leaves_out_the_tyres_of_the_own_oth(tmp_path, monkeypatch):
    from tires_sync.panel import income_view
    conn = order_db(tmp_path)
    a = point(conn, "Альфа")
    first, x = _on_oth(conn, monkeypatch, a, DAY)
    b = card(conn, a, phone="+7 900 000 00 74")
    go(conn, b, "placed")
    _take_from(conn, b, x, 4)
    handed(conn, b, a)
    other = card(conn, a, phone="+7 900 000 00 75")
    handed(conn, other, a)
    view = income_view.build(conn, a, {"tab": "bonus", "from": START, "to": END, "deal": "all", "pay": "all"}, "2026-12-15")
    amounts = {r["order"]: r["amount"] for r in view["rows"]}
    assert amounts[b] == 0 and amounts[other] == 200


def test_a_new_client_of_the_office_on_oth_is_one_issue_without_the_bonus(tmp_path):
    from tires_sync import oth_swap
    conn = order_db(tmp_path)
    a = point(conn, "Альфа")
    cid = card(conn, a)
    go(conn, cid, "placed", "moving", "at_point", "oth")                        # the old way: the order itself on «На ОТХ»
    oth_swap.to_client(conn, cid, "Пётр", "+7 900 555 66 77", "2026-12-12T10:00:00", "owner")
    assert orders.get_card(conn, cid)["stage"]["code"] == "at_point" and _income(conn, a, START, END) == {}
    go(conn, cid, "done", when="2026-12-13T10:00:00")
    orders.record_sales(conn, cid, "2026-12-13", NOW, "owner", "2026-12-13")
    assert _income(conn, a, START, END) == {cid: (300, 0)}                      # one issue for the chain, no bonus of «На ОТХ»


# ---- the owner 2026-11-27: «Точка не передала статус», the texts of «Доход» by the office's settings ---------------------------------------

def _no_status(conn, cid, when=DAY):
    assert orders.move_card(conn, cid, stage(conn, "cancel"), when, "o", {"cancel_reason": orders.cancel_reason_of(orders.NO_STATUS, "")},
                            oth_split=False)


def test_a_cancel_the_point_did_not_pass_the_status_of_pays_nothing(tmp_path):
    from tires_sync import clients
    conn = order_db(tmp_path)
    a = point(conn, "Альфа")
    late = card(conn, a, phone="+7 900 000 00 01")
    go(conn, late, "placed", "moving", "at_point")
    _no_status(conn, late)
    usual = card(conn, a, phone="+7 900 000 00 02")
    go(conn, usual, "placed", "moving", "at_point", "cancel")             # «отменил клиент»: paid as before
    conn.commit()
    assert issue_of(conn, a) == {usual: (300, True)}
    assert late not in [w["order"] for w in order_report.waiting(conn, "2026-12-15", a)]
    assert late in point_arrival.no_status(conn) and orders.get_card(conn, late)["cancel_reason"] == orders.NO_STATUS
    client = clients.client_of_order(conn, late)
    assert client is None or clients.REFUSED_ORDER_TAG not in clients.tags_of(conn, client["id"])     # no «Неблагонадёжный»
    stuck = card(conn, a, qty=2, phone="+7 900 000 00 03")                 # on «На ОТХ» by the point's «ОТКАЗ», then given back: no fee
    go(conn, stuck, "placed", "moving", "at_point")
    assert orders.move_card(conn, stuck, stage(conn, "oth"), DAY, "o", {"cancel_reason": "Отказ клиента на точке: Точка не передала статус"},
                            oth_split=False)
    _give_back(conn, stuck, "2026-12-14T10:00:00")
    conn.commit()
    assert stuck not in issue_of(conn, a)


def test_a_settled_arrival_stays_when_the_reason_comes_later(tmp_path):
    conn = order_db(tmp_path)
    a = point(conn, "Альфа")
    cid = card(conn, a)
    go(conn, cid, "placed", "moving", "at_point", "cancel")
    conn.commit()
    point_payouts.settle(conn, [(a, "order:%d" % cid)], START, END, NOW, "owner", today="2026-12-15")
    point_arrival.set_no_status(conn, cid, True)                           # the mark after the settling
    assert issue_of(conn, a) == {cid: (300, True)}                         # «Рассчитан» is never recounted
    assert cid not in point_arrival.no_status(conn)


def test_the_new_reason_is_in_both_windows(tmp_path):
    app, owner, tok, conn, a, b = panel(tmp_path)
    orders.ensure_pipeline(conn, DAY)
    cid = card(conn, a)
    go(conn, cid, "placed", "moving", "at_point")
    conn.commit()
    page = owner.get("/orders/%d" % cid).get_data(as_text=True)
    assert 'name="cancel_kind" value="точка не передала статус"' in page
    post(owner, "/orders/%d/move" % cid, tok, stage_id=str(stage(conn, "cancel")), cancel_kind="точка не передала статус", oth_answer="no")
    assert orders.get_card(conn, cid)["stage"]["code"] == "cancel" and cid in point_arrival.no_status(conn)
    assert cid not in [u["order"] for u in point_arrival.unsold(conn, START, END)]       # the fixture's sales hold numbers 1–6: the arrival only
    from tires_sync.panel import routes_mypoint
    assert routes_mypoint.REFUSE_CHOICES["status"] == "Точка не передала статус"
    for name in ("mypoint.html", "point2/mypoint.html"):
        text = open("src/tires_sync/panel/templates/" + name, encoding="utf-8").read()
        assert 'name="reason" value="status"> Точка не передала статус' in text, name


def test_the_kpi_text_shows_the_premium_of_the_settings_and_follows_a_change(tmp_path):
    app, owner, tok, conn, a, b = panel(tmp_path)
    switch(owner, tok, True)
    mine = manager(app, conn, a)
    url = "/my-point/income?tab=kpi&km=2026-11"
    page = mine.get(url).get_data(as_text=True).replace("\xa0", " ")
    assert "+0 ₽" not in page and "+1 500 ₽</b> сверх выдачи и бонусов" in page
    assert "20 заказов или 72 шины — +3 000 ₽" in page and "30 заказов или 108 шин — +4 500 ₽" in page
    assert "KPI считаются с 01 декабря 2026 года" in page and page.index("data-kpi-text") < page.index("data-income-tabs")
    order_report.set_rates(conn, "2026-11-27", "2026-11-27", NOW, "owner", kpi_tyres=40, kpi_bonus=2000, handoff_fee=60)
    page = mine.get(url).get_data(as_text=True).replace("\xa0", " ")
    assert "10 заказов или 40 шин</b> (~10 комплектов)" in page and "+2 000 ₽</b>" in page
    assert "30 заказов или 120 шин — +6 000 ₽" in page and "+ 2 400 ₽" in page          # the example: 40 tyres × 60 ₽
    bonus = mine.get("/my-point/income?tab=bonus").get_data(as_text=True).replace("\xa0", " ")
    assert "Бонус +240 ₽ за комплект" in bonus and bonus.index("data-bonus-banner") < bonus.index("data-income-tabs")


def test_the_mark_goes_when_the_order_is_back_in_work_and_a_text_never_sets_it(tmp_path):
    from tires_sync import clients
    conn = order_db(tmp_path)
    a = point(conn, "Альфа")
    cid = card(conn, a)
    go(conn, cid, "placed", "moving", "at_point")
    assert orders.move_card(conn, cid, stage(conn, "oth"), DAY, "o", {"cancel_reason": "Отказ клиента на точке: Точка не передала статус"},
                            oth_split=False)
    assert cid in point_arrival.no_status(conn)
    assert orders.move_card(conn, cid, stage(conn, "at_point"), "2026-12-11T10:00:00", "o")      # back to the client: in work again
    assert cid not in point_arrival.no_status(conn)
    assert orders.move_card(conn, cid, stage(conn, "oth"), "2026-12-12T10:00:00", "o", oth_split=False)   # a real refusal, no new reason
    _give_back(conn, cid, "2026-12-14T10:00:00")
    conn.commit()
    assert issue_of(conn, a)[cid] == (300, True)
    client = clients.client_of_order(conn, cid)
    assert client is None or clients.REFUSED_ORDER_TAG in clients.tags_of(conn, client["id"])
    other = card(conn, a, phone="+7 900 000 00 08")                         # «другое» naming the words: no mark, paid
    go(conn, other, "placed", "moving", "at_point")
    assert orders.move_card(conn, other, stage(conn, "cancel"), DAY, "o",
                            {"cancel_reason": orders.cancel_reason_of("другое", "точка не передала статус вовремя, но клиент ждал")},
                            oth_split=False)
    conn.commit()
    assert other not in point_arrival.no_status(conn) and issue_of(conn, a)[other] == (300, True)


def test_the_split_of_a_refusal_keeps_the_mark_on_the_cancelled_order(tmp_path):
    from tires_sync import oth_storage
    conn = order_db(tmp_path)
    a = point(conn, "Альфа")
    cid = card(conn, a)
    go(conn, cid, "placed", "moving", "at_point")
    assert orders.move_card(conn, cid, stage(conn, "oth"), DAY, "o", {"cancel_reason": "Отказ клиента на точке: Точка не передала статус"})
    record = oth_storage.record_of(conn, cid)
    if record:
        _give_back(conn, record, "2026-12-14T10:00:00")
    conn.commit()
    assert cid in point_arrival.no_status(conn) and cid not in issue_of(conn, a)
