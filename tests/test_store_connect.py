"""Opening the database must not need a write lock once it is migrated: a panel request or a CLI command waits for nobody."""
import sqlite3

from tires_sync import store


def test_connecting_to_a_migrated_database_works_while_another_connection_writes(tmp_path):
    path = tmp_path / "t.db"
    store.connect(path).close()
    writer = sqlite3.connect(str(path), timeout=0.1)
    writer.execute("BEGIN IMMEDIATE")                                         # a long write of another process
    try:
        conn = store.connect(path, timeout=0.2)                               # used to fail with «database is locked»
        assert conn.execute("SELECT COUNT(*) FROM account").fetchone()[0] >= 0
        conn.close()
    finally:
        writer.rollback()
        writer.close()


def test_the_first_seen_backfill_still_happens_for_rows_that_have_none(tmp_path):
    path = tmp_path / "t.db"
    conn = store.connect(path)
    conn.execute("INSERT INTO avito_ad (account_id, avito_id, seen_at) VALUES (1, 7, '2026-10-01T00:00:00')")
    conn.commit()
    conn.close()
    again = store.connect(path)
    assert again.execute("SELECT first_seen FROM avito_ad WHERE avito_id = 7").fetchone()[0] == "2026-10-01T00:00:00"
