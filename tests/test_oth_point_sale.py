"""«Продать» of the point on «На ОТХ» with the window of the sale (the ТЗ «Невыкуп, ОТХ, возвраты», part D; oth_point_sale): a record of
storage sells some or all of its tyres to a buyer: a new order of the panel on «Выполнено» with the client of the phone (or none, «Клиент
отказался…»), the numbers, the pieces sold and the record's price; the rest stays on «На ОТХ» with its dates; the client's order stays in
«Отмена» with « О»; the Google table gets no row of the new order; the stock of OTH in the feed goes down by the pieces sold only."""
from __future__ import annotations

import re

import pytest
from cryptography.fernet import Fernet

from panel_helpers import post
from test_mypoint import manager
from test_order_reserve import db, stage
from test_order_sheet import turn_on
from test_oth_sale import panel
from test_point_new_view import flag
from test_oth_storage import MOVED, TRANSFER, at_point, number, split
from tires_sync import order_oth, order_rules as rules, order_sheet, orders, oth_point_sale as sale, oth_stock, oth_storage
from tires_sync.secretbox import SecretBox

OTH_SPLIT = True
SOLD = "2026-10-22T15:40:00"
BUYER, PHONE = "Глеб", "+7 900 333 44 55"


def sale_rule(conn):
    rules.add_rule(conn, stage(conn, "done"), "enter", 0, "record_sale", None, "", "auto", None)
    conn.commit()


def item(conn, x):
    return orders.get_card(conn, x)["items"][0]["id"]


def sell(conn, x, qty=4, name=BUYER, phone=PHONE, anonymous=False, number_=None, transfer=None):
    return sale.sell(conn, x, {item(conn, x): qty}, name, phone, number_, transfer, anonymous, SOLD, "менеджер Бета")


def sales(conn, cid):
    return conn.execute("SELECT pieces, sale_total, cost_total FROM sale WHERE note = ? AND reversed_at IS NULL", ("Заказ №%d" % cid,)).fetchall()


def client_of(conn, cid):
    row = conn.execute("SELECT c.name FROM client_order l JOIN client c ON c.id = l.client_id WHERE l.order_id = ?", (cid,)).fetchone()
    return row[0] if row else None


# ---- the new order and its client ------------------------------------------------------------------------------------------------------

def test_the_sale_makes_a_client_and_an_order_on_done_with_the_numbers_the_tyres_and_the_price(tmp_path):
    conn = db(tmp_path)
    sale_rule(conn)
    cid, x = split(conn)
    b = sell(conn, x)
    card = orders.get_card(conn, b)
    assert card["stage"]["code"] == "done" and (card["client_name"], card["client_phone"]) == (BUYER, PHONE)
    assert client_of(conn, b) == BUYER                                                    # the card of the client in the CRM
    assert (card["webmim_number"], card["transfer_number"]) == (number(cid), TRANSFER)    # the record's, without « О»
    assert [(i["goods_id"], i["qty"], i["sale_unit"], i["supplier_price"]) for i in card["items"]] == [("1", 4, 5000, 4000)]
    assert sales(conn, b) == [(4, 20000, 16000)]                                          # the sale of «Выполнено», once
    assert orders.get_card(conn, x)["stage"]["code"] == "cancel" and oth_stock.pieces(conn) == {}      # emptied: closed
    assert oth_storage.record(conn, x)["state"] == oth_storage.TAKEN and oth_storage.record(conn, x)["to"] == b
    assert sale.sold_of(conn, cid) == [{"order": b, "record": x, "pieces": 4, "at": SOLD, "by": "менеджер Бета"}]


def test_an_existing_client_of_the_phone_is_taken_not_made_again(tmp_path):
    conn = db(tmp_path)
    cid, x = split(conn)
    first = sell(conn, x, qty=1)
    count = conn.execute("SELECT COUNT(*) FROM client").fetchone()[0]
    second = sell(conn, x, qty=1, name="Глеб Петрович")
    assert conn.execute("SELECT COUNT(*) FROM client").fetchone()[0] == count
    assert client_of(conn, first) == client_of(conn, second) == BUYER


