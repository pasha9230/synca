"""Directory of points: the manager, the channel, the delivery address at the supplier."""
from __future__ import annotations

import pytest

from tires_sync import order_points as points, orders, store


def db(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    orders.ensure_pipeline(conn, "2026-10-12T12:00:00")
    return conn


def test_a_point_is_saved_listed_and_edited(tmp_path):
    conn = db(tmp_path)
    pid = points.save_point(conn, {"name": "Гамма", "manager_name": "Алексей", "manager_phone": "+7 (900) 000-00-15", "channel": "vk",
                                   "default_time": "до 18:00", "webmim_address_id": "1243"})
    assert [p["name"] for p in points.list_points(conn)] == ["Гамма"]
    points.save_point(conn, {"name": "Гамма", "manager_name": "Сергей", "channel": "telegram"}, pid)
    p = points.get_point(conn, pid)
    assert p["manager_name"] == "Сергей" and p["channel"] == "telegram" and p["webmim_address_id"] == ""


def test_bad_values_and_duplicates_are_refused(tmp_path):
    conn = db(tmp_path)
    points.save_point(conn, {"name": "Гамма"})
    for bad in ({"name": ""}, {"name": "x" * 61}, {"name": "Гамма"}, {"name": "A", "channel": "sms"}, {"name": "B", "manager_phone": "12"},
                {"name": "C", "default_time": "t" * 61}, {"name": "D", "webmim_address_id": "12 34"}, {"name": "E", "manager_name": "m" * 81}):
        with pytest.raises(points.PointError):
            points.save_point(conn, bad)
    with pytest.raises(points.PointError):
        points.get_point(conn, 999)


def test_a_point_used_by_an_order_is_not_deleted_and_a_free_one_is(tmp_path):
    conn = db(tmp_path)
    used, free = points.save_point(conn, {"name": "Гамма"}), points.save_point(conn, {"name": "Бета"})
    stage = orders.list_stages(conn)[0]["id"]
    conn.execute("INSERT INTO order_card (stage_id, point_id, created_at, created_by, entered_at, updated_at) VALUES (?,?, 'n','o','n','n')", (stage, used))
    with pytest.raises(points.PointError):
        points.delete_point(conn, used)
    points.delete_point(conn, free)
    assert [p["name"] for p in points.list_points(conn)] == ["Гамма"]


def test_a_bind_code_is_new_each_time_short_and_free_of_look_alike_characters(tmp_path):
    conn = db(tmp_path)
    pid = points.save_point(conn, {"name": "Гамма"})
    first, second = points.new_bind_code(conn, pid), points.new_bind_code(conn, pid)
    assert first != second and len(first) == 8 and first == first.upper() and not set(first) & set("01OIL")
    assert points.get_point(conn, pid)["bind_code"] == second


def test_saving_a_point_keeps_the_fields_the_form_does_not_show(tmp_path):
    conn = db(tmp_path)
    pid = points.save_point(conn, {"name": "Гамма", "avito_address_id": "A1"})
    points.save_point(conn, {"name": "Гамма", "manager_name": "Сергей"}, pid)                                      # the page form has no Avito address
    assert points.get_point(conn, pid)["avito_address_id"] == "A1"
    points.save_point(conn, {"name": "Гамма", "avito_address_id": ""}, pid)                                         # an explicit empty value clears it
    assert points.get_point(conn, pid)["avito_address_id"] == ""


def test_vk_number_is_taken_from_digits_or_the_page_address_and_kept_when_not_sent(tmp_path):
    conn = db(tmp_path)
    pid = points.save_point(conn, {"name": "Бета", "channel": "vk", "vk_peer_id": "https://vk.ru/id1115909747"})
    assert points.get_point(conn, pid)["vk_peer_id"] == "1115909747"
    points.save_point(conn, {"name": "Бета", "channel": "vk", "vk_peer_id": "1106595129"}, pid)
    assert points.get_point(conn, pid)["vk_peer_id"] == "1106595129"
    points.save_point(conn, {"name": "Бета", "channel": "vk"}, pid)                       # field not sent: the number stays
    assert points.get_point(conn, pid)["vk_peer_id"] == "1106595129"
    points.save_point(conn, {"name": "Бета", "channel": "vk", "vk_peer_id": ""}, pid)     # sent empty: cleared on purpose
    assert points.get_point(conn, pid)["vk_peer_id"] == ""


def test_bad_vk_number_is_refused(tmp_path):
    conn = db(tmp_path)
    for bad in ("abc", "0", "12 34", "vk.com/durov", "https://evil.example/id123", "id123", "١٢٣"):
        with pytest.raises(points.PointError):
            points.save_point(conn, {"name": "X", "vk_peer_id": bad})
    assert points.list_points(conn) == []


def test_the_vk_field_is_on_the_page_and_saves_through_it(tmp_path):
    from datetime import datetime
    from panel_helpers import login, make_app, post
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 10, 15, 9, 0, 0))
    client = app.test_client()
    tok = login(client)
    post(client, "/connections/points/save", tok, name="Гамма", channel="vk", vk_peer_id="vk.ru/id1106193037")
    html = client.get("/connections/points").get_data(as_text=True)
    assert 'name="vk_peer_id" value="1106193037"' in html
    conn = store.connect(app.config["DB_PATH"])
    pid = points.list_points(conn)[0]["id"]
    post(client, "/connections/points/save", tok, id=str(pid), name="Гамма", channel="vk", vk_peer_id="abc")
    assert points.get_point(conn, pid)["vk_peer_id"] == "1106193037"          # a bad number is refused, the stored one stays


