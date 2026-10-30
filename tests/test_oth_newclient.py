"""Back from «На ОТХ» to «Товар на точке» (the owner 2026-10-30, the order 444: «скорее всего всегда новый клиент будет»): the office is
asked first — «Новый клиент» (the default: a new order of the point takes the tyres from OTH, the old one goes to «Отмена», the new one to the
point with «можно забирать») or «Тот же клиент» (as before). No answer, no move; a point is never asked."""
from __future__ import annotations

import re

import pytest

from panel_helpers import post
from test_mypoint import manager
from test_oth_sale import BUY_OLD, LATER, T, panel, point_id, stage
from tests.test_order_reserve import card_at, db
from tires_sync import order_manager, order_notify, orders, oth_sale, oth_stock, store


def _quiet(monkeypatch):
    told = {"client": [], "point": []}
    monkeypatch.setattr(order_manager, "tell_client_arrived", lambda c, card_id, deliver, now, who, body=None: (told["client"].append(card_id), (True, "Клиенту отправлено."))[1])
    monkeypatch.setattr(order_notify, "tell_point", lambda c, box, card, text, now, **kw: (told["point"].append(card["id"]), "показано точке")[1])
    return told


# ---- the model -------------------------------------------------------------------------------------------------------------------------

def test_a_new_client_takes_the_tyres_of_oth_into_a_new_order_and_the_old_one_is_cancelled(tmp_path):
    conn = db(tmp_path)
    old = card_at(conn, "oth", T)
    new = oth_sale.to_new_client(conn, old, "Пётр", "+7 900 555 66 77", LATER, "o")
    card = orders.get_card(conn, new)
    assert (card["client_name"], card["client_phone"], card["point_id"]) == ("Пётр", "+7 900 555 66 77", orders.get_card(conn, old)["point_id"])
    assert [(i["goods_id"], int(i["qty"]), float(i["supplier_price"])) for i in card["items"]] == [("1", 4, BUY_OLD)]   # the old purchase
    assert [t["from_order"] for t in oth_sale.taken(conn, new).values()] == [old]
    old_card = orders.get_card(conn, old)
    assert old_card["stage"]["code"] == "cancel" and old_card["cancel_reason"] == oth_sale.TAKEN_REASON % new
    assert oth_stock.by_point(conn) == {}                                          # the stock of OTH (and the ad) is gone
    assert oth_sale.all_from_oth(conn, card)                                       # ready for «Товар на точке» without WebMIM


def test_what_keeps_a_new_client_out(tmp_path):
    conn = db(tmp_path)
    old = card_at(conn, "oth", T)
    with pytest.raises(orders.OrderError, match="телефон"):
        oth_sale.to_new_client(conn, old, "Пётр", "", LATER, "o")
    conn.execute("INSERT INTO oth_return_plan (order_id, agreed_at, agreed_by, return_date) VALUES (?,?,?,?)", (old, T, "o", "2026-10-29"))
    conn.commit()
    with pytest.raises(orders.OrderError, match="возврат"):
        oth_sale.to_new_client(conn, old, "Пётр", "+7 900 555 66 77", LATER, "o")
    assert orders.get_card(conn, old)["stage"]["code"] == "oth"
    assert conn.execute("SELECT COUNT(*) FROM order_card").fetchone()[0] == 1       # nothing made of a refused one
    other = card_at(conn, "at_point", T)
    busy = orders.get_card(conn, other)["client_phone"]
    third = card_at(conn, "oth", T)
    with pytest.raises(orders.OrderError, match="открытый заказ"):
        oth_sale.to_new_client(conn, third, "Пётр", busy, LATER, "o")
    assert orders.get_card(conn, third)["stage"]["code"] == "oth"


def test_an_order_not_on_oth_is_refused(tmp_path):
    conn = db(tmp_path)
    here = card_at(conn, "at_point", T)
    with pytest.raises(orders.OrderError, match="На ОТХ"):
        oth_sale.to_new_client(conn, here, "Пётр", "+7 900 555 66 77", LATER, "o")


# ---- the panel -------------------------------------------------------------------------------------------------------------------------

def test_the_move_back_from_oth_asks_first_and_moves_nothing(tmp_path):
    app, owner, tok, conn = panel(tmp_path)
    old = card_at(conn, "oth", T)
    r = post(owner, "/orders/%d/move" % old, tok, stage_id=str(stage(conn, "at_point")))
    assert r.status_code == 302 and r.headers["Location"].endswith("/orders/%d#oth-back" % old)
    assert orders.get_card(conn, old)["stage"]["code"] == "oth"
    page = owner.get("/orders/%d" % old).get_data(as_text=True)
    assert "Новый клиент" in page and "Тот же клиент" in page and 'action="/orders/%d/oth-back"' % old in page
    r = owner.post("/orders/%d/move" % old, data={"csrf": tok, "stage_id": str(stage(conn, "at_point"))}, headers={"X-Requested-With": "fetch"})
    assert r.get_json()["go"].endswith("#oth-back") and orders.get_card(conn, old)["stage"]["code"] == "oth"      # the board: to the card


