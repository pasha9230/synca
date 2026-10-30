from __future__ import annotations

import json
import re

from cryptography.fernet import Fernet

from http_fakes import ScriptedHttp
from panel_helpers import login, make_app, post
from tires_sync import connections, store
from tires_sync.secretbox import SecretBox

KEY = "B" * 32


def setup(tmp_path, **cfg):
    box = SecretBox(Fernet.generate_key())
    app = make_app(tmp_path, SECRET_BOX=box, **cfg)
    client = app.test_client()
    return app, box, client, login(client)


def page(client):
    return client.get("/connections/webmim").get_data(as_text=True)


def apage(client, account_id=1):
    return client.get("/connections/avito/%d" % account_id).get_data(as_text=True)


SELF = (200, {"id": 80000001, "name": "Шины СПб", "email": "x@y.z"})


def test_page_requires_login_and_lists_sections(tmp_path):
    app, _, client, _ = setup(tmp_path)
    assert app.test_client().get("/connections").status_code == 302
    assert client.get("/connections").headers["Location"].endswith("/connections/webmim")
    html = page(client)
    assert "Подключения" in html and "WebMIM" in html and "Аккаунты Авито" in html and "Telegram" in html
    assert 'href="/connections/telegram"' in html and 'href="/connections/avito"' in html
    assert "Подключения" in client.get("/connections/telegram").get_data(as_text=True)
    none = client.get("/connections/avito")
    assert none.status_code == 200 and "Добавить аккаунт" in none.get_data(as_text=True)


def test_saved_secrets_are_encrypted_and_never_shown_or_audited(tmp_path):
    app, box, client, tok = setup(tmp_path)
    post(client, "/connections/webmim", tok, base_url="https://mimtest.svrauto.ru", mode="login",
         login="user1", password="p@ss-SECRET-1", api_key="")
    html = page(client)
    assert "p@ss-SECRET-1" not in html and "user1" in html and "задан" in html
    conn = store.connect(app.config["DB_PATH"])
    ciphertext = conn.execute("SELECT ciphertext FROM secret").fetchone()[0]
    assert "SECRET" not in ciphertext
    audit = json.dumps(store.list_audit(conn, 20), ensure_ascii=False)
    assert "secret_set" in audit and "SECRET" not in audit


def test_empty_secret_field_keeps_the_saved_value(tmp_path):
    app, box, client, tok = setup(tmp_path)
    post(client, "/connections/webmim", tok, base_url="https://mimtest.svrauto.ru", mode="login",
         login="user1", password="first-secret", api_key="")
    post(client, "/connections/webmim", tok, base_url="https://mimtest.svrauto.ru", mode="login",
         login="user2", password="   ", api_key="")
    conn = store.connect(app.config["DB_PATH"])
    assert connections.get_secret(conn, box, "webmim", 0, "password") == "first-secret"
    assert connections.get_config(conn, "webmim", 0)["login"] == "user2"


def test_http_address_is_rejected_and_config_is_unchanged(tmp_path):
    app, box, client, tok = setup(tmp_path)
    post(client, "/connections/webmim", tok, base_url="https://mimtest.svrauto.ru", mode="login", login="u")
    post(client, "/connections/webmim", tok, base_url="http://evil.example", mode="login", login="u")
    assert "https" in page(client)
    conn = store.connect(app.config["DB_PATH"])
    assert connections.get_config(conn, "webmim", 0)["base_url"] == "https://mimtest.svrauto.ru"


def test_without_a_master_key_the_page_works_and_secrets_are_refused(tmp_path):
    app = make_app(tmp_path, SECRET_BOX=None)
    client = app.test_client()
    tok = login(client)
    assert "Мастер-ключ недоступен" in page(client)
    post(client, "/connections/webmim", tok, base_url="https://mimtest.svrauto.ru", mode="login",
         login="u", password="TOPSECRET")
    html = page(client)
    assert "TOPSECRET" not in html and "Мастер-ключ недоступен" in html
    conn = store.connect(app.config["DB_PATH"])
    assert conn.execute("SELECT COUNT(*) FROM secret").fetchone()[0] == 0
    r = client.post("/connections/webmim/check", data={"csrf": tok})
    assert r.status_code == 302 and r.headers["Location"].endswith("/connections/webmim/1")


