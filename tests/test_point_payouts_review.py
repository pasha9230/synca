"""Ревью расчётов с точками (2026-11-25) и решения владельца: доплата и переплата после «Рассчитан», отмена расчёта, корректировка, сверка
суммы «Рассчитан (всё)», запрос точки, KPI заказа в двух месяцах, «чей клиент» одно правило с таблицей, выплата за выдачу за шину."""
from __future__ import annotations

import re

from panel_helpers import post
from test_mypoint import manager
from test_point_income import END, NOW, START, TODAY, base, db, handed, panel, point, sell, switch

from tires_sync import order_report, orders, point_payouts, sales


def test_a_sum_that_grew_after_the_payment_owes_a_doplata_paid_as_a_row_of_its_own(tmp_path):
    conn = db(tmp_path)
    a, b = base(conn)
    point_payouts.settle(conn, [(a, "order:1")], START, END, NOW, "owner")                  # 300 ₽ paid
    sell(conn, 1, a, pieces=2)                                                             # the order got 2 tyres more: 450 ₽
    [e] = [e for e in point_payouts.items(conn, START, END, a) if e["item"] == "order:1"]
    assert (e["total"], e["paid_sum"], e["due"], e["status"]) == (450, 300, 150, "extra")
    assert point_payouts.balance(conn, a, TODAY)["extra"] == 150
    assert point_payouts.settle(conn, [(a, "order:1")], START, END, NOW, "owner")["amount"] == 150
    assert [r[0] for r in conn.execute("SELECT amount FROM point_paid WHERE item = 'order:1' ORDER BY id")] == [300, 150]
    [e] = [e for e in point_payouts.items(conn, START, END, a) if e["item"] == "order:1"]
    assert (e["due"], e["status"]) == (0, "paid")


def test_a_reversed_sale_after_the_payment_shows_an_overpayment_and_its_hold_back(tmp_path):
    conn = db(tmp_path)
    a = point(conn, "Альфа")
    sale = sell(conn, 1, a)
    point_payouts.settle(conn, [(a, "order:1")], START, END, NOW, "owner")
    sales.reverse_sale(conn, sale, NOW, "o", from_order=True)                             # gone from the report: kept with 0
    [e] = point_payouts.items(conn, START, END, a)
    assert (e["total"], e["paid_sum"], e["due"], e["status"]) == (0, 300, -300, "over")
    debt = point_payouts.balance(conn, a, TODAY)
    assert (debt["over"], debt["total"]) == (-300, -300)
    assert point_payouts.settle(conn, [(a, "order:1")], START, END, NOW, "owner")["amount"] == -300     # held back
    [e] = point_payouts.items(conn, START, END, a)
    assert (e["paid_sum"], e["due"], e["status"]) == (0, 0, "paid")
    assert any("Удержано с точки" in ev["text"] for ev in orders.list_events(conn, 1))


def test_the_settle_all_sum_must_be_the_one_confirmed(tmp_path):
    conn = db(tmp_path)
    a, b = base(conn)
    chosen = [(a, "order:%d" % n) for n in (1, 2, 3, 4, 5)]
    try:
        point_payouts.settle(conn, chosen, START, END, NOW, "owner", expect=1000)
        raise AssertionError("another sum was taken")
    except point_payouts.PayoutError as err:
        assert "изменилась" in str(err)
    assert not conn.execute("SELECT COUNT(*) FROM point_paid").fetchone()[0]
    assert point_payouts.settle(conn, chosen, START, END, NOW, "owner", expect=1550)["amount"] == 1550


