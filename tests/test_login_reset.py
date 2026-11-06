"""«Сбросить вход» (the owner 2026-11-03): a one-time link of the owner for one user to set a new password and, if wanted, a new login;
the rename moves every place the login is a key, the journal keeps the old login as written; the old sessions end on use only."""
from __future__ import annotations

import json
import re
from datetime import datetime

import pytest

from panel_helpers import login, make_app, post
from tires_sync import connections, store, users
from tires_sync.panel import common

NOW = "2026-10-07T12:00:00"
PASSWORD = "длинный-пароль-1"
NEW_PASSWORD = "новый-пароль-22"


@pytest.fixture(autouse=True)
def cheap_hashes(monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")


def conn_of(app):
    return store.connect(app.config["DB_PATH"])


def sign_in(client, conn, login_) -> str:
    with client.session_transaction() as session:
        session["user"] = login_
        session["epoch"] = store.get_session_epoch(conn, login_)
        session["csrf"] = "tok"
    return "tok"


def _csrf(client) -> str:
    with client.session_transaction() as session:
        session["csrf"] = "tok"
    return "tok"


def _issue(app, owner, tok, login_) -> str:
    """The owner presses «Сбросить вход»: the path of the link, as the page shows it once."""
    assert post(owner, "/users/%s/reset" % login_, tok).status_code == 302
    page = owner.get("/users").get_data(as_text=True)
    found = re.search(r'value="http://localhost(/reset/[^"]+)"', page)
    assert found, "the link is not on the page"
    assert found.group(1) not in owner.get("/users").get_data(as_text=True)                  # shown once
    return found.group(1)


# --- the link -------------------------------------------------------------------------------------------------------------------------

def test_the_link_is_hashed_one_time_24_hours_and_revocable():
    conn = store.connect(":memory:")
    users.add_user(conn, "zaman", PASSWORD, NOW, role="owner")
    token = users.create_login_reset(conn, "zaman", NOW, "owner", "owner")
    assert len(token) >= 43                                                                  # 32 random bytes, ≥128 bits
    dump = "\n".join(conn.iterdump())
    assert token not in dump and users._token_hash(token) in dump
    assert users.find_login_reset(conn, token, "2026-10-08T11:59:59") == {"login": "zaman", "expires": "2026-10-08T12:00:00"}
    assert users.find_login_reset(conn, token, "2026-10-08T12:00:00") is None                # 24 hours later
    with pytest.raises(users.UserError):
        users.use_login_reset(conn, token, "", NEW_PASSWORD, "2026-10-08T12:00:01", "owner")
    assert users.verify(conn, "zaman", PASSWORD) == "zaman"
    assert users.use_login_reset(conn, token, "", NEW_PASSWORD, NOW, "owner") == "zaman"      # the login stays
    assert users.verify(conn, "zaman", NEW_PASSWORD) == "zaman" and users.verify(conn, "zaman", PASSWORD) is None
    with pytest.raises(users.UserError):                                                      # once
        users.use_login_reset(conn, token, "", "ещё-один-пароль", NOW, "owner")
    assert users.verify(conn, "zaman", NEW_PASSWORD) == "zaman"
    other = users.create_login_reset(conn, "zaman", NOW, "owner", "owner")
    users.revoke_login_reset(conn, users.list_login_resets(conn, NOW)[0]["id"], NOW, "owner")
    assert users.find_login_reset(conn, other, NOW) is None and users.list_login_resets(conn, NOW) == []
    with pytest.raises(users.UserError):
        users.use_login_reset(conn, other, "", "ещё-один-пароль", NOW, "owner")
    actions = [(a["action"], a["author"], a["detail"]) for a in store.list_audit(conn)]
    assert ("login_reset_created", "owner", {"login": "zaman", "expires": "2026-10-08T12:00:00"}) in actions
    assert ("login_reset_used", "zaman", {"from": "zaman", "to": "zaman"}) in actions
    assert any(a[0] == "login_reset_revoked" for a in actions)
    assert NEW_PASSWORD not in str(actions) and token not in str(actions)


def test_a_new_link_ends_the_earlier_one_of_the_user():
    conn = store.connect(":memory:")
    users.add_user(conn, "zaman", PASSWORD, NOW)
    first = users.create_login_reset(conn, "zaman", NOW, "owner", "owner")
    second = users.create_login_reset(conn, "zaman", NOW, "owner", "owner")
    assert users.find_login_reset(conn, first, NOW) is None and users.find_login_reset(conn, second, NOW) is not None


def test_never_for_the_shared_owner_login_nor_an_unknown_user():
    conn = store.connect(":memory:")
    for target in ("owner", "OWNER ", "nobody", "server"):
        with pytest.raises(users.UserError):
            users.create_login_reset(conn, target, NOW, "owner", "owner")
    assert users.list_login_resets(conn, NOW) == []


def test_the_shared_login_is_refused_whatever_its_name():
    conn = store.connect(":memory:")
    users.add_user(conn, "boss", PASSWORD, NOW)                                              # a panel user under the AUTHOR's name
    with pytest.raises(users.UserError):
        users.create_login_reset(conn, "boss", NOW, "owner", "boss")
    users.add_user(conn, "zaman", PASSWORD, NOW)
    token = users.create_login_reset(conn, "zaman", NOW, "owner", "boss")
    with pytest.raises(users.UserError):
        users.use_login_reset(conn, token, "boss2", "короткий", NOW, "boss")
    with pytest.raises(users.UserError):                                                     # the shared login is never a new login
        users.use_login_reset(conn, token, "boss", NEW_PASSWORD, NOW, "boss")


def test_the_same_login_with_a_new_password_ends_the_old_sessions_and_the_link(tmp_path):
    app = make_app(tmp_path)
    conn = conn_of(app)
    users.add_user(conn, "zaman", PASSWORD, NOW, role="owner")
    old_session = app.test_client()
    sign_in(old_session, conn, "zaman")
    owner = app.test_client()
    path = _issue(app, owner, login(owner), "zaman")
    guest = app.test_client()
    tok = _csrf(guest)
    assert guest.post(path, data={"csrf": tok, "login": "", "password": NEW_PASSWORD, "password2": NEW_PASSWORD}).status_code == 302
    assert old_session.get("/orders").status_code == 302 and users.verify(conn, "zaman", NEW_PASSWORD) == "zaman"
    assert guest.get(path).status_code == 404
    tok = _csrf(guest)                                                                       # the success signed the page out
    assert guest.post(path, data={"csrf": tok, "login": "", "password": "третий-пароль-3", "password2": "третий-пароль-3"}).status_code == 404
    assert users.verify(conn, "zaman", NEW_PASSWORD) == "zaman"


def test_the_password_and_the_login_follow_the_rules_of_the_invitation():
    conn = store.connect(":memory:")
    users.add_user(conn, "zaman", PASSWORD, NOW)
    users.add_user(conn, "olya", PASSWORD, NOW, role="viewer")
    token = users.create_login_reset(conn, "zaman", NOW, "owner", "owner")
    for new_login, password in (("", "короткий"), ("Я", NEW_PASSWORD), ("a", NEW_PASSWORD), ("olya", NEW_PASSWORD), ("owner", NEW_PASSWORD),
                                ("system", NEW_PASSWORD), ("x" * 33, NEW_PASSWORD), ("bad login", NEW_PASSWORD)):
        with pytest.raises(users.UserError):
            users.use_login_reset(conn, token, new_login, password, NOW, "owner")
    assert users.find_login_reset(conn, token, NOW) is not None                               # a refusal keeps the link
    assert users.verify(conn, "zaman", PASSWORD) == "zaman" and users.verify(conn, "olya", PASSWORD) == "olya"
    assert users.use_login_reset(conn, token, " ZamanTyres2 ", NEW_PASSWORD, NOW, "owner") == "zamantyres2"


# --- the rename -----------------------------------------------------------------------------------------------------------------------

def test_the_new_login_keeps_the_users_things_and_the_journal_keeps_the_old_login():
    conn = store.connect(":memory:")
    users.add_user(conn, "zamantyres", PASSWORD, NOW, role="owner", display_name="Заман")
    users.add_user(conn, "olya", PASSWORD, NOW, role="chats")
    store.write_audit(conn, NOW, "zamantyres", "something", {"login": "zamantyres"})
    conn.execute("INSERT INTO user_pref VALUES ('zamantyres', 'orders_columns', '[\"a\"]', ?)", (NOW,))
    conn.execute("INSERT INTO user_pref VALUES ('olya', 'orders_columns', '[\"b\"]', ?)", (NOW,))
    conn.execute("INSERT INTO connection (kind, owner_id, config) VALUES ('avito', 1, ?)",
                 (json.dumps({"problems_to": "zamantyres", "responsible": "zamantyres", "name": "x"}),))
    conn.execute("INSERT INTO connection (kind, owner_id, config) VALUES ('avito', 2, ?)", (json.dumps({"problems_to": "olya"}),))
    conn.commit()
    conn.execute("INSERT INTO callback_task (due_at, assignee, note, created_at, created_by, kind, title) "
                 "VALUES ('2026-10-08T10:00', 'zamantyres', '', ?, 'olya', 'other', 'Позвонить')", (NOW,))
    conn.commit()
    token = users.create_login_reset(conn, "zamantyres", NOW, "owner", "owner")
    assert users.use_login_reset(conn, token, "zaman", NEW_PASSWORD, NOW, "owner") == "zaman"
    assert not users.exists(conn, "zamantyres") and users.verify(conn, "zaman", NEW_PASSWORD) == "zaman"
    assert users.get_role(conn, "zaman") == {"role": "owner", "active": True, "point_id": None, "display_name": "Заман"}
    assert dict(conn.execute("SELECT login, value FROM user_pref").fetchall()) == {"zaman": '["a"]', "olya": '["b"]'}
    assert connections.get_problems_to(conn, 1) == "zaman" and connections.get_responsible(conn, 1) == "zaman"
    assert connections.get_config(conn, "avito", 1)["name"] == "x" and connections.get_problems_to(conn, 2) == "olya"
    assert conn.execute("SELECT assignee, created_by FROM callback_task").fetchone() == ("zaman", "olya")
    journal = store.list_audit(conn)
    assert any(a["author"] == "zamantyres" and a["action"] == "something" and a["detail"] == {"login": "zamantyres"} for a in journal)
    assert any(a["action"] == "login_reset_used" and a["detail"] == {"from": "zamantyres", "to": "zaman"} for a in journal)


def test_the_rename_moves_every_login_of_a_task_who_did_it_too():
    """«Выполнил» (done_by) after the rename shows the new login, like «Кому» (assignee) and «Поставил» (created_by)."""
    conn = store.connect(":memory:")
    users.add_user(conn, "zamantyres", PASSWORD, NOW, role="owner")
    users.add_user(conn, "olya", PASSWORD, NOW, role="chats")
    conn.execute("INSERT INTO callback_task (due_at, assignee, note, created_at, created_by, done_at, done_by, kind, title) "
                 "VALUES ('2026-10-08T10:00', 'olya', '', ?, 'zamantyres', ?, 'zamantyres', 'other', 'Внести трек-номер')", (NOW, NOW))
    conn.commit()
    token = users.create_login_reset(conn, "zamantyres", NOW, "owner", "owner")
    assert users.use_login_reset(conn, token, "zaman", NEW_PASSWORD, NOW, "owner") == "zaman"
    assert conn.execute("SELECT assignee, created_by, done_by FROM callback_task").fetchone() == ("olya", "zaman", "zaman")
    assert users.login_taken(conn, "zamantyres") is False                    # nothing of the old login is left behind


def test_a_new_login_with_leftovers_of_a_removed_user_is_refused():
    """A removed user's things under a login (an order answered for, a read mark) never become another person's by a rename."""
    conn = store.connect(":memory:")
    users.add_user(conn, "zaman", PASSWORD, NOW)
    users.add_user(conn, "bob", PASSWORD, NOW)
    conn.execute("INSERT INTO order_event_seen VALUES ('bob', 1, 1, ?)", (NOW,))
    conn.commit()
    users.remove_user(conn, "bob", NOW)
    token = users.create_login_reset(conn, "zaman", NOW, "owner", "owner")
    with pytest.raises(users.UserError):
        users.use_login_reset(conn, token, "bob", NEW_PASSWORD, NOW, "owner")
    assert users.exists(conn, "zaman") and users.find_login_reset(conn, token, NOW) is not None


def test_a_failure_in_the_middle_changes_nothing(monkeypatch):
    conn = store.connect(":memory:")
    users.add_user(conn, "zaman", PASSWORD, NOW)
    conn.execute("INSERT INTO user_pref VALUES ('zaman', 'k', 'v', ?)", (NOW,))
    conn.commit()
    token = users.create_login_reset(conn, "zaman", NOW, "owner", "owner")

    def broken(conn_, old, new):
        conn_.execute("UPDATE user_pref SET login = ? WHERE login = ?", (new, old))
        raise RuntimeError("boom")
    monkeypatch.setattr(users, "rename_login", broken)
    with pytest.raises(RuntimeError):
        users.use_login_reset(conn, token, "zaman2", NEW_PASSWORD, NOW, "owner")
    assert users.verify(conn, "zaman", PASSWORD) == "zaman" and not users.exists(conn, "zaman2")
    assert conn.execute("SELECT login FROM user_pref").fetchall() == [("zaman",)]
    assert users.find_login_reset(conn, token, NOW) is not None
    assert store.get_session_epoch(conn, "zaman") == 0 and store.get_session_epoch(conn, "zaman2") == 0      # the sessions stay


# --- the pages ------------------------------------------------------------------------------------------------------------------------

def test_through_the_pages_the_old_sessions_end_on_use_only(tmp_path):
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 10, 7, 12, 0, 0))
    conn = conn_of(app)
    users.add_user(conn, "zamantyres", PASSWORD, NOW, role="owner")
    old_session = app.test_client()
    sign_in(old_session, conn, "zamantyres")
    owner = app.test_client()
    tok = login(owner)
    path = _issue(app, owner, tok, "zamantyres")
    assert old_session.get("/orders").status_code == 200                                   # not ended by the link alone
    guest = app.test_client()
    page = guest.get(path)
    assert page.status_code == 200 and "zamantyres" in page.get_data(as_text=True)
    tok2 = _csrf(guest)
    assert guest.post(path, data={"csrf": tok2, "login": "zaman", "password": NEW_PASSWORD, "password2": "другой-пароль-2"}).status_code == 400
    assert users.find_login_reset(conn, path.rsplit("/", 1)[1], NOW) is not None
    got = guest.post(path, data={"csrf": tok2, "login": "zaman", "password": NEW_PASSWORD, "password2": NEW_PASSWORD})
    assert got.status_code == 302 and got.headers["Location"].endswith("/login")
    assert old_session.get("/orders").status_code == 302                                   # every old session ended
    assert guest.get(path).status_code == 404                                                # used
    fresh = app.test_client()
    assert fresh.post("/login", data={"csrf": _csrf(fresh), "login": "zamantyres", "password": PASSWORD}).status_code == 401
    assert fresh.post("/login", data={"csrf": _csrf(fresh), "login": "zaman", "password": NEW_PASSWORD}).status_code == 302
    assert fresh.get("/users").status_code == 200


