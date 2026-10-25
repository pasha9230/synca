"""Generation started from the panel: the owner's run request, the models asked for first, the agent's status.

The agent (wherever it runs) asks the panel every minute whether there is work (`work`) and reports the end of a run
(`finish`); without a request it does nothing. All of it lives in app_state: no schema change."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import subprocess
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from . import photo_library, store

REQUEST_KEY = "image_agent_request"        # {"at": stamp, "scope": "all" | "rush"}: a run the owner asked for
COPIES_KEY = "image_copies_request"        # stamp: copies, crops and cards are due (free, after an approval)
RUSH_KEY = photo_library.RUSH_KEY          # set keys the owner asked for by model, first in the queue
STATUS_KEY = "image_agent_status"          # {"state": running|done|error, "at", "done", "failed", "message"}
SEEN_KEY = "image_agent_seen"              # stamp of the agent's last «is there work»
SCOPES = ("all", "rush")
MAX_RUSH = 50
MAX_MESSAGE = 300
SILENT_MINUTES = 5                         # a request older than this the agent has not picked up: it is off
STUCK_MINUTES = 200                        # a run silent this long is over (longer than the server's RUN_TIMEOUT of 3 h)


def _json(conn, key, default):
    try:
        value = json.loads(store.get_state(conn, key) or "null")
    except ValueError:
        return default
    return value if isinstance(value, type(default)) else default


def _put(conn, key, value) -> None:
    conn.execute("INSERT OR REPLACE INTO app_state VALUES (?, ?)", (key, value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)))


def _ago(now: str, stamp: Optional[str], minutes: float) -> bool:
    """True when `stamp` is missing or more than `minutes` before `now`."""
    try:
        return datetime.fromisoformat(stamp) < datetime.fromisoformat(now) - timedelta(minutes=minutes)
    except (TypeError, ValueError):
        return True


rush_keys = photo_library.rush_keys


def pending_request(conn) -> Optional[dict]:
    req = _json(conn, REQUEST_KEY, {})
    return req if req.get("scope") in SCOPES and isinstance(req.get("at"), str) else None


def request_run(conn, now: str, scope: str) -> None:
    """Ask the agent for a run; a pending «all» is not narrowed to «rush». Commits."""
    if scope not in SCOPES:
        raise ValueError(scope)
    old = pending_request(conn)
    if old and old["scope"] == "all":
        scope = "all"
    _put(conn, REQUEST_KEY, {"at": now, "scope": scope})
    conn.commit()


def request_copies(conn, now: str) -> None:
    _put(conn, COPIES_KEY, now)
    conn.commit()


def _open_job(conn, key: str) -> bool:
    return conn.execute("SELECT 1 FROM image_job WHERE set_key=? AND status IN ('queued','running')", (key,)).fetchone() is not None


def _prune_rush(conn) -> List[str]:
    """Keep the models still on their way: needed, not both frames approved, and a job open or a frame waiting for review
    (out of attempts or out of the queue: dropped, «Сгенерировать» puts them back)."""
    keep = []
    for key in rush_keys(conn):
        row = conn.execute("SELECT state FROM photo_set WHERE key=?", (key,)).fetchone()
        approved = conn.execute("SELECT COUNT(DISTINCT kind) FROM set_frame WHERE set_key=? AND status='approved'", (key,)).fetchone()[0]
        waiting = _open_job(conn, key) or conn.execute("SELECT 1 FROM set_frame WHERE set_key=? AND status='pending'", (key,)).fetchone()
        if row and row[0] == photo_library.STATE_NEEDED and approved < len(photo_library.FRAME_KINDS) and waiting and key not in keep:
            keep.append(key)
    _put(conn, RUSH_KEY, keep[-MAX_RUSH:])
    return keep


def _why_not(conn, key: str, now: str) -> str:
    """Why a needed model got no job."""
    if photo_library.spent_today(conn, now) >= photo_library.DAILY_CAP_USD - 1e-9:
        return "Лимит расходов на сегодня исчерпан."
    for kind in photo_library.FRAME_KINDS:
        if photo_library._has_frame(conn, key, kind, "approved"):
            continue
        if photo_library._has_frame(conn, key, kind, "pending"):
            return "Кадр модели ждёт проверки: одобрите или отклоните его."
        if photo_library.attempts_used(conn, key, kind) >= photo_library.MAX_ATTEMPTS:
            return "У модели кончились попытки (%d из %d)." % (photo_library.MAX_ATTEMPTS, photo_library.MAX_ATTEMPTS)
        break
    return "Оба кадра модели уже одобрены." if photo_library._has_frame(conn, key, "set4", "approved") else "Задание не поставлено: попробуйте позже."


def rush_model(conn, key: str, now: str) -> Tuple[bool, str]:
    """«Сгенерировать» on a model: it goes first, its job is queued and a run of it is asked for. (ok, why not)."""
    row = conn.execute("SELECT state FROM photo_set WHERE key=?", (key,)).fetchone()
    if row is None or row[0] != photo_library.STATE_NEEDED:
        return False, "Модель сейчас не нужна (нет в наличии или выбыла)."
    if photo_library.queue_stopped(conn):
        return False, "Очередь остановлена: сначала запустите её."
    keys = [k for k in rush_keys(conn) if k != key] + [key]
    _put(conn, RUSH_KEY, keys[-MAX_RUSH:])
    photo_library.enqueue(conn, now)                         # commits
    if not _open_job(conn, key):
        _put(conn, RUSH_KEY, [k for k in rush_keys(conn) if k != key])
        conn.commit()
        return False, _why_not(conn, key, now)
    request_run(conn, now, "rush")
    return True, ""


def after_approve(conn, key: str, now: str) -> None:
    """An approved frame: copies are due; a model asked for by name gets its next frame at once."""
    request_copies(conn, now)
    after_regenerate(conn, key, now)


def after_regenerate(conn, key: str, now: str) -> None:
    """A model asked for by name gets its new or next frame at once (others wait for a press)."""
    if key in rush_keys(conn) and _open_job(conn, key):
        request_run(conn, now, "rush")


def work(conn, now: str) -> dict:
    """The agent's «is there work»: the pending request and copies; marks the run as started. Commits."""
    _put(conn, SEEN_KEY, now)
    _prune_rush(conn)
    req = pending_request(conn)
    copies_at = store.get_state(conn, COPIES_KEY) or ""
    out = {"run": req is not None, "scope": req["scope"] if req else "", "request_at": req["at"] if req else "",
           "copies": bool(copies_at), "copies_at": copies_at}
    if out["run"]:                                           # copies alone are quiet: they do not hide the last run's result
        _put(conn, STATUS_KEY, {"state": "running", "at": now, "done": 0, "failed": 0, "message": ""})
    conn.commit()
    return out


