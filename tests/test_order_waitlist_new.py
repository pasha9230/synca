"""The owner 2026-11-26: «Добавить заказ» is on view in the order's card again (both views, the office only, one button), and the window of a
new order has «Лист ожидания»: the client's wishes in words, «можно другие бренды», «можно другие размеры», «Поставить в ожидание» makes the order on «Ожидает наличие»."""
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
def test_the_card_has_one_add_order_button_beside_merge_for_the_office(w, view):
    flag(w, view)
    card_id = _card(w["conn"])
    for client in (w["owner"], person(w, "chat1")):
        page = client.get("/orders/%d" % card_id).get_data(as_text=True)
        assert ("o2-card" in page) == (view == "all")                              # «Новый вид» draws office2/order_card.html
        assert page.count("data-chain-again") == 1 and "Добавить заказ</button>" in page
        assert "Новый заказ этому клиенту</button>" not in page                   # no second button in «Другие заказы клиента»
        row = page[page.index("data-order-actions"):]
        row = row[:row.index("</div>")]                                            # the owner 26.11: the two buttons side by side
        assert 'class="btn btn--secondary btn--sm" href="/orders/%d/merge" data-merge-open' % card_id in row and "data-chain-again" in row
        assert page.index("data-order-actions") < page.index('id="ow-again"')
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
    assert any(t.startswith("Поставлен в лист ожидания. Ждёт: %s" % WISH) and "Можно другие бренды: да" in t and "Можно другие размеры: нет" in t for t in texts)
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
    assert "Можно другие бренды: да" in texts and any(t.startswith("Пожелания клиента (лист ожидания): ") for t in texts)
    with pytest.raises(orders.OrderError, match="что нужно клиенту"):              # «Ожидает наличие» keeps what the client waits for
        orders.update_card(conn, card_id, {"wait_wish": ""}, NOW, "o")
    line = order_wait.add_want(conn, card_id, {"width": "205", "profile": "55", "radius": "16", "qty": "4"}, NOW, "o")
    want_id = line if isinstance(line, int) else order_wait.list_wants(conn, card_id)[0]["id"]
    order_wait.remove_want(conn, want_id, NOW, "o")                                 # the words hold the stage: the last line may go
    assert orders.get_card(conn, card_id)["wants"] == []


def _waiting(w, **extra):
    reply = _new(w, wait_on="1", wait_wish=WISH, **extra)
    return int(reply.headers["Location"].rsplit("/", 1)[1])


def test_analogs_tick_other_brands_in_the_lines(w):
    conn = w["conn"]
    card_id = _waiting(w)
    order_wait.add_want(conn, card_id, {"width": "205", "profile": "55", "radius": "16", "qty": "4", "brand": "Ikon"}, NOW, "o")
    assert order_wait.list_wants(conn, card_id)[0]["any_brand"] is False
    orders.update_card(conn, card_id, {"wait_analogs": "1"}, NOW, "o")                 # ticked in the card: the line allows other brands
    assert order_wait.list_wants(conn, card_id)[0]["any_brand"] is True
    assert any("можно другие бренды" in e["text"] for e in orders.list_events(conn, card_id))
    order_wait.add_want(conn, card_id, {"width": "195", "profile": "65", "radius": "15", "qty": "4"}, NOW, "o")
    assert [x["any_brand"] for x in order_wait.list_wants(conn, card_id)] == [True, True]     # a later line too
    other = _waiting(w, client_phone="+7 900 555-07-08", wait_analogs="1")              # ticked when the order is made
    order_wait.add_want(conn, other, {"width": "205", "profile": "55", "radius": "16", "qty": "4"}, NOW, "o")
    assert order_wait.list_wants(conn, other)[0]["any_brand"] is True


def test_the_card_of_a_chat_order_puts_it_waiting_with_the_same_block(w):
    conn = w["conn"]
    card_id = _card(conn)                                                           # made like «Создать заказ» of a chat: no form before
    page = w["owner"].get("/orders/%d" % card_id).get_data(as_text=True)
    assert "data-waitlist" in page and 'name="wait_on"' in page
    was = __import__("re").search(r'name="was" value="([^"]*)"', page).group(1)
    import html
    reply = post(w["owner"], "/orders/%d/save" % card_id, w["tok"], was=html.unescape(was), wait_on="1", wait_wish=WISH)
    assert reply.status_code == 302
    card = orders.get_card(conn, card_id)
    assert card["stage"]["code"] == "wait" and card["wait_wish"] == WISH
    assert any(e["text"].startswith("Поставлен в лист ожидания") for e in orders.list_events(conn, card_id))


def test_matching_variants_open_the_catalog_search_on_the_wait_stage(w):
    conn = w["conn"]
    card_id = _waiting(w)
    page = w["owner"].get("/orders/%d" % card_id).get_data(as_text=True)
    assert 'href="/catalog?size=205%2F55R16&amp;s=winter"' in page and "data-match-open" in page
    order_wait.add_want(conn, card_id, {"width": "195", "profile": "65", "radius": "15", "qty": "4", "season": "winter"}, NOW, "o")
    page = w["owner"].get("/orders/%d" % card_id).get_data(as_text=True)
    assert 'href="/catalog?size=195%2F65R15+205%2F55R16&amp;s=winter"' in page
    assert w["owner"].get("/catalog?size=205%2F55R16&s=winter").status_code == 200
    plain = _card(conn, phone="+7 900 555-09-10")                                   # not waiting: no button
    assert "data-match-open" not in w["owner"].get("/orders/%d" % plain).get_data(as_text=True)


