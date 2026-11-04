"""The numbers of tyres taken from «На ОТХ» (the owner 2026-11-04, the order 515): the new order has the WebMIM number and the transfer number of
the order its tyres were bought by, no order of WebMIM of its own."""
from __future__ import annotations

import json

import pytest

from tests.test_order_reserve import card_at, db, stage
from tests.test_oth_sale import LATER, T, line, new_order, offer, take
from test_point_orders import ACCOUNT, _order, cheap_hashes, w  # noqa: F401  (fixtures)
from tires_sync import oth_manual, order_engine as engine, order_manager, order_merge, order_points, order_report, \
    order_templates as templates, orders, oth_sale, sales as sales_store, webmim_create, webmim_match

NUMBER, TRANSFER = "5254033", "26R-1400424"
OLD_NUMBERS = (NUMBER, TRANSFER)


def source(conn, number=NUMBER, transfer=TRANSFER, code="oth"):
    cid = card_at(conn, code, T)
    conn.execute("UPDATE order_card SET webmim_number = ?, transfer_number = ? WHERE id = ?", (number, transfer, cid))
    conn.commit()
    return cid


def numbers(conn, cid):
    return tuple(conn.execute("SELECT webmim_number, transfer_number FROM order_card WHERE id = ?", (cid,)).fetchone())


def journal(conn, cid):
    return [e["text"] for e in orders.list_events(conn, cid)]


def add_supplier(conn, cid, goods="2"):
    return orders.add_item(conn, cid, goods_id=goods, name="Шина " + goods, brand_key="b", season="winter", diameter=16, territory="1", qty=4,
                           sale_unit=6000, supplier_price=4500, terms=None, fee_pct=0, promo_pct=0, now=LATER, author="o")


def untake_all(conn, cid):
    for item_id in list(oth_sale.taken(conn, cid)):
        oth_sale.untake(conn, cid, item_id, LATER, "o")


# ---- the copy --------------------------------------------------------------------------------------------------------------------------

def test_a_take_gives_the_new_order_the_numbers_of_the_old_one_with_a_journal_line(tmp_path):
    conn = db(tmp_path)
    old = source(conn)
    cid = new_order(conn)
    assert take(conn, cid) == 4
    assert numbers(conn, cid) == OLD_NUMBERS and numbers(conn, old) == OLD_NUMBERS
    assert "Номер WebMIM и перемещение взяты из заказа №%d" % old in journal(conn, cid)
    card = orders.get_card(conn, cid)
    assert card["webmim_id"] == ""                                                  # no id of WebMIM: the order is the old one's
    assert oth_sale.borrowed_ids(conn) == {cid} and oth_sale.borrowed_ids(conn, cid) == {cid} and oth_sale.borrowed_ids(conn, old) == set()


def test_a_field_filled_in_is_left_and_only_the_empty_one_is_taken(tmp_path):
    conn = db(tmp_path)
    old = source(conn)
    cid = new_order(conn)
    orders.update_card(conn, cid, {"transfer_number": "МОЙ-1"}, T, "o")
    take(conn, cid)
    assert numbers(conn, cid) == (NUMBER, "МОЙ-1")
    assert "Номер WebMIM взят из заказа №%d" % old in journal(conn, cid)


def test_a_source_without_a_transfer_gives_the_number_alone(tmp_path):
    conn = db(tmp_path)
    old = source(conn, transfer="")
    cid = new_order(conn)
    take(conn, cid)
    assert numbers(conn, cid) == (NUMBER, "") and "Номер WebMIM взят из заказа №%d" % old in journal(conn, cid)


def test_positions_of_the_supplier_keep_it_empty_and_it_is_said(tmp_path):
    conn = db(tmp_path)
    source(conn)
    cid = new_order(conn, qty=6)                                                    # 4 from OTH, 2 stay the supplier's
    take(conn, cid)
    assert numbers(conn, cid) == ("", "")
    assert any("не взяты: в заказе есть позиции поставщика" in t for t in journal(conn, cid))
    assert not any("Впишите" in t for t in journal(conn, cid))                      # the next take may bring them: nobody is asked to type


