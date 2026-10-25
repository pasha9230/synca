"""The outside watchdog (.github/workflows/watch.yml): its check step is run here by bash as GitHub runs it
(bash -eo pipefail), with a fake curl that plays the panel and Telegram."""
from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "watch.yml"

FAKE_CURL = """#!/bin/bash
out=""; url=""; args=("$@")
for ((i=0; i<${#args[@]}; i++)); do
  case "${args[$i]}" in -o) out=${args[$((i+1))]} ;; https://*) url=${args[$i]} ;; esac
done
case "$url" in
  *api.telegram.org*) printf '%s\\n' "$*" >> "$FAKE_DIR/telegram.log"; printf '%s' "$(cat "$FAKE_DIR/tg_code")" ;;
  *) [ -f "$FAKE_DIR/down" ] && exit 7
     printf '%s' "$(cat "$FAKE_DIR/body")" > "$out"; printf '%s' "$(cat "$FAKE_DIR/code")" ;;
esac
"""


def check_script() -> str:
    text = WORKFLOW.read_text(encoding="utf-8")
    block = re.search(r"- name: Проверка панели\n.*?        run: \|\n(.*?)\n\n      - name:", text, re.S).group(1)
    return "\n".join(line[10:] for line in block.splitlines()) + "\n"


@pytest.fixture
def watch(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    curl = bindir / "curl"
    curl.write_text(FAKE_CURL)
    curl.chmod(curl.stat().st_mode | stat.S_IEXEC)
    script = tmp_path / "check.sh"
    script.write_text(check_script())
    work = tmp_path / "work"
    work.mkdir()

    def run(code="200", body="ok\n", secrets=True, down=False, tg="200", now_shift=0):
        (tmp_path / "code").write_text(code)
        (tmp_path / "body").write_text(body)
        (tmp_path / "tg_code").write_text(tg)
        down_flag = tmp_path / "down"
        if down:
            down_flag.write_text("")
        elif down_flag.exists():
            down_flag.unlink()
        state = work / "watch-state" / "state"
        if now_shift and state.exists():                               # the hour passes: the last alert is moved back in time
            text = state.read_text()
            stamp = re.search(r"alerted=(\d+)", text).group(1)
            if stamp != "0":
                state.write_text(text.replace("alerted=" + stamp, "alerted=%d" % (int(stamp) - now_shift)))
        output = tmp_path / "github_output"
        output.write_text("")
        env = dict(os.environ, PATH="%s:%s" % (bindir, os.environ["PATH"]), FAKE_DIR=str(tmp_path), GITHUB_OUTPUT=str(output),
                   URL="https://panel.example.com/health", REPEAT_SECONDS="3600",
                   TG_BOT_TOKEN="123:tok" if secrets else "", TG_CHAT_ID="42" if secrets else "")
        done = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)], cwd=str(work), env=env,
                              capture_output=True, text=True, timeout=30)
        log = tmp_path / "telegram.log"
        sent = log.read_text().splitlines() if log.exists() else []
        if log.exists():
            log.unlink()
        return done.returncode, done.stdout + done.stderr, sent, "changed=true" in output.read_text()

    return run


def test_all_fine_sends_nothing_and_stores_nothing(watch):
    code, out, sent, changed = watch()
    assert (code, sent, changed) == (0, [], False) and "в порядке" in out


def test_a_failure_is_told_once_an_hour_and_the_recovery_once(watch):
    code, out, sent, changed = watch(code="503", body="fail cycle chats\n")
    assert code == 0 and changed and len(sent) == 1
    assert "цикл не запускался больше 2 часов" in sent[0] and "чаты Авито не читались" in sent[0]
    assert watch(code="503", body="fail cycle\n")[2] == []                     # within the hour: quiet
    code, out, sent, changed = watch(code="503", body="fail cycle\n", now_shift=3601)
    assert len(sent) == 1 and changed                                         # an hour later: told again
    code, out, sent, changed = watch()
    assert len(sent) == 1 and "снова в порядке" in sent[0] and changed
    assert watch()[2] == []


def test_no_answer_at_all_is_a_failure(watch):
    code, out, sent, changed = watch(down=True)
    assert code == 0 and len(sent) == 1 and "панель не отвечает" in sent[0]


def test_an_odd_page_is_a_failure_and_its_text_is_not_repeated(watch):
    code, out, sent, changed = watch(code="200", body="<html>login $(id)</html>")
    assert len(sent) == 1 and "панель не отвечает (код 200)" in sent[0] and "html" not in sent[0]


def test_without_secrets_only_a_warning_and_the_run_stays_green(watch):
    code, out, sent, changed = watch(code="503", body="fail db\n", secrets=False)
    assert code == 0 and sent == [] and "::warning::Секреты TG_BOT_TOKEN" in out


def test_telegram_refusing_makes_the_run_red_and_it_is_tried_again(watch):
    code, out, sent, changed = watch(code="503", body="fail db\n", tg="401")
    assert code != 0 and len(sent) == 1 and "Telegram не принял" in out
    assert len(watch(code="503", body="fail db\n")[2]) == 1                   # nothing was stored: the next run tells it


def test_a_flapping_panel_is_not_told_more_than_once_an_hour(watch):
    assert len(watch(code="503", body="fail chats\n")[2]) == 1                # told
    assert "снова в порядке" in watch()[2][0]                                 # recovered: told once
    assert watch(code="503", body="fail chats\n")[2] == []                    # fails again within the hour: quiet
    assert watch()[2] == []                                                   # nothing was told about it, so no recovery message
    assert len(watch(code="503", body="fail chats\n", now_shift=3601)[2]) == 1
