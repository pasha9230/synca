"""Orders on «На ОТХ» from before the split into records of storage (oth_storage.SPLIT off): a sale from OTH is the one who made it (the owner 2026-11-20): the order that sells the tyres takes the seller as its
responsible, and «Аккаунт» of the Google table follows. A sale by a point keeps the responsible of the order that made it."""
from __future__ import annotations

from test_oth_client_swap import NEW_PHONE, _b, put_aside
from test_oth_sale import LATER, T, new_order, offer, line
from tests.test_order_reserve import card_at, db
from tires_sync import order_sheet, orders, oth_sale, oth_swap, users

SELLER = "manager_b"


def responsible(conn, cid):
    return conn.execute("SELECT responsible FROM order_card WHERE id = ?", (cid,)).fetchone()[0]


def told(conn, cid):
    return [e["text"] for e in orders.list_events(conn, cid) if e["text"].startswith("Продажа с ОТХ: ответственный")]


def test_taking_from_oth_makes_the_taker_the_responsible_and_the_table_s_account(tmp_path):
    conn = db(tmp_path)
    old = card_at(conn, "oth", T)
    conn.execute("UPDATE order_card SET responsible = 'manager_a' WHERE id = ?", (old,))
    b = new_order(conn)
    conn.execute("UPDATE order_card SET responsible = 'manager_a' WHERE id = ?", (b,))
    users.add_user(conn, SELLER, "secret-pass-123", T, role="partner")
    users.set_sheet_label(conn, SELLER, "Подпись менеджера Б", T, "o")
    conn.commit()
    assert oth_sale.take(conn, b, line(conn, b, "1")["id"], offer(conn, b, "1")["from_item"], LATER, SELLER) == 4
    assert responsible(conn, b) == SELLER and told(conn, b) == ["Продажа с ОТХ: ответственный «manager_a» → «manager_b» (продал с ОТХ)"]
    assert order_sheet.account_label(conn, orders.get_card(conn, b)) == "Подпись менеджера Б"


def test_the_responsible_who_took_them_himself_is_not_told_again(tmp_path):
    conn = db(tmp_path)
    card_at(conn, "oth", T)
    b = new_order(conn)
    conn.execute("UPDATE order_card SET responsible = ? WHERE id = ?", (SELLER, b))
    conn.commit()
    oth_sale.take(conn, b, line(conn, b, "1")["id"], offer(conn, b, "1")["from_item"], LATER, SELLER)
    assert responsible(conn, b) == SELLER and told(conn, b) == []


def test_a_new_client_of_all_the_tyres_makes_the_seller_the_responsible_of_the_order(tmp_path):
    conn = db(tmp_path)
    a = card_at(conn, "oth", T)
    conn.execute("UPDATE order_card SET responsible = 'manager_a' WHERE id = ?", (a,))
    conn.commit()
    put_aside(conn, a)
    assert oth_sale.to_new_client(conn, a, "Пётр", NEW_PHONE, LATER, SELLER) == a
    assert responsible(conn, a) == SELLER and told(conn, a)


def test_a_new_client_of_part_of_the_tyres_gets_a_new_order_of_the_seller(tmp_path):
    conn = db(tmp_path)
    old = card_at(conn, "oth", T)
    item = orders.get_card(conn, old)["items"][0]["id"]
    conn.execute("UPDATE order_card SET responsible = 'manager_a' WHERE id = ?", (old,))
    conn.execute("UPDATE order_item SET qty = 5 WHERE id = ?", (item,))
    conn.execute("INSERT INTO oth_return (item_id, order_id, qty, at, author) VALUES (?, ?, 1, ?, 'o')", (item, old, T))
    conn.commit()
    new = oth_sale.to_new_client(conn, old, "Пётр", NEW_PHONE, LATER, SELLER)
    assert new != old and responsible(conn, new) == SELLER and responsible(conn, old) == "manager_a"


def test_an_order_taking_all_the_tyres_with_its_client_makes_the_taker_the_responsible(tmp_path):
    conn = db(tmp_path)
    a = card_at(conn, "oth", T)
    conn.execute("UPDATE order_card SET responsible = 'manager_a' WHERE id = ?", (a,))
    conn.commit()
    put_aside(conn, a)
    b, _row = _b(conn)
    item = orders.get_card(conn, b)["items"][0]
    assert oth_swap.take_client(conn, b, item["id"], orders.get_card(conn, a)["items"][0]["id"], LATER, SELLER) == a
    assert responsible(conn, a) == SELLER and told(conn, a)

