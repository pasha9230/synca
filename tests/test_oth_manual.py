"""«Свой остаток (ОТХ)» (oth_manual, the owner 2026-11-03): tyres of ours at a point entered by hand in the catalog card count as «На ОТХ» —
the ad is held or brought back with «Шины в наличии на …» and the count, «Взять с ОТХ» takes them; a position the supplier has no more goes
to Avito on them only at an own price of the account, under the Id it had; 0 / removed: the usual rules. Only the owner and the partner."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

import pytest

from panel_helpers import login, make_app, post
from tests.test_oth_feed import (ad_of, ads_of, count, description, events, on_oth, passed, prepared, public, published, run, shown,
                                 snapshot)
from tests.test_oth_sale import new_order, line, items, T, LATER, BUY_NOW
from tests.test_order_reserve import card_at, db
from tests.test_roles import person
from tests.test_stock_pass import AFTER, FIVE, rows
from tires_sync import oth_manual, oth_sale, oth_stock, order_oth, order_points, orders, settings_store, store, users

NOW = "2026-11-03T10:00:00"
AT_YUNONA = oth_stock.FIRST_LINE + " на Юнона"


def connect(tmp_path):
    return store.connect(tmp_path / "t.db")


def a_point(conn, name="Юнона"):
    found = conn.execute("SELECT id FROM point WHERE name = ?", (name,)).fetchone()
    return found[0] if found else order_points.save_point(conn, dict(name=name, manager_name="М", channel="vk",
                                                                     vk_peer_id=str(100700 + len(order_points.list_points(conn)))))


def manual(tmp_path, code, qty, point="Юнона"):
    conn = connect(tmp_path)
    try:
        oth_manual.set_qty(conn, code, a_point(conn, point), qty, NOW, "owner")
    finally:
        conn.close()


def own(tmp_path, overrides):
    conn = connect(tmp_path)
    try:
        draft = settings_store.create_draft(conn, {"price_overrides": overrides}, NOW)
        settings_store.confirm_draft(conn, draft, NOW, "owner", strong=True)
    finally:
        conn.close()


def price_of(data, ad_id):
    return ad_of(data, ad_id).findtext("Price")


# ---- the rows ----------------------------------------------------------------------------------------------------------------------

def test_set_change_remove_and_the_log(tmp_path):
    conn = db(tmp_path)
    pid = a_point(conn)
    assert oth_manual.set_qty(conn, "102", pid, 3, NOW, "owner", "  с  витрины ") == 0
    assert oth_manual.set_qty(conn, "102", pid, 5, LATER, "zaman") == 3
    assert [(r["point"], r["qty"], r["author"]) for r in oth_manual.rows(conn, "102")] == [("Юнона", 5, "zaman")]
    assert oth_stock.pieces(conn) == {"102": 5} and oth_stock.points(conn) == {"102": ("Юнона",)}
    assert oth_stock.pieces(conn, manual=False) == {} and order_oth.goods_on_oth(conn) == {"102": 5}
    assert oth_manual.set_qty(conn, "102", pid, 0, LATER, "owner") == 5
    assert oth_manual.rows(conn, "102") == [] and oth_stock.pieces(conn) == {}
    assert [(l["before"], l["after"], l["why"]) for l in oth_manual.log_of(conn, "102")] == [(5, 0, "hand"), (3, 5, "hand"), (0, 3, "hand")]
    assert oth_manual.log_of(conn, "102")[-1]["note"] == "с витрины"
    assert conn.execute("SELECT COUNT(*) FROM audit WHERE action = 'oth_manual'").fetchone()[0] == 3


def test_a_test_point_a_wrong_number_and_an_unknown_point_are_refused(tmp_path):
    conn = db(tmp_path)
    with pytest.raises(oth_manual.ManualError):
        oth_manual.set_qty(conn, "102", a_point(conn, "Тест склад"), 3, NOW, "owner")
    with pytest.raises(oth_manual.ManualError):
        oth_manual.set_qty(conn, "102", 9999, 3, NOW, "owner")
    for bad in ("-1", "2.5", "", "abc", "100001", "٣"):
        with pytest.raises(oth_manual.ManualError):
            oth_manual.pieces_of(bad)
    assert oth_manual.pieces_of(" 4 ") == 4 and oth_manual.pieces_of("0") == 0
    pid = a_point(conn, "Склад")
    oth_manual.set_qty(conn, "102", pid, 3, NOW, "owner")
    store.set_state(conn, oth_stock.TEST_POINTS_KEY, "[%d]" % pid)        # a point named a test one later: not counted
    assert oth_stock.pieces(conn) == {}


# ---- the feed ----------------------------------------------------------------------------------------------------------------------

def test_a_gone_position_with_manual_stock_stays_under_its_id_at_the_own_price_only_where_there_is_one(tmp_path, capsys):
    prepared(tmp_path, price_overrides={"1:102": {"price": 7000}})
    assert run(tmp_path, FIVE) == 0
    first_id = {1: "102", 2: "102_a2"}
    assert all(first_id[a] in ads_of(public(tmp_path, a)) for a in (1, 2))
    manual(tmp_path, "102", 3)
    capsys.readouterr()
    assert run(tmp_path, AFTER) == 0                                     # 102 left the unload
    passed(capsys)                                                       # the check before publication passes
    data = public(tmp_path, 1)
    assert "102" in ads_of(data) and price_of(data, "102") == "7000"     # the same Id, the own price
    text = description(data, "102")
    assert text.startswith(AT_YUNONA + "\n") and text.count(oth_stock.FIRST_LINE) == 1 and shown(text) == count(3)
    assert "7000 руб" in text
    assert "102_a2" not in ads_of(public(tmp_path, 2))                   # no own price in the second account: not on Avito
    assert ("102", "removed", "absent_from_supplier") in events(tmp_path, 2) and events(tmp_path, 1) == [("101", "removed", "low_stock")]
    assert run(tmp_path, AFTER) == 0                                     # told once: the journal and the log say which account and why
    conn = connect(tmp_path)
    told = [__import__("json").loads(r[0]) for r in conn.execute("SELECT detail FROM audit WHERE action = 'oth_manual_no_price'")]
    assert told == [{"account": 2, "goods_id": "102", "qty": 3}]
    assert [l["why"] for l in oth_manual.log_of(conn, "102") if l["why"].startswith("no_price")] == ["no_price:2"]
    conn.close()
    manual(tmp_path, "102", 0)                                           # removed: the usual rules
    assert run(tmp_path, AFTER) == 0
    assert "102" not in ads_of(public(tmp_path, 1)) and ("102", "removed", "absent_from_supplier") in events(tmp_path, 1)


def test_a_gone_position_taken_off_comes_back_under_its_former_id_and_never_a_new_one(tmp_path, capsys):
    prepared(tmp_path, accounts=("A",))
    assert run(tmp_path, FIVE) == 0
    assert run(tmp_path, AFTER) == 0                                     # 102 taken off: its last ad kept (retired_ad)
    assert "102" not in ads_of(public(tmp_path))
    manual(tmp_path, "102", 2)
    assert run(tmp_path, AFTER) == 0                                     # no own price: it stays off
    assert "102" not in ads_of(public(tmp_path))
    own(tmp_path, {"1:102": {"price": 6800}})
    capsys.readouterr()
    assert run(tmp_path, AFTER) == 0
    passed(capsys)
    data = public(tmp_path)
    assert "102" in ads_of(data) and price_of(data, "102") == "6800" and description(data, "102").startswith(AT_YUNONA + "\n")
    assert shown(description(data, "102")) == count(2) and "102" in published(tmp_path)


def test_a_gone_position_whose_former_id_differs_is_not_published(tmp_path):
    prepared(tmp_path, accounts=("A",), price_overrides={"1:102": {"price": 6800}})
    assert run(tmp_path, FIVE) == 0
    assert run(tmp_path, AFTER) == 0
    conn = connect(tmp_path)
    conn.execute("UPDATE retired_ad SET ad_id = '102_old' WHERE goods_id = '102'")
    conn.commit()
    conn.close()
    manual(tmp_path, "102", 2)
    assert run(tmp_path, AFTER) == 0
    assert not any(i.startswith("102") for i in ads_of(public(tmp_path)))


def test_a_percent_own_price_of_a_gone_position_is_counted_from_the_last_purchase(tmp_path):
    prepared(tmp_path, accounts=("A",), price_overrides={"1:102": {"pct": 10}})
    assert run(tmp_path, FIVE) == 0
    conn = connect(tmp_path)
    last = conn.execute("SELECT last_price FROM catalog_item WHERE goods_id = '102'").fetchone()[0]
    conn.close()
    manual(tmp_path, "102", 4)
    assert run(tmp_path, AFTER) == 0
    import math
    assert price_of(public(tmp_path), "102") == str(int(math.ceil(last * 1.1 / 10) * 10))


def test_with_no_archive_row_the_saved_ad_is_held_at_the_own_price(tmp_path, capsys):
    prepared(tmp_path, accounts=("A",))
    assert run(tmp_path, FIVE) == 0
    old = price_of(public(tmp_path), "102")
    conn = connect(tmp_path)
    conn.execute("DELETE FROM catalog_item WHERE goods_id = '102'")
    conn.commit()
    conn.close()
    own(tmp_path, {"1:102": {"price": 7300}})
    manual(tmp_path, "102", 3)
    capsys.readouterr()
    assert run(tmp_path, AFTER) == 0
    passed(capsys)
    data = public(tmp_path)
    text = description(data, "102")
    assert price_of(data, "102") == "7300" and text.startswith(AT_YUNONA + "\n") and shown(text) == count(3)
    assert "7300 руб" in text and old + " руб" not in text


def test_manual_stock_of_a_position_in_the_unload_below_the_threshold_publishes_it_with_both_counts(tmp_path, capsys):
    prepared(tmp_path, accounts=("A",))
    assert run(tmp_path, FIVE) == 0
    assert run(tmp_path, AFTER) == 0                                     # 101 fell to 2: off
    assert "101" not in ads_of(public(tmp_path))
    manual(tmp_path, "101", 3)
    on_oth(tmp_path, "101", qty=4)                                       # and an order on OTH at «Бета»
    capsys.readouterr()
    assert run(tmp_path, AFTER) == 0
    passed(capsys)
    text = description(public(tmp_path), "101")
    assert text.startswith(oth_stock.FIRST_LINE + " на Бета, Юнона\n") and shown(text) == count(3 + 4 + 2)


def test_a_manual_off_still_keeps_a_gone_position_with_manual_stock_off(tmp_path):
    from tires_sync.overrides import override_key
    prepared(tmp_path, accounts=("A",), price_overrides={"1:102": {"price": 7000}}, overrides={override_key("102"): "off"})
    assert run(tmp_path, FIVE) == 0
    manual(tmp_path, "102", 3)
    assert run(tmp_path, AFTER) == 0
    assert "102" not in ads_of(public(tmp_path))


# ---- «Взять с ОТХ» --------------------------------------------------------------------------------------------------------------

def manual_db(tmp_path, qty=3, point="Бета"):
    conn = db(tmp_path)
    card_at(conn, "placed", T)                                           # makes the point «Бета»
    conn.execute("DELETE FROM order_card")
    conn.execute("DELETE FROM order_item")
    conn.commit()
    oth_manual.set_qty(conn, "1", a_point(conn, point), qty, T, "owner")
    return conn


def test_the_manual_stock_is_offered_after_the_orders_and_taken_and_given_back(tmp_path):
    conn = manual_db(tmp_path)
    old = card_at(conn, "oth", T)                                        # 4 of an order on OTH at «Бета»
    cid = new_order(conn, qty=6)
    found = oth_sale.offers(conn, orders.get_card(conn, cid))[line(conn, cid)["id"]]
    assert [(o["from_order"], o["qty"], o["take"]) for o in found] == [(old, 4, 4), (0, 3, 3)]
    manual_item = found[1]["from_item"]
    assert manual_item < 0
    assert oth_sale.take(conn, cid, line(conn, cid)["id"], manual_item, LATER, "o") == 3
    assert oth_manual.rows(conn, "1") == [] and oth_stock.pieces(conn) == {"1": 4}
    assert sorted(items(conn, cid)) == [("1", 3, BUY_NOW, False), ("1", 3, BUY_NOW, True)]   # the purchase of the order's own position
    taken = next(i for i, t in oth_sale.taken(conn, cid).items() if t["from_order"] == 0)
    assert oth_sale.can_untake(conn, cid, taken) and oth_sale.holds(conn, cid) == [0]
    assert "свой остаток" in oth_sale.holds_text(oth_sale.holds(conn, cid))
    assert oth_sale.untake(conn, cid, taken, LATER, "o") == 3
    assert [r["qty"] for r in oth_manual.rows(conn, "1")] == [3] and oth_stock.pieces(conn) == {"1": 7}
    assert [l["why"] for l in oth_manual.log_of(conn, "1")][:2] == ["untake:%d" % cid, "take:%d" % cid]


def test_a_take_never_goes_below_zero_and_another_point_is_not_offered(tmp_path, monkeypatch):
    conn = manual_db(tmp_path, qty=2)
    cid = new_order(conn, qty=4)
    o = oth_sale.offers(conn, orders.get_card(conn, cid))[line(conn, cid)["id"]][0]
    stale = oth_stock._rows(conn)
    monkeypatch.setattr(oth_stock, "_rows", lambda _conn, manual=True: stale)     # read before another tab took one
    conn.execute("UPDATE oth_manual SET qty = 1")
    conn.commit()
    with pytest.raises(oth_sale.OthSaleError):
        oth_sale.take(conn, cid, line(conn, cid)["id"], o["from_item"], LATER, "o")
    assert conn.execute("SELECT qty FROM oth_manual").fetchone()[0] == 1 and items(conn, cid) == [("1", 4, BUY_NOW, False)]
    monkeypatch.undo()
    other = new_order(conn, qty=1, at="Гамма")
    assert oth_sale.offers(conn, orders.get_card(conn, other))[line(conn, other)["id"]][0]["why"]


def test_an_order_with_manual_tyres_that_is_refused_puts_them_on_oth_as_its_own(tmp_path):
    conn = manual_db(tmp_path, qty=2)
    cid = new_order(conn, qty=2)
    o = oth_sale.offers(conn, orders.get_card(conn, cid))[line(conn, cid)["id"]][0]
    oth_sale.take(conn, cid, line(conn, cid)["id"], o["from_item"], LATER, "o")
    assert oth_stock.pieces(conn) == {}
    with pytest.raises(orders.OrderError):                               # held: never cancelled with them
        orders.move_card(conn, cid, next(s["id"] for s in orders.list_stages(conn) if s["code"] == "cancel"), LATER, "o")


# ---- the panel ----------------------------------------------------------------------------------------------------------------------

def card_html(client, goods="100"):
    return client.get("/catalog/item/" + goods).get_data(as_text=True)


def test_the_card_block_and_its_rights(tmp_path):
    app = make_app(tmp_path)
    owner = app.test_client()
    tok = login(owner)
    conn = store.connect(app.config["DB_PATH"])
    pid = a_point(conn)
    a_point(conn, "Тест 1")
    html = card_html(owner)
    assert "Свой остаток (ОТХ)" in html and "Тест 1" not in html.split("Свой остаток (ОТХ)")[1]
    r = post(owner, "/catalog/item/100/oth-manual", tok, point=str(pid), qty="3", note="витрина", action="set")
    assert r.status_code == 302 and [r_["qty"] for r_ in oth_manual.rows(conn, "100")] == [3]
    assert "витрина" in card_html(owner)
    partner, pconn = person(app, "partner")
    assert partner.post("/catalog/item/100/oth-manual", data={"csrf": "tok", "point": pid, "qty": "5", "action": "set"}).status_code == 302
    assert [r_["qty"] for r_ in oth_manual.rows(conn, "100")] == [5]
    for role in ("chats", "viewer"):
        other, _c = person(app, role)
        assert other.post("/catalog/item/100/oth-manual", data={"csrf": "tok", "point": pid, "qty": "9", "action": "set"}).status_code == 403
    pointer, _c = person(app, "point", point_id=pid)
    assert pointer.post("/catalog/item/100/oth-manual", data={"csrf": "tok", "point": pid, "qty": "9", "action": "set"}).status_code in (302, 403)
    assert [r_["qty"] for r_ in oth_manual.rows(conn, "100")] == [5]
    assert owner.post("/catalog/item/100/oth-manual", data={"point": pid, "qty": "9"}).status_code in (400, 403)   # no CSRF
    r = post(owner, "/catalog/item/100/oth-manual", tok, point=str(pid), action="remove")
    assert oth_manual.rows(conn, "100") == []
    assert post(owner, "/catalog/item/nope/oth-manual", tok, point=str(pid), qty="1").status_code == 404


def test_as_a_point_the_owner_cannot_enter_manual_stock(tmp_path):
    from tires_sync.panel import common
    assert common.SECTION_OF["catalog_oth_manual"] == "orders" and common.NEEDS["catalog_oth_manual"] == "supplier"
    assert "catalog_oth_manual" not in common.POINT_PAGES
    assert {r for r, v in users.MATRIX.items() if "supplier" in v["rights"]} == {"owner", "partner"}


def test_the_card_of_a_gone_position_says_why_it_does_not_go_to_avito(tmp_path):
    app = make_app(tmp_path)
    owner = app.test_client()
    tok = login(owner)
    conn = store.connect(app.config["DB_PATH"])
    from tires_sync import catalog_archive
    from helpers import make_offer
    offer = make_offer(goods_id="555", stock=0)
    conn.execute("INSERT INTO catalog_item (goods_id, supplier_id, data, first_seen, last_seen, last_stock, last_price, gone_since) "
                 "VALUES ('555', 1, ?, ?, ?, 0, 5000, ?)", (catalog_archive._dump(offer), NOW, NOW, NOW))
    conn.commit()
    from tires_sync import connections
    connections.set_live(conn, connections.create_account(conn, "A", NOW, "owner"), True, NOW, "owner")
    pid = a_point(conn)
    post(owner, "/catalog/item/555/oth-manual", tok, point=str(pid), qty="2", action="set")
    assert "нужна своя цена" in card_html(owner, "555")
    draft = settings_store.create_draft(conn, {"price_overrides": {"1:555": {"price": 7000}}}, NOW)
    settings_store.confirm_draft(conn, draft, NOW, "owner", strong=True)
    html = card_html(owner, "555")
    assert "раньше не публиковалась в этом аккаунте" in html and "нужна своя цена" not in html      # never on Avito here: no Id to come back under
    conn.execute("INSERT INTO retired_ad (account_id, goods_id, ad_id, snapshot, identity, removed_at) VALUES (1, '555', '555', '<Ad/>', '[]', ?)",
                 (NOW,))
    conn.commit()
    html = card_html(owner, "555")
    assert "уйдёт на Авито по своей цене 7000 ₽ (из архива каталога)" in html
    conn.execute("DELETE FROM retired_ad")
    conn.execute("DELETE FROM catalog_item")                            # nothing to build the ad from, and no card to open either
    conn.commit()
    with app.test_request_context():
        from flask import session
        session.update(user="owner", epoch=0, pw=__import__("panel_helpers").shared_mark_of(app))
        block = __import__("tires_sync.panel.routes_catalog_oth", fromlist=["x"]).manual_block("555")
    assert [st["ok"] for st in block["status"]] == [False] and "не из чего собрать объявление" in block["status"][0]["text"]


# ---- the review of 2026-11-03 -----------------------------------------------------------------------------------------------------

def test_the_list_says_the_tyres_came_from_the_own_stock(tmp_path):
    from tires_sync import order_sheet
    conn = manual_db(tmp_path, qty=2)
    cid = new_order(conn, qty=2)
    o = oth_sale.offers(conn, orders.get_card(conn, cid))[line(conn, cid)["id"]][0]
    oth_sale.take(conn, cid, line(conn, cid)["id"], o["from_item"], LATER, "o")
    card = orders.get_card(conn, cid)
    row = order_sheet.card_rows(conn, card, LATER)[order_sheet.key(cid, card["items"][0]["id"])]
    assert row["Примечание"].startswith("с ОТХ, свой остаток") and "№0" not in row["Примечание"] and row["№ заказа MIM"] == ""


def test_a_change_of_the_quantity_keeps_the_note(tmp_path):
    conn = db(tmp_path)
    pid = a_point(conn)
    oth_manual.set_qty(conn, "102", pid, 3, NOW, "owner", "витрина")
    oth_manual.set_qty(conn, "102", pid, 2, LATER, "owner", None)
    assert oth_manual.rows(conn, "102")[0]["note"] == "витрина"
    oth_manual.set_qty(conn, "102", pid, 2, LATER, "owner", "склад")
    assert oth_manual.rows(conn, "102")[0]["note"] == "склад"


def test_a_saved_ad_taken_off_by_a_setting_is_not_promised(tmp_path):
    prepared(tmp_path, accounts=("A",), price_overrides={"1:102": {"price": 7000}})
    assert run(tmp_path, FIVE) == 0
    assert run(tmp_path, AFTER) == 0
    manual(tmp_path, "102", 2)
    conn = connect(tmp_path)
    conn.execute("DELETE FROM catalog_item WHERE goods_id = '102'")
    conn.commit()
    from tires_sync.panel.routes_catalog_oth import account_status
    assert account_status(conn, "102", 1, AFTER_PARSED())[0] is True     # taken off for stock: comes back on its saved ad
    conn.execute("UPDATE ad_event SET reason = 'manual_off' WHERE goods_id = '102' AND event = 'removed'")
    conn.commit()
    ok, text = account_status(conn, "102", 1, AFTER_PARSED())
    assert ok is False and "настройкой" in text
    conn.close()


def test_the_text_of_two_held_orders_is_as_before():
    assert oth_sale.holds_text([5, 6]) == "В заказе шины с ОТХ (заказ №5, №6): переведите заказ на «На ОТХ» или верните их («Вернуть на ОТХ» у позиций)."
    assert oth_sale.holds_text([0]).startswith("В заказе шины с ОТХ (свой остаток)")
    assert oth_sale.holds_text([0, 5]).startswith("В заказе шины с ОТХ (заказ №5, свой остаток)")


def test_the_card_tells_what_the_cycle_does(tmp_path):
    """The card's status comes from the cycle's own rules (sync._manual_gone and the Id guard): a type left out, a percent with no purchase."""
    from tires_sync import connections
    prepared(tmp_path, accounts=("A",), price_overrides={"1:102": {"price": 7000}})
    assert run(tmp_path, FIVE) == 0
    assert run(tmp_path, AFTER) == 0                                     # 102 gone, its ad retired under «102»
    manual(tmp_path, "102", 2)
    conn = connect(tmp_path)
    from tires_sync.panel.routes_catalog_oth import account_status
    assert account_status(conn, "102", 1, AFTER_PARSED())[0] is True
    conn.execute("UPDATE retired_ad SET ad_id = '102_old' WHERE goods_id = '102'")
    conn.commit()
    ok, text = account_status(conn, "102", 1, AFTER_PARSED())
    assert ok is False and "Id" in text
    conn.execute("UPDATE retired_ad SET ad_id = '102' WHERE goods_id = '102'")
    conn.commit()
    from tires_sync import catalog_archive, cycle
    connections.set_excluded_types(conn, 1, [cycle.feed_tire_type(catalog_archive.last_offer(conn, "102"))], NOW, "owner")
    ok, text = account_status(conn, "102", 1, AFTER_PARSED())
    assert ok is False and "тип шин" in text
    draft = settings_store.create_draft(conn, {"price_overrides": {}}, NOW)
    settings_store.confirm_draft(conn, draft, NOW, "owner", strong=True)
    assert "тип шин" in account_status(conn, "102", 1, AFTER_PARSED())[1]    # a setting, not «нужна своя цена»
    connections.set_excluded_types(conn, 1, [], NOW, "owner")
    draft = settings_store.create_draft(conn, {"price_overrides": {"1:102": {"pct": 10}}}, NOW)
    settings_store.confirm_draft(conn, draft, NOW, "owner", strong=True)
    conn.execute("UPDATE catalog_item SET last_price = 0 WHERE goods_id = '102'")
    conn.commit()
    ok, text = account_status(conn, "102", 1, AFTER_PARSED())
    assert ok is False and "последней закупки" in text
    assert account_status(conn, "103", 1, AFTER_PARSED()) is None          # in the unload: no status
    conn.close()


def AFTER_PARSED():
    from tires_sync.webmim import parse_unload
    return parse_unload(AFTER, "T1")


def test_the_page_of_oth_lists_the_own_stock_and_filters_by_point(tmp_path):
    from tests.test_order_reserve import card_at as order_on
    app = make_app(tmp_path)
    owner = app.test_client()
    login(owner)
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, T)
    old = order_on(conn, "oth", T)                                       # 4 of an order on OTH at «Бета»
    beta, yunona, test = a_point(conn, "Бета"), a_point(conn), a_point(conn, "Тест 1")
    oth_manual.set_qty(conn, "100", yunona, 3, NOW, "owner", "витрина")
    conn.execute("INSERT INTO oth_manual (goods_id, point_id, qty, author, at) VALUES ('101', ?, 5, 'o', ?)", (test, NOW))   # named a test point later: not listed
    conn.commit()
    html = owner.get("/oth").get_data(as_text=True)
    own = html.split("data-oth-manual-list")[1]
    assert "Свой остаток" in html and "витрина" in own and "Юнона" in own and 'href="/catalog/item/100"' in own and "5 шт." not in own
    assert "№%d" % old in html
    page = owner.get("/oth?point=%d" % yunona).get_data(as_text=True)
    assert "data-oth-manual-list" in page and "№%d" % old not in page.split("data-oth-manual-list")[0].split("<tbody>", 1)[-1]
    page = owner.get("/oth?point=%d" % beta).get_data(as_text=True)
    assert "data-oth-manual-list" not in page and "/orders/%d" % old in page
    viewer, _c = person(app, "viewer")
    assert viewer.get("/oth").status_code in (200, 302, 403)


def test_a_gone_position_never_published_in_an_account_never_gets_a_new_id(tmp_path, capsys):
    """feed-reviewer 2026-11-03: built from the archive it would go under live_id_of — a new Id; it goes nowhere, the journal says why once."""
    import json
    from tires_sync import catalog_archive
    prepared(tmp_path, price_overrides={"1:199": {"price": 7000}, "2:199": {"price": 7000}})
    assert run(tmp_path, FIVE) == 0
    conn = connect(tmp_path)
    data = json.loads(conn.execute("SELECT data FROM catalog_item WHERE goods_id = '105'").fetchone()[0])
    data.update(goods_id="199", code="TS199", width=235, name=data["name"].replace("205", "235"),
                name_orig=data["name_orig"].replace("205", "235"))          # another tire: no lot of the same tire carries it
    conn.execute("INSERT INTO catalog_item (goods_id, supplier_id, data, first_seen, last_seen, last_stock, last_price, gone_since) "
                 "VALUES ('199', 1, ?, ?, ?, 0, 5000, ?)", (json.dumps(data, ensure_ascii=False), NOW, NOW, NOW))
    conn.commit()
    conn.close()
    manual(tmp_path, "199", 4)
    capsys.readouterr()
    assert run(tmp_path, FIVE) == 0
    passed(capsys)
    assert run(tmp_path, FIVE) == 0
    for acct in (1, 2):
        assert not any(i.startswith("199") for i in ads_of(public(tmp_path, acct)))
        assert "199" not in published(tmp_path, acct)
    conn = connect(tmp_path)
    told = sorted(json.loads(r[0])["account"] for r in conn.execute("SELECT detail FROM audit WHERE action = 'oth_manual_no_id'"))
    assert told == [1, 2]
    from tires_sync.panel.routes_catalog_oth import account_status
    from tires_sync.webmim import parse_unload
    assert "раньше не публиковалась" in account_status(conn, "199", 2, parse_unload(FIVE, "T1"))[1]
    conn.close()


def test_the_saved_ad_changes_only_its_price_line():
    from tires_sync import sync
    ad = ET.fromstring("<Ad><Price>4900</Price><Description>💰 Цена: 4900 руб. за 1 шт.\nДоставка 4900 руб. отдельно\n4900 ₽</Description></Ad>")
    sync._set_price(ad, 7300)
    assert ad.findtext("Price") == "7300"
    assert ad.findtext("Description") == "💰 Цена: 7300 руб. за 1 шт.\nДоставка 4900 руб. отдельно\n4900 ₽"


def test_the_page_of_oth_hides_test_points_and_ignores_an_unknown_one(tmp_path):
    from tests.test_order_reserve import card_at as order_on
    app = make_app(tmp_path)
    owner = app.test_client()
    login(owner)
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, T)
    test = a_point(conn, "Тест склад")
    old = order_on(conn, "oth", T)
    conn.execute("UPDATE order_card SET point_id = ? WHERE id = ?", (test, old))       # a test order on OTH
    conn.commit()
    oth_manual.set_qty(conn, "100", a_point(conn), 3, NOW, "owner")
    html = owner.get("/oth").get_data(as_text=True)
    assert '<option value="%d"' % test not in html and "Юнона</option>" in html
    assert "Заказов на ОТХ нет" not in html                              # the test order is still listed as before
    page = owner.get("/oth?point=99999").get_data(as_text=True)
    assert "data-oth-manual-list" in page                                # an unknown point: the whole page
    only = owner.get("/oth?point=%d" % a_point(conn)).get_data(as_text=True)
    assert "Заказов на ОТХ нет" in only and "data-oth-manual-list" in only


def test_a_never_published_lot_does_not_take_the_oth_of_a_lot_that_can_come_back(tmp_path, capsys):
    """Review 2026-11-03: one tire, gone lot G (never on Avito here, manual stock) sorts before lot L (taken off for stock, an order on OTH):
    G must not become the carrier and then be dropped — L comes back under its own Id."""
    import json
    prepared(tmp_path, accounts=("A",), price_overrides={"1:1015": {"price": 7000}})
    assert run(tmp_path, FIVE) == 0
    assert run(tmp_path, AFTER) == 0                                     # 102 taken off for stock (retired_ad «102»)
    conn = connect(tmp_path)
    data = json.loads(conn.execute("SELECT data FROM catalog_item WHERE goods_id = '102'").fetchone()[0])
    data.update(goods_id="1015", code="TS1015")                          # the same tire, another lot
    conn.execute("INSERT INTO catalog_item (goods_id, supplier_id, data, first_seen, last_seen, last_stock, last_price, gone_since) "
                 "VALUES ('1015', 1, ?, ?, ?, 0, 5000, ?)", (json.dumps(data, ensure_ascii=False), NOW, NOW, NOW))
    conn.commit()
    conn.close()
    manual(tmp_path, "1015", 2)
    on_oth(tmp_path, "102")
    capsys.readouterr()
    assert run(tmp_path, AFTER) == 0
    passed(capsys)
    ids = ads_of(public(tmp_path))
    assert "102" in ids and not any(i.startswith("1015") for i in ids)


def test_a_never_published_lot_on_own_stock_is_carried_by_the_live_ad_of_the_same_tire(tmp_path, capsys):
    """Review round 2: gone lot G (never on Avito here) of the same tire as the published 103: 103 carries its pieces and point, nothing
    is told as «не публиковалась»."""
    import json
    prepared(tmp_path, accounts=("A",), price_overrides={"1:1035": {"price": 7000}})
    assert run(tmp_path, FIVE) == 0
    conn = connect(tmp_path)
    data = json.loads(conn.execute("SELECT data FROM catalog_item WHERE goods_id = '103'").fetchone()[0])
    data.update(goods_id="1035", code="TS1035")
    conn.execute("INSERT INTO catalog_item (goods_id, supplier_id, data, first_seen, last_seen, last_stock, last_price, gone_since) "
                 "VALUES ('1035', 1, ?, ?, ?, 0, 5000, ?)", (json.dumps(data, ensure_ascii=False), NOW, NOW, NOW))
    conn.commit()
    conn.close()
    manual(tmp_path, "1035", 2)
    capsys.readouterr()
    assert run(tmp_path, FIVE) == 0
    passed(capsys)
    ids = ads_of(public(tmp_path))
    assert not any(i.startswith("1035") for i in ids) and description(public(tmp_path), "103").startswith(AT_YUNONA + "\n")
    conn = connect(tmp_path)
    assert conn.execute("SELECT COUNT(*) FROM audit WHERE action = 'oth_manual_no_id'").fetchone()[0] == 0
    from tires_sync.panel.routes_catalog_oth import account_status
    from tires_sync.webmim import parse_unload
    assert account_status(conn, "1035", 1, parse_unload(FIVE, "T1")) == (True, "штуки показаны в объявлении 103 (та же шина)")   # the card
    conn.close()
    manual(tmp_path, "1035", 0)                                          # gone from the own stock: the next cycle forgets it
    assert run(tmp_path, FIVE) == 0
    conn = connect(tmp_path)
    assert oth_manual.carried_by(conn, 1) == {}
    conn.close()
