from __future__ import annotations

import sqlite3
from datetime import datetime

from tires_sync import connections, store
from panel_helpers import make_app

UTC_NOW = datetime(2026, 10, 21, 12, 0, 0)                  # 15:00 in Moscow


def setup(tmp_path, cycle="2026-10-21T11:00:00", chats=True, synced="2026-10-21T14:50:00", now=UTC_NOW):
    app = make_app(tmp_path, NOW=lambda: now)
    conn = store.connect(app.config["DB_PATH"])
    account = connections.create_account(conn, "Основной", "2026-10-10T12:00:00", "owner")
    connections.set_modes(conn, account, chats, True, "2026-10-10T12:00:00", "owner")
    if cycle:
        store.save_run(conn, cycle, True, "sha", "{}", account)
    if synced is not None:
        conn.execute("INSERT INTO avito_chat (account_id, chat_id, synced_at) VALUES (?, 'c1', ?)", (account, synced))
    conn.commit()
    conn.close()
    return app.test_client()


def test_all_fresh_is_ok_without_login_and_without_a_cookie(tmp_path):
    r = setup(tmp_path).get("/health")
    assert (r.status_code, r.get_data(as_text=True)) == (200, "ok\n")
    assert r.mimetype == "text/plain" and r.headers["Cache-Control"] == "no-store" and "Set-Cookie" not in r.headers


def test_old_cycle_fails_with_its_code_only(tmp_path):
    r = setup(tmp_path, cycle="2026-10-21T09:59:00").get("/health")
    assert (r.status_code, r.get_data(as_text=True)) == (503, "fail cycle\n")


def test_no_cycle_at_all_fails(tmp_path):
    assert setup(tmp_path, cycle=None).get("/health").get_data(as_text=True) == "fail cycle\n"


def test_two_hours_is_still_fine(tmp_path):
    assert setup(tmp_path, cycle="2026-10-21T10:00:00").get("/health").status_code == 200


def test_chats_not_read_for_15_minutes_fail(tmp_path):
    r = setup(tmp_path, synced="2026-10-21T14:44:59").get("/health")
    assert (r.status_code, r.get_data(as_text=True)) == (503, "fail chats\n")
    (tmp_path / "b").mkdir()
    assert setup(tmp_path / "b", synced="2026-10-21T14:45:00").get("/health").status_code == 200


def test_both_old_and_never_read_chats(tmp_path):
    assert setup(tmp_path, cycle="2026-10-20T00:00:00", synced=None).get("/health").get_data(as_text=True) == "fail cycle chats\n"


def test_chats_are_not_checked_when_no_account_reads_them(tmp_path):
    assert setup(tmp_path, chats=False, synced=None).get("/health").status_code == 200


def test_a_broken_base_is_fail_db_and_nothing_else(tmp_path, monkeypatch):
    client = setup(tmp_path)
    from tires_sync.panel import routes_health

    def broken(conn, now):
        raise sqlite3.OperationalError("database is locked /var/lib/tires-sync/state.db")

    monkeypatch.setattr(routes_health, "problems", broken)
    r = client.get("/health")
    assert (r.status_code, r.get_data(as_text=True)) == (503, "fail db\n")


def test_head_works_for_the_watchdog(tmp_path):
    assert setup(tmp_path).head("/health").status_code == 200
