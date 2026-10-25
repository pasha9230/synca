from __future__ import annotations

from panel_helpers import login, make_app, post
from tires_sync import connections, store


def setup(tmp_path, ads=2):
    feed_root = tmp_path / "feed"
    app = make_app(tmp_path, FEED_ROOT=str(feed_root))
    client = app.test_client()
    tok = login(client)
    conn = store.connect(app.config["DB_PATH"])
    connections.create_account(conn, "Основной", "2026-10-10T12:00:00", "owner")
    token = connections.get_account(conn, 1)["feed_token"]
    folder = feed_root / token / "next"
    folder.mkdir(parents=True)
    (folder / "avito_feed.xml").write_text("<Ads>" + "<Ad/>" * ads + "</Ads>")
    return app, client, tok


def live(app):
    return connections.get_account(store.connect(app.config["DB_PATH"]), 1)["live"]


def test_confirmation_page_shows_what_will_be_published(tmp_path):
    app, client, tok = setup(tmp_path)
    html = client.get("/connections/accounts/1/live").get_data(as_text=True)
    assert "Включить публикацию" in html and "Основной" in html and "объявлений в черновике фида: 2" in html
    assert "Я понимаю" in html and live(app) == 0


def test_enabling_needs_the_explicit_checkbox_and_is_audited(tmp_path):
    app, client, tok = setup(tmp_path)
    post(client, "/connections/accounts/1/live", tok, action="enable")
    assert live(app) == 0
    post(client, "/connections/accounts/1/live", tok, action="enable", understand="on")
    assert live(app) == 1
    audit = store.list_audit(store.connect(app.config["DB_PATH"]), 10)
    assert any(e["action"] == "account_live" and e["detail"]["live"] is True for e in audit)
    assert "включена" in client.get("/connections/avito/1").get_data(as_text=True)


def test_disabling_needs_no_confirmation(tmp_path):
    app, client, tok = setup(tmp_path)
    post(client, "/connections/accounts/1/live", tok, action="enable", understand="on")
    post(client, "/connections/accounts/1/live", tok, action="disable")
    assert live(app) == 0


def test_requires_login_and_known_account(tmp_path):
    app, client, tok = setup(tmp_path)
    assert app.test_client().get("/connections/accounts/1/live").status_code == 302
    assert client.get("/connections/accounts/99/live").status_code == 404
    assert client.post("/connections/accounts/99/live", data={"csrf": tok, "action": "enable"}).status_code == 404
    assert post(client, "/connections/accounts/1/live", tok, action="bogus").status_code == 400


def test_confirmation_page_warns_when_addresses_are_not_added(tmp_path):
    app, client, tok = setup(tmp_path)
    assert "Адреса не добавлены" in client.get("/connections/accounts/1/live").get_data(as_text=True)
    conn = store.connect(app.config["DB_PATH"])
    connections.save_addresses(conn, 1, "percent", None, [{"name": "A", "address_id": "101", "enabled": True, "share": 100}], "2026-10-10T12:00:00", "owner")
    assert "Адреса не добавлены" not in client.get("/connections/accounts/1/live").get_data(as_text=True)