def test_typed_numbers_are_the_orders(tmp_path):
    conn = db(tmp_path)
    cid, x = split(conn)
    b = sell(conn, x, number_="5999001", transfer="26R-0009999")
    assert (orders.get_card(conn, b)["webmim_number"], orders.get_card(conn, b)["transfer_number"]) == ("5999001", "26R-0009999")


# ---- «Клиент отказался предоставлять имя и телефон» ----------------------------------------------------------------------------------

def test_without_the_box_the_name_and_the_phone_are_needed_and_the_phone_is_checked(tmp_path):
    conn = db(tmp_path)
    cid, x = split(conn)
    with pytest.raises(sale.SaleError, match="имя покупателя"):
        sell(conn, x, name="")
    with pytest.raises(sale.SaleError, match="телефон покупателя"):
        sell(conn, x, phone="")
    with pytest.raises(orders.OrderError, match="от 10 до 15 цифр"):
        sell(conn, x, phone="12345")
    assert orders.get_card(conn, x)["stage"]["code"] == "oth" and oth_stock.pieces(conn) == {"1": 4}


def test_with_the_box_no_name_no_phone_and_no_client_card(tmp_path):
    conn = db(tmp_path)
    cid, x = split(conn)
    clients = conn.execute("SELECT COUNT(*) FROM client").fetchone()[0]
    b = sell(conn, x, name="Глеб", phone=PHONE, anonymous=True)                          # typed and then refused: not kept
    card = orders.get_card(conn, b)
    assert (card["client_name"], card["client_phone"]) == ("", "") and client_of(conn, b) is None
    assert conn.execute("SELECT COUNT(*) FROM client").fetchone()[0] == clients and card["stage"]["code"] == "done"


# ---- a part: the rest stays on «На ОТХ», its term does not move ---------------------------------------------------------------------

def test_a_part_leaves_the_rest_on_oth_with_the_same_dates(tmp_path):
    conn = db(tmp_path)
    sale_rule(conn)
    cid, x = split(conn)
    before = orders.get_card(conn, x)
    dates, since = order_oth.reserve_dates(conn)[x], order_oth._oth_since(conn, x)
    b = sell(conn, x, qty=1)
    rest = orders.get_card(conn, x)
    assert rest["stage"]["code"] == "oth" and rest["entered_at"] == before["entered_at"] == MOVED
    assert [i["qty"] for i in rest["items"]] == [3] and [i["qty"] for i in orders.get_card(conn, b)["items"]] == [1]
    assert order_oth.reserve_dates(conn)[x] == dates and order_oth._oth_since(conn, x) == since     # the seven days count as before
    assert oth_storage.record(conn, x)["state"] == oth_storage.HELD and sales(conn, b) == [(1, 5000, 4000)]
    assert sales(conn, x) == []
    with pytest.raises(sale.SaleError, match="от 1 до 3 шт"):
        sell(conn, x, qty=4)
    b2 = sell(conn, x, qty=3)
    assert orders.get_card(conn, x)["stage"]["code"] == "cancel" and b2 != b


def test_the_window_needs_at_least_one_piece(tmp_path):
    conn = db(tmp_path)
    cid, x = split(conn)
    with pytest.raises(sale.SaleError, match="хотя бы 1 шт"):
        sell(conn, x, qty=0)


# ---- the price: the record's, or the one corrected in it -----------------------------------------------------------------------------

def test_the_price_is_the_orders_own_when_nobody_changed_it(tmp_path):
    conn = db(tmp_path)
    cid, x = split(conn)
    b = sell(conn, x, qty=2)
    assert [i["sale_unit"] for i in orders.get_card(conn, b)["items"]] == [5000] == [i["sale_unit"] for i in orders.get_card(conn, cid)["items"]]