def test_webmim_check_reports_ok_and_is_rate_limited(tmp_path):
    http = ScriptedHttp((200, {"result": True, "authKey": "A"}), (200, '"%s"' % KEY), (200, {"result": True}))
    app, box, client, tok = setup(tmp_path, WEBMIM_HTTP=http)
    post(client, "/connections/webmim", tok, base_url="https://mimtest.svrauto.ru", mode="login",
         login="user1", password="pw", api_key="")
    post(client, "/connections/webmim/check", tok)
    assert "ключ получен" in page(client)
    calls = len(http.calls)
    post(client, "/connections/webmim/check", tok)  # the test clock is frozen: 0 s since the last check
    assert len(http.calls) == calls
    assert "10 секунд" in page(client)


def test_webmim_check_failure_does_not_leak_the_password(tmp_path):
    http = ScriptedHttp((401, "echo TOPSECRET-PW"))
    app, box, client, tok = setup(tmp_path, WEBMIM_HTTP=http)
    post(client, "/connections/webmim", tok, base_url="https://mimtest.svrauto.ru", mode="login",
         login="user1", password="TOPSECRET-PW")
    post(client, "/connections/webmim/check", tok)
    html = page(client)
    assert "TOPSECRET-PW" not in html
    assert "error: Вход отклонён" in html
    conn = store.connect(app.config["DB_PATH"])
    assert "TOPSECRET-PW" not in json.dumps(connections.get_check(conn, "webmim", 0), ensure_ascii=False)


def test_adding_an_account_shows_its_private_feed_link_and_keys_are_masked(tmp_path):
    app, box, client, tok = setup(tmp_path)
    post(client, "/connections/accounts", tok, name="Основной")
    html = apage(client)
    token = re.search(r"/feed/([0-9a-f]{32})/avito_feed\.xml", html).group(1)
    assert token and "Основной" in html
    post(client, "/connections/accounts/1/keys", tok, client_id="CLIENTID-12345678", client_secret="SECRETVALUE-XYZ")
    html = apage(client)
    assert "SECRETVALUE-XYZ" not in html and "CLIENTID-12345678" not in html and "••••5678" in html
    post(client, "/connections/accounts/1/keys", tok, client_id="", client_secret="")
    conn = store.connect(app.config["DB_PATH"])
    assert connections.get_secret(conn, box, "avito", 1, "client_secret") == "SECRETVALUE-XYZ"


def test_unknown_account_returns_404(tmp_path):
    app, _, client, tok = setup(tmp_path)
    assert client.post("/connections/accounts/99/keys", data={"csrf": tok}).status_code == 404
    assert client.post("/connections/accounts/99/check", data={"csrf": tok}).status_code == 404


def test_avito_check_explains_forbidden_profile_and_rejected_keys(tmp_path):
    forbidden = (403, {"error": {"code": 403, "message": "Получение профиля недоступно."}})
    token = (200, {"access_token": "t", "expires_in": 3600})
    app, box, client, tok = setup(tmp_path, AVITO_HTTP=ScriptedHttp(token, SELF, forbidden))
    post(client, "/connections/accounts", tok, name="Основной")
    post(client, "/connections/accounts/1/keys", tok, client_id="CLIENTID-12345678", client_secret="SECRETVALUE-XYZ")
    post(client, "/connections/accounts/1/check", tok)
    html = apage(client)
    assert "профиль автозагрузки недоступен" in html and "SECRETVALUE-XYZ" not in html
    (tmp_path / "second").mkdir()
    rejected = ScriptedHttp((401, {"error": {"code": 1, "message": "bad SECRETVALUE-XYZ"}}))
    app2, box2, client2, tok2 = setup(tmp_path / "second", AVITO_HTTP=rejected)
    post(client2, "/connections/accounts", tok2, name="Основной")
    post(client2, "/connections/accounts/1/keys", tok2, client_id="CLIENTID-12345678", client_secret="SECRETVALUE-XYZ")
    post(client2, "/connections/accounts/1/check", tok2)
    html2 = apage(client2)
    assert "ключи отклонены" in html2 and "SECRETVALUE-XYZ" not in html2


