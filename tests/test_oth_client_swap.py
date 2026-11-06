"""A new client of the tyres of an order on «На ОТХ» (the plan of 2026-11-06): the order A on «На ОТХ» is a purchase of WebMIM (its number,
transfer, invoice), its client is the one who comes. All the tyres given: A stays and gets the new client (oth_swap); a part: a new order takes
them as before (oth_sale.take). The fee of a handed client is never paid for the client who left."""
from __future__ import annotations

import json
import re

import pytest

from order_fixtures import free_phone
from panel_helpers import post
from test_oth_newclient import _quiet
from test_oth_sale import BUY_OLD, LATER, T, new_order, panel, point_id
from tests.test_order_reserve import card_at, db, stage
from tests import test_webmim_create as wc
from tires_sync import (clients, order_oth, order_report, order_sheet, orders, oth_sale, oth_stock, oth_swap, store)

LATEST = "2026-10-29T12:00:00"
NEW_PHONE = "+7 900 555 66 77"


def client_of(conn, order_id):
    row = conn.execute("SELECT client_id FROM client_order WHERE order_id = ?", (order_id,)).fetchone()
    return row[0] if row else None


def put_aside(conn, order_id):
    row = conn.execute("SELECT client_id FROM order_oth_client WHERE order_id = ?", (order_id,)).fetchone()
    return row[0] if row else None


def chat(conn, order_id, name="c1"):
    conn.execute("INSERT INTO avito_chat (account_id, chat_id, order_id) VALUES (1, ?, ?)", (name, order_id))
    conn.commit()
    return conn.execute("SELECT id FROM avito_chat WHERE chat_id = ?", (name,)).fetchone()[0]


def call(conn, order_id, call_id=1):
    conn.execute("INSERT INTO avito_call (account_id, call_id, call_at, order_id) VALUES (1, ?, ?, ?)", (call_id, T, order_id))
    conn.commit()


def audits(conn, action):
    return [json.loads(r[0]) for r in conn.execute("SELECT detail FROM audit WHERE action = ? ORDER BY id", (action,))]


def texts(conn, order_id):
    return [e["text"] for e in orders.list_events(conn, order_id)]


# ---- 1. the name of an order on «На ОТХ» --------------------------------------------------------------------------------------------------

def test_an_order_on_oth_is_named_by_its_purchase_of_webmim(tmp_path):
    conn = db(tmp_path)
    a = card_at(conn, "oth", T)
    conn.execute("UPDATE order_card SET transfer_number = '26R-7' WHERE id = ?", (a,))
    conn.commit()
    card = orders.get_card(conn, a)
    assert order_sheet.mim_title(conn, card) == "№ MIM %s · пер. 26R-7" % card["webmim_number"]
    conn.execute("UPDATE order_card SET webmim_number = '', transfer_number = '' WHERE id = ?", (a,))
    conn.commit()
    assert order_sheet.mim_title(conn, orders.get_card(conn, a)) == "без № MIM"


def test_the_invoice_day_is_in_the_name_as_the_table_counts_it(tmp_path):
    conn = db(tmp_path)
    a = card_at(conn, "oth", T)
    conn.execute("INSERT INTO webmim_create (order_id, server, state, first_at, started_at, updated_at, author) VALUES (?, 'x', 'done', ?, ?, ?, 'o')",
                 (a, T, T, "2026-10-21T09:00:00"))
    conn.commit()
    card = orders.get_card(conn, a)
    assert order_sheet._invoice_day(conn, card) == "2026-10-21"
    assert order_sheet.mim_title(conn, card).endswith("· счёт 21.10.2026")


def test_the_board_names_an_order_on_oth_by_mim_first_and_never_by_the_client(tmp_path):
    conn = db(tmp_path)
    a = card_at(conn, "oth", T)
    here = card_at(conn, "at_point", T)
    from datetime import datetime
    cards = {c["id"]: c for col in orders.board(conn, datetime(2026, 10, 29, 12))["columns"] for c in col["cards"]}
    number = orders.get_card(conn, a)["webmim_number"]
    assert cards[a]["oth_title"] == "№ MIM %s" % number and "Иван" not in cards[a]["client"]
    assert cards[here]["oth_title"] == "" and cards[here]["client"] == "Иван"


def test_the_list_never_names_the_client_of_an_order_on_oth(tmp_path):
    from tires_sync import orders_list
    conn = db(tmp_path)
    a = card_at(conn, "oth", T)
    here = card_at(conn, "at_point", T)
    assert [r["values"]["Имя"] for r in orders_list.rows(conn, [a, here], True)] == ["", "Иван"]
    assert orders.get_card(conn, a)["client_name"] == "Иван"                        # the field of the order stays


