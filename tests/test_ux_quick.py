from __future__ import annotations

from panel_helpers import login, make_app, post
from tires_sync import connections, staging, store
from tires_sync.panel import texts

T = "2026-10-07T10:00:00"


def test_audit_lines_are_plain_russian_without_secrets():
    assert texts.audit_text({"action": "account_live", "detail": {"account_id": 1, "live": True}}) == "Публикация аккаунта 1 включена"
    assert texts.audit_text({"action": "account_live", "detail": {"account_id": 2, "live": False}}) == "Публикация аккаунта 2 выключена"
    assert texts.audit_text({"action": "mapping_confirm", "detail": {"brand": "Ikon Tyres", "model_raw": "X", "avito_model": "Y"}}) == "Сопоставлена модель X → Y"
    assert texts.audit_text({"action": "settings_confirm", "detail": {"draft": 3, "to": 5}}) == "Применены настройки: черновик №3, версия 5"
    assert texts.audit_text({"action": "secret_set", "detail": {"name": "api_key", "value": "TOPSECRET"}}) == "Сохранён секрет: api_key"
    assert texts.audit_text({"action": "unknown_thing", "detail": {}}) == "unknown_thing"


def test_run_keys_and_draft_status_have_russian_words():
    assert texts.run_text("publish") == "к публикации" and texts.run_text("defer") == "ждут очереди"
    assert texts.run_text("weird") == "weird"
    assert texts.draft_status("open") == "Открыт" and texts.draft_status("confirmed") == "Применён"


def test_home_and_history_show_plain_words_not_codes(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    tok = login(client)
    conn = store.connect(app.config["DB_PATH"])
    connections.create_account(conn, "A", T, "owner")
    connections.set_live(conn, 1, True, T, "owner")
    assert "account_live" not in client.get("/").get_data(as_text=True)       # the journal left the home page for «История» (2026-10-21)
    html = client.get("/history").get_data(as_text=True)
    assert "account_live" not in html and "Публикация аккаунта 1 включена" in html
    staging.set_override(conn, "1", "off", T)
    draft = client.get("/catalog/review").headers["Location"]
    review = client.get(draft).get_data(as_text=True)
    assert ">Открыт<" in review and ">open<" not in review


def test_catalog_has_text_next_to_season_icons_and_accessible_status(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    login(client)
    html = client.get("/catalog").get_data(as_text=True)
    assert '<span class="sr-only">Зима · шипы</span>' in html or '<span class="sr-only">Зима</span>' in html
    assert 'aria-live="polite"' in client.get("/").get_data(as_text=True)
    assert "focus-visible" in client.get("/static/panel.css").get_data(as_text=True)


def test_account_tab_shows_the_cabinet_name_next_to_the_id(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    login(client)
    conn = store.connect(app.config["DB_PATH"])
    a = connections.create_account(conn, "A", T, "owner")
    connections.set_avito_identity(conn, a, 80000001, "Шины СПб")
    html = client.get("/connections/avito/1").get_data(as_text=True)
    assert 'class="is-active">Шины СПб · ID 80000001<' in html


def test_catalog_help_is_collapsible_and_keeps_the_example_visible(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    login(client)
    connections.create_account(store.connect(app.config["DB_PATH"]), "A", T, "owner")
    html = client.get("/connections/avito/1").get_data(as_text=True)
    assert html.count('<details class="collapse">') == 2 and "Как это работает" in html
    assert "40 на Север" in html.split("<details")[0] or "Север 40" in html


def test_review_form_guards_against_double_submit(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    login(client)
    conn = store.connect(app.config["DB_PATH"])
    staging.set_override(conn, "1", "off", T)
    draft = client.get("/catalog/review").headers["Location"]
    html = client.get(draft).get_data(as_text=True)
    assert 'data-busy="Применяю…"' in html


def test_review_screen_puts_the_outcome_first_and_flags_a_large_removal(tmp_path):
    from tires_sync.settings_store import confirm_draft, create_draft
    app = make_app(tmp_path)
    client = app.test_client()
    login(client)
    conn = store.connect(app.config["DB_PATH"])
    confirm_draft(conn, create_draft(conn, {"guard_min": 0, "guard_fraction": 0.0}, T), T, "owner")
    staging.set_brand_enabled(conn, "IKON TYRES", False, T)
    html = client.get(client.get("/catalog/review").headers["Location"]).get_data(as_text=True)
    assert html.index("К чему это приведёт") < html.index("Что меняется")
    assert 'class="summary summary--red"' in html
    assert html.index("Технические детали") > html.index("Что меняется")


def test_actions_that_apply_immediately_say_so(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    login(client)
    connections.create_account(store.connect(app.config["DB_PATH"]), "A", T, "owner")
    assert "Сохраняется сразу" in client.get("/mappings").get_data(as_text=True)
    assert "сохраняются сразу" in client.get("/connections/avito/1").get_data(as_text=True)
