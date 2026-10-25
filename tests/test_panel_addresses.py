from __future__ import annotations

from panel_helpers import login, make_app, post
from tires_sync import connections, store


def setup(tmp_path, with_account=True):
    app = make_app(tmp_path)
    client = app.test_client()
    tok = login(client)
    conn = store.connect(app.config["DB_PATH"])
    if with_account:
        connections.create_account(conn, "Основной", "2026-10-10T12:00:00", "owner")
    return app, client, tok


def saved(app):
    return connections.list_addresses(store.connect(app.config["DB_PATH"]), 1)


FORM = {"mode": "percent", "rest": "", "name_0": "Бета", "addr_0": "100000034", "on_0": "on", "share_0": "60",
        "name_1": "Гамма", "addr_1": "100000033", "on_1": "on", "share_1": "40", "name_2": "", "addr_2": "", "share_2": ""}


def test_page_warns_when_no_addresses_were_added(tmp_path):
    app, client, tok = setup(tmp_path)
    html = client.get("/connections/avito/1").get_data(as_text=True)
    assert "Авито адреса" in html and "Основной" in html
    assert "Адреса не добавлены" in html and "общие настройки" in html
    assert "не проверяет" in html


def test_saving_addresses_and_empty_rows_are_ignored(tmp_path):
    app, client, tok = setup(tmp_path)
    r = post(client, "/accounts/1/addresses", tok, **FORM)
    assert r.status_code == 302
    assert [(a["name"], a["address_id"], a["share"]) for a in saved(app)] == [("Бета", "100000034", 60.0), ("Гамма", "100000033", 40.0)]
    html = client.get("/connections/avito/1").get_data(as_text=True)
    assert "Адреса не добавлены" not in html and "100000034" in html


def test_invalid_input_is_explained_and_nothing_is_saved(tmp_path):
    app, client, tok = setup(tmp_path)
    r = post(client, "/accounts/1/addresses", tok, **dict(FORM, share_1="30"))
    assert r.status_code == 400 and "в сумме 100" in r.get_data(as_text=True)
    r = post(client, "/accounts/1/addresses", tok, **dict(FORM, addr_0="abc"))
    assert r.status_code == 400 and "идентификатор" in r.get_data(as_text=True)
    assert saved(app) == []


def test_readiness_and_connections_page_mention_missing_addresses(tmp_path):
    app, client, tok = setup(tmp_path)
    assert "Авито адреса Основной" in client.get("/").get_data(as_text=True)
    assert "адреса не добавлены" in client.get("/").get_data(as_text=True)
    assert client.get("/accounts/1/addresses").headers["Location"].endswith("/connections/avito/1")
    post(client, "/accounts/1/addresses", tok, **FORM)
    assert "адреса не добавлены" not in client.get("/").get_data(as_text=True)


def test_requires_login_and_known_account(tmp_path):
    app, client, tok = setup(tmp_path)
    assert app.test_client().get("/accounts/1/addresses").status_code == 302
    assert client.get("/accounts/9/addresses").status_code == 404
    assert client.post("/accounts/9/addresses", data={"csrf": tok}).status_code == 404


def test_text_address_mode_can_be_saved_from_the_form(tmp_path):
    app, client, tok = setup(tmp_path)
    form = dict(FORM, address_mode="text", text_0="Санкт-Петербург, ул. Генерала Примерова, 35А", text_1="Санкт-Петербург, пр-т Образцова, 21к1")
    assert post(client, "/accounts/1/addresses", tok, **form).status_code == 302
    conn = store.connect(app.config["DB_PATH"])
    assert connections.get_address_mode(conn, 1) == "text"
    assert [a["address_text"] for a in saved(app)][0].startswith("Санкт-Петербург, ул. Генерала")
    r = post(client, "/accounts/1/addresses", tok, **dict(FORM, address_mode="text"))
    assert r.status_code == 400 and "полный адрес" in r.get_data(as_text=True)


def test_brand_rules_can_be_set_and_cleared_from_the_card(tmp_path):
    app, client, tok = setup(tmp_path)
    post(client, "/accounts/1/addresses", tok, **FORM)
    page = client.get("/connections/avito/1").get_data(as_text=True)
    assert "Распределение по брендам" in page and "IKON TYRES · зима" in page
    r = post(client, "/accounts/1/point-rules", tok, pair="IKON TYRES|winter", address_id="100000033", action="set")
    assert r.status_code == 302
    conn = store.connect(app.config["DB_PATH"])
    assert connections.list_point_rules(conn, 1) == [{"brand_key": "IKON TYRES", "season": "winter", "address_id": "100000033"}]
    assert "Гамма" in client.get("/connections/avito/1").get_data(as_text=True)
    post(client, "/accounts/1/point-rules", tok, pair="IKON TYRES|winter", action="clear")
    assert connections.list_point_rules(conn, 1) == []
    post(client, "/accounts/1/point-rules", tok, pair="IKON TYRES|winter", address_id="999", action="set")
    assert connections.list_point_rules(conn, 1) == []
    assert post(client, "/accounts/1/point-rules", tok, pair="bad", action="set").status_code == 400
    assert client.post("/accounts/9/point-rules", data={"csrf": tok}).status_code == 404