def test_tyres_of_two_orders_keep_it_empty_and_a_number_taken_before_goes(tmp_path):
    conn = db(tmp_path)
    first = source(conn)
    second = source(conn, number="5254044", transfer="26R-2")
    cid = new_order(conn, qty=4, extra=(("1", 4),))                                 # two positions of the same tyre
    lines = [i for i in orders.get_card(conn, cid)["items"]]
    oth_sale.take(conn, cid, lines[0]["id"], orders.get_card(conn, first)["items"][0]["id"], LATER, "o")
    assert numbers(conn, cid) == ("", "")                                           # the second position is still the supplier's
    oth_sale.take(conn, cid, lines[1]["id"], orders.get_card(conn, second)["items"][0]["id"], LATER, "o")
    assert numbers(conn, cid) == ("", "") and any("разных источников" in t and "№%d" % first in t and "Впишите вручную" in t for t in journal(conn, cid))
    # one source first, then a second: what was taken goes
    one = new_order(conn, qty=2)
    third = source(conn, number="5254055", transfer="26R-3")
    oth_sale.take(conn, one, line(conn, one)["id"], orders.get_card(conn, third)["items"][0]["id"], LATER, "o")
    assert numbers(conn, one) == ("5254055", "26R-3")
    fourth = source(conn, number="5254066", transfer="26R-4")
    add_id = add_supplier(conn, one, goods="1")                                     # a new position: the number gives way at once
    assert numbers(conn, one) == ("", "")
    oth_sale.take(conn, one, add_id, orders.get_card(conn, fourth)["items"][0]["id"], LATER, "o")
    assert numbers(conn, one) == ("", "") and not oth_sale.borrowed_ids(conn, one)


def test_the_point_own_stock_has_no_numbers_and_nothing_is_said(tmp_path):
    conn = db(tmp_path)
    cid = new_order(conn)
    oth_manual.set_qty(conn, "1", orders.get_card(conn, cid)["point_id"], 4, T, "owner")
    take(conn, cid)
    assert numbers(conn, cid) == ("", "") and oth_sale.taken_ids(conn, cid)
    assert not any("Номер" in t for t in journal(conn, cid))


def test_the_point_own_stock_and_an_order_together_are_not_guessed(tmp_path):
    conn = db(tmp_path)
    old = source(conn)
    cid = new_order(conn, qty=4, extra=(("1", 2),))
    a, b = orders.get_card(conn, cid)["items"]
    oth_sale.take(conn, cid, a["id"], orders.get_card(conn, old)["items"][0]["id"], LATER, "o")
    oth_manual.set_qty(conn, "1", orders.get_card(conn, cid)["point_id"], 2, T, "owner")
    oth_sale.take(conn, cid, b["id"], offer(conn, cid)["from_item"], LATER, "o")
    assert numbers(conn, cid) == ("", "") and any("свой остаток" in t for t in journal(conn, cid))


def test_a_deal_of_the_point_stock_and_the_point_own_sale_get_no_number(tmp_path, monkeypatch):
    conn = db(tmp_path)
    source(conn)
    source(conn, number="5254044", transfer="26R-2")
    stock = new_order(conn)
    conn.execute("UPDATE order_card SET deal_kind = 'stock' WHERE id = ?", (stock,))
    conn.commit()
    take(conn, stock)
    assert numbers(conn, stock) == ("", "")
    own = new_order(conn)
    monkeypatch.setattr(order_points, "account_orders", lambda _c: {own})           # the order of the point's own Avito account
    pid = orders.get_card(conn, own)["point_id"]
    assert own in order_points.own_sales(conn, pid)
    take(conn, own)
    assert numbers(conn, own) == ("", "") and own in order_points.own_sales(conn, pid)       # still the point's


def test_a_chain_takes_the_number_of_the_order_of_purchase(tmp_path):
    conn = db(tmp_path)
    first = source(conn)
    middle = new_order(conn)
    take(conn, middle)
    conn.execute("UPDATE order_card SET webmim_number = '', transfer_number = '' WHERE id = ?", (middle,))     # B without a number
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (stage(conn, "oth"), middle))
    conn.commit()
    last = new_order(conn)
    oth_sale.take(conn, last, line(conn, last)["id"], next(iter(oth_sale.taken_ids(conn, middle))), LATER, "o")
    assert numbers(conn, last) == OLD_NUMBERS and "Номер WebMIM и перемещение взяты из заказа №%d" % first in journal(conn, last)


# ---- what the number must not change -----------------------------------------------------------------------------------------------------

def test_the_copied_number_lets_the_order_take_more_and_give_back(tmp_path):
    conn = db(tmp_path)
    source(conn)
    cid = new_order(conn, qty=2)
    take(conn, cid)
    card = orders.get_card(conn, cid)
    assert numbers(conn, cid)[0] == NUMBER and oth_sale.why_not(conn, card) is None and oth_sale.all_from_oth(conn, card)
    assert oth_sale.can_untake(conn, cid, next(iter(oth_sale.taken_ids(conn, cid))))


