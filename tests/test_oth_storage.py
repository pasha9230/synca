"""The owner 2026-11-10 (the partner asked): a client's order that goes to «На ОТХ» is cancelled at once — «Отмена» with the reason «отказ
клиента», its WebMIM and transfer numbers get « О» (2026-11-11; the transfer was cleared before), the client stays linked and hears nothing — and its tyres become a
record of storage: an order without a client on «На ОТХ» with the point, the positions, the numbers and the dates. A new client takes a
record whole only; the same client gets the order back. The orders already on «На ОТХ» live on the old way (the guard at the end)."""
from __future__ import annotations

from datetime import date

import pytest

from panel_helpers import post
from test_not_bought_to_oth import merged
from test_order_reserve import card_at, db, stage
from test_oth_sale import new_order, offer, panel, take
from test_oth_seventh_day import started
from tires_sync import (connections, order_manager as mgr, order_oth, order_reserve as reserve, order_rules as rules, order_templates as tpl, orders,
                        oth_sale, oth_stock, oth_storage, search, shop_cart)

OTH_SPLIT = True
ARRIVED = "2026-10-15T10:00:00"
MOVED = "2026-10-20T11:00:00"
LATER = "2026-10-21T09:00:00"
TRANSFER = "26R-0001234"


def at_point(conn, transfer=TRANSFER):
    cid = card_at(conn, "at_point", ARRIVED)
    conn.execute("UPDATE order_card SET transfer_number = ?, webmim_id = ?, responsible = 'o' WHERE id = ?", (transfer, str(900000 + cid), cid))
    conn.commit()
    return cid


def number(cid):
    return str(5200000 + cid)                                     # card_at


def record(conn, cid):
    x = oth_storage.record_of(conn, cid)
    assert x is not None, "no record of storage"
    return x


def split(conn, when=MOVED, reason="Клиент не пришёл"):
    cid = at_point(conn)
    assert orders.move_card(conn, cid, stage(conn, "oth"), when, "o", changes={"cancel_reason": reason})
    return cid, record(conn, cid)


def linked(conn, cid):
    return conn.execute("SELECT 1 FROM client_order WHERE order_id = ?", (cid,)).fetchone() is not None


def texts(conn, cid):
    return [e["text"] for e in orders.list_events(conn, cid)]


# ---- the split ------------------------------------------------------------------------------------------------------------------------

def test_the_clients_order_is_cancelled_with_the_letter_and_the_record_holds_everything(tmp_path):
    conn = db(tmp_path)
    cid, x = split(conn)
    a, rec = orders.get_card(conn, cid), orders.get_card(conn, x)
    assert a["stage"]["code"] == "cancel" and a["cancel_reason"] == "отказ клиента: Клиент не пришёл"
    assert a["webmim_number"] == number(cid) + " О" and a["transfer_number"] == TRANSFER + " О" and a["webmim_id"] == ""
    assert a["client_name"] == "Иван" and linked(conn, cid)                              # their cancelled order
    assert [(i["goods_id"], i["qty"]) for i in a["items"]] == [("1", 4)]                 # what the client had ordered stays in it
    assert "Шины переданы на ОТХ, запись №%d" % x in texts(conn, cid)
    assert rec["stage"]["code"] == "oth" and rec["entered_at"] == MOVED
    assert (rec["client_name"], rec["client_phone"], rec["avito_chat"]) == ("", "", "") and not linked(conn, x)
    assert (rec["webmim_number"], rec["transfer_number"], rec["webmim_id"]) == (number(cid), TRANSFER, str(900000 + cid))
    assert rec["point_id"] == a["point_id"] and rec["responsible"] == "o"
    assert [(i["goods_id"], i["code"], i["name"], i["qty"], i["supplier_price"], i["sale_unit"], i["terms"]) for i in rec["items"]] == [
        (i["goods_id"], i["code"], i["name"], i["qty"], i["supplier_price"], i["sale_unit"], i["terms"]) for i in a["items"]]
    assert "Из заказа №%d (клиент отказался)" % cid in texts(conn, x)
    assert order_oth.reserve_dates(conn)[x] == {"arrived": "2026-10-15", "until": "2026-10-18", "oth_since": "2026-10-20"}
    assert order_oth._oth_since(conn, x) == MOVED
    assert oth_storage.is_record(conn, x) and not oth_storage.is_record(conn, cid) and oth_storage.source_of(conn, x) == cid
    assert orders.unlinked_orders(conn) == {x}                                           # the lists show the record without a client


