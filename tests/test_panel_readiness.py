from __future__ import annotations

import json

from panel_helpers import login, make_app
from tires_sync import connections, store

NOW = "2026-10-10T12:00:00"


def home(client):
    return client.get("/").get_data(as_text=True)


def steps(app):
    """Every readiness step, the ready ones too: the home page lists only the steps that are not ready (owner's word 2026-10-21)."""
    from tires_sync.panel import service
    from tires_sync.panel.data import load_merged, load_reference
    from tires_sync.settings_store import active_settings
    conn = store.connect(app.config["DB_PATH"])
    settings = active_settings(conn)
    found = service.readiness(conn, app.config["UNLOAD_PATH"], load_merged(conn, settings, app.config["UNLOAD_PATH"]),
                              load_reference(app.config["REFERENCE_PATH"]), settings)
    return " ".join("%s: %s" % (s["title"], s["detail"]) for s in found), found


def test_readiness_block_lists_every_step(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    login(client)
    html = home(client)
    assert "Готовность к публикации" in html
    text, found = steps(app)
    for title in ("Подключение к WebMIM", "Выгрузка поставщика", "Справочник Авито", "Аккаунт Авито", "Включённые сочетания"):
        assert title in text
        step = next(s for s in found if s["title"] == title)
        assert (title in html) == (step["level"] != "ok")                       # on the home page: only the steps that are not ready


def test_default_state_tells_what_to_do_next(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    login(client)
    html = home(client)
    assert "ещё не проверялось" in html
    assert "время скачивания неизвестно" in html
    assert "похоже на временный справочник" in html
    assert "Аккаунтов Авито пока нет" in html
    assert "Включено сочетаний: 2" in steps(app)[0] and "Включено сочетаний: 2" not in html      # a ready step


def test_ready_state_shows_times_and_counts(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    login(client)
    conn = store.connect(app.config["DB_PATH"])
    connections.record_check(conn, "webmim", 0, True, "ключ получен", NOW)
    connections.create_account(conn, "Основной", NOW, "owner")
    connections.record_check(conn, "avito", 1, True, "ключи верны, профиль автозагрузки доступен", NOW)
    (tmp_path / "unload.xml.meta.json").write_text(json.dumps({"fetched_at": NOW, "items": 4}))
    html = home(client)
    text = steps(app)[0]
    assert "ключ получен" in text and "4 позиции" in text and "скачана 10.10.2026 15:00:00" in text                 # UTC shown in Moscow time (run6c)
    assert "профиль автозагрузки доступен" in text
    assert "Аккаунтов Авито пока нет" not in text and "Аккаунтов Авито пока нет" not in html
    assert "ключ получен" not in html and "профиль автозагрузки доступен" not in html                # ready steps are not listed on the home page


def test_missing_unload_is_flagged_as_a_blocker(tmp_path):
    client = make_app(tmp_path, with_data=False).test_client()
    login(client)
    html = home(client)
    assert "Нет выгрузки поставщика" in html and "Справочник Авито не загружен" in html


def test_failed_webmim_check_is_shown_as_an_error(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    login(client)
    conn = store.connect(app.config["DB_PATH"])
    connections.record_check(conn, "webmim", 0, False, "Вход отклонён (код 401).", NOW)
    assert "Вход отклонён (код 401)." in home(client)


def test_menu_is_grouped_and_links_to_connections(tmp_path):
    client = make_app(tmp_path).test_client()
    login(client)
    html = home(client)
    assert all('class="sb-label">%s<' % group in html for group in ("Продажи", "Товар и фид", "Аналитика", "Настройка"))   # UX 2.11 (run3a)
    assert 'href="/connections"' in html