def test_the_webmim_addresses_are_shown_on_the_points_page_only_when_asked(tmp_path):
    """The owner 2026-10-23 (№429): the address of a point had to be found by a roundabout way. «Показать адреса WebMIM» reads them (a read only)."""
    from datetime import datetime
    from cryptography.fernet import Fernet
    from panel_helpers import login, make_app, post
    from test_webmim_create import FakeWebmim
    from tires_sync import connections
    from tires_sync.secretbox import SecretBox
    box, fake = SecretBox(Fernet.generate_key()), FakeWebmim()
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 10, 23, 9, 0, 0), SECRET_BOX=box, WEBMIM_HTTP=fake)
    client = app.test_client()
    tok = login(client)
    post(client, "/connections/points/save", tok, name="Гамма", channel="vk")
    html = client.get("/connections/points").get_data(as_text=True)
    assert "Показать адреса WebMIM" in html and fake.calls == [] and 'list="webmim-addresses"' not in html
    html = client.get("/connections/points?addresses=1").get_data(as_text=True)
    assert "API-пользователь WebMIM не настроен" in html and fake.calls == []
    conn = store.connect(app.config["DB_PATH"])
    connections.set_config(conn, "webmim_orders", 0, {"server": "test", "login": "api-bot"}, "2026-10-23T09:00:00", "owner")
    connections.set_secret(conn, box, "webmim_orders", 0, "password", "pw-SECRET-1", "2026-10-23T09:00:00", "owner")
    conn.commit()
    html = client.get("/connections/points?addresses=1").get_data(as_text=True)
    assert "Тестовый сервер (mimtest)" in html and "СПб, Гамма" in html and "СПб, Парнас" in html
    assert '<option value="77">' in html and 'list="webmim-addresses"' in html and "pw-SECRET-1" not in html
    assert {c[0] for c in fake.calls if c[1] != "/api/v3/auth/logout"} == {"GET"}
    fake.fail[("GET", "/api/v2/addresses")] = ["timeout"]
    assert "Адреса WebMIM не прочитаны" in client.get("/connections/points?addresses=1").get_data(as_text=True)


def test_the_webmim_addresses_are_read_only_for_those_who_edit_the_points(tmp_path):
    """Review 2026-10-23: a read of WebMIM is a login there — the partner, who only looks at the settings, does not trigger it by a link."""
    from datetime import datetime
    from cryptography.fernet import Fernet
    from panel_helpers import make_app
    from test_webmim_create import FakeWebmim
    from tires_sync import connections, users
    from tires_sync.secretbox import SecretBox
    box, fake = SecretBox(Fernet.generate_key()), FakeWebmim()
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 10, 23, 9, 0, 0), SECRET_BOX=box, WEBMIM_HTTP=fake)
    conn = store.connect(app.config["DB_PATH"])
    connections.set_config(conn, "webmim_orders", 0, {"server": "test", "login": "api-bot"}, "2026-10-23T09:00:00", "owner")
    connections.set_secret(conn, box, "webmim_orders", 0, "password", "pw-SECRET-1", "2026-10-23T09:00:00", "owner")
    users.add_user(conn, "partner1", "длинный-пароль-1", "2026-10-23T09:00:00", role="partner")
    conn.commit()
    client = app.test_client()
    with client.session_transaction() as session:
        session["user"], session["epoch"], session["csrf"] = "partner1", store.get_session_epoch(conn, "partner1"), "tok"
    page = client.get("/connections/points")
    assert page.status_code == 200 and "Показать адреса WebMIM" not in page.get_data(as_text=True)
    html = client.get("/connections/points?addresses=1").get_data(as_text=True)
    assert "Адреса WebMIM читают те, кто правит точки" in html and fake.calls == []