def test_an_order_without_a_webmim_number_gets_no_letter(tmp_path):
    conn = db(tmp_path)
    cid = at_point(conn, transfer="")
    conn.execute("UPDATE order_card SET webmim_number = '', webmim_id = '' WHERE id = ?", (cid,))
    conn.commit()
    orders.move_card(conn, cid, stage(conn, "oth"), MOVED, "o", changes={"cancel_reason": "отказ"})
    assert orders.get_card(conn, cid)["webmim_number"] == "" and orders.get_card(conn, record(conn, cid))["webmim_number"] == ""


def test_the_reason_of_the_cancel_can_be_chosen_in_the_window():
    assert "отказ клиента" in orders.CANCEL_KINDS
    assert orders.cancel_reason_of("отказ клиента", "передумал") == "отказ клиента: передумал"


def test_a_refused_move_leaves_nothing_behind(tmp_path):
    conn = db(tmp_path)
    cid = at_point(conn)
    conn.execute("UPDATE order_card SET sale_ids = '[7]' WHERE id = ?", (cid,))
    conn.commit()
    with pytest.raises(orders.OrderError):
        orders.move_card(conn, cid, stage(conn, "oth"), MOVED, "o", changes={"cancel_reason": "отказ"})
    assert oth_storage.record_of(conn, cid) is None and oth_storage.record_ids(conn) == set()
    assert orders.get_card(conn, cid)["webmim_number"] == number(cid) and conn.execute("SELECT COUNT(*) FROM order_card").fetchone()[0] == 1


def test_no_switch_in_the_base_turns_the_split_off(tmp_path):
    """The owner 2026-11-10: the split is always on; the former key «oth_split» = «off» in app_state changes nothing."""
    conn = db(tmp_path)
    conn.execute("INSERT INTO app_state (key, value) VALUES ('oth_split', 'off')")
    cid = at_point(conn)
    orders.move_card(conn, cid, stage(conn, "oth"), MOVED, "o", changes={"cancel_reason": "отказ"})
    assert orders.get_card(conn, cid)["stage"]["code"] == "cancel" and oth_storage.record_ids(conn) == {record(conn, cid)}
    assert not hasattr(oth_storage, "enabled") and not hasattr(oth_storage, "SWITCH")


# ---- every way to «На ОТХ» --------------------------------------------------------------------------------------------------------------

def client_rule(conn, code):
    """A rule of the stage that writes to the client by itself: what must never fire for the order cancelled by the split."""
    template = tpl.add_template(conn, "Клиенту: отмена", "client", "Ваш заказ отменён.")
    return rules.add_rule(conn, stage(conn, code), "enter", 0, "message", template, "", "auto", None)


def nothing_for_the_client(conn, cid):
    return conn.execute("SELECT COUNT(*) FROM order_task WHERE order_id = ?", (cid,)).fetchone()[0]


def check_split(conn, cid, detail):
    a = orders.get_card(conn, cid)
    x = record(conn, cid)
    assert a["stage"]["code"] == "cancel" and a["cancel_reason"] == "отказ клиента: " + detail, a["cancel_reason"]
    assert a["webmim_number"].endswith("О") and orders.get_card(conn, x)["stage"]["code"] == "oth"
    assert orders.get_card(conn, x)["webmim_number"] == number(cid) and orders.get_card(conn, x)["transfer_number"] == TRANSFER
    return x