def test_the_price_corrected_in_the_order_is_the_sales(tmp_path):
    conn = db(tmp_path)
    cid, x = split(conn)
    orders.update_item(conn, item(conn, x), 4, 4700, MOVED, "o")                         # «Цена продажи» corrected in the order
    b = sell(conn, x, qty=2)
    assert [i["sale_unit"] for i in orders.get_card(conn, b)["items"]] == [4700]
    assert sale.lines(conn, orders.get_card(conn, x))[0]["unit"] == 4700


# ---- the client's order: «Отмена», « О» ------------------------------------------------------------------------------------------------

def test_the_clients_order_stays_cancelled_with_the_letter(tmp_path):
    conn = db(tmp_path)
    cid, x = split(conn)
    sell(conn, x, qty=2)
    a = orders.get_card(conn, cid)
    assert a["stage"]["code"] == "cancel" and a["client_name"] == "Иван"
    assert (a["webmim_number"], a["transfer_number"]) == (number(cid) + " О", TRANSFER + " О")


# ---- the Google table: no new row, the client's order says «Выполнено» / «О» -------------------------------------------------------------

def test_the_table_gets_no_row_of_the_sale_and_the_clients_row_says_done_and_o(tmp_path):
    conn = db(tmp_path)
    turn_on(conn, SecretBox(Fernet.generate_key()))
    cid, x = split(conn)
    order_sheet.enroll(conn, cid, MOVED)                                                  # A was placed while the table was on
    conn.commit()
    queued = {r[0] for r in conn.execute("SELECT order_id FROM sheet_order")}
    b = sell(conn, x, qty=1)
    assert {r[0] for r in conn.execute("SELECT order_id FROM sheet_order")} == queued and b not in queued
    rows = order_sheet.rows_of(conn, cid, MOVED)
    assert rows and all(r["Этап"] == orders.stage_name(conn, "done") and r["Реализация"] == order_sheet.REFUSED for r in rows.values())


def test_a_cancelled_order_with_nothing_sold_keeps_its_row_as_before(tmp_path):
    conn = db(tmp_path)
    cid, x = split(conn)
    rows = order_sheet.rows_of(conn, cid, MOVED)
    assert all(r["Этап"] == orders.stage_name(conn, "cancel") and r["Реализация"] == "" for r in rows.values())


# ---- the feed: the stock of OTH goes down by the pieces sold and nothing else --------------------------------------------------------

def test_the_stock_of_oth_in_the_feed_goes_down_exactly_by_the_pieces_sold(tmp_path):
    conn = db(tmp_path)
    cid, x = split(conn)
    before = (oth_stock.pieces(conn), oth_stock.pieces(conn, manual=False), oth_stock.by_point(conn), oth_stock.places(conn))
    assert before[0] == {"1": 4}
    sell(conn, x, qty=1)
    after = (oth_stock.pieces(conn), oth_stock.pieces(conn, manual=False), oth_stock.by_point(conn), oth_stock.places(conn))
    assert after[0] == {"1": 3} and after[1] == {"1": 3}
    assert after[2] == {k: v - 1 for k, v in before[2].items()} and after[3] == before[3]     # the same point, the same names
    assert oth_storage.record_ids(conn) == {x}                                                  # no second record


# ---- what the window refuses -----------------------------------------------------------------------------------------------------------

def test_an_order_put_on_oth_the_old_way_is_not_sold_by_the_window(tmp_path, monkeypatch):
    conn = db(tmp_path)
    monkeypatch.setattr(oth_storage, "SPLIT", False)
    cid = at_point(conn)
    orders.move_card(conn, cid, stage(conn, "oth"), MOVED, "o", changes={"cancel_reason": "Клиент не пришёл"})
    with pytest.raises(sale.SaleError, match="не запись хранения"):
        sale.sell(conn, cid, {orders.get_card(conn, cid)["items"][0]["id"]: 1}, BUYER, PHONE, None, None, False, SOLD, "o")