def test_a_number_written_by_a_person_still_blocks(tmp_path):
    conn = db(tmp_path)
    source(conn)
    cid = new_order(conn)
    orders.update_card(conn, cid, {"webmim_number": "777"}, T, "o")
    assert "уже есть в WebMIM" in oth_sale.why_not(conn, orders.get_card(conn, cid))
    with pytest.raises(orders.OrderError):
        take(conn, cid)
    assert numbers(conn, cid)[0] == "777"


def test_the_same_number_without_a_take_is_a_person_s_and_is_kept(tmp_path):
    conn = db(tmp_path)
    source(conn)
    cid = new_order(conn)
    orders.update_card(conn, cid, {"webmim_number": NUMBER}, T, "o")
    assert oth_sale.borrowed_ids(conn, cid) == set() and "уже есть в WebMIM" in oth_sale.why_not(conn, orders.get_card(conn, cid))
    add_supplier(conn, cid)
    assert numbers(conn, cid)[0] == NUMBER


# ---- untake ------------------------------------------------------------------------------------------------------------------------------

def test_untake_removes_what_was_taken(tmp_path):
    conn = db(tmp_path)
    old = source(conn)
    cid = new_order(conn)
    take(conn, cid)
    untake_all(conn, cid)
    assert numbers(conn, cid) == ("", "") and numbers(conn, old) == OLD_NUMBERS
    assert any("Номер WebMIM и перемещение убраны" in t for t in journal(conn, cid))
    assert orders.get_card(conn, old)["stage"]["code"] == "oth"


def test_untake_keeps_a_transfer_changed_by_a_person(tmp_path):
    conn = db(tmp_path)
    source(conn)
    cid = new_order(conn)
    take(conn, cid)
    orders.update_card(conn, cid, {"transfer_number": "ИСПРАВЛЕН"}, LATER, "o")
    untake_all(conn, cid)
    assert numbers(conn, cid) == ("", "ИСПРАВЛЕН")


def test_untake_keeps_them_while_another_line_needs_them(tmp_path):
    conn = db(tmp_path)
    old = source(conn)
    cid = new_order(conn, qty=2, extra=(("1", 2),))
    a, b = orders.get_card(conn, cid)["items"]
    from_item = orders.get_card(conn, old)["items"][0]["id"]
    oth_sale.take(conn, cid, a["id"], from_item, LATER, "o")
    oth_sale.take(conn, cid, b["id"], from_item, LATER, "o")
    assert numbers(conn, cid) == OLD_NUMBERS
    item_ids = sorted(oth_sale.taken_ids(conn, cid))
    oth_sale.untake(conn, cid, item_ids[0], LATER, "o")
    assert numbers(conn, cid) == OLD_NUMBERS
    oth_sale.untake(conn, cid, item_ids[1], LATER, "o")
    assert numbers(conn, cid) == ("", "")


def test_a_failed_take_leaves_no_number(tmp_path):
    conn = db(tmp_path)
    old = source(conn)
    cid = new_order(conn)
    real = orders.move_card
    def refuse(*_a, **_k):
        conn.rollback()
        return False
    orders.move_card = refuse
    try:
        with pytest.raises(oth_sale.OthSaleError):
            take(conn, cid)
    finally:
        orders.move_card = real
    assert numbers(conn, cid) == ("", "") and numbers(conn, old) == OLD_NUMBERS


# ---- positions added later -----------------------------------------------------------------------------------------------------------------

def test_a_position_of_the_supplier_added_takes_the_copy_away_and_removing_it_brings_it_back(tmp_path):
    conn = db(tmp_path)
    source(conn)
    cid = new_order(conn)
    take(conn, cid)
    item = add_supplier(conn, cid)
    assert numbers(conn, cid) == ("", "")
    assert any("убраны: в заказе есть позиции поставщика" in t for t in journal(conn, cid))
    orders.remove_item(conn, item, LATER, "o")
    assert numbers(conn, cid) == OLD_NUMBERS


def test_past_placed_a_position_added_can_still_be_ordered(tmp_path):
    """The tyres of the supplier need their own order of WebMIM: the copy never hides the button, at any open stage."""
    conn = db(tmp_path)
    source(conn)
    cid = new_order(conn)
    take(conn, cid)
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (stage(conn, "at_point"), cid))
    conn.commit()
    assert webmim_create.button(conn, orders.get_card(conn, cid), "prod") is None
    add_supplier(conn, cid)
    assert numbers(conn, cid) == ("", "")
    assert webmim_create.button(conn, orders.get_card(conn, cid), "prod")["label"] == "Оформить в WebMIM"


