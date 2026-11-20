"""A sale from OTH is the one who made it (the owner 2026-11-20): the order that sells the tyres takes the seller as its
responsible, and «Аккаунт» of the Google table follows. A sale by a point keeps the responsible of the order that made it. As production
runs: a move to «На ОТХ» splits the tyres into a record of storage (oth_storage); the orders of before: test_oth_seller_legacy."""
from __future__ import annotations

from test_oth_point_sale import sale_rule, sell
from test_oth_sale import LATER, new_order, offer, line
from test_oth_seller_legacy import SELLER, responsible, told
from test_oth_storage import split
from test_order_reserve import db
from tires_sync import order_sheet, orders, oth_sale, users

OTH_SPLIT = True


def _record(conn):
    cid, x = split(conn)
    conn.execute("UPDATE order_card SET responsible = 'manager_a' WHERE id IN (?, ?)", (cid, x))
    conn.commit()
    return cid, x


def test_taking_a_record_from_oth_makes_the_taker_the_responsible_and_the_table_s_account(tmp_path):
    conn = db(tmp_path)
    _record(conn)
    b = new_order(conn)
    conn.execute("UPDATE order_card SET responsible = 'manager_a' WHERE id = ?", (b,))
    users.add_user(conn, SELLER, "secret-pass-123", LATER, role="partner")
    users.set_sheet_label(conn, SELLER, "Подпись менеджера Б", LATER, "o")
    conn.commit()
    assert oth_sale.take(conn, b, line(conn, b, "1")["id"], offer(conn, b, "1")["from_item"], LATER, SELLER) == 4
    assert responsible(conn, b) == SELLER and told(conn, b) == ["Продажа с ОТХ: ответственный «manager_a» → «manager_b» (продал с ОТХ)"]
    assert order_sheet.account_label(conn, orders.get_card(conn, b)) == "Подпись менеджера Б"


def test_a_new_client_of_a_record_gets_a_new_order_of_the_seller(tmp_path):
    conn = db(tmp_path)
    _cid, x = _record(conn)
    new = oth_sale.to_new_client(conn, x, "Пётр", "+7 900 555 66 77", LATER, SELLER)
    assert new != x and responsible(conn, new) == SELLER


def test_a_sale_by_a_point_keeps_the_responsible_of_the_order_that_made_it(tmp_path):
    conn = db(tmp_path)
    sale_rule(conn)
    _cid, x = _record(conn)
    b = sell(conn, x)
    assert responsible(conn, b) == "manager_a" and told(conn, b) == []
