"""Access check to the supplier's orders API (reads only): token by Basic auth, version, dictionaries."""
from __future__ import annotations

import base64

import pytest
from cryptography.fernet import Fernet

from http_fakes import ScriptedHttp
from panel_helpers import login, make_app, post
from tires_sync import connections, store, webmim_orders
from tires_sync.secretbox import SecretBox

TOKEN = (200, {"accessToken": "TOKEN-123", "tokenType": "bearer"})
VERSION = (200, {"version": "2"})
SHIP = (200, {"1": "Доставка", "2": "Самовывоз"})
PAY = (200, {"3": "безнал", "2": "физ.лицо"})
TERMS = (200, {"1": "Предоплата", "2": "Отсрочка"})
REJECTED = ((401, {"name": "Unauthorized"}), (200, {"result": False, "message": "Неверный логин или пароль"}))   # Basic, then the login of v3


def client_for(*replies):
    http = ScriptedHttp(*replies)
    return webmim_orders.OrdersClient("https://webmim.example", "api-user", "p@ss", http=http), http


def test_token_is_taken_with_basic_auth_and_then_used_as_bearer():
    client, http = client_for(TOKEN, VERSION)
    assert client.version() == "2"
    method, url, headers, body = http.calls[0]
    assert (method, url) == ("GET", "https://webmim.example/api/v2/token") and body is None
    assert headers["Authorization"] == "Basic " + base64.b64encode(b"api-user:p@ss").decode()
    assert http.calls[1][1].endswith("/api/v2/version") and http.calls[1][2]["Authorization"] == "Bearer TOKEN-123"


def test_wrong_credentials_and_missing_ones_are_explained_without_echoing_them():
    client, _ = client_for(*REJECTED)
    with pytest.raises(webmim_orders.OrdersError) as err:
        client.token()
    assert "не приняты" in str(err.value) and "p@ss" not in str(err.value) and "api-user" not in str(err.value)
    empty = webmim_orders.OrdersClient("https://x.example", "", "", http=ScriptedHttp())
    with pytest.raises(webmim_orders.OrdersError):
        empty.token()
    with pytest.raises(ValueError):
        webmim_orders.OrdersClient("http://insecure.example", "a", "b")
    assert "p@ss" not in repr(client) and "api-user" not in repr(client)


def test_dictionaries_are_read_one_by_one_and_a_closed_one_does_not_hide_the_rest():
    client, http = client_for(TOKEN, PAY, SHIP, TERMS, (403, {"x": 1}), (200, [{"id": 1}, {"id": 2}]), (200, [{"id": 5}]))
    data = client.dictionaries()
    assert [c[1].split("/api/v2")[1] for c in http.calls[1:]] == [
        "/account/payment-types", "/account/shipment-types", "/account/payment-terms", "/addresses", "/consignee", "/territories"]
    assert "error" in data["addresses"] and len(data["consignees"]) == 2
    line = webmim_orders.summarize(data)
    assert "Самовывоз" in line and "безнал" in line and "адресов ошибка" in line and "получателей 2" in line and "территорий 1" in line
    assert all(c[0] == "GET" for c in http.calls)                                        # nothing is written


def test_the_full_description_is_returned_only_when_the_token_opens_it():
    document = b"openapi: 3.0.0\ninfo:\n  title: MIM"
    client, _ = client_for(TOKEN, (200, document))
    assert client.openapi() == document
    login_page, _ = client_for(TOKEN, (200, b"<!doctype html><html>login</html>"))
    assert login_page.openapi() is None
    redirected, _ = client_for(TOKEN, (302, b""))
    assert redirected.openapi() is None


# ---- the page

def setup(tmp_path, *replies):
    box = SecretBox(Fernet.generate_key())
    http = ScriptedHttp(*replies)
    app = make_app(tmp_path, SECRET_BOX=box, WEBMIM_HTTP=http)
    client = app.test_client()
    tok = login(client)
    conn = store.connect(app.config["DB_PATH"])
    return app, client, tok, conn, box, http


def save_orders(client, tok, server="test", login_name="api-bot", password="pw-SECRET-1"):
    return post(client, "/connections/webmim/orders", tok, supplier="1", server=server, login=login_name, password=password)


def page(client):
    return client.get("/connections/webmim").get_data(as_text=True)


def check_button(html):
    import re
    block = html[html.index('id="orders"'):]
    return re.search(r"<button[^>]*formaction=\"/connections/webmim/orders-check\"([^>]*)>", block).group(1)


def test_the_page_offers_the_check_only_with_a_login_and_a_password(tmp_path):
    app, client, tok, conn, box, http = setup(tmp_path)
    html = page(client)
    assert "Заказы в WebMIM" in html and "disabled" in check_button(html) and "Тестовый сервер" in html and "Боевой сервер" in html
    save_orders(client, tok)
    html = page(client)
    assert "disabled" not in check_button(html) and 'value="api-bot"' in html and "pw-SECRET-1" not in html and "задан" in html