def test_the_points_refusal_keeps_its_reason_as_the_detail(tmp_path):
    conn = db(tmp_path)
    cid = at_point(conn)
    before = nothing_for_the_client(conn, cid)
    client_rule(conn, "cancel")
    outcome, answer, done = mgr.refuse(conn, orders.get_card(conn, cid), "Ошиблись размером", "менеджер Бета", MOVED, None)
    assert done, outcome
    check_split(conn, cid, "Отказ клиента на точке: Ошиблись размером")
    assert nothing_for_the_client(conn, cid) == before                                   # no rule of «Отмена» fired: the client hears nothing
    assert any(e["level"] == "red" and e["text"].startswith("Отказ клиента на точке") for e in orders.list_events(conn, cid))


def test_not_bought_by_the_points_word(tmp_path):
    conn = db(tmp_path)
    cid = at_point(conn)
    command = merged(conn, "at_point")
    outcome, _answer = mgr.apply(conn, orders.get_card(conn, cid), command["to_stage_id"], "менеджер Бета", MOVED, None, mgr.merged_reason(conn, command))
    assert outcome == "заказ перенесён на «На ОТХ»"
    check_split(conn, cid, orders.NOT_BOUGHT + " (команда точки)")
    assert not mgr._return_from(orders.get_card(conn, cid))                               # «возврат отдал» is for the record now, not for this order


def test_the_timer_of_the_reservation(tmp_path):
    from test_reserve_noon import NEXT, told
    conn, cid, _chat, _deliver, _vk = told(tmp_path)
    conn.execute("UPDATE order_card SET transfer_number = ? WHERE id = ?", (TRANSFER, cid))
    conn.commit()
    client_rule(conn, "cancel")
    before = nothing_for_the_client(conn, cid)
    assert reserve._no_answer_step(conn, orders.get_card(conn, cid), NEXT + "T00:30:00")
    check_split(conn, cid, reserve.NO_ANSWER_REASON)
    assert nothing_for_the_client(conn, cid) == before
    assert any(e["text"].startswith("Позвонить клиенту:") for e in orders.list_events(conn, cid))        # the call is about the client's order
    held = conn.execute("SELECT key FROM app_state WHERE key LIKE 'reserve_moved_tell:%'").fetchall()
    told_people = []
    reserve._release_held(conn, NEXT + "T09:05:00", lambda card, text: told_people.append(text))
    assert not held or told_people                                                       # the alert held for the morning still goes


def test_yes_to_the_question_of_the_window(tmp_path):
    app, owner, tok, conn = panel(tmp_path)
    cid = at_point(conn)
    client_rule(conn, "cancel")
    before = nothing_for_the_client(conn, cid)
    r = post(owner, "/orders/%d/move" % cid, tok, stage_id=str(stage(conn, "oth")), oth_answer="yes", cancel_reason="не выкупил: не пришёл")
    assert r.status_code == 302
    x = check_split(conn, cid, "не выкупил: не пришёл")
    assert nothing_for_the_client(conn, cid) == before
    page = owner.get("/orders/%d" % x).get_data(as_text=True)
    assert "Из заказа №%d" % cid in page and "Вернуть заказ клиенту" in page
    assert "Вернуть заказ клиенту" in owner.get("/orders/%d" % cid).get_data(as_text=True)


def test_the_rules_of_oth_fire_for_the_record_and_none_of_cancel_for_the_order(tmp_path):
    conn = db(tmp_path)
    cid = at_point(conn)
    client_rule(conn, "cancel")
    rules.add_rule(conn, stage(conn, "oth"), "enter", 0, "remind", None, "Проверить шины на ОТХ", "ask", None)
    before = nothing_for_the_client(conn, cid)
    orders.move_card(conn, cid, stage(conn, "oth"), MOVED, "o", changes={"cancel_reason": "отказ"})
    assert nothing_for_the_client(conn, cid) == before
    assert [r[0] for r in conn.execute("SELECT title FROM order_task WHERE order_id = ?", (record(conn, cid),))] == ["Проверить шины на ОТХ"]


# ---- what works from an order on «На ОТХ» works from the record ---------------------------------------------------------------------------

def stock_view(conn):
    rows = order_oth.stock(conn)["stock"]
    return (sorted((r["goods_id"], r["code"], r["name"], r["qty"], r["point"], r["since"], r["stage_code"]) for r in rows),
            oth_stock.pieces(conn), order_oth.goods_on_oth(conn))


