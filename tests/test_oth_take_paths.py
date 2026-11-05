"""Every way a new order gets tyres from «На ОТХ» (the owner 2026-11-05: «при взятии с ОТХ … присваивать номер счета MIM и номер перемещения»): order_item_oth is
written by oth_sale.take and by a merge only, so an order made anywhere else has the supplier's position until «Взять с ОТХ» is pressed in its card."""
from __future__ import annotations

import re

import pytest

from panel_helpers import post
from test_chat_deals import Fake, setup as chats_setup, sync  # noqa: F401
from test_order_window import created, setup as window_setup
from test_oth_newclient import _quiet
from tests.test_order_reserve import card_at, db
from tests.test_oth_numbers import NUMBER, OLD_NUMBERS, TRANSFER, add_supplier, numbers, source
from tests.test_oth_sale import LATER, T, new_order, panel, stage, take
from tests.test_oth_take_invoice import COLUMNS, invoice, row_of
from tires_sync import chats, orders, oth_sale


def press_take(client, tok, cid):
    """The offer in the card, as a person sees and presses it."""
    html = client.get("/orders/%d" % cid).get_data(as_text=True)
    form = re.search(r'action="/orders/%d/items/(\d+)/oth-take".*?name="from_item" value="(\d+)"' % cid, html, re.S)
    assert form, "no offer in the card"
    assert post(client, "/orders/%d/items/%s/oth-take" % (cid, form.group(1)), tok, from_item=form.group(2)).status_code == 302


def source_of_goods(conn, goods):
    old = source(conn)
    conn.execute("UPDATE order_item SET goods_id = ? WHERE order_id = ?", (goods, old))
    conn.commit()
    return old


# ---- the order is made with both numbers at once ------------------------------------------------------------------------------------------

def test_new_client_on_the_oth_page_gets_both_numbers_as_it_is_made(tmp_path, monkeypatch):
    app, owner, tok, conn = panel(tmp_path)
    old = source(conn)
    _quiet(monkeypatch)
    post(owner, "/orders/%d/move" % old, tok, stage_id=str(stage(conn, "at_point")))
    r = post(owner, "/orders/%d/oth-back" % old, tok, answer="new", client_name="Пётр", client_phone="+7 900 555 66 77")
    new = int(re.search(r"/orders/(\d+)", r.headers["Location"]).group(1))
    assert new != old and numbers(conn, new) == OLD_NUMBERS and orders.get_card(conn, new)["webmim_id"] == ""
    assert orders.get_card(conn, new)["stage"]["code"] == "at_point"


def test_new_client_of_an_order_of_two_positions_gets_both_numbers(tmp_path):
    conn = db(tmp_path)
    old = source(conn)
    orders.add_item(conn, old, goods_id="2", name="Шина 2", brand_key="b", season="winter", diameter=16, territory="1", qty=2, sale_unit=6000,
                    supplier_price=4500, terms=None, fee_pct=0, promo_pct=0, now=T, author="o")
    new = oth_sale.to_new_client(conn, old, "Пётр", "+7 900 555 66 77", LATER, "o")
    assert numbers(conn, new) == OLD_NUMBERS and len(orders.get_card(conn, new)["items"]) == 2


# ---- the order is made elsewhere, the offer in its card takes the tyres ----------------------------------------------------------------------

@pytest.mark.parametrize("how,form", [
    ("catalog", {"client_name": "Антон", "goods_id": "100"}),
    ("window", {"client_name": "Антон", "item_goods_id": ["100"], "item_query": [""], "item_qty": ["4"], "item_sale_unit": ["5000"]})])
def test_an_order_made_with_the_position_gets_both_numbers_by_the_offer_in_its_card(tmp_path, monkeypatch, how, form):
    app, client, tok, conn = window_setup(tmp_path, monkeypatch)
    source_of_goods(conn, "100")
    cid = created(post(client, "/orders/new", tok, **form))
    assert numbers(conn, cid) == ("", "")                                           # nothing is taken before the press
    press_take(client, tok, cid)
    assert numbers(conn, cid) == OLD_NUMBERS


def test_the_quick_add_of_the_board_gets_both_numbers_by_the_offer_in_its_card(tmp_path, monkeypatch):
    app, client, tok, conn = window_setup(tmp_path, monkeypatch)
    source_of_goods(conn, "1")
    cid = created(post(client, "/orders/new", tok, client_name="Иван"))
    add_supplier(conn, cid, goods="1")
    press_take(client, tok, cid)
    assert numbers(conn, cid) == OLD_NUMBERS


def test_an_order_from_a_chat_gets_both_numbers_by_the_offer(tmp_path):
    conn, account, fake = chats_setup(tmp_path)
    fake.add("c1", "Татьяна", [("in", "Здравствуйте, это актуально?")])
    sync(conn, account, fake)
    cid = chats.list_chats(conn)[0]["order_id"]
    source(conn)
    add_supplier(conn, cid, goods="1")
    take(conn, cid)
    assert numbers(conn, cid) == OLD_NUMBERS


# ---- the source closed, an order of WebMIM of its own --------------------------------------------------------------------------------------

def test_the_source_cancelled_after_giving_all_its_tyres_keeps_its_numbers_and_so_does_the_new_order(tmp_path):
    conn = db(tmp_path)
    old = source(conn)
    invoice(conn, old)
    cid = new_order(conn)
    take(conn, cid)
    assert orders.get_card(conn, old)["stage"]["code"] == "cancel"
    assert numbers(conn, old) == OLD_NUMBERS and numbers(conn, cid) == OLD_NUMBERS
    assert row_of(conn, cid) == (NUMBER, TRANSFER, "2026-10-21")
    orders.update_card(conn, cid, {"comment": "позвонить"}, LATER, "o")             # any later write of the order
    orders.update_card(conn, old, {"comment": "закрыт"}, LATER, "o")
    assert numbers(conn, cid) == OLD_NUMBERS and numbers(conn, old) == OLD_NUMBERS


def test_a_source_with_an_id_of_webmim_gives_its_number_never_its_id(tmp_path):
    conn = db(tmp_path)
    old = source(conn)
    conn.execute("UPDATE order_card SET webmim_id = 'id-1' WHERE id = ?", (old,))
    conn.commit()
    cid = new_order(conn)
    take(conn, cid)
    assert numbers(conn, cid) == OLD_NUMBERS and orders.get_card(conn, cid)["webmim_id"] == ""


def test_an_order_with_an_id_of_webmim_cannot_take_and_keeps_its_numbers(tmp_path):
    conn = db(tmp_path)
    source(conn)
    cid = new_order(conn)
    orders.update_card(conn, cid, {"webmim_id": "own", "webmim_number": "7000001", "transfer_number": "26R-OWN"}, LATER, "o")
    with pytest.raises(oth_sale.OthSaleError, match="уже есть в WebMIM"):
        take(conn, cid)
    assert numbers(conn, cid) == ("7000001", "26R-OWN")


def test_an_id_of_webmim_given_after_the_take_keeps_the_numbers_and_they_stop_following_the_source(tmp_path):
    conn = db(tmp_path)
    old = source(conn)
    cid = new_order(conn)
    take(conn, cid)
    orders.update_card(conn, cid, {"webmim_id": "own"}, LATER, "o")
    orders.update_card(conn, old, {"transfer_number": "26R-9"}, LATER, "o")
    assert numbers(conn, cid) == OLD_NUMBERS                                        # the early way out of _sync_numbers
    assert COLUMNS[0] == "№ заказа MIM" and row_of(conn, cid)[:2] == (NUMBER, "26R-9")   # the row is of the tyres, by position