def test_a_session_kept_under_the_new_login_from_a_removed_user_does_not_come_back(tmp_path):
    app = make_app(tmp_path)
    conn = conn_of(app)
    users.add_user(conn, "bob", PASSWORD, NOW, role="owner")
    users.add_user(conn, "zaman", PASSWORD, NOW, role="owner")
    stale = app.test_client()
    sign_in(stale, conn, "bob")                                                              # bob's session, its number as it was
    store.bump_session_epoch(conn, "bob")                                                    # bob changed the password: a later session
    later = app.test_client()
    sign_in(later, conn, "bob")
    users.remove_user(conn, "bob", NOW)
    token = users.create_login_reset(conn, "zaman", NOW, "owner", "owner")
    users.use_login_reset(conn, token, "bob", NEW_PASSWORD, NOW, "owner")
    assert stale.get("/orders").status_code == 302 and later.get("/orders").status_code == 302


def test_a_wrong_used_or_expired_link_is_one_answer_and_a_failed_login(tmp_path):
    app = make_app(tmp_path, MAX_LOGIN_FAILURES=3)
    conn = conn_of(app)
    users.add_user(conn, "zaman", PASSWORD, NOW)
    guest = app.test_client()
    tok = _csrf(guest)
    texts = set()
    for _ in range(3):
        r = guest.post("/reset/not-a-token", data={"csrf": tok, "password": NEW_PASSWORD, "password2": NEW_PASSWORD})
        assert r.status_code == 404
        texts.add(re.search(r'flash-text">([^<]+)<', r.get_data(as_text=True)).group(1))
    assert texts == {users.RESET_BAD}
    assert store.count_login_failures(conn, "127.0.0.1", "2000-01-01") == 3
    good = users.create_login_reset(conn, "zaman", NOW, "owner", "owner")
    r = guest.post("/reset/" + good, data={"csrf": tok, "password": NEW_PASSWORD, "password2": NEW_PASSWORD})
    assert r.status_code == 429 and users.verify(conn, "zaman", PASSWORD) == "zaman"         # blocked even with a good link
    assert guest.get("/reset/not-a-token").status_code == 429


