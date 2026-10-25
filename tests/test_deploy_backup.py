"""Копия базы перед выкладкой (deploy/tires-deploy.sh, backup_db) на настоящем sqlite3 и заглушках runuser/chown.

Как в tests/test_deploy_wait.py, скрипт целиком не запускается: из него берутся log, die, backup_db и константы.
sqlite3 разрешён только через `runuser -u tires --`: файлы -wal/-shm базы в режиме WAL не должны появиться от root.
"""
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "tires-deploy.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None or shutil.which("sqlite3") is None, reason="нужны bash и sqlite3")

STUBS = r'''
runuser() {
  [ "$1" = -u ] && [ "$3" = -- ] || { echo "runuser: не тот вызов: $*" >&2; return 97; }
  [ -z "${RUNUSER_FAILS:-}" ] || { echo "runuser: отказ" >&2; return 1; }
  local who=$2; shift 3
  AS_USER=$who "$@"
}
sqlite3() {
  [ "${AS_USER:-}" = tires ] || { echo "sqlite3 не от tires" >&2; return 98; }
  command sqlite3 "$@"
}
chown() { echo "chown $*" >> "$CALLS"; }
sleep() { :; }
'''


def _extract():
    text = SCRIPT.read_text(encoding="utf-8")
    parts = []
    for name in ("KEEP_BACKUPS", "BACKUP_MIN_DAYS"):
        m = re.search(rf"^{name}=\d+", text, re.M)
        assert m, name
        parts.append(m.group(0))
    m = re.search(r"^die\(\) \{.*\}$", text, re.M)
    assert m
    parts.append(m.group(0))
    for name in ("log", "backup_db"):
        m = re.search(rf"^{name}\(\) \{{.*?^\}}$", text, re.M | re.S)
        assert m, name
        parts.append(m.group(0))
    return "\n".join(parts)


def run_backup(tmp_path, *env):
    data, state = tmp_path / "data", tmp_path / "state"
    state.mkdir()
    calls = tmp_path / "calls"
    calls.write_text("")
    program = "\n".join([
        "set -euo pipefail",
        f"DATA={data}", f"DB={data}/state.db", f"STATE={state}", f"CALLS={calls}", "LOGFILE=/dev/null",
        *env, STUBS, _extract(),
        'backup_db abc1234; echo "КОПИЯ $(cat "$STATE/last-backup")"',
    ])
    proc = subprocess.run(["bash", "-c", program], capture_output=True, text=True, timeout=60)
    return proc, calls.read_text()


def make_wal_db(path):
    path.parent.mkdir()
    conn = sqlite3.connect(str(path))
    assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    conn.execute("CREATE TABLE t (x TEXT)")
    conn.execute("INSERT INTO t VALUES ('подтверждено')")
    conn.commit()
    return conn


def test_the_copy_of_a_wal_base_is_taken_as_tires_during_a_write_and_holds_what_was_committed(tmp_path):
    conn = make_wal_db(tmp_path / "data" / "state.db")
    conn.execute("BEGIN IMMEDIATE")                                    # tires-chats is writing right now
    conn.execute("INSERT INTO t VALUES ('не подтверждено')")
    try:
        proc, calls = run_backup(tmp_path)
    finally:
        conn.rollback()
        conn.close()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = Path(re.search(r"^КОПИЯ (.+)$", proc.stdout, re.M).group(1))
    assert out.name.startswith("pre-deploy-abc1234-") and out.is_file()
    assert f"chown tires:tires {out}" in calls
    copy = sqlite3.connect(str(out))
    assert [r[0] for r in copy.execute("SELECT x FROM t")] == ["подтверждено"]
    assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    copy.close()


def test_a_copy_that_fails_three_times_stops_the_deploy_and_leaves_nothing(tmp_path):
    make_wal_db(tmp_path / "data" / "state.db").close()
    proc, _ = run_backup(tmp_path, "RUNUSER_FAILS=1")                  # e.g. tires cannot read the base
    assert proc.returncode == 1
    assert "нет копии базы" in proc.stdout + proc.stderr
    assert not list((tmp_path / "data").glob("pre-deploy-*"))