def test_a_record_on_the_agreement_of_the_return_is_refused(tmp_path):
    conn = db(tmp_path)
    cid, x = split(conn)
    assert order_oth.item_to_return(conn, item(conn, x), MOVED, "офис")
    assert order_oth.agreeing(conn, orders.get_card(conn, x))
    with pytest.raises(sale.SaleError, match="согласовании возврата"):
        sell(conn, x, qty=1)
    assert oth_storage.record_ids(conn) == {x} and conn.execute("SELECT COUNT(*) FROM order_card").fetchone()[0] == 2


# ---- the window of «Рабочее место» -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("view", ["old", "new"])
def test_the_point_opens_the_window_and_sells_a_part(tmp_path, view):
    app, owner, tok, conn = panel(tmp_path)
    if view == "new":
        flag(conn, "all")
    cid, x = split(conn)
    mine = manager(app, conn, orders.get_card(conn, x)["point_id"])
    page = mine.get("/my-point?tab=oth").get_data(as_text=True)
    assert 'href="/my-point/%d/oth-sale' % x in page and 'action="/my-point/%d/oth_sold"' % x not in page
    window = mine.get("/my-point/%d/oth-sale" % x).get_data(as_text=True)
    for text in ("Имя покупателя", "Телефон", "Номер заказа", "Номер перемещения", "Nokian 205/55 R16", "20 000", sale.NO_CONTACT, "+50 ₽"):
        assert text in window, text
    assert 'max="4"' in window and 'min="1"' in window and 'value="%s"' % number(cid) in window and 'value="%s"' % TRANSFER in window
    assert re.search(r'<label class="mp-sale-none" style="font-size:12px"><input type="checkbox" name="anonymous" value="1"[^>]*>%s</label>'
                     % sale.NO_CONTACT, window)
    tok_ = re.search(r'name="token" value="([0-9a-f]{24})"', window).group(1)
    r = post(mine, "/my-point/%d/oth-sale" % x, "tok", token=tok_, **{"name": BUYER, "phone": PHONE, "number": number(cid), "transfer": TRANSFER,
                                                                      "qty_%d" % item(conn, x): "1"})
    assert r.status_code == 302
    assert [i["qty"] for i in orders.get_card(conn, x)["items"]] == [3]
    [b] = [s["order"] for s in sale.sold_of(conn, cid)]
    assert orders.get_card(conn, b)["stage"]["code"] == "done"
    r = post(mine, "/my-point/%d/oth-sale" % x, "tok", token=tok_, **{"name": BUYER, "phone": PHONE, "qty_%d" % item(conn, x): "1"})
    assert [i["qty"] for i in orders.get_card(conn, x)["items"]] == [3]                     # the same press twice: one sale


def test_the_window_says_what_is_missing_and_sells_nothing(tmp_path):
    app, owner, tok, conn = panel(tmp_path)
    cid, x = split(conn)
    mine = manager(app, conn, orders.get_card(conn, x)["point_id"])
    window = mine.get("/my-point/%d/oth-sale" % x).get_data(as_text=True)
    tok_ = re.search(r'name="token" value="([0-9a-f]{24})"', window).group(1)
    r = post(mine, "/my-point/%d/oth-sale" % x, "tok", token=tok_, **{"name": "", "phone": "", "qty_%d" % item(conn, x): "2"})
    assert r.status_code == 400 and "Укажите имя покупателя" in r.get_data(as_text=True)
    assert oth_stock.pieces(conn) == {"1": 4}


def test_the_box_in_the_window_sells_without_a_client(tmp_path):
    app, owner, tok, conn = panel(tmp_path)
    cid, x = split(conn)
    mine = manager(app, conn, orders.get_card(conn, x)["point_id"])
    window = mine.get("/my-point/%d/oth-sale" % x).get_data(as_text=True)
    tok_ = re.search(r'name="token" value="([0-9a-f]{24})"', window).group(1)
    r = post(mine, "/my-point/%d/oth-sale" % x, "tok", token=tok_, anonymous="1", **{"name": "", "phone": "", "qty_%d" % item(conn, x): "4"})
    assert r.status_code == 302
    [b] = [s["order"] for s in sale.sold_of(conn, cid)]
    assert client_of(conn, b) is None and orders.get_card(conn, b)["client_phone"] == "" and oth_stock.pieces(conn) == {}