@pytest.mark.parametrize("new_view", [False, True])
def test_the_panel_shows_the_mim_name_first_on_the_board_the_list_the_oth_page_and_the_card(tmp_path, new_view):
    app, owner, tok, conn = panel(tmp_path)
    store.set_state(conn, "ui_new.office", "all" if new_view else "off")
    conn.commit()
    a = card_at(conn, "oth", T)
    number = orders.get_card(conn, a)["webmim_number"]
    board = owner.get("/orders").get_data(as_text=True)
    assert re.search(r"№ MIM %s · №%d" % (number, a), board)
    listed = owner.get("/orders?view=list").get_data(as_text=True)
    assert re.search(r"№ MIM %s · №%d" % (number, a), listed)
    oth = owner.get("/oth").get_data(as_text=True)
    assert re.search(r"№ MIM %s · №%d" % (number, a), oth)
    page = owner.get("/orders/%d" % a).get_data(as_text=True)
    assert "№ MIM %s" % number in page and "data-oth-title" in page
    assert ("office2" in board) == new_view
    for text in (board, listed):
        assert not re.search(r"№%d</a>\s*Иван|o2-cli\">Иван" % a, text)


# ---- 2. a new client of all the tyres, from the card on «На ОТХ» -------------------------------------------------------------------------

def test_a_new_client_of_all_the_tyres_stays_in_the_order_on_oth(tmp_path):
    conn = db(tmp_path)
    a = card_at(conn, "oth", T)
    old_client = put_aside(conn, a)
    assert old_client and client_of(conn, a) is None
    row = chat(conn, a)
    call(conn, a)
    before = order_sheet.card_rows(conn, orders.get_card(conn, a), "")
    got = oth_sale.to_new_client(conn, a, "Пётр", NEW_PHONE, LATER, "o")
    assert got == a and conn.execute("SELECT COUNT(*) FROM order_card").fetchone()[0] == 1       # no new order
    card = orders.get_card(conn, a)
    assert card["stage"]["code"] == "at_point" and (card["client_name"], card["client_phone"]) == ("Пётр", NEW_PHONE)
    assert [(i["goods_id"], int(i["qty"]), float(i["supplier_price"])) for i in card["items"]] == [("1", 4, BUY_OLD)]
    assert put_aside(conn, a) is None                                                # the old client is unlinked for good
    new_client = client_of(conn, a)
    assert new_client and new_client != old_client
    assert clients._by_phone(conn, clients.normalize(NEW_PHONE)) == new_client      # joined by the phone
    assert conn.execute("SELECT order_id, unlinked FROM avito_chat WHERE id = ?", (row,)).fetchone() == (None, 1)
    assert card["avito_chat"] == ""
    assert conn.execute("SELECT order_id FROM avito_call").fetchall() == [(a,)]     # his calls and the history stay
    [swap] = audits(conn, "oth_client_swap")
    assert swap["order"] == a and swap["old_client"] == old_client and swap["old_link"]["client_id"] == old_client
    assert swap["old_name"] == "Иван" and swap["chats"] == [row]
    assert any(t.startswith("Клиент сменён: №%d (Иван" % old_client) and t.endswith("→ Пётр, %s" % NEW_PHONE) for t in texts(conn, a))
    after = order_sheet.card_rows(conn, card, "")
    assert list(after) == list(before)                                               # the same row of the table
    assert [(r["Имя"], r["Телефон"]) for r in after.values()] != [(r["Имя"], r["Телефон"]) for r in before.values()]
    assert [r["Имя"] for r in after.values()] == ["Пётр"]
    assert oth_stock.by_point(conn) == {}                                            # off OTH: the stock (and the ad) is gone
    assert order_oth.reserve_dates(conn).get(a) is None


def test_the_seventh_day_never_comes_after_the_swap(tmp_path):
    conn = db(tmp_path)
    a = card_at(conn, "oth", T)
    oth_sale.to_new_client(conn, a, "Пётр", NEW_PHONE, LATER, "o")
    order_oth.ask_seventh_day(conn, "2026-11-20T13:00:00")
    assert orders.get_card(conn, a)["stage"]["code"] == "at_point"


def test_the_order_keeps_its_point(tmp_path):
    conn = db(tmp_path)
    a = card_at(conn, "oth", T)
    pid = orders.get_card(conn, a)["point_id"]
    oth_sale.to_new_client(conn, a, "Пётр", NEW_PHONE, LATER, "o")
    assert orders.get_card(conn, a)["point_id"] == pid


