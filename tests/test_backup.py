from __future__ import annotations

import gzip
import sqlite3
from datetime import date, timedelta

from tires_sync import connections, store
from tires_sync.backup import backup_db
from tires_sync.cli import main


def make_db(tmp_path):
    conn = store.connect(tmp_path / "state.db")
    connections.create_account(conn, "Основной", "2026-10-10T12:00:00", "owner")
    conn.close()
    return tmp_path / "state.db"


def test_backup_is_a_consistent_gzipped_copy_with_private_permissions(tmp_path):
    db = make_db(tmp_path)
    out = backup_db(db, tmp_path / "backups", date(2026, 10, 10))
    assert out.name == "state-2026-10-10.db.gz"
    restored = tmp_path / "restored.db"
    restored.write_bytes(gzip.decompress(out.read_bytes()))
    names = [r[0] for r in sqlite3.connect(str(restored)).execute("SELECT name FROM account")]
    assert names == ["Основной"]
    assert (out.stat().st_mode & 0o077) == 0
    assert sorted(p.name for p in (tmp_path / "backups").iterdir()) == ["state-2026-10-10.db.gz"]


def test_only_the_newest_copies_are_kept(tmp_path):
    db = make_db(tmp_path)
    start = date(2026, 10, 1)
    for offset in range(5):
        backup_db(db, tmp_path / "backups", start + timedelta(days=offset), keep=3)
    assert sorted(p.name for p in (tmp_path / "backups").iterdir()) == [
        "state-2026-10-03.db.gz", "state-2026-10-04.db.gz", "state-2026-10-05.db.gz"]


def test_running_twice_on_the_same_day_replaces_the_copy(tmp_path):
    db = make_db(tmp_path)
    backup_db(db, tmp_path / "backups", date(2026, 10, 10))
    conn = store.connect(db)
    connections.create_account(conn, "Второй", "2026-10-10T13:00:00", "owner")
    conn.close()
    out = backup_db(db, tmp_path / "backups", date(2026, 10, 10))
    restored = tmp_path / "r.db"
    restored.write_bytes(gzip.decompress(out.read_bytes()))
    assert len(list(sqlite3.connect(str(restored)).execute("SELECT * FROM account"))) == 2


def test_missing_database_is_reported_and_creates_nothing(tmp_path):
    code = main(["backup", "--db", str(tmp_path / "nope.db"), "--out-dir", str(tmp_path / "b")])
    assert code == 2 and not (tmp_path / "b").exists()


def test_cli_backup_command(tmp_path, capsys):
    db = make_db(tmp_path)
    assert main(["backup", "--db", str(db), "--out-dir", str(tmp_path / "b"), "--keep", "5"]) == 0
    assert "state-" in capsys.readouterr().out and len(list((tmp_path / "b").iterdir())) == 1


def test_backup_is_verified_and_a_corrupt_copy_fails_loudly(tmp_path, monkeypatch):
    import pytest
    from tires_sync import backup
    db = make_db(tmp_path)
    out = backup_db(db, tmp_path / "backups", date(2026, 10, 10))
    assert backup.verify_backup(out, db)["tables"] > 5
    out.write_bytes(gzip.compress(b"not a database at all"))
    with pytest.raises(backup.BackupError):
        backup.verify_backup(out, db)
    monkeypatch.setattr(backup, "verify_backup", lambda *a, **k: (_ for _ in ()).throw(backup.BackupError("boom")))
    with pytest.raises(backup.BackupError):
        backup_db(db, tmp_path / "backups2", date(2026, 10, 11))
    assert not list((tmp_path / "backups2").glob("state-*"))


def test_backup_command_prints_the_verification(tmp_path, capsys):
    db = make_db(tmp_path)
    code = main(["backup", "--db", str(db), "--out-dir", str(tmp_path / "b")])
    assert code == 0 and "проверена" in capsys.readouterr().out


def test_a_failed_verification_keeps_the_good_copy_made_earlier_the_same_day(tmp_path, monkeypatch):
    import pytest
    from tires_sync import backup
    db = make_db(tmp_path)
    good = backup_db(db, tmp_path / "backups", date(2026, 10, 10))
    before = good.read_bytes()
    real = backup.verify_backup

    def broken(path, *a, **k):
        raise backup.BackupError("boom")

    monkeypatch.setattr(backup, "verify_backup", broken)
    with pytest.raises(backup.BackupError):
        backup_db(db, tmp_path / "backups", date(2026, 10, 10))
    assert good.exists() and good.read_bytes() == before


def test_verification_compares_with_the_snapshot_not_with_the_live_database(tmp_path):
    from tires_sync import backup
    db = make_db(tmp_path)
    out = backup_db(db, tmp_path / "backups", date(2026, 10, 10))
    live = sqlite3.connect(str(db))
    live.execute("CREATE TABLE added_later (x)")
    live.commit()
    live.close()
    assert backup.verify_backup(out, None, expected_tables=None)["tables"] > 5   # no live comparison needed
