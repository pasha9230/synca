"""The tag of the find follows the stock (the owner 2026-11-09): «Появилось» and «Есть аналоги» come off when the list is no longer covered
and come back when it is again; the task stays one per stay; a tag the manager took off by hand stays off."""
from __future__ import annotations

from test_order_wait import WANT, db, tire, waiting
from tires_sync import order_wait, orders

T1, T2, T3, T4 = "2026-10-15T12:00:00", "2026-10-15T13:00:00", "2026-10-15T14:00:00", "2026-10-15T15:00:00"
OTHER = "Шина другая"


def tags(conn, card_id):
    return orders.get_card(conn, card_id)["tags"]


def tasks(conn, card_id):
    return conn.execute("SELECT COUNT(*) FROM order_task WHERE order_id = ?", (card_id,)).fetchone()[0]


def gone():
    """Something else in stock: the unload came, the wanted tire is not in it."""
    return [tire(width=315, code="X9")]


def test_the_exact_tag_comes_off_when_the_tires_are_gone_and_back_when_they_come_and_the_task_is_not_doubled(tmp_path):
    conn = db(tmp_path)
    card_id = waiting(conn, WANT, tags="VIP")
    order_wait.check(conn, [tire()], T1)
    assert tags(conn, card_id) == ["VIP", "Появилось"] and tasks(conn, card_id) == 1
    order_wait.check(conn, gone(), T2)
    assert tags(conn, card_id) == ["VIP"]                                                    # the other tags stay
    order_wait.check(conn, [tire()], T3)
    assert tags(conn, card_id) == ["VIP", "Появилось"] and tasks(conn, card_id) == 1
    order_wait.check(conn, [tire()], T4)
    assert tags(conn, card_id) == ["VIP", "Появилось"] and tasks(conn, card_id) == 1


def test_the_analog_tag_comes_off_and_back_too(tmp_path):
    conn = db(tmp_path)
    card_id = waiting(conn, {**WANT, "any_brand": True})
    order_wait.check(conn, [tire(brand_key="KUMHO", code="K1")], T1)
    assert tags(conn, card_id) == ["Есть аналоги"]
    order_wait.check(conn, gone(), T2)
    assert tags(conn, card_id) == []
    order_wait.check(conn, [tire(brand_key="KUMHO", code="K1")], T3)
    assert tags(conn, card_id) == ["Есть аналоги"] and tasks(conn, card_id) == 1


def test_a_part_of_the_set_is_not_covered_the_tag_comes_off(tmp_path):
    conn = db(tmp_path)
    card_id = waiting(conn, WANT, {**WANT, "width": 225})
    order_wait.check(conn, [tire(), tire(width=225, code="TS2")], T1)
    assert tags(conn, card_id) == ["Появилось"]
    order_wait.check(conn, [tire()], T2)
    assert tags(conn, card_id) == []
    order_wait.check(conn, [tire(stock=3), tire(width=225, code="TS2")], T3)                   # 3 of 4: not covered either
    assert tags(conn, card_id) == []


def test_a_tag_the_manager_took_off_does_not_come_back_in_this_stay(tmp_path):
    conn = db(tmp_path)
    card_id = waiting(conn, WANT)
    order_wait.check(conn, [tire()], T1)
    orders.update_card(conn, card_id, {"tags": ""}, T1, "worker2")
    order_wait.check(conn, gone(), T2)
    order_wait.check(conn, [tire()], T3)
    assert tags(conn, card_id) == []
    order_wait.check(conn, [tire()], T4)
    assert tags(conn, card_id) == [] and tasks(conn, card_id) == 1


def test_no_stock_at_all_is_a_failed_unload_and_the_tag_stays(tmp_path):
    conn = db(tmp_path)
    card_id = waiting(conn, WANT)
    order_wait.check(conn, [tire()], T1)
    order_wait.check(conn, [], T2)
    assert tags(conn, card_id) == ["Появилось"]


def test_the_check_takes_the_tag_off_in_the_journal_as_the_wait_list(tmp_path):
    conn = db(tmp_path)
    card_id = waiting(conn, WANT)
    order_wait.check(conn, [tire()], T1)
    order_wait.check(conn, gone(), T2)
    authors = [r[0] for r in conn.execute("SELECT author FROM order_event WHERE order_id = ? AND at = ?", (card_id, T2))]
    assert authors and set(authors) == {order_wait.AUTHOR}


def test_a_tag_that_could_not_come_back_is_tried_again_at_the_next_check(tmp_path):
    conn = db(tmp_path)
    card_id = waiting(conn, WANT)
    order_wait.check(conn, [tire()], T1)
    order_wait.check(conn, gone(), T2)
    full = ["т%d" % i for i in range(10)]
    orders.update_card(conn, card_id, {"tags": ",".join(full)}, T2, "worker2")                 # ten tags: no room
    order_wait.check(conn, [tire()], T3)
    assert tags(conn, card_id) == full
    orders.update_card(conn, card_id, {"tags": ""}, T3, "worker2")
    order_wait.check(conn, [tire()], T4)
    assert tags(conn, card_id) == ["Появилось"]
