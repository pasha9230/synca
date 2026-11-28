"""The owner 2026-11-27: «Заказ должен был приехать — отметьте» to the point (10:00–19:00, twice a day at most, 3 hours apart, not while its
notifications are off; «Сводки» always), the office's task for an order 3 days and more past its delivery day (one, closes itself), and
«Срок хранения ОТХ истекает завтра» on «На ОТХ» and in «Задачи» (arrival_remind, oth_expiry)."""
from __future__ import annotations

import pytest

from panel_helpers import post
from test_point_notify import clock, env, staged  # noqa: F401 - the fixture
from tires_sync import arrival_remind as ar, oth_expiry, order_oth, order_reserve, orders, point_notify as pn

OTH_SPLIT = True


def late(conn, a, number="5300001", delivery="2026-11-25"):
    return staged(conn, a, "moving", number, delivery=delivery)


def open_tasks(conn, cid):
    return [r[0] for r in conn.execute("SELECT title FROM callback_task WHERE order_id = ? AND done_at IS NULL AND cancelled_at IS NULL", (cid,))]


# ---- 1. the point's reminder -------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("now, shown", [("2026-11-26T09:59:00", False), ("2026-11-26T10:00:00", True), ("2026-11-26T18:59:00", True),
                                        ("2026-11-26T19:00:00", False), ("2026-11-26T22:00:00", False)])
def test_the_working_hours_10_to_19(tmp_path, now, shown):
    app, owner, tok, conn, a, b, mine = env(tmp_path)
    late(conn, a)
    assert (ar.current(conn, a, now) is not None) is shown


def test_twice_a_day_at_most_and_three_hours_apart_and_each_showing_is_recorded(tmp_path):
    app, owner, tok, conn, a, b, mine = env(tmp_path)
    late(conn, a)
    first = ar.current(conn, a, "2026-11-26T10:05:00")
    assert first["n"] == 1 and first["orders"] == ["5300001"]
    assert ar.current(conn, a, "2026-11-26T13:04:00")["key"] == first["key"]              # 2 h 59 min: the same showing, no ring
    second = ar.current(conn, a, "2026-11-26T13:05:00")
    assert second["n"] == 2 and second["key"] != first["key"]
    assert ar.current(conn, a, "2026-11-26T18:30:00")["key"] == second["key"]            # the third: not today
    assert [r["kind"] for r in pn.history(conn, a)] == ["arrival_remind", "arrival_remind"]
    assert "5300001" in pn.history(conn, a)[0]["text"]
    assert ar.current(conn, a, "2026-11-27T10:00:00")["n"] == 1                           # a new day: again
    assert ar.current(conn, b, "2026-11-26T11:00:00") is None                             # the other point has no late order


def test_without_record_nothing_is_shown_before_the_first_showing(tmp_path):
    app, owner, tok, conn, a, b, mine = env(tmp_path)
    late(conn, a)
    assert ar.current(conn, a, "2026-11-26T10:05:00", record=False) is None
    ar.current(conn, a, "2026-11-26T10:06:00")
    assert ar.current(conn, a, "2026-11-26T11:00:00", record=False)["n"] == 1


@pytest.mark.parametrize("placed, delivery, first_day", [
    ("2026-11-24T10:30:00", "2026-11-24", "2026-11-25"),     # before 10:45, the delivery day the same: the next day
    ("2026-11-24T13:00:00", "2026-11-25", "2026-11-26"),     # 11:00–15:50, delivered the next day by 20:00: the day after
    ("2026-11-24T18:30:00", "2026-11-25", "2026-11-26"),     # after 18:00, by 19:30 the next day: the day after, from 10:00
])
def test_the_owners_examples(tmp_path, placed, delivery, first_day):
    app, owner, tok, conn, a, b, mine = env(tmp_path)
    cid = staged(conn, a, "placed", "5300002", delivery=delivery, entered=placed)
    from datetime import date, timedelta
    day_before = (date.fromisoformat(first_day) - timedelta(days=1)).isoformat()
    assert ar.current(conn, a, day_before + "T18:59:00") is None
    shown = ar.current(conn, a, first_day + "T10:00:00")
    assert shown and cid in shown["ids"]


def test_thirty_hours_on_the_way_without_a_delivery_day(tmp_path):
    app, owner, tok, conn, a, b, mine = env(tmp_path)
    staged(conn, a, "moving", "5300003", entered="2026-11-25T08:00:00")
    assert ar.current(conn, a, "2026-11-26T13:59:00") is None                             # 29 h 59 min
    assert ar.current(conn, a, "2026-11-26T14:00:00")["orders"] == ["5300003"]


def test_an_order_marked_arrived_is_no_more_reminded(tmp_path):
    app, owner, tok, conn, a, b, mine = env(tmp_path)
    cid = late(conn, a)
    assert ar.current(conn, a, "2026-11-26T10:00:00")
    conn.execute("UPDATE order_card SET stage_id = (SELECT id FROM order_stage WHERE code = 'at_point') WHERE id = ?", (cid,))
    conn.commit()
    assert ar.current(conn, a, "2026-11-26T11:00:00") is None


