"""Таймеры при выкладке (deploy/tires-deploy.sh, sync_timers) и проверка аргумента deploy, на заглушках.

Как в tests/test_deploy_wait.py, скрипт целиком не запускается: из него берутся функции и константы.
Строки таймера по TIMER_KEYS проверяет tests/test_deploy_pull.py на Python: BSD grep на Маке не понимает
пустую альтернативу в выражении, и здесь эта проверка не показательна. Главное: таймер, которого на сервере ещё нет (новый tires-pull.timer), выкладка не ставит и не включает,
его ставит только владелец вручную (deploy/AUTODEPLOY.md).
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "tires-deploy.sh"
SHA = "c" * 40

pytestmark = pytest.mark.skipif(shutil.which("bash") is None or shutil.which("tar") is None, reason="нужны bash и tar")

STUBS = r'''
git() {   # git -C <repo> archive <sha> deploy: архив папки $SRC/deploy
  [ "$1" = -C ] && [ "$3" = archive ] && [ "$5" = deploy ] || { echo "git: не тот вызов: $*" >&2; return 97; }
  command tar -c -C "$SRC" deploy
}
install() {   # install -o root -g root -m 0644 <src> <dst>
  echo "install ${8##*/}" >> "$CALLS"
  cp "$7" "$8"
}
systemctl() {
  echo "systemctl $*" >> "$CALLS"
  case "$1" in
    is-active) [[ " $ACTIVE " == *" ${!#} "* ]] ;;
    *) return 0 ;;
  esac
}
systemd-analyze() { return 0; }
paste() { command paste "$@" -; }   # BSD paste на Маке без «-» не читает stdin
'''


def _extract(names, consts=()):
    text = SCRIPT.read_text(encoding="utf-8")
    parts = []
    for name in consts:
        m = re.search(rf"^{name}=.*$", text, re.M)
        assert m, name
        parts.append(m.group(0))
    m = re.search(r"^die\(\) \{.*\}$", text, re.M)
    assert m
    parts.append(m.group(0))
    for name in names:
        m = re.search(rf"^{name}\(\) \{{.*?^\}}$", text, re.M | re.S)
        assert m, name
        parts.append(m.group(0))
    return "\n".join(parts)


def run_sync(tmp_path, src_deploy: dict, installed: dict, active=""):
    src = tmp_path / "src"
    (src / "deploy").mkdir(parents=True)
    for name, body in src_deploy.items():
        (src / "deploy" / name).write_text(body)
    units = tmp_path / "units"
    units.mkdir()
    for name, body in installed.items():
        (units / name).write_text(body)
    calls = tmp_path / "calls"
    calls.write_text("")
    program = "\n".join([
        # без -u: bash 3.2 на Маке считает пустой массив (was_active) неопределённым, на сервере bash 5
        "set -eo pipefail",
        "shopt -s inherit_errexit 2>/dev/null || true",
        f"SRC={src}", f"CALLS={calls}", f"ACTIVE='{active}'",
        STUBS,
        _extract(["log", "restore_timer", "sync_timers"], consts=["TIMER_KEYS"]),
        f"LOGFILE=/dev/null REPO={tmp_path}/repo UNITS={units} TIMERS_BACKUP={tmp_path}/backup",
        f'rc=0; sync_timers {SHA} || rc=$?; echo "rc=$rc"',
    ])
    p = subprocess.run([shutil.which("bash"), "-c", program], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stdout + p.stderr
    return p.stdout + p.stderr, calls.read_text(), units


TIMER_OLD = "[Unit]\nDescription=x\n\n[Timer]\nOnCalendar=*-*-* *:40:00\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n"
TIMER_NEW = TIMER_OLD.replace("*:40:00", "*:45:00")


def test_new_timer_is_not_installed_nor_enabled(tmp_path):
    pull = (ROOT / "deploy" / "tires-pull.timer").read_text()
    out, calls, units = run_sync(
        tmp_path,
        {"tires-cycle.timer": TIMER_NEW, "tires-pull.timer": pull},
        {"tires-cycle.timer": TIMER_OLD},
        active="tires-cycle.timer",
    )
    assert "rc=0" in out
    assert not (units / "tires-pull.timer").exists()
    assert "tires-pull" not in calls                        # ни install, ни enable/start/restart
    assert (units / "tires-cycle.timer").read_text() == TIMER_NEW   # установленный обновляется как раньше
    assert "systemctl restart tires-cycle.timer" in calls


def test_installed_pull_timer_is_updated_but_not_started_if_stopped(tmp_path):
    pull = (ROOT / "deploy" / "tires-pull.timer").read_text()
    old = pull.replace("RandomizedDelaySec=20", "RandomizedDelaySec=30")
    out, calls, units = run_sync(tmp_path, {"tires-pull.timer": pull}, {"tires-pull.timer": old}, active="")
    assert "rc=0" in out, out
    assert (units / "tires-pull.timer").read_text() == pull
    assert "restart tires-pull" not in calls and "start tires-pull" not in calls


def test_all_repository_timers_pass_and_pull_is_skipped(tmp_path):
    # настоящие deploy/*.timer: установлены все, кроме tires-pull.timer, и все отличаются от установленных
    src = {p.name: p.read_text() for p in (ROOT / "deploy").glob("*.timer")}
    assert "tires-pull.timer" in src
    installed = {n: "# old\n" for n in src if n != "tires-pull.timer"}
    out, calls, units = run_sync(tmp_path, src, installed, active="")
    assert "rc=0" in out, out
    assert "не ставлю" not in out
    assert not (units / "tires-pull.timer").exists()
    assert "tires-pull" not in calls


@pytest.mark.parametrize("arg", ["", "rollback", SHA[:-1], SHA.upper(), SHA + "0", f"{SHA} x", "$(id)", "-h"])
def test_deploy_rejects_anything_but_40_hex_before_any_action(tmp_path, arg):
    # sudo пропускает tires-pull только «deploy <40 hex>» (deploy/sudoers-tires-pull), а у tires-deploy.sh
    # диспетчер передаёт в cmd_deploy только второй аргумент ("${2:-}"), остальные не используются;
    # сам cmd_deploy первым делом отвергает всё, кроме 40 знаков hex, до флага, замка и git.
    program = "\n".join([
        "set -euo pipefail",
        f"CALLS={tmp_path}/calls",
        _extract(["log", "cmd_deploy"]),
        "LOGFILE=/dev/null",
        'require_enabled() { echo require_enabled >> "$CALLS"; }',
        'lock() { echo lock >> "$CALLS"; }',
        'fetch_master() { echo fetch >> "$CALLS"; MASTER=x; }',
        'cmd_deploy "$1"',
    ])
    p = subprocess.run([shutil.which("bash"), "-c", program, "test", arg], capture_output=True, text=True, timeout=30)
    assert p.returncode == 1
    assert "нужен полный хеш" in p.stdout + p.stderr
    assert not (tmp_path / "calls").exists()


def test_dispatcher_passes_only_the_second_argument():
    text = SCRIPT.read_text(encoding="utf-8")
    assert re.search(r'^\s*deploy\)\s+cmd_deploy "\$\{2:-\}" ;;$', text, re.M)