def test_a_closed_order_keeps_its_numbers(tmp_path):
    conn = db(tmp_path)
    source(conn)
    cid = new_order(conn)
    take(conn, cid)
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (stage(conn, "cancel"), cid))
    conn.commit()
    add_supplier(conn, cid)
    assert numbers(conn, cid) == OLD_NUMBERS


def test_a_merged_order_keeps_the_transfer_it_got_from_its_source(tmp_path):
    conn = db(tmp_path)
    old = source(conn)
    merged = new_order(conn, qty=1, goods="9")
    conn.execute("UPDATE order_card SET transfer_number = ? WHERE id = ?", (TRANSFER, merged))
    item = orders.get_card(conn, merged)["items"][0]["id"]
    conn.execute("INSERT INTO order_merge (into_order, at, author) VALUES (?, ?, 'o')", (merged, T))
    conn.execute("INSERT INTO order_item_oth (item_id, order_id, from_item, from_order, qty, at, author) VALUES (?,?,?,?,1,?, 'o')",
                 (item, merged, orders.get_card(conn, old)["items"][0]["id"], old, T))
    conn.commit()
    assert order_merge.is_target(conn, merged)
    add_supplier(conn, merged)
    assert numbers(conn, merged) == ("", TRANSFER)


def test_a_deal_of_the_point_stock_keeps_a_transfer_typed_by_a_person(tmp_path):
    conn = db(tmp_path)
    source(conn)
    cid = new_order(conn)
    conn.execute("UPDATE order_card SET deal_kind = 'stock', transfer_number = 'МОЙ-1' WHERE id = ?", (cid,))
    conn.commit()
    take(conn, cid)
    untake_all(conn, cid)
    assert numbers(conn, cid) == ("", "МОЙ-1")


def test_a_copy_goes_with_the_tyres_after_the_deal_became_the_point_stock(tmp_path):
    """Left behind it would be the order's «own» number: two orders on one order of WebMIM, and no price for either."""
    conn = db(tmp_path)
    source(conn)
    cid = new_order(conn)
    take(conn, cid)
    orders.update_card(conn, cid, {"deal_kind": "stock"}, LATER, "o")
    untake_all(conn, cid)
    assert numbers(conn, cid) == ("", "")


def test_a_press_of_webmim_going_on_is_not_given_a_copy(tmp_path):
    """_write_order would not write the made order over it: it would stay unbound to the card."""
    conn = db(tmp_path)
    source(conn)
    cid = new_order(conn, qty=6)
    take(conn, cid)
    assert numbers(conn, cid) == ("", "")
    conn.execute("INSERT INTO webmim_create (order_id, server, state, first_at, author, started_at, updated_at) VALUES (?, 'prod', 'running', ?, 'o', ?, ?)", (cid, T, T, T))
    conn.commit()
    orders.remove_item(conn, line(conn, cid)["id"], LATER, "o")
    assert numbers(conn, cid) == ("", "")


def test_a_position_added_on_oth_takes_the_copy_away_too(tmp_path):
    conn = db(tmp_path)
    source(conn)
    cid = new_order(conn)
    take(conn, cid)
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (stage(conn, "oth"), cid))
    conn.commit()
    add_supplier(conn, cid)
    assert numbers(conn, cid) == ("", "")


# ---- the message --------------------------------------------------------------------------------------------------------------------------

def test_the_waiting_message_becomes_sendable_with_the_right_numbers(tmp_path):
    conn = db(tmp_path)
    source(conn)
    templates.add_samples(conn)
    conn.execute("UPDATE point SET address = 'Улица, 1' WHERE name = 'Бета'")
    conn.commit()
    template = next(t for t in templates.list_templates(conn) if t["name"] == "Клиенту: заказ оформлен")
    cid = new_order(conn)
    conn.execute("INSERT INTO order_task (order_id, rule_id, entered_at, kind, arg, stage_id, status, title, body, result, created_at) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (cid, -2, T, "message", str(template["id"]), stage(conn, "new"), "error", template["name"], "",
                                                     "Не хватает: Номер заказа WebMIM.", T))
    conn.commit()
    task = next(t for t in engine.list_tasks(conn, cid) if t["kind"] == "message")
    engine.refresh_tasks(conn, cid, T)
    assert engine.get_task(conn, task["id"])["status"] == "error"
    take(conn, cid)
    engine.refresh_tasks(conn, cid, LATER)
    done = engine.get_task(conn, task["id"])
    assert done["status"] == "pending" and done["result"] == "" and "Заказ %s оформлен" % NUMBER in done["body"] and "[" not in done["body"]
    text, missing = templates.render(conn, orders.get_card(conn, cid), template["body"], LATER)
    assert missing == [] and NUMBER in text


