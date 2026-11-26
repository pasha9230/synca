"""Взаиморасчёты с точками (ТЗ 91, 95): the active debt of a point, «Рассчитан» of the office (by the order and for a whole point), the
request «Вычли с кассы и получили бонусы» of the point (a task; the sum stays until «Рассчитан»), the rights."""
from __future__ import annotations

import re

from panel_helpers import post
from test_mypoint import manager
from test_point_income import START, END, NOW, TODAY, base, db, panel, switch

from tires_sync import point_payouts, store, users


def test_items_balance_and_settle(tmp_path):
    conn = db(tmp_path)
    a, b = base(conn)
    found = {e["item"]: e for e in point_payouts.items(conn, START, END, a)}
    assert sorted(found) == ["order:1", "order:2", "order:3", "order:4", "order:5"]
    assert found["order:2"]["total"] == 200 and found["order:3"]["total"] == 250
    debt = point_payouts.balance(conn, a, TODAY)
    assert (debt["issue"], debt["bonus"], debt["kpi"], debt["total"], debt["count"]) == (1050, 500, 0, 1550, 5)
    done = point_payouts.settle(conn, [(a, "order:3"), (a, "order:2"), (a, "order:99"), (b, "order:3")], START, END, NOW, "owner")
    assert done == {"count": 2, "amount": 450}                               # unknown items and another point's are skipped
    assert point_payouts.balance(conn, a, TODAY)["total"] == 1100
    paid = {e["item"]: e["paid"] for e in point_payouts.items(conn, START, END, a)}
    assert paid["order:3"]["amount"] == 250 and paid["order:1"] is None
    try:
        point_payouts.settle(conn, [(a, "order:3")], START, END, NOW, "owner")    # once
        raise AssertionError("settled twice")
    except point_payouts.PayoutError:
        pass
    assert conn.execute("SELECT COUNT(*) FROM order_event WHERE order_id = 3 AND text LIKE 'Рассчитано с точкой%'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM audit WHERE action = 'point_paid'").fetchone()[0] == 1


def test_the_request_makes_a_task_and_keeps_the_sum(tmp_path):
    conn = db(tmp_path)
    a, b = base(conn)
    made = point_payouts.request(conn, a, TODAY, NOW, "gamma")
    conn.commit()
    assert made["amount"] == 1550
    task = conn.execute("SELECT title, assignee FROM callback_task WHERE id = ?", (made["task_id"],)).fetchone()
    assert "Альфа" in task[0] and "Рассчитан" in task[0]
    assert point_payouts.balance(conn, a, TODAY)["total"] == 1550             # not zeroed by the point
    try:
        point_payouts.request(conn, a, TODAY, NOW, "gamma")
        raise AssertionError("two requests")
    except point_payouts.PayoutError:
        pass
    assert point_payouts.open_requests(conn, TODAY)[a]["amount"] == 1550
    point_payouts.settle(conn, [(a, "order:%d" % n) for n in (1, 2, 3, 4, 5)], START, END, NOW, "owner")
    assert point_payouts.balance(conn, a, TODAY)["total"] == 0 and a not in point_payouts.open_requests(conn, TODAY)


def test_the_point_page_debt_request_and_paid_hidden(tmp_path):
    app, owner, tok, conn, a, b = panel(tmp_path)
    switch(owner, tok, True)
    mine = manager(app, conn, a)
    page = mine.get("/my-point/income").get_data(as_text=True).replace("\xa0", " ")
    assert 'data-debt="total" style="font-size:var(--fs-2xl, 1.75rem)">1 550 ₽' in page
    assert "Вычли с кассы и получили бонусы" in page
    assert post(mine, "/my-point/income/request", "tok").status_code == 302
    page = mine.get("/my-point/income").get_data(as_text=True).replace("\xa0", " ")
    assert "data-request-open" in page and "1 550 ₽" in page
    assert conn.execute("SELECT COUNT(*) FROM point_paid_request WHERE point_id = ?", (a,)).fetchone()[0] == 1
    point_payouts.settle(conn, [(a, "order:1")], START, END, NOW, "owner")
    conn.commit()
    page = mine.get("/my-point/income").get_data(as_text=True)
    assert sorted(map(int, re.findall(r'<tr data-order="(\d+)">', page))) == [3, 4, 5]          # «Рассчитан» leaves the active list
    paid = mine.get("/my-point/income?pay=paid").get_data(as_text=True)
    assert re.findall(r'<tr data-order="(\d+)">', paid) == ["1"]
    by_sum = mine.get("/my-point/income?pay=all&sort=sum&dir=desc").get_data(as_text=True)
    assert len(re.findall(r'<tr data-order="(\d+)">', by_sum)) == 4
    search = mine.get("/my-point/income?pay=all&q=%E2%84%96+4").get_data(as_text=True)          # «№ 4»
    assert re.findall(r'<tr data-order="(\d+)">', search) == ["4"]
    bonus = mine.get("/my-point/income?tab=bonus&deal=done").get_data(as_text=True)
    assert "Бонус +200" in bonus.replace("\xa0", " ") and 'data-deal="done"' in bonus


def test_the_switch_off_hides_the_request_too(tmp_path):
    app, owner, tok, conn, a, b = panel(tmp_path)
    mine = manager(app, conn, a)
    assert post(mine, "/my-point/income/request", "tok").status_code == 404
    assert not conn.execute("SELECT COUNT(*) FROM point_paid_request").fetchone()[0]


