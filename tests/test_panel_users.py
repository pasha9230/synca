"""A second panel user: login + password, added on the server."""
from __future__ import annotations

import io

import pytest

from panel_helpers import login, make_app, post
from tires_sync import cli, store, users
from tires_sync.users import UserError

NOW = "2026-10-14T10:00:00"
GOOD = "correct-horse-battery"


@pytest.fixture(autouse=True)
def cheap_hashes(monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")


def conn_of(tmp_path):
    return store.connect(tmp_path / "t.db")


# --- the user store ---------------------------------------------------------------------------------------------------

def test_add_verify_and_list_without_hashes(tmp_path):
    conn = conn_of(tmp_path)
    users.add_user(conn, "Anna", GOOD, NOW)
    assert users.verify(conn, "anna", GOOD) == "anna"          # logins are lower-case
    assert users.verify(conn, "ANNA", GOOD) == "anna"
    assert users.verify(conn, "anna", GOOD + "x") is None
    assert users.verify(conn, "nobody", GOOD) is None
    listing = users.list_users(conn)
    assert [u["login"] for u in listing] == ["anna"] and all("hash" not in str(u).lower() for u in listing)
    assert GOOD not in conn.execute("SELECT password_hash FROM panel_user").fetchone()[0]


@pytest.mark.parametrize("login_,password", [("a", GOOD), ("bad login", GOOD), ("../x", GOOD), ("anna", "short"), ("", GOOD)])
def test_bad_logins_and_weak_passwords_are_refused(tmp_path, login_, password):
    with pytest.raises(UserError):
        users.add_user(conn_of(tmp_path), login_, password, NOW)


def test_duplicate_user_is_refused_and_password_can_be_changed(tmp_path):
    conn = conn_of(tmp_path)
    users.add_user(conn, "anna", GOOD, NOW)
    with pytest.raises(UserError):
        users.add_user(conn, "Anna", GOOD, NOW)
    users.set_password(conn, "anna", "another-long-password", NOW)
    assert users.verify(conn, "anna", GOOD) is None and users.verify(conn, "anna", "another-long-password") == "anna"
    with pytest.raises(UserError):
        users.set_password(conn, "nobody", GOOD, NOW)


def test_removed_user_cannot_log_in(tmp_path):
    conn = conn_of(tmp_path)
    users.add_user(conn, "anna", GOOD, NOW)
    users.remove_user(conn, "anna", NOW)
    assert users.verify(conn, "anna", GOOD) is None


# --- the panel ------------------------------------------------------------------------------------------------------------

def panel(tmp_path):
    app = make_app(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    users.add_user(conn, "anna", GOOD, NOW)
    client = app.test_client()
    with client.session_transaction() as s:
        s["csrf"] = "tok"
    return app, client, conn


def csrf_of(client):
    import re
    return re.search(r'name="csrf" value="([0-9a-f]+)"', client.get("/history").get_data(as_text=True)).group(1)


def sign_in(client, login_, password):
    return client.post("/login", data={"csrf": "tok", "login": login_, "password": password})


def test_login_with_login_and_password_and_the_name_is_shown(tmp_path):
    app, client, conn = panel(tmp_path)
    r = sign_in(client, "anna", GOOD)
    assert r.status_code == 302
    assert "anna" in client.get("/").get_data(as_text=True).split("sb-foot")[1][:400]


def test_the_old_owner_password_still_works_with_an_empty_login(tmp_path):
    app, client, conn = panel(tmp_path)
    assert sign_in(client, "", "pw").status_code == 302
    

def test_wrong_password_and_unknown_login_look_the_same(tmp_path):
    app, client, conn = panel(tmp_path)
    a = sign_in(client, "anna", "wrong-password-here")
    b = sign_in(client, "nobody", "wrong-password-here")
    assert a.status_code == b.status_code == 401
    assert "Неверный логин или пароль" in a.get_data(as_text=True) and "Неверный логин или пароль" in b.get_data(as_text=True)


def test_failures_with_any_login_count_against_the_address(tmp_path):
    app, client, conn = panel(tmp_path)
    for i in range(5):
        sign_in(client, "user%d" % i, "wrong-password-here")
    assert sign_in(client, "anna", GOOD).status_code == 429


def test_one_users_logout_does_not_end_the_other_users_session(tmp_path):
    app, anna, conn = panel(tmp_path)
    owner = app.test_client()
    login(owner)
    sign_in(anna, "anna", GOOD)
    anna.post("/logout", data={"csrf": csrf_of(anna)})
    assert owner.get("/history").status_code == 200
    assert anna.get("/history").status_code == 302


def test_changing_the_password_ends_that_users_sessions_only(tmp_path):
    app, anna, conn = panel(tmp_path)
    owner = app.test_client()
    login(owner)
    sign_in(anna, "anna", GOOD)
    assert anna.get("/history").status_code == 200
    users.set_password(conn, "anna", "another-long-password", NOW)
    assert anna.get("/history").status_code == 302 and owner.get("/history").status_code == 200


def test_a_removed_user_loses_the_open_session(tmp_path):
    app, anna, conn = panel(tmp_path)
    sign_in(anna, "anna", GOOD)
    users.remove_user(conn, "anna", NOW)
    assert anna.get("/history").status_code == 302


def test_actions_are_recorded_under_the_users_own_name(tmp_path):
    from tires_sync import staging
    app, anna, conn = panel(tmp_path)
    sign_in(anna, "anna", GOOD)
    staging.set_override(conn, "1", "off", NOW)
    draft = anna.get("/catalog/review").headers["Location"]
    html = anna.get(draft).get_data(as_text=True)
    import re
    rev = re.search(r'name="rev" value="([0-9a-f]+)"', html).group(1)
    anna.post(draft + "/confirm", data={"csrf": csrf_of(anna), "rev": rev})
    assert any(a["author"] == "anna" and a["action"] == "settings_confirm" for a in store.list_audit(conn, 20))


def test_login_page_asks_for_login_and_password(tmp_path):
    app, client, conn = panel(tmp_path)
    html = client.get("/login").get_data(as_text=True)
    assert 'name="login"' in html and 'name="password"' in html


# --- the server commands -------------------------------------------------------------------------------------------------

def run(tmp_path, monkeypatch, *argv, stdin=""):
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    return cli.main(list(argv) + ["--db", str(tmp_path / "t.db")])


def test_user_commands(tmp_path, monkeypatch, capsys):
    assert run(tmp_path, monkeypatch, "user-add", "--login", "anna", "--password-stdin", stdin=GOOD + "\n") == 0
    conn = conn_of(tmp_path)
    assert users.verify(conn, "anna", GOOD) == "anna"
    assert run(tmp_path, monkeypatch, "user-add", "--login", "anna", "--password-stdin", stdin=GOOD + "\n") == 2       # duplicate
    assert run(tmp_path, monkeypatch, "user-add", "--login", "bob", "--password-stdin", stdin="short\n") == 2
    capsys.readouterr()
    assert run(tmp_path, monkeypatch, "user-list") == 0
    out = capsys.readouterr().out
    assert "anna" in out and GOOD not in out and "pbkdf2" not in out and "scrypt" not in out
    assert run(tmp_path, monkeypatch, "user-passwd", "--login", "anna", "--password-stdin", stdin="a-new-long-password\n") == 0
    assert users.verify(conn_of(tmp_path), "anna", "a-new-long-password") == "anna"
    assert run(tmp_path, monkeypatch, "user-remove", "--login", "anna") == 0
    assert users.verify(conn_of(tmp_path), "anna", "a-new-long-password") is None
    assert run(tmp_path, monkeypatch, "user-remove", "--login", "anna") == 2


def test_an_anonymous_logout_does_not_end_the_owners_sessions(tmp_path):
    app, anna, conn = panel(tmp_path)
    owner = app.test_client()
    login(owner)
    stranger = app.test_client()
    with stranger.session_transaction() as s:
        s["csrf"] = "tok"
    stranger.post("/logout", data={"csrf": "tok"})
    assert owner.get("/history").status_code == 200


@pytest.mark.parametrize("name", ["owner", "Owner", "server", "system"])
def test_reserved_names_cannot_be_used_as_logins(tmp_path, name):
    with pytest.raises(UserError):
        users.add_user(conn_of(tmp_path), name, GOOD, NOW)


def test_the_shared_password_works_for_the_owner_login_and_for_an_empty_one(tmp_path):
    app, client, conn = panel(tmp_path)
    def fresh():
        c = app.test_client()
        with c.session_transaction() as s:
            s["csrf"] = "tok"
        return c

    assert sign_in(fresh(), "owner", "pw").status_code == 302
    assert sign_in(fresh(), "", "pw").status_code == 302
    assert sign_in(fresh(), "owner", "wrong-password-here").status_code == 401


def test_a_crlf_password_from_stdin_is_cleaned(tmp_path, monkeypatch):
    assert run(tmp_path, monkeypatch, "user-add", "--login", "bob", "--password-stdin", stdin=GOOD + "\r\n") == 0
    assert users.verify(conn_of(tmp_path), "bob", GOOD) == "bob"
