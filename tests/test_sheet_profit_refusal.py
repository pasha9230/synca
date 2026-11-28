"""The owner 2026-11-28: the profit of the Google table's rows takes off what the report does (one count, the KPI and the sums the office
set); «О» / «Н» go by the fact the tyres came; «Неблагонадёжный» for an order closed as «Н», taken off when it is back in work."""
from __future__ import annotations

from test_order_reserve import db as order_db, stage
from test_point_income import db, point
from test_point_income_rules import DAY, card, go
from test_point_kpi import NOW, clients as handed_orders

from tires_sync import clients, order_report, order_sheet, orders, point_payouts, sales

START, END = "2026-12-01", "2026-12-31"


def _fee(conn, order_id):
    rows = [r for r in sales.list_sales(conn, START, END) if r["note"] == "Заказ №%d" % order_id]
    return order_report.order_fee(conn, order_id, order_report._counted(conn, rows), keeps_before=order_sheet.KEEPS_BEFORE)


def test_the_row_takes_off_the_kpi_the_order_closes_and_adds_up_to_the_report(tmp_path):
    conn = db(tmp_path)
    a, b = point(conn, "Альфа"), point(conn, "Бета")
    handed_orders(conn, a, 10, pieces=4, day="2026-12-05", issued=b)          # 10 clients Альфа handed over: one KPI premium
    fees = {o: _fee(conn, o) for o in range(1, 11)}
    rep = order_report.build(conn, START, END)
    assert rep["kpi"]["fee"] == order_report.KPI_BONUS
    assert round(sum(fees.values()), 2) == rep["total"]["fee"]                   # the rows: the report's fees, bonuses and KPI
    [premium] = rep["kpi"]["premiums"][("id", a)]
    closing = premium["order"]                                                  # 9 × 4 = 36 tyres: «ИЛИ» by the tyres
    assert closing == 9 and fees[9] - fees[1] == order_report.KPI_BONUS and fees[10] == fees[1]
    point_payouts.fix_items(conn, [(a, premium["item"])], "kpi", 1000.0, "проверка", NOW, "owner")
    assert _fee(conn, closing) == fees[closing] - 500                           # the sum the office set, as the report
    assert order_report.build(conn, START, END)["total"]["fee"] == round(sum(_fee(conn, o) for o in range(1, 11)), 2)


def test_o_and_n_go_by_the_tyres_that_came(tmp_path):
    conn = order_db(tmp_path)
    a = point(conn, "Альфа")
    before = card(conn, a, phone="+7 900 000 00 01")
    go(conn, before, "placed", "moving", "cancel")                              # «отменил клиент», the tyres never came: «О»
    after = card(conn, a, phone="+7 900 000 00 02")
    go(conn, after, "placed", "moving", "at_point")
    assert orders.move_card(conn, after, stage(conn, "cancel"), DAY, "o", {"cancel_reason": orders.cancel_reason_of("другое", "передумал")},
                            oth_split=False)                                    # whatever the reason: the tyres came, «Н»
    late = card(conn, a, phone="+7 900 000 00 03")
    go(conn, late, "placed", "moving")                                          # the point never pressed «На точке»
    assert orders.move_card(conn, late, stage(conn, "cancel"), DAY, "o", {"cancel_reason": orders.cancel_reason_of(orders.NO_STATUS, "")},
                            oth_split=False)
    conn.commit()
    got = {cid: order_sheet.realization(orders.get_card(conn, cid), conn) for cid in (before, after, late)}
    assert got == {before: order_sheet.CANCELLED, after: order_sheet.NOT_BOUGHT, late: order_sheet.NOT_BOUGHT}


def _tagged(conn, cid):
    client = clients.client_of_order(conn, cid)
    assert client is not None
    return clients.REFUSED_ORDER_TAG in clients.tags_of(conn, client["id"])


def test_the_mark_is_for_n_only_and_goes_when_the_order_is_back_in_work(tmp_path):
    conn = order_db(tmp_path)
    a = point(conn, "Альфа")
    early = card(conn, a, phone="+7 900 000 00 01")
    go(conn, early, "placed", "moving", "cancel")                               # «О»: «отменил клиент» before the tyres came — no mark
    conn.commit()
    assert not _tagged(conn, early)
    cid = card(conn, a, phone="+7 900 000 00 02")
    go(conn, cid, "placed", "moving", "at_point")
    assert orders.move_card(conn, cid, stage(conn, "cancel"), DAY, "o", {"cancel_reason": orders.cancel_reason_of("другое", "передумал")},
                            oth_split=False)                                    # «Н»: the mark, whatever the reason
    conn.commit()
    assert _tagged(conn, cid)
    assert orders.move_card(conn, cid, stage(conn, "at_point"), "2026-12-11T10:00:00", "o")       # back in work: the mark goes
    conn.commit()
    assert not _tagged(conn, cid)


def test_a_mark_typed_by_hand_or_of_another_n_order_stays(tmp_path):
    conn = order_db(tmp_path)
    a = point(conn, "Альфа")
    one = card(conn, a, phone="+7 900 000 00 05")
    two = card(conn, a, phone="+7 900 000 00 05")                               # the same client
    for cid in (one, two):
        go(conn, cid, "placed", "moving", "at_point", "cancel")
    conn.commit()
    assert _tagged(conn, one)
    assert orders.move_card(conn, one, stage(conn, "at_point"), "2026-12-11T10:00:00", "o")
    conn.commit()
    assert _tagged(conn, one)                                                    # the other order is still «Н»
    hand = card(conn, a, phone="+7 900 000 00 06")
    clients.set_tag(conn, clients.client_of_order(conn, hand)["id"], clients.REFUSED_ORDER_TAG, True, NOW, "owner")
    go(conn, hand, "placed", "moving", "at_point", "cancel")
    assert orders.move_card(conn, hand, stage(conn, "at_point"), "2026-12-11T10:00:00", "o")
    conn.commit()
    assert _tagged(conn, hand)                                                   # typed by hand: stays


def test_no_mark_for_the_point_that_did_not_pass_the_status(tmp_path):
    conn = order_db(tmp_path)
    a = point(conn, "Альфа")
    cid = card(conn, a)
    go(conn, cid, "placed", "moving", "at_point")
    assert orders.move_card(conn, cid, stage(conn, "cancel"), DAY, "o", {"cancel_reason": orders.cancel_reason_of(orders.NO_STATUS, "")},
                            oth_split=False)
    conn.commit()
    assert order_sheet.realization(orders.get_card(conn, cid), conn) == order_sheet.NOT_BOUGHT and not _tagged(conn, cid)
