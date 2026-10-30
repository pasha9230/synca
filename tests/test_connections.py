from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet

from tires_sync import connections, store
from tires_sync.secretbox import SecretBox, SecretKeyError

NOW = "2026-10-10T12:00:00"


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "t.db")
    yield c
    c.close()


@pytest.fixture
def box():
    return SecretBox(Fernet.generate_key())


def test_accounts_get_unique_unguessable_feed_tokens_and_are_audited(conn):
    a = connections.create_account(conn, "  Основной  ", NOW, "owner")
    b = connections.create_account(conn, "Второй", NOW, "owner")
    accounts = connections.list_accounts(conn)
    assert [x["name"] for x in accounts] == ["Основной", "Второй"]
    assert a != b and accounts[0]["id"] == a
    assert len(accounts[0]["feed_token"]) == 32 and accounts[0]["feed_token"] != accounts[1]["feed_token"]
    assert accounts[0]["live"] == 0
    assert connections.get_account(conn, b)["name"] == "Второй"
    assert connections.get_account(conn, 999) is None
    assert any(e["action"] == "account_created" for e in store.list_audit(conn, 10))


@pytest.mark.parametrize("name", ["", "   ", "x" * 61])
def test_account_name_is_validated(conn, name):
    with pytest.raises(ValueError):
        connections.create_account(conn, name, NOW, "owner")
    assert connections.list_accounts(conn) == []


def test_config_roundtrip_updates_in_place_and_keeps_the_last_check(conn):
    assert connections.get_config(conn, "webmim", 0) == {}
    connections.set_config(conn, "webmim", 0, {"base_url": "https://a.example", "mode": "login"}, NOW, "owner")
    connections.record_check(conn, "webmim", 0, True, "ключ получен", NOW)
    connections.set_config(conn, "webmim", 0, {"base_url": "https://b.example", "mode": "apikey"}, NOW, "owner")
    assert connections.get_config(conn, "webmim", 0) == {"base_url": "https://b.example", "mode": "apikey"}
    assert connections.get_check(conn, "webmim", 0)["status"] == "ok: ключ получен"


def test_secret_is_encrypted_masked_audited_without_value_and_overwritten(conn, box):
    connections.set_secret(conn, box, "avito", 1, "client_id", "ABCDEFGH1234", NOW, "owner")
    connections.set_secret(conn, box, "avito", 1, "client_secret", "TOPSECRETVALUE", NOW, "owner")
    stored = [r[0] for r in conn.execute("SELECT ciphertext FROM secret")]
    assert all("ABCDEFGH" not in t and "TOPSECRET" not in t for t in stored)
    status = connections.secret_status(conn, box, "avito", 1)
    assert status["client_id"]["mask"] == "••••1234" and status["client_secret"]["mask"] == ""
    assert status["client_id"]["readable"] is True
    assert connections.get_secret(conn, box, "avito", 1, "client_secret") == "TOPSECRETVALUE"
    connections.set_secret(conn, box, "avito", 1, "client_secret", "NEWVALUE", NOW, "owner")
    assert connections.get_secret(conn, box, "avito", 1, "client_secret") == "NEWVALUE"
    audit = json.dumps(store.list_audit(conn, 20), ensure_ascii=False)
    assert "secret_set" in audit and "TOPSECRET" not in audit and "ABCDEFGH" not in audit and "NEWVALUE" not in audit
    assert connections.get_secret(conn, box, "avito", 2, "client_id") is None


@pytest.mark.parametrize("value", ["", "   ", "\n\t"])
def test_empty_secret_is_rejected_and_keeps_the_old_one(conn, box, value):
    connections.set_secret(conn, box, "webmim", 0, "password", "keepme", NOW, "owner")
    with pytest.raises(ValueError):
        connections.set_secret(conn, box, "webmim", 0, "password", value, NOW, "owner")
    assert connections.get_secret(conn, box, "webmim", 0, "password") == "keepme"