def test_the_page_and_the_feed_of_the_point_its_own_only_and_off_with_the_notifications(tmp_path, clock):
    app, owner, tok, conn, a, b, mine = env(tmp_path)
    late(conn, a)
    other = late(conn, b, "5300009")
    page = mine.get("/my-point").get_data(as_text=True)
    assert 'data-arrival-remind="1"' in page and "5300001" in page and "5300009" not in page and "Перейти к заказам" in page
    assert 'href="/my-point?tab=moving" data-arrival-go' in page
    items = mine.get("/my-point/feed").get_json()["items"]
    assert [i["key"] for i in items if i["key"].startswith("arrival-")] == ["arrival-20261126-1"]
    assert other and len(pn.history(conn, a)) == 1 and not pn.history(conn, b)
    post(mine, "/my-point/notify", "tok", on="0", who="Иван", back="/my-point")
    assert "data-arrival-remind" not in mine.get("/my-point").get_data(as_text=True)
    assert not [i for i in mine.get("/my-point/feed").get_json()["items"] if i["key"].startswith("arrival-")]
    notices = mine.get("/my-point/notices").get_data(as_text=True)                         # «Сводки» list them always
    assert 'data-act-late="' in notices and "5300001" in notices


def test_a_background_tab_of_the_feed_makes_no_showing_a_tab_in_front_does(tmp_path, clock):
    app, owner, tok, conn, a, b, mine = env(tmp_path)
    late(conn, a)
    assert not [i for i in mine.get("/my-point/feed").get_json()["items"] if i["key"].startswith("arrival-")]
    assert not pn.history(conn, a, kind="arrival_remind")
    [item] = [i for i in mine.get("/my-point/feed?seen=1").get_json()["items"] if i["key"].startswith("arrival-")]
    assert "«Приехал»" in item["title"] and len(pn.history(conn, a, kind="arrival_remind")) == 1


def test_the_office_working_as_the_point_makes_no_showing(tmp_path, clock):
    app, owner, tok, conn, a, b, mine = env(tmp_path)
    late(conn, a)
    owner.get("/my-point?p=%d" % a)
    assert not pn.history(conn, a, kind="arrival_remind")


# ---- 2. the office's task -------------------------------------------------------------------------------------------------------------

def test_one_task_three_days_past_the_delivery_day_and_it_closes_itself(tmp_path):
    app, owner, tok, conn, a, b, mine = env(tmp_path)
    cid = late(conn, a, delivery="2026-11-23")
    young = late(conn, a, "5300004", delivery="2026-11-24")
    assert ar.late_tasks(conn, "2026-11-26T08:00:00") == 1
    [title] = open_tasks(conn, cid)
    assert "5300001" in title and "Альфа" in title and "23.11.2026" in title and not open_tasks(conn, young)
    assert ar.late_tasks(conn, "2026-11-26T09:00:00") == 0 and len(open_tasks(conn, cid)) == 1      # no second one
    tid = conn.execute("SELECT id FROM callback_task WHERE order_id = ?", (cid,)).fetchone()[0]
    conn.execute("UPDATE order_card SET stage_id = (SELECT id FROM order_stage WHERE code = 'at_point') WHERE id = ?", (cid,))
    conn.commit()
    ar.late_tasks(conn, "2026-11-26T10:00:00")
    row = conn.execute("SELECT done_at, done_by FROM callback_task WHERE id = ?", (tid,)).fetchone()
    assert row[0] and row[1] == "система" and not open_tasks(conn, cid)
    conn.execute("UPDATE order_card SET stage_id = (SELECT id FROM order_stage WHERE code = 'moving') WHERE id = ?", (cid,))
    conn.commit()
    assert ar.late_tasks(conn, "2026-11-27T10:00:00") == 1 and open_tasks(conn, young)     # the same date never again; the other one now
    assert not open_tasks(conn, cid)


def test_a_task_closed_by_hand_is_not_made_again(tmp_path):
    from tires_sync import tasks
    app, owner, tok, conn, a, b, mine = env(tmp_path)
    cid = late(conn, a, delivery="2026-11-20")
    ar.late_tasks(conn, "2026-11-26T08:00:00")
    tid = conn.execute("SELECT id FROM callback_task WHERE order_id = ?", (cid,)).fetchone()[0]
    tasks.close(conn, tid, "cancel", "2026-11-26T08:10:00", "admin")
    assert ar.late_tasks(conn, "2026-11-26T09:00:00") == 0 and not open_tasks(conn, cid)


def test_no_task_for_the_old_orders_of_before_the_first_tick_nor_older_than_30_days(tmp_path):
    app, owner, tok, conn, a, b, mine = env(tmp_path)
    old = late(conn, a, "5300010", delivery="2026-11-18")                   # before the first tick's day minus 7 (19.11)
    edge = late(conn, a, "5300011", delivery="2026-11-19")
    assert ar.late_tasks(conn, "2026-11-26T08:00:00") == 1 and open_tasks(conn, edge) and not open_tasks(conn, old)
    conn.execute("UPDATE app_state SET value = '2026-01-01' WHERE key = ?", (ar.SINCE_KEY,))
    conn.commit()
    ancient = late(conn, a, "5300012", delivery="2026-10-26")               # 31 days before 26.11
    month = late(conn, a, "5300013", delivery="2026-10-27")                 # 30 days
    ar.late_tasks(conn, "2026-11-26T09:00:00")
    assert open_tasks(conn, month) and not open_tasks(conn, ancient)