def test_the_stock_of_oth_is_what_it_would_be_the_old_way(tmp_path, monkeypatch):
    (tmp_path / "new").mkdir()
    (tmp_path / "old").mkdir()
    new = db(tmp_path / "new")
    split(new)
    monkeypatch.setattr(oth_storage, "SPLIT", False)
    old = db(tmp_path / "old")
    cid = at_point(old)
    orders.move_card(old, cid, stage(old, "oth"), MOVED, "o", changes={"cancel_reason": "Клиент не пришёл"})
    assert orders.get_card(old, cid)["stage"]["code"] == "oth"
    assert stock_view(new) == stock_view(old) and stock_view(new)[1] == {"1": 4}


def test_the_seventh_day_and_the_return_to_the_supplier_go_from_the_record(tmp_path):
    conn = db(tmp_path)
    started(conn)
    cid, x = split(conn)
    assert order_oth.ask_seventh_day(conn, "2026-10-26T12:29:00") == 0
    assert order_oth.ask_seventh_day(conn, "2026-10-26T12:30:00") == 1
    rec = orders.get_card(conn, x)
    assert rec["stage"]["code"] == orders.RETURN_GIVEN and orders.get_card(conn, cid)["stage"]["code"] == "cancel"
    assert oth_stock.pieces(conn) == {}                                                   # «На возврат»: off sale, as before
    day = order_oth.agree(conn, rec, date(2026, 10, 27), "2026-10-27T10:00:00", "o")
    assert order_oth.return_plan(conn, x)["return_date"] == day
    assert order_oth.sign_act(conn, orders.get_card(conn, x), "Бета", "2026-10-28T10:00:00", "менеджер Бета")
    assert "уходят поставщику" in oth_storage.why_not_back(conn, cid)                     # the same client is too late


def test_a_record_never_takes_a_client_itself(tmp_path):
    conn = db(tmp_path)
    cid, x = split(conn)
    orders.update_card(conn, x, {"client_name": "Пётр", "client_phone": "+7 900 555 66 77", "delivery_date": "2026-10-30"}, LATER, "o")
    with pytest.raises(orders.OrderError, match="запись хранения"):
        orders.move_card(conn, x, stage(conn, "at_point"), LATER, "o")
    assert orders.get_card(conn, x)["stage"]["code"] == "oth"


# ---- a new client takes the record whole ------------------------------------------------------------------------------------------------

def test_a_new_order_takes_the_whole_record_with_the_numbers_the_dates_and_the_price(tmp_path):
    conn = db(tmp_path)
    cid, x = split(conn)
    b = new_order(conn, qty=4)                                                           # its line: 6000 a tyre, the record's: 5000
    assert offer(conn, b)["why"] is None and offer(conn, b)["from_order"] == x
    assert take(conn, b, now=LATER) == 4
    card, rec = orders.get_card(conn, b), orders.get_card(conn, x)
    assert (card["webmim_number"], card["transfer_number"]) == (number(cid), TRANSFER)
    [item] = card["items"]
    assert item["id"] in oth_sale.taken_ids(conn, b) and (item["qty"], item["supplier_price"], item["sale_unit"]) == (4, 4000.0, 5000.0)
    assert card["in_stock"] and card["point_id"] == rec["point_id"]
    assert rec["stage"]["code"] == "cancel" and rec["cancel_reason"] == oth_sale.TAKEN_REASON % b
    assert "Шины с ОТХ (запись №%d) взял заказ №%d" % (x, b) in texts(conn, cid)
    assert any("Передано с ОТХ в заказ №%d" % b in t for t in texts(conn, x))
    assert any(t.startswith("Шины записи хранения №%d (из заказа №%d): на точке с 15.10.2026, на ОТХ с 20.10.2026" % (x, cid)) for t in texts(conn, b))
    assert oth_stock.pieces(conn) == {} and oth_sale.holds(conn, b) == [x]
    assert b in oth_sale.borrowed_ids(conn) and mgr.find_orders(conn, int(number(cid))) == [b]      # the points know the live order by the number
    assert "взял заказ" in oth_storage.why_not_back(conn, cid)