def test_a_new_client_makes_the_new_order_at_the_point_and_tells_the_client(tmp_path, monkeypatch):
    app, owner, tok, conn = panel(tmp_path)
    old = card_at(conn, "oth", T)
    told = _quiet(monkeypatch)
    post(owner, "/orders/%d/move" % old, tok, stage_id=str(stage(conn, "at_point")))
    r = post(owner, "/orders/%d/oth-back" % old, tok, answer="new", client_name="Пётр", client_phone="+7 900 555 66 77")
    new = int(re.search(r"/orders/(\d+)", r.headers["Location"]).group(1))
    assert new != old
    card = orders.get_card(conn, new)
    assert card["stage"]["code"] == "at_point" and card["client_name"] == "Пётр"
    assert orders.get_card(conn, old)["stage"]["code"] == "cancel"
    assert told == {"client": [new], "point": [new]}


def test_the_same_client_moves_the_old_order_as_before_and_tells_nobody(tmp_path, monkeypatch):
    app, owner, tok, conn = panel(tmp_path)
    old = card_at(conn, "oth", T)
    told = _quiet(monkeypatch)
    post(owner, "/orders/%d/move" % old, tok, stage_id=str(stage(conn, "at_point")))
    post(owner, "/orders/%d/oth-back" % old, tok, answer="same")
    assert orders.get_card(conn, old)["stage"]["code"] == "at_point"
    assert conn.execute("SELECT COUNT(*) FROM order_card").fetchone()[0] == 1 and told == {"client": [], "point": []}


def test_a_new_client_without_a_phone_is_asked_again(tmp_path, monkeypatch):
    app, owner, tok, conn = panel(tmp_path)
    old = card_at(conn, "oth", T)
    _quiet(monkeypatch)
    r = post(owner, "/orders/%d/oth-back" % old, tok, answer="new", client_name="Пётр", client_phone="")
    assert r.status_code == 302 and "#oth-back" in r.headers["Location"]
    assert orders.get_card(conn, old)["stage"]["code"] == "oth" and conn.execute("SELECT COUNT(*) FROM order_card").fetchone()[0] == 1


def test_the_window_and_the_chat_ask_too(tmp_path):
    app, owner, tok, conn = panel(tmp_path)
    old = card_at(conn, "oth", T)
    r = post(owner, "/orders/%d/save" % old, tok, stage_id=str(stage(conn, "at_point")))
    assert "#oth-back" in r.headers["Location"] and orders.get_card(conn, old)["stage"]["code"] == "oth"
    conn.execute("INSERT INTO avito_chat (account_id, chat_id, order_id) VALUES (1, 'c1', ?)", (old,))
    conn.commit()
    row = conn.execute("SELECT id FROM avito_chat WHERE chat_id = 'c1'").fetchone()[0]
    r = post(owner, "/chats/%d/stage" % row, tok, stage_id=str(stage(conn, "at_point")))
    assert "#oth-back" in r.headers["Location"] and orders.get_card(conn, old)["stage"]["code"] == "oth"


def test_a_point_is_never_asked(tmp_path):
    app, owner, tok, conn = panel(tmp_path)
    old = card_at(conn, "oth", T)
    mine = manager(app, conn, point_id(conn))
    mine.post("/orders/%d/move" % old, data={"csrf": "tok", "stage_id": str(stage(conn, "at_point"))})
    assert orders.get_card(conn, old)["stage"]["code"] == "at_point"                # as before


# ---- the review of 2026-10-30 -----------------------------------------------------------------------------------------------------------------

def test_a_take_that_gets_fewer_tyres_undoes_the_new_order(tmp_path, monkeypatch):
    conn = db(tmp_path)
    old = card_at(conn, "oth", T)
    real = oth_sale.take
    monkeypatch.setattr(oth_sale, "take", lambda *a, **kw: real(*a, **kw) - 1)    # the race: another tab took one a moment ago
    with pytest.raises(orders.OrderError, match="не переданы"):
        oth_sale.to_new_client(conn, old, "Пётр", "+7 900 555 66 77", LATER, "o")
    assert orders.get_card(conn, old)["stage"]["code"] == "oth" and oth_stock.by_point(conn) == {("1", point_id(conn)): 4}
    assert [r[0] for r in conn.execute("SELECT id FROM order_card")] == [old]          # the new one is in «Удалённые»


def test_the_same_client_is_offered_the_messages_of_the_stage(tmp_path, monkeypatch):
    from tires_sync.panel import routes_orders
    app, owner, tok, conn = panel(tmp_path)
    old = card_at(conn, "oth", T)
    offered = []
    monkeypatch.setattr(routes_orders, "offer_send", lambda conn, card_id, stamp: offered.append(card_id))
    post(owner, "/orders/%d/oth-back" % old, tok, answer="same")
    assert offered == [old]


def test_the_window_saves_as_before_for_any_other_order(tmp_path):
    app, owner, tok, conn = panel(tmp_path)
    here = card_at(conn, "moving", T)
    r = post(owner, "/orders/%d/save" % here, tok, comment="позвонить")
    assert r.status_code == 302 and orders.get_card(conn, here)["comment"] == "позвонить"
