"""The card of an order in the panel: «Что нужно клиенту», «Ответственный», the settings for the robot's orders (W1c)."""
from __future__ import annotations

from datetime import datetime

from helpers import make_offer
from panel_helpers import login, make_app, post
from tires_sync import order_wait, orders, store, users
from tires_sync.models import Season

WANT = dict(brand="Ikon Tyres", width="205", profile="55", radius="16", season="winter", studded="no", qty="4")


def setup(tmp_path):
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 10, 15, 9, 0, 0))
    client = app.test_client()
    tok = login(client)
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, "2026-10-15T12:00:00")
    return app, client, tok, conn


def new_card(conn):
    return orders.create_card(conn, {"client_name": "Иван"}, "2026-10-15T12:00:00", "owner")


def text(client, url):
    return client.get(url).get_data(as_text=True)


def test_the_card_has_a_tab_for_what_the_client_wants_and_a_line_can_be_added(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    card_id = new_card(conn)
    page = text(client, "/orders/%d?tab=wants" % card_id)
    assert "Что нужно" in page and 'action="/orders/%d/wants/add"' % card_id in page
    assert post(client, "/orders/%d/wants/add" % card_id, tok, **WANT, any_brand="on").status_code == 302
    [line] = order_wait.list_wants(conn, card_id)
    assert line["width"] == 205 and line["qty"] == 4 and line["any_brand"] and not line["any_size"]
    page = text(client, "/orders/%d?tab=wants" % card_id)
    assert "205/55 R16" in page and "можно другие бренды" in page


def test_a_wrong_line_is_refused_with_a_reason_and_nothing_is_saved(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    card_id = new_card(conn)
    post(client, "/orders/%d/wants/add" % card_id, tok, **{**WANT, "qty": "0"})
    assert order_wait.list_wants(conn, card_id) == []
    assert "Количество" in text(client, "/orders/%d?tab=wants" % card_id)


def test_a_tick_can_be_taken_off_because_the_form_says_so_explicitly(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    card_id = new_card(conn)
    post(client, "/orders/%d/wants/add" % card_id, tok, **WANT, any_brand="on", any_size="on")
    [line] = order_wait.list_wants(conn, card_id)
    post(client, "/orders/%d/wants/%d/update" % (card_id, line["id"]), tok, **WANT, any_size="on")           # «другие бренды» is not ticked any more
    [again] = order_wait.list_wants(conn, card_id)
    assert not again["any_brand"] and again["any_size"]


def test_a_line_can_be_removed_but_not_the_last_one_of_a_waiting_card(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    card_id = new_card(conn)
    order_wait.add_want(conn, card_id, WANT, "2026-10-15T12:00:00", "owner")
    orders.move_card(conn, card_id, {s["code"]: s["id"] for s in orders.list_stages(conn)}["wait"], "2026-10-15T12:00:00", "owner")
    [line] = order_wait.list_wants(conn, card_id)
    post(client, "/orders/%d/wants/%d/delete" % (card_id, line["id"]), tok)
    assert len(order_wait.list_wants(conn, card_id)) == 1
    assert "последн" in text(client, "/orders/%d?tab=wants" % card_id)
    assert post(client, "/orders/%d/wants/999/delete" % card_id, tok).status_code == 404                    # a line of nowhere


def test_a_line_of_another_card_cannot_be_changed_through_this_one(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    mine, other = new_card(conn), new_card(conn)
    order_wait.add_want(conn, other, WANT, "2026-10-15T12:00:00", "owner")
    [line] = order_wait.list_wants(conn, other)
    assert post(client, "/orders/%d/wants/%d/delete" % (mine, line["id"]), tok).status_code == 404
    assert post(client, "/orders/%d/wants/%d/update" % (mine, line["id"]), tok, **WANT).status_code == 404
    assert len(order_wait.list_wants(conn, other)) == 1


def test_the_responsible_manager_is_chosen_on_the_card_from_the_panel_users(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    users.add_user(conn, "worker2", "пароль-для-партнёра-1", "2026-10-15T12:00:00")
    card_id = new_card(conn)
    page = text(client, "/orders/%d?tab=main" % card_id)
    assert 'name="responsible"' in page and 'value="worker2"' in page
    post(client, "/orders/%d/save" % card_id, tok, responsible="worker2", client_name="Иван")
    assert orders.get_card(conn, card_id)["responsible"] == "worker2"


def test_the_orders_settings_keep_the_default_responsible_manager_of_the_robot(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    users.add_user(conn, "worker2", "пароль-для-партнёра-1", "2026-10-15T12:00:00")
    assert "Ответственный по умолчанию" in text(client, "/orders/settings")
    post(client, "/orders/settings/responsible", tok, responsible="worker2")
    assert orders.default_responsible(conn) == "worker2"
    post(client, "/orders/settings/responsible", tok, responsible="")
    assert orders.default_responsible(conn) == ""


def test_the_found_tire_leaves_a_task_with_its_text_on_the_card_and_a_tag_on_the_board(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    card_id = new_card(conn)
    order_wait.add_want(conn, card_id, WANT, "2026-10-15T12:00:00", "owner")
    orders.move_card(conn, card_id, {s["code"]: s["id"] for s in orders.list_stages(conn)}["wait"], "2026-10-15T12:00:00", "owner")
    order_wait.check(conn, [make_offer(brand_key="IKON TYRES", width=205, height=55, diameter=16, season=Season.WINTER, studded=False, stock=8)],
                     "2026-10-15T13:00:00")
    page = text(client, "/orders/%d" % card_id)
    assert "Позиция появилась: предложите клиенту" in page and "Появилось" in page and "Ожидает наличие" in page
    assert "Появилось" in text(client, "/orders")


# ---- the review of W1b and W1c

def test_saving_the_main_tab_does_not_write_the_default_responsible_into_the_card(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    card_id = new_card(conn)                                                                                   # nobody is named: the maker (owner) answers
    before = len(orders.list_events(conn, card_id))
    post(client, "/orders/%d/save" % card_id, tok, responsible="owner", client_name="Иван", comment="позвонить")
    assert conn.execute("SELECT responsible FROM order_card WHERE id = ?", (card_id,)).fetchone()[0] == ""
    assert not any("Ответственный" in e["text"] for e in orders.list_events(conn, card_id)[before:])
    post(client, "/orders/%d/save" % card_id, tok, responsible="x" * 61)
    assert conn.execute("SELECT responsible FROM order_card WHERE id = ?", (card_id,)).fetchone()[0] == ""


def test_a_failed_check_leaves_nothing_half_written_in_the_connection(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from tires_sync import cli
    conn = store.connect(str(tmp_path / "x.db"))

    def boom(conn, offers, now):
        conn.execute("INSERT INTO app_state VALUES ('half', 'written')")
        raise RuntimeError("boom")
    monkeypatch.setattr(order_wait, "check", boom)
    cli._check_waiting(conn, SimpleNamespace(offers={}))
    assert conn.execute("SELECT 1 FROM app_state WHERE key = 'half'").fetchone() is None


def test_a_cycle_where_the_guard_tripped_does_not_judge_the_waiting_clients(tmp_path, monkeypatch):
    from datetime import datetime
    from test_review4_fixes import OK, ScriptedHttp, goods, published_ten
    from tires_sync import cli
    conn, key_file, box = published_ten(tmp_path)
    conn.close()
    (tmp_path / "unload.xml").write_bytes(goods(2))                                                            # eight positions left: the guard stops the cycle
    (tmp_path / "reference.txt").write_text('<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>', encoding="utf-8")
    monkeypatch.setattr(cli, "_http", lambda: ScriptedHttp(OK, OK, OK, OK))
    monkeypatch.setattr(cli, "moscow_now", lambda: datetime(2026, 10, 7, 12, 0))
    calls = []
    monkeypatch.setattr(cli, "_check_waiting", lambda conn, parsed, box=None: calls.append(1))
    args = ["--db", str(tmp_path / "t.db"), "--unload", str(tmp_path / "unload.xml"), "--reference", str(tmp_path / "reference.txt"),
            "--feed-root", str(tmp_path / "feed"), "--key-file", str(key_file)]
    assert cli.main(["run-cycle", "--publish"] + args) == 3
    assert calls == []


# ---- W2c: the message to the client, one press, through the chat of the order

def chat_with_order(tmp_path, send=True, want=None, offer=None):
    """A panel with an Avito chat bound to an order that waits for a tire that has just been found."""
    from test_chat_deals import Fake, sync
    from tires_sync import chats, connections
    tmp_path.mkdir(exist_ok=True)
    app, client, tok, conn = setup(tmp_path)
    account = connections.create_account(conn, "Основной", "2026-10-15T12:00:00", "o")
    connections.set_modes(conn, account, True, True, "2026-10-15T12:00:00", "o")                               # the chats of the account are read
    if send:
        connections.set_chats_send(conn, account, True, "2026-10-15T12:00:00", "o")                           # and answers may be sent
    fake = Fake()
    fake.add("c1", "Татьяна", [("in", "Здравствуйте, нужна шина")])
    sync(conn, account, fake)
    [chat] = chats.list_chats(conn)
    card_id = chat["order_id"]
    order_wait.add_want(conn, card_id, want or WANT, "2026-10-15T12:00:00", "owner")
    orders.move_card(conn, card_id, {s["code"]: s["id"] for s in orders.list_stages(conn)}["wait"], "2026-10-15T12:00:00", "owner")
    order_wait.check(conn, [offer or make_offer(brand_key="IKON TYRES", width=205, height=55, diameter=16, season=Season.WINTER, studded=False, stock=8)],
                     "2026-10-15T13:00:00")
    return client, conn, card_id, chat


def test_the_found_tire_offers_the_message_to_the_client_through_the_chat_of_the_order(tmp_path):
    client, conn, card_id, chat = chat_with_order(tmp_path)
    page = text(client, "/orders/%d" % card_id)
    assert 'action="/chats/%d/send"' % chat["id"] in page and 'name="reply_token"' in page
    assert "появились в наличии" in page and "205/55 R16" in page and "Отправить клиенту в чат Авито" in page
    assert "можно другие бренды" not in page.split("Отправить клиенту в чат Авито")[0].split("появились в наличии")[-1]     # the client is not told about the manager's ticks


def test_no_message_form_when_the_account_may_not_send_or_for_analogs_or_without_a_chat(tmp_path):
    client, conn, card_id, chat = chat_with_order(tmp_path / "off", send=False)
    assert "Отправить клиенту в чат Авито" not in text(client, "/orders/%d" % card_id)
    (tmp_path / "plain").mkdir()
    app, client2, tok, conn2 = setup(tmp_path / "plain")
    plain = new_card(conn2)
    order_wait.add_want(conn2, plain, {**WANT, "any_brand": True}, "2026-10-15T12:00:00", "owner")
    orders.move_card(conn2, plain, {s["code"]: s["id"] for s in orders.list_stages(conn2)}["wait"], "2026-10-15T12:00:00", "owner")
    order_wait.check(conn2, [make_offer(brand_key="KUMHO", width=205, height=55, diameter=16, season=Season.WINTER, studded=False, stock=8, code="K1")],
                     "2026-10-15T13:00:00")
    page = text(client2, "/orders/%d" % plain)
    assert "Есть аналоги" in page and "Отправить клиенту в чат Авито" not in page                              # analogs are for the manager only, and there is no chat


def test_an_analog_leaves_no_message_form_for_the_client_even_with_a_chat_that_may_send(tmp_path):
    client, conn, card_id, chat = chat_with_order(
        tmp_path, want={**WANT, "any_brand": True},
        offer=make_offer(brand_key="KUMHO", width=205, height=55, diameter=16, season=Season.WINTER, studded=False, stock=8, code="K1"))
    page = text(client, "/orders/%d" % card_id)
    assert "Есть аналоги" in page and "Отправить клиенту в чат Авито" not in page and "появились в наличии" not in page


def test_the_send_mark_of_the_message_to_the_client_is_the_same_on_every_visit_so_a_second_press_sends_nothing(tmp_path):
    import re
    client, conn, card_id, chat = chat_with_order(tmp_path)
    marks = {re.search(r'name="reply_token" value="([^"]+)"', text(client, "/orders/%d" % card_id)).group(1) for _ in range(3)}
    assert len(marks) == 1                                                                                      # chats.send_reply sends one message per mark