def test_only_the_owner_issues_and_revokes(tmp_path):
    app = make_app(tmp_path)
    conn = conn_of(app)
    users.add_user(conn, "zaman", PASSWORD, NOW)
    for role in ("partner", "chats", "viewer", "client"):
        users.add_user(conn, role + "1", PASSWORD, NOW, role=role)
        client = app.test_client()
        sign_in(client, conn, role + "1")
        assert client.post("/users/zaman/reset", data={"csrf": "tok"}).status_code == 403, role
        assert client.post("/users/resets/1/revoke", data={"csrf": "tok"}).status_code == 403, role
    assert users.list_login_resets(conn, NOW) == []
    partner = app.test_client()
    sign_in(partner, conn, "partner1")
    assert "Сбросить вход" not in partner.get("/users").get_data(as_text=True)


def test_csrf_and_the_shared_owner_login_on_the_page(tmp_path):
    app = make_app(tmp_path)
    conn = conn_of(app)
    users.add_user(conn, "zaman", PASSWORD, NOW)
    owner = app.test_client()
    tok = login(owner)
    assert owner.post("/users/zaman/reset", data={}).status_code == 400                      # no CSRF token
    assert owner.post("/users/zaman/reset", data={"csrf": "wrong"}).status_code == 400
    assert users.list_login_resets(conn, NOW) == []
    assert post(owner, "/users/owner/reset", tok).status_code == 302 and users.list_login_resets(conn, NOW) == []
    path = _issue(app, owner, tok, "zaman")
    guest = app.test_client()
    guest.get(path)
    assert guest.post(path, data={"password": NEW_PASSWORD, "password2": NEW_PASSWORD}).status_code == 400
    assert users.verify(conn, "zaman", PASSWORD) == "zaman"
    reset_id = users.list_login_resets(conn, NOW)[0]["id"]
    assert post(owner, "/users/resets/%d/revoke" % reset_id, tok).status_code == 302
    assert guest.get(path).status_code == 404