def finish(conn, now: str, request_at: str, copies_at: str, done: int, failed: int, message: str) -> None:
    """End of a run: clears the request and the copies it was given (not newer ones). Commits."""
    req = pending_request(conn)
    if req and request_at and req["at"] == request_at:
        conn.execute("DELETE FROM app_state WHERE key=?", (REQUEST_KEY,))
    if copies_at and store.get_state(conn, COPIES_KEY) == copies_at:
        conn.execute("DELETE FROM app_state WHERE key=?", (COPIES_KEY,))
    message = (message or "")[:MAX_MESSAGE]
    if request_at or message:
        _put(conn, STATUS_KEY, {"state": "error" if message else "done", "at": now, "done": done, "failed": failed, "message": message})
    _put(conn, SEEN_KEY, now)
    conn.commit()


def status(conn) -> dict:
    return _json(conn, STATUS_KEY, {})


def status_view(conn, now: str) -> dict:
    """What «Генерация» shows: {state, text, at, silent}."""
    st, req = status(conn), pending_request(conn)
    running = st.get("state") == "running" and not _ago(now, st.get("at"), STUCK_MINUTES)
    silent = bool(req) and not running and _ago(now, req["at"], SILENT_MINUTES) and _ago(now, store.get_state(conn, SEEN_KEY), SILENT_MINUTES)
    if running:
        return {"state": "running", "text": "Идёт генерация", "at": st.get("at"), "silent": False}
    if req and st.get("state") == "error" and (st.get("at") or "") >= req["at"]:     # the run asked for failed: say why, it is retried
        return {"state": "error", "text": st.get("message") or "Генерация прервана", "at": st.get("at"), "silent": False}
    if req:
        text = "Генерация запрошена, ждёт агента (он проверяет раз в минуту)"
        if silent:
            text = "Агент не забирает запрос: он выключен или нет связи с панелью"
        return {"state": "waiting", "text": text, "at": req["at"], "silent": silent}
    if st.get("state") in ("done", "error"):
        text = "Последний запуск: готово %d, ошибок %d" % (st.get("done") or 0, st.get("failed") or 0)
        if st.get("message"):
            text += ". " + st["message"]
        return {"state": st["state"], "text": text, "at": st.get("at"), "silent": False}
    return {"state": "idle", "text": "Генерацию запускают кнопки «Сгенерировать»", "at": None, "silent": False}


