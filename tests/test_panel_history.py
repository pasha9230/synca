from tires_sync import store
from tires_sync.settings_store import active_settings, confirm_draft, create_draft, list_versions
from panel_helpers import login, make_app, post


def test_history_lists_versions_and_audit(tmp_path):
    app = make_app(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    confirm_draft(conn, create_draft(conn, {"min_stock": 9}, "2026-10-08T10:00:00"), "2026-10-08T10:00:00", "owner")
    conn.close()
    client = app.test_client()
    login(client)
    page = client.get("/history").get_data(as_text=True)
    assert "Применены настройки" in page and "draft 1" in page
    assert [v["id"] for v in list_versions(store.connect(app.config["DB_PATH"]))] == [2, 1]


def test_rollback_requires_confirmation_checkbox_and_creates_a_new_version(tmp_path):
    app = make_app(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    confirm_draft(conn, create_draft(conn, {"min_stock": 9}, "2026-10-08T10:00:00"), "2026-10-08T10:00:00", "owner")
    conn.close()
    client = app.test_client()
    tok = login(client)
    post(client, "/history/rollback/1", tok)
    assert active_settings(store.connect(app.config["DB_PATH"])).min_stock == 9
    post(client, "/history/rollback/1", tok, confirm="on")
    assert active_settings(store.connect(app.config["DB_PATH"])).min_stock == 4


def test_rollback_of_unknown_version_shows_a_message(tmp_path):
    client = make_app(tmp_path).test_client()
    tok = login(client)
    r = post(client, "/history/rollback/99", tok, confirm="on")
    assert "does not exist" in client.get(r.headers["Location"]).get_data(as_text=True)