def test_the_old_press_of_a_record_goes_to_the_window_and_sells_nothing(tmp_path):
    app, owner, tok, conn = panel(tmp_path)
    cid, x = split(conn)
    mine = manager(app, conn, orders.get_card(conn, x)["point_id"])
    r = post(mine, "/my-point/%d/oth_sold" % x, "tok", token="a" * 24, confirm="yes")
    assert r.status_code == 302 and "/my-point/%d/oth-sale" % x in r.headers["Location"]
    assert orders.get_card(conn, x)["stage"]["code"] == "oth" and oth_stock.pieces(conn) == {"1": 4}


# ---- by the review: two tabs, another point, two parts in a row --------------------------------------------------------------------------

def window_token(client, x):
    page = client.get("/my-point/%d/oth-sale" % x).get_data(as_text=True)
    return re.search(r'name="token" value="([0-9a-f]{24})"', page).group(1)


def test_two_tabs_with_their_own_tokens_sell_once(tmp_path):
    app, owner, tok, conn = panel(tmp_path)
    cid, x = split(conn)
    mine = manager(app, conn, orders.get_card(conn, x)["point_id"])
    first, second = window_token(mine, x), window_token(mine, x)          # the window opened in two tabs
    form = {"name": BUYER, "phone": PHONE, "qty_%d" % item(conn, x): "3"}
    assert post(mine, "/my-point/%d/oth-sale" % x, "tok", token=first, **form).status_code == 302
    r = post(mine, "/my-point/%d/oth-sale" % x, "tok", token=second, **form)
    assert r.status_code == 400 and "от 1 до 1 шт." in r.get_data(as_text=True)
    assert len(sale.sold_of(conn, cid)) == 1
    assert [i["qty"] for i in orders.get_card(conn, x)["items"]] == [1] and oth_stock.pieces(conn) == {"1": 1}


def test_the_manager_of_another_point_neither_sees_the_window_nor_sells(tmp_path):
    app, owner, tok, conn = panel(tmp_path)
    cid, x = split(conn)
    other = conn.execute("INSERT INTO point (name) VALUES ('Другая точка')").lastrowid
    conn.commit()
    them = manager(app, conn, other)
    assert them.get("/my-point/%d/oth-sale" % x).status_code == 403
    r = post(them, "/my-point/%d/oth-sale" % x, "tok", token="b" * 24, **{"name": BUYER, "phone": PHONE, "qty_%d" % item(conn, x): "1"})
    assert r.status_code == 403
    assert sale.sold_of(conn, cid) == [] and oth_stock.pieces(conn) == {"1": 4} and orders.get_card(conn, x)["stage"]["code"] == "oth"


def test_two_parts_in_a_row_make_two_done_orders_and_close_the_record(tmp_path):
    app, owner, tok, conn = panel(tmp_path)
    cid, x = split(conn)
    first = sell(conn, x, qty=3)
    second = sell(conn, x, qty=1, name="Ира", phone="+7 900 111 22 33")
    assert first != second and [s["order"] for s in sale.sold_of(conn, cid)] == [first, second]
    for b, pieces in ((first, 3), (second, 1)):
        card = orders.get_card(conn, b)
        assert card["stage"]["code"] == "done" and sum(i["qty"] for i in card["items"]) == pieces
    assert orders.get_card(conn, x)["stage"]["code"] == "cancel"
    a = orders.get_card(conn, cid)
    assert a["stage"]["code"] == "cancel" and a["webmim_number"] == number(cid) + " О" and not a["webmim_number"].endswith("О О")
    assert oth_stock.pieces(conn) == {}
