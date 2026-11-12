"""«Аналитика рекламы» in the panel: the owner and the partner see it (the section «analytics» with the money right); the point, the chats
manager and the observer never; the periods and the sorts; the dash of a ДРР without sales."""
from __future__ import annotations

from datetime import datetime

import pytest

from panel_helpers import login, make_app
from tires_sync import connections, order_points as points, store, users


@pytest.fixture(autouse=True)
def cheap_hashes(monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")


NOW = datetime(2026, 11, 11, 9, 0, 0)


def app_with_data(tmp_path):
    app = make_app(tmp_path, NOW=lambda: NOW)
    conn = store.connect(app.config["DB_PATH"])
    connections.create_account(conn, "Основной", "2026-11-01T10:00:00", "o")
    conn.execute("INSERT INTO avito_ad (account_id, avito_id, ad_id, title, seen_at) VALUES (1, 4242, 'G1', 'Nokian Hakka 205/55 R16', '2026-11-11')")
    conn.execute("INSERT INTO ad_spend_day (account_id, avito_id, day, spend, views, contacts) VALUES (1, 4242, '2026-11-09', 1234.5, 500, 7)")
    conn.execute("INSERT INTO ad_spend_day (account_id, avito_id, day) VALUES (1, 0, '2026-11-09')")
    conn.execute("INSERT INTO ad_bid (account_id, avito_id, taken_at, mode, bid, day_limit, rec_bid, min_bid, max_bid, options) "
                 "VALUES (1, 4242, '2026-11-11T01:20:00', 'manual', 3.5, 300, 5, 1, 30, '[{\"bid\": 9.0, \"info\": {\"maxForecast\": 20}}]')")
    conn.commit()
    return app, conn


def as_user(app, conn, login_name, role, point_id=None):
    users.add_user(conn, login_name, "длинный-пароль-1", "2026-11-01T10:00:00", role=role, point_id=point_id)
    client = app.test_client()
    with client.session_transaction() as s:
        s["user"], s["epoch"] = login_name, store.get_session_epoch(conn, login_name)
    return client


def test_the_owner_sees_the_spending_the_dash_and_the_menu_item(tmp_path):
    app, conn = app_with_data(tmp_path)
    owner = app.test_client()
    login(owner)
    page = owner.get("/economics/ads?p=7").get_data(as_text=True)
    assert "Аналитика рекламы" in page and "Nokian Hakka 205/55 R16" in page and "1 234 ₽" in page
    assert "Расходы загружены за 1 из 7 дн." in page and "2,47&nbsp;₽" in page            # 1234.5 / 500 views
    assert "с 2026-11-04 по 2026-11-10" in page.lower() and 'href="/economics/ads"' in page
    assert "Без объявления" in page
    assert "своя 3,50&nbsp;₽" in page and "рекомендуемая 5,00&nbsp;₽" in page and "maxForecast: 20" in page and "cpxpromo/4242" in page
    assert "лимит 300\u00a0₽ в день" in page and "вариант 9,00&nbsp;₽" in page and "прочитано 11.11.2026 04:20:00" in page
    assert "не прочитано" not in page and "не продвигается" not in page and "продвижения нет" not in page
    for query in ("?p=30&s=drr", "?p=90&s=drr_margin", "?p=13&s=bad", ""):
        assert owner.get("/economics/ads" + query).status_code == 200


def test_the_partner_sees_it(tmp_path):
    app, conn = app_with_data(tmp_path)
    partner = as_user(app, conn, "partner1", "partner")
    response = partner.get("/economics/ads")
    assert response.status_code == 200 and "Nokian Hakka" in response.get_data(as_text=True)


@pytest.mark.parametrize("role", ["viewer", "chats"])
def test_the_others_do_not(tmp_path, role):
    app, conn = app_with_data(tmp_path)
    other = as_user(app, conn, "u" + role, role)
    response = other.get("/economics/ads")
    assert response.status_code == 403 and "Nokian Hakka" not in response.get_data(as_text=True)
    assert 'href="/economics/ads"' not in other.get("/analytics").get_data(as_text=True)


def test_a_point_does_not(tmp_path):
    app, conn = app_with_data(tmp_path)
    point_id = points.save_point(conn, {"name": "Юнона", "manager_name": "А", "channel": "panel"})
    conn.commit()
    response = as_user(app, conn, "upoint", "point", point_id=point_id).get("/economics/ads")
    assert response.status_code in (302, 403) and "Nokian Hakka" not in response.get_data(as_text=True)


def test_an_ad_without_a_price_says_not_read_and_why(tmp_path):
    app, conn = app_with_data(tmp_path)
    for avito_id, title, day in ((5001, "Отказная 185/65 R15", "2026-11-09"), (5002, "Очередная 195/65 R15", "2026-11-09"),
                                 (5003, "Давняя 215/60 R16", "2026-09-01")):
        conn.execute("INSERT INTO avito_ad (account_id, avito_id, ad_id, title, seen_at) VALUES (1, ?, ?, ?, '2026-11-11')", (avito_id, "G%d" % avito_id, title))
        conn.execute("INSERT INTO ad_spend_day (account_id, avito_id, day, spend, presence, views) VALUES (1, ?, ?, 100, 100, 10)", (avito_id, day))
    store.set_state(conn, "ads_bid_fail:1", '{"5001": {"at": "2026-11-11T01:20:00", "why": "ответ 404, код 404: item is not available"},'
                                            ' "4242": {"at": "2026-11-11T03:20:00", "why": "ответ 500"}}')
    conn.commit()
    owner = app.test_client()
    login(owner)
    page = owner.get("/economics/ads?p=90").get_data(as_text=True)
    cell = {name: page.split(name, 1)[1].split("</tr>", 1)[0] for name in ("Отказная", "Очередная", "Давняя", "Nokian Hakka")}
    assert "не прочитано" in cell["Отказная"] and "Авито отказал (11.11.2026 04:20:00): ответ 404, код 404: item is not available" in cell["Отказная"]
    assert "не прочитано" in cell["Очередная"] and "очередь ещё не дошла" in cell["Очередная"]
    assert "не прочитано" in cell["Давняя"] and "нет расходов за последние 30 дн." in cell["Давняя"]
    assert "своя 3,50&nbsp;₽" in cell["Nokian Hakka"] and "не прочитано" not in cell["Nokian Hakka"]            # the old price stays, with the failure
    assert "последнее чтение не удалось (11.11.2026 06:20:00): ответ 500" in cell["Nokian Hakka"]
    store.set_state(conn, "ads_spend_error:1", "2026-11-11T04:20:00: Авито ограничило частоту запросов (429)")
    page = owner.get("/economics/ads?p=90").get_data(as_text=True)
    assert "последний сбор не удался (причина над таблицей)" in page.split("Очередная", 1)[1].split("</tr>", 1)[0]
