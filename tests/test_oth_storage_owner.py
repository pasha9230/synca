"""The owner's answers of 2026-11-10 on the record of storage (oth_storage): a record is never a source of a merge (2); a record cancelled
by hand by mistake goes back on «На ОТХ» (3); any move of a record to «Выполнено» is the point's «Продать» (order_oth.sell) (4); and the low
points of the review: the panel's routes of the record, a second split after a give-back, one sale per «Продать»."""
from __future__ import annotations

import pytest

from test_oth_sale import new_order, take
from test_oth_storage import LATER, MOVED, at_point, split
from test_order_reserve import db, stage
from tires_sync import order_merge, orders, oth_stock, oth_storage

OTH_SPLIT = True
LATE = "2026-10-22T10:00:00"


def code(conn, cid):
    return orders.get_card(conn, cid)["stage"]["code"]


# ---- 2: the merge ---------------------------------------------------------------------------------------------------------------------

def test_a_record_is_never_a_source_of_a_merge(tmp_path):
    conn = db(tmp_path)
    cid, x = split(conn)
    other = new_order(conn, qty=4)
    with pytest.raises(order_merge.MergeError, match="Шины с ОТХ берутся кнопкой «Взять с ОТХ»"):
        order_merge.merge(conn, [x, other], stage(conn, "at_point"), LATER, "o", client_from=other)
    assert code(conn, x) == "oth" and order_merge.into(conn, x) is None and oth_stock.pieces(conn) == {"1": 4}


# ---- 3: a record cancelled by hand by mistake goes back on «На ОТХ» ---------------------------------------------------------------------

def test_a_record_cancelled_by_hand_by_mistake_goes_back_on_oth_with_the_same_tyres(tmp_path):
    conn = db(tmp_path)
    cid, x = split(conn)
    assert orders.move_card(conn, x, stage(conn, "cancel"), LATER, "o", changes={"cancel_reason": "другое: ошиблись"})
    assert oth_stock.pieces(conn) == {}
    assert orders.move_card(conn, x, stage(conn, "oth"), LATE, "o")
    assert code(conn, x) == "oth" and oth_storage.record(conn, x)["state"] == oth_storage.HELD
    assert oth_stock.pieces(conn) == {"1": 4} and oth_storage.record_ids(conn) == {x}               # no second record, no doubles
    assert code(conn, cid) == "cancel" and oth_storage.why_not_back(conn, cid) is None             # the client may still be called back
    assert orders.get_card(conn, x)["cancel_reason"] == ""
    assert any("отмена была ошибкой" in e["text"] for e in orders.list_events(conn, x))
    with pytest.raises(orders.OrderError, match="записи хранения №%d" % x):
        orders.move_card(conn, cid, stage(conn, "oth"), LATE, "o")                                  # Б1 still: the cancelled order never
    b = new_order(conn, qty=4)
    assert take(conn, b, now=LATE) == 4 and code(conn, x) == "cancel" and oth_stock.pieces(conn) == {}


def test_a_record_cancelled_by_mistake_does_not_go_to_return_given(tmp_path):
    conn = db(tmp_path)
    cid, x = split(conn)
    orders.move_card(conn, x, stage(conn, "cancel"), LATER, "o", changes={"cancel_reason": "другое: ошиблись"})
    with pytest.raises(orders.OrderError, match="Запись хранения №%d закрыта" % x):
        orders.move_card(conn, x, stage(conn, orders.RETURN_GIVEN), LATE, "o")


def _given_back(conn):
    cid, x = split(conn)
    assert orders.move_card(conn, cid, stage(conn, "at_point"), LATER, "o")
    return x


def _taken(conn):
    cid, x = split(conn)
    take(conn, new_order(conn, qty=4), now=LATER)
    return x


@pytest.mark.parametrize("closed", [_given_back, _taken])
def test_a_record_closed_by_the_take_or_the_give_back_never_goes_back(tmp_path, closed):
    conn = db(tmp_path)
    x = closed(conn)
    assert code(conn, x) == "cancel" and not oth_storage.cancelled_by_mistake(conn, orders.get_card(conn, x))
    with pytest.raises(orders.OrderError, match="Запись хранения №%d закрыта" % x):
        orders.move_card(conn, x, stage(conn, "oth"), LATE, "o")
    assert code(conn, x) == "cancel" and oth_stock.pieces(conn) == {}
