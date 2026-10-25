"""The panel against someone who read its open code (docs/runs/2026-10-24-open2): guessing the passwords, the session cookie, the shared
password's sessions, the small hardenings."""
from __future__ import annotations

import re

import pytest

from werkzeug.security import generate_password_hash

from panel_helpers import make_app, post
from tires_sync import cli, store, users
from tires_sync.panel import common
from tires_sync.panel.__main__ import build_app

PASSWORD = "длинный-пароль-1"


@pytest.fixture(autouse=True)
def cheap_hashes(monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")


def csrf_of(client) -> str:
    return re.search(r'name="csrf" value="([0-9a-f]+)"', client.get("/login").get_data(as_text=True)).group(1)


def sign(client, tok, password, login="", address=None):
    headers = {"X-Forwarded-For": address} if address else {}
    return client.post("/login", data={"csrf": tok, "login": login, "password": password}, headers=headers)


# --- Т1: guessing ------------------------------------------------------------------------------------------------------------------

def test_signing_in_with_ones_own_login_does_not_reset_the_guessing_of_the_owner_password(tmp_path):
    app = make_app(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    users.add_user(conn, "staff1", PASSWORD, "2026-10-07T10:00:00", role="viewer")
    client = app.test_client()
    tok = csrf_of(client)
    for _ in range(4):
        assert sign(client, tok, "bad").status_code == 401
    staff = app.test_client()                                                       # the same address
    assert sign(staff, csrf_of(staff), PASSWORD, login="staff1").status_code == 302    # one's own login...
    assert sign(client, tok, "bad").status_code == 401                              # ...and the fifth guess still counts
    assert sign(client, tok, "pw").status_code == 429


def test_the_addresses_of_one_ipv6_network_share_the_count_and_another_network_does_not(tmp_path):
    client = make_app(tmp_path, TRUST_PROXY=1).test_client()
    tok = csrf_of(client)
    for host in range(1, 6):
        sign(client, tok, "bad", address="2001:db8:1:2::%x" % host)
    assert sign(client, tok, "pw", address="2001:db8:1:2:ffff::1").status_code == 429
    assert sign(client, tok, "pw", address="2001:db8:1:3::1").status_code == 302


@pytest.mark.parametrize("address, key", [("2001:db8:1:2:3:4:5:6", "2001:db8:1:2::/64"), ("::ffff:9.9.9.9", "9.9.9.9"),
                                          ("9.9.9.9", "9.9.9.9"), ("garbage", "garbage")])
def test_the_key_of_an_address(tmp_path, address, key):
    app = make_app(tmp_path)
    with app.test_request_context(environ_base={"REMOTE_ADDR": address}):
        assert common.client_key() == key


def test_a_wrong_current_password_counts_as_a_failed_sign_in(tmp_path):
    app = make_app(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    users.add_user(conn, "staff1", PASSWORD, "2026-10-07T10:00:00", role="viewer")
    client = app.test_client()
    tok = csrf_of(client)
    assert sign(client, tok, PASSWORD, login="staff1").status_code == 302
    tok = re.search(r'name="csrf" value="([0-9a-f]+)"', client.get("/account/password").get_data(as_text=True)).group(1)
    for _ in range(5):
        post(client, "/account/password", tok, old="guess-%d-xxxxx" % _, new="новый-пароль-22", new2="новый-пароль-22")
    r = post(client, "/account/password", tok, old=PASSWORD, new="новый-пароль-22", new2="новый-пароль-22")
    assert r.status_code == 302 and users.verify(conn, "staff1", PASSWORD) == "staff1"      # the right one is refused too: the count is full
    assert "Слишком много попыток" in client.get("/account/password").get_data(as_text=True)
    other = app.test_client()
    assert sign(other, csrf_of(other), "pw").status_code == 429                   # the same address: the sign-in page is closed as well


def told(monkeypatch):
    """What the owner hears on Telegram (notify.notifier_for), whatever Telegram the base has."""
    from tires_sync import notify
    said = []
    monkeypatch.setattr(notify, "notifier_for", lambda conn, box, http=None: said.append)
    return said


def test_many_failures_from_everywhere_tell_the_owner_once_and_lock_nobody(tmp_path, monkeypatch):
    said = told(monkeypatch)
    client = make_app(tmp_path, TRUST_PROXY=1).test_client()
    tok = csrf_of(client)
    for n in range(common.LOGIN_ALERT_FAILURES + 5):
        sign(client, tok, "bad", address="10.0.%d.%d" % (n // 4, n % 4))
    assert len(said) == 1 and "больше 30 неудачных входов" in said[0]
    assert sign(client, tok, "pw", address="10.9.9.9").status_code == 302              # an address of its own still signs in


def test_the_alert_is_not_lost_when_another_worker_counted_past_the_threshold(tmp_path, monkeypatch):
    said = told(monkeypatch)
    app = make_app(tmp_path, TRUST_PROXY=1)
    conn = store.connect(app.config["DB_PATH"])
    for n in range(common.LOGIN_ALERT_FAILURES + 1):                                        # the other worker's failures: 31 already
        store.add_login_failure(conn, "10.5.0.%d" % n, "2026-10-07T11:59:00")
    client = app.test_client()
    sign(client, csrf_of(client), "bad", address="10.6.0.1")                                # this one makes 32: past, never «== 31»
    assert len(said) == 1


def test_the_guessing_alert_never_silences_the_alerts_of_the_services(tmp_path, monkeypatch):
    told(monkeypatch)
    app = make_app(tmp_path, TRUST_PROXY=1)
    client = app.test_client()
    tok = csrf_of(client)
    for n in range(common.LOGIN_ALERT_FAILURES + 1):
        sign(client, tok, "bad", address="10.0.%d.%d" % (n // 4, n % 4))
    conn = store.connect(app.config["DB_PATH"])
    assert store.get_state(conn, "last_alert_at") is None and store.get_state(conn, "last_alert_text") is None     # cli._alert's own pause
    assert conn.execute("SELECT COUNT(*) FROM audit WHERE action = 'alert'").fetchone()[0] == 1                       # the journal has it


def test_a_failing_alert_never_breaks_the_sign_in_page(tmp_path, monkeypatch):
    from tires_sync import notify

    def broken(text):
        raise RuntimeError("telegram is down")
    monkeypatch.setattr(notify, "notifier_for", lambda conn, box, http=None: broken)
    client = make_app(tmp_path, TRUST_PROXY=1).test_client()
    tok = csrf_of(client)
    codes = {sign(client, tok, "bad", address="10.1.%d.%d" % (n // 4, n % 4)).status_code for n in range(common.LOGIN_ALERT_FAILURES + 2)}
    assert codes == {401}


def test_opening_a_wrong_invitation_link_counts_nothing_only_sending_its_form_does(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    for _ in range(6):                                                                      # five <img src=/invite/x> on a foreign page
        assert client.get("/invite/x").status_code == 404
    assert sign(client, csrf_of(client), "pw").status_code == 302                          # the address is not closed by them
    guesser = app.test_client()                                                             # the same address
    tok = csrf_of(guesser)
    for _ in range(5):
        guesser.post("/invite/x", data={"csrf": tok, "login": "a1", "password": "p" * 12, "password2": "p" * 12})
    assert guesser.get("/invite/x").status_code == 429


def test_a_csrf_token_out_of_ascii_is_a_400_not_a_500(tmp_path):
    client = make_app(tmp_path).test_client()
    client.get("/login")
    assert client.post("/login", data={"csrf": "тест", "password": "pw"}).status_code == 400


# --- Т2: the cookie over https only, HSTS ------------------------------------------------------------------------------------------

def built(tmp_path, **env):
    """The panel as the server starts it (from the environment), on the base and files of make_app."""
    seeded = make_app(tmp_path)
    env = dict({"PANEL_PASSWORD_HASH": generate_password_hash("pw", method="pbkdf2:sha256:1000"), "PANEL_SECRET_KEY": "k" * 32}, **env)
    return build_app(env, seeded.config["DB_PATH"], seeded.config["UNLOAD_PATH"], seeded.config["REFERENCE_PATH"])


def test_the_session_cookie_is_https_only_by_default_and_hsts_is_left_to_nginx(tmp_path):
    client = built(tmp_path).test_client()
    page = client.get("/login", base_url="https://panel.example")
    assert "Secure" in page.headers["Set-Cookie"]
    # HSTS comes from nginx (its server block of 443, deploy/nginx-panel.conf): from both the browser got the header twice (tails2 П7)
    assert "Strict-Transport-Security" not in page.headers
    assert "Strict-Transport-Security" not in client.get("/s/x", base_url="https://panel.example").headers


def test_a_local_panel_over_http_with_secure_cookies_off_signs_in_and_keeps_the_session(tmp_path):
    client = built(tmp_path, PANEL_SECURE_COOKIES="0").test_client()
    page = client.get("/login")
    assert "Secure" not in page.headers["Set-Cookie"]
    tok = re.search(r'name="csrf" value="([0-9a-f]+)"', page.get_data(as_text=True)).group(1)
    assert client.post("/login", data={"csrf": tok, "password": "pw"}).status_code == 302
    assert client.get("/").status_code == 200 and client.get("/feeds").status_code == 200


# --- Т3: a new shared password ends the sessions of the old one ---------------------------------------------------------------------

def test_a_new_shared_password_in_the_config_ends_the_open_sessions_of_the_old_one(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    assert sign(client, csrf_of(client), "pw").status_code == 302 and client.get("/").status_code == 200
    app.config["PASSWORD_HASH"] = generate_password_hash("new-pw", method="pbkdf2:sha256:1000")       # the owner changed panel.env
    r = client.get("/")
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    assert sign(client, csrf_of(client), "new-pw").status_code == 302 and client.get("/").status_code == 200


def test_a_shared_password_session_without_the_mark_is_over_and_personal_sessions_need_none(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    with client.session_transaction() as session:
        session.update({"user": "owner", "epoch": 0, "csrf": "tok"})                              # made before the mark (one sign-in again)
    assert client.get("/").status_code == 302
    conn = store.connect(app.config["DB_PATH"])
    users.add_user(conn, "pavel", PASSWORD, "2026-10-07T10:00:00")
    staff = app.test_client()
    assert sign(staff, csrf_of(staff), PASSWORD, login="pavel").status_code == 302
    app.config["PASSWORD_HASH"] = generate_password_hash("new-pw", method="pbkdf2:sha256:1000")
    assert staff.get("/").status_code == 200                                                       # a personal password is not the shared one


# --- Т4: the small hardenings ------------------------------------------------------------------------------------------------------

def test_the_open_pages_are_exactly_these():
    """A page open without a sign-in is a decision: the list is here, so that adding one fails a test (GET pages also fail
    test_panel_security.test_every_page_except_login_requires_a_session; a POST only this one)."""
    assert common.OPEN == {
        "static", "login", "logout", "health", "invite_page", "account_photo", "stored_photo", "share_public", "share_photo", "vk_hook",
        "avito_hook", "agent_jobs", "agent_fail", "agent_ref", "agent_result", "agent_copy_upload", "agent_copies_todo",
        "agent_review_sheet", "agent_work", "agent_run_done"}


def test_no_address_prefix_opens_a_page_without_a_sign_in(tmp_path):
    app = make_app(tmp_path)
    app.add_url_rule("/s/<token>/probe", "probe_under_s", lambda token: "inside")
    r = app.test_client().get("/s/abc/probe")
    assert r.status_code == 302 and "/login" in r.headers["Location"]


@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH"])
def test_every_changing_method_needs_the_csrf_token_not_only_post(tmp_path, method):
    app = make_app(tmp_path)
    app.add_url_rule("/probe", "probe_methods", lambda: "changed", methods=[method])
    client = app.test_client()
    from panel_helpers import login
    tok = login(client)
    assert client.open("/probe", method=method).status_code == 400
    assert client.open("/probe", method=method, data={"csrf": tok}).status_code == 403          # past the token: no right to a page nobody named