@pytest.mark.parametrize("why", ["returned", "point_tyre", "sale"])
def test_what_is_not_all_the_tyres_makes_a_new_order_as_before(tmp_path, why):
    conn = db(tmp_path)
    a = card_at(conn, "oth", T)
    item = orders.get_card(conn, a)["items"][0]
    if why == "returned":
        conn.execute("INSERT INTO oth_return (item_id, order_id, qty, at, author) VALUES (?, ?, 1, ?, 'o')", (item["id"], a, T))
    elif why == "point_tyre":
        conn.execute("INSERT INTO order_item (order_id, goods_id, name, qty, supplier_price, sale_unit) VALUES (?, ?, 'Шина точки', 1, 0, 1000)",
                     (a, orders.POINT_GOODS))
    else:
        conn.execute("UPDATE order_card SET sale_ids = '[999]' WHERE id = ?", (a,))
    conn.commit()
    assert oth_swap.why_not(conn, orders.get_card(conn, a))
    if why == "sale":
        with pytest.raises(orders.OrderError):
            oth_sale.to_new_client(conn, a, "Пётр", NEW_PHONE, LATER, "o")
        assert orders.get_card(conn, a)["client_name"] == "Иван"                     # an order with a sale never changes its client
        return
    new = oth_sale.to_new_client(conn, a, "Пётр", NEW_PHONE, LATER, "o")
    assert new != a and orders.get_card(conn, new)["client_name"] == "Пётр"
    assert orders.get_card(conn, a)["client_name"] == "Иван" and audits(conn, "oth_client_swap") == []


def test_a_phone_with_an_open_order_of_the_point_is_refused_and_nothing_changes(tmp_path):
    conn = db(tmp_path)
    other = card_at(conn, "at_point", T)
    busy = orders.get_card(conn, other)["client_phone"]
    a = card_at(conn, "oth", T)
    with pytest.raises(orders.OrderError, match="открытый заказ"):
        oth_sale.to_new_client(conn, a, "Пётр", busy, LATER, "o")
    card = orders.get_card(conn, a)
    assert card["stage"]["code"] == "oth" and card["client_name"] == "Иван" and put_aside(conn, a)


def test_the_panel_swaps_the_client_and_tells_the_new_one(tmp_path, monkeypatch):
    app, owner, tok, conn = panel(tmp_path)
    a = card_at(conn, "oth", T)
    told = _quiet(monkeypatch)
    r = post(owner, "/orders/%d/oth-back" % a, tok, answer="new", client_name="Пётр", client_phone=NEW_PHONE)
    assert r.headers["Location"].endswith("/orders/%d" % a)
    assert orders.get_card(conn, a)["stage"]["code"] == "at_point" and told == {"client": [a], "point": [a]}


# ---- 3. the manager takes all the tyres of A into the order B of the client -----------------------------------------------------------

def _b(conn, qty=4, extra=()):
    b = new_order(conn, qty=qty, extra=extra)
    row = chat(conn, b, "cb")
    conn.execute("UPDATE order_card SET avito_chat = ? WHERE id = ?", ("Ч-%d" % row, b))
    call(conn, b, 7)
    return b, row


def test_taking_all_the_tyres_of_a_moves_the_client_of_b_into_a(tmp_path):
    conn = db(tmp_path)
    a = card_at(conn, "oth", T)
    old_client = put_aside(conn, a)
    old_chat = chat(conn, a)
    b, b_chat = _b(conn)
    b_client = client_of(conn, b)
    line = orders.get_card(conn, b)["items"][0]
    source = orders.get_card(conn, a)["items"][0]["id"]
    assert oth_swap.take_target(conn, b, line["id"], source) == a
    assert oth_swap.take_client(conn, b, line["id"], source, LATER, "o") == a
    card, gone = orders.get_card(conn, a), orders.get_card(conn, b)
    assert card["stage"]["code"] == "at_point" and (card["client_name"], card["client_phone"]) == ("Пётр", orders.get_card(conn, b)["client_phone"])
    assert client_of(conn, a) == b_client and client_of(conn, a) != old_client and put_aside(conn, a) is None
    assert conn.execute("SELECT order_id FROM avito_chat WHERE id = ?", (b_chat,)).fetchone() == (a,)
    assert conn.execute("SELECT order_id, unlinked FROM avito_chat WHERE id = ?", (old_chat,)).fetchone() == (None, 1)
    assert conn.execute("SELECT call_id, order_id FROM avito_call").fetchall() == [(7, a)]
    assert card["avito_chat"] == "Ч-%d" % b_chat
    assert gone["stage"]["code"] == "cancel" and gone["cancel_reason"] == oth_swap.CLIENT_TOOK % a
    assert [int(i["qty"]) for i in gone["items"]] == [4]                               # the data of B stays
    assert [int(i["qty"]) for i in card["items"]] == [4] and oth_sale.taken(conn, b) == {}
    [swap] = audits(conn, "oth_client_swap")
    assert swap["from_order"] == b and swap["old_client"] == old_client


