"""The owner's answers of 2026-10-30 on the sale from «На ОТХ» (lane oth-fixes): the point sees where the tyres come from, no date of
delivery is made up for tyres already at the point, the plaque of the return names the WebMIM order of the act МХ-3, and the point hears of
a cancelled return."""
from __future__ import annotations

from test_mypoint import manager
from test_oth_sale import LATER, T, new_order, panel, point_id, stage, take
from panel_helpers import post
from tests.test_order_reserve import card_at, db
from tires_sync import order_manager, order_notify, order_oth, order_templates, orders

IN_STOCK = "Товар в наличии на точке"


def _quiet(monkeypatch):
    told = []
    monkeypatch.setattr(order_manager, "tell_client_arrived", lambda *a, **kw: (True, "ok"))
    monkeypatch.setattr(order_notify, "tell_point", lambda c, box, card, text, now, **kw: (told.append((card["id"], text)), "показано точке")[1])
    return told


def _webmim(conn, cid):
    return orders.get_card(conn, cid)["webmim_number"]


# ---- 1. the mark on the point's card --------------------------------------------------------------------------------------------------

def test_the_point_sees_the_old_order_of_tyres_taken_from_oth_and_nothing_on_an_ordinary_order(tmp_path, monkeypatch):
    app, owner, tok, conn = panel(tmp_path)
    old = card_at(conn, "oth", T)
    cid = new_order(conn, qty=2)
    take(conn, cid)
    _quiet(monkeypatch)
    assert post(owner, "/orders/%d/oth-at-point" % cid, tok).status_code == 302
    plain = card_at(conn, "at_point", T)
    page = manager(app, conn, point_id(conn)).get("/my-point").get_data(as_text=True)
    card = page.split('id="order-%d"' % cid)[1].split("</article>")[0]
    assert "Шины с ОТХ, заказ №%s" % _webmim(conn, old) in card
    assert "Шины с ОТХ" not in page.split('id="order-%d"' % plain)[1].split("</article>")[0]


# ---- 2. no date of delivery for tyres already at the point ----------------------------------------------------------------------------

def test_the_move_to_the_point_puts_no_date_and_every_place_says_in_stock(tmp_path, monkeypatch):
    app, owner, tok, conn = panel(tmp_path)
    card_at(conn, "oth", T)
    cid = new_order(conn, qty=2)
    take(conn, cid)
    conn.execute("UPDATE order_card SET delivery_date = '' WHERE id = ?", (cid,))
    conn.commit()
    _quiet(monkeypatch)
    assert post(owner, "/orders/%d/oth-at-point" % cid, tok).status_code == 302
    card = orders.get_card(conn, cid)
    assert card["stage"]["code"] == "at_point" and card["delivery_date"] == ""                  # today is never put (the owner 2026-10-30)
    assert order_templates.values(conn, card, LATER)["delivery_date"] == IN_STOCK
    assert order_templates.render(conn, card, "Доставка: {delivery_date}", LATER) == ("Доставка: " + IN_STOCK, [])
    assert IN_STOCK in owner.get("/orders/%d" % cid).get_data(as_text=True)
    assert IN_STOCK in owner.get("/orders?v=list").get_data(as_text=True) or IN_STOCK in owner.get("/orders").get_data(as_text=True)
    orders.move_card(conn, cid, stage(conn, "done"), LATER, "o")                                  # the stages after need no date either
    assert orders.get_card(conn, cid)["stage"]["code"] == "done"


def test_a_date_typed_is_shown_as_before_and_an_order_with_the_suppliers_tyres_still_needs_one(tmp_path):
    conn = db(tmp_path)
    card_at(conn, "oth", T)
    cid = new_order(conn, qty=2)
    take(conn, cid)
    assert order_templates.values(conn, orders.get_card(conn, cid), LATER)["delivery_date"] == "30.10.2026"
    mixed = new_order(conn, qty=6)
    take(conn, mixed)
    conn.execute("UPDATE order_card SET delivery_date = '' WHERE id = ?", (mixed,))
    conn.commit()
    card = orders.get_card(conn, mixed)
    assert order_templates.values(conn, card, LATER)["delivery_date"] == ""
    assert "дата доставки" in orders.missing_for(card, orders.get_stage(conn, stage(conn, "at_point")), orders._placed_stage(conn))


# ---- 3. the plaque of the return names the WebMIM order of the act ---------------------------------------------------------------------

def test_the_plaque_of_the_return_names_the_webmim_order_the_tyres_were_bought_by(tmp_path, monkeypatch):
    app, owner, tok, conn = panel(tmp_path)
    old = card_at(conn, "oth", T)
    cid = new_order(conn, qty=4)
    take(conn, cid)                                                                               # the old order is emptied: «Отмена»
    _quiet(monkeypatch)
    post(owner, "/orders/%d/oth-at-point" % cid, tok)
    orders.move_card(conn, cid, stage(conn, "oth"), LATER, "o")                                   # the new client refused too
    plain = card_at(conn, "oth", T)
    for order in (cid, plain):
        assert post(owner, "/orders/%d/oth-agreed" % order, tok).status_code == 302
    page = manager(app, conn, point_id(conn)).get("/my-point").get_data(as_text=True)
    plaque = page.split('data-return="%d"' % cid)[1].split("</article>")[0]
    assert "Подпишите акт МХ-3" in plaque and "заказу WebMIM №%s" % _webmim(conn, old) in plaque
    ordinary = page.split('data-return="%d"' % plain)[1].split("</article>")[0]
    assert "Подпишите акт МХ-3" in ordinary and "WebMIM №" not in ordinary                      # an ordinary order on OTH: as before


# ---- 4. the point hears of a cancelled return ----------------------------------------------------------------------------------------

def test_the_cancelled_agreement_is_told_to_the_point_once(tmp_path, monkeypatch):
    app, owner, tok, conn = panel(tmp_path)
    oth = card_at(conn, "oth", T)
    told = _quiet(monkeypatch)
    post(owner, "/orders/%d/oth-unagree" % oth, tok)                                              # nothing agreed: nothing to tell
    assert told == []
    post(owner, "/orders/%d/oth-agreed" % oth, tok)
    assert told == []                                                                              # the agreement is the plaque, as before
    assert post(owner, "/orders/%d/oth-unagree" % oth, tok).status_code == 302
    assert told == [(oth, "Возврат по заказу №%s поставщику отменён, шины остаются на точке" % _webmim(conn, oth))]
    post(owner, "/orders/%d/oth-unagree" % oth, tok)                                              # a second press: nothing more
    assert len(told) == 1
