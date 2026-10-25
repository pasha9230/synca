from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from helpers import catalog_xml, settings_dict
from helpers import commodity_xml as _commodity_xml
from http_fakes import ScriptedHttp
from tires_sync import cli, connections, store
from tires_sync.cli import main
from tires_sync.fetcher import fetch_for_connection
from tires_sync.secretbox import SecretBox, SecretKeyError, create_key_file, load_box
from tires_sync.settings_store import init_settings
from tires_sync.webmim_client import WebmimError

NOW = "2026-10-10T12:00:00"
KEY = "C" * 32
TERRITORY = "103261489424"


def item(**kw):
    return _commodity_xml(**dict({"TERRITORY_RN": "103261489424"}, **kw))


def prepared(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(territory_rn=TERRITORY), NOW)
    box = SecretBox(Fernet.generate_key())
    connections.set_config(
        conn, "webmim", 0, {"base_url": "https://mimtest.svrauto.ru", "mode": "login", "login": "user"}, NOW, "owner")
    connections.set_secret(conn, box, "webmim", 0, "password", "pw", NOW, "owner")
    return conn, box


def script():
    return ScriptedHttp(
        (200, {"result": True, "authKey": "A"}), (200, '"%s"' % KEY), (200, {"result": True}),
        (200, catalog_xml(item())))


def test_fetch_uses_the_saved_connection_and_the_active_territory(tmp_path):
    conn, box = prepared(tmp_path)
    http = script()
    meta = fetch_for_connection(conn, box, tmp_path / "unload.xml", NOW, http=http)
    assert meta == {"fetched_at": NOW, "items": 1}
    assert (tmp_path / "unload.xml").exists()
    assert http.calls[2][1].endswith("/api/v3/auth/logout")
    assert "sterritoryid=%s" % TERRITORY in http.calls[3][1]


def test_missing_connection_or_key_is_reported_not_crashed(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(territory_rn=TERRITORY), NOW)
    with pytest.raises(WebmimError):
        fetch_for_connection(conn, SecretBox(Fernet.generate_key()), tmp_path / "u.xml", NOW, http=ScriptedHttp())
    (tmp_path / "x").mkdir()
    conn2, _ = prepared(tmp_path / "x")
    with pytest.raises(SecretKeyError):
        fetch_for_connection(conn2, None, tmp_path / "x" / "u.xml", NOW, http=ScriptedHttp())


def test_cli_fetch_unload_success_and_failure(tmp_path, monkeypatch, capsys):
    conn, _ = prepared(tmp_path)
    key_file = tmp_path / "secret.key"
    create_key_file(key_file)
    connections.set_secret(conn, load_box(key_file), "webmim", 0, "password", "pw", NOW, "owner")
    conn.close()
    base = ["--db", str(tmp_path / "t.db"), "--key-file", str(key_file)]
    monkeypatch.setattr(cli, "_http", lambda: script())
    code = main(["fetch-unload", "--out", str(tmp_path / "unload.xml")] + base)
    assert code == 0 and "items: 1" in capsys.readouterr().out
    monkeypatch.setattr(cli, "_http", lambda: ScriptedHttp((401, "no")))
    code = main(["fetch-unload", "--out", str(tmp_path / "unload2.xml")] + base)
    assert code == 2 and not (tmp_path / "unload2.xml").exists()
    assert "pw" not in capsys.readouterr().err
    code = main(["fetch-unload", "--db", str(tmp_path / "t.db"), "--out", str(tmp_path / "u3.xml"),
                 "--key-file", str(tmp_path / "missing.key")])
    assert code == 2


def test_cli_fetch_unload_reports_a_network_error_instead_of_a_traceback(tmp_path, monkeypatch, capsys):
    conn, _ = prepared(tmp_path)
    key_file = tmp_path / "secret.key"
    create_key_file(key_file)
    connections.set_secret(conn, load_box(key_file), "webmim", 0, "password", "pw", NOW, "owner")
    conn.close()

    def down(*args):
        raise OSError("network is down")

    monkeypatch.setattr(cli, "_http", lambda: down)
    code = main(["fetch-unload", "--db", str(tmp_path / "t.db"), "--out", str(tmp_path / "u.xml"),
                 "--key-file", str(key_file)])
    assert code == 2 and "error:" in capsys.readouterr().err


def test_fetch_command_can_download_a_secondary_supplier(tmp_path, monkeypatch, capsys):
    conn, _ = prepared(tmp_path)
    key_file = tmp_path / "secret.key"
    create_key_file(key_file)
    box = load_box(key_file)
    two = connections.create_supplier(conn, "Второй", NOW, "owner")
    connections.set_config(conn, "webmim", two, {"base_url": "https://b.example", "mode": "apikey", "territory_rn": TERRITORY}, NOW, "owner")
    connections.set_secret(conn, box, "webmim", two, "api_key", "K" * 32, NOW, "owner")
    conn.close()
    http = ScriptedHttp((200, catalog_xml(item())))
    monkeypatch.setattr(cli, "_http", lambda: http)
    out = tmp_path / "u2.xml"
    code = main(["fetch-unload", "--db", str(tmp_path / "t.db"), "--out", str(out), "--key-file", str(key_file),
                 "--supplier", str(two)])
    assert code == 0 and out.exists() and "b.example" in http.calls[0][1] and "sterritoryid=%s" % TERRITORY in http.calls[0][1]
    assert main(["fetch-unload", "--db", str(tmp_path / "t.db"), "--out", str(tmp_path / "u9.xml"),
                 "--key-file", str(key_file), "--supplier", "99"]) == 2
