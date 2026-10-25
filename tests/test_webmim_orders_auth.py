"""How the orders client gets in: Basic token, else the login of /api/v3/auth/login (Bearer authKey, then the account's key), or the unload's API key.
At most two login attempts per check, the session is always closed, no password or key in a message or the journal."""
from __future__ import annotations

import json
import logging

import pytest
from cryptography.fernet import Fernet

from http_fakes import ScriptedHttp
from panel_helpers import login, make_app, post
from tires_sync import connections, store, webmim_orders
from tires_sync.secretbox import SecretBox

BASE = "https://webmim.example"
TOKEN = (200, {"accessToken": "TOKEN-123"})
VERSION = (200, {"version": "2"})
UNAUTH = (401, {"name": "Unauthorized"})
V3_OK = (200, {"result": True, "authKey": "AUTH-KEY-9"})
V3_BAD = (200, {"result": False, "message": "Неверный логин или пароль"})
LOGOUT = (200, {"result": True})
KEY = "K" * 32
SECRETS = ("p@ss", "AUTH-KEY-9", "KEY123", KEY)


def client_for(*replies):
    http = ScriptedHttp(*replies)
    return webmim_orders.OrdersClient(BASE, "api-user", "p@ss", http=http), http


def paths(http):
    return [(c[0], c[1][len(BASE):]) for c in http.calls]


def logins(http):
    return [c for c in http.calls if c[1].endswith("/api/v2/token") or c[1].endswith("/api/v3/auth/login")]


def test_basic_token_comes_first_and_needs_no_logout():
    client, http = client_for(TOKEN, VERSION)
    with client:
        assert client.version() == "2"
    assert paths(http) == [("GET", "/api/v2/token"), ("GET", "/api/v2/version")]
    assert http.calls[1][2]["Authorization"] == "Bearer TOKEN-123" and client.method == "Basic-токен"


def test_a_rejected_basic_falls_back_to_the_login_and_its_auth_key_works_as_bearer():
    client, http = client_for(UNAUTH, V3_OK, VERSION, LOGOUT)
    with client:
        assert client.version() == "2"
    assert paths(http) == [("GET", "/api/v2/token"), ("POST", "/api/v3/auth/login"), ("GET", "/api/v2/version"), ("POST", "/api/v3/auth/logout")]
    assert json.loads(http.calls[1][3]) == {"login": "api-user", "password": "p@ss"} and http.calls[1][2]["Content-Type"] == "application/json"
    assert http.calls[2][2]["Authorization"] == "Bearer AUTH-KEY-9" and http.calls[3][2]["Authorization"] == "Bearer AUTH-KEY-9"
    assert client.method == "вход логином"


def test_when_v2_refuses_the_auth_key_the_account_key_is_taken_and_sent_as_access_token():
    client, http = client_for(UNAUTH, V3_OK, UNAUTH, (200, '"KEY123"'), LOGOUT, VERSION)
    with client:
        assert client.version() == "2"
    assert paths(http)[:5] == [("GET", "/api/v2/token"), ("POST", "/api/v3/auth/login"), ("GET", "/api/v2/version"),
                               ("GET", "/api/v2/apikey"), ("POST", "/api/v3/auth/logout")]
    assert http.calls[3][2]["Authorization"] == "Bearer AUTH-KEY-9"
    method, url, headers, _ = http.calls[5]
    assert url == BASE + "/api/v2/version?access-token=KEY123" and "Authorization" not in headers
    assert len([c for c in http.calls if c[1].endswith("/logout")]) == 1 and client.method == "вход логином, ключ API"


def test_both_ways_rejected_say_why_without_the_password_and_never_try_again():
    client, http = client_for(UNAUTH, V3_BAD)
    with client:
        with pytest.raises(webmim_orders.OrdersError) as err:
            client.version()
        text = str(err.value)
        assert "Basic-токен: 401" in text and "вход логином: неверный логин или пароль" in text
        assert not any(s in text for s in SECRETS) and "api-user" not in text
        for again in (client.version, client.dictionaries, client.openapi):
            try:
                again()
            except webmim_orders.OrdersError:
                pass
    assert len(http.calls) == 2 and len(logins(http)) == 2                               # no logout: no session was opened


def test_the_session_limit_and_an_odd_login_answer_are_named():
    client, _ = client_for(UNAUTH, (200, {"result": False, "message": "Превышено количество подключений"}))
    with pytest.raises(webmim_orders.OrdersError, match="лимит") as err:
        client.token()
    assert str(err.value).startswith("Войти не удалось")                               # not «не приняты»: the password may be right
    client, _ = client_for(UNAUTH, (500, b"oops"))
    with pytest.raises(webmim_orders.OrdersError, match="вход логином: код 500") as err:
        client.token()
    assert str(err.value).startswith("Войти не удалось")


def test_other_basic_failures_do_not_try_the_login():
    client, http = client_for((500, b"oops"))
    with pytest.raises(webmim_orders.OrdersError, match="код 500"):
        client.token()
    assert len(http.calls) == 1


