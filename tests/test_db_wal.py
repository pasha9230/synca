"""The base opens the same way everywhere (store.open_db): one busy timeout, and the code works whether the file is in the
rollback journal (delete) or in WAL. The mode itself is switched once on the server by hand (report of the run tails2, «wal»),
the code never changes it. Copies of a WAL base hold what was committed and open on their own."""
import gzip
import re
import sqlite3
import threading
import time
from datetime import date
from pathlib import Path

import pytest

from tires_sync import backup, connections, order_cleanup, store

SRC = Path(__file__).resolve().parents[1] / "src"
MODES = ("delete", "wal")


def make_db(path, mode):
    store.connect(path).close()
    raw = sqlite3.connect(str(path))
    assert raw.execute("PRAGMA journal_mode=%s" % mode).fetchone()[0] == mode
    raw.close()
    conn = store.connect(path)
    connections.create_account(conn, "Основной", "2026-10-22T12:00:00", "owner")
    conn.close()
    return path


def mode_of(path):
    raw = sqlite3.connect(str(path))
    try:
        return raw.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        raw.close()


def accounts(path):
    raw = sqlite3.connect(str(path))
    try:
        return [r[0] for r in raw.execute("SELECT name FROM account ORDER BY id")]
    finally:
        raw.close()


def test_every_connection_waits_15_seconds_for_a_busy_base(tmp_path):
    path = make_db(tmp_path / "state.db", "delete")
    assert store.BUSY_SECONDS == 15
    for conn in (store.open_db(path), store.connect(path)):
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 15000
        conn.close()


@pytest.mark.parametrize("mode", MODES)
def test_opening_keeps_the_journal_mode_of_the_file(tmp_path, mode):
    path = make_db(tmp_path / "state.db", mode)
    conn = store.connect(path)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == mode
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2               # FULL: no committed write is lost on a power cut
    conn.close()
    assert mode_of(path) == mode


@pytest.mark.parametrize("mode", MODES)
def test_a_second_writer_waits_for_the_first_instead_of_failing(tmp_path, mode):
    path = make_db(tmp_path / "state.db", mode)
    first = sqlite3.connect(str(path), check_same_thread=False)               # another process; it commits from the timer's thread
    first.execute("BEGIN IMMEDIATE")
    first.execute("INSERT INTO account (name, feed_token, created) VALUES ('Первый', 't-Первый', 'x')")
    done = threading.Timer(0.5, first.commit)
    done.start()
    try:
        second = store.connect(path)                                             # «database is locked» before the timeout would fail here
        second.execute("INSERT INTO account (name, feed_token, created) VALUES ('Второй', 't-Второй', 'x')")
        second.commit()
        second.close()
    finally:
        done.join()
        first.close()
    assert accounts(path) == ["Основной", "Первый", "Второй"]


def test_in_wal_a_reader_does_not_wait_for_a_writer(tmp_path):
    path = make_db(tmp_path / "state.db", "wal")
    writer = store.open_db(path)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO account (name, feed_token, created) VALUES ('Не подтверждено', 't-Не подтверждено', 'x')")
    try:
        started = time.monotonic()
        reader = store.connect(path, timeout=0.1)
        names = [r[0] for r in reader.execute("SELECT name FROM account")]
        reader.close()
        assert names == ["Основной"]                                             # only what was committed
        assert time.monotonic() - started < 1
    finally:
        writer.rollback()
        writer.close()


@pytest.mark.parametrize("mode", MODES)
def test_the_daily_copy_taken_during_a_write_holds_what_was_committed(tmp_path, mode):
    path = make_db(tmp_path / "state.db", mode)
    writer = store.open_db(path)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO account (name, feed_token, created) VALUES ('Не подтверждено', 't-Не подтверждено', 'x')")
    try:
        out = backup.backup_db(path, tmp_path / "backups", date(2026, 10, 22))
    finally:
        writer.rollback()
        writer.close()
    restored = tmp_path / "restored.db"
    restored.write_bytes(gzip.decompress(out.read_bytes()))
    assert accounts(restored) == ["Основной"]
    raw = sqlite3.connect(str(restored))
    assert raw.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    raw.close()


@pytest.mark.parametrize("mode", MODES)
def test_the_copy_before_a_bulk_change_taken_during_a_write_holds_what_was_committed(tmp_path, mode):
    path = make_db(tmp_path / "state.db", mode)
    writer = store.open_db(path)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO account (name, feed_token, created) VALUES ('Не подтверждено', 't-Не подтверждено', 'x')")
    try:
        copy = order_cleanup.backup(path, "20261022-120000", what="wal-test")
    finally:
        writer.rollback()
        writer.close()
    assert copy.name == "pre-wal-test-20261022-120000.db"
    assert accounts(copy) == ["Основной"]


def test_the_base_is_opened_only_through_the_helper():
    """One place decides how the base is opened: a new sqlite3.connect elsewhere would open it without the busy timeout."""
    found = [str(p.relative_to(SRC)) for p in SRC.rglob("*.py") if re.search(r"sqlite3\.connect\(", p.read_text(encoding="utf-8"))]
    assert found == ["tires_sync/store.py"]
