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
    for code in ("done", "cancel", "return"):
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


@pytest.mark.parametrize("journal", ["delete", "wal"])
def test_two_tabs_at_once_make_one_order(tmp_path, journal):
    """The second tab waits for the first one's write and then sees its order: the check and the insert are one transaction (both journals)."""
    conn = db(tmp_path)
    assert conn.execute("PRAGMA journal_mode=%s" % journal).fetchone()[0] == journal
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
    conn.execute("UPDATE order_card SET webmim_number = '5000777' WHERE id = ?", (first,))
    conn.commit()
    answer = mgr.handoff(conn, "m1", "555", {"phone": "+79001112233", "need": "зима"}, "", NOW, None, point=point)
    assert answer == "У клиента уже есть открытый заказ №5000777 — откройте его." and count(conn) == 1    # the number the point knows
    assert conn.execute("SELECT outcome, order_id FROM order_command_log WHERE message_id = 'm1'").fetchone() == ("открытый заказ №5000777", None)
    mgr._note_voice(conn, "m1", "https://vk.com/doc1", NOW)                        # a voice of the refused message goes to no order
    assert not any("голосовое" in e["text"] for e in orders.list_events(conn, first))


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


# ---- one open order per phone and point (the lead 2026-10-27 after the review: the rule and the binding stay inside the point) ----------

def two_points(conn):
    acc_a = connections.create_account(conn, "Кабинет А", NOW, "o")
    acc_b = connections.create_account(conn, "Кабинет Б", NOW, "o")
    pa = points.save_point(conn, dict(name="Руставели", channel="vk", avito_account_id=str(acc_a)))
    pb = points.save_point(conn, dict(name="Юнона", channel="vk", avito_account_id=str(acc_b)))
    return acc_a, pa, pb


def chat_of(conn, account, text="Здравствуйте, 8 900 000 00 17"):
    fake = Fake()
    fake.add("c1", text, 1)
    chats.sync_account(conn, fake, account, NOW)
    return next(c["id"] for c in chats.list_chats(conn) if c["account_id"] == account)


def test_an_open_order_of_another_point_blocks_nothing_and_one_of_no_point_blocks_everyone(tmp_path):
    conn = db(tmp_path)
    acc_a, pa, pb = two_points(conn)
    of_b = orders.create_card(conn, dict(client_phone="+79001112233", point_id=str(pb)), NOW, "o")
    of_a = orders.create_card(conn, dict(client_phone="+79001112233", point_id=str(pa)), NOW, "o")            # another point: allowed
    with pytest.raises(orders.OpenOrderExists) as err:
        orders.create_card(conn, dict(client_phone="8 900 111 22 33", point_id=str(pa)), NOW, "o")
    assert err.value.order_id == of_a
    with pytest.raises(orders.OpenOrderExists):                                         # an order of no point stands beside none
        orders.create_card(conn, dict(client_phone="+79001112233"), NOW, "o")
    other = orders.create_card(conn, dict(client_phone="+79004445566"), NOW, "o")       # no point, nobody else's phone
    with pytest.raises(orders.OpenOrderExists) as err:
        orders.create_card(conn, dict(client_phone="+79004445566", point_id=str(pb)), NOW, "o")
    assert err.value.order_id == other and of_b != of_a


def test_a_chat_of_point_a_does_not_join_the_open_order_of_point_b_and_gets_its_own(tmp_path):
    conn = db(tmp_path)
    acc_a, pa, pb = two_points(conn)
    of_b = orders.create_card(conn, dict(client_name="Валера", client_phone="+79000000017", point_id=str(pb)), NOW, "o")
    row = chat_of(conn, acc_a)
    made = deals.create_deal(conn, row, NOW, "pavel")
    assert made != of_b and orders.get_card(conn, made)["point_id"] == pa and chats.get_chat(conn, row)["order_id"] == made
    assert not any("Привязан чат" in e["text"] for e in orders.list_events(conn, of_b))


def test_a_chat_of_point_a_joins_an_open_order_of_no_point_which_becomes_point_a(tmp_path):
    conn = db(tmp_path)
    acc_a, pa, pb = two_points(conn)
    common = orders.create_card(conn, dict(client_name="Валера", client_phone="+79000000017"), NOW, "o")
    row = chat_of(conn, acc_a)
    assert deals.create_deal(conn, row, NOW, "pavel") == common and count(conn) == 1
    assert orders.get_card(conn, common)["point_id"] == pa


def test_the_wait_button_in_a_chat_of_point_a_never_touches_the_order_of_point_b(tmp_path):
    conn = db(tmp_path)
    acc_a, pa, pb = two_points(conn)
    of_b = orders.create_card(conn, dict(client_name="Валера", client_phone="+79000000017", point_id=str(pb)), NOW, "o")
    row = chat_of(conn, acc_a)
    card_id, added, moved = deals.wait_from_chat(conn, row, {"width": "205", "profile": "55", "radius": "16", "season": "winter", "qty": "4"}, [], NOW, "a")
    assert card_id != of_b and orders.get_card(conn, of_b)["stage"]["code"] == "new" and orders.get_card(conn, of_b)["wants"] == []


def test_a_call_to_point_a_makes_an_order_beside_the_open_order_of_point_b(tmp_path):
    from test_calls import FakeClient, call, chats_on
    conn = db(tmp_path)
    acc_a, pa, pb = two_points(conn)
    chats_on(conn, acc_a)
    orders.create_card(conn, dict(client_phone="+79111234567", point_id=str(pb)), NOW, "o")
    calls.sync_account(conn, FakeClient([[call(1)]]), acc_a, NOW, lambda s: None)
    made = calls.create_order(conn, calls.list_calls(conn)[0]["id"], NOW, "pavel")
    assert orders.get_card(conn, made)["point_id"] == pa and count(conn) == 2