def test_no_more_than_two_login_attempts_even_when_every_dictionary_is_closed():
    replies = [UNAUTH, V3_OK, UNAUTH, (403, {"x": 1})] + [UNAUTH] * 5 + [LOGOUT]
    client, http = client_for(*replies)
    with client:
        data = client.dictionaries()
    assert all("error" in v for v in data.values()) and len(logins(http)) == 2
    assert paths(http)[-1] == ("POST", "/api/v3/auth/logout") and len(http.replies) == 0


def test_a_login_without_rights_to_orders_is_told_apart():
    client, http = client_for(TOKEN, (403, {"name": "Forbidden"}))
    with pytest.raises(webmim_orders.OrdersError) as err:
        client.version()
    assert "вход есть" in str(err.value) and "к заказам нет прав" in str(err.value) and "Basic-токен" in str(err.value)
    assert len(logins(http)) == 1


def test_the_logout_happens_even_when_the_network_breaks_and_its_own_failure_is_ignored():
    calls = []

    def http(method, url, headers, body):
        calls.append(url)
        if url.endswith("/api/v2/token"):
            return 401, {}, b"{}"
        if url.endswith("/api/v3/auth/login"):
            return 200, {}, json.dumps({"result": True, "authKey": "AUTH-KEY-9"}).encode()
        raise OSError("timeout")

    client = webmim_orders.OrdersClient(BASE, "api-user", "p@ss", http=http)
    with pytest.raises(OSError):
        with client:
            client.version()
    assert calls[-1].endswith("/api/v3/auth/logout")
    client.close()                                                                       # a second close does nothing
    assert sum(1 for u in calls if u.endswith("/logout")) == 1


# ---- the page

def setup(tmp_path, *replies):
    box = SecretBox(Fernet.generate_key())
    http = ScriptedHttp(*replies)
    app = make_app(tmp_path, SECRET_BOX=box, WEBMIM_HTTP=http)
    client = app.test_client()
    tok = login(client)
    conn = store.connect(app.config["DB_PATH"])
    return app, client, tok, conn, box, http


def save_orders(client, tok, **form):
    data = dict(supplier="1", server="test", login="api-bot", password="pw-SECRET-1")
    data.update(form)
    return post(client, "/connections/webmim/orders", tok, **data)


def check(client, tok):
    return post(client, "/connections/webmim/orders-check", tok, supplier="1")


def no_secret_anywhere(conn, client, caplog, *secrets):
    status = connections.get_check(conn, "webmim_orders", 0)["status"]
    html = client.get("/connections/webmim").get_data(as_text=True)
    audit = str(list(conn.execute("SELECT * FROM audit")))
    for secret in secrets:
        assert secret not in status and secret not in html and secret not in audit and secret not in caplog.text


def test_the_check_by_login_says_how_it_got_in_and_closes_the_session(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    app, client, tok, conn, box, http = setup(tmp_path, UNAUTH, V3_OK, VERSION, *[(200, [])] * 6, (200, b"<html></html>"), LOGOUT)
    save_orders(client, tok)
    check(client, tok)
    status = connections.get_check(conn, "webmim_orders", 0)["status"]
    assert status.startswith("ok: ") and "вход логином" in status and "версия API 2" in status
    assert http.calls[-1][1] == "https://mimtest.svrauto.ru/api/v3/auth/logout"
    no_secret_anywhere(conn, client, caplog, "pw-SECRET-1", "AUTH-KEY-9")


def test_the_check_with_both_ways_rejected_records_both_reasons(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    app, client, tok, conn, box, http = setup(tmp_path, UNAUTH, V3_BAD)
    save_orders(client, tok)
    check(client, tok)
    status = connections.get_check(conn, "webmim_orders", 0)["status"]
    assert "Basic-токен: 401" in status and "неверный логин или пароль" in status and len(http.calls) == 2
    no_secret_anywhere(conn, client, caplog, "pw-SECRET-1")


def test_the_form_has_no_unload_key_choice_and_a_posted_one_is_saved_as_login(tmp_path):
    app, client, tok, conn, box, http = setup(tmp_path)
    save_orders(client, tok, auth="apikey", server="prod")
    assert connections.get_config(conn, "webmim_orders", 0) == {"server": "prod", "login": "api-bot", "auth": "login"}


def test_a_saved_apikey_mode_reads_as_login_and_the_check_goes_by_login(tmp_path):
    app, client, tok, conn, box, http = setup(tmp_path, UNAUTH, V3_OK, VERSION, *[(200, [])] * 6, (200, b"<html></html>"), LOGOUT)
    save_orders(client, tok)
    connections.set_config(conn, "webmim_orders", 0, {"server": "test", "login": "api-bot", "auth": "apikey"}, "2026-10-22T12:00:00", "owner")
    conn.commit()
    html = client.get("/connections/webmim").get_data(as_text=True)
    assert 'formaction="/connections/webmim/orders-check" disabled' not in html[html.index('id="orders"'):]
    check(client, tok)
    status = connections.get_check(conn, "webmim_orders", 0)["status"]
    assert status.startswith("ok: ") and "вход логином" in status


def test_the_page_names_what_each_server_needs(tmp_path):
    app, client, tok, conn, box, http = setup(tmp_path)
    html = client.get("/connections/webmim").get_data(as_text=True)
    assert "тестовый: учётная запись для теста от WebMIM; боевой: ваш API-доступ" in html
    assert 'name="auth"' not in html and "Тот же API-ключ" not in html