@pytest.mark.parametrize("view", ["off", "all"])
def test_the_new_order_is_the_same_window_as_the_card(w, view):
    flag(w, view)
    page = w["owner"].get("/orders/new").get_data(as_text=True)
    assert ("o2c-body" in page) == (view == "all") and "data-waitlist" in page       # «Новый вид»: the card's three columns
    assert 'name="webmim_id"' in page and 'name="reserve_until"' in page and 'name="avito_chat"' in page


def test_new_order_of_orders_opens_the_same_card_empty_on_the_first_stage(w):
    conn = w["conn"]
    board = w["owner"].get("/orders").get_data(as_text=True)
    assert 'action="/orders/blank"' in board and 'href="/orders/new"' not in board
    reply = post(w["owner"], "/orders/blank", w["tok"])
    card_id = int(reply.headers["Location"].rsplit("/", 1)[1])
    card = orders.get_card(conn, card_id)
    assert card["stage"]["code"] == "new" and card["client_name"] == "" and card["items"] == []
    again = post(w["owner"], "/orders/blank", w["tok"])                             # a double press: the same empty order
    assert again.headers["Location"].endswith("/orders/%d" % card_id)
    page = w["owner"].get("/orders/%d" % card_id).get_data(as_text=True)
    assert 'id="ow-main"' in page and "data-waitlist" in page and 'name="want_width"' in page and "data-order-actions" in page


def test_other_sizes_is_a_tick_of_its_own(w):
    conn = w["conn"]
    card_id = _waiting(w, wait_other_sizes="1")
    card = orders.get_card(conn, card_id)
    assert card["wait_other_sizes"] == 1 and card["wait_analogs"] == 0
    order_wait.add_want(conn, card_id, {"width": "205", "profile": "55", "radius": "16", "qty": "4"}, NOW, "o")
    line = order_wait.list_wants(conn, card_id)[0]
    assert line["any_size"] is True and line["any_brand"] is False
    page = w["owner"].get("/orders/%d" % card_id).get_data(as_text=True)
    assert "data-wait-sizes" in page and 'name="wait_other_sizes"' in page


def test_a_line_typed_in_the_card_is_saved_with_the_window(w):
    import html
    import re
    conn = w["conn"]
    card_id = _card(conn)
    page = w["owner"].get("/orders/%d" % card_id).get_data(as_text=True)
    assert 'form="ow-main" name="want_add"' in page
    was = html.unescape(re.search(r'name="was" value="([^"]*)"', page).group(1))
    reply = post(w["owner"], "/orders/%d/save" % card_id, w["tok"], was=was, want_width="205", want_profile="55", want_radius="16",
                 want_qty="4", want_season="winter", wait_on="1")                    # «Сохранить» with a line typed and «Поставить в ожидание»
    assert reply.status_code == 302
    card = orders.get_card(conn, card_id)
    assert [(x["width"], x["season"]) for x in card["wants"]] == [(205, "winter")] and card["stage"]["code"] == "wait"
    post(w["owner"], "/orders/%d/save" % card_id, w["tok"], was=was, want_add="1", want_width="195", want_profile="65", want_radius="15", want_qty="4")   # «Добавить строку»
    assert len(order_wait.list_wants(conn, card_id)) == 2


def test_a_point_may_not_make_a_blank_order(w):
    conn = w["conn"]
    before = conn.execute("SELECT COUNT(*) FROM order_card").fetchone()[0]
    reply = post(person(w, "pt1"), "/orders/blank", "tok")
    assert reply.status_code in (302, 403) and "/orders/" not in reply.headers.get("Location", "").replace("/orders/blank", "")
    assert conn.execute("SELECT COUNT(*) FROM order_card").fetchone()[0] == before


def test_an_empty_order_is_on_no_board_and_in_no_analytics_until_filled(w):
    from datetime import datetime
    from tires_sync import analytics
    conn = w["conn"]
    counted = len(analytics._cards(conn))
    card_id = int(post(w["owner"], "/orders/blank", w["tok"]).headers["Location"].rsplit("/", 1)[1])
    assert card_id in orders.empty_ids(conn) and len(analytics._cards(conn)) == counted
    shown = [c["id"] for col in orders.board(conn, datetime(2026, 11, 26, 12))["columns"] for c in col["cards"]]
    assert card_id not in shown
    assert post(w["owner"], "/orders/blank", w["tok"]).headers["Location"].endswith("/orders/%d" % card_id)   # the own empty one, again
    orders.update_card(conn, card_id, {"client_name": "Олег"}, NOW, "o")
    shown = [c["id"] for col in orders.board(conn, datetime(2026, 11, 26, 12))["columns"] for c in col["cards"]]
    assert card_id in shown and len(analytics._cards(conn)) == counted + 1 and card_id not in orders.empty_ids(conn)
    fresh = int(post(w["owner"], "/orders/blank", w["tok"]).headers["Location"].rsplit("/", 1)[1])
    assert fresh != card_id                                                         # filled: the next press makes a new one


def test_a_refused_line_says_it_once(w):
    import html
    import re
    conn = w["conn"]
    card_id = _card(conn)
    page = w["owner"].get("/orders/%d" % card_id).get_data(as_text=True)
    assert 'name="want_add" value="1" formnovalidate' in page
    was = html.unescape(re.search(r'name="was" value="([^"]*)"', page).group(1))
    post(w["owner"], "/orders/%d/save" % card_id, w["tok"], was=was, want_add="1", want_brand="Ikon", want_qty="4")
    with w["owner"].session_transaction() as s:
        texts = [f[1] for f in s.get("_flashes", [])]
    assert "Сохранено." not in texts and len([t for t in texts if "не добавлена" in t]) == 1, texts
