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
    cell = page.split('data-bid="4242"', 1)[1].split("</td>", 1)[0]
    short, more = cell.split("<details", 1)                      # the cell: the price and the limit; the rest unfolds (the owner 2026-11-12)
    assert "своя 3,50\u00a0₽" in short and "лимит 300\u00a0₽ в день" in short and "рекомендуемая" not in short and "вариант" not in short
    assert "рекомендуемая 5,00&nbsp;₽" in more and "9,00&nbsp;₽ — прогноз до 20 просмотров" in more and "cpxpromo/4242" in more
    assert "прочитано 11.11.2026 04:20:00" in more
    assert "Цена просмотра прочитана у 1 из 1 объявлений" in page
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
    assert "<details" not in cell["Отказная"] and 'data-tip="' in cell["Отказная"]                   # one word, the reason in the hint
    assert "не прочитано" in cell["Отказная"] and "Авито отказал (11.11.2026 04:20:00): ответ 404, код 404: item is not available" in cell["Отказная"]
    assert "не прочитано" in cell["Очередная"] and "очередь ещё не дошла" in cell["Очередная"]
    assert "не прочитано" in cell["Давняя"] and "нет расходов за последние 30 дн." in cell["Давняя"]
    assert "своя 3,50\u00a0₽" in cell["Nokian Hakka"] and "не прочитано" not in cell["Nokian Hakka"]            # the old price stays, with the failure
    assert "последнее чтение не удалось (11.11.2026 06:20:00): ответ 500" in cell["Nokian Hakka"]
    store.set_state(conn, "ads_spend_error:1", "2026-11-11T04:20:00: Авито ограничило частоту запросов (429)")
    page = owner.get("/economics/ads?p=90").get_data(as_text=True)
    assert "последний сбор не удался (причина над таблицей)" in page.split("Очередная", 1)[1].split("</tr>", 1)[0]


def two_accounts(tmp_path):
    app, conn = app_with_data(tmp_path)
    connections.create_account(conn, "Шина-Колесо", "2026-11-01T10:00:00", "o")
    conn.execute("INSERT INTO avito_ad (account_id, avito_id, ad_id, title, seen_at) VALUES (2, 777, 'K1', 'Cordiant 175/70 R13', '2026-11-11')")
    conn.execute("INSERT INTO ad_spend_day (account_id, avito_id, day, spend, views, contacts) VALUES (2, 777, '2026-11-09', 100, 50, 2)")
    conn.execute("INSERT INTO ad_spend_day (account_id, avito_id, day, spend, views) VALUES (2, 31337, '2026-11-09', 25, 5)")   # not in the list
    conn.commit()
    owner = app.test_client()
    login(owner)
    return owner, conn


def test_the_tree_shows_the_accounts_the_brands_and_the_ads_with_the_total_on_top(tmp_path):
    """Как в Roistat (the owner 2026-11-12): one tree instead of the summary and the tabs of the accounts; «Итого» in the head."""
    owner, conn = two_accounts(tmp_path)
    page = owner.get("/economics/ads?p=7").get_data(as_text=True)
    table = page.split('id="ads-tree"', 1)[1].split("</table>", 1)[0]
    head, body = table.split("<tbody>", 1)
    assert "Итого" in head and "1\u00a0360\u00a0₽" in head and "ads-total" in head
    assert "Основной" in body and "Шина-Колесо" in body and "Nokian Hakka" in body and "Cordiant" in body
    assert "№31337" in body and "Без данных" in body                                    # no our Id: «Без данных», under its account
    assert 'aria-label="Сортировка"' not in page and 'id="ads-summary"' not in page          # the buttons of the sort are gone
    assert "Объявления не из нашего фида" not in page
    heads = [h.split("</th>", 1)[0] for h in head.split("<th")[2:]]
    order = [h.split(">", 2)[2].split("<", 1)[0].split(" ▼")[0] if "<a " in h else "" for h in heads]
    labels = [o for o in order if o]
    assert labels[:6] == ["Расходы", "Просмотры", "Цена просмотра", "Контакты", "Конверсия в контакт", "Цена контакта"]
    assert "Прибыль" in labels and "ROI" in labels and labels.index("Прибыль") < labels.index("ROI")
    assert "Цена просмотра на Авито" in head and 'data-tip="Маржа − расходы на рекламу.' in head
    assert 'class="ads-neg"' in body                                                    # a loss: the profit in red
    assert owner.get("/economics/ads?a=abc&f=x&g=nonsense").status_code == 200


def test_the_sort_is_a_link_on_the_head_down_first_then_up(tmp_path):
    owner, conn = two_accounts(tmp_path)
    page = owner.get("/economics/ads?p=7").get_data(as_text=True)
    assert 'href="/economics/ads?p=7&amp;g=account,brand,ad&amp;s=views"' in page
    assert 'href="/economics/ads?p=7&amp;g=account,brand,ad&amp;s=spend&amp;d=asc"' in page and "Расходы ▼" in page
    page = owner.get("/economics/ads?p=7&s=views").get_data(as_text=True)
    assert "Просмотры ▼" in page and 'href="/economics/ads?p=7&amp;g=account,brand,ad&amp;s=views&amp;d=asc"' in page
    body = page.split("<tbody>", 1)[1]
    assert body.index("Основной") < body.index("Шина-Колесо")                             # 500 views against 55
    page = owner.get("/economics/ads?p=7&s=views&d=asc").get_data(as_text=True)
    assert "Просмотры ▲" in page and 'href="/economics/ads?p=7&amp;g=account,brand,ad&amp;s=views"' in page
    body = page.split("<tbody>", 1)[1]
    assert body.index("Шина-Колесо") < body.index("Основной")
    assert 'href="/economics/ads?p=30&amp;g=account,brand,ad&amp;s=views&amp;d=asc"' in page          # the period keeps the sort


