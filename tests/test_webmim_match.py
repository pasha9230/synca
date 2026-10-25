"""The WebMIM orders next to the panel's orders (webmim_match): the snapshot (GET only), the pairs, «Это он», the rights."""
from __future__ import annotations

import re
from datetime import date

import pytest
from cryptography.fernet import Fernet

from http_fakes import ScriptedHttp
from panel_helpers import login, make_app, post
from tires_sync import cli, connections, order_points as points, orders, store, users, webmim_match, webmim_orders
from tires_sync.secretbox import SecretBox

NOW = "2026-10-20T12:00:00"
TODAY = date(2026, 10, 20)
TOKEN = (200, {"accessToken": "TOKEN-123", "tokenType": "bearer"})
PASSWORD = "длинный-пароль-1"


def wm(order_id, number, created, phone="", status="20", text="подтверждено", external=""):
    return {"id": order_id, "number": number, "createDate": created, "statusCode": status, "statusDescription": text,
            "externalNumber": external, "customerName": "Иван", "customerPhone": phone}


def page(rows, pages):
    return (200, rows, {"X-Pagination-Page-Count": str(pages)})


def items(*rows):
    return (200, [{"goodsId": g, "quantity": q, "price": p, "sum": q * p, "goodsName": "Шина " + g} for g, q, p in rows],
            {"X-Pagination-Page-Count": "1"})


def api(*replies):
    http = ScriptedHttp(TOKEN, *replies)
    return webmim_orders.OrdersClient("https://webmim.example", "api-bot", "pw-SECRET-1", http=http), http


