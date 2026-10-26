"""One open order per client phone (the owner 2026-10-27, replacing the warning of 2026-10-25): a new order on any path is refused while an
order of the same phone stands on an open stage; a chat of the same client joins that order instead. Orders without a phone are untouched."""
from __future__ import annotations

import sqlite3
import threading

import pytest

from panel_helpers import login, make_app, post
from tires_sync import calls, chat_deals as deals, chats, connections, order_manager as mgr, order_points as points, orders, store

NOW = "2026-10-27T12:00:00"


def db(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    orders.ensure_pipeline(conn, NOW)
    return conn


def stage(conn, code):
    return next(s["id"] for s in orders.list_stages(conn) if s["code"] == code)


def count(conn):
    return conn.execute("SELECT COUNT(*) FROM order_card").fetchone()[0]


def test_a_second_order_on_the_same_phone_typed_otherwise_is_refused_with_the_number(tmp_path):
    conn = db(tmp_path)
    first = orders.create_card(conn, dict(client_name="Иван", client_phone="+7 900 111-22-33"), NOW, "o")
    with pytest.raises(orders.OpenOrderExists) as err:
        orders.create_card(conn, dict(client_name="Иван", client_phone="8 (900) 111 22 33"), NOW, "o")
    assert err.value.order_id == first and str(err.value) == "У клиента уже есть открытый заказ №%d — откройте его." % first
    assert isinstance(err.value, orders.OrderError)
    assert count(conn) == 1 and not conn.in_transaction                                   # nothing of the refused one is left, nothing hangs open


def test_closed_orders_another_phone_and_orders_without_a_phone_are_not_affected(tmp_path):
    conn = db(tmp_path)
    for code in ("done", "cancel", "refused", "return"):
        card = orders.create_card(conn, dict(client_phone="+79001112233"), NOW, "o")
        conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (stage(conn, code), card))
        conn.commit()
    orders.create_card(conn, dict(client_phone="+79001112233"), NOW, "o")                 # every earlier one is finished: allowed
    orders.create_card(conn, dict(client_phone="+79004445566"), NOW, "o")
    orders.create_card(conn, dict(client_name="Без телефона"), NOW, "o")
    orders.create_card(conn, dict(client_name="Без телефона"), NOW, "o")
    with pytest.raises(orders.OpenOrderExists):
        orders.create_card(conn, dict(client_phone="+79001112233"), NOW, "o")


def test_an_order_on_any_open_stage_blocks(tmp_path):
    conn = db(tmp_path)
    card = orders.create_card(conn, dict(client_phone="+79001112233"), NOW, "o")
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (stage(conn, "wait"), card))
    conn.commit()
    with pytest.raises(orders.OpenOrderExists):
        orders.create_card_with_items(conn, dict(client_phone="+79001112233"), [], NOW, "o")
    assert count(conn) == 1


def test_two_tabs_at_once_make_one_order(tmp_path):
    """The second tab waits for the first one's write and then sees its order: the check and the insert are one transaction."""
    conn = db(tmp_path)
    other = sqlite3.connect(str(tmp_path / "x.db"), timeout=store.BUSY_SECONDS, check_same_thread=False)   # the second tab, opened before
    orders.create_card(conn, dict(client_phone="+79001112233"), NOW, "o", commit=False)  # the first tab is inside its write
    result = {}

    def second_tab():
        try:
            result["id"] = orders.create_card(other, dict(client_phone="8 900 111 22 33"), NOW, "o")
        except Exception as exc:                                                        # noqa: BLE001 (the test reads what came)
            result["error"] = exc

    worker = threading.Thread(target=second_tab)
    worker.start()
    worker.join(0.5)
    assert worker.is_alive(), result                                                     # it waits for the lock, it does not read past it
    conn.commit()
    worker.join(10)
    assert isinstance(result.get("error"), orders.OpenOrderExists) and count(conn) == 1


def test_the_window_of_a_new_order_says_it_and_links_the_open_order(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    tok = login(client)
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, NOW)
    first = orders.create_card(conn, dict(client_name="Иван", client_phone="+79001112233"), NOW, "o")
    r = post(client, "/orders/new", tok, client_name="Иван", client_phone="8 900 111-22-33", comment="второй комплект")
    page = r.get_data(as_text=True)
    assert r.status_code == 200 and "У клиента уже есть открытый заказ №%d — откройте его." % first in page
    assert 'href="/orders/%d" data-open-order' % first in page and "второй комплект" in page          # the link, and nothing typed is lost
    assert count(conn) == 1


def test_a_call_of_a_client_with_an_open_order_is_refused_and_the_call_stays_free(tmp_path):
    conn = db(tmp_path)
    account = connections.create_account(conn, "Основной", NOW, "o")
    from test_calls import FakeClient, call, chats_on
    chats_on(conn, account)
    first = orders.create_card(conn, dict(client_phone="+79111234567"), NOW, "o")
    calls.sync_account(conn, FakeClient([[call(1)]]), account, NOW, lambda s: None)
    row = calls.list_calls(conn)[0]["id"]
    with pytest.raises(orders.OpenOrderExists) as err:
        calls.create_order(conn, row, NOW, "pavel")
    assert err.value.order_id == first and count(conn) == 1
    assert conn.execute("SELECT order_id FROM avito_call WHERE id = ?", (row,)).fetchone()[0] is None    # the claim is given back