def test_a_new_delivery_day_closes_the_old_task(tmp_path):
    app, owner, tok, conn, a, b, mine = env(tmp_path)
    cid = late(conn, a, delivery="2026-11-22")
    ar.late_tasks(conn, "2026-11-26T08:00:00")
    [old] = open_tasks(conn, cid)
    conn.execute("UPDATE order_card SET delivery_date = '2026-11-23' WHERE id = ?", (cid,))
    conn.commit()
    assert ar.late_tasks(conn, "2026-11-26T09:00:00") == 1
    [new] = open_tasks(conn, cid)
    assert "23.11.2026" in new and old != new
    conn.execute("UPDATE order_card SET delivery_date = '2026-11-28' WHERE id = ?", (cid,))   # not late any more: no task
    conn.commit()
    assert ar.late_tasks(conn, "2026-11-26T10:00:00") == 0 and not open_tasks(conn, cid)


def test_a_points_own_sale_is_marked_once(tmp_path, monkeypatch):
    from tires_sync import order_points
    app, owner, tok, conn, a, b, mine = env(tmp_path)
    cid = late(conn, a, delivery="2026-11-20")
    calls = []
    monkeypatch.setattr(order_points, "own_sales", lambda c, p: calls.append(p) or {cid})
    ar.late_tasks(conn, "2026-11-26T08:00:00")
    ar.late_tasks(conn, "2026-11-26T09:00:00")
    assert calls == [a] and not open_tasks(conn, cid)


def test_the_tick_of_the_reservations_makes_the_tasks(tmp_path):
    app, owner, tok, conn, a, b, mine = env(tmp_path)
    cid = late(conn, a, delivery="2026-11-20")
    order_reserve.check(conn, "2026-11-26T08:00:00", lambda card, text: "ok")
    assert len(open_tasks(conn, cid)) == 1


# ---- 3. «Срок хранения ОТХ истекает завтра» ---------------------------------------------------------------------------------------------

def test_the_end_of_storage_tomorrow_a_mark_and_one_task_that_closes_itself(tmp_path, monkeypatch):
    from tires_sync.panel import routes_orders
    app, owner, tok, conn, a, b, mine = env(tmp_path)
    conn.execute("INSERT OR IGNORE INTO app_state (key, value) VALUES (?, ?)", (order_oth.SINCE_KEY, "2026-11-01T00:00:00"))
    ends = staged(conn, a, "oth", "5300005", entered="2026-11-21T11:00:00")       # the seventh day 27.11: tomorrow of 26.11
    later = staged(conn, a, "oth", "5300006", entered="2026-11-22T11:00:00")
    for cid in (ends, later):                                                      # the page lists the positions
        orders.add_item(conn, cid, goods_id="9", name="Шина K-%d" % cid, brand_key="nokian", season="winter", diameter=16, territory="1", qty=4,
                        sale_unit=5000, supplier_price=4000, terms=None, fee_pct=0, promo_pct=0, now="2026-11-21T11:00:00", author="o")
    assert oth_expiry.ends_tomorrow(conn, "2026-11-26T08:00:00") == {ends: "2026-11-27"}
    assert oth_expiry.ends_tomorrow(conn, "2026-11-25T08:00:00") == {}
    assert oth_expiry.tick(conn, "2026-11-26T08:00:00") == 1 and oth_expiry.tick(conn, "2026-11-26T15:00:00") == 0
    [title] = open_tasks(conn, ends)
    assert title.startswith(oth_expiry.TASK_TITLE) and "Альфа" in title and not open_tasks(conn, later)
    monkeypatch.setattr(routes_orders, "local_iso", lambda: "2026-11-26T09:00:00")
    page = owner.get("/oth").get_data(as_text=True)
    assert page.count("data-oth-ends") == 1 and "Срок хранения истекает завтра" in page
    note = conn.execute("SELECT note FROM callback_task WHERE order_id = ?", (ends,)).fetchone()[0]
    assert "«Согласование возврата»" in note and "«На возврат»" not in note       # the owner 2026-11-28: the agreement, not «На возврат»
    order_oth.ask_seventh_day(conn, "2026-11-27T12:30:00")                         # on «Согласование возврата» by itself
    assert orders.get_card(conn, ends)["stage"]["code"] == orders.RETURN_AGREE
    assert oth_expiry.tick(conn, "2026-11-27T12:31:00") == 1                       # the other one's day before
    assert not [t for t in open_tasks(conn, ends) if t.startswith(oth_expiry.TASK_TITLE)] and len(open_tasks(conn, later)) == 1
    assert "data-oth-ends" not in mine.get("/oth").get_data(as_text=True)          # the point never sees the page's mark
