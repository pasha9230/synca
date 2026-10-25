"""Выкладка опросом (deploy/tires-pull.sh) целиком, на поддельных curl, sudo, flock и date в PATH.

Настоящие только bash и jq. Поддельный curl отдаёт ответы GitHub из файлов сценария и пишет свои
аргументы и stdin (там заголовок с токеном); поддельный sudo вместо tires-deploy пишет вызов и ведёт
себя по коду выхода сценария (0/4/5 пишет deployed-sha, 2/3 оставляет выпуск коммита, как настоящий).
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "tires-pull.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None or shutil.which("jq") is None, reason="нужны bash и jq")

OLD = "a" * 40
NEW = "b" * 40
NOW = 1_800_000_000
TOKEN = "github_pat_TESTONLY0123456789abcdef"

FAKE_CURL = r'''#!/bin/sh
out= url=
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out=$2; shift ;;
    -w|-H|-m|--proto|--max-filesize) shift ;;
    https://*) url=$1 ;;
  esac
  shift
done
printf '%s\n' "$url" >> "$FAKE/curl-urls"
cat >> "$FAKE/curl-stdin"
[ -z "${FAKE_CURL_FAIL:-}" ] || { echo "curl: (6) Could not resolve host: api.github.com" >&2; exit 6; }
case "$url" in
  */check-runs*) name=checks ;;
  */compare/*) name=compare ;;
  */commits/master) name=master ;;
  *) echo "неизвестный адрес $url" >&2; exit 3 ;;
esac
cp "$FAKE/$name.body" "$out"
cat "$FAKE/$name.code"
'''

FAKE_SUDO = r'''#!/bin/sh
printf '%s\n' "$*" >> "$FAKE/sudo-calls"
[ "$1" = -n ] && [ "$2" = /usr/local/sbin/tires-deploy ] && [ "$3" = deploy ] || exit 99
rc=${FAKE_DEPLOY_RC:-0}
case "$rc" in
  0|4|5) printf '%s\n' "$4" > "$DEPLOY_STATE_DIR/deployed-sha"; mkdir -p "$RELEASES_DIR/r-$4"; printf '%s\n' "$4" > "$RELEASES_DIR/r-$4/sha" ;;
  2|3) mkdir -p "$RELEASES_DIR/r-$4"; printf '%s\n' "$4" > "$RELEASES_DIR/r-$4/sha" ;;
  1) echo "2026-10-25 12:00:00 СТОП: не дождался тишины за 75 мин" ;;
esac
echo "журнал tires-deploy"
exit "$rc"
'''

FAKE_FLOCK = r'''#!/bin/sh
[ "$1" = -n ] || exit 64
case "$2" in
  8) [ -z "${FAKE_OWN_LOCK_BUSY:-}" ] || exit 1 ;;
  7) [ -z "${FAKE_DEPLOY_LOCK_BUSY:-}" ] || exit 1 ;;
  *) exit 64 ;;
esac
exit 0
'''

FAKE_DATE = r'''#!/bin/sh
[ "$1" = +%s ] && { cat "$FAKE/now"; exit 0; }
exec /bin/date "$@"
'''


def iso(epoch):
    import datetime
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def check_run(status="completed", conclusion="success", done=NOW - 3600, name="tests / pytest",
              app="github-actions", sha=NEW, id_=10):
    return {"id": id_, "name": name, "head_sha": sha, "status": status, "conclusion": conclusion,
            "completed_at": iso(done) if done else None, "app": {"slug": app}}


class Env:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.fake = tmp_path / "fake"
        self.bin = tmp_path / "bin"
        self.state = tmp_path / "state"
        self.dstate = tmp_path / "deploy-state"
        self.releases = tmp_path / "releases"
        self.cred = tmp_path / "cred"
        for d in (self.fake, self.bin, self.state, self.dstate, self.releases, self.cred):
            d.mkdir()
        for name, body in (("curl", FAKE_CURL), ("sudo", FAKE_SUDO), ("flock", FAKE_FLOCK), ("date", FAKE_DATE)):
            p = self.bin / name
            p.write_text(body)
            p.chmod(0o755)
        (self.bin / "jq").symlink_to(shutil.which("jq"))
        (self.dstate / "deployed-sha").write_text(OLD + "\n")
        (self.releases / f"r-{OLD}").mkdir()
        (self.releases / f"r-{OLD}" / "sha").write_text(OLD + "\n")
        (self.tmp / "lock").write_text("")
        (self.dstate / "previous-release").write_text(str(self.releases / "r-older") + "\n")
        self.now = NOW
        self.extra = {}
        self.respond(master=NEW, checks=[check_run()], compare="ahead")

    def rolled_back(self, at):
        import os
        p = self.dstate / "previous-release"
        p.write_text("")
        os.utime(p, (at, at))

    def respond(self, master=None, checks=None, compare=None, master_code=200, checks_code=200, compare_code=200):
        if master is not None:
            (self.fake / "master.body").write_text(master)
        if checks is not None:
            (self.fake / "checks.body").write_text(json.dumps({"total_count": len(checks), "check_runs": checks}))
        if compare is not None:
            (self.fake / "compare.body").write_text(json.dumps({"status": compare}))
        (self.fake / "master.code").write_text(str(master_code))
        (self.fake / "checks.code").write_text(str(checks_code))
        (self.fake / "compare.code").write_text(str(compare_code))

    def token(self, value=TOKEN):
        (self.cred / "tires-pull.github-token").write_text(value + "\n")

    def run(self, **env):
        (self.fake / "now").write_text(str(self.now))
        e = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "FAKE": str(self.fake),
            "DEPLOY_STATE_DIR": str(self.dstate),
            "RELEASES_DIR": str(self.releases),
            "TIRES_PULL_STATE": str(self.state),
            "TIRES_PULL_DEPLOY_STATE": str(self.dstate),
            "TIRES_PULL_RELEASES": str(self.releases),
            "TIRES_PULL_DEPLOY_LOCK": str(self.tmp / "lock"),
            "CREDENTIALS_DIRECTORY": str(self.cred),
            "LC_ALL": "C",
        }
        e.update(self.extra)
        e.update({k: str(v) for k, v in env.items()})
        p = subprocess.run([shutil.which("bash"), str(SCRIPT)], capture_output=True, text=True, env=e, timeout=60)
        self.out = p.stdout
        self.err = p.stderr
        return p.returncode

    def sudo_calls(self):
        f = self.fake / "sudo-calls"
        return f.read_text().splitlines() if f.exists() else []

    def urls(self):
        f = self.fake / "curl-urls"
        return f.read_text().splitlines() if f.exists() else []

    def one_line(self):
        assert self.err == "", self.err
        lines = self.out.splitlines()
        assert len(lines) == 1, self.out
        return lines[0]


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def test_new_green_descendant_is_deployed(env):
    assert env.run() == 0
    assert env.sudo_calls() == [f"-n /usr/local/sbin/tires-deploy deploy {NEW}"]
    assert "выложено bbbbbbb" in env.one_line()
    assert any("/check-runs?check_name=tests%20%2F%20pytest&" in u for u in env.urls())
    assert any(f"/compare/{OLD}...{NEW}" in u for u in env.urls())
    # следующий прогон: уже выложен, один запрос, без вызова выкладки
    assert env.run() == 0
    assert "выложен bbbbbbb, нового нет" in env.one_line()
    assert len(env.sudo_calls()) == 1


def test_master_already_deployed_makes_one_request(env):
    env.respond(master=OLD)
    assert env.run() == 0
    assert "нового нет" in env.one_line()
    assert len(env.urls()) == 1
    assert env.sudo_calls() == []


@pytest.mark.parametrize("run,expect", [
    (check_run(status="in_progress", conclusion=None, done=None), "тесты in_progress"),
    (check_run(status="queued", conclusion=None, done=None), "тесты queued"),
    (check_run(conclusion="failure"), "тесты failure"),
    (check_run(conclusion="cancelled"), "тесты cancelled"),
    (check_run(conclusion="skipped"), "тесты skipped"),
    (check_run(app="evil-app"), "ещё нет"),
    (check_run(name="pytest"), "ещё нет"),
    (check_run(sha="c" * 40), "ещё нет"),
])
def test_not_green_is_never_deployed(env, run, expect):
    env.respond(checks=[run])
    assert env.run() == 0
    assert expect in env.one_line()
    assert env.sudo_calls() == []


def test_latest_check_run_wins(env):
    # перезапуск: старый прогон зелёный, последний красный — не выкладывать (с токеном: без паузы на повторный вопрос)
    env.token()
    env.respond(checks=[check_run(id_=1), check_run(id_=2, conclusion="failure")])
    assert env.run() == 0
    assert env.sudo_calls() == []
    env.respond(checks=[check_run(id_=2), check_run(id_=1, conclusion="failure")])
    assert env.run() == 0
    assert len(env.sudo_calls()) == 1


def test_no_check_at_all(env):
    env.respond(checks=[])
    assert env.run() == 0
    assert "ещё нет" in env.one_line()
    assert env.sudo_calls() == []


def test_min_age_delays_the_deploy(env):
    env.respond(checks=[check_run(done=NOW - 5 * 60)])
    assert env.run(TIRES_PULL_MIN_AGE_MIN=20) == 0
    assert "зелёные 5 мин" in env.one_line()
    assert env.sudo_calls() == []
    env.now = NOW + 16 * 60
    assert env.run(TIRES_PULL_MIN_AGE_MIN=20) == 0
    assert len(env.sudo_calls()) == 1


@pytest.mark.parametrize("status", ["behind", "diverged"])
def test_not_a_descendant_is_blocked_with_one_alert(env, status):
    env.respond(compare=status)
    assert env.run() == 1
    assert f"GitHub: {status}" in env.one_line()
    assert env.run() == 0
    assert "заблокирован" in env.one_line()
    assert env.sudo_calls() == []


def test_rolled_back_commit_is_not_redeployed(env):
    # выпуск NEW есть, а выложен OLD: откат кнопкой (или неудача раннера)
    (env.releases / "r-new").mkdir()
    (env.releases / "r-new" / "sha").write_text(NEW + "\n")
    assert env.run() == 1
    assert "уже выкладывался" in env.one_line()
    assert env.run() == 0
    assert env.sudo_calls() == []
    assert env.urls() == [env.urls()[0]] * len(env.urls())   # дальше master ничего не спрашивали


def test_new_commit_after_block_is_deployed(env):
    env.respond(compare="diverged")
    assert env.run() == 1
    newer = "d" * 40
    env.respond(master=newer, checks=[check_run(sha=newer)], compare="ahead")
    assert env.run() == 0
    assert env.sudo_calls() == [f"-n /usr/local/sbin/tires-deploy deploy {newer}"]


def test_no_deployed_record_blocks(env):
    (env.dstate / "deployed-sha").unlink()
    assert env.run() == 1
    assert "нет записи о выложенном" in env.one_line()
    assert env.sudo_calls() == []


@pytest.mark.parametrize("body", [
    "$(touch /tmp/pwned)" + "a" * 30,
    "B" * 40,
    "b" * 39,
    "b" * 41,
    "b" * 39 + ";",
    "",
    "<html>rate limit</html>",
])
def test_garbage_master_is_never_used(env, body):
    env.respond(master=body)
    assert env.run() == 0
    assert "не похож на коммит" in env.one_line()
    assert env.sudo_calls() == []
    assert len(env.urls()) == 1


def test_network_failure_alerts_once_after_30(env):
    for i in range(1, 4):
        assert env.run(FAKE_CURL_FAIL=1) == 0, i
        assert f"({i} подряд)" in env.one_line()
        assert "Could not resolve host" in env.one_line()
    (env.state / "net-fails").write_text("28\n")            # быстрее, чем 26 прогонов
    assert env.run(FAKE_CURL_FAIL=1) == 0
    assert env.run(FAKE_CURL_FAIL=1) == 1
    assert "30 раз подряд" in env.one_line()
    assert env.run(FAKE_CURL_FAIL=1) == 0
    assert env.sudo_calls() == []
    assert env.run() == 0                                    # сеть вернулась: выкладка и сброс счётчика
    assert not (env.state / "net-fails").exists()


@pytest.mark.parametrize("which", ["master_code", "checks_code", "compare_code"])
@pytest.mark.parametrize("code", [403, 404, 429, 500])
def test_http_errors_do_nothing(env, which, code):
    env.respond(**{which: code})
    assert env.run() == 0
    assert f"ответ {code}" in env.one_line()
    assert env.sudo_calls() == []
    assert (env.state / "net-fails").read_text().strip() == "1"


def test_checks_api_failing_still_accumulates_to_alert(env):
    # успешный ответ master не сбрасывает счётчик, если дальше (тесты) сбой: иначе тревоги не было бы никогда
    env.respond(checks_code=403)
    assert env.run() == 0 and env.run() == 0
    assert (env.state / "net-fails").read_text().strip() == "2"
    (env.state / "net-fails").write_text("28\n")
    assert env.run() == 0
    assert env.run() == 1


def test_deploy_code_1_backs_off_then_blocks(env):
    assert env.run(FAKE_DEPLOY_RC=1) == 0
    assert "попытка 1" in env.one_line() and "не дождался тишины" in env.one_line()
    assert env.run(FAKE_DEPLOY_RC=1) == 0                     # пауза 10 минут: без вызова
    assert "пауза" in env.one_line()
    assert len(env.sudo_calls()) == 1
    for n, wait in ((2, 10), (3, 30)):
        env.now += wait * 60
        assert env.run(FAKE_DEPLOY_RC=1) == 0
        assert f"попытка {n}" in env.one_line()
    env.now += 60 * 60
    assert env.run(FAKE_DEPLOY_RC=1) == 1                     # 4-я неудача: блокировка и тревога
    assert "4 раз подряд" in env.one_line()
    env.now += 600 * 60
    assert env.run(FAKE_DEPLOY_RC=0) == 0
    assert "заблокирован" in env.one_line()
    assert len(env.sudo_calls()) == 4


def test_deploy_code_1_then_success_clears_backoff(env):
    assert env.run(FAKE_DEPLOY_RC=1) == 0
    env.now += 10 * 60
    assert env.run(FAKE_DEPLOY_RC=0) == 0
    assert not (env.state / "fail").exists()


@pytest.mark.parametrize("rc", [2, 3])
def test_failed_deploy_is_not_retried(env, rc):
    assert env.run(FAKE_DEPLOY_RC=rc) == 1
    assert f"код {rc}" in env.one_line()
    for _ in range(3):
        assert env.run(FAKE_DEPLOY_RC=0) == 0
    assert len(env.sudo_calls()) == 1


@pytest.mark.parametrize("rc", [4, 5])
def test_deployed_with_warning_alerts_once(env, rc):
    assert env.run(FAKE_DEPLOY_RC=rc) == 1
    assert f"код {rc}" in env.one_line()
    assert env.run() == 0
    assert "нового нет" in env.one_line()
    assert len(env.sudo_calls()) == 1


@pytest.mark.parametrize("rc", [77, 128])
def test_unknown_code_backs_off_like_code_1(env, rc):
    # git под set -e у tires-deploy выходит со своим кодом (128), это не «панель сломана»
    assert env.run(FAKE_DEPLOY_RC=rc) == 0
    assert f"код {rc}, попытка 1" in env.one_line() and "повтор через 10 мин" in env.one_line()
    assert not (env.state / "blocked").exists()


def test_rollback_holds_every_new_commit_until_manual_deploy(env):
    # X выложен, владелец откатил на OLD; в master Y поверх X (или запись в docs/ после отката): несёт плохое X
    env.rolled_back(NOW - 600)
    assert env.run() == 1
    assert "пауза после отката" in env.one_line() and "tires-deploy deploy" in env.one_line()
    env.now += 10 * 60
    newer = "d" * 40                                         # и коммит, сделанный после отката, тоже ждёт
    env.respond(master=newer, checks=[check_run(sha=newer)])
    assert env.run() == 0
    assert "пауза после отката" in env.one_line()
    assert env.sudo_calls() == []
    assert len(env.urls()) == 2                              # только master, тесты не спрашивали
    # владелец выложил вручную: previous-release снова заполнен, пауза снята
    (env.dstate / "previous-release").write_text(str(env.releases / "r-older") + "\n")
    newest = "e" * 40
    env.respond(master=newest, checks=[check_run(sha=newest)])
    assert env.run() == 0
    assert env.sudo_calls() == [f"-n /usr/local/sbin/tires-deploy deploy {newest}"]


def test_second_rollback_alerts_again(env):
    env.rolled_back(NOW - 600)
    assert env.run() == 1
    env.rolled_back(NOW - 60)
    assert env.run() == 1


def test_lock_held_forever_alerts_as_stall(env):
    env.token()
    assert env.run(FAKE_DEPLOY_LOCK_BUSY=1) == 0
    env.now += 181 * 60
    assert env.run(FAKE_DEPLOY_LOCK_BUSY=1) == 1
    assert "не выложен уже 181 мин" in env.one_line() and "идёт выкладка" in env.one_line()


def test_backoff_pause_counts_from_the_end_of_the_attempt(env):
    # поддельный sudo «идёт» 80 минут: двигает часы
    sudo = env.bin / "sudo"
    sudo.write_text(sudo.read_text().replace('rc=${FAKE_DEPLOY_RC:-0}', 'rc=${FAKE_DEPLOY_RC:-0}; echo $(( $(cat "$FAKE/now") + 4800 )) > "$FAKE/now"'))
    assert env.run(FAKE_DEPLOY_RC=1) == 0
    line = (env.state / "fail").read_text().split()
    assert int(line[2]) == NOW + 4800 + 600


def test_unreadable_releases_do_not_deploy(env):
    env.releases.chmod(0)
    try:
        assert env.run() == 1
        assert "не прочитать выпуски" in env.one_line()
        assert env.run() == 0
        assert env.sudo_calls() == []
    finally:
        env.releases.chmod(0o755)


def test_deployed_release_missing_does_not_deploy(env):
    shutil.rmtree(env.releases / f"r-{OLD}")
    assert env.run() == 1
    assert "нет выложенного" in env.one_line()
    assert env.sudo_calls() == []


def test_unwritable_attempt_state_does_not_call_deploy(env):
    (env.state / "fail").mkdir()                             # printf > fail не пройдёт
    assert env.run() == 1
    assert "не записать" in env.one_line()
    assert env.run() == 0
    assert env.sudo_calls() == []


def test_attempt_is_recorded_before_the_call(env):
    # tires-deploy убит посреди (перезагрузка): попытка уже записана, следующий прогон ждёт паузу
    assert env.run(FAKE_DEPLOY_RC=1) == 0
    line = (env.state / "fail").read_text().split()
    assert line[:2] == [NEW, "1"] and int(line[2]) == NOW + 600


def test_stalled_master_alerts_once(env):
    env.token()
    env.respond(checks=[check_run(status="queued", conclusion=None, done=None)])
    assert env.run() == 0
    env.now += 181 * 60
    assert env.run() == 1
    assert "не выложен уже 181 мин" in env.one_line()
    env.now += 2 * 60
    assert env.run() == 0
    assert env.sudo_calls() == []


def test_without_token_checks_are_asked_every_5_minutes(env):
    env.respond(checks=[check_run(status="in_progress", conclusion=None, done=None)])
    assert env.run() == 0
    assert env.run() == 0
    assert "спрошу снова через 5 мин" in env.one_line()
    assert sum("/check-runs" in u for u in env.urls()) == 1
    env.respond(checks=[check_run()])
    env.now += 5 * 60
    assert env.run() == 0
    assert len(env.sudo_calls()) == 1


def test_unopenable_deploy_lock_skips(env):
    (env.tmp / "lock").chmod(0)
    try:
        assert env.run() == 0
        assert "не открыть замок" in env.one_line()
        assert env.sudo_calls() == []
    finally:
        (env.tmp / "lock").chmod(0o644)


def test_busy_deploy_lock_skips(env):
    assert env.run(FAKE_DEPLOY_LOCK_BUSY=1) == 0
    assert "идёт выкладка" in env.one_line()
    assert env.sudo_calls() == []


def test_own_lock_busy_skips(env):
    assert env.run(FAKE_OWN_LOCK_BUSY=1) == 0
    assert "другой прогон" in env.one_line()
    assert env.urls() == []


def test_token_goes_to_stdin_not_argv_or_log(env):
    env.token()
    assert env.run() == 0
    stdin = (env.fake / "curl-stdin").read_text()
    assert f'header = "Authorization: Bearer {TOKEN}"' in stdin
    assert TOKEN not in (env.fake / "curl-urls").read_text()
    assert TOKEN not in env.out + env.err
    assert TOKEN not in (env.state / "last-deploy.log").read_text()


def test_no_token_no_auth_header(env):
    assert env.run() == 0
    assert "Authorization" not in (env.fake / "curl-stdin").read_text()


@pytest.mark.parametrize("bad", ['abc"\ninclude = "/etc/shadow', "short", "x" * 20 + " y"])
def test_bad_token_alerts_once_and_does_nothing(env, bad):
    env.token(bad)
    assert env.run() == 1
    assert "неверном формате" in env.one_line()
    assert env.run() == 0
    assert env.urls() == [] and env.sudo_calls() == []


@pytest.mark.parametrize("var,value", [
    ("TIRES_PULL_REPO", "evil/repo?x=1"),
    ("TIRES_PULL_REPO", "../x"),
    ("TIRES_PULL_CHECK", 'tests"; rm -rf /'),
    ("TIRES_PULL_MIN_AGE_MIN", "1e3"),
    ("TIRES_PULL_MIN_AGE_MIN", "99999"),
])
def test_bad_settings_alert_once(env, var, value):
    assert env.run(**{var: value}) == 1
    assert "неверные настройки" in env.one_line()
    assert env.run(**{var: value}) == 0
    assert env.urls() == [] and env.sudo_calls() == []


def test_garbage_check_json_does_nothing(env):
    (env.fake / "checks.body").write_text("not json")
    assert env.run() == 0
    assert "не разобран" in env.one_line()
    assert env.sudo_calls() == []


def test_custom_check_name_is_encoded(env):
    env.respond(checks=[check_run(name="pytest")])
    assert env.run(TIRES_PULL_CHECK="pytest") == 0
    assert any("check_name=pytest&" in u for u in env.urls())
    assert len(env.sudo_calls()) == 1


def test_sudoers_rule_allows_only_deploy_and_sha():
    # sudo сверяет аргументы с регулярным выражением (POSIX ERE, вся строка аргументов через пробел)
    import re
    line = [l for l in (ROOT / "deploy" / "sudoers-tires-pull").read_text().splitlines() if l and not l.startswith("#")]
    assert line == ["tires-pull ALL=(root) NOPASSWD: /usr/local/sbin/tires-deploy ^deploy [0-9a-f]{40}$"]
    rx = re.compile(r"^deploy [0-9a-f]{40}$")
    assert rx.fullmatch("deploy " + NEW)
    for bad in ("rollback", "timers", "status", "deploy", f"deploy {NEW} x", f"deploy {NEW[:-1]}",
                f"deploy {NEW.upper()}", f"x deploy {NEW}", f"deploy  {NEW}"):
        assert not rx.fullmatch(bad), bad


def test_pull_timer_passes_tires_deploy_timer_rules():
    # иначе после установки каждая выкладка падала бы с кодом 5 на этом таймере
    import re
    text = (ROOT / "deploy" / "tires-deploy.sh").read_text(encoding="utf-8")
    keys = re.search(r"^TIMER_KEYS='(.*)'$", text, re.M).group(1)
    timer = (ROOT / "deploy" / "tires-pull.timer").read_bytes()
    assert all(b == 9 or 32 <= b <= 126 for b in timer if b != 10)
    for line in timer.decode().splitlines():
        assert re.fullmatch(keys, line), line
