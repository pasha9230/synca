"""Ожидание тишины перед выкладкой (deploy/tires-deploy.sh, wait_quiet) на заглушках systemctl/date/sleep.

Скрипт целиком не запускается (он ставит свой PATH и работает с /opt): из него берутся функции
log, die, busy, next_cycle_epoch, wait_quiet и константы, заглушки подменяют команды функциями bash.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "tires-deploy.sh"
STUBS = ROOT / "tests" / "fixtures" / "deploy_wait" / "stubs.sh"
T0 = 1_800_000_000
MIN = 60

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="нужен bash")


def _extract():
    text = SCRIPT.read_text(encoding="utf-8")
    parts = []
    for name in ("CYCLE_GAP_MIN", "WAIT_MAX_MIN", "NEXT_UNKNOWN_MAX_MIN"):
        m = re.search(rf"^{name}=\d+", text, re.M)
        assert m, name
        parts.append(m.group(0))
    m = re.search(r"^die\(\) \{.*\}$", text, re.M)
    assert m
    parts.append(m.group(0))
    for name in ("log", "busy", "next_cycle_epoch", "wait_quiet"):
        m = re.search(rf"^{name}\(\) \{{.*?^\}}$", text, re.M | re.S)
        assert m, name
        parts.append(m.group(0))
    return "\n".join(parts)


def run_wait(tmp_path, **scenario):
    clock = tmp_path / "clock"
    clock.write_text(str(T0))
    env_lines = [f"{k}={v}" for k, v in scenario.items()]
    program = "\n".join([
        "set -euo pipefail",
        "shopt -s inherit_errexit 2>/dev/null || true",  # bash 3.2 на macOS его не знает
        f"FAKE_CLOCK={clock}", f"FAKE_T0={T0}", *env_lines,
        f"source {STUBS}",
        _extract(),
        "LOGFILE=/dev/null",
        'wait_quiet; echo "ТИШИНА t=$(fake_t)"',
    ])
    # ~3 s alone; the clock is fake, so the limit only guards a hang: 30 s was hit under a loaded machine (all cores busy with -n auto)
    p = subprocess.run(["bash", "-c", program], capture_output=True, text=True, timeout=120)
    return p.returncode, p.stdout + p.stderr


def quiet_at(out):
    m = re.search(r"ТИШИНА t=(\d+)", out)
    return int(m.group(1)) if m else None


def test_cycle_running_and_next_time_empty_waits_for_cycle_end(tmp_path):
    # (а) ошибка 2026-10-20: цикл идёт, таймер не отдаёт время — раньше СТОП, теперь ждём конца цикла.
    # Пустое время во время цикла заглушка отдаёт сама (как systemd); NEXT_EMPTY_UNTIL — для наглядности.
    rc, out = run_wait(tmp_path, CYCLE_BUSY_UNTIL=7 * MIN, NEXT_EMPTY_UNTIL=7 * MIN, NEXT_AT=60 * MIN)
    assert rc == 0, out
    assert quiet_at(out) == 7 * MIN
    assert "идёт tires-cycle, жду" in out
    assert "СТОП" not in out


def test_next_time_unknown_three_minutes_then_known_continues(tmp_path):
    # (б) ничего не идёт, времени нет 3 минуты, потом появилось
    rc, out = run_wait(tmp_path, NEXT_EMPTY_UNTIL=3 * MIN, NEXT_AT=60 * MIN)
    assert rc == 0, out
    assert quiet_at(out) == 3 * MIN
    assert out.count("время следующего цикла пока неизвестно, жду") == 3


def test_next_time_unknown_over_five_minutes_stops(tmp_path):
    # (в) ничего не идёт, времени нет больше 5 минут — СТОП с прежним текстом, код 1
    rc, out = run_wait(tmp_path, NEXT_EMPTY_UNTIL=10 * MIN, NEXT_AT=60 * MIN)
    assert rc == 1, out
    assert "СТОП: не удалось узнать время следующего цикла (systemctl show tires-cycle.timer: '', active)" in out
    assert "выкладку не начинаю" in out
    assert out.count("время следующего цикла пока неизвестно, жду") == 5
    assert "ТИШИНА" not in out


def test_unknown_counter_resets_when_time_known_again(tmp_path):
    # пусто 4 мин, 1 мин время известно (цикл скоро, ждём), снова пусто 4 мин, потом цикл через час —
    # счёт начинается заново, всего 8 минут без времени не дают СТОП
    rc, out = run_wait(tmp_path, NEXT_EMPTY_UNTIL=4 * MIN, NEXT_EMPTY2=f"{5 * MIN}-{9 * MIN}", NEXT_AT=3 * MIN,
                       NEXT_AT_LATE=f"{9 * MIN}:{70 * MIN}")
    assert rc == 0, out
    assert quiet_at(out) == 9 * MIN
    assert out.count("время следующего цикла пока неизвестно, жду") == 8


def test_counter_does_not_include_busy_time(tmp_path):
    # пусто 12 мин; 3 мин тихо, с 3-й по 8-ю идёт tires-orders, потом ещё 4 мин тихо — счёт заново, не СТОП
    # (без сброса на «занято» было бы 3 + 4 мин тишины, а с t=0 — СТОП на 8-й минуте)
    rc, out = run_wait(tmp_path, ORDERS_FROM=3 * MIN, ORDERS_BUSY_UNTIL=8 * MIN, NEXT_EMPTY_UNTIL=12 * MIN,
                       NEXT_AT=60 * MIN)
    assert rc == 0, out
    assert quiet_at(out) == 12 * MIN
    assert "идёт tires-orders, жду" in out
    assert out.count("время следующего цикла пока неизвестно, жду") == 7


def test_the_stock_pass_is_waited_for_like_the_orders(tmp_path):
    # проверка остатков между циклами (tires-stock, 2026-10-24) идёт первые 3 минуты: выкладка ждёт её конца
    rc, out = run_wait(tmp_path, STOCK_FROM=0, STOCK_BUSY_UNTIL=3 * MIN, NEXT_AT=40 * MIN)
    assert rc == 0, out
    assert quiet_at(out) == 3 * MIN
    assert out.count("идёт tires-stock, жду") == 3


def test_usual_case_no_wait(tmp_path):
    # (г) ничего не идёт, до цикла 40 минут
    rc, out = run_wait(tmp_path, NEXT_AT=40 * MIN)
    assert rc == 0, out
    assert quiet_at(out) == 0
    assert "жду" not in out


def test_gap_is_five_minutes(tmp_path):
    # зазор 5 минут (слово владельца 2026-10-21): за 6 минут до цикла выкладка начинается, за 4 ждёт
    rc, out = run_wait(tmp_path, NEXT_AT=6 * MIN)
    assert rc == 0 and quiet_at(out) == 0 and "жду" not in out, out
    rc, out = run_wait(tmp_path, NEXT_AT=4 * MIN, CYCLE_FROM=4 * MIN, CYCLE_BUSY_UNTIL=8 * MIN, NEXT_AT_AFTER=70 * MIN)
    assert rc == 0 and quiet_at(out) == 8 * MIN and "до цикла 4 мин, жду" in out, out


def test_cycle_soon_waits_as_before(tmp_path):
    # (д) до цикла 3 мин (зазор 5): ждём; цикл идёт с 3-й по 8-ю минуту (время пустое), потом следующий через час
    rc, out = run_wait(tmp_path, NEXT_AT=3 * MIN, CYCLE_FROM=3 * MIN, CYCLE_BUSY_UNTIL=8 * MIN,
                       NEXT_AT_AFTER=70 * MIN)
    assert rc == 0, out
    assert quiet_at(out) == 8 * MIN
    assert "до цикла 3 мин, жду его окончания" in out
    assert "идёт tires-cycle, жду" in out
    assert "неизвестно" not in out


def test_cycle_soon_never_quiet_hits_overall_limit(tmp_path):
    # потолок WAIT_MAX_MIN не изменился: цикл всё время «вот-вот» — СТОП через 75 минут
    rc, out = run_wait(tmp_path, NEXT_AT=3 * MIN)
    assert rc == 1, out
    assert "СТОП: не дождался тишины за 75 мин" in out
    assert "ТИШИНА" not in out


def test_cycle_running_with_empty_time_still_has_overall_limit(tmp_path):
    # цикл «идёт» дольше потолка при пустом времени — прежний отказ по потолку, а не вечное ожидание
    rc, out = run_wait(tmp_path, CYCLE_BUSY_UNTIL=200 * MIN, NEXT_EMPTY_UNTIL=200 * MIN, NEXT_AT=300 * MIN)
    assert rc == 1, out
    assert "СТОП: не дождался тишины за 75 мин" in out


def test_old_systemd_microseconds_value(tmp_path):
    # systemd без --timestamp=unix: значение в микросекундах
    rc, out = run_wait(tmp_path, NEXT_AT=40 * MIN, OLD_SYSTEMD=1)
    assert rc == 0, out
    assert quiet_at(out) == 0
    rc, out = run_wait(tmp_path, NEXT_AT=3 * MIN, OLD_SYSTEMD=1, CYCLE_FROM=3 * MIN, CYCLE_BUSY_UNTIL=12 * MIN,
                       NEXT_AT_AFTER=70 * MIN)
    assert rc == 0, out
    assert quiet_at(out) == 12 * MIN


def test_a_stock_pass_due_within_the_gap_is_waited_for_too(tmp_path):
    # tails2 Ф6: проход остатков (:10/:25/:55) через 3 минуты: выкладка не начинается посреди него, ждёт его конца (минута)
    rc, out = run_wait(tmp_path, NEXT_AT=40 * MIN, STOCK_NEXT_AT=3 * MIN, STOCK_FROM=3 * MIN, STOCK_BUSY_UNTIL=4 * MIN,
                       STOCK_NEXT_AFTER=18 * MIN)
    assert rc == 0, out
    assert quiet_at(out) == 4 * MIN
    assert "до проверки остатков 3 мин, жду" in out and "идёт tires-stock, жду" in out


def test_a_stock_pass_beyond_the_gap_or_no_stock_timer_does_not_hold_the_deploy(tmp_path):
    rc, out = run_wait(tmp_path, NEXT_AT=40 * MIN, STOCK_NEXT_AT=6 * MIN)
    assert rc == 0 and quiet_at(out) == 0 and "жду" not in out, out
    rc, out = run_wait(tmp_path, NEXT_AT=40 * MIN)                                       # the timer is not installed yet
    assert rc == 0 and quiet_at(out) == 0 and "жду" not in out, out


def test_a_stock_time_long_past_is_not_waited_for(tmp_path):
    # время прохода в прошлом больше чем на 5 минут (таймер не в порядке): не повод не выкладывать
    rc, out = run_wait(tmp_path, NEXT_AT=40 * MIN, STOCK_NEXT_AT=-10 * MIN)
    assert rc == 0 and quiet_at(out) == 0 and "жду" not in out, out
