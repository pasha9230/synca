"""The check of the feeds after a deploy (deploy/tires-deploy.sh, feed_check and the exit code 4) on the real `feed-check`.

The script is not run as a whole (it sets its own PATH and works with /opt): the functions log, die, feed_check and the lines of `deploy`
that turn its result into the exit code are taken from it; `runuser` is a stub that runs the command as is.
"""
import platform
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from helpers import catalog_xml, commodity_xml, settings_dict
from tires_sync import connections, cycle, publishing, store
from tires_sync.catalog import parse_reference
from tires_sync.settings_store import init_settings

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "tires-deploy.sh"
NOW = "2026-10-14T12:00:00"
REF_TEXT = '<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>'
BROKEN = catalog_xml(commodity_xml(NNOMMODIF="100", NREST="abc") + commodity_xml(NNOMMODIF="101"))

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="нужен bash")


def _extract():
    text = SCRIPT.read_text(encoding="utf-8")
    parts = []
    m = re.search(r"^die\(\) \{.*\}$", text, re.M)
    assert m
    parts.append(m.group(0))
    for name in ("log", "feed_check"):
        m = re.search(rf"^{name}\(\) \{{.*?^\}}$", text, re.M | re.S)
        assert m, name
        parts.append(m.group(0))
    m = re.search(r"^\s*feed_check \|\| feed_rc=\$\?$", text, re.M)
    assert m
    tail = [m.group(0)]
    m = re.search(r"^\s*\[ \"\$feed_rc\" = 0 \] \|\| die 4 .*$", text, re.M)
    assert m
    tail.append(m.group(0))
    return "\n".join(parts), "\n".join(tail)


def prepared(tmp_path):
    """Two positions published; then the row of 100 breaks and the cycle keeps it on its snapshot (the false alarm of 2026-10-21/22)."""
    data = tmp_path / "data"
    data.mkdir()
    conn = store.connect(data / "state.db")
    init_settings(conn, settings_dict(), NOW)
    account = connections.create_account(conn, "Основной", NOW, "o")
    connections.set_live(conn, account, True, NOW, "o")
    ref = parse_reference(REF_TEXT)
    cycle.run_for_accounts(conn, catalog_xml(commodity_xml(NNOMMODIF="100") + commodity_xml(NNOMMODIF="101")), ref, date(2026, 10, 14), data / "feed")
    publishing.publish_live_accounts(conn, data / "feed")
    cycle.run_for_accounts(conn, BROKEN, ref, date(2026, 10, 15), data / "feed")
    publishing.publish_live_accounts(conn, data / "feed")
    conn.close()
    (data / "unload.xml").write_bytes(BROKEN)
    (data / "reference.txt").write_text(REF_TEXT, encoding="utf-8")
    return data


def run_deploy_check(tmp_path, data):
    python = tmp_path / "python"                                  # the x86 .venv on Apple Silicon needs `arch` for its children too
    prefix = "arch -x86_64 " if sys.platform == "darwin" and platform.machine() == "x86_64" else ""
    python.write_text('#!/bin/sh\nexec %s"%s" "$@"\n' % (prefix, sys.executable))
    python.chmod(0o755)
    functions, tail = _extract()
    program = "\n".join([
        "set -euo pipefail",
        "runuser() { shift 3; \"$@\"; }",                           # runuser -u tires -- <command>
        functions,
        f"APP={ROOT}", f"PY={python}", f"DATA={data}", f"DB={data / 'state.db'}", "LOGFILE=/dev/null",
        "feed_rc=0 timers_rc=0",
        tail,
        'echo "ГОТОВО"',
    ])
    p = subprocess.run(["bash", "-c", program], capture_output=True, text=True, timeout=120)
    return p.returncode, p.stdout + p.stderr


def test_a_position_the_cycle_holds_does_not_turn_the_deploy_red(tmp_path):
    data = prepared(tmp_path)
    code, out = run_deploy_check(tmp_path, data)
    assert code == 0 and "ГОТОВО" in out and "stock" not in out, out


def test_a_real_error_still_stops_the_deploy_with_code_4(tmp_path):
    data = prepared(tmp_path)
    (data / "unload.xml").write_bytes(catalog_xml(commodity_xml(NNOMMODIF="100", NREST="abc")))     # 101 left the supplier, nobody holds it
    code, out = run_deploy_check(tmp_path, data)
    assert code == 4 and "[critical] stock 101" in out and "stock 100" not in out and "feed-check нашёл ошибки" in out, out
