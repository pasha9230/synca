from __future__ import annotations

import gzip
import os
import shutil
import sqlite3
import tempfile
from datetime import date
from pathlib import Path

from .store import open_db


class BackupError(Exception):
    pass


PREFIX = "state-"
SUFFIX = ".db.gz"


def verify_backup(backup_path, source_db=None, expected_tables=None) -> dict:
    """Open the gzipped copy as a database: it must pass the integrity check and contain every table of the source."""
    fd, raw = tempfile.mkstemp(suffix=".verify")
    os.close(fd)
    try:
        try:
            with gzip.open(str(backup_path), "rb") as src, open(raw, "wb") as dst:
                shutil.copyfileobj(src, dst)
            copy = open_db(raw)
            try:
                if copy.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise BackupError("копия базы не прошла проверку целостности")
                names = {r[0] for r in copy.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            finally:
                copy.close()
        except (OSError, sqlite3.DatabaseError, EOFError) as err:
            raise BackupError("копия базы повреждена: %s" % err) from err
        wanted = set(expected_tables or ())
        if source_db is not None and expected_tables is None:
            source = open_db(source_db)
            try:
                wanted = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            finally:
                source.close()
        missing = sorted(wanted - names - {"sqlite_sequence"})
        if missing:
            raise BackupError("в копии нет таблиц: %s" % ", ".join(missing))
        return {"tables": len(names)}
    finally:
        if os.path.exists(raw):
            os.unlink(raw)


def backup_db(db_path, out_dir, today: date, keep: int = 14) -> Path:
    """Consistent online copy of the SQLite database, gzipped, newest `keep` copies are retained."""
    if not Path(db_path).exists():
        raise FileNotFoundError("база данных не найдена: %s" % db_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(str(out_dir), 0o700)
    final = out_dir / (PREFIX + today.isoformat() + SUFFIX)
    fd, raw = tempfile.mkstemp(dir=str(out_dir), suffix=".part")
    os.close(fd)
    try:
        source = open_db(db_path)
        target = open_db(raw)
        try:
            source.backup(target)
            snapshot_tables = {r[0] for r in target.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        finally:
            target.close()
            source.close()
        fd, packed = tempfile.mkstemp(dir=str(out_dir), suffix=".gz.part")
        os.close(fd)
        try:
            with open(raw, "rb") as src, gzip.open(packed, "wb") as dst:
                shutil.copyfileobj(src, dst)
            os.chmod(packed, 0o600)
            verify_backup(packed, expected_tables=snapshot_tables)   # a bad copy never replaces a good one
            os.replace(packed, str(final))
        finally:
            if os.path.exists(packed):
                os.unlink(packed)
    finally:
        if os.path.exists(raw):
            os.unlink(raw)
    copies = sorted(p for p in out_dir.iterdir() if p.name.startswith(PREFIX) and p.name.endswith(SUFFIX))
    for old in copies[:-keep] if keep > 0 else []:
        old.unlink()
    return final