def feed_price(conn, goods, price):
    """The ad of the main account as its last published feed has it (order_offers.ad_prices)."""
    if not connections.list_feed_accounts(conn):
        connections.create_account(conn, "Основной", ARRIVED, "o")
    account = connections.list_feed_accounts(conn)[0]["id"]
    conn.execute("INSERT INTO published_state (account_id, goods_id, price, base_price, point, snapshot) VALUES (?, ?, ?, 4000, 'Бета', '{}')",
                 (account, goods, price))
    conn.commit()


@pytest.mark.parametrize("ad,expected", [(5600.0, 5600.0), (4800.0, 5000.0), (None, 5000.0)])
def test_the_sale_price_is_the_records_or_the_ads_when_that_is_higher(tmp_path, ad, expected):
    conn = db(tmp_path)
    cid, x = split(conn)
    if ad is not None:
        feed_price(conn, "1", ad)
    b = new_order(conn, qty=4)
    take(conn, b, now=LATER)
    assert [i["sale_unit"] for i in orders.get_card(conn, b)["items"]] == [expected]


def test_a_part_of_a_record_is_never_taken(tmp_path):
    conn = db(tmp_path)
    cid, x = split(conn)
    b = new_order(conn, qty=2)
    assert offer(conn, b)["why"].startswith("С ОТХ берётся вся запись: 4 шт.")
    with pytest.raises(oth_sale.OthSaleError, match="С ОТХ берётся вся запись: 4 шт."):
        oth_sale.take(conn, b, orders.get_card(conn, b)["items"][0]["id"], orders.get_card(conn, x)["items"][0]["id"], LATER, "o")
    assert orders.get_card(conn, x)["stage"]["code"] == "oth" and oth_sale.taken_ids(conn, b) == set() and oth_stock.pieces(conn) == {"1": 4}


def test_a_record_of_two_positions_goes_whole_or_not_at_all(tmp_path):
    conn = db(tmp_path)
    cid = at_point(conn)
    orders.add_item(conn, cid, goods_id="2", name="Шина 2", brand_key="b", season="winter", diameter=16, territory="1", qty=2, sale_unit=7000,
                    supplier_price=4500, terms=None, fee_pct=0, promo_pct=0, now=ARRIVED, author="o")
    orders.move_card(conn, cid, stage(conn, "oth"), MOVED, "o", changes={"cancel_reason": "отказ"})
    x = record(conn, cid)
    one = new_order(conn, qty=4)
    assert offer(conn, one)["why"].startswith("С ОТХ берётся вся запись: 6 шт.")
    both = new_order(conn, qty=4, extra=(("2", 2),))
    assert take(conn, both, now=LATER) == 6
    assert sorted((i["goods_id"], i["qty"], i["sale_unit"]) for i in orders.get_card(conn, both)["items"]) == [("1", 4, 5000.0), ("2", 2, 7000.0)]
    assert orders.get_card(conn, x)["stage"]["code"] == "cancel" and orders.get_card(conn, both)["webmim_number"] == number(cid)


def test_new_client_on_the_way_back_to_the_point_makes_a_new_order(tmp_path):
    conn = db(tmp_path)
    cid, x = split(conn)
    b = oth_sale.to_new_client(conn, x, "Пётр", "+7 900 555 66 77", LATER, "o")
    assert b not in (cid, x)                                                             # never the record itself (oth_swap is not used)
    card = orders.get_card(conn, b)
    assert (card["client_name"], card["webmim_number"], card["transfer_number"]) == ("Пётр", number(cid), TRANSFER)
    assert oth_sale.all_from_oth(conn, card) and orders.get_card(conn, x)["stage"]["code"] == "cancel"
    assert orders.get_card(conn, x)["client_name"] == ""


