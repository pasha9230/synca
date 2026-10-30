"""Названия (владелец, 2026-10-30): аккаунт Авито везде называется названием кабинета с Авито (иначе имя из таблицы, иначе «ID …»;
номер, где он нужен, рядом), точка имени аккаунта по-прежнему не видит; основной вход владельца (общий пароль) показывается своим
именем, заданным в «Пользователях» (по умолчанию логин)."""
from __future__ import annotations

import pytest

from panel_helpers import login, make_app, post
from tires_sync import cli, connections, orders, store, users

NOW = "2026-10-30T12:00:00"


@pytest.fixture(autouse=True)
def cheap_hashes(monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")


def _account(conn, name="Основной", avito=None, user_id=None):
    a = connections.create_account(conn, name, NOW, "owner")
    if avito or user_id:
        connections.set_avito_identity(conn, a, user_id, avito)
    return a


# --- аккаунты --------------------------------------------------------------------------------------------------------------------

def test_the_account_is_named_by_its_avito_cabinet_then_by_the_table(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    shown = _account(conn, "Основной", "Шины СПб", 80000001)
    plain = _account(conn, "Второй")
    numbered = _account(conn, "Третий", None, 80000003)
    assert connections.display_name(conn, shown) == "Шины СПб"
    assert connections.display_name(conn, plain) == "Второй"
    assert connections.display_name(conn, numbered) == "Третий"                      # the table's name before the bare number


def test_the_number_stays_beside_the_name_in_the_connections_tab_and_the_telegram_header(tmp_path):
    from tires_sync.panel.routes_connections import _tab_label
    conn = store.connect(tmp_path / "t.db")
    a = _account(conn, "Основной", "Шины СПб", 80000001)
    assert _tab_label(conn, a) == "Шины СПб · ID 80000001"
    header = cli._cabinet_header(conn, connections.get_account(conn, a), 3)
    assert "«Шины СПб»" in header and "№%d" % a in header and "профиль Авито 80000001" in header and "Основной" not in header


def test_the_point_never_reads_the_avito_name_of_an_account_either(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    _account(conn, "Основной", "Шины СПб", 80000001)
    text = orders.without_account_names(conn, "Звонок на аккаунт «Шины СПб» и на Основной")
    assert "Шины СПб" not in text and "Основной" not in text


# --- имя основного входа владельца -------------------------------------------------------------------------------------------------

def test_a_person_is_named_by_the_display_name_and_the_main_login_by_its_setting(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    users.add_user(conn, "olya", "длинный-пароль-1", NOW, role="chats", display_name="Оля")
    assert users.person_name(conn, "olya") == "Оля" and users.person_name(conn, "ghost") == "ghost"
    assert users.person_name(conn, "owner", main_login="owner") == "owner"                   # nothing set: the login
    users.set_main_name(conn, "Иван", NOW, "owner")
    assert users.main_name(conn) == "Иван"
    assert users.person_name(conn, "owner", main_login="owner") == "Иван" and users.person_name(conn, "olya", main_login="owner") == "Оля"
    users.set_main_name(conn, "", NOW, "owner")
    assert users.person_name(conn, "owner", main_login="owner") == "owner"


def test_the_owner_sets_the_name_in_users_and_it_is_shown_in_the_order(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    tok = login(client)
    conn = store.connect(app.config["DB_PATH"])
    assert "Имя основного входа" in client.get("/users").get_data(as_text=True)
    assert post(client, "/users/main-name", tok, name="Иван").status_code == 302
    assert users.main_name(conn) == "Иван"
    assert "Имя основного входа: Иван" in client.get("/history").get_data(as_text=True)          # in the history like other settings
    orders.ensure_pipeline(conn, NOW)
    card = orders.create_card(conn, {"client_name": "Клиент", "client_phone": "+79001112233"}, NOW, "owner")
    orders.add_note(conn, card, "Позвонить вечером", NOW, "owner")
    page = client.get("/orders/%d" % card).get_data(as_text=True)
    assert "<b>Иван</b> · Позвонить вечером" in page
    assert ">Иван (владелец)</option>" in page                                              # «Ответственный»


@pytest.mark.parametrize("role", ["partner", "chats", "point"])
def test_only_the_owner_sets_the_main_name(tmp_path, role):
    from test_roles import person
    from tires_sync import order_points as points
    app = make_app(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    pid = points.save_point(conn, {"name": "Альфа", "manager_name": "А", "channel": "panel"}) if role == "point" else None
    client, _ = person(app, role, pid)
    r = client.post("/users/main-name", data={"csrf": "tok", "name": "Чужой"})
    assert r.status_code in (302, 403) and users.main_name(conn) == ""


def test_the_point_reads_the_name_of_the_main_login_as_the_responsible(tmp_path):
    from tires_sync.panel.routes_mypoint import _who
    app = make_app(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    users.set_main_name(conn, "Иван", NOW, "owner")
    with app.test_request_context():
        assert _who(conn, app.config["AUTHOR"]) == "Иван" and _who(conn, "") == "не назначен"
