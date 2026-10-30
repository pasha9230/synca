"""«Объединить заказы», этап 2 (plan docs/runs/2026-10-30-order-merge): the old numbers of the points lead to the merged order, «Рабочее место»
says where it came from and the handed clients follow it, the funnel counts it once, no WebMIM number is asked of it and «можно забирать»
goes without one."""
from __future__ import annotations

from test_mypoint import manager
from test_order_merge import NOW, _card, _merge, _second
from test_oth_sale import T, panel, point_id, stage
from tests.test_order_reserve import card_at, db
from tires_sync import analytics, order_manager as mgr, order_reserve, orders


def _merged(conn):
    a = card_at(conn, "moving", T)
    b = _second(conn)
    return a, b, _merge(conn, [a, b])


def test_the_old_number_of_a_source_leads_the_points_command_to_the_merged_order(tmp_path):
    conn = db(tmp_path)
    a, b, new = _merged(conn)
    assert mgr.find_orders(conn, int(_card(conn, a)["webmim_number"])) == [new]
    assert mgr.find_orders(conn, int(_card(conn, b)["webmim_number"])) == [new]
    assert mgr.find_orders(conn, new) == [new]                                    # its own number (the panel's) still names it


def test_the_workplace_says_where_the_merged_order_came_from(tmp_path):
    app, owner, tok, conn = panel(tmp_path)
    a, b, new = _merged(conn)
    page = manager(app, conn, point_id(conn)).get("/my-point?tab=moving").get_data(as_text=True)      # «Едут»: the merged order is moving
    card = page.split('id="order-%d"' % new)[1].split("</article>")[0]
    assert "Объединён из №%s, №%s" % (_card(conn, a)["webmim_number"], _card(conn, b)["webmim_number"]) in card


def test_a_handed_client_follows_the_merged_order(tmp_path):
    conn = db(tmp_path)
    a = card_at(conn, "moving", T)
    conn.execute("INSERT INTO order_handoff (order_id, message_id, point_id, created_at) VALUES (?, 'm1', ?, ?)", (a, point_id(conn), T))
    conn.commit()
    b = _second(conn)
    new = _merge(conn, [a, b])
    [row] = mgr.point_handoffs(conn, point_id(conn), "2026-10-01")
    assert row["status"] == "placed" and row["label"].endswith("№%d" % new)          # not «отказ»: the client buys in the merged order
    mgr.handoff_tick(conn, NOW)
    assert conn.execute("SELECT status FROM order_handoff_status WHERE order_id = ?", (a,)).fetchall() == [("placed",)]


def test_the_funnel_counts_the_merged_order_once_and_no_cancellation(tmp_path):
    conn = db(tmp_path)
    a, b, new = _merged(conn)
    total = analytics.funnel(conn, "2026-10-01", "2026-10-31")["total"]
    assert (total["orders"], total["cancelled"]) == (1, 0)


def test_no_webmim_number_is_asked_of_a_merged_order_of_placed_sources(tmp_path):
    conn = db(tmp_path)
    a, b, new = _merged(conn)
    told = []
    order_reserve._ask_number(conn, _card(conn, new), lambda c, t: (told.append(t), "ok")[1], NOW)
    assert told == []


def test_arrived_goes_without_a_webmim_number_for_a_merged_order(tmp_path):
    conn = db(tmp_path)
    a, b, new = _merged(conn)
    body = mgr._oth_arrived_body(conn, _card(conn, new))
    assert body is not None and "{webmim_number}" not in body


def test_the_number_of_a_source_for_reference_still_names_it(tmp_path):
    from test_oth_sale import done_with_sale, new_order
    conn = db(tmp_path)
    sold = new_order(conn, qty=2)
    done_with_sale(conn, sold)
    other = new_order(conn, qty=2, goods="2")
    new = _merge(conn, [other, sold], code="new")
    assert mgr.find_orders(conn, sold) == [sold] and mgr.find_orders(conn, other) == [new]     # its sale and tyres stayed its own


# ---- the review of 2026-10-30 ----------------------------------------------------------------------------------------------------------------