def db(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    orders.ensure_pipeline(conn, NOW)
    connections.set_config(conn, "webmim_orders", 0, {"server": "prod"}, NOW, "owner")       # the pairs: the live server (the test one has none)
    conn.commit()
    return conn


def card(conn, at=NOW, phone="+7 (911) 111-22-33", goods=(), **fields):
    card_id = orders.create_card(conn, dict({"client_name": "Иван", "client_phone": phone}, **fields), at, "owner")
    for goods_id, qty in goods:
        orders.add_item(conn, card_id, goods_id=goods_id, name="Шина " + goods_id, brand_key="b", season="summer", diameter=16, territory="",
                        qty=qty, sale_unit=5000, supplier_price=4000, terms=None, fee_pct=0, promo_pct=0, now=at, author="owner")
    return card_id


def no_sleep(_seconds):
    return None


def order_row(conn, order_id):
    return conn.execute("SELECT number, status_code, sum, items IS NOT NULL FROM webmim_order WHERE id = ?", (order_id,)).fetchone()


# ---- the snapshot ---------------------------------------------------------------------------------------------------------------------

def test_the_pages_are_read_newest_first_until_the_window_and_every_call_is_a_get(tmp_path):
    conn = db(tmp_path)
    client, http = api(page([wm(3, 103, "19.10.26 10:00:00"), wm(2, 102, "15.10.26 10:00:00")], 3),
                       page([wm(1, 101, "01.08.26 10:00:00"), wm(0, 100, "01.07.26 10:00:00")], 3),
                       items(("A", 4, 4000)), items(("B", 2, 3000)))
    counts = webmim_match.sync(conn, client, "test", NOW, TODAY, days=60, sleep=no_sleep)
    assert counts == {"pages": 2, "orders": 2, "new": 2, "changed": 0, "items": 2, "undated": 0}           # page 3 is not read: page 2 is past the window
    assert [c[0] for c in http.calls] == ["GET"] * 5
    paths = [c[1].split("webmim.example")[1] for c in http.calls]
    assert paths[1:3] == ["/api/v2/orders?page=1&per-page=50", "/api/v2/orders?page=2&per-page=50"]
    assert paths[3:] == ["/api/v2/orders/3/items?page=1&per-page=100", "/api/v2/orders/2/items?page=1&per-page=100"]
    assert order_row(conn, "3") == ("103", "20", 16000.0, 1) and order_row(conn, "1") is None   # older than 60 days: not kept


def test_oldest_first_is_read_from_the_last_page_backwards(tmp_path):
    conn = db(tmp_path)
    client, http = api(page([wm(1, 101, "01.07.26 10:00:00"), wm(2, 102, "02.07.26 10:00:00")], 3),
                       page([wm(5, 105, "18.10.26 10:00:00"), wm(6, 106, "19.10.26 10:00:00")], 3),
                       page([wm(3, 103, "01.08.26 10:00:00"), wm(4, 104, "10.10.26 10:00:00")], 3),
                       items(), items(), items())
    counts = webmim_match.sync(conn, client, "test", NOW, TODAY, sleep=no_sleep)
    assert [c[1].split("page=")[1][0] for c in http.calls[1:4]] == ["1", "3", "2"]
    assert counts["orders"] == 3 and {r[0] for r in conn.execute("SELECT id FROM webmim_order")} == {"4", "5", "6"}


def test_an_unchanged_order_is_only_touched_and_a_changed_one_reads_its_positions_again(tmp_path):
    conn = db(tmp_path)
    client, _ = api(page([wm(7, 107, "18.10.26 10:00:00")], 1), items(("A", 4, 4000)))
    webmim_match.sync(conn, client, "test", NOW, TODAY, sleep=no_sleep)
    client, http = api(page([wm(7, 107, "18.10.26 10:00:00")], 1))
    assert webmim_match.sync(conn, client, "test", "2026-10-20T13:00:00", TODAY, sleep=no_sleep)["items"] == 0
    assert len(http.calls) == 2
    client, http = api(page([wm(7, 107, "18.10.26 10:00:00", status="70", text="отгружено")], 1), items(("A", 2, 4000)))
    counts = webmim_match.sync(conn, client, "test", "2026-10-20T14:00:00", TODAY, sleep=no_sleep)
    assert counts["changed"] == 1 and counts["items"] == 1 and order_row(conn, "7") == ("107", "70", 8000.0, 1)


def test_a_failure_keeps_what_was_read_and_does_not_retry(tmp_path):
    conn = db(tmp_path)
    client, http = api(page([wm(7, 107, "18.10.26 10:00:00"), wm(8, 108, "17.10.26 10:00:00")], 1), (403, {"x": 1}))
    with pytest.raises(webmim_orders.OrdersError):
        webmim_match.sync(conn, client, "test", NOW, TODAY, sleep=no_sleep)
    assert order_row(conn, "7")[3] == 0 and order_row(conn, "8") is not None and len(http.calls) == 3      # the orders stay, no retry


def test_one_order_whose_positions_are_refused_does_not_stop_the_others(tmp_path):
    conn = db(tmp_path)
    client, http = api(page([wm(7, 107, "18.10.26 10:00:00"), wm(8, 108, "17.10.26 10:00:00")], 1), (404, {"x": 1}), items(("A", 1, 10)))
    assert webmim_match.sync(conn, client, "test", NOW, TODAY, sleep=no_sleep)["items"] == 1
    assert order_row(conn, "7")[3] == 0 and order_row(conn, "8")[3] == 1
    client, http = api(page([wm(7, 107, "18.10.26 10:00:00"), wm(8, 108, "17.10.26 10:00:00")], 1))
    webmim_match.sync(conn, client, "test", NOW, TODAY, sleep=no_sleep)
    assert len(http.calls) == 2                                                           # not asked again until it changes


def test_without_the_count_of_pages_the_pages_are_read_until_a_short_one(tmp_path, monkeypatch):
    monkeypatch.setattr(webmim_match, "PER_PAGE", 2)
    conn = db(tmp_path)
    client, http = api((200, [wm(1, 101, "01.07.26 10:00:00"), wm(2, 102, "18.10.26 10:00:00")]),
                       (200, [wm(3, 103, "19.10.26 10:00:00")]), items(), items())
    assert webmim_match.sync(conn, client, "test", NOW, TODAY, sleep=no_sleep)["pages"] == 2
    assert {r[0] for r in conn.execute("SELECT id FROM webmim_order")} == {"2", "3"}


def test_at_most_two_login_attempts_and_no_secret_in_the_line(tmp_path, monkeypatch):
    conn = db(tmp_path)
    box = SecretBox(Fernet.generate_key())
    connections.set_config(conn, "webmim_orders", 0, {"server": "test", "login": "api-bot", "auth": "login"}, NOW, "owner")
    connections.set_secret(conn, box, "webmim_orders", 0, "password", "pw-SECRET-1", NOW, "owner")
    conn.commit()
    http = ScriptedHttp((401, {"name": "Unauthorized"}), (200, {"result": False, "message": "Неверный логин или пароль"}))
    status, line = webmim_match.run(conn, box, http, NOW, TODAY, sleep=no_sleep)
    assert status == "failed" and "не приняты" in line and len(http.calls) == 2
    assert "pw-SECRET-1" not in line and "api-bot" not in line
    assert webmim_match.run(conn, None, http, NOW, TODAY)[0] == "failed" and len(http.calls) == 2        # no key: nothing is asked


def test_not_set_up_is_off_and_a_saved_apikey_mode_reads_as_login(tmp_path):
    conn = db(tmp_path)
    box = SecretBox(Fernet.generate_key())
    http = ScriptedHttp()
    assert webmim_match.run(conn, box, http, NOW, TODAY)[0] == "off"
    connections.set_config(conn, "webmim_orders", 0, {"server": "test", "auth": "apikey"}, NOW, "owner")
    connections.set_secret(conn, box, "webmim", 0, "api_key", "KEY0SECRET", NOW, "owner")
    conn.commit()
    assert webmim_match.run(conn, box, http, NOW, TODAY)[0] == "off" and http.calls == []        # no login, no password: the unload's key is not used


def test_the_command_and_the_tick_print_no_secret(tmp_path, monkeypatch, capsys):
    path = tmp_path / "t.db"
    conn = store.connect(path)
    key = tmp_path / "secret.key"
    key.write_bytes(Fernet.generate_key())
    from tires_sync.secretbox import load_box
    box = load_box(str(key))
    connections.set_config(conn, "webmim_orders", 0, {"server": "test", "login": "api-bot", "auth": "login"}, NOW, "owner")
    connections.set_secret(conn, box, "webmim_orders", 0, "password", "pw-SECRET-1", NOW, "owner")
    conn.commit()
    conn.close()
    http = ScriptedHttp(TOKEN, page([wm(7, 107, "18.10.26 10:00:00")], 1), items(("A", 1, 100)))
    monkeypatch.setattr(cli, "_http", lambda: http)
    monkeypatch.setattr(webmim_match.time, "sleep", no_sleep)
    monkeypatch.setattr(cli, "moscow_now", lambda: __import__("datetime").datetime(2026, 10, 20, 12, 0, 0))
    assert cli.main(["webmim-orders-sync", "--db", str(path), "--key-file", str(key)]) == 0
    out = capsys.readouterr()
    assert "заказов 1" in out.out and "pw-SECRET-1" not in out.out + out.err and "TOKEN-123" not in out.out + out.err
    http.replies.extend([(401, {}), (200, {"result": False, "message": "Неверный логин или пароль"})])
    assert cli.main(["orders-tick", "--db", str(path), "--key-file", str(key)]) == 0          # the tick goes on; the failure is a line
    out = capsys.readouterr()
    assert "снимок WebMIM" in out.err and "pw-SECRET-1" not in out.out + out.err


# ---- the pairs ------------------------------------------------------------------------------------------------------------------------

def snap(conn, *rows):
    """The snapshot as the sync leaves it: (order, positions)."""
    replies = [page([r for r, _ in rows], 1)] + [items(*its) for _, its in sorted(rows, key=lambda x: (webmim_match.parse_date(x[0]["createDate"]), str(x[0]["id"])), reverse=True)]
    client, _ = api(*replies)
    webmim_match.sync(conn, client, webmim_match.current_server(conn), NOW, TODAY, sleep=no_sleep)


def test_the_number_or_the_id_of_the_card_pairs_exactly(tmp_path):
    conn = db(tmp_path)
    snap(conn, (wm(11, 511, "18.10.26 10:00:00"), [("A", 4, 4000)]), (wm(12, 512, "17.10.26 10:00:00"), [("B", 4, 4000)]))
    by_number = card(conn, goods=[("A", 2)], webmim_number="№ 511")
    by_id = card(conn, goods=[("B", 4)], webmim_id="12")
    block = webmim_match.card_block(conn, orders.get_card(conn, by_number))
    assert block["order"]["id"] == "11" and block["differences"] == [{"goods_id": "A", "name": "Шина A", "panel_qty": 2, "webmim_qty": 4}]
    block = webmim_match.card_block(conn, orders.get_card(conn, by_id))
    assert block["order"]["id"] == "12" and block["differences"] == [] and block["hints"] == []
    wrong = card(conn, webmim_number="999")
    assert webmim_match.card_block(conn, orders.get_card(conn, wrong))["not_found"]


def test_a_suggestion_needs_a_near_date_and_the_phone_or_the_positions(tmp_path):
    conn = db(tmp_path)
    snap(conn, (wm(21, 621, "19.10.26 10:00:00", phone="89111112233"), [("A", 4, 4000)]),
         (wm(22, 622, "18.10.26 10:00:00", phone=""), [("A", 4, 4000), ("C", 1, 1)]),
         (wm(25, 625, "19.10.26 11:00:00", phone="89990000000"), [("A", 4, 4000)]),
         (wm(23, 623, "01.10.26 10:00:00", phone="89111112233"), [("A", 4, 4000)]),
         (wm(24, 624, "19.10.26 10:00:00", phone="89990000000"), [("Z", 4, 4000)]))
    mine = card(conn, at="2026-10-19T09:00:00", goods=[("A", 4)])
    found = webmim_match.card_block(conn, orders.get_card(conn, mine))["hints"]
    assert [h["order"]["id"] for h in found] == ["21", "22", "25"]            # 23: 18 days apart; 24: another phone and nothing in common
    assert found[0]["score"] > found[1]["score"] > found[2]["score"] and "тот же телефон" in found[0]["why"]
    assert "телефон другой" in found[2]["why"]                               # the same positions, another phone: shown lower, said why
    stranger = card(conn, at="2026-10-19T09:00:00", phone="+7 900 000-00-01", goods=[("Q", 1)])
    assert webmim_match.card_block(conn, orders.get_card(conn, stranger))["hints"] == []
    stock = card(conn, at="2026-10-19T09:00:00", goods=[("A", 4)], deal_kind="stock")
    assert webmim_match.card_block(conn, orders.get_card(conn, stock))["hints"] == []


def test_the_suggestion_of_an_order_paired_elsewhere_is_not_shown(tmp_path):
    conn = db(tmp_path)
    snap(conn, (wm(31, 731, "19.10.26 10:00:00", phone="89111112233"), [("A", 4, 4000)]))
    card(conn, webmim_number="731")
    other = card(conn, at="2026-10-19T09:00:00", goods=[("A", 4)])
    assert webmim_match.card_block(conn, orders.get_card(conn, other))["hints"] == []


def test_nothing_is_written_to_the_panel_without_the_button(tmp_path):
    conn = db(tmp_path)
    mine = card(conn, at="2026-10-19T09:00:00", goods=[("A", 4)])
    before = conn.execute("SELECT * FROM order_card").fetchall(), conn.execute("SELECT COUNT(*) FROM order_event").fetchone()
    snap(conn, (wm(41, 841, "19.10.26 10:00:00", phone="89111112233", external=str(mine)), [("A", 4, 4000)]))
    assert webmim_match.card_block(conn, orders.get_card(conn, mine))["hints"][0]["score"] == 99
    webmim_match.reconcile(conn, TODAY)
    assert (conn.execute("SELECT * FROM order_card").fetchall(), conn.execute("SELECT COUNT(*) FROM order_event").fetchone()) == before


def test_this_is_it_writes_the_number_the_id_and_the_journal_once(tmp_path):
    conn = db(tmp_path)
    snap(conn, (wm(51, 951, "19.10.26 10:00:00", phone="89111112233"), [("A", 4, 4000)]))
    mine = card(conn, at="2026-10-19T09:00:00", goods=[("A", 4)])
    purchase = orders.list_items(conn, mine)[0]["supplier_price"]
    webmim_match.link(conn, mine, "51", "2026-10-20T15:00:00", "manager1")
    row = orders._row(conn, mine)
    assert (row["webmim_number"], row["webmim_id"]) == ("951", "51")
    texts = [e["text"] for e in orders.list_events(conn, mine) if e["author"] == "manager1"]
    assert any("Сопоставлен с заказом WebMIM №951" in t for t in texts) and any(t.startswith("Номер заказа WebMIM: 951") for t in texts)
    assert orders.list_items(conn, mine)[0]["supplier_price"] == purchase              # the purchase is not taken again
    with pytest.raises(orders.OrderError):
        webmim_match.link(conn, mine, "51", NOW, "manager1")                              # it has a number now
    snap(conn, (wm(52, 952, "19.10.26 11:00:00"), []), (wm(51, 951, "19.10.26 10:00:00", phone="89111112233"), [("A", 4, 4000)]))
    numbered = card(conn, at="2026-10-19T09:00:00", webmim_number="777")                 # a number typed by hand, not in the snapshot
    with pytest.raises(orders.OrderError, match="уже есть номер"):
        webmim_match.link(conn, numbered, "52", NOW, "manager1")
    assert orders._row(conn, numbered)["webmim_id"] == ""
    other = card(conn, at="2026-10-19T09:00:00")
    with pytest.raises(orders.OrderError, match="уже сопоставлен"):
        webmim_match.link(conn, other, "51", NOW, "manager1")
    with pytest.raises(orders.OrderError, match="нет в снимке"):
        webmim_match.link(conn, other, "999", NOW, "manager1")


def test_the_snapshot_of_another_server_is_not_used(tmp_path):
    conn = db(tmp_path)
    snap(conn, (wm(61, 161, "19.10.26 10:00:00"), []))
    mine = card(conn, webmim_number="161")
    assert webmim_match.card_block(conn, orders.get_card(conn, mine))["order"] is not None
    connections.set_config(conn, "webmim_orders", 0, {"server": "test"}, NOW, "owner")
    assert webmim_match.card_block(conn, orders.get_card(conn, mine))["order"] is None


# ---- the pages and the rights --------------------------------------------------------------------------------------------------------

@pytest.fixture
def panel(tmp_path, monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")
    app = make_app(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, NOW)
    connections.set_config(conn, "webmim_orders", 0, {"server": "prod"}, NOW, "owner")
    conn.commit()
    return app, conn


def person(app, conn, role, point_id=None):
    users.add_user(conn, role + "1", PASSWORD, NOW, role=role, point_id=point_id)
    client = app.test_client()
    with client.session_transaction() as session:
        session["user"], session["epoch"], session["csrf"] = role + "1", store.get_session_epoch(conn, role + "1"), "tok"
    return client


def test_the_card_shows_the_block_and_the_button_writes(panel):
    app, conn = panel
    snap(conn, (wm(71, 371, "07.10.26 10:00:00", phone="89111112233"), [("A", 4, 4000)]))
    mine = card(conn, at="2026-10-07T09:00:00", goods=[("A", 4)])
    client = app.test_client()
    tok = login(client)
    html = client.get("/orders/%d" % mine).get_data(as_text=True)
    assert 'data-webmim-hint="71"' in html and "Это он" in html
    assert post(client, "/orders/%d/webmim/link" % mine, tok, order="71").status_code == 302
    html = client.get("/orders/%d" % mine).get_data(as_text=True)
    assert "Заказ WebMIM №371" in html and "data-webmim-sum" in html and "Позиции и количество совпадают" in html
    assert "data-webmim-price-differs" not in html                                       # the same purchase as in WebMIM
    assert 'href="/orders/webmim"' in client.get("/orders").get_data(as_text=True)
    page_html = client.get("/orders/webmim").get_data(as_text=True)
    assert "Сверка с WebMIM" in page_html and "сопоставлено 1" in page_html


def test_without_the_money_right_there_is_no_webmim_sum_and_a_viewer_has_no_button(panel):
    app, conn = panel
    snap(conn, (wm(81, 481, "07.10.26 10:00:00", phone="89111112233"), [("A", 4, 4321)]),
         (wm(82, 482, "07.10.26 11:00:00", phone="89111112233"), [("A", 4, 4321)]))
    paired_card = card(conn, at="2026-10-07T09:00:00", goods=[("A", 4)], webmim_number="481")
    open_card = card(conn, at="2026-10-07T09:00:00", goods=[("A", 4)])
    owner = app.test_client()
    login(owner)
    html = owner.get("/orders/%d" % paired_card).get_data(as_text=True)
    assert "4\u00a0321" in html and "data-webmim-price-differs" in html                 # the control: the owner sees WebMIM's price ≠ ours
    assert "17\u00a0284" in owner.get("/orders/webmim").get_data(as_text=True)
    chats = person(app, conn, "chats")
    html = chats.get("/orders/%d" % paired_card).get_data(as_text=True)
    assert "Заказ WebMIM №481" in html and "data-webmim-sum" not in html and "data-webmim-price-differs" not in html and "17\u00a0284" not in html and "4\u00a0321" not in html
    assert "17\u00a0284" not in chats.get("/orders/webmim").get_data(as_text=True)
    viewer = person(app, conn, "viewer")
    html = viewer.get("/orders/%d" % open_card).get_data(as_text=True)
    assert 'data-webmim-hint="82"' in html and "Это он" not in html
    assert viewer.post("/orders/%d/webmim/link" % open_card, data={"csrf": "tok", "order": "82"}).status_code == 403
    assert orders._row(conn, open_card)["webmim_number"] == ""


def test_the_manager_of_a_point_sees_only_the_paired_order_of_the_own_point(panel):
    app, conn = panel
    mine_point = points.save_point(conn, {"name": "Гамма", "manager_name": "А", "channel": "vk"})
    snap(conn, (wm(91, 591, "07.10.26 10:00:00", phone="89111112233"), [("A", 4, 4000)]),
         (wm(92, 592, "07.10.26 10:00:00", phone="89111112233"), [("A", 4, 4000)]))
    paired_card = card(conn, at="2026-10-07T09:00:00", goods=[("A", 4)], webmim_number="591", point_id=str(mine_point))
    open_card = card(conn, at="2026-10-07T09:00:00", goods=[("A", 4)], point_id=str(mine_point))
    point = person(app, conn, "point", mine_point)
    assert "Заказ WebMIM №591" in point.get("/orders/%d" % paired_card).get_data(as_text=True)
    html = point.get("/orders/%d" % open_card).get_data(as_text=True)
    assert "data-webmim" not in html and "592" not in html
    assert point.get("/orders/webmim").status_code == 403
    assert 'href="/orders/webmim"' not in point.get("/orders").get_data(as_text=True)
    assert point.post("/orders/%d/webmim/link" % open_card, data={"csrf": "tok", "order": "92"}).status_code == 403
    assert orders._row(conn, open_card)["webmim_number"] == ""


# ---- after the review -----------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("code", [429, 500, 503])
def test_a_passing_failure_of_the_positions_ends_the_run_and_the_order_is_asked_again(tmp_path, code):
    conn = db(tmp_path)
    tries = 2                                                      # a read is repeated once after 429 or a fast 5xx (reliability.WEBMIM)
    client, http = api(page([wm(7, 107, "18.10.26 10:00:00")], 1), *[(code, {"x": 1})] * tries)
    with pytest.raises(webmim_orders.OrdersError):
        webmim_match.sync(conn, client, "prod", NOW, TODAY, sleep=no_sleep)
    assert len(http.calls) == 1 + 1 + tries                        # the token, the page, the positions' attempts
    client, http = api(page([wm(7, 107, "18.10.26 10:00:00")], 1), items(("A", 1, 10)))
    assert webmim_match.sync(conn, client, "prod", NOW, TODAY, sleep=no_sleep)["items"] == 1 and order_row(conn, "7")[3] == 1


def test_on_the_test_server_there_are_no_suggestions_and_no_pairing(tmp_path):
    conn = db(tmp_path)
    connections.set_config(conn, "webmim_orders", 0, {"server": "test"}, NOW, "owner")
    snap(conn, (wm(21, 621, "19.10.26 10:00:00", phone="89111112233"), [("A", 4, 4000)]))
    mine = card(conn, at="2026-10-19T09:00:00", goods=[("A", 4)])
    block = webmim_match.card_block(conn, orders.get_card(conn, mine))
    assert block["hints"] == [] and block["test"]
    assert webmim_match.reconcile(conn, TODAY)["unpaired"][0]["hints"] == []
    with pytest.raises(orders.OrderError, match="тестового сервера"):
        webmim_match.link(conn, mine, "21", NOW, "owner")
    assert orders._row(conn, mine)["webmim_id"] == "" and not conn.in_transaction


def test_a_wrong_password_is_not_tried_again_every_hour_until_a_new_one(tmp_path):
    conn = db(tmp_path)
    box = SecretBox(Fernet.generate_key())
    connections.set_config(conn, "webmim_orders", 0, {"server": "prod", "login": "api-bot", "auth": "login"}, NOW, "owner")
    connections.set_secret(conn, box, "webmim_orders", 0, "password", "pw-SECRET-1", "2026-10-20T10:00:00", "owner")
    conn.commit()
    http = ScriptedHttp((401, {"name": "Unauthorized"}), (200, {"result": False, "message": "Неверный логин или пароль"}))
    assert webmim_match.run(conn, box, http, NOW, TODAY)[0] == "failed" and len(http.calls) == 2
    assert "не приняты" in connections.get_check(conn, "webmim_orders", 0)["status"]          # «Подключения» shows it
    status, line = webmim_match.run(conn, box, http, "2026-10-20T13:00:00", TODAY)
    assert status == "failed" and "не повторяется" in line and len(http.calls) == 2           # the next hour: no knock
    connections.set_secret(conn, box, "webmim_orders", 0, "password", "pw-NEW-2", "2026-10-20T14:00:00", "owner")
    conn.commit()
    http.replies.extend([TOKEN, page([], 1)])
    assert webmim_match.run(conn, box, http, "2026-10-20T15:00:00", TODAY, sleep=no_sleep)[0] == "read" and len(http.calls) == 4


def test_orders_without_a_readable_date_make_the_reading_a_failure(tmp_path):
    conn = db(tmp_path)
    box = SecretBox(Fernet.generate_key())
    connections.set_config(conn, "webmim_orders", 0, {"server": "prod", "login": "api-bot", "auth": "login"}, NOW, "owner")
    connections.set_secret(conn, box, "webmim_orders", 0, "password", "pw-SECRET-1", NOW, "owner")
    conn.commit()
    http = ScriptedHttp(TOKEN, page([wm(7, 107, "2026-10-18T10:00:00Z")], 1), items())
    status, line = webmim_match.run(conn, box, http, NOW, TODAY, sleep=no_sleep)
    assert status == "failed" and "дата" in line


def test_the_page_lists_a_webmim_order_paired_twice_and_leaves_out_cancelled_and_old_ones(tmp_path):
    conn = db(tmp_path)
    snap(conn, (wm(61, 161, "19.10.26 10:00:00"), []), (wm(62, 162, "19.10.26 11:00:00"), []))
    conn.execute("INSERT INTO webmim_order (server, id, number, create_date, seen_at, items_at) VALUES ('prod', '60', '160', '2026-06-01T10:00:00', ?, ?)",
                 (NOW, NOW))                                                              # left from an older reading, out of the window
    first, second = card(conn, webmim_number="161"), card(conn, webmim_id="61")
    cancel = next(s["id"] for s in orders.list_stages(conn) if s["code"] == "cancel")
    cancelled = card(conn)
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (cancel, cancelled))       # the test's own base
    page_data = webmim_match.reconcile(conn, TODAY)
    assert page_data["shared"] == [{"order": page_data["shared"][0]["order"], "cards": [second, first]}]
    assert [o["id"] for o in page_data["lonely"]] == ["62"] and cancelled not in [c["id"] for c in page_data["unpaired"]]


def test_review_2_leftovers_no_purchase_a_lost_stage_a_long_id_a_busy_base(tmp_path, monkeypatch):
    conn = db(tmp_path)
    snap(conn, (wm(71, 171, "19.10.26 10:00:00"), [("A", 4, 4000)]), (wm("1" * 21, 172, "18.10.26 11:00:00"), []))
    assert order_row(conn, "1" * 21) is None                                              # longer than an id the positions can be asked for
    mine = card(conn, goods=[("A", 4)], webmim_number="171")
    conn.execute("UPDATE order_item SET supplier_price = 0 WHERE order_id = ?", (mine,))
    block = webmim_match.card_block(conn, orders.get_card(conn, mine))
    assert block["order"]["items"] and block["purchase"] == {}                             # no purchase yet: nothing to call different
    lost = orders.add_stage(conn, "Отказался", "#F04438", "lost", "", 0)
    gone = card(conn)
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (lost, gone))
    assert gone not in [c["id"] for c in webmim_match.reconcile(conn, TODAY)["unpaired"]]
    conn.commit()
    (tmp_path / "app").mkdir()
    app = make_app(tmp_path / "app")
    client = app.test_client()
    tok = login(client)
    import sqlite3
    monkeypatch.setattr(webmim_match, "link", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")))
    other = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(other, NOW)
    target = card(other)
    assert post(client, "/orders/%d/webmim/link" % target, tok, order="71").status_code == 302