def test_without_a_master_key_nothing_is_stored_or_read(conn, box):
    with pytest.raises(SecretKeyError):
        connections.set_secret(conn, None, "webmim", 0, "password", "x", NOW, "owner")
    assert conn.execute("SELECT COUNT(*) FROM secret").fetchone()[0] == 0
    connections.set_secret(conn, box, "webmim", 0, "password", "x", NOW, "owner")
    with pytest.raises(SecretKeyError):
        connections.get_secret(conn, None, "webmim", 0, "password")
    assert connections.secret_status(conn, None, "webmim", 0)["password"]["readable"] is False


def test_secret_encrypted_with_another_key_is_reported_unreadable(conn, box):
    connections.set_secret(conn, box, "avito", 1, "client_id", "ABCDEFGH1234", NOW, "owner")
    other = SecretBox(Fernet.generate_key())
    with pytest.raises(SecretKeyError):
        connections.get_secret(conn, other, "avito", 1, "client_id")
    status = connections.secret_status(conn, other, "avito", 1)["client_id"]
    assert status["readable"] is False and status["mask"] == ""


def test_check_status_and_rate_limit(conn):
    assert connections.check_allowed(conn, "avito", 1, "2026-10-10T12:00:00") is True
    connections.record_check(conn, "avito", 1, False, "x" * 500, "2026-10-10T12:00:00")
    check = connections.get_check(conn, "avito", 1)
    assert check["status"].startswith("error: ") and len(check["status"]) <= 207
    assert connections.check_allowed(conn, "avito", 1, "2026-10-10T12:00:05") is False
    assert connections.check_allowed(conn, "avito", 1, "2026-10-10T12:00:11") is True
    assert connections.check_allowed(conn, "avito", 2, "2026-10-10T12:00:05") is True
    assert connections.get_check(conn, "avito", 2) is None


def test_description_template_is_stored_per_account_audited_and_resettable(conn):
    a = connections.create_account(conn, "Основной", NOW, "owner")
    b = connections.create_account(conn, "Второй", NOW, "owner")
    assert connections.get_account(conn, a)["description_template"] is None
    connections.set_description_template(conn, a, "Цена {price}", NOW, "owner")
    assert connections.get_account(conn, a)["description_template"] == "Цена {price}"
    assert connections.get_account(conn, b)["description_template"] is None
    assert [x["description_template"] for x in connections.list_accounts(conn)] == ["Цена {price}", None]
    connections.set_description_template(conn, a, None, NOW, "owner")
    assert connections.get_account(conn, a)["description_template"] is None
    audit = json.dumps(store.list_audit(conn, 10), ensure_ascii=False)
    assert "description_template_set" in audit and "Цена {price}" not in audit


def test_existing_database_gets_the_template_column(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    old = sqlite3.connect(str(path))
    old.execute("CREATE TABLE account (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, "
                "feed_token TEXT NOT NULL UNIQUE, live INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL)")
    old.execute("INSERT INTO account (name, feed_token, live, created) VALUES ('Основной', 'abc', 0, 'x')")
    old.commit()
    old.close()
    upgraded = store.connect(path)
    assert connections.get_account(upgraded, 1)["description_template"] is None
    store.connect(path).close()  # a second start must not fail


def test_avito_identity_is_stored_per_account_and_drives_the_display_name(tmp_path):
    from tires_sync import connections, store
    conn = store.connect(tmp_path / "t.db")
    a = connections.create_account(conn, "Основной", "2026-10-10T12:00:00", "owner")
    assert connections.get_avito_identity(conn, a) == {"id": None, "name": None, "phone": "", "phone_source": "cabinet", "phones": [], "pinned": False}
    assert connections.display_name(conn, a) == "Основной"
    connections.set_avito_identity(conn, a, 80000001, "Шины СПб")
    assert connections.get_avito_identity(conn, a) == {"id": 80000001, "name": "Шины СПб", "phone": "", "phone_source": "cabinet", "phones": [], "pinned": False}
    assert connections.display_name(conn, a) == "Шины СПб"                  # the cabinet's own name (the owner 2026-10-30; it was «ID …»)
    assert connections.get_config(conn, "avito", a)["avito_user_id"] == 80000001