def test_the_office_ledger_and_settle_rights(tmp_path):
    app, owner, tok, conn, a, b = panel(tmp_path)
    page = owner.get("/points/payouts?from=%s&to=%s" % (START, END)).get_data(as_text=True).replace("\xa0", " ")
    assert "Пункты выдачи" in page and 'href="/points/payouts"' in page
    assert re.search(r'<tr data-point="%d"><td>Альфа</td><td>1 050 ₽</td><td>500 ₽</td><td>0 ₽</td><td>0 ₽</td><td>1 550 ₽</td><td>0 ₽</td><td><b>1 550 ₽</b>' % a, page)
    got = post(owner, "/points/payouts/settle", tok, **{"from": START, "to": END, "item": ["%d|order:1" % a, "%d|order:3" % a]})
    assert got.status_code == 302
    assert point_payouts.balance(conn, a, TODAY)["total"] == 1000
    page = owner.get("/points/payouts?from=%s&to=%s&pay=paid" % (START, END)).get_data(as_text=True)
    assert sorted(re.findall(r'data-item="%d\|order:(\d+)"' % a, page)) == ["1", "3"]
    for role in ("chats", "point"):                                           # no money right / a point: no ledger, no «Рассчитан»
        login_ = "u" + role
        users.add_user(conn, login_, "Secret-pass-123", NOW, role=role, point_id=a if role == "point" else None)
        conn.commit()
        other = app.test_client()
        with other.session_transaction() as session:
            session["user"], session["epoch"], session["csrf"] = login_, store.get_session_epoch(conn, login_), "tok"
        assert other.get("/points/payouts").status_code == 403
        assert post(other, "/points/payouts/settle", "tok", **{"from": START, "to": END, "item": "%d|order:4" % a}).status_code == 403
    assert point_payouts.balance(conn, a, TODAY)["total"] == 1000


def test_the_settings_page_saves_kpi_and_the_rates_of_a_point(tmp_path):
    from tires_sync import order_report
    app, owner, tok, conn, a, b = panel(tmp_path)
    page = owner.get("/orders/settings/money").get_data(as_text=True)
    assert 'name="kpi_orders"' in page and "Индивидуальные настройки точек" in page
    assert post(owner, "/orders/report/rates", tok, kpi_orders="12", kpi_orders_was="10", kpi_tyres="38", kpi_tyres_was="38",
                kpi_bonus="1500", kpi_bonus_was="1500", valid_from=TODAY).status_code == 302
    assert [(r["name"], r["value"]) for r in order_report.list_rates(conn)] == [("kpi_orders", 12.0)]
    assert post(owner, "/orders/report/rates", tok, what="point_rates", point_id=str(a), point_fee="90", kpi_bonus="2 000",
                valid_from=TODAY).status_code == 302
    assert order_report.point_rates_now(conn, a, TODAY)["point_fee"] == 90 and order_report.point_rates_now(conn, a, TODAY)["kpi_bonus"] == 2000
    assert order_report.point_rates_now(conn, b, TODAY)["point_fee"] == 75
    page = owner.get("/orders/settings/money").get_data(as_text=True)
    assert "data-point-rates-list" in page and "Альфа" in page
    assert post(owner, "/orders/report/rates", tok, what="point_rates", point_id=str(a), reset="1", valid_from="2026-12-16").status_code == 302
    assert order_report.point_rates_now(conn, a, "2026-12-16")["point_fee"] == 75
    assert post(owner, "/orders/report/rates", tok, what="point_rates", point_id=str(a), kpi_orders="0", valid_from=TODAY).status_code == 302
    assert order_report.point_rates_now(conn, a, TODAY)["kpi_orders"] == 12                  # refused (1 and more): the general one stays


def test_a_client_given_by_phone_is_set_in_the_order_window(tmp_path):
    from tires_sync import order_report, orders
    app, owner, tok, conn, a, b = panel(tmp_path)
    card = orders.create_card(conn, {"client_name": "Тест", "client_phone": "+7 900 111 22 33", "point_id": b, "source": "pvz"}, NOW, "o")
    conn.commit()
    page = owner.get("/orders/%d" % card).get_data(as_text=True)
    assert 'name="source_point"' in page
    assert point_payouts.set_source_point(conn, card, str(a), "", NOW, "owner")
    assert order_report.source_point(conn, card) == a and order_report.handed_point(conn, card) == a
    assert not point_payouts.set_source_point(conn, card, str(a), str(a), NOW, "owner")            # as the window showed it
    assert 'name="source_point_was" value="%d"' % a in owner.get("/orders/%d" % card).get_data(as_text=True)
    handed_page = manager(app, conn, a).get("/my-point/handed").get_data(as_text=True)
    assert "№ %d" % card in handed_page and "указано офисом" in handed_page
    orders.update_card(conn, card, {"source": "avito"}, NOW, "owner")
    assert point_payouts.set_source_point(conn, card, str(a), str(a), NOW, "owner")                # another source clears it
    assert order_report.source_point(conn, card) is None
    events = [e["text"] for e in orders.list_events(conn, card)]
    assert any("передала точка «Альфа»" in t for t in events) and any("больше не указана" in t for t in events)
