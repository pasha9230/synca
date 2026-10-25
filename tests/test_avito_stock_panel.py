"""The page of an Avito account: the switch of the number in stock on Avito (off / the list / all), the codes, the ceiling."""
from __future__ import annotations

from cryptography.fernet import Fernet

from panel_helpers import login, make_app, post
from tires_sync import avito_stock, store
from tires_sync.secretbox import SecretBox


def setup(tmp_path):
    app = make_app(tmp_path, SECRET_BOX=SecretBox(Fernet.generate_key()))
    client = app.test_client()
    tok = login(client)
    post(client, "/connections/accounts", tok, name="Основной")
    return app, client, tok


def saved(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    try:
        return avito_stock.config(conn, 1), conn.execute("SELECT COUNT(*) FROM audit WHERE action = 'avito_stock_config'").fetchone()[0]
    finally:
        conn.close()


def test_the_card_shows_the_switch_off_by_default(tmp_path):
    _, client, _ = setup(tmp_path)
    html = client.get("/connections/avito/1").get_data(as_text=True)
    assert "Количество «в наличии» на Авито" in html and 'action="/connections/accounts/1/stock"' in html
    assert 'name="mode" value="off" checked' in html


def test_the_list_of_one_code_and_the_ceiling_are_saved_and_shown(tmp_path):
    _, client, tok = setup(tmp_path)
    answer = post(client, "/connections/accounts/1/stock", tok, mode="list", codes="123456", cap="20")
    assert answer.status_code == 302
    assert saved(tmp_path) == ({"mode": "list", "codes": ["123456"], "cap": 20}, 1)
    html = client.get("/connections/avito/1").get_data(as_text=True)
    assert 'name="mode" value="list" checked' in html and "123456</textarea>" in html and 'value="20"' in html


def test_a_wrong_form_changes_nothing_and_says_why(tmp_path):
    _, client, tok = setup(tmp_path)
    post(client, "/connections/accounts/1/stock", tok, mode="list", codes="", cap="")
    assert saved(tmp_path) == ({"mode": "off", "codes": [], "cap": None}, 0)
    assert "без кодов" in client.get("/connections/avito/1").get_data(as_text=True)
    post(client, "/connections/accounts/1/stock", tok, mode="all", codes="", cap="ноль")
    assert saved(tmp_path)[0]["mode"] == "off"
    assert client.post("/connections/accounts/99/stock", data={"csrf": tok, "mode": "all"}).status_code == 404
    assert client.post("/connections/accounts/1/stock", data={"mode": "all"}).status_code in (400, 403)      # no csrf


def test_only_the_owner_switches_it_a_partner_or_a_manager_gets_403(tmp_path, monkeypatch):
    from tires_sync import order_points, users
    from tires_sync.panel import common
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")
    assert common.NEEDS["connections_account_stock"] == "publish"                    # a write to Avito: the right to publish, like «live»
    app, _, _ = setup(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    point = order_points.save_point(conn, {"name": "Гамма", "manager_name": "А", "channel": "vk"})
    for role in ("partner", "chats", "point", "viewer"):
        users.add_user(conn, role + "1", "длинный-пароль-1", "2026-10-24T12:00:00", role=role, point_id=point if role == "point" else None)
        client = app.test_client()
        with client.session_transaction() as session:
            session["user"] = role + "1"
            session["epoch"] = store.get_session_epoch(conn, role + "1")
            session["csrf"] = "tok"
        answer = client.post("/connections/accounts/1/stock", data={"csrf": "tok", "mode": "all", "codes": "", "cap": ""})
        assert answer.status_code == 403, role
    conn.close()
    assert saved(tmp_path) == ({"mode": "off", "codes": [], "cap": None}, 0)