from tests.test_order_reserve import POINT_VK                               # noqa: E402  (Бета's manager in VK)


def test_arrived_by_an_old_number_of_a_source_is_no_arrival(tmp_path):
    """The owner 2026-10-30: the tyres of the sources are at the point already, in the merged order; only the tyres ordered for it travel."""
    conn = db(tmp_path)
    a, b, new = _merged(conn)
    na = _card(conn, a)["webmim_number"]
    answer = mgr.handle_command(conn, "m1", POINT_VK, "приехал %s" % na, None, NOW)
    assert answer == "Шины заказа №%s уже на точке, они в объединённом заказе №%s." % (na, new)
    assert _card(conn, new)["stage"]["code"] == "moving"                           # nothing moved, nothing sent
    assert conn.execute("SELECT COUNT(*) FROM manager_message").fetchone()[0] == 0


def test_returned_by_an_old_number_waits_for_the_owner(tmp_path):
    conn = db(tmp_path)
    a, b, new = _merged(conn)
    for step in ("at_point",):
        orders.move_card(conn, new, stage(conn, step), NOW, "o")
    na = _card(conn, a)["webmim_number"]
    answer = mgr.handle_command(conn, "m2", POINT_VK, "возврат %s" % na, None, NOW)         # «вернул», «не выкупил»: the owner confirms
    assert "часть объединённого" in answer and _card(conn, new)["stage"]["code"] == "at_point"
    text = conn.execute("SELECT text FROM manager_message WHERE message_id = 'm2'").fetchone()[0]
    assert "заказ %s — это часть объединённого №%s" % (na, new) in text


def test_arrived_by_the_merged_orders_own_number_moves_it(tmp_path):
    conn = db(tmp_path)
    a, b, new = _merged(conn)
    conn.execute("UPDATE order_card SET webmim_number = '9990001' WHERE id = ?", (new,))     # the tyres ordered for it, its own number
    conn.commit()
    mgr.handle_command(conn, "m3", POINT_VK, "приехал 9990001", None, NOW)
    assert _card(conn, new)["stage"]["code"] == "at_point"


def test_the_merged_order_keeps_the_source_and_the_responsible_and_the_funnel_counts_it_at_the_first_day(tmp_path):
    conn = db(tmp_path)
    a = card_at(conn, "moving", T)
    conn.execute("UPDATE order_card SET source = 'avito', responsible = 'zaman', created_at = '2026-09-20T10:00:00' WHERE id = ?", (a,))
    conn.commit()
    b = _second(conn)
    new = _merge(conn, [a, b])
    card = _card(conn, new)
    assert (card["source"], card["responsible"]) == ("avito", "zaman")
    assert analytics.funnel(conn, "2026-09-01", "2026-09-30")["total"]["orders"] == 1      # counted on the day of its first source
    assert analytics.funnel(conn, "2026-10-01", "2026-10-31")["total"]["orders"] == 0


def test_the_panels_old_number_in_a_line_of_the_bot_leads_to_the_merged_order(tmp_path):
    from test_oth_sale import new_order
    conn = db(tmp_path)
    a = new_order(conn, qty=1, goods="3")                                        # known by the panel's number
    b = new_order(conn, qty=1, goods="4")
    conn.execute("UPDATE order_card SET webmim_number = '777' WHERE id = ?", (b,))
    conn.commit()
    new = _merge(conn, [a, b], code="new")
    conn.execute("UPDATE order_card SET webmim_number = '888' WHERE id = ?", (a,))   # it got a WebMIM number later: the line has the panel's
    conn.commit()
    assert mgr.find_named(conn, a) == [new]


def test_arrived_keeps_the_number_while_tyres_of_the_supplier_are_not_ordered(tmp_path):
    from test_oth_sale import new_order
    conn = db(tmp_path)
    a = card_at(conn, "moving", T)
    other = new_order(conn, qty=1, goods="3")
    new = _merge(conn, [a, other], code="placed")
    assert mgr._oth_arrived_body(conn, _card(conn, new)) is None                   # a part still to order: the template as it is