def test_the_reset_page_is_open_by_design():
    assert "login_reset_page" in common.OPEN
    assert common.SECTION_OF["users_login_reset"] == "users" and common.SECTION_OF["users_login_reset_revoke"] == "users"


def test_the_literals_of_the_rename_are_the_modules_own():
    from tires_sync import order_manager, order_notify, orders, shop_cart
    assert set(users.LOGIN_STATE_KEYS) == {orders.DEFAULT_RESPONSIBLE_KEY, shop_cart.RESPONSIBLE_KEY}
    assert (users.DUTY_KEY, users.DUTY_TODAY_KEY) == (order_manager.DUTY_KEY, order_manager.DUTY_TODAY_KEY)
    assert users.TELEGRAM_KIND == order_notify.KIND and users.RESPONSIBLE_MARK == orders.LABELS["responsible"] + ": "


def test_every_setting_and_record_that_holds_the_login_moves():
    conn = store.connect(":memory:")
    users.add_user(conn, "zamantyres", PASSWORD, NOW)
    users.add_user(conn, "olya", PASSWORD, NOW, role="chats")
    for key in users.LOGIN_STATE_KEYS:
        store.set_state(conn, key, "zamantyres")
    store.set_state(conn, users.DUTY_KEY, json.dumps({"0": "zamantyres", "1": "olya"}))
    store.set_state(conn, users.DUTY_TODAY_KEY, json.dumps({"date": "2026-10-07", "login": "zamantyres"}))
    conn.execute("INSERT INTO connection (kind, owner_id, config) VALUES (?, 0, ?)",
                 (users.TELEGRAM_KIND, json.dumps({"chats": {"zamantyres": "11", "olya": "22"}, "owner_login": "zamantyres"})))
    conn.execute("INSERT INTO order_event_seen VALUES ('zamantyres', 1, 5, ?)", (NOW,))
    conn.execute("INSERT INTO order_event_ack VALUES (7, 'zamantyres', ?, 'zamantyres')", (NOW,))
    conn.execute("INSERT INTO order_event_ack VALUES (7, '*', ?, 'olya')", (NOW,))
    conn.execute("INSERT INTO shop_cart VALUES ('zamantyres', 'g1', 2, ?)", (NOW,))
    conn.execute("INSERT INTO order_event (order_id, at, author, kind, text) VALUES (1, ?, 'olya', 'field', 'Ответственный: zamantyres')", (NOW,))
    conn.execute("INSERT INTO order_event (order_id, at, author, kind, text) VALUES (1, ?, 'zamantyres', 'note', 'zamantyres сказал')", (NOW,))
    conn.commit()
    token = users.create_login_reset(conn, "zamantyres", NOW, "owner", "owner")
    users.use_login_reset(conn, token, "zaman", NEW_PASSWORD, NOW, "owner")
    state = dict(conn.execute("SELECT key, value FROM app_state").fetchall())
    assert all(state[key] == "zaman" for key in users.LOGIN_STATE_KEYS)
    assert json.loads(state[users.DUTY_KEY]) == {"0": "zaman", "1": "olya"}
    assert json.loads(state[users.DUTY_TODAY_KEY]) == {"date": "2026-10-07", "login": "zaman"}
    assert connections.get_config(conn, users.TELEGRAM_KIND, 0) == {"chats": {"zaman": "11", "olya": "22"}, "owner_login": "zaman"}
    assert conn.execute("SELECT login FROM order_event_seen").fetchall() == [("zaman",)]
    assert sorted(conn.execute("SELECT login, author FROM order_event_ack").fetchall()) == [("*", "olya"), ("zaman", "zamantyres")]
    assert conn.execute("SELECT login FROM shop_cart").fetchall() == [("zaman",)]
    assert sorted(conn.execute("SELECT author, text FROM order_event").fetchall()) == [
        ("olya", "Ответственный: zaman"), ("zamantyres", "zamantyres сказал")]                # the history keeps the old login
    assert not users.login_taken(conn, "zamantyres") and users.login_taken(conn, "zaman")


