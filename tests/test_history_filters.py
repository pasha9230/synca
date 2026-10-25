from __future__ import annotations

from panel_helpers import login, make_app
from tires_sync import cli, store

NOW = "2026-10-09T10:00:00"


def page(client, q=""):
    return client.get("/history" + q).get_data(as_text=True)


def seed(app):
    conn = store.connect(app.config["DB_PATH"])
    store.write_audit(conn, NOW, "owner", "mapping_confirm", {"model_raw": "Own Model", "avito_model": "X"})
    store.write_audit(conn, NOW, "worker2tyres", "mapping_confirm", {"model_raw": "Other Person Model", "avito_model": "Y"})
    store.write_audit(conn, NOW, "system", "alert", {"text": "Tires Sync: цикл завершился с ошибками"})
    store.write_audit(conn, NOW, "cli", "mapping_confirm", {"model_raw": "Cli Model", "avito_model": "Z"})
    return conn


def test_journal_filters_all_mine_system_and_errors(tmp_path):
    app = make_app(tmp_path)
    seed(app)
    client = app.test_client()
    login(client)
    everything = page(client)
    assert all(t in everything for t in ("Own Model", "Other Person Model", "цикл завершился с ошибками", "Cli Model"))
    mine = page(client, "?f=mine")
    assert "Own Model" in mine and "Other Person Model" not in mine and "Cli Model" not in mine
    system = page(client, "?f=system")
    assert "Cli Model" in system and "цикл завершился с ошибками" in system and "Own Model" not in system
    errors = page(client, "?f=errors")
    assert "цикл завершился с ошибками" in errors and "Own Model" not in errors and "Cli Model" not in errors
    for label in ("Все", "Мои действия", "Система", "Ошибки"):
        assert label in everything
    assert page(client, "?f=nonsense").count("Own Model") == 1          # an unknown filter shows everything


def test_empty_filter_says_so(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    login(client)
    assert "Ничего нет" in page(client, "?f=errors")


def test_an_alert_is_written_to_the_journal_once_even_without_telegram(tmp_path):
    app = make_app(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    cli._alert(conn, None, "Tires Sync: тестовый сбой")
    cli._alert(conn, None, "Tires Sync: тестовый сбой")                    # same text within the pause: not repeated
    entries = [e for e in store.list_audit(conn, 20) if e["action"] == "alert"]
    assert len(entries) == 1 and entries[0]["author"] == "system" and entries[0]["detail"]["text"] == "Tires Sync: тестовый сбой"
    client = app.test_client()
    login(client)
    assert "тестовый сбой" in page(client, "?f=errors")
