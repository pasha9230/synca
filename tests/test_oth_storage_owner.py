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