def test_our_own_numbers_never_block(tmp_path):
    conn = db(tmp_path)
    points.save_point(conn, dict(name="Гамма", channel="vk", manager_phone="+7 911 222-33-44"))
    orders.create_card(conn, dict(client_name="Первый", client_phone="+79112223344"), NOW, "o")
    orders.create_card(conn, dict(client_name="Второй", client_phone="8 911 222 33 44"), NOW, "o")
    assert count(conn) == 2


def test_a_phone_the_client_writes_later_is_not_put_into_a_second_open_order(tmp_path):
    conn = db(tmp_path)
    account = connections.create_account(conn, "Основной", NOW, "o")
    first = orders.create_card(conn, dict(client_name="Валера", client_phone="+79000000017"), NOW, "o")
    fake = Fake()
    fake.add("c1", "Здравствуйте", 1)
    chats.sync_account(conn, fake, account, NOW)
    row = chats.list_chats(conn)[0]["id"]
    deal = deals.create_deal(conn, row, NOW, "pavel")                                    # no phone yet: its own deal
    assert deal != first
    conn.execute("INSERT INTO avito_message (chat_row, message_id, at, ts, direction, kind, text) VALUES (?, 'm9', ?, 1791900100, 'in', 'text', ?)",
                 (row, NOW, "мой номер 8 900 000 00 17"))
    conn.commit()
    for _ in range(2):
        deals.process_all(conn, NOW)
    assert orders.get_card(conn, deal)["client_phone"] == ""
    notes = [e for e in orders.list_events(conn, deal) if "уже есть открытый заказ №%d" % first in e["text"]]
    assert len(notes) == 1 and notes[0]["level"] == "yellow"


def test_a_new_order_after_a_closed_one_joins_the_same_client(tmp_path):
    from tires_sync import clients
    conn = db(tmp_path)
    first = orders.create_card(conn, dict(client_name="Иван", client_phone="+79001112233"), NOW, "o")
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (stage(conn, "done"), first))
    conn.commit()
    second = orders.create_card(conn, dict(client_name="Иван", client_phone="8 900 111 22 33"), NOW, "o")
    assert clients.client_of_order(conn, second)["id"] == clients.client_of_order(conn, first)["id"]


def test_a_manager_of_point_a_on_a_shared_account_never_joins_the_order_of_point_b_the_client_named(tmp_path, monkeypatch):
    conn = db(tmp_path)
    shared = connections.create_account(conn, "Общий кабинет", NOW, "o")
    pa = points.save_point(conn, dict(name="Руставели", channel="vk", avito_account_id=str(shared)))
    pb = points.save_point(conn, dict(name="Юнона", channel="vk", avito_account_id=str(shared)))           # two points: no point of the account
    of_b = orders.create_card(conn, dict(client_name="Валера", client_phone="+79000000017", point_id=str(pb)), NOW, "o")
    monkeypatch.setattr(deals, "named_point", lambda texts, pts: (next(p for p in pts if p["id"] == pb), texts[0]))   # «заберу на Юноне»
    row = chat_of(conn, shared)
    made = deals.create_deal(conn, row, NOW, "manager-a", own_point=pa)
    assert made != of_b and chats.get_chat(conn, row)["order_id"] == made
    assert [e["text"] for e in orders.list_events(conn, of_b)] == [e["text"] for e in orders.list_events(conn, of_b) if "Заказ создан" in e["text"]]


def test_a_handover_of_an_early_chat_order_keeps_out_a_phone_of_another_open_order_of_the_point(tmp_path):
    conn = db(tmp_path)
    acc_a, pa, pb = two_points(conn)
    row = chat_of(conn, acc_a, text="Здравствуйте")
    early = deals.create_deal(conn, row, NOW, "pavel")
    busy = orders.create_card(conn, dict(client_phone="+79001112233", point_id=str(pa)), NOW, "o")
    made, _answer = mgr.hand_over(conn, pa, {"kind": "chat", "chat_row": row}, "+7 900 111-22-33", mgr.handoff_fields({}), "ab" * 12, "a", NOW)
    assert made == early and orders.get_card(conn, early)["client_phone"] == ""
    assert any("уже есть открытый заказ №%d" % busy in e["text"] and e["level"] == "yellow" for e in orders.list_events(conn, early))


def test_the_record_of_a_later_phone_never_names_the_order_of_another_point(tmp_path):
    conn = db(tmp_path)
    acc_a, pa, pb = two_points(conn)
    general = connections.create_account(conn, "Основной", NOW, "o")
    of_b = orders.create_card(conn, dict(client_phone="+79000000017", point_id=str(pb)), NOW, "o")
    row = chat_of(conn, general, text="Здравствуйте")
    deal = deals.create_deal(conn, row, NOW, "pavel")                                     # a deal of no point
    conn.execute("INSERT INTO avito_message (chat_row, message_id, at, ts, direction, kind, text) VALUES (?, 'm9', ?, 1791900100, 'in', 'text', ?)",
                 (row, NOW, "мой номер 8 900 000 00 17"))
    conn.commit()
    deals.process_all(conn, NOW)
    texts = [e["text"] for e in orders.list_events(conn, deal)]
    assert any("открытый заказ другой точки" in t for t in texts) and not any("№%d" % of_b in t for t in texts)