def test_two_uses_at_once_only_one_wins(monkeypatch):
    """The page found the link good, another use took it meanwhile: the claim (one statement) refuses the second."""
    conn = store.connect(":memory:")
    users.add_user(conn, "zaman", PASSWORD, NOW)
    token = users.create_login_reset(conn, "zaman", NOW, "owner", "owner")
    seen = users.find_login_reset(conn, token, NOW)
    users.use_login_reset(conn, token, "", NEW_PASSWORD, NOW, "owner")
    monkeypatch.setattr(users, "find_login_reset", lambda *a: seen)
    with pytest.raises(users.UserError):
        users.use_login_reset(conn, token, "zaman2", "третий-пароль-3", NOW, "owner")
    assert users.verify(conn, "zaman", NEW_PASSWORD) == "zaman" and not users.exists(conn, "zaman2")


def test_the_link_of_a_removed_user_ends_and_never_opens_a_later_user_of_the_name():
    conn = store.connect(":memory:")
    users.add_user(conn, "bob", PASSWORD, NOW)
    users.add_user(conn, "zaman", PASSWORD, NOW)
    bobs = users.create_login_reset(conn, "bob", NOW, "owner", "owner")
    users.remove_user(conn, "bob", NOW)
    assert users.list_login_resets(conn, NOW) == []
    token = users.create_login_reset(conn, "zaman", NOW, "owner", "owner")
    users.use_login_reset(conn, token, "bob", NEW_PASSWORD, NOW, "owner")
    assert users.find_login_reset(conn, bobs, NOW) is None


def test_a_switched_off_user_gets_no_link_and_the_open_one_ends():
    conn = store.connect(":memory:")
    users.add_user(conn, "zaman", PASSWORD, NOW)
    token = users.create_login_reset(conn, "zaman", NOW, "owner", "owner")
    users.set_active(conn, "zaman", False, NOW, "owner")
    assert users.find_login_reset(conn, token, NOW) is None
    with pytest.raises(users.UserError):
        users.use_login_reset(conn, token, "zaman2", NEW_PASSWORD, NOW, "owner")
    with pytest.raises(users.UserError):
        users.create_login_reset(conn, "zaman", NOW, "owner", "owner")
    users.set_active(conn, "zaman", True, NOW, "owner")
    assert users.find_login_reset(conn, token, NOW) is None and users.verify(conn, "zaman", PASSWORD) == "zaman"