def test_a_vk_handoff_of_a_client_with_an_open_order_answers_with_its_number(tmp_path):
    conn = db(tmp_path)
    first = orders.create_card(conn, dict(client_phone="+79001112233"), NOW, "o")
    point = points.get_point(conn, points.save_point(conn, dict(name="Бета", channel="vk", vk_peer_id="555")))
    answer = mgr.handoff(conn, "m1", "555", {"phone": "+79001112233", "need": "зима"}, "", NOW, None, point=point)
    assert answer == "У клиента уже есть открытый заказ №%d — откройте его." % first and count(conn) == 1
    assert conn.execute("SELECT outcome, order_id FROM order_command_log WHERE message_id = 'm1'").fetchone() == ("открытый заказ", first)


class Fake:
    def __init__(self):
        self.items, self.messages = [], {}

    def get_self(self):
        return {"id": 1}

    def chats(self, user_id, limit=50, offset=0, retry=True):
        return {"chats": self.items, "meta": {"has_more": False}}

    def chat_messages(self, user_id, chat_id, limit=100, offset=0, retry=True):
        return self.messages.get(chat_id, [])

    def add(self, chat_id, text, item):
        ts = 1_791_900_000
        self.items.append({"id": chat_id, "created": ts - 50, "updated": ts,
                           "context": {"type": "item", "value": {"id": item, "title": "Ikon %d" % item, "url": "https://www.avito.ru/x_%d" % item}},
                           "users": [{"id": 1, "name": "Я"}, {"id": 9, "name": "Валера"}],
                           "last_message": {"id": "l-" + chat_id, "author_id": 9, "created": ts, "content": {"text": text}, "type": "text",
                                            "direction": "in", "read": 0}})
        self.messages[chat_id] = [{"id": chat_id + "-0", "author_id": 9, "created": ts, "content": {"text": text}, "type": "text",
                                   "direction": "in", "isRead": False, "read": 0}]


def test_a_chat_of_the_same_phone_joins_the_open_order_instead_of_a_second_one(tmp_path):
    conn = db(tmp_path)
    account = connections.create_account(conn, "Основной", NOW, "o")
    first = orders.create_card(conn, dict(client_name="Валера", client_phone="+79000000017"), NOW, "o")
    fake = Fake()
    fake.add("c1", "Здравствуйте, 8 900 000 00 17", 1)
    chats.sync_account(conn, fake, account, NOW)
    row = chats.list_chats(conn)[0]["id"]
    assert deals.create_deal(conn, row, NOW, "pavel") == first and count(conn) == 1
    assert chats.get_chat(conn, row)["order_id"] == first and not conn.in_transaction
    events = [e["text"] for e in orders.list_events(conn, first)]
    assert any("Привязан чат" in t for t in events) and any("тот же телефон" in t and "Ikon 1" in t for t in events)
    assert orders.list_items(conn, first) == []                                          # the open order's positions are not touched


def test_a_chat_joined_to_an_order_further_on_is_not_taken_back_to_the_wait_list(tmp_path):
    conn = db(tmp_path)
    account = connections.create_account(conn, "Основной", NOW, "o")
    first = orders.create_card(conn, dict(client_name="Валера", client_phone="+79000000017"), NOW, "o")
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (stage(conn, "placed"), first))
    conn.commit()
    fake = Fake()
    fake.add("c1", "Здравствуйте, 8 900 000 00 17", 1)
    chats.sync_account(conn, fake, account, NOW)
    row = chats.list_chats(conn)[0]["id"]
    with pytest.raises(orders.OrderError, match="уже в работе"):
        deals.wait_from_chat(conn, row, {"width": "205", "profile": "55", "radius": "16", "season": "winter", "qty": "4"}, [], NOW, "pavel")
    assert orders.get_card(conn, first)["stage"]["code"] == "placed"


def test_the_catalog_wait_list_is_refused_for_a_client_with_another_open_order(tmp_path):
    from test_catalog_wait import NAME, PHONE, cards, owner_app, send
    app, client, tok, conn = owner_app(tmp_path)
    first = orders.create_card(conn, dict(client_name=NAME, client_phone=PHONE), NOW, "o")
    r = send(client, tok)
    assert cards(conn) == [first]
    page = client.get(r.headers["Location"]).get_data(as_text=True) if r.status_code == 302 else r.get_data(as_text=True)
    assert "У клиента уже есть открытый заказ №%d" % first in page


def test_the_point_page_refuses_with_a_link_and_offers_no_way_around(tmp_path):
    from test_mypoint import handed, handoff_token, manager
    from test_vk_commands import ready
    app, client, tok, conn, pid, card = ready(tmp_path)
    mine = manager(app, conn, pid)
    token = handoff_token(mine.get("/my-point").get_data(as_text=True))
    r = post(mine, "/my-point", "tok", token=token, phone="8 (900) 111-22-33", need="второй комплект")
    page = r.get_data(as_text=True)
    assert r.status_code == 200 and 'id="mp-dup"' in page and "У клиента уже есть открытый заказ № 5000100" in page
    assert "откройте его" in page and 'href="#order-%d"' % card in page
    assert "data-dup-force" not in page and "Всё равно создать" not in page
    r = post(mine, "/my-point", "tok", token=token, phone="8 (900) 111-22-33", need="x", force="+79001112233")   # an old form's «force» is ignored
    assert r.status_code == 200 and 'id="mp-dup"' in r.get_data(as_text=True) and handed(conn) == []