# ---- the rest of the system -------------------------------------------------------------------------------------------------------------

def webmim_snapshot(conn, order_id="900", number=NUMBER, goods="1", price=5000.0):
    connections_config(conn)
    conn.execute("INSERT INTO webmim_order (server, id, number, items, seen_at, items_at, create_date) VALUES ('prod',?,?,?,?,?,?)",
                 (order_id, number, json.dumps([{"goods_id": goods, "name": "Nokian", "qty": 4, "price": price}]), T, T, "2026-10-29T10:00:00"))
    conn.commit()


def connections_config(conn):
    from tires_sync import connections
    connections.set_config(conn, "webmim_orders", 0, {"server": "prod"}, T, "owner")


def test_the_webmim_order_stays_paired_with_the_first_order_only(tmp_path):
    conn = db(tmp_path)
    old = source(conn)
    cid = new_order(conn)
    take(conn, cid)
    webmim_snapshot(conn)
    snap = webmim_match.snapshot(conn, "prod")
    assert webmim_match.shared(conn, snap) == {} and webmim_match.taken(conn, snap) == {"900": old}
    block = webmim_match.card_block(conn, orders.get_card(conn, cid))
    assert block["borrowed"] and block["order"] is None and not block["not_found"] and block["differences"] == [] and block["hints"] == []
    assert webmim_match.card_block(conn, orders.get_card(conn, old))["order"]["id"] == "900"
    from datetime import date
    report = webmim_match.reconcile(conn, date(2026, 10, 29))
    assert report["shared"] == [] and cid not in {c["id"] for c in report["not_found"] + report["unpaired"]}


def test_no_purchase_is_counted_twice_or_lost(tmp_path, monkeypatch):
    conn = db(tmp_path)
    old = source(conn)
    cid = new_order(conn, qty=2)                                                    # 2 of 4: the old order keeps the other two
    take(conn, cid)
    assert numbers(conn, cid)[0] == NUMBER
    webmim_snapshot(conn)
    assert order_report.webmim_prices(conn, {old, cid}) == {old: {"1": 5000.0}}     # the old order's price stays, the new one has none
    # the sale of the new order: at the purchase written, one sale per position, never at WebMIM's price
    monkeypatch.setattr(order_report, "WEBMIM_FROM", "2000-01-01")
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (stage(conn, "done"), cid))
    conn.commit()
    ids = orders.record_sales(conn, cid, "2026-11-04", T, "o", "2026-11-04")
    names = [c.strip() for c in sales_store.COLUMNS.split(",")]
    rows = [dict(zip(names, r)) for r in conn.execute("SELECT %s FROM sale ORDER BY id" % sales_store.COLUMNS)]
    assert len(rows) == 1 and rows[0]["pieces"] == 2 and ids
    counted = order_report._counted(conn, rows)
    assert len(counted) == 1 and not counted[0].get("webmim") and counted[0]["price_total"] == rows[0]["price_total"]
    assert orders.get_card(conn, old)["sale_ids"] == []


def test_the_point_s_word_means_the_new_order_when_the_old_one_gave_all_its_tyres(tmp_path):
    conn = db(tmp_path)
    old = source(conn)
    cid = new_order(conn)
    take(conn, cid)
    assert orders.get_card(conn, old)["stage"]["code"] == "cancel"
    assert order_manager.find_orders(conn, int(NUMBER)) == [cid]


def test_two_orders_of_one_number_are_a_dispute_but_the_panel_number_still_names_each(tmp_path):
    conn = db(tmp_path)
    old = source(conn)
    b = new_order(conn, qty=2)
    take(conn, b)
    c = new_order(conn, qty=2)
    take(conn, c)
    assert orders.get_card(conn, old)["stage"]["code"] == "cancel"
    assert order_manager.find_orders(conn, int(NUMBER)) == [b, c]
    assert order_manager.find_orders(conn, b) == [b] and order_manager.find_orders(conn, c) == [c]
    assert order_manager.find_orders(conn, old) == []                               # its number is its own: the panel's is not known


