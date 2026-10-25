"""Quick fixes of 2026-10-19: the client's name is needed to place an order, «Переговоры» may go when empty, special terms and the receipt flag."""
from __future__ import annotations

from datetime import datetime

import pytest
from panel_helpers import login, make_app, post

from tires_sync import order_points as points, order_rules as rules, orders, store

NOW = "2026-10-19T12:00:00"


def stage(conn, code):
    return next(s["id"] for s in orders.list_stages(conn) if s["code"] == code)


def db(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    orders.ensure_pipeline(conn, NOW)
    return conn


def lead(conn, name="Иван"):
    pid = points.save_point(conn, dict(name="Бета", manager_name="Алексей", channel="vk"))
    cid = orders.create_card(conn, dict(client_name=name, client_phone="+7 900 111 22 33", point_id=pid, delivery_date="2026-10-20"), NOW, "o", require_contact=True)
    orders.add_item(conn, cid, goods_id="1", name="Nokian 205/55 R16", brand_key="nokian", season="winter", diameter=16, territory="1",
                    qty=4, sale_unit=5000, supplier_price=4000, terms=None, fee_pct=0, promo_pct=0, now=NOW, author="o")
    return cid


def test_an_order_cannot_be_placed_without_the_clients_name(tmp_path):
    conn = db(tmp_path)
    cid = lead(conn, name="")
    with pytest.raises(orders.OrderError, match="имя клиента"):
        orders.move_card(conn, cid, stage(conn, "placed"), NOW, "o")
    orders.update_card(conn, cid, {"client_name": "Иван"}, NOW, "o")
    assert orders.move_card(conn, cid, stage(conn, "placed"), NOW, "o") is True


def test_a_placed_order_without_a_name_from_before_can_still_be_edited(tmp_path):
    conn = db(tmp_path)
    cid = lead(conn)
    orders.move_card(conn, cid, stage(conn, "placed"), NOW, "o")
    conn.execute("UPDATE order_card SET client_name = '' WHERE id = ?", (cid,))                  # a card made before the rule
    conn.commit()
    orders.update_card(conn, cid, {"comment": "позвонить"}, NOW, "o")
    assert orders.get_card(conn, cid)["comment"] == "позвонить"
    with pytest.raises(orders.OrderError):
        orders.update_card(conn, cid, {"client_phone": ""}, NOW, "o")                           # taking away other data is still refused


def test_the_empty_talk_stage_can_be_deleted_with_its_rules_and_other_system_stages_cannot(tmp_path):
    conn = db(tmp_path)
    talk, new = stage(conn, "talk"), stage(conn, "new")
    rules.add_rule(conn, talk, "stale", 24, "remind", None, "Позвоните", "ask", None)
    for code in ("new", "wait", "placed", "done"):
        with pytest.raises(orders.OrderError, match="Системный"):
            orders.delete_stage(conn, stage(conn, code))
    busy = orders.create_card(conn, dict(client_name="Иван"), NOW, "o")
    orders.move_card(conn, busy, talk, NOW, "o")
    with pytest.raises(orders.OrderError, match="есть заказы"):
        orders.delete_stage(conn, talk)                                                       # a stage with orders stays
    orders.move_card(conn, busy, new, NOW, "o")
    orders.delete_stage(conn, talk)
    assert "talk" not in [s["code"] for s in orders.list_stages(conn)] and rules.list_rules(conn, talk) == []
    orders.ensure_pipeline(conn, NOW)                                                         # the stage does not come back
    names = [s["name"] for s in orders.list_stages(conn)]
    assert "Переговоры" not in names and names[:2] == ["Новый заказ", "Ожидает наличие"]
    assert [s["position"] for s in orders.list_stages(conn)] == list(range(len(names)))


def test_special_terms_amount_and_receipt_are_saved_checked_and_cleared(tmp_path):
    conn = db(tmp_path)
    cid = orders.create_card(conn, dict(client_name="Иван"), NOW, "o")
    card = orders.get_card(conn, cid)
    assert (card["special_terms"], card["special_amount"], card["receipt_needed"]) == ("", 0, 0)
    orders.update_card(conn, cid, {"special_terms": "предоплата, резерв 10 дней", "special_amount": "5 000,50", "receipt_needed": "1"}, NOW, "o")
    card = orders.get_card(conn, cid)
    assert (card["special_terms"], card["special_amount"], card["receipt_needed"]) == ("предоплата, резерв 10 дней", 5000.5, 1)
    assert any("Особые условия: предоплата" in e["text"] for e in orders.list_events(conn, cid))
    texts = [e["text"] for e in orders.list_events(conn, cid)]
    assert "Нужен товарный чек: да" in texts and "Сумма по особым условиям: 5000.5" in texts
    for typed, expected in (("1.500", 1500.0), ("5 000 ₽", 5000.0), ("5\u00a0000", 5000.0), ("19 240,50", 19240.5), ("1,5", 1.5)):          # the project's way of reading money
        orders.update_card(conn, cid, {"special_amount": typed}, NOW, "o")
        assert orders.get_card(conn, cid)["special_amount"] == expected
    orders.update_card(conn, cid, {"special_amount": "5000.5"}, NOW, "o")
    for bad in ({"special_amount": "много"}, {"special_amount": "-1"}, {"special_amount": "99999999"}, {"special_terms": "я" * 301}):
        with pytest.raises(orders.OrderError):
            orders.update_card(conn, cid, bad, NOW, "o")
    orders.update_card(conn, cid, {"special_terms": "", "special_amount": "", "receipt_needed": ""}, NOW, "o")
    card = orders.get_card(conn, cid)
    assert (card["special_terms"], card["special_amount"], card["receipt_needed"]) == ("", 0, 0)


def test_the_card_form_saves_the_checkbox_and_clears_it(tmp_path):
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 10, 19, 9, 0, 0))
    client = app.test_client()
    tok = login(client)
    conn = store.connect(app.config["DB_PATH"])
    client.get("/orders")
    cid = orders.create_card(conn, dict(client_name="Иван"), NOW, "o")
    html = client.get("/orders/%d?tab=main" % cid).get_data(as_text=True)
    assert 'name="special_terms"' in html and 'name="receipt_needed"' in html
    post(client, "/orders/%d/save" % cid, tok, tab="main", client_name="Иван", receipt_needed="1", special_terms="предоплата")
    assert orders.get_card(conn, cid)["receipt_needed"] == 1
    post(client, "/orders/%d/save" % cid, tok, tab="main", client_name="Иван", receipt_needed="", special_terms="")           # the box was unticked
    post(client, "/orders/%d/save" % cid, tok, tab="main", client_name="Иван", receipt_needed=["1", ""])                      # what a browser sends for a ticked box: the box, then the hidden field
    assert orders.get_card(conn, cid)["receipt_needed"] == 1
    post(client, "/orders/%d/save" % cid, tok, tab="main", client_name="Иван", receipt_needed=[""])
    assert orders.get_card(conn, cid)["receipt_needed"] == 0
    card = orders.get_card(conn, cid)
    assert card["receipt_needed"] == 0 and card["special_terms"] == ""
    html = client.get("/orders/settings").get_data(as_text=True)
    talk = next(x["id"] for x in orders.list_stages(conn) if x["code"] == "talk")
    assert "/orders/settings/stage/%d/delete" % talk in html                                  # the empty stage offers «Удалить»
    new = next(x["id"] for x in orders.list_stages(conn) if x["code"] == "new")
    assert "/orders/settings/stage/%d/delete" % new not in html                               # the others do not