# --- the agent on the server: `python -m tires_sync photo-agent-tick` every minute (deploy/tires-photo-agent.timer) ---

SERVER_TOKEN_KEY = "image_agent_token_server"      # sha256 of the one-time token of the run going on now (none between runs)
SECRET_KIND = "openai"                             # «Подключения → OpenAI (фото)»: secrets api_key and proxy
PROXY = re.compile(r"^(http|socks5)://(?:[^\s/@]+@)?[A-Za-z0-9.\-]+:[0-9]{1,5}/?$")
RUN_TIMEOUT = 3 * 3600
REQUEST_MAX_AGE_MINUTES = 24 * 60          # an older request (the timer was off) is dropped, not run
FAILS_KEY = "image_agent_failures"         # {"for": the request it is about, "n": runs of it that died in a row}
MAX_FAILED_RUNS = 3                        # then the request is dropped: a press is needed again


def check_proxy(value: str) -> str:
    """The proxy for OpenAI as entered: http://host:port or socks5://host:port, optionally user:password@; ValueError otherwise."""
    value = value.strip()
    if not PROXY.match(value):
        raise ValueError("Прокси: адрес вида socks5://127.0.0.1:1080 или http://логин:пароль@хост:порт.")
    return value


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def server_token_digest(conn) -> str:
    return store.get_state(conn, SERVER_TOKEN_KEY) or ""


def _drop(conn, now: str, message: str, request_at: str, copies_at: str) -> None:
    """Take this request and these copies off (no more retries; a newer press stays) and say why."""
    req = pending_request(conn)
    if req and request_at and req["at"] == request_at:
        conn.execute("DELETE FROM app_state WHERE key=?", (REQUEST_KEY,))
    if copies_at and store.get_state(conn, COPIES_KEY) == copies_at:
        conn.execute("DELETE FROM app_state WHERE key=?", (COPIES_KEY,))
    conn.execute("DELETE FROM app_state WHERE key=?", (FAILS_KEY,))
    _put(conn, STATUS_KEY, {"state": "error", "at": now, "done": 0, "failed": 0, "message": message[:MAX_MESSAGE]})
    conn.commit()


