"""«Звонки» in the panel: the list with its filters, who may listen (the operator, the admins, the owners; the point only its account; the
observer never), «Создать заказ» from a call, the calls in the client's card."""
from __future__ import annotations

from pathlib import Path

import pytest

from panel_helpers import login, make_app, post
from test_calls import FakeClient, call
from order_fixtures import card_of_phone
from tires_sync import calls, clients, connections, order_points as points, orders, store, users

NOW = "2026-10-07T12:00:00"                     # the panel's clock in the tests (panel_helpers)
PASSWORD = "длинный-пароль-1"


@pytest.fixture(autouse=True)
def cheap_hashes(monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")


@pytest.fixture
def setup(tmp_path):
    app = make_app(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    main = connections.create_account(conn, "Основной", NOW, "o")
    gamma = connections.create_account(conn, "Гамма", NOW, "o")
    pid = points.save_point(conn, {"name": "Гамма", "manager_name": "А", "channel": "vk", "avito_account_id": gamma})
    calls.sync_account(conn, FakeClient([[call(1, at="2026-10-07T10:00:00+03:00"), call(2, at="2026-10-07T09:00:00+03:00", talk=0, buyer="+79117654321")]]),
                       main, NOW, lambda s: None)
    calls.sync_account(conn, FakeClient([[call(3, at="2026-10-07T11:00:00+03:00", buyer="+79113330000")]]), gamma, NOW, lambda s: None)
    folder = Path(app.config["DB_PATH"]).resolve().parent / "calls"
    calls.fetch_records(conn, FakeClient(records={1: b"ID3-main"}), main, folder, NOW, lambda s: None)
    calls.fetch_records(conn, FakeClient(records={3: b"ID3-gamma"}), gamma, folder, NOW, lambda s: None)
    rows = {r["call_id"]: r["id"] for r in calls.list_calls(conn)}
    return app, conn, {"main": main, "gamma": gamma, "point": pid, "rows": rows}


def person(app, conn, role, point_id=None):
    login_ = role + "1"
    users.add_user(conn, login_, PASSWORD, NOW, role=role, point_id=point_id)
    client = app.test_client()
    with client.session_transaction() as session:
        session["user"] = login_
        session["epoch"] = store.get_session_epoch(conn, login_)
        session["csrf"] = "tok"
    return client


def test_the_owner_sees_every_call_and_filters_them(setup):
    app, conn, ids = setup
    owner = app.test_client()
    login(owner)
    html = owner.get("/calls").get_data(as_text=True)
    assert html.count('class="call-actions"') == 3 and "Пропущен" in html and 'href="/calls"' in html
    assert '/calls/%d/record' % ids["rows"][1] in html and '/calls/%d/record' % ids["rows"][2] not in html      # a missed call has no recording
    missed = owner.get("/calls?missed=1").get_data(as_text=True)
    assert missed.count('class="call-actions"') == 1 and "765-43-21" in missed
    only = owner.get("/calls?account=%d" % ids["gamma"]).get_data(as_text=True)
    assert only.count('class="call-actions"') == 1 and "333-00-00" in only
    assert owner.get("/calls?q=7654321").get_data(as_text=True).count('class="call-actions"') == 1
    assert owner.get("/calls/%d/record" % ids["rows"][1]).get_data() == b"ID3-main"


def test_the_record_is_audio_not_kept_by_the_browser(setup):
    app, conn, ids = setup
    owner = app.test_client()
    login(owner)
    answer = owner.get("/calls/%d/record" % ids["rows"][1])
    assert answer.status_code == 200 and answer.mimetype == "audio/mpeg" and "no-store" in answer.headers["Cache-Control"]
    assert owner.get("/calls/%d/record" % ids["rows"][2]).status_code == 404                  # no recording
    assert owner.get("/calls/999/record").status_code == 404


@pytest.mark.parametrize("role", ["partner", "chats"])
def test_the_operator_and_the_partner_listen(setup, role):
    app, conn, ids = setup
    client = person(app, conn, role)
    assert client.get("/calls").status_code == 200
    assert client.get("/calls/%d/record" % ids["rows"][3]).get_data() == b"ID3-gamma"


def test_the_observer_sees_the_list_but_never_the_recordings_nor_the_order_button(setup):
    app, conn, ids = setup
    viewer = person(app, conn, "viewer")
    html = viewer.get("/calls").get_data(as_text=True)
    assert html.count('class="call-actions"') == 3 and "<audio" not in html and "Создать заказ" not in html
    assert viewer.get("/calls/%d/record" % ids["rows"][1]).status_code == 403
    assert post(viewer, "/calls/%d/order" % ids["rows"][1], "tok").status_code == 403


def test_the_manager_of_a_point_hears_only_the_calls_of_the_points_account(setup):
    app, conn, ids = setup
    manager = person(app, conn, "point", ids["point"])
    html = manager.get("/calls?account=%d" % ids["main"]).get_data(as_text=True)            # asking for another account changes nothing
    assert html.count('class="call-actions"') == 1 and "333-00-00" in html and "123-45-67" not in html
    assert manager.get("/calls/%d/record" % ids["rows"][3]).get_data() == b"ID3-gamma"
    assert manager.get("/calls/%d/record" % ids["rows"][1]).status_code == 403
    assert post(manager, "/calls/%d/order" % ids["rows"][1], "tok").status_code == 403
    assert 'href="/calls"' in html


def test_a_manager_of_a_point_without_an_account_sees_no_calls(setup):
    app, conn, ids = setup
    other = points.save_point(conn, {"name": "Альфа", "manager_name": "Б", "channel": "vk"})
    manager = person(app, conn, "point", other)
    html = manager.get("/calls").get_data(as_text=True)
    assert 'class="call-actions"' not in html and "не привязан аккаунт Авито" in html


def test_create_order_from_a_call_opens_the_card_with_the_number(setup):
    app, conn, ids = setup
    manager = person(app, conn, "point", ids["point"])
    answer = post(manager, "/calls/%d/order" % ids["rows"][3], "tok")
    assert answer.status_code == 302 and answer.headers["Location"].startswith("/orders/")
    card_id = int(answer.headers["Location"].rsplit("/", 1)[1])
    card = orders.get_card(conn, card_id)
    assert card["client_phone"] == "+79113330000" and card["point_id"] == ids["point"]
    assert manager.get("/orders/%d" % card_id).status_code == 200
    html = manager.get("/calls").get_data(as_text=True)
    assert "Заказ №%d" % card_id in html and "Создать заказ" not in html


def test_the_clients_card_shows_the_calls_with_the_player(setup):
    app, conn, ids = setup
    client_id = calls.get_call(conn, ids["rows"][1])["client_id"]
    owner = app.test_client()
    login(owner)
    html = owner.get("/clients/%d" % client_id).get_data(as_text=True)
    assert "Звонки" in html and "Принят, 0:57" in html and '/calls/%d/record' % ids["rows"][1] in html and 'class="call-actions"' in html
    viewer = person(app, conn, "viewer")
    seen = viewer.get("/clients/%d" % client_id).get_data(as_text=True)
    assert "Принят, 0:57" in seen and "<audio" not in seen
    calls.clean_records(conn, Path(app.config["DB_PATH"]).resolve().parent / "calls", "2026-10-15T00:00:00")
    html = owner.get("/clients/%d" % client_id).get_data(as_text=True)
    assert "удалена" in html and "<audio" not in html


def test_every_listening_is_in_the_journal_who_when_which_call(setup):
    app, conn, ids = setup
    operator = person(app, conn, "chats")
    operator.get("/calls/%d/record" % ids["rows"][3])
    operator.get("/calls/%d/record" % ids["rows"][3], headers={"Range": "bytes=100-"})          # the player asking the rest: not a new listening
    operator.get("/calls/%d/record" % ids["rows"][3], headers={"Range": "bytes=1-"})            # nor a request that skips the first byte
    entries = [e for e in store.list_audit(conn) if e["action"] == "call_listened"]
    assert len(entries) == 1 and entries[0]["author"] == "chats1"
    assert entries[0]["detail"]["call"] == ids["rows"][3] and entries[0]["detail"]["account"] == ids["gamma"]
    assert "333" not in str(entries[0]["detail"])                                                  # no number in the journal
    viewer = person(app, conn, "viewer")
    viewer.get("/calls/%d/record" % ids["rows"][3])
    assert len([e for e in store.list_audit(conn) if e["action"] == "call_listened"]) == 1        # a refused one is no listening
    owner = app.test_client()
    login(owner)
    owner.get("/calls/%d/record" % ids["rows"][3], headers={"Range": "bytes=5-"})
    assert len([e for e in store.list_audit(conn) if e["action"] == "call_listened"]) == 2        # another person: another listening


def _row_of(html: str, call_row: int) -> str:
    start = html.index('<tr id="call-%d"' % call_row)
    return html[start:html.index("</tr>", start)]


def _chat(conn, account_id, chat_id, client_id, last_at):
    row = conn.execute("INSERT INTO avito_chat (account_id, chat_id, peer_name, last_at) VALUES (?, ?, 'Клиент', ?)",
                       (account_id, chat_id, last_at)).lastrowid
    conn.execute("INSERT INTO client_chat (chat_row, client_id, how, at) VALUES (?, ?, 'phone', ?)", (row, client_id, NOW))
    conn.commit()
    return row


def _order(conn, phone, point_id=None, closed=False):
    orders.ensure_pipeline(conn, NOW)
    card_id = card_of_phone(conn, {"client_name": "Клиент", "client_phone": phone, "point_id": point_id}, NOW, "o")
    if closed:                                                    # a finished order: the test base, straight to a closed stage
        won = next(s for s in orders.list_stages(conn) if s["kind"] == "won")
        conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (won["id"], card_id))
        conn.commit()
    return card_id


def test_the_client_of_a_call_shows_the_name_the_chat_and_the_open_order(setup):
    """The owner 2026-10-23: in «Звонки» the client is not a bare number: the name from the base and the buttons to the chat and the order."""
    app, conn, ids = setup
    client_id = calls.get_call(conn, ids["rows"][1])["client_id"]
    clients.save(conn, client_id, "Иван Петров", "", NOW, "o")
    old_chat = _chat(conn, ids["gamma"], "c-old", client_id, "2026-10-01T10:00:00")
    new_chat = _chat(conn, ids["main"], "c-new", client_id, "2026-10-06T10:00:00")
    open_order = _order(conn, "+79111234567")
    done_order = _order(conn, "+79111234567", closed=True)          # newer, but finished: the open one is the one to go to
    owner = app.test_client()
    login(owner)
    row = _row_of(owner.get("/calls").get_data(as_text=True), ids["rows"][1])
    assert "Иван Петров" in row and 'href="/clients/%d"' % client_id in row
    assert 'href="/chats/%d"' % new_chat in row and 'href="/chats/%d"' % old_chat not in row
    assert 'href="/orders/%d"' % open_order in row and 'href="/orders/%d"' % done_order not in row
    bare = _row_of(owner.get("/calls").get_data(as_text=True), ids["rows"][2])       # a client with no chat and no order: the number only
    assert "/chats/" not in bare and "/orders/" not in bare


def test_the_client_of_a_call_without_an_open_order_goes_to_the_last_one(setup):
    app, conn, ids = setup
    first, last = _order(conn, "+79111234567", closed=True), _order(conn, "+79111234567", closed=True)
    owner = app.test_client()
    login(owner)
    row = _row_of(owner.get("/calls").get_data(as_text=True), ids["rows"][1])
    assert 'href="/orders/%d"' % last in row and 'href="/orders/%d"' % first not in row


def test_the_point_sees_the_name_and_only_its_own_chat_and_order(setup):
    app, conn, ids = setup
    client_id = calls.get_call(conn, ids["rows"][3])["client_id"]
    clients.save(conn, client_id, "Ольга", "", NOW, "o")
    own_chat = _chat(conn, ids["gamma"], "c-own", client_id, "2026-10-01T10:00:00")
    other_chat = _chat(conn, ids["main"], "c-other", client_id, "2026-10-06T10:00:00")          # newer, of another account
    other_point = points.save_point(conn, {"name": "Альфа", "manager_name": "Б", "channel": "vk"})
    own_order = _order(conn, "+79113330000", point_id=ids["point"])
    other_order = _order(conn, "+79113330000", point_id=other_point)                            # newer, of another point
    manager = person(app, conn, "point", ids["point"])
    row = _row_of(manager.get("/calls").get_data(as_text=True), ids["rows"][3])
    assert "Ольга" in row and 'href="/clients/%d"' % client_id in row                         # «CRM»: the point's own part (2026-10-26)
    card = manager.get("/clients/%d" % client_id).get_data(as_text=True)
    assert "/orders/%d\"" % own_order in card and "/orders/%d\"" % other_order not in card and "Ч-%d" % other_chat not in card
    assert 'href="/chats/%d"' % own_chat in row and 'href="/chats/%d"' % other_chat not in row
    assert 'href="/orders/%d"' % own_order in row and 'href="/orders/%d"' % other_order not in row
    assert manager.get("/chats/%d" % own_chat).status_code == 200 and manager.get("/orders/%d" % own_order).status_code == 200
    owner = app.test_client()
    login(owner)
    seen = _row_of(owner.get("/calls").get_data(as_text=True), ids["rows"][3])                  # the owner: the newest of all
    assert 'href="/chats/%d"' % other_chat in seen and 'href="/orders/%d"' % other_order in seen


def test_the_order_of_the_call_itself_is_not_repeated_in_the_client(setup):
    app, conn, ids = setup
    owner = app.test_client()
    login(owner)
    card_id = int(post(owner, "/calls/%d/order" % ids["rows"][1], "tok").headers["Location"].rsplit("/", 1)[1])
    row = _row_of(owner.get("/calls").get_data(as_text=True), ids["rows"][1])
    assert row.count('href="/orders/%d"' % card_id) == 1                                       # «Заказ №…» of the call, once
