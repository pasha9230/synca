"""What the client learns about a point (address, hours, Yandex Maps) and how a manager's VK is bound to it by a one-time code."""
from __future__ import annotations

import pytest

from panel_helpers import login, make_app, post
from tires_sync import order_points as points, order_templates as tpl, orders, store

NOW = "2026-10-12T12:00:00"
MAP = "https://yandex.ru/maps/-/CDabc"


def db(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    orders.ensure_pipeline(conn, NOW)
    return conn


def test_the_address_hours_and_map_are_saved_checked_and_kept_when_not_sent(tmp_path):
    conn = db(tmp_path)
    pid = points.save_point(conn, dict(name="Бета", address="  СПб,   Примерова 35А ", hours="10–20", map_url=MAP))
    p = points.get_point(conn, pid)
    assert (p["address"], p["hours"], p["map_url"]) == ("СПб, Примерова 35А", "10–20", MAP)
    points.save_point(conn, dict(name="Бета", manager_name="Алексей"), pid)                  # a caller without these fields keeps them
    assert points.get_point(conn, pid)["map_url"] == MAP and points.get_point(conn, pid)["hours"] == "10–20"
    for good in ("https://yandex.com/maps/org/123", "https://maps.yandex.ru/?ll=30,59", "https://ya.ru/maps/-/x", ""):
        points.save_point(conn, dict(name="Бета", map_url=good), pid)
    for bad in ("http://yandex.ru/maps/-/x", "https://evil.example/yandex.ru", "https://yandex.ru.evil.com/maps", "javascript:alert(1)",
                "https://yandex.ru/maps/ x", "https://яндекс.рф/maps", "https://yandex.ru/" + "a" * 300):
        with pytest.raises(points.PointError):
            points.save_point(conn, dict(name="Бета", map_url=bad), pid)
    with pytest.raises(points.PointError):
        points.save_point(conn, dict(name="Бета", address="а" * 201), pid)


def test_the_points_own_address_comes_before_the_avito_address_in_a_text(tmp_path):
    conn = db(tmp_path)
    pid = points.save_point(conn, dict(name="Бета", hours="10–20", map_url=MAP))
    conn.execute("INSERT INTO avito_address (account_id, position, name, address_id, enabled, share, address_text) VALUES (1, 0, 'Бета', '77', 1, 1, 'Адрес Авито')")
    conn.execute("UPDATE point SET avito_address_id = '77'")
    conn.commit()
    card = orders.get_card(conn, orders.create_card(conn, dict(client_name="Иван", point_id=pid), NOW, "o"))
    assert tpl.render(conn, card, "{point_address}|{point_hours}|{point_map}")[0] == "Адрес Авито|10–20|" + MAP
    points.save_point(conn, dict(name="Бета", address="Свой адрес"), pid)
    card = orders.get_card(conn, card["id"])
    assert tpl.render(conn, card, "{point_address}")[0] == "Свой адрес"


def test_a_code_binds_one_vk_once_and_never_takes_the_vk_of_another_point(tmp_path):
    conn = db(tmp_path)
    a = points.save_point(conn, dict(name="Бета"))
    b = points.save_point(conn, dict(name="Гамма", channel="vk", vk_peer_id="777"))
    code = points.new_bind_code(conn, a)
    assert points.bind_vk(conn, code.lower(), "555")["name"] == "Бета"
    p = points.get_point(conn, a)
    assert (p["vk_peer_id"], p["channel"], p["bind_code"]) == ("555", "vk", "")
    with pytest.raises(points.PointError):
        points.bind_vk(conn, code, "556")                                                        # used up
    with pytest.raises(points.PointError):
        points.bind_vk(conn, points.new_bind_code(conn, a), "777")                               # the VK of Гамма
    for sender in ("", "-5", "abc", "0"):
        with pytest.raises(points.PointError):
            points.bind_vk(conn, points.new_bind_code(conn, a), sender)
    assert points.get_point(conn, b)["vk_peer_id"] == "777" and points.get_point(conn, a)["vk_peer_id"] == "555"


def test_the_points_page_edits_the_address_hours_and_map(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    tok = login(client)
    post(client, "/connections/points/save", tok, name="Бета", channel="vk", address="СПб, Примерова 35А", hours="10–20", map_url=MAP)
    conn = store.connect(app.config["DB_PATH"])
    [p] = points.list_points(conn)
    assert (p["address"], p["hours"], p["map_url"]) == ("СПб, Примерова 35А", "10–20", MAP)
    page = client.get("/connections/points").get_data(as_text=True)
    assert 'name="map_url" value="%s"' % MAP in page and "График работы" in page and "Адрес точки для клиента" in page
    post(client, "/connections/points/save", tok, id=str(p["id"]), name="Бета", map_url="http://bad")
    assert points.get_point(conn, p["id"])["map_url"] == MAP
    post(client, "/connections/points/%d/code" % p["id"], tok)
    assert "отправляет его сообщением сообществу" in client.get("/connections/points").get_data(as_text=True)


def test_one_vk_is_never_given_to_two_points_by_the_form(tmp_path):
    conn = db(tmp_path)
    a = points.save_point(conn, dict(name="Бета", vk_peer_id="555"))
    with pytest.raises(points.PointError):
        points.save_point(conn, dict(name="Гамма", vk_peer_id="vk.com/id555"))
    points.save_point(conn, dict(name="Бета", vk_peer_id="555", hours="10–20"), a)            # its own VK is fine
    assert points.get_point(conn, a)["hours"] == "10–20"
