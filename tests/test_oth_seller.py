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


def test_the_point_s_sale_says_in_the_journal_that_the_responsible_stays(tmp_path):
    conn = db(tmp_path)
    sale_rule(conn)
    _cid, x = _record(conn)
    b = sell(conn, x)
    assert "Продажа с ОТХ точкой: ответственный не меняется (остаётся «manager_a»)" in [e["text"] for e in orders.list_events(conn, b)]


def test_the_owner_selling_in_the_point_s_window_answers_for_the_sale(tmp_path):
    """The owner 2026-11-20: the admin pressing «Продать» in the panel of the point (as the point) is the seller; the point never is."""
    from test_oth_point_sale import BUYER, PHONE, SOLD, item
    from tires_sync import oth_point_sale
    conn = db(tmp_path)
    sale_rule(conn)
    _cid, x = _record(conn)
    b = oth_point_sale.sell(conn, x, {item(conn, x): 4}, BUYER, PHONE, None, None, False, SOLD, "Владелец за точку «Бета»", seller="admin_a")
    assert responsible(conn, b) == "admin_a" and told(conn, b)


def test_the_panel_of_the_point_names_the_owner_acting_as_the_point_and_never_the_point(tmp_path):
    import re
    from panel_helpers import post
    from test_mypoint import manager
    from test_oth_point_sale import BUYER, PHONE, item, window_token
    from test_oth_sale import panel
    for acting in (True, False):
        (tmp_path / str(acting)).mkdir()
        app, owner, tok, conn = panel(tmp_path / str(acting))
        sale_rule(conn)
        _cid, x = _record(conn)
        pid = orders.get_card(conn, x)["point_id"]
        if acting:
            client = owner
            with client.session_transaction() as session:
                session["as_point"] = pid
        else:
            client = manager(app, conn, pid)
        r = post(client, "/my-point/%d/oth-sale" % x, "tok", token=window_token(client, x), **{"name": BUYER, "phone": PHONE, "qty_%d" % item(conn, x): "4"})
        assert r.status_code == 302, acting
        b = max(r[0] for r in conn.execute("SELECT id FROM order_card"))
        assert responsible(conn, b) == (app.config["AUTHOR"] if acting else "manager_a"), acting