def test_the_point_s_word_stays_a_dispute_while_the_old_one_holds_tyres(tmp_path):
    conn = db(tmp_path)
    old = source(conn)
    cid = new_order(conn, qty=2)
    take(conn, cid)
    assert order_manager.find_orders(conn, int(NUMBER)) == sorted([old, cid])


def test_a_copied_number_does_not_let_a_point_bind_its_chat_to_the_manager_s_order(w):
    conn, made, alfa = w["conn"], w["made"], w["alfa"]
    goods = dict(goods_id="1", name="Шина", brand_key="b", season="winter", diameter=16, territory="1", qty=4, sale_unit=6000, supplier_price=4500,
                 terms=None, fee_pct=0, promo_pct=0, now=T, author="o")
    old = _order(conn, w["stages"], alfa, "oth", "Старый", webmim_number=NUMBER)
    orders.add_item(conn, old, **goods)
    orders.add_item(conn, made["ours_new"], **goods)
    conn.commit()
    take(conn, made["ours_new"])
    assert numbers(conn, made["ours_new"])[0] == NUMBER and made["ours_new"] not in order_points.own_sales(conn, alfa)
    chat = conn.execute("INSERT INTO avito_chat (account_id, chat_id, peer_id, peer_name, unread) VALUES (?, 'c9', 'p9', 'К', 0)", (ACCOUNT,)).lastrowid
    conn.commit()
    w["point"].post("/chats/%d/link" % chat, data={"csrf": "tok", "order_id": str(made["ours_new"])})
    assert conn.execute("SELECT order_id FROM avito_chat WHERE id = ?", (chat,)).fetchone()[0] is None


# ---- a number typed by hand, a number changed at the source (the owner 2026-11-04) -----------------------------------------------------------

def test_a_transfer_typed_before_the_take_equal_to_the_source_is_kept_when_a_position_is_added(tmp_path):
    conn = db(tmp_path)
    source(conn)
    cid = new_order(conn)
    orders.update_card(conn, cid, {"transfer_number": TRANSFER}, T, "o")
    take(conn, cid)
    assert numbers(conn, cid) == OLD_NUMBERS
    add_supplier(conn, cid)
    assert numbers(conn, cid) == ("", TRANSFER)                                     # the copied number gives way, the typed one stays


def test_a_number_typed_when_the_journal_asked_for_it_is_kept(tmp_path):
    """Tyres of two orders: «Впишите вручную», and the person types the number of one of them."""
    conn = db(tmp_path)
    first = source(conn)
    other = source(conn, number="111", transfer="T-111")
    cid = new_order(conn, qty=4, extra=(("1", 4),))
    a, b = orders.get_card(conn, cid)["items"]
    oth_sale.take(conn, cid, a["id"], orders.get_card(conn, first)["items"][0]["id"], LATER, "o")
    oth_sale.take(conn, cid, b["id"], orders.get_card(conn, other)["items"][0]["id"], LATER, "o")
    assert numbers(conn, cid) == ("", "") and any("Впишите вручную" in t for t in journal(conn, cid))
    orders.update_card(conn, cid, {"webmim_number": NUMBER, "transfer_number": "T-111"}, LATER, "o")
    add_supplier(conn, cid)
    assert numbers(conn, cid) == (NUMBER, "T-111")
    oth_sale.untake(conn, cid, min(oth_sale.taken_ids(conn, cid)), LATER, "o")
    assert numbers(conn, cid) == (NUMBER, "T-111")


def test_a_copy_changed_by_hand_to_what_the_source_became_later_is_kept(tmp_path):
    conn = db(tmp_path)
    old = source(conn)
    cid = new_order(conn)
    take(conn, cid)
    orders.update_card(conn, cid, {"webmim_number": "999"}, LATER, "o")
    orders.update_card(conn, old, {"webmim_number": "999"}, LATER, "o")
    add_supplier(conn, cid)
    assert numbers(conn, cid) == ("999", "")


def test_a_copy_changed_by_hand_and_changed_back_is_the_person_s(tmp_path):
    conn = db(tmp_path)
    source(conn)
    cid = new_order(conn)
    take(conn, cid)
    orders.update_card(conn, cid, {"transfer_number": "ДРУГОЙ"}, LATER, "o")
    orders.update_card(conn, cid, {"transfer_number": TRANSFER}, LATER, "o")
    add_supplier(conn, cid)
    assert numbers(conn, cid) == ("", TRANSFER)


