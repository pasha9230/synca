"""«На ОТХ» has no time to sell (the owner 2026-10-30: whether the goods go back to the supplier is the owner's call): entering the stage
clears the date of the reservation, no alert ever comes for an order there, a date left by the seven days before is never read. Before
2026-10-30 the stage gave seven days with alerts about their end."""
from __future__ import annotations

from tests.test_order_reserve import card_at, db, stage
from tires_sync import order_reserve as reserve, orders


def test_entering_oth_sets_no_term_and_clears_the_reserve_date(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    orders.update_card(conn, cid, {"reserve_until": "2026-10-25"}, "2026-10-16T10:00:00", "o")        # extended earlier, says nothing about the ОТХ stay
    orders.move_card(conn, cid, stage(conn, "oth"), "2026-10-17T10:00:00", "o")
    assert orders.get_card(conn, cid)["reserve_until"] == ""
    texts = [e["text"] for e in orders.list_events(conn, cid)]
    assert orders.OTH_ENTERED in texts and not any("срок продажи до" in t for t in texts)
    assert not hasattr(orders, "OTH_DAYS")


def test_no_alert_ever_comes_for_an_order_on_oth(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "oth", "2026-10-17T10:00:00")
    sent = []
    notify = lambda card, text: (sent.append(text), "отправлено")[1]
    for day in ("2026-10-18", "2026-10-23", "2026-10-24", "2026-11-30"):
        assert reserve.check(conn, day + "T12:00:00", notify) == 0
    assert sent == [] and not any(e["kind"] == "reserve" for e in orders.list_events(conn, cid))


def test_a_date_left_by_the_seven_days_is_never_read(tmp_path):
    """An order that entered «На ОТХ» before 2026-10-30 keeps its date and the record «срок продажи до …»: no alert, nothing late on «Моя точка»."""
    conn = db(tmp_path)
    cid = card_at(conn, "oth", "2026-10-17T10:00:00")
    conn.execute("UPDATE order_card SET reserve_until = '2026-10-24' WHERE id = ?", (cid,))
    orders.add_event(conn, cid, "2026-10-17T10:00:00", "o", "field", "На ОТХ: срок продажи до 2026-10-24")
    conn.commit()
    for day in ("2026-10-23", "2026-10-24", "2026-10-26"):
        assert reserve.check(conn, day + "T12:00:00", lambda c, t: "ok") == 0


def test_the_reserve_alerts_of_the_point_still_come_and_going_back_from_oth_starts_a_new_reservation(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")                             # reserve until 18.10
    assert reserve.check(conn, "2026-10-18T12:00:00", lambda c, t: "ok") == 1
    orders.move_card(conn, cid, stage(conn, "oth"), "2026-10-18T13:00:00", "o")
    assert reserve.check(conn, "2026-10-25T12:00:00", lambda c, t: "ok") == 0
    orders.move_card(conn, cid, stage(conn, "at_point"), "2026-10-26T10:00:00", "o")    # the client came back
    assert orders.get_card(conn, cid)["reserve_until"] == "2026-10-29"


def test_the_point_sees_no_term_for_an_order_on_oth_even_with_an_old_date(tmp_path):
    from test_mypoint import manager
    from test_vk_commands import NOW, _card, _stage, ready
    app, client, tok, conn, pid, card = ready(tmp_path)
    oth = _card(conn, pid, webmim="700501", code="at_point")
    orders.move_card(conn, oth, _stage(conn, "oth"), NOW, "o")
    conn.execute("UPDATE order_card SET reserve_until = '2026-01-01' WHERE id = ?", (oth,))            # a date of the seven days, long past
    conn.commit()
    page = manager(app, conn, pid).get("/my-point", query_string={"tab": "return"}).get_data(as_text=True)
    assert "700501" in page and "Хранение до 01.01" not in page and "срок прошёл" not in page


def test_the_templates_give_no_reservation_date_for_an_order_on_oth(tmp_path):
    from tires_sync import order_templates
    conn = db(tmp_path)
    cid = card_at(conn, "oth", "2026-10-17T10:00:00")
    conn.execute("UPDATE order_card SET reserve_until = '2026-10-24' WHERE id = ?", (cid,))
    conn.commit()
    assert order_templates.values(conn, orders.get_card(conn, cid), "2026-10-30T10:00:00")["hold_until"] == ""
