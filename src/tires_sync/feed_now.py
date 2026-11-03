"""«Собрать фид сейчас» (the owner 2026-11-03): the usual hourly cycle with publication, once, now, in a process of its own (the same
`run-cycle --fetch --publish` as tires-cycle.service: the same lock, the same checks before publishing). Refused while a cycle is
building or publishing, within minutes after one started, while the one started by the button still runs, and just before the
hourly cycle (it would wait for the lock and fail)."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from typing import Optional

from . import store
from .price_push import _try_lock, start
from .publishing import try_feed_lock

CYCLE_STARTED_KEY = "cycle_started_at"   # UTC; written by every run-cycle (not the stock pass) and by the button itself
STATE_KEY = "feed_now"                   # {"at", "pid", "author"} of the last press that started a cycle
GAP = timedelta(minutes=5)
HOURLY_MINUTE = 40                       # tires-cycle.timer: *:40 (and 06:45), up to 2 min late
BEFORE_HOURLY = 10                       # minutes before it the button is refused
CYCLE_LOCK = ".cycle.lock"               # held by every run-cycle from its start to its very end (feed_lock covers only the build)


def hold_cycle(feed_root):
    """Taken by run-cycle for all its run (the feeds, then the reports, the messages, the stock and the prices on Avito). None when another
    cycle holds it: that cycle goes on as before (the feed lock still orders the builds)."""
    return _try_lock(feed_root, CYCLE_LOCK)


def cycle_running(feed_root) -> bool:
    held = _try_lock(feed_root, CYCLE_LOCK)
    if held is None:
        return True
    held.close()
    return False


def refusal(conn, feed_root, now_utc: datetime) -> str:
    """Why the button may not start a cycle now; empty when it may."""
    raw = store.get_state(conn, CYCLE_STARTED_KEY)
    try:
        started = datetime.fromisoformat(raw) if raw else None
    except ValueError:
        started = None
    if started is not None and timedelta(0) <= now_utc - started < GAP:
        return "Цикл запускался %d мин назад: повторите через несколько минут." % ((now_utc - started).seconds // 60)
    if cycle_running(feed_root):
        return "Цикл ещё идёт (сборка, отчёты, отправка на Авито): дождитесь конца."
    moscow = now_utc + timedelta(hours=3)
    if HOURLY_MINUTE - BEFORE_HOURLY <= moscow.minute <= HOURLY_MINUTE + 2 or (moscow.hour == 6 and 35 <= moscow.minute <= 47):
        return "Скоро плановый цикл (в :%02d): он соберёт и опубликует фид сам." % HOURLY_MINUTE
    held = try_feed_lock(feed_root)
    if held is None:
        return "Фид сейчас собирает или публикует другой процесс: повторите через несколько минут."
    held.close()
    return ""


def start_cycle(conn, cfg: dict, now_utc: datetime, author: str, popen=None) -> str:
    """Start the cycle if allowed; the text for the owner. `cfg`: DB_PATH, UNLOAD_PATH, REFERENCE_PATH, FEED_ROOT, SECRET_KEY_FILE."""
    now_iso = now_utc.isoformat(timespec="seconds")
    if not cfg.get("FEED_ROOT"):
        return "Каталог фидов не задан (TIRES_FEED_ROOT): цикл не запущен."
    why = refusal(conn, cfg["FEED_ROOT"], now_utc)
    if why:
        store.write_audit(conn, now_iso, author, "feed_now_refused", {"why": why})
        conn.commit()
        return "Фид не собирается: " + why
    args = [sys.executable, "-m", "tires_sync", "run-cycle", "--fetch", "--publish", "--db", str(cfg["DB_PATH"]),
            "--unload", str(cfg["UNLOAD_PATH"]), "--reference", str(cfg["REFERENCE_PATH"]), "--feed-root", str(cfg["FEED_ROOT"])]
    if cfg.get("SECRET_KEY_FILE"):
        args += ["--key-file", str(cfg["SECRET_KEY_FILE"])]
    before = store.get_state(conn, CYCLE_STARTED_KEY)
    store.set_state(conn, CYCLE_STARTED_KEY, now_iso)               # a second press before the process writes it is refused too
    pid = start(args, popen) if popen is not None else start(args)
    if pid is None:
        store.set_state(conn, CYCLE_STARTED_KEY, before or "")       # no cycle started: the 5 minutes do not hold
        store.write_audit(conn, now_iso, author, "feed_now_refused", {"why": "процесс не запустился"})
        conn.commit()
        return "Фид не собирается: процесс цикла не запустился."
    store.set_state(conn, STATE_KEY, json.dumps({"at": now_iso, "pid": pid, "author": author}, ensure_ascii=False))
    store.write_audit(conn, now_iso, author, "feed_now_started", {"pid": pid})
    conn.commit()
    return "Цикл запущен: фид соберётся и опубликуется в течение нескольких минут (как плановый, с теми же проверками)."


def last(conn, feed_root) -> Optional[dict]:
    try:
        data = json.loads(store.get_state(conn, STATE_KEY) or "{}")
    except ValueError:
        return None
    if not data.get("at"):
        return None
    data["running"] = bool(feed_root) and cycle_running(feed_root)
    return data