def test_the_levels_are_kept_in_the_address_and_for_the_person(tmp_path):
    owner, conn = two_accounts(tmp_path)
    page = owner.get("/economics/ads?p=7&l=diameter&l=&l=ad").get_data(as_text=True)
    assert "Группировка: Диаметр → Объявление" in page and 'href="/economics/ads?p=7&amp;g=diameter,ad&amp;s=spend&amp;d=asc"' in page
    page = owner.get("/economics/ads").get_data(as_text=True)                              # the bare address: the levels kept
    assert "Группировка: Диаметр → Объявление" in page
    page = owner.get("/economics/ads?g=brand").get_data(as_text=True)
    assert "Группировка: Бренд</summary>" in page and "Nokian Hakka" not in page.split("<tbody>", 1)[1]      # no level «ad»


def test_the_columns_are_chosen_and_kept_for_the_person(tmp_path):
    owner, conn = two_accounts(tmp_path)
    response = owner.post("/economics/ads/columns", data={"csrf": "tok", "show": ["spend", "roi"], "back": "/economics/ads?p=7&g=brand,ad"})
    assert response.status_code == 302 and response.headers["Location"].endswith("/economics/ads?p=7&g=brand,ad")
    head = owner.get("/economics/ads?p=7").get_data(as_text=True).split('id="ads-tree"', 1)[1].split("<tbody>", 1)[0]
    assert ">Расходы" in head and ">ROI" in head and ">Просмотры" not in head and "Цена просмотра на Авито" not in head
    assert owner.post("/economics/ads/columns", data={"csrf": "tok", "show": ["spend"], "back": "https://example.com/x"}).headers["Location"].endswith("/economics/ads")
    assert owner.post("/economics/ads/columns", data={"show": ["spend"]}).status_code == 400              # no csrf


def test_the_empty_columns_are_hidden_and_the_filters_kept_in_the_links(tmp_path):
    owner, conn = two_accounts(tmp_path)
    conn.execute("DELETE FROM ad_bid")
    conn.commit()
    page = owner.get("/economics/ads?p=7").get_data(as_text=True)
    head = page.split('id="ads-tree"', 1)[1].split("<tbody>", 1)[0]
    assert "Продажи, шт." not in head and "s=margin\"" not in head and "Цена просмотра на Авито" not in head and "Расходы" in head
    assert 'href="/economics/ads?p=7&amp;g=account,brand,ad&amp;s=spend&amp;f=sales"' in page
    page = owner.get("/economics/ads?p=7&f=sales").get_data(as_text=True)
    assert "Cordiant" not in page and "Нет объявлений с продажами" in page
    assert 'href="/economics/ads?p=30&amp;g=account,brand,ad&amp;s=spend&amp;f=sales"' in page


def test_the_change_against_the_previous_period_is_shown_on_the_total_and_the_top_level(tmp_path):
    owner, conn = two_accounts(tmp_path)
    conn.execute("INSERT INTO ad_spend_day (account_id, avito_id, day, spend, views) VALUES (1, 4242, '2026-11-01', 1000, 10)")
    conn.commit()
    page = owner.get("/economics/ads?p=7").get_data(as_text=True)
    head, body = page.split('id="ads-tree"', 1)[1].split("</table>", 1)[0].split("<tbody>", 1)
    assert "▲&nbsp;35,9&nbsp;%" in head and "ads-delta is-bad" in head                     # 1359,5 against 1000: more spending, in red
    rows = body.split("</tr>")
    first = next(r for r in rows if "Основной" in r)
    assert "▲&nbsp;23,4&nbsp;%" in first                                                  # 1234,5 against 1000
    assert all("ads-delta" not in r for r in rows if 'data-depth="1"' in r or 'data-depth="2"' in r)


def test_the_page_stands_on_an_empty_base(tmp_path):
    app = make_app(tmp_path, NOW=lambda: NOW)
    owner = app.test_client()
    login(owner)
    response = owner.get("/economics/ads")
    assert response.status_code == 200 and "Нет аккаунтов с фидом" in response.get_data(as_text=True)


def test_the_columns_of_the_partner_are_kept_and_the_others_may_not_post_them(tmp_path):
    app, conn = app_with_data(tmp_path)
    partner = as_user(app, conn, "partner1", "partner")
    with partner.session_transaction() as s:
        s["csrf"] = "tok"
    assert partner.post("/economics/ads/columns", data={"csrf": "tok", "show": ["spend"]}).status_code == 302
    assert ">Просмотры" not in partner.get("/economics/ads").get_data(as_text=True).split('id="ads-tree"', 1)[1].split("<tbody>", 1)[0]
    viewer = as_user(app, conn, "viewer1", "viewer")
    with viewer.session_transaction() as s:
        s["csrf"] = "tok"
    assert viewer.post("/economics/ads/columns", data={"csrf": "tok", "show": ["spend"]}).status_code == 403