def test_b_with_another_position_takes_as_before(tmp_path):
    conn = db(tmp_path)
    a = card_at(conn, "oth", T)
    b, _ = _b(conn, extra=(("2", 2),))
    line = next(i for i in orders.get_card(conn, b)["items"] if i["goods_id"] == "1")
    source = orders.get_card(conn, a)["items"][0]["id"]
    assert oth_swap.take_target(conn, b, line["id"], source) is None
    assert oth_sale.take(conn, b, line["id"], source, LATER, "o") == 4
    assert orders.get_card(conn, a)["stage"]["code"] == "cancel" and audits(conn, "oth_client_swap") == []


def test_another_point_is_never_a_target(tmp_path):
    conn = db(tmp_path)
    a = card_at(conn, "oth", T)
    b = new_order(conn, at="Гамма")
    line = orders.get_card(conn, b)["items"][0]
    assert oth_swap.take_target(conn, b, line["id"], orders.get_card(conn, a)["items"][0]["id"]) is None


def test_the_panel_take_opens_a_after_the_swap(tmp_path, monkeypatch):
    app, owner, tok, conn = panel(tmp_path)
    a = card_at(conn, "oth", T)
    b, _ = _b(conn)
    told = _quiet(monkeypatch)
    line = orders.get_card(conn, b)["items"][0]
    r = post(owner, "/orders/%d/items/%d/oth-take" % (b, line["id"]), tok, from_item=str(orders.get_card(conn, a)["items"][0]["id"]))
    assert r.headers["Location"].endswith("/orders/%d" % a)
    assert orders.get_card(conn, a)["stage"]["code"] == "at_point" and orders.get_card(conn, b)["stage"]["code"] == "cancel"
    assert told["client"] == [a]


# ---- 4, 5. a part of the tyres: a new order, the chain stays ----------------------------------------------------------------------------

def test_a_part_makes_the_new_order_from_the_purchase_of_a(tmp_path):
    conn = db(tmp_path)
    a = card_at(conn, "oth", T)
    b = new_order(conn, qty=2)
    line = orders.get_card(conn, b)["items"][0]
    source = orders.get_card(conn, a)["items"][0]["id"]
    assert oth_swap.take_target(conn, b, line["id"], source) is None
    assert oth_sale.take(conn, b, line["id"], source, LATER, "o") == 2
    number = orders.get_card(conn, a)["webmim_number"]
    rows_a = order_sheet.card_rows(conn, orders.get_card(conn, a), "")
    rows_b = order_sheet.card_rows(conn, orders.get_card(conn, b), "")
    assert [r["Кол-во"] for r in rows_a.values()] == [2] and orders.get_card(conn, a)["stage"]["code"] == "oth"
    assert [(r["Кол-во"], r["№ заказа MIM"], r["Цена MIM"]) for r in rows_b.values()] == [(2, number, BUY_OLD)]
    assert [t["from_order"] for t in oth_sale.taken(conn, b).values()] == [a]          # the chain
    assert b in oth_sale.borrowed_ids(conn) and a not in oth_sale.borrowed_ids(conn)


# ---- 6, 7. the client came back; a refusal after the swap -------------------------------------------------------------------------------

def test_a_refusal_after_the_swap_unlinks_the_new_client_and_the_same_one_comes_back(tmp_path):
    conn = db(tmp_path)
    a = card_at(conn, "oth", T)
    oth_sale.to_new_client(conn, a, "Пётр", NEW_PHONE, LATER, "o")
    new_client = client_of(conn, a)
    orders.move_card(conn, a, stage(conn, "oth"), LATEST, "o")
    card = orders.get_card(conn, a)
    assert card["entered_at"] == LATEST and client_of(conn, a) is None and put_aside(conn, a) == new_client
    orders.move_card(conn, a, stage(conn, "at_point"), "2026-10-29T13:00:00", "o")
    assert client_of(conn, a) == new_client                                          # the last one linked, never the first