def test_avito_check_never_calls_a_write_method(tmp_path):
    ok_profile = (200, {"autoload_enabled": True, "feeds_data": [], "report_email": "", "schedule": []})
    http = ScriptedHttp((200, {"access_token": "t", "expires_in": 3600}), SELF, ok_profile)
    app, box, client, tok = setup(tmp_path, AVITO_HTTP=http)
    post(client, "/connections/accounts", tok, name="Основной")
    post(client, "/connections/accounts/1/keys", tok, client_id="CLIENTID-12345678", client_secret="SECRETVALUE-XYZ")
    post(client, "/connections/accounts/1/check", tok)
    assert [c[0] for c in http.calls] == ["POST", "GET", "GET"]
    assert http.calls[0][1].endswith("/token/") and http.calls[1][1].endswith("/core/v1/accounts/self")
    assert http.calls[2][1].endswith("/autoload/v2/profile")
    html = apage(client)
    assert "профиль автозагрузки доступен" in html and "ID 80000001" in html and "Шины СПб" in html
    assert 'href="/connections/avito/1" class="is-active">Шины СПб · ID 80000001<' in html


def boom(*args):
    raise OSError("network is down")


def test_changing_the_webmim_address_clears_saved_credentials(tmp_path):
    http = ScriptedHttp()
    app, box, client, tok = setup(tmp_path, WEBMIM_HTTP=http)
    post(client, "/connections/webmim", tok, base_url="https://mimtest.svrauto.ru", mode="login",
         login="u", password="REAL-PW", api_key="K" * 32)
    post(client, "/connections/webmim", tok, base_url="https://other.example", mode="login",
         login="u", password="", api_key="")
    conn = store.connect(app.config["DB_PATH"])
    assert conn.execute("SELECT COUNT(*) FROM secret WHERE kind = 'webmim'").fetchone()[0] == 0
    assert "введите их заново" in page(client)
    post(client, "/connections/webmim/check", tok)
    assert http.calls == []  # nothing is sent anywhere without a stored password


def test_unchanged_address_keeps_the_credentials(tmp_path):
    app, box, client, tok = setup(tmp_path)
    for login_name in ("u1", "u2"):
        post(client, "/connections/webmim", tok, base_url="https://mimtest.svrauto.ru", mode="login",
             login=login_name, password="REAL-PW" if login_name == "u1" else "", api_key="")
    conn = store.connect(app.config["DB_PATH"])
    assert connections.get_secret(conn, box, "webmim", 0, "password") == "REAL-PW"


def test_webmim_check_survives_a_network_error_and_is_still_rate_limited(tmp_path):
    calls = []

    def flaky(*args):
        calls.append(args)
        raise OSError("network is down")

    app, box, client, tok = setup(tmp_path, WEBMIM_HTTP=flaky)
    post(client, "/connections/webmim", tok, base_url="https://mimtest.svrauto.ru", mode="login",
         login="u", password="pw")
    assert client.post("/connections/webmim/check", data={"csrf": tok}).status_code == 302
    assert "Сервер недоступен" in page(client)
    post(client, "/connections/webmim/check", tok)
    assert len(calls) == 1


def test_avito_check_survives_a_network_error_and_is_still_rate_limited(tmp_path):
    calls = []

    def flaky(*args):
        calls.append(args)
        raise OSError("network is down")

    app, box, client, tok = setup(tmp_path, AVITO_HTTP=flaky)
    post(client, "/connections/accounts", tok, name="Основной")
    post(client, "/connections/accounts/1/keys", tok, client_id="CLIENTID-12345678", client_secret="SECRETVALUE-XYZ")
    assert client.post("/connections/accounts/1/check", data={"csrf": tok}).status_code == 302
    assert "Сервер недоступен" in apage(client)
    post(client, "/connections/accounts/1/check", tok)
    assert len(calls) == 1


def test_avito_check_does_not_retry_on_rate_limit(tmp_path):
    http = ScriptedHttp((200, {"access_token": "t", "expires_in": 3600}),
                        (429, {"error": {"code": 429, "message": "slow"}}, {"Retry-After": "200"}))
    app, box, client, tok = setup(tmp_path, AVITO_HTTP=http)
    post(client, "/connections/accounts", tok, name="Основной")
    post(client, "/connections/accounts/1/keys", tok, client_id="CLIENTID-12345678", client_secret="SECRETVALUE-XYZ")
    post(client, "/connections/accounts/1/check", tok)
    assert len(http.calls) == 2
    assert "код 429" in apage(client)