def test_the_office_page_settles_all_with_the_confirmed_sum_and_unsettles(tmp_path):
    app, owner, tok, conn, a, b = panel(tmp_path)
    page = owner.get("/points/payouts?from=%s&to=%s" % (START, END)).get_data(as_text=True)
    found = re.search(r'name="expect" value="([\d.]+)">((?:<input type="hidden" name="item" value="%d\|[^"]+">)+)' % a, page)
    assert found and found.group(1) == "1550.00"
    items = re.findall(r'value="(%d\|[^"]+)"' % a, found.group(2))
    sell(conn, 99, a)                                                                       # a new sale after the page was opened
    handed(conn, 99, b)
    got = post(owner, "/points/payouts/settle", tok, **{"from": START, "to": END, "expect": "1250.00", "item": items})
    assert got.status_code == 302 and not conn.execute("SELECT COUNT(*) FROM point_paid").fetchone()[0]     # 1 550 now: asked again
    post(owner, "/points/payouts/settle", tok, **{"from": START, "to": END, "expect": "1550.00", "item": items})
    assert point_payouts.balance(conn, a, TODAY)["total"] == 300                          # order 99 (issued by Альфа) stays owed
    post(owner, "/points/payouts/unsettle", tok, **{"from": START, "to": END, "item": "%d|order:3" % a})
    assert point_payouts.balance(conn, a, TODAY)["total"] == 550
    assert conn.execute("SELECT COUNT(*) FROM point_paid WHERE cancelled_at IS NOT NULL").fetchone()[0] == 1
    assert any("Расчёт с точкой «Альфа» отменён" in ev["text"] for ev in orders.list_events(conn, 3))


def test_an_adjustment_with_a_reason_is_an_item_the_point_sees(tmp_path):
    app, owner, tok, conn, a, b = panel(tmp_path)
    assert post(owner, "/points/payouts/adjust", tok, point_id=str(a), amount="-100", day=TODAY, reason="").status_code == 302
    assert not conn.execute("SELECT COUNT(*) FROM point_adjust").fetchone()[0]             # a reason is required
    post(owner, "/points/payouts/adjust", tok, point_id=str(a), amount="-100", day=TODAY, reason="недостача в кассе")
    debt = point_payouts.balance(conn, a, TODAY)
    assert (debt["adj"], debt["total"]) == (-100, 1450)
    report = order_report.build(conn, START, END)
    assert {r["point"]: r["fee"] for r in report["lines"]}["Альфа"] == 1550                # the counted sums stay
    switch(owner, tok, True)
    page = manager(app, conn, a).get("/my-point/income").get_data(as_text=True).replace("\xa0", " ")
    assert "Корректировка" in page and "недостача в кассе" in page and ">1 450 ₽" in page


def test_the_request_waits_only_for_items_still_owed_and_the_office_closes_it(tmp_path):
    conn = db(tmp_path)
    a, b = base(conn)
    made = point_payouts.request(conn, a, TODAY, NOW, "gamma")
    assert point_payouts.open_requests(conn, TODAY)[a]["id"] == made["id"]
    assert point_payouts.close_request(conn, made["id"], NOW, "owner")
    assert a not in point_payouts.open_requests(conn, TODAY)
    again = point_payouts.request(conn, a, TODAY, NOW, "gamma")                             # closed: a new one is taken
    sales.reverse_sale(conn, conn.execute("SELECT id FROM sale WHERE note = 'Заказ №1'").fetchone()[0], NOW, "o", from_order=True)
    point_payouts.settle(conn, [(a, "order:%d" % n) for n in (2, 3, 4, 5)], START, END, NOW, "owner")
    assert a not in point_payouts.open_requests(conn, TODAY) and again["id"]             # nothing of it is owed any more


def test_an_order_sold_in_two_months_counts_once_in_the_first(tmp_path):
    from test_point_kpi import sell as sell_any
    conn = db(tmp_path)
    a = point(conn, "Альфа")
    for order_id in range(1, 10):                                                           # 9 orders of December, 4 tyres
        sell_any(conn, order_id, a, pieces=4, day="2026-12-05")
        handed(conn, order_id, a)
    sell_any(conn, 10, a, pieces=2, day="2026-12-30")                                       # the 10th: 2 tyres in December,
    sell_any(conn, 10, a, pieces=2, day="2027-01-03")                                       # 2 more in January
    handed(conn, 10, a)
    jan = order_report.point_income(conn, "2027-01-01", "2027-01-31", a)
    assert jan["progress"].get("2027-01", {}).get("orders", 0) == 0                          # not again in January
    dec = order_report.point_income(conn, "2026-12-01", "2027-01-31", a)
    assert dec["progress"]["2026-12"]["orders"] == 10 and dec["progress"]["2026-12"]["tyres"] == 40 and dec["kpi"] == 1500