def test_the_split_order_goes_to_oth_with_its_own_numbers(tmp_path):
    conn = db(tmp_path)
    a = card_at(conn, "oth", T)
    b = new_order(conn, qty=2)
    line = orders.get_card(conn, b)["items"][0]
    oth_sale.take(conn, b, line["id"], orders.get_card(conn, a)["items"][0]["id"], LATER, "o")
    for code in ("at_point", "oth"):
        orders.move_card(conn, b, stage(conn, code), LATEST, "o")
    card = orders.get_card(conn, b)
    assert card["stage"]["code"] == "oth" and client_of(conn, b) is None
    assert order_sheet.mim_title(conn, card) == "№ MIM %s" % orders.get_card(conn, a)["webmim_number"]


# ---- 9. the fee of a handed client --------------------------------------------------------------------------------------------------------

def _sell(conn, order_id):
    for code in ("done",):
        orders.move_card(conn, order_id, stage(conn, code), LATEST, "o")
    orders.record_sales(conn, order_id, "2026-10-29", LATEST, "o", "2026-10-29")


def test_the_fee_of_the_client_who_left_is_never_paid(tmp_path):
    conn = db(tmp_path)
    a = card_at(conn, "oth", T)
    conn.execute("INSERT INTO order_handoff (order_id, message_id, point_id, created_at) VALUES (?, 'h1', ?, ?)", (a, point_id(conn), T))
    conn.commit()
    oth_sale.to_new_client(conn, a, "Пётр", NEW_PHONE, LATER, "o")
    _sell(conn, a)
    report = order_report.build(conn, "2026-10-01", "2026-10-31")
    assert report["handoff"]["clients"] == 0
    profit = sum(r["Прибыль ₽"] for r in order_sheet.card_rows(conn, orders.get_card(conn, a), "").values())
    sale = conn.execute("SELECT margin_total FROM sale WHERE reversed_at IS NULL").fetchone()[0]
    rates = order_report.list_rates(conn)
    assert profit == round(sale - order_report.rate_on(rates, "point_fee", "2026-10-29"), 2)       # 75 ₽ only, no 50 ₽
    assert conn.execute("SELECT 1 FROM order_handoff WHERE order_id = ?", (a,)).fetchone()          # the handoff itself is kept
    assert any(t.startswith("Сбор за передачу клиента точкой снят") for t in texts(conn, a))


def test_the_fee_without_a_swap_is_paid_as_before(tmp_path):
    conn = db(tmp_path)
    a = card_at(conn, "at_point", T)
    conn.execute("INSERT INTO order_handoff (order_id, message_id, point_id, created_at) VALUES (?, 'h1', ?, ?)", (a, point_id(conn), T))
    conn.commit()
    _sell(conn, a)
    assert order_report.build(conn, "2026-10-01", "2026-10-31")["handoff"]["clients"] == 1
    profit = sum(r["Прибыль ₽"] for r in order_sheet.card_rows(conn, orders.get_card(conn, a), "").values())
    sale = conn.execute("SELECT margin_total FROM sale WHERE reversed_at IS NULL").fetchone()[0]
    rates = order_report.list_rates(conn)
    assert profit == round(sale - order_report.rate_on(rates, "point_fee", "2026-10-29") - order_report.rate_on(rates, "handoff_fee", "2026-10-29"), 2)


def test_the_fee_of_b_handed_by_a_point_goes_with_its_client_into_a(tmp_path):
    conn = db(tmp_path)
    a = card_at(conn, "oth", T)
    b, _ = _b(conn)
    conn.execute("INSERT INTO order_handoff (order_id, message_id, point_id, created_at) VALUES (?, 'hb', ?, ?)", (b, point_id(conn), T))
    conn.commit()
    line = orders.get_card(conn, b)["items"][0]
    oth_swap.take_client(conn, b, line["id"], orders.get_card(conn, a)["items"][0]["id"], LATER, "o")
    _sell(conn, a)
    assert order_report.build(conn, "2026-10-01", "2026-10-31")["handoff"]["clients"] == 1


def test_the_sale_of_a_is_counted_once_at_its_own_purchase(tmp_path):
    conn = db(tmp_path)
    a = card_at(conn, "oth", T)
    oth_sale.to_new_client(conn, a, "Пётр", NEW_PHONE, LATER, "o")
    _sell(conn, a)
    rows = conn.execute("SELECT note, pieces, cost_total FROM sale WHERE reversed_at IS NULL").fetchall()
    assert rows == [("Заказ №%d" % a, 4, BUY_OLD * 4)]
    assert a not in oth_sale.borrowed_ids(conn)