def test_a_number_changed_at_the_source_reaches_the_new_order(tmp_path):
    conn = db(tmp_path)
    old = source(conn)
    cid = new_order(conn)
    take(conn, cid)
    orders.update_card(conn, old, {"webmim_number": "7777777"}, LATER, "o")
    assert numbers(conn, cid) == ("7777777", TRANSFER)
    assert oth_sale.borrowed_ids(conn, cid) == {cid}                                # still not an order of WebMIM of its own
    assert oth_sale.why_not(conn, orders.get_card(conn, cid)) is None
    assert "Номер WebMIM взят из заказа №%d" % old in journal(conn, cid)
    orders.update_card(conn, old, {"transfer_number": "26R-2"}, LATER, "o")
    assert numbers(conn, cid) == ("7777777", "26R-2")


def test_a_number_that_appears_at_the_source_after_the_take_reaches_the_new_order(tmp_path):
    conn = db(tmp_path)
    old = source(conn, number="", transfer="")
    cid = new_order(conn)
    take(conn, cid)
    assert numbers(conn, cid) == ("", "")
    orders.update_card(conn, old, {"webmim_number": NUMBER, "transfer_number": TRANSFER}, LATER, "o")
    assert numbers(conn, cid) == OLD_NUMBERS


def test_a_number_typed_in_the_new_order_wins_over_the_source(tmp_path):
    conn = db(tmp_path)
    old = source(conn)
    cid = new_order(conn)
    take(conn, cid)
    orders.update_card(conn, cid, {"transfer_number": "МОЙ-1"}, LATER, "o")
    orders.update_card(conn, old, {"webmim_number": "7777777", "transfer_number": "26R-2"}, LATER, "o")
    assert numbers(conn, cid) == ("7777777", "МОЙ-1")


def test_a_number_taken_away_at_the_source_leaves_the_new_order_empty_not_stale(tmp_path):
    conn = db(tmp_path)
    old = source(conn)
    cid = new_order(conn)
    take(conn, cid)
    orders.update_card(conn, old, {"webmim_number": ""}, LATER, "o")
    assert numbers(conn, cid) == ("", TRANSFER)
    assert any("Номер WebMIM убран: в заказе №%d его больше нет" % old in t for t in journal(conn, cid))


def test_a_closed_order_follows_a_correction_but_keeps_a_number_with_nothing_in_its_place(tmp_path):
    conn = db(tmp_path)
    old = source(conn)
    cid = new_order(conn)
    take(conn, cid)
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (stage(conn, "cancel"), cid))
    conn.commit()
    orders.update_card(conn, old, {"webmim_number": "7777777"}, LATER, "o")
    assert numbers(conn, cid) == ("7777777", TRANSFER)                              # one order of WebMIM: its right number
    orders.update_card(conn, old, {"webmim_number": ""}, LATER, "o")
    assert numbers(conn, cid) == ("7777777", TRANSFER)


def test_a_source_change_does_not_touch_a_press_of_webmim_going_on(tmp_path):
    conn = db(tmp_path)
    old = source(conn)
    cid = new_order(conn)
    take(conn, cid)
    conn.execute("INSERT INTO webmim_create (order_id, server, state, first_at, author, started_at, updated_at) VALUES (?, 'prod', 'running', ?, 'o', ?, ?)", (cid, T, T, T))
    conn.commit()
    orders.update_card(conn, old, {"webmim_number": "7777777"}, LATER, "o")
    assert numbers(conn, cid) == OLD_NUMBERS


def test_a_source_change_leaves_an_order_that_gave_the_tyres_back_alone(tmp_path):
    conn = db(tmp_path)
    old = source(conn)
    gave_back = new_order(conn)
    take(conn, gave_back)
    untake_all(conn, gave_back)
    orders.update_card(conn, old, {"webmim_number": "7777777"}, LATER, "o")
    assert numbers(conn, gave_back) == ("", "")


def test_a_chain_follows_the_order_of_purchase(tmp_path):
    conn = db(tmp_path)
    first = source(conn)
    second = new_order(conn)
    take(conn, second)
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (stage(conn, "oth"), second))
    conn.commit()
    third = new_order(conn, goods="1")
    oth_sale.take(conn, third, line(conn, third)["id"], orders.get_card(conn, second)["items"][0]["id"], LATER, "o")
    assert numbers(conn, third) == OLD_NUMBERS
    orders.update_card(conn, first, {"webmim_number": "7777777"}, LATER, "o")
    assert numbers(conn, third)[0] == "7777777"