def test_the_point_never_sees_the_comment_nor_another_point(tmp_path):
    app, owner, tok, conn, a, b = panel(tmp_path)
    for _ in range(10):
        orders.create_card(conn, {"client_name": "-"}, NOW, "o")
    card = orders.create_card(conn, {"client_name": "Тест", "point_id": b, "comment": "СЕКРЕТ закупка 4000"}, NOW, "o")
    handed(conn, card, a)
    switch(owner, tok, True)
    mine = manager(app, conn, a)
    bonus = mine.get("/my-point/income?tab=bonus").get_data(as_text=True)
    assert 'data-bonus-order="%d"' % card in bonus and "СЕКРЕТ" not in bonus
    theirs = mine.get("/my-point/income?p=%d" % b).get_data(as_text=True)                  # another point's ?p=: still its own sums only
    assert re.findall(r'<tr data-order="(\d+)">', theirs) == re.findall(r'<tr data-order="(\d+)">', mine.get("/my-point/income").get_data(as_text=True))
    assert 'data-order="2"' not in theirs                                                   # order 2 is Бета's issue


def test_only_the_money_right_sets_the_point_of_the_client(tmp_path):
    from tires_sync import store, users
    app, owner, tok, conn, a, b = panel(tmp_path)
    card = orders.create_card(conn, {"client_name": "Тест", "point_id": b, "source": "pvz"}, NOW, "o")
    users.add_user(conn, "chats1", "Secret-pass-123", NOW, role="chats")
    conn.commit()
    other = app.test_client()
    with other.session_transaction() as session:
        session["user"], session["epoch"], session["csrf"] = "chats1", store.get_session_epoch(conn, "chats1"), "tok"
    assert 'name="source_point"' not in other.get("/orders/%d" % card).get_data(as_text=True)
    post(other, "/orders/%d/save" % card, "tok", source_point=str(a), source_point_was="")
    assert order_report.source_point(conn, card) is None
    assert 'name="source_point"' in owner.get("/orders/%d" % card).get_data(as_text=True)


def test_the_issue_fee_is_per_order_before_its_day_and_per_tyre_from_it(tmp_path):
    from test_point_kpi import sell as sell_any
    conn = db(tmp_path)
    a = point(conn, "Альфа")
    sell_any(conn, 1, a, pieces=4, day="2026-11-26")
    sell_any(conn, 2, a, pieces=4, day=order_report.POINT_FEE_PER_TYRE_FROM)
    got = {e["order"]: (e["issue"], e["issue_pieces"]) for e in order_report.point_income(conn, "2026-11-01", "2026-11-30", a)["orders"]}
    assert got == {1: (75, 0), 2: (300, 4)}
    assert order_report.HANDOFF_PER_TYRE_FROM == order_report.POINT_FEE_PER_TYRE_FROM == "2026-11-27"


def test_the_row_of_the_table_takes_the_report_rule_of_the_handed_client(tmp_path):
    from test_order_sheet import placed, stage
    from tires_sync import order_sheet as sheet
    from test_order_reserve import db as order_db
    conn = order_db(tmp_path)
    pid = point(conn, "Альфа")
    mine = placed(conn, goods=(("111", 4),))
    conn.execute("INSERT INTO order_source_point (order_id, point_id, at, author) VALUES (?, ?, ?, 'o')", (mine, pid, NOW))
    conn.commit()
    orders.move_card(conn, mine, stage(conn, "done")["id"], NOW, "owner")
    orders.record_sales(conn, mine, "2026-12-10", NOW, "owner", "2026-12-10")
    rows = list(sheet.rows_of(conn, mine, NOW).values())
    report = order_report.build(conn, "2026-12-10", "2026-12-10")
    assert round(sum(r["Прибыль ₽"] for r in rows), 2) == report["total"]["net"]
    assert report["handoff"]["fee"] == 200                                                  # «Откуда клиент» = the point: its bonus


def test_whats_new_shows_the_point_its_income_only_while_the_switch_is_on(tmp_path):
    app, owner, tok, conn, a, b = panel(tmp_path)
    mine = manager(app, conn, a)
    page = mine.get("/whats-new").get_data(as_text=True)
    assert "Чаты: вкладки" in page and "Доход: ваши выплаты" not in page and "Пункты выдачи: расчёты" not in page
    switch(owner, tok, True)
    page = mine.get("/whats-new").get_data(as_text=True)
    assert "Доход: ваши выплаты" in page and "Пункты выдачи: расчёты" not in page
    office = owner.get("/whats-new").get_data(as_text=True)
    assert "Пункты выдачи: расчёты с точками" in office and "Доход: ваши выплаты" in office
