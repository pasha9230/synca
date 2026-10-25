import re

from panel_helpers import login, make_app, post


def csrf_from(html):
    return re.search(r'name="csrf" value="([0-9a-f]+)"', html).group(1)


def test_anonymous_user_is_redirected_to_login(tmp_path):
    r = make_app(tmp_path).test_client().get("/")
    assert r.status_code == 302 and "/login" in r.headers["Location"]


def test_wrong_password_is_rejected_and_right_one_logs_in(tmp_path):
    client = make_app(tmp_path).test_client()
    tok = csrf_from(client.get("/login").get_data(as_text=True))
    assert client.post("/login", data={"csrf": tok, "password": "bad"}).status_code == 401
    assert client.get("/").status_code == 302
    assert client.post("/login", data={"csrf": tok, "password": "pw"}).status_code == 302
    assert client.get("/").status_code == 200


def test_post_without_csrf_token_is_rejected(tmp_path):
    client = make_app(tmp_path).test_client()
    client.get("/login")
    assert client.post("/login", data={"password": "pw"}).status_code == 400
    assert client.post("/login", data={"csrf": "wrong", "password": "pw"}).status_code == 400


def test_five_failures_lock_the_login_even_for_the_right_password(tmp_path):
    client = make_app(tmp_path).test_client()
    tok = csrf_from(client.get("/login").get_data(as_text=True))
    for _ in range(5):
        client.post("/login", data={"csrf": tok, "password": "bad"})
    r = client.post("/login", data={"csrf": tok, "password": "pw"})
    assert r.status_code == 429
    assert client.get("/").status_code == 302


def test_successful_login_replaces_the_session(tmp_path):
    client = make_app(tmp_path).test_client()
    tok = csrf_from(client.get("/login").get_data(as_text=True))
    with client.session_transaction() as session:
        session["planted"] = "x"
    client.post("/login", data={"csrf": tok, "password": "pw"})
    with client.session_transaction() as session:
        assert "planted" not in session and session["user"] == "owner"


def test_logout_requires_post_with_token_and_ends_the_session(tmp_path):
    client = make_app(tmp_path).test_client()
    tok = login(client)
    assert client.get("/logout").status_code == 405
    assert post(client, "/logout", tok).status_code == 302
    assert client.get("/").status_code == 302


def test_security_headers_are_present(tmp_path):
    r = make_app(tmp_path).test_client().get("/login")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "same-origin"
    assert r.headers["Set-Cookie"].count("HttpOnly") == 1


def test_a_stolen_session_cookie_stops_working_after_logout(tmp_path):
    client = make_app(tmp_path).test_client()
    tok = csrf_from(client.get("/login").get_data(as_text=True))
    client.post("/login", data={"csrf": tok, "password": "pw"})
    with client.session_transaction() as session:
        stolen = dict(session)
    assert client.get("/").status_code == 200
    with client.session_transaction() as session:
        csrf = session["csrf"]
    post(client, "/logout", csrf)
    with client.session_transaction() as session:
        session.update(stolen)
    r = client.get("/")
    assert r.status_code == 302 and "/login" in r.headers["Location"]


def test_login_sets_a_session_that_expires(tmp_path):
    client = make_app(tmp_path).test_client()
    tok = csrf_from(client.get("/login").get_data(as_text=True))
    r = client.post("/login", data={"csrf": tok, "password": "pw"})
    assert "Expires=" in r.headers["Set-Cookie"]


def test_forwarded_header_is_ignored_unless_a_proxy_is_trusted(tmp_path):
    client = make_app(tmp_path).test_client()
    tok = csrf_from(client.get("/login").get_data(as_text=True))
    for _ in range(5):
        client.post("/login", data={"csrf": tok, "password": "bad"}, headers={"X-Forwarded-For": "9.9.9.9"})
    r = client.post("/login", data={"csrf": tok, "password": "pw"}, headers={"X-Forwarded-For": "8.8.8.8"})
    assert r.status_code == 429


def test_lockout_is_per_real_client_address_behind_a_trusted_proxy(tmp_path):
    client = make_app(tmp_path, TRUST_PROXY=1).test_client()
    tok = csrf_from(client.get("/login").get_data(as_text=True))
    for _ in range(5):
        client.post("/login", data={"csrf": tok, "password": "bad"}, headers={"X-Forwarded-For": "9.9.9.9"})
    attacker = client.post("/login", data={"csrf": tok, "password": "pw"}, headers={"X-Forwarded-For": "9.9.9.9"})
    owner = client.post("/login", data={"csrf": tok, "password": "pw"}, headers={"X-Forwarded-For": "8.8.8.8"})
    assert attacker.status_code == 429 and owner.status_code == 302
