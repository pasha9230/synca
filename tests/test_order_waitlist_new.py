"""The owner 2026-11-26: «Добавить заказ» is on view in the order's card again (both views, the office only, one button), and the window of a
new order has «Лист ожидания»: the client's wishes in words, «подойдут аналоги», «Поставить в ожидание» makes the order on «Ожидает наличие»."""
from __future__ import annotations

import pytest

from panel_helpers import post
from test_office_new_view import cheap_hashes, flag, person, w  # noqa: F401 (the fixtures)
from tires_sync import order_wait, orders

NOW = "2026-11-26T12:00:00"
WISH = "205/55 R16 зима, шипы, до 6000 ₽ за шт."


def _card(conn, phone="+7 900 555-01-02"):
    card_id = orders.create_card(conn, dict(client_name="Пётр", client_phone=phone), NOW, "o", many_open=True)
    conn.commit()
    return card_id


@pytest.mark.parametrize("view", ["off", "all"])
def test_the_card_has_one_add_order_button_in_its_head_for_the_office(w, view):
    flag(w, view)
    card_id = _card(w["conn"])
    for client in (w["owner"], person(w, "chat1")):
        page = client.get("/orders/%d" % card_id).get_data(as_text=True)
        assert ("o2-card" in page) == (view == "all")                              # «Новый вид» draws office2/order_card.html
        assert page.count("data-chain-again") == 1 and "Добавить заказ</button>" in page
        assert "Новый заказ этому клиенту</button>" not in page                   # no second button in «Другие заказы клиента»
        assert page.index("data-chain-again") < page.index('<div class="owin-body">') < page.index('id="ow-again"')
    reply = post(w["owner"], "/orders/%d/again" % card_id, w["tok"])
    assert reply.status_code == 302 and "/orders/" in reply.headers["Location"]


def _new(w, **extra):
    data = dict(client_name="Анна", client_phone="+7 900 555-03-04", source="avito", deal_kind="webmim", responsible="")
    data.update(extra)
    return post(w["owner"], "/orders/new", w["tok"], **data)


def test_the_new_order_window_offers_the_waitlist(w):
    page = w["owner"].get("/orders/new").get_data(as_text=True)
    assert "data-waitlist" in page and 'name="wait_on"' in page and 'name="wait_wish"' in page and 'name="wait_analogs"' in page


def test_put_into_waiting_makes_the_order_on_the_wait_stage_with_its_journal(w):
    conn = w["conn"]
    reply = _new(w, wait_on="1", wait_wish=WISH, wait_analogs="1")
    assert reply.status_code == 302
    card_id = int(reply.headers["Location"].rsplit("/", 1)[1])
    card = orders.get_card(conn, card_id)
    assert card["stage"]["code"] == "wait" and card["wait_wish"] == WISH and card["wait_analogs"] == 1
    texts = [e["text"] for e in orders.list_events(conn, card_id)]
    assert any("Ожидает наличие" in t for t in texts)
    assert any(t.startswith("Поставлен в лист ожидания. Ждёт: %s" % WISH) and "аналоги: да" in t for t in texts)
    page = w["owner"].get("/orders/%d" % card_id).get_data(as_text=True)
    assert "data-waitlist" in page and "205/55 R16 зима, шипы" in page and "data-wait-analogs" in page
    board = w["owner"].get("/orders").get_data(as_text=True)
    assert "data-wait-wish" in board and "Ждёт: 205/55 R16" in board


def test_without_wishes_or_the_tick_nothing_waits(w):
    conn = w["conn"]
    before = conn.execute("SELECT COUNT(*) FROM order_card").fetchone()[0]
    page = _new(w, wait_on="1", wait_wish="  ").get_data(as_text=True)
    assert "опишите, что ждёт клиент" in page and conn.execute("SELECT COUNT(*) FROM order_card").fetchone()[0] == before
    reply = _new(w, client_phone="+7 900 555-05-06", wait_wish=WISH)                 # no tick: an ordinary new order that keeps the words
    card = orders.get_card(conn, int(reply.headers["Location"].rsplit("/", 1)[1]))
    assert card["stage"]["code"] == "new" and card["wait_wish"] == WISH and card["wait_analogs"] == 0


def test_the_wishes_hold_the_stage_and_are_edited_in_the_card(w):
    conn = w["conn"]
    reply = _new(w, wait_on="1", wait_wish=WISH)
    card_id = int(reply.headers["Location"].rsplit("/", 1)[1])
    orders.update_card(conn, card_id, {"wait_wish": WISH + " Nokian", "wait_analogs": "1"}, NOW, "o")
    texts = [e["text"] for e in orders.list_events(conn, card_id)]
    assert "Подойдут аналоги: да" in texts and any(t.startswith("Пожелания клиента (лист ожидания): ") for t in texts)
    with pytest.raises(orders.OrderError, match="что нужно клиенту"):              # «Ожидает наличие» keeps what the client waits for
        orders.update_card(conn, card_id, {"wait_wish": ""}, NOW, "o")
    line = order_wait.add_want(conn, card_id, {"width": "205", "profile": "55", "radius": "16", "qty": "4"}, NOW, "o")
    want_id = line if isinstance(line, int) else order_wait.list_wants(conn, card_id)[0]["id"]
    order_wait.remove_want(conn, want_id, NOW, "o")                                 # the words hold the stage: the last line may go
    assert orders.get_card(conn, card_id)["wants"] == []