def test_webmim_apikey_mode_check_really_talks_to_the_server(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    conn.close()
    http = ScriptedHttp((401, "nope"))
    app, box, client, tok = setup(tmp_path, WEBMIM_HTTP=http)
    db = store.connect(app.config["DB_PATH"])
    raw = json.loads(db.execute("SELECT json FROM settings_version WHERE id = 1").fetchone()[0])
    raw["territory_rn"] = "103261489424"
    db.execute("UPDATE settings_version SET json = ? WHERE id = 1", (json.dumps(raw),))
    db.commit()
    post(client, "/connections/webmim", tok, base_url="https://webmim.svrauto.ru", mode="apikey",
         login="", password="", api_key="GARBAGEKEY12345678")
    post(client, "/connections/webmim/check", tok)
    assert len(http.calls) == 1 and "access-token=GARBAGEKEY12345678" in http.calls[0][1]
    html = page(client)
    assert "error: Выгрузка недоступна (код 401)" in html and "GARBAGEKEY" not in html


def test_account_page_has_sections_and_plain_explanations(tmp_path):
    app, box, client, tok = setup(tmp_path)
    post(client, "/connections/accounts", tok, name="Основной")
    html = apage(client)
    for text in ("Публикация", "Авито адреса", "Распределение по брендам", "Описание объявлений",
                 "Как делятся объявления", "Для чего", "Рекомендуется"):
        assert text in html
    assert "Основной" in html  # until the Avito id is known the tab keeps the given name
    post(client, "/connections/accounts", tok, name="Второй")
    assert 'href="/connections/avito/2"' in apage(client)


def test_keys_typed_into_the_check_form_read_the_cabinet_anew_and_one_number_can_be_pinned(tmp_path):
    """New keys in the form of «Проверить подключение» go the way of «Сохранить ключи»: the number pinned in the old cabinet does not stay
    for a cabinet that lacks it; the choice of the number is shown for one number too (the team lead 2026-10-23)."""
    token = (200, {"access_token": "t", "expires_in": 3600})
    profile = (200, {"autoload_enabled": True, "feeds_data": [], "report_email": "", "schedule": []})
    other = (200, {"id": 777, "name": "Другой", "phone": "79990001122", "phones": ["79990001122"]})
    app, box, client, tok = setup(tmp_path, AVITO_HTTP=ScriptedHttp(token, other, profile))
    post(client, "/connections/accounts", tok, name="Партнёр-2")
    conn = store.connect(app.config["DB_PATH"])
    connections.set_avito_identity(conn, 1, 80000001, "Шины СПб", "79000000013", ["79000000013", "79000000012"])
    connections.pin_phone(conn, 1, "79000000012", "2026-10-23T10:00:00", "o")
    assert 'data-phone-pick' in apage(client) and "несколько номеров" in apage(client)
    post(client, "/connections/accounts/1/check", tok, client_id="CLIENTID-NEW-0001", client_secret="SECRETVALUE-NEW")
    ident = connections.get_avito_identity(conn, 1)
    assert ident["id"] == 777 and ident["phone"] == "+79990001122" and ident["pinned"] is False
    html = apage(client)
    assert "data-phone-pick" in html and "В кабинете один номер." in html and "не закреплён" in html


def test_keys_typed_into_the_check_form_forget_the_old_cabinet_even_when_the_new_one_does_not_say_who_it_is(tmp_path):
    """The old cabinet's id and number must not stay with new keys: no number goes into the ads until a check reads the new cabinet."""
    token = (200, {"access_token": "t", "expires_in": 3600})
    hidden = (403, {"error": {"code": 403, "message": "нет доступа"}})
    profile = (200, {"autoload_enabled": True, "feeds_data": [], "report_email": "", "schedule": []})
    app, box, client, tok = setup(tmp_path, AVITO_HTTP=ScriptedHttp(token, hidden, profile))
    post(client, "/connections/accounts", tok, name="Партнёр-2")
    conn = store.connect(app.config["DB_PATH"])
    connections.set_avito_identity(conn, 1, 80000001, "Шины СПб", "79000000013", ["79000000013"])
    post(client, "/connections/accounts/1/check", tok, client_id="CLIENTID-NEW-0001", client_secret="SECRETVALUE-NEW")
    ident = connections.get_avito_identity(conn, 1)
    assert ident["id"] is None and ident["phone"] == ""
