from __future__ import annotations

from panel_helpers import login, make_app, post
from tires_sync import connections, store
from tires_sync.description import ALLOWED, DEFAULT_TEMPLATE


def setup(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    tok = login(client)
    conn = store.connect(app.config["DB_PATH"])
    connections.create_account(conn, "Основной", "2026-10-10T12:00:00", "owner")
    return app, client, tok


def page(client):
    return client.get("/accounts/1/description").get_data(as_text=True)


def test_page_requires_login_and_known_account(tmp_path):
    app, client, tok = setup(tmp_path)
    assert app.test_client().get("/accounts/1/description").status_code == 302
    assert client.get("/accounts/99/description").status_code == 404
    assert client.post("/accounts/99/description", data={"csrf": tok, "action": "save", "template": "x"}).status_code == 404


def test_page_shows_the_default_template_placeholders_and_a_preview(tmp_path):
    app, client, tok = setup(tmp_path)
    html = page(client)
    assert "Шаблон описания" in html and "Основной" in html
    assert "<textarea" in html and "НОВЫЕ ШИНЫ {brand_full}" in html
    for name in ALLOWED:
        assert "{%s}" % name in html
    assert "Предпросмотр" in html and "Цена:" in html and "руб. за 1 шт." in html


def test_saving_a_valid_template_stores_it_and_updates_the_preview(tmp_path):
    app, client, tok = setup(tmp_path)
    r = post(client, "/accounts/1/description", tok, action="save", template="Артикул {code}, цена {price}")
    assert r.status_code == 302
    conn = store.connect(app.config["DB_PATH"])
    assert connections.get_account(conn, 1)["description_template"] == "Артикул {code}, цена {price}"
    html = page(client)
    assert "Артикул {code}, цена {price}" in html and "Шаблон сохранён" in html
    assert "Артикул TS1" in html


def test_invalid_template_is_not_saved_and_keeps_the_text_the_user_typed(tmp_path):
    app, client, tok = setup(tmp_path)
    r = post(client, "/accounts/1/description", tok, action="save", template="Привет {nope}")
    assert r.status_code == 400
    html = r.get_data(as_text=True)
    assert "Неизвестная подстановка {nope}" in html and "Привет {nope}" in html
    conn = store.connect(app.config["DB_PATH"])
    assert connections.get_account(conn, 1)["description_template"] is None


def test_preview_renders_without_saving(tmp_path):
    app, client, tok = setup(tmp_path)
    r = post(client, "/accounts/1/description", tok, action="preview", template="Проба {code}")
    assert r.status_code == 200
    assert "Проба TS1" in r.get_data(as_text=True)
    conn = store.connect(app.config["DB_PATH"])
    assert connections.get_account(conn, 1)["description_template"] is None


def test_reset_returns_to_the_default(tmp_path):
    app, client, tok = setup(tmp_path)
    post(client, "/accounts/1/description", tok, action="save", template="Свой {code}")
    post(client, "/accounts/1/description", tok, action="reset", template="")
    conn = store.connect(app.config["DB_PATH"])
    assert connections.get_account(conn, 1)["description_template"] is None
    assert DEFAULT_TEMPLATE.splitlines()[0] in page(client)


def test_connections_page_links_to_the_template_editor(tmp_path):
    app, client, tok = setup(tmp_path)
    html = client.get("/connections/avito/1").get_data(as_text=True)
    assert 'href="/accounts/1/description"' in html