def test_the_password_is_stored_encrypted_kept_when_the_field_is_left_empty_and_never_shown(tmp_path):
    app, client, tok, conn, box, http = setup(tmp_path)
    save_orders(client, tok)
    save_orders(client, tok, login_name="other-bot", password="")                              # the field is empty: the password stays
    assert connections.get_secret(conn, box, "webmim_orders", 0, "password") == "pw-SECRET-1"
    assert connections.get_config(conn, "webmim_orders", 0)["login"] == "other-bot"
    raw = conn.execute("SELECT ciphertext FROM secret WHERE kind = 'webmim_orders'").fetchone()[0]
    assert "pw-SECRET-1" not in raw and "pw-SECRET-1" not in page(client)
    assert "pw-SECRET-1" not in str(list(conn.execute("SELECT * FROM audit")))


def test_only_the_two_known_servers_can_be_chosen_and_a_bad_login_is_refused(tmp_path):
    app, client, tok, conn, box, http = setup(tmp_path)
    save_orders(client, tok, server="https://evil.example")
    assert connections.get_config(conn, "webmim_orders", 0) == {} and connections.get_secret(conn, box, "webmim_orders", 0, "password") is None
    save_orders(client, tok, login_name="has space")
    assert connections.get_config(conn, "webmim_orders", 0) == {}
    save_orders(client, tok, server="prod")
    assert connections.get_config(conn, "webmim_orders", 0)["server"] == "prod"


def test_the_chosen_server_decides_where_the_login_is_sent(tmp_path):
    app, client, tok, conn, box, http = setup(tmp_path, *REJECTED)
    save_orders(client, tok, server="test")
    post(client, "/connections/webmim/orders-check", tok, supplier="1")
    assert http.calls[0][1].startswith("https://mimtest.svrauto.ru/api/v2/token")
    (tmp_path / "p").mkdir()
    app2, client2, tok2, conn2, box2, http2 = setup(tmp_path / "p", *REJECTED)
    save_orders(client2, tok2, server="prod")
    post(client2, "/connections/webmim/orders-check", tok2, supplier="1")
    assert http2.calls[0][1].startswith("https://webmim.svrauto.ru/api/v2/token")


def test_a_successful_check_is_recorded_with_a_summary_and_never_shows_the_password(tmp_path):
    app, client, tok, conn, box, http = setup(tmp_path, TOKEN, VERSION, PAY, SHIP, TERMS, (200, [{"id": 1}]), (200, []), (200, [{"id": 7}]),
                                              (200, b"<html>login</html>"))
    save_orders(client, tok)
    assert post(client, "/connections/webmim/orders-check", tok, supplier="1").status_code == 302
    html = page(client)
    assert "доступ есть" in html and "версия API 2" in html and "Самовывоз" in html and "Тестовый" in html and "Basic-токен" in html and "pw-SECRET-1" not in html
    assert all(c[0] == "GET" for c in http.calls)
    assert connections.get_check(conn, "webmim_orders", 0)["status"].startswith("ok: ")


def test_a_rejected_login_is_shown_as_no_access(tmp_path):
    app, client, tok, conn, box, http = setup(tmp_path, *REJECTED)
    save_orders(client, tok)
    post(client, "/connections/webmim/orders-check", tok, supplier="1")
    html = page(client)
    assert "нет доступа" in html and "не приняты" in html and "pw-SECRET-1" not in html


def test_the_main_webmim_form_does_not_touch_the_orders_settings(tmp_path):
    app, client, tok, conn, box, http = setup(tmp_path)
    save_orders(client, tok)
    post(client, "/connections/webmim", tok, supplier="1", base_url="https://webmim.svrauto.ru", mode="apikey", login="", password="", api_key="K" * 32)
    post(client, "/connections/webmim", tok, supplier="1", base_url="https://other.svrauto.ru", mode="apikey", login="", password="", api_key="")      # the server changes: its secrets go
    assert connections.get_secret(conn, box, "webmim_orders", 0, "password") == "pw-SECRET-1"
    assert connections.get_config(conn, "webmim_orders", 0)["login"] == "api-bot"


def test_the_check_needs_a_login_a_csrf_token_and_does_not_run_twice_in_a_row(tmp_path):
    app, client, tok, conn, box, http = setup(tmp_path, *REJECTED, TOKEN, VERSION)
    save_orders(client, tok)
    assert app.test_client().post("/connections/webmim/orders-check", data={"supplier": "1"}).status_code == 400
    assert app.test_client().post("/connections/webmim/orders", data={"supplier": "1", "server": "test", "login": "x"}).status_code == 400
    post(client, "/connections/webmim/orders-check", tok, supplier="1")
    calls = len(http.calls)
    post(client, "/connections/webmim/orders-check", tok, supplier="1")                  # too soon: no second round of requests
    assert len(http.calls) == calls
