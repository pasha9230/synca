"""The owner 2026-11-11: a record of storage (oth_storage) goes whole only — «если на ОТХ 4, то и продадим 4 или вернём 4». One tyre of it
marked «На возврат» (order_oth.item_to_return) and the whole record leaves the stock of OTH (oth_stock: the feed, «Взять с ОТХ»); the point's
«Продать» (order_oth.sell) sells it whole only. The mark itself is written as it is (the pieces asked); the orders put on «На ОТХ» the old way
(no record) work as before."""
from __future__ import annotations

import pytest

from test_oth_sale import code, new_order, take
from test_oth_storage import LATER, split
from test_order_reserve import card_at, db
from test_oth_feed import every_point_is_named  # noqa: F401 - a fixture
from tires_sync import order_oth, orders, oth_sale, oth_stock, oth_storage

OTH_SPLIT = True
MARKED = "2026-10-21T10:00:00"
SOLD = "2026-10-22T10:00:00"


def item_of(conn, cid):
    return conn.execute("SELECT id FROM order_item WHERE order_id = ?", (cid,)).fetchone()[0]


def marked_record(conn, pieces="1"):
    """A record of 4 tyres, `pieces` of them marked «На возврат»."""
    cid, x = split(conn)
    assert order_oth.item_to_return(conn, item_of(conn, x), MARKED, "o", pieces)
    return cid, x


def test_a_record_with_one_tyre_marked_leaves_the_stock_of_oth_whole(tmp_path):
    conn = db(tmp_path)
    cid, x = marked_record(conn)
    assert conn.execute("SELECT qty FROM oth_return WHERE order_id = ?", (x,)).fetchone() == (1,)    # the mark as it was asked
    assert oth_stock.pieces(conn) == {} and oth_stock.by_point(conn) == {} and oth_stock.places(conn) == {}
    assert oth_stock.returning_records(conn) == {x} and oth_storage.returning(conn, x)


def test_a_record_with_a_mark_is_not_offered_and_not_taken(tmp_path):
    conn = db(tmp_path)
    cid, x = marked_record(conn)
    b = new_order(conn, qty=4)
    assert oth_sale.offers(conn, orders.get_card(conn, b)) == {}
    line = orders.get_card(conn, b)["items"][0]["id"]
    with pytest.raises(oth_sale.OthSaleError, match="Запись на возврат поставщику целиком: брать с ОТХ нельзя"):
        oth_sale.take(conn, b, line, item_of(conn, x), LATER, "o")
    assert oth_storage.why_not_whole(conn, orders.get_card(conn, b), x) == oth_storage.RETURN_WHOLE
    assert [i["qty"] for i in orders.get_card(conn, x)["items"]] == [4] and code(conn, x) == "oth" and not oth_sale.taken_ids(conn, b)
    with pytest.raises(oth_sale.OthSaleError, match="целиком"):
        oth_sale.to_new_client(conn, x, "Пётр", "+7 900 000 11 22", LATER, "o")


def test_the_point_sells_the_marked_record_whole(tmp_path):
    conn = db(tmp_path)
    cid, x = marked_record(conn)
    outcome, answer, done = order_oth.sell(conn, orders.get_card(conn, x), "менеджер Бета", SOLD)
    assert done, answer
    assert code(conn, x) == "done" and [i["qty"] for i in orders.get_card(conn, x)["items"]] == [4]
    assert conn.execute("SELECT qty FROM oth_return WHERE order_id = ?", (x,)).fetchone() is None    # the mark went with the sale


def test_the_point_never_sells_a_part_of_a_marked_record(tmp_path):
    """2 of the 4 tyres of the record are left in it (taken the old way before 2026-11-11, or the quantity lowered): «Продать» is refused."""
    conn = db(tmp_path)
    cid, x = marked_record(conn)
    conn.execute("UPDATE order_item SET qty = 2 WHERE order_id = ?", (x,))
    conn.commit()
    outcome, answer, done = order_oth.sell(conn, orders.get_card(conn, x), "менеджер Бета", SOLD)
    assert not done and "только целиком" in answer and "2 из 4 шт." in answer
    assert code(conn, x) == "oth"


def test_a_record_without_a_mark_is_as_before(tmp_path):
    conn = db(tmp_path)
    cid, x = split(conn)
    assert oth_stock.pieces(conn) == {"1": 4} and oth_stock.returning_records(conn) == set()
    b = new_order(conn, qty=4)
    assert take(conn, b) == 4 and code(conn, x) == "cancel"


def test_a_part_kept_after_a_take_is_sold_as_before_without_a_mark(tmp_path):
    conn = db(tmp_path)
    cid, x = split(conn)
    conn.execute("UPDATE order_item SET qty = 2 WHERE order_id = ?", (x,))
    conn.commit()
    outcome, answer, done = order_oth.sell(conn, orders.get_card(conn, x), "менеджер Бета", SOLD)
    assert done, answer


def test_an_order_on_oth_the_old_way_with_a_part_marked_is_as_before(tmp_path, monkeypatch):
    """No record of storage (put on «На ОТХ» before 2026-11-10): the free pieces stay in the stock and are taken, the marked ones are not."""
    monkeypatch.setattr(oth_storage, "SPLIT", False)
    conn = db(tmp_path)
    old = card_at(conn, "oth", "2026-10-29T10:00:00")
    assert not oth_storage.is_record(conn, old)
    assert order_oth.item_to_return(conn, item_of(conn, old), "2026-10-29T10:30:00", "o", "1")
    assert oth_stock.pieces(conn) == {"1": 3} and oth_stock.returning_records(conn) == set()
    b = new_order(conn, qty=3)
    assert take(conn, b) == 3
    assert [i["qty"] for i in orders.get_card(conn, old)["items"]] == [1] and code(conn, old) == "oth"


def test_the_feed_drops_the_marked_record_whole_and_keeps_an_unmarked_one(tmp_path, capsys):
    """The cycle (sync.build_ads of every account): the record on «На ОТХ» gives «Шины в наличии на Бета» and its 4 pieces; one tyre of it
    marked «На возврат» and the ad is the supplier's only — no line, the supplier's count."""
    from test_oth_feed import AT_BETA, LINE, connect, description, on_oth, passed, shown
    from test_stock_pass import FIVE, prepared, public, run
    prepared(tmp_path, accounts=("A",))
    assert run(tmp_path, FIVE) == 0
    supplier = shown(description(public(tmp_path, 1), "101"))
    cid = on_oth(tmp_path, "101")
    conn = connect(tmp_path)
    x = oth_storage.record_of(conn, cid)
    assert x is not None and oth_stock.pieces(conn) == {"101": 4}
    conn.close()
    capsys.readouterr()
    assert run(tmp_path, FIVE) == 0
    passed(capsys)
    text = description(public(tmp_path, 1), "101")
    assert text.startswith(AT_BETA + "\n")
    conn = connect(tmp_path)
    assert order_oth.item_to_return(conn, item_of(conn, x), "2026-10-29T11:00:00", "o", "1")
    assert oth_stock.pieces(conn) == {}
    conn.close()
    capsys.readouterr()
    assert run(tmp_path, FIVE) == 0
    passed(capsys)
    text = description(public(tmp_path, 1), "101")
    assert not text.startswith(LINE) and shown(text) == supplier