def _lock(agent_dir: str):
    """One tick at a time (a manual run next to the timer): the open lock file, or None when another tick holds it."""
    os.makedirs(agent_dir, exist_ok=True)
    handle = open(os.path.join(agent_dir, ".tick.lock"), "a")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def tick_server(conn, load_box, agent_bin: str, agent_dir: str, now: str, run=subprocess.run, timeout: int = RUN_TIMEOUT) -> int:
    """One timer tick: nothing asked = return at once (the binary is not started, the master key is not read). Asked: a one-time
    token, then the agent gets the token, the OpenAI key and the proxy on stdin; the token is revoked after the run whatever
    happens. A run that dies is retried next minute, at most MAX_FAILED_RUNS times."""
    req = pending_request(conn)
    if req and _ago(now, req["at"], REQUEST_MAX_AGE_MINUTES):
        _drop(conn, now, "Запрос генерации старше суток снят: нажмите «Сгенерировать» снова.", req["at"], "")
        return 0
    copies_at = store.get_state(conn, COPIES_KEY) or ""
    if req is None and not copies_at:
        return 0
    lock = _lock(agent_dir)
    if lock is None:
        return 0
    try:
        return _tick_locked(conn, load_box, agent_bin, agent_dir, now, run, timeout, req["at"] if req else "", copies_at)
    finally:
        lock.close()


def _tick_locked(conn, load_box, agent_bin, agent_dir, now, run, timeout, request_at, copies_at) -> int:
    # the failure count follows the request (an approval in between changes the copies, not the run that keeps dying)
    asked = request_at or "copies|" + copies_at
    base = (store.get_state(conn, "public_base") or "").rstrip("/")
    if not base:
        _drop(conn, now, "Агент не запущен: не задан адрес панели (public_base).", request_at, copies_at)
        return 1
    box = load_box()
    if box is None:
        _drop(conn, now, "Агент не запущен: мастер-ключ недоступен.", request_at, copies_at)
        return 1
    from . import connections
    from .secretbox import SecretKeyError
    try:
        key = connections.get_secret(conn, box, SECRET_KIND, 0, "api_key") or ""
        proxy = connections.get_secret(conn, box, SECRET_KIND, 0, "proxy") or ""
    except SecretKeyError:
        _drop(conn, now, "Агент не запущен: ключ OpenAI не читается (мастер-ключ).", request_at, copies_at)
        return 1
    token = secrets.token_urlsafe(32)
    store.set_state(conn, SERVER_TOKEN_KEY, _digest(token))
    env = {"PATH": "/usr/bin:/bin", "HOME": agent_dir, "TIRES_PANEL_URL": base, "TIRES_AGENT_DIR": agent_dir}
    env.update({k: v for k, v in os.environ.items() if k in ("TIRES_IMAGE_MODEL", "TIRES_PRICE_IN", "TIRES_PRICE_OUT")})
    failure, before = "", status(conn)
    try:
        res = run([agent_bin, "tick", "--stdin-secrets"], input=json.dumps({"token": token, "openai_key": key, "proxy": proxy}),
                  text=True, env=env, timeout=timeout)
        if res.returncode != 0:
            failure = "агент завершился с ошибкой (код %d), подробности в журнале службы" % res.returncode
    except subprocess.TimeoutExpired:
        failure = "агент не уложился в %d ч и остановлен" % (timeout // 3600)
    except OSError as err:
        failure = "агент не запускается (%s)" % type(err).__name__
    finally:
        conn.execute("DELETE FROM app_state WHERE key=?", (SERVER_TOKEN_KEY,))
        conn.commit()
    st = status(conn)
    reported = st != before and st.get("state") in ("done", "error")
    if not failure or reported:
        conn.execute("DELETE FROM app_state WHERE key=?", (FAILS_KEY,))
        conn.commit()
        return 1 if failure else 0
    fails = _json(conn, FAILS_KEY, {})
    n = (fails.get("n", 0) if fails.get("for") == asked else 0) + 1
    if n >= MAX_FAILED_RUNS:
        _drop(conn, now, "Генерация прервана %d раза подряд (%s): нажмите «Сгенерировать» снова." % (n, failure), request_at, copies_at)
        return 1
    _put(conn, FAILS_KEY, {"for": asked, "n": n})
    finish(conn, now, "", "", 0, 0, "Генерация прервана: %s; повтор через минуту (%d из %d)." % (failure, n, MAX_FAILED_RUNS))
    return 1