def message_task(conn, cid):
    templates.add_samples(conn)
    conn.execute("UPDATE point SET address = 'Улица, 1' WHERE name = 'Бета'")
    template = next(t for t in templates.list_templates(conn) if t["name"] == "Клиенту: заказ оформлен")
    conn.execute("INSERT INTO order_task (order_id, rule_id, entered_at, kind, arg, stage_id, status, title, body, result, created_at) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (cid, -2, T, "message", str(template["id"]), stage(conn, "new"), "pending", template["name"], "",
                                                     "", T))
    conn.commit()
    return next(t for t in engine.list_tasks(conn, cid) if t["kind"] == "message")["id"]


def test_the_message_of_the_new_order_goes_with_the_current_number_never_a_stale_or_empty_one(tmp_path):
    conn = db(tmp_path)
    old = source(conn)
    cid = new_order(conn)
    take(conn, cid)
    task = message_task(conn, cid)
    engine.refresh_tasks(conn, cid, LATER)
    assert "Заказ %s оформлен" % NUMBER in engine.get_task(conn, task)["body"]
    orders.update_card(conn, old, {"webmim_number": "7777777"}, LATER, "o")
    sent = []

    def deliver(card, text, prefer=None):
        sent.append(text)
        return 1
    engine.refresh_tasks(conn, cid, LATER)
    body = engine.get_task(conn, task)["body"]
    assert "Заказ 7777777 оформлен" in body and NUMBER not in body
    orders.update_card(conn, old, {"webmim_number": ""}, LATER, "o")
    with pytest.raises(orders.OrderError):                                          # refreshed before the send: nothing leaves without the number
        engine.send_task(conn, task, body, deliver, LATER, "o")
    assert sent == [] and engine.get_task(conn, task)["status"] == "error"
    orders.update_card(conn, cid, {"webmim_number": "РУКОЙ-5"}, LATER, "o")         # typed by hand: the message names it, the source does not change it
    orders.update_card(conn, old, {"webmim_number": "8888888"}, LATER, "o")
    engine.refresh_tasks(conn, cid, LATER)
    assert "Заказ РУКОЙ-5 оформлен" in engine.get_task(conn, task)["body"]


def test_the_point_is_told_the_current_number_of_the_source(tmp_path):
    conn = db(tmp_path)
    old = source(conn)
    cid = new_order(conn)
    take(conn, cid)
    assert templates._oth_source(conn, cid) == "Шины с ОТХ (заказ №%s)" % NUMBER
    orders.update_card(conn, old, {"webmim_number": "7777777"}, LATER, "o")
    assert templates._oth_source(conn, cid) == "Шины с ОТХ (заказ №7777777)"
    orders.update_card(conn, old, {"webmim_number": ""}, LATER, "o")
    assert templates._oth_source(conn, cid) == "Шины с ОТХ (заказ №%d)" % old       # the panel's number, never an empty one


def test_an_order_that_gave_all_its_tyres_on_follows_the_source_and_stays_borrowed(tmp_path):
    """A → B → C, B gave everything: it has no pieces, its copy still means A's order of WebMIM."""
    conn = db(tmp_path)
    first = source(conn)
    second = new_order(conn)
    take(conn, second)
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (stage(conn, "oth"), second))
    conn.commit()
    third = new_order(conn, goods="1")
    oth_sale.take(conn, third, line(conn, third)["id"], orders.get_card(conn, second)["items"][0]["id"], LATER, "o")
    orders.update_card(conn, first, {"webmim_number": "7777777"}, LATER, "o")
    assert numbers(conn, second)[0] == "7777777" and second in oth_sale.borrowed_ids(conn)


def test_a_copy_left_on_a_closed_order_is_still_not_its_own_order_of_webmim(tmp_path):
    conn = db(tmp_path)
    old = source(conn)
    cid = new_order(conn)
    take(conn, cid)
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (stage(conn, "cancel"), cid))
    conn.commit()
    orders.update_card(conn, old, {"webmim_number": ""}, LATER, "o")
    assert numbers(conn, cid)[0] == NUMBER and oth_sale.borrowed_ids(conn, cid) == {cid}


def test_a_number_typed_by_hand_that_differs_from_the_source_is_the_order_s_own(tmp_path):
    conn = db(tmp_path)
    source(conn)
    cid = new_order(conn)
    take(conn, cid)
    orders.update_card(conn, cid, {"webmim_number": "РУКОЙ"}, LATER, "o")
    assert oth_sale.borrowed_ids(conn, cid) == set()

