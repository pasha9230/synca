from __future__ import annotations

import json

from cryptography.fernet import Fernet

from helpers import catalog_xml, commodity_xml, settings_dict
from http_fakes import ScriptedHttp
from panel_helpers import login, make_app, post
from tires_sync import cli, connections, store
from tires_sync.cli import main
from tires_sync.notify import notifier_for
from tires_sync.secretbox import SecretBox, create_key_file, load_box
from tires_sync.settings_store import init_settings

NOW = "2026-10-10T12:00:00"
BOT = "123456:ABCdef-SECRET"
OK = (200, {"ok": True, "result": {}})


def test_notifier_is_none_until_chat_and_token_are_saved(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    box = SecretBox(Fernet.generate_key())
    assert notifier_for(conn, box) is None
    connections.set_config(conn, "telegram", 0, {"chat_id": "42"}, NOW, "owner")
    assert notifier_for(conn, box) is None
    connections.set_secret(conn, box, "telegram", 0, "bot_token", BOT, NOW, "owner")
    http = ScriptedHttp(OK)
    notifier_for(conn, box, http=http)("тест")
    assert BOT in http.calls[0][1]
    assert notifier_for(conn, None) is None


def test_panel_saves_telegram_settings_and_never_shows_the_token(tmp_path):
    http = ScriptedHttp(OK)
    app = make_app(tmp_path, SECRET_BOX=SecretBox(Fernet.generate_key()), TELEGRAM_HTTP=http)
    client = app.test_client()
    tok = login(client)
    post(client, "/connections/telegram", tok, chat_id="42", bot_token=BOT)
    html = client.get("/connections/telegram").get_data(as_text=True)
    assert "Telegram" in html and "42" in html and BOT not in html and "SECRET" not in html
    post(client, "/connections/telegram/test", tok)
    assert len(http.calls) == 1
    assert "ok: " in client.get("/connections/telegram").get_data(as_text=True)
    post(client, "/connections/telegram/test", tok)  # frozen clock: rate limited
    assert len(http.calls) == 1


def test_panel_telegram_check_reports_failures_without_the_token(tmp_path):
    http = ScriptedHttp((401, {"ok": False, "description": "Unauthorized %s" % BOT}))
    app = make_app(tmp_path, SECRET_BOX=SecretBox(Fernet.generate_key()), TELEGRAM_HTTP=http)
    client = app.test_client()
    tok = login(client)
    post(client, "/connections/telegram", tok, chat_id="42", bot_token=BOT)
    post(client, "/connections/telegram/test", tok)
    html = client.get("/connections/telegram").get_data(as_text=True)
    assert "error: " in html and BOT not in html


def cycle_env(tmp_path, with_telegram=True):
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(territory_rn="103261489424"), NOW)
    connections.create_account(conn, "Основной", NOW, "owner")
    key_file = tmp_path / "secret.key"
    create_key_file(key_file)
    if with_telegram:
        box = load_box(key_file)
        connections.set_config(conn, "telegram", 0, {"chat_id": "42"}, NOW, "owner")
        connections.set_secret(conn, box, "telegram", 0, "bot_token", BOT, NOW, "owner")
    conn.close()
    (tmp_path / "unload.xml").write_bytes(catalog_xml(commodity_xml(SMODEL="Unknown Model", TERRITORY_RN="103261489424")))
    (tmp_path / "reference.txt").write_text('<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>', encoding="utf-8")
    return ["--db", str(tmp_path / "t.db"), "--unload", str(tmp_path / "unload.xml"),
            "--reference", str(tmp_path / "reference.txt"), "--feed-root", str(tmp_path / "feed"),
            "--key-file", str(key_file)]


def test_failed_account_in_run_cycle_sends_an_alert(tmp_path, monkeypatch):
    args = cycle_env(tmp_path)
    http = ScriptedHttp(OK)
    monkeypatch.setattr(cli, "_http", lambda: http)
    assert main(["run-cycle"] + args) == 3
    assert len(http.calls) == 1
    assert "Основной" in json.loads(http.calls[0][3])["text"]


def test_run_cycle_without_telegram_just_works(tmp_path, monkeypatch):
    args = cycle_env(tmp_path, with_telegram=False)
    http = ScriptedHttp()
    monkeypatch.setattr(cli, "_http", lambda: http)
    assert main(["run-cycle"] + args) == 3 and http.calls == []


def test_a_failing_alert_never_breaks_the_cycle(tmp_path, monkeypatch, capsys):
    args = cycle_env(tmp_path)
    monkeypatch.setattr(cli, "_http", lambda: ScriptedHttp((500, "down")))
    assert main(["run-cycle"] + args) == 3
    assert "не удалось отправить" in capsys.readouterr().err


def test_a_failed_fetch_also_sends_an_alert(tmp_path, monkeypatch):
    args = cycle_env(tmp_path)
    conn = store.connect(tmp_path / "t.db")
    box = load_box(tmp_path / "secret.key")
    connections.set_config(conn, "webmim", 0, {"base_url": "https://mimtest.svrauto.ru", "mode": "login", "login": "u"}, NOW, "owner")
    connections.set_secret(conn, box, "webmim", 0, "password", "pw", NOW, "owner")
    conn.close()
    http = ScriptedHttp((401, "no"), OK)
    monkeypatch.setattr(cli, "_http", lambda: http)
    assert main(["run-cycle", "--fetch"] + args) == 2
    assert len(http.calls) == 2 and "api.telegram.org" in http.calls[1][1]


def test_alert_command_sends_a_message_through_the_configured_bot(tmp_path, monkeypatch, capsys):
    from tires_sync import cli, connections, store
    from tires_sync.secretbox import create_key_file, load_box
    from http_fakes import ScriptedHttp
    conn = store.connect(tmp_path / "t.db")
    key_file = tmp_path / "secret.key"
    create_key_file(key_file)
    box = load_box(key_file)
    connections.set_config(conn, "telegram", 0, {"chat_id": "42"}, "2026-10-10T12:00:00", "owner")
    connections.set_secret(conn, box, "telegram", 0, "bot_token", "123456789:AAH-abcdefghijklmnopqrstuvwxyz012345", "2026-10-10T12:00:00", "owner")
    conn.close()
    http = ScriptedHttp((200, {"ok": True}))
    monkeypatch.setattr(cli, "_http", lambda: http)
    code = cli.main(["alert", "--db", str(tmp_path / "t.db"), "--key-file", str(key_file), "--message", "Сбой службы backup"])
    assert code == 0 and len(http.calls) == 1
    import json as _json
    assert _json.loads(http.calls[0][3])["text"] == "Сбой службы backup"


def test_alert_command_without_a_configured_bot_is_quiet_and_succeeds(tmp_path, capsys):
    from tires_sync import cli, store
    store.connect(tmp_path / "t.db").close()
    assert cli.main(["alert", "--db", str(tmp_path / "t.db"), "--key-file", "", "--message", "x"]) == 0
