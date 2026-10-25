from __future__ import annotations

import re

import pytest

from panel_helpers import login, make_app, post
from tires_sync import connections, staging, store

T = "2026-10-07T10:00:00"


def render(app, source, **ctx):
    with app.test_request_context("/"):
        return app.jinja_env.from_string(source).render(**ctx)


CASES = [
    ({"scode": "feed", "pending": False, "reason": "в фиде"}, "st-ico--ok", "В фиде"),
    ({"scode": "manual_on", "pending": False, "reason": "добавлено вручную"}, "st-ico--brand", "Добавлено вручную"),
    ({"scode": "manual_off", "pending": False, "reason": "снято вами"}, "st-ico--err", "Снято вами"),
    ({"scode": "low_stock", "pending": False, "reason": "остаток ниже порога"}, "st-ico--neutral", "Остаток ниже порога"),
    ({"scode": "model_needs_mapping", "pending": False, "reason": "не сопоставлена модель"}, "st-ico--warn", "Не сопоставлена модель"),
    ({"scode": "not_enabled", "pending": False, "reason": "сочетание не включено"}, "st-ico--neutral", "Сочетание не включено"),
    ({"scode": "no_data", "pending": False, "reason": "нет данных"}, "st-ico--neutral", "Нет данных"),
    ({"scode": "feed", "pending": True, "reason": "в фиде"}, "st-ico--pend", "Станет: в фиде"),
]


@pytest.mark.parametrize("item,cls,title", CASES)
def test_status_icon_has_class_title_and_readable_text(tmp_path, item, cls, title):
    app = make_app(tmp_path)
    html = render(app, '{% from "_ui.html" import status_icon %}{{ status_icon(i) }}', i=item)
    assert cls in html and 'data-tip-title="%s"' % title in html
    assert '<span class="sr-only">%s</span>' % item["reason"] in html


@pytest.mark.parametrize("kind,title", [("summer", "Лето"), ("winter", "Зима"), ("winter_studded", "Зима · шипы"), ("all", "Всесезонные")])
def test_season_macro_has_icon_and_text(tmp_path, kind, title):
    app = make_app(tmp_path)
    html = render(app, '{% from "_ui.html" import season %}{{ season(k) }}', k=kind)
    assert 'title="%s"' % title in html and '<span class="sr-only">%s</span>' % title in html


def test_icon_macro_escapes_nothing_unsafe_and_unknown_names_do_not_crash(tmp_path):
    app = make_app(tmp_path)
    html = render(app, '{% from "_ui.html" import icon %}{{ icon("check") }}|{{ icon("no-such-icon") }}')
    assert html.startswith("<svg")


def test_every_page_renders_logged_in_and_requires_login(tmp_path):
    app = make_app(tmp_path)
    anon = app.test_client()
    client = app.test_client()
    login(client)
    conn = store.connect(app.config["DB_PATH"])
    connections.create_account(conn, "A", T, "owner")
    staging.set_override(conn, "1", "off", T)
    urls = ["/", "/catalog", "/catalog?sup=1&s=winter&t=studded&st=nopair&p=2", "/new", "/new?days=7", "/mappings",
            "/history", "/settings", "/settings/brands", "/settings/exclusions", "/connections/webmim", "/connections/webmim/1", "/connections/telegram",
            "/connections/avito", "/connections/avito/1", "/connections/accounts/1/live",
            "/catalog/table?q=ikon", "/catalog/review"]
    for url in urls:
        assert anon.get(url).status_code in (302, 308), url
        r = client.get(url, follow_redirects=True)
        assert r.status_code == 200, url
        html = r.get_data(as_text=True)
        assert "Traceback" not in html and "{{" not in html and "{%" not in html, url
        if "<html" in html:
            assert html.count("<form") == html.count("</form>"), url
            assert html.count("<section") == html.count("</section>"), url


def test_every_html_page_has_a_nonempty_title_and_sidebar_marks_the_section(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    login(client)
    for url, label in (("/catalog", "Каталог"), ("/mappings", "Сопоставления"), ("/history", "История"), ("/settings", "Настройки")):
        html = client.get(url).get_data(as_text=True)
        assert re.search(r"<title>[^<]+</title>", html)
        assert re.search(r'sb-link is-active"[^>]*>(?:<svg.*?</svg>)?%s' % label, html, re.S), url


def test_history_has_the_rollback_modal_with_both_checkmarks(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    login(client)
    hist = client.get("/history").get_data(as_text=True)
    assert 'id="rollback"' in hist and 'id="rb1"' in hist and 'id="rb2"' in hist and 'data-requires="rb1"' in hist


def test_login_page_has_no_menu_and_wrong_password_is_reported(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    html = client.get("/login").get_data(as_text=True)
    assert "sb-link" not in html and 'type="password"' in html
    with client.session_transaction() as session:
        session["csrf"] = "tok"
    r = client.post("/login", data={"password": "bad", "csrf": "tok"})
    assert r.status_code == 401 and "Неверный логин или пароль" in r.get_data(as_text=True)


def test_error_page_template_renders_the_text_with_menu(tmp_path):
    app = make_app(tmp_path)
    with app.test_request_context("/"):
        from flask import render_template, session
        session["user"] = "owner"
        html = render_template("message.html", text="Ключ недоступен")
    assert "Ключ недоступен" in html and "<h3>Не получилось</h3>" in html


def test_dates_are_shown_by_one_rule_in_moscow_time(tmp_path):
    """UX 4.1 (2026-10-21): «20.10.2026 12:00:05» in journals, Moscow time; the record of the panel's own action is written in UTC."""
    app = make_app(tmp_path)
    client = app.test_client()
    login(client)
    conn = store.connect(app.config["DB_PATH"])
    store.write_audit(conn, "2026-10-07T15:46:50", "owner", "account_created", {})
    html = client.get("/history").get_data(as_text=True)
    assert "07.10.2026 18:46:50" in html and "2026-10-07T15:46:50" not in html


@pytest.mark.parametrize("raw,shown", [("2026-10-07T15:46:50", "07.10.2026 15:46:50"), ("2026-10-07 15:46:50", "07.10.2026 15:46:50"),
                                       ("2026-10-07T15:46:50.123456", "07.10.2026 15:46:50"), ("", "—"), (None, "—"), ("вчера", "вчера")])
def test_dt_filter(tmp_path, raw, shown):
    app = make_app(tmp_path)
    assert render(app, "{{ v|dt }}", v=raw) == shown


def test_every_panel_page_has_its_own_favicon_and_the_file_is_served(tmp_path):
    from panel_helpers import login, make_app
    app = make_app(tmp_path)
    client = app.test_client()
    html = client.get("/login").get_data(as_text=True)
    assert 'rel="icon"' in html and "/static/favicon.svg" in html
    resp = client.get("/static/favicon.svg")
    assert resp.status_code == 200 and b"<svg" in resp.get_data()