def test_the_sale_of_the_new_order_is_counted_at_the_purchase_of_the_record(tmp_path):
    conn = db(tmp_path)
    cid, x = split(conn)
    b = new_order(conn, qty=4)
    take(conn, b, now=LATER)
    for code in ("at_point", "done"):
        orders.move_card(conn, b, stage(conn, code), "2026-10-22T10:00:00", "o")
    ids = orders.record_sales(conn, b, "2026-10-22", "2026-10-22T10:05:00", "o", "2026-10-22")
    assert set(ids) == oth_sale.sale_ids(conn)                                           # never at WebMIM's price of today
    assert [r[0] for r in conn.execute("SELECT cost_total FROM sale WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)] == [16000.0]


def test_the_new_client_refuses_too_and_the_tyres_are_a_record_again(tmp_path):
    conn = db(tmp_path)
    cid, x = split(conn)
    b = new_order(conn, qty=4)
    take(conn, b, now=LATER)
    orders.move_card(conn, b, stage(conn, "at_point"), "2026-10-22T10:00:00", "o")
    orders.move_card(conn, b, stage(conn, "oth"), "2026-10-24T10:00:00", "o", changes={"cancel_reason": "передумал"})
    y = record(conn, b)
    assert orders.get_card(conn, b)["stage"]["code"] == "cancel" and orders.get_card(conn, b)["webmim_number"] == number(cid) + " О"
    rec = orders.get_card(conn, y)
    assert rec["stage"]["code"] == "oth" and (rec["webmim_number"], rec["transfer_number"]) == (number(cid), TRANSFER)
    assert oth_stock.pieces(conn) == {"1": 4} and oth_sale.holds(conn, b) == [] and oth_sale.holds(conn, y) == [x]
    assert [(i["qty"], i["supplier_price"]) for i in rec["items"]] == [(4, 4000.0)]


# ---- the same client is back -------------------------------------------------------------------------------------------------------------

def test_the_same_client_gets_the_order_back(tmp_path):
    conn = db(tmp_path)
    cid, x = split(conn)
    assert oth_storage.waits(conn, orders.get_card(conn, cid))
    assert orders.move_card(conn, cid, stage(conn, "at_point"), LATER, "o")
    a, rec = orders.get_card(conn, cid), orders.get_card(conn, x)
    assert a["stage"]["code"] == "at_point" and (a["webmim_number"], a["transfer_number"], a["webmim_id"]) == (number(cid), TRANSFER, str(900000 + cid))
    assert a["cancel_reason"] == "" and a["reserve_until"] == "2026-10-24" and linked(conn, cid)
    assert rec["stage"]["code"] == "cancel" and rec["cancel_reason"] == "другое: Возвращено в заказ №%d" % cid
    assert (rec["webmim_number"], rec["transfer_number"], rec["webmim_id"]) == ("", "", "")
    assert oth_stock.pieces(conn) == {} and oth_storage.record_of(conn, cid) is None
    assert any(t.startswith("Заказ возвращён клиенту") for t in texts(conn, cid))
    orders.move_card(conn, cid, stage(conn, "oth"), "2026-10-25T10:00:00", "o", changes={"cancel_reason": "снова отказ"})
    assert record(conn, cid) != x and orders.get_card(conn, cid)["webmim_number"] == number(cid) + " О"      # a refusal again: a new record


def test_the_order_is_not_given_back_once_the_record_is_taken_or_goes_elsewhere(tmp_path):
    conn = db(tmp_path)
    cid, x = split(conn)
    with pytest.raises(orders.OrderError, match="только на этап"):
        orders.move_card(conn, cid, stage(conn, "placed"), LATER, "o")
    b = new_order(conn, qty=4)
    take(conn, b, now=LATER)
    with pytest.raises(orders.OrderError, match="уже взял заказ №%d" % b):
        orders.move_card(conn, cid, stage(conn, "at_point"), LATER, "o")
    a = orders.get_card(conn, cid)
    assert a["stage"]["code"] == "cancel" and a["webmim_number"] == number(cid) + " О"
    assert orders.get_card(conn, b)["webmim_number"] == number(cid)


def test_the_button_gives_the_order_back_from_the_record(tmp_path):
    app, owner, tok, conn = panel(tmp_path)
    cid = at_point(conn)
    orders.move_card(conn, cid, stage(conn, "oth"), MOVED, "o", changes={"cancel_reason": "отказ"})
    x = record(conn, cid)
    r = post(owner, "/orders/%d/storage-back" % x, tok)
    assert r.status_code == 302 and r.headers["Location"].endswith("/orders/%d" % cid)
    assert orders.get_card(conn, cid)["stage"]["code"] == "at_point" and orders.get_card(conn, x)["stage"]["code"] == "cancel"
    assert post(owner, "/orders/%d/storage-back" % x, tok).status_code == 302            # a second press: refused with its reason, nothing breaks
    assert orders.get_card(conn, cid)["stage"]["code"] == "at_point"


def test_the_points_undo_of_the_refusal_gives_the_order_back(tmp_path):
    from tires_sync import point_log
    conn = db(tmp_path)
    cid = at_point(conn)
    before = orders.get_card(conn, cid)
    mgr.refuse(conn, before, "Ошиблись размером", "менеджер Бета", MOVED, None)
    point_log.remember(conn, before, "refuse", before["point_id"], "менеджер Бета", MOVED)
    x = record(conn, cid)
    point_log.undo(conn, orders.get_card(conn, cid), before["point"], "менеджер Бета", LATER)
    a = orders.get_card(conn, cid)
    assert a["stage"]["code"] == "at_point" and a["webmim_number"] == number(cid) and a["reserve_until"] == before["reserve_until"]
    assert orders.get_card(conn, x)["stage"]["code"] == "cancel" and oth_stock.pieces(conn) == {}


# ---- the search and WebMIM ----------------------------------------------------------------------------------------------------------------

def test_the_search_by_the_number_finds_the_cancelled_order_and_the_one_holding_the_tyres(tmp_path):
    from datetime import datetime
    conn = db(tmp_path)
    cid, x = split(conn)
    scope = search.scope_for("owner")

    def found(q):
        groups = search.run(conn, q, scope, datetime(2026, 10, 21, 12, 0))["groups"]
        return sorted(int(i["url"].rsplit("/", 1)[1]) for g in groups if g["key"] == "orders" for i in g["items"])
    assert found(number(cid)) == [cid, x]
    assert found(number(cid) + " О") == [cid]
    wanted = orders.board_query(number(cid))
    assert orders.board_matches(wanted, cid, "Иван", "", "", number(cid) + " О") and orders.board_matches(wanted, x, "", "", "", number(cid))
    assert mgr.find_orders(conn, int(number(cid))) == [x]                                # the points' bot: the one that holds the tyres
    assert mgr.label(orders.get_card(conn, x)) == number(cid)


def test_the_clients_cabinet_shows_the_order_cancelled(tmp_path):
    conn = db(tmp_path)
    cid, _x = split(conn)
    assert shop_cart.STATUS[orders.get_card(conn, cid)["stage"]["code"]] == "Отменён"


# ---- the orders already on «На ОТХ» live on the old way ----------------------------------------------------------------------------------

def old_way(conn, monkeypatch):
    """An order put on «На ОТХ» before the release: the same order stands there, its client unlinked."""
    monkeypatch.setattr(oth_storage, "SPLIT", False)
    cid = at_point(conn)
    orders.move_card(conn, cid, stage(conn, "oth"), MOVED, "o", changes={"cancel_reason": "Клиент не пришёл"})
    monkeypatch.setattr(oth_storage, "SPLIT", True)
    assert orders.get_card(conn, cid)["stage"]["code"] == "oth" and not oth_storage.is_record(conn, cid)
    return cid


def test_an_old_order_on_oth_gives_a_part_and_takes_it_back(tmp_path, monkeypatch):
    conn = db(tmp_path)
    old = old_way(conn, monkeypatch)
    b = new_order(conn, qty=2)
    assert take(conn, b, now=LATER) == 2                                                 # a part: an old order is no record
    assert orders.get_card(conn, old)["stage"]["code"] == "oth" and oth_stock.pieces(conn) == {"1": 2}
    [item] = [i["id"] for i in orders.get_card(conn, b)["items"] if i["id"] in oth_sale.taken_ids(conn, b)]
    assert oth_sale.untake(conn, b, item, LATER, "o") == 2 and oth_stock.pieces(conn) == {"1": 4}


def test_an_old_order_on_oth_takes_a_new_client_itself_and_goes_back_to_its_own(tmp_path, monkeypatch):
    conn = db(tmp_path)
    old = old_way(conn, monkeypatch)
    assert oth_sale.to_new_client(conn, old, "Пётр", "+7 900 555 66 77", LATER, "o") == old          # the plan of 2026-11-06 (oth_swap)
    again = old_way(conn, monkeypatch)
    assert orders.move_card(conn, again, stage(conn, "at_point"), LATER, "o")            # the same client: the order itself, linked again
    assert orders.get_card(conn, again)["stage"]["code"] == "at_point" and linked(conn, again)
    assert orders.get_card(conn, again)["webmim_number"] == number(again) and oth_storage.record_ids(conn) == set()


def test_an_old_order_on_oth_goes_to_the_return_and_back_on_the_seventh_day(tmp_path, monkeypatch):
    conn = db(tmp_path)
    started(conn)
    old = old_way(conn, monkeypatch)
    assert order_oth.ask_seventh_day(conn, "2026-10-26T12:30:00") == 1
    card = orders.get_card(conn, old)
    assert card["stage"]["code"] == orders.RETURN_GIVEN
    assert orders.move_card(conn, old, stage(conn, "oth"), "2026-10-27T10:00:00", "o")   # back on sale by hand: the same order, no record
    assert orders.get_card(conn, old)["stage"]["code"] == "oth" and oth_storage.record_ids(conn) == set()
    assert orders.get_card(conn, old)["webmim_number"] == number(old)


def test_an_old_order_closed_by_a_take_stands_on_oth_again_itself(tmp_path, monkeypatch):
    conn = db(tmp_path)
    old = old_way(conn, monkeypatch)
    b = new_order(conn, qty=6)                                                           # holds a line of its own too: no swap of the client
    assert take(conn, b, now=LATER) == 4 and orders.get_card(conn, old)["stage"]["code"] == "cancel"
    [item] = oth_sale.taken_ids(conn, b)
    assert oth_sale.untake(conn, b, item, LATER, "o") == 4
    card = orders.get_card(conn, old)
    assert card["stage"]["code"] == "oth" and card["webmim_number"] == number(old) and oth_storage.record_ids(conn) == set()
    assert oth_stock.pieces(conn) == {"1": 4}


def test_a_record_taken_by_mistake_is_given_back_whole(tmp_path):
    conn = db(tmp_path)
    cid, x = split(conn)
    b = new_order(conn, qty=4)
    take(conn, b, now=LATER)
    [item] = oth_sale.taken_ids(conn, b)
    assert oth_sale.untake(conn, b, item, LATER, "o") == 4                               # «Вернуть на ОТХ»: the record holds them again
    rec = orders.get_card(conn, x)
    assert rec["stage"]["code"] == "oth" and oth_storage.is_record(conn, x) and oth_storage.record_ids(conn) == {x}
    assert (rec["webmim_number"], rec["transfer_number"]) == (number(cid), TRANSFER) and orders.get_card(conn, b)["webmim_number"] == ""
    assert oth_storage.why_not_back(conn, cid) is None and oth_stock.pieces(conn) == {"1": 4}


def test_a_record_comes_back_on_sale_from_the_return(tmp_path):
    conn = db(tmp_path)
    started(conn)
    cid, x = split(conn)
    order_oth.ask_seventh_day(conn, "2026-10-26T12:30:00")
    assert orders.get_card(conn, x)["stage"]["code"] == orders.RETURN_GIVEN
    assert orders.move_card(conn, x, stage(conn, "oth"), "2026-10-27T10:00:00", "o")     # «не возвращаем»: on sale again, the same record
    assert orders.get_card(conn, x)["stage"]["code"] == "oth" and oth_storage.record_ids(conn) == {x} and oth_stock.pieces(conn) == {"1": 4}
