"""The calls to the outside services: repeats of what is safe to repeat, and a breaker per service (the owner 2026-10-24, «надёжность
внешних сервисов»).

Repeats: only a call marked safe (a read: GET, or a POST that only reads); a write is made once, whatever the answer. 5xx and a fast
network failure (a refused or broken connection, while the call is younger than `fast_window`) go again after a growing pause with a random
part; a timeout is never repeated, so a call never takes longer than it did before; 429 waits as `Retry-After` says, within a ceiling.
Breaker: at least BREAKER_FAILURES calls in a row failed (5xx or the network, after the repeats) over at least BREAKER_SPAN seconds (the
owner 2026-10-20: a few misses in a row are no alarm, a long silence is) → the service rests BREAKER_PAUSE seconds after its last failure:
a read then fails at once with ServiceUnavailable (an OSError: every caller already handles the network failing), except one trial call
every TRIAL_EVERY seconds; a write always goes (refused here, a send would be recorded as «unknown, may have gone»); a call that works
closes the breaker. One alert to the owner when it opens (for Avito only the journal: the
chats' own alarm of 15 minutes unread tells it), one note when it closes. 4xx answers mean the service is alive; 429 counts neither way. The state is in the base (app_state `service_health:<service>`): the commands live a minute, the panel longer.
Without bind() (tests, one-off scripts) only the repeats work."""
from __future__ import annotations

import contextlib
import json
import logging
import os
import random
import socket
import sqlite3
import threading
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple

from .transport import ResponseTooLarge

log = logging.getLogger("tires_sync.reliability")

BREAKER_FAILURES = 5
BREAKER_SPAN = 15 * 60
BREAKER_PAUSE = 15 * 60
TRIAL_EVERY = 50             # «a minute» for a timer of every minute with its own jitter (RandomizedDelaySec)
QUIET_TRIP = ("avito",)      # its long failure is already one alarm (tires-chats fails after 15 minutes unread): the opening goes to the journal only
STATE_PREFIX = "service_health:"
STALE_LINE = 7 * 86400
STORE_TIMEOUT = 2.0          # seconds the breaker waits for the base: it never holds a call up for long


@dataclass(frozen=True)
class Policy:
    retries: int = 2                    # repeats after a 5xx or a fast network failure
    base: float = 1.0                   # the first pause; then twice as long each time
    cap: float = 30.0                   # no pause is longer
    jitter: float = 0.5                 # a pause is base·2^k plus up to this share of it, never less
    rate_retries: int = 3               # repeats after 429
    max_retry_after: float = 300.0
    default_retry_after: float = 5.0    # 429 without a usable Retry-After
    fast_window: float = 20.0           # a network failure is repeated only while the call is younger than this
    max_total: Optional[float] = None   # no pause starts that would end the call later than this (seconds from its start)
    fast_attempt: Optional[float] = None  # a failure is repeated only when its attempt took no longer (a blip, not a slow server)
    no_retry: Tuple[int, ...] = ()      # 5xx never repeated (504: the gateway's own timeout)
    total_retries: Optional[int] = None  # repeats of any kind (429, 5xx, the network) together at most


AVITO = Policy()                                                                       # as before: 429 up to 3 times, 5xx up to 2
# a press in the panel has 45 s between its steps, a request up to 30: one repeat in all. A 5xx or a network failure is repeated only
# after a blip: 5 s (the blip) + 3 s (the pause) + 30 s = 38 s at most; a 429 is repeated when its answer and the pause end within 20 s
# (max_total): 20 s + 30 s = 50 s at most, against 30 s before
WEBMIM = Policy(retries=1, base=2.0, rate_retries=1, max_retry_after=10.0, max_total=20.0, fast_attempt=5.0, no_retry=(504,), total_retries=1)
TOCHKI = Policy(retries=1, base=2.0, rate_retries=1, max_retry_after=10.0, max_total=20.0, fast_attempt=5.0, no_retry=(504,), total_retries=1)
NO_RETRY = Policy(retries=0, rate_retries=0)


class ServiceUnavailable(OSError):
    """The service rests after failures in a row: the call was not made."""

    def __init__(self, service: str, until: float, why: str):
        super().__init__("%s: пауза после сбоев подряд до %s (последний сбой: %s)" % (service_label(service), moscow(until), why or "—"))
        self.service = service
        self.until = until


def service_label(service: str) -> str:
    if service == "avito":
        return "Авито"
    if service == "tochki":
        return "4 точки"
    if service.startswith("webmim:"):
        return "WebMIM (%s)" % service.split(":", 1)[1]
    return service


def webmim_service(base_url: str) -> str:
    return "webmim:" + (urllib.parse.urlsplit(base_url).hostname or "?")


def moscow(epoch: float, fmt: str = "%H:%M") -> str:
    return (datetime.utcfromtimestamp(epoch) + timedelta(hours=3)).strftime(fmt)


def retry_after(value: Optional[str], ceiling: float, default: float = 5.0) -> float:
    try:
        wait = float(value) if value is not None else default
    except ValueError:
        return default
    if wait != wait or wait in (float("inf"), float("-inf")):
        return default
    return min(max(wait, 0.0), ceiling)


def backoff(policy: Policy, attempt: int, rand: Callable[[], float]) -> float:
    pause = min(policy.cap, policy.base * (2 ** attempt))
    return min(policy.cap, pause * (1.0 + policy.jitter * max(0.0, min(1.0, rand()))))


def _timeout(err: BaseException) -> bool:
    return isinstance(err, (TimeoutError, socket.timeout)) or "timed out" in str(err).lower()


def _describe(err: BaseException) -> str:
    """The kind of a network failure, never its text (an address may carry a key)."""
    if _timeout(err):
        return "время ожидания ответа вышло"
    if isinstance(err, ConnectionRefusedError):
        return "соединение отклонено"
    return "нет ответа сети (%s)" % type(err).__name__


# --- the process's base and alert ------------------------------------------------------------------------------------------------------

_BOUND: Optional[Tuple[str, Optional[Callable]]] = None
_LOCAL = threading.local()
wall: Callable[[], float] = time.time                 # the breaker's clock (the tests replace it)
default_sleep: Callable[[float], None] = time.sleep  # the clients without a sleep of their own use this one (the tests replace it)


def bind(db_path, alert: Optional[Callable] = None) -> None:
    """The base of the breaker's state and alert(conn, text) for the owner. Called once by the command and by the panel at start."""
    global _BOUND
    _BOUND = (str(db_path), alert) if db_path else None


def owner_alert(box_of: Callable[[], object]) -> Callable:
    """alert(conn, text, kind) through the commands' own alert (cli._alert: the journal, Telegram, the same text not again within 6 hours):
    kind "alert" a failure, "notice" information, "journal" the journal only. box_of() gives the key box; when it cannot, the journal only."""
    def alert(conn, text: str, kind: str = "alert") -> None:
        from . import cli, store
        if kind == "journal":
            store.write_audit(conn, datetime.utcnow().isoformat(timespec="seconds"), "system", "service_pause", {"text": text})
            return
        try:
            box = box_of()
        except Exception:                                   # noqa: BLE001 - no key: the journal still gets the alert
            box = None
        cli._alert(conn, box, text, notice=kind == "notice", recovery=kind == "notice")      # its only notice: «… снова отвечает»
    return alert


def unbind() -> None:
    global _BOUND
    _BOUND = None


@contextlib.contextmanager
def probe():
    """A check the owner pressed: it goes even while the service rests, and its result counts (a success closes the breaker)."""
    old = getattr(_LOCAL, "probe", False)
    _LOCAL.probe = True
    try:
        yield
    finally:
        _LOCAL.probe = old


def _empty() -> dict:
    return {"fails": 0, "series_at": 0.0, "open_until": 0.0, "trial_at": 0.0, "alerted": False, "last_error": "", "last_error_at": 0.0}


def _parse(raw: Optional[str]) -> dict:
    state = _empty()
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    if isinstance(data, dict):
        for key, value in state.items():
            if isinstance(data.get(key), type(value)) or (isinstance(value, float) and isinstance(data.get(key), int)):
                state[key] = type(value)(data[key])
    return state


class _Guard:
    def __init__(self, service: str, db_path: str, alert: Optional[Callable], now: Callable[[], float]):
        self.service = service
        self.key = STATE_PREFIX + service
        self._db = db_path
        self._alert = alert
        self._now = now
        self._seen: Optional[dict] = None

    def _open(self) -> sqlite3.Connection:
        from . import store
        if not os.path.isfile(self._db):                                                   # never makes a base where there is none
            raise sqlite3.OperationalError("no base")
        conn = store.open_db(self._db, timeout=STORE_TIMEOUT)
        conn.isolation_level = None                                                        # its own connection, autocommit: never commits a caller's work
        return conn

    def before(self, safe: bool) -> None:
        """A read is held back while the service rests; a write always goes (a send refused here would be recorded as «unknown, may have
        gone» and never sent again)."""
        try:
            conn = self._open()
            try:
                row = conn.execute("SELECT value FROM app_state WHERE key = ?", (self.key,)).fetchone()
            finally:
                conn.close()
        except sqlite3.Error as err:
            log.warning("breaker %s: state not read (%s)", self.service, type(err).__name__)
            self._seen = None
            return
        self._seen = _parse(row[0] if row else None)
        if safe and self._seen["open_until"] > self._now() and not getattr(_LOCAL, "probe", False) and not self._trial():
            raise ServiceUnavailable(self.service, self._seen["open_until"], self._seen["last_error"])

    def _trial(self) -> bool:
        """True: this call is the trial of the minute (claimed in the base, so two processes do not both try)."""
        try:
            conn = self._open()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT value FROM app_state WHERE key = ?", (self.key,)).fetchone()
                state = _parse(row[0] if row else None)
                now = self._now()
                if state["open_until"] > now and now - state["trial_at"] < TRIAL_EVERY:
                    return False
                state["trial_at"] = now
                conn.execute("INSERT OR REPLACE INTO app_state VALUES (?, ?)", (self.key, json.dumps(state, ensure_ascii=False)))
                conn.execute("COMMIT")
                return True
            finally:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                conn.close()
        except sqlite3.Error as err:
            log.warning("breaker %s: trial not claimed (%s)", self.service, type(err).__name__)
            return False

    def ok(self) -> None:
        if self._seen is not None and not self._seen["fails"] and not self._seen["alerted"]:
            fresh = self._read()                                    # healthy before the call; another process may have failed meanwhile
            if fresh is None or (not fresh["fails"] and not fresh["alerted"]):
                return                                              # healthy and still healthy (or the base busy): nothing is written
        self._change(None)

    def _read(self) -> Optional[dict]:
        """The state as it is now (a read on its own connection, no write lock); None when the base does not answer."""
        try:
            conn = self._open()
            try:
                row = conn.execute("SELECT value FROM app_state WHERE key = ?", (self.key,)).fetchone()
            finally:
                conn.close()
        except sqlite3.Error as err:
            log.warning("breaker %s: state not read (%s)", self.service, type(err).__name__)
            return None
        return _parse(row[0] if row else None)

    def failed(self, why: str) -> None:
        self._change(why)

    def _change(self, why: Optional[str]) -> None:
        text, kind = None, "alert"
        try:
            conn = self._open()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT value FROM app_state WHERE key = ?", (self.key,)).fetchone()
                state = _parse(row[0] if row else None)
                now = self._now()
                if why is None:
                    if state["alerted"]:
                        text, kind = "Tires Sync: %s снова отвечает." % service_label(self.service), "notice"
                    state.update(fails=0, series_at=0.0, open_until=0.0, trial_at=0.0, alerted=False)
                else:
                    if not state["fails"]:
                        state["series_at"] = now
                    state.update(fails=state["fails"] + 1, last_error=why[:200], last_error_at=now)
                    if state["fails"] >= BREAKER_FAILURES and now - state["series_at"] >= BREAKER_SPAN:
                        if state["open_until"] <= now:
                            state["trial_at"] = now                                        # opening: this failure was the try of the minute
                        state["open_until"] = now + BREAKER_PAUSE                          # a failed trial keeps its claim's time
                        if not state["alerted"]:
                            state["alerted"] = True
                            kind = "journal" if self.service in QUIET_TRIP else "alert"
                            text = ("Tires Sync: %s не отвечает: %d сбоев подряд за %d мин (последний: %s). Запросы к нему на паузе, раз в минуту "
                                    "пробный; сообщу, когда заработает." % (service_label(self.service), state["fails"],
                                                                            (now - state["series_at"]) // 60, why))
                conn.execute("INSERT OR REPLACE INTO app_state VALUES (?, ?)", (self.key, json.dumps(state, ensure_ascii=False)))
                conn.execute("COMMIT")
                if text and self._alert is not None:
                    try:
                        self._alert(conn, text, kind)
                    except Exception as err:                         # noqa: BLE001 - the alert never breaks the call
                        log.warning("breaker %s: alert not sent (%s)", self.service, type(err).__name__)
            finally:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                conn.close()
        except sqlite3.Error as err:
            log.warning("breaker %s: state not written (%s)", self.service, type(err).__name__)


def _guard(service: Optional[str]) -> Optional[_Guard]:
    if not service or _BOUND is None:
        return None
    return _Guard(service, _BOUND[0], _BOUND[1], wall)


def call(attempt: Callable[[], Tuple[int, Dict[str, str], bytes]], *, safe: bool, policy: Policy, service: Optional[str] = None,
         sleep: Optional[Callable[[float], None]] = None, rand: Callable[[], float] = random.random,
         clock: Callable[[], float] = time.monotonic,
         answered: Optional[Callable[[int, bytes], bool]] = None, read: Optional[bool] = None) -> Tuple[int, Dict[str, str], bytes]:
    """attempt() makes the request once: (status, headers lower-case, body) or OSError. The last answer is returned as it is (the client
    reads it); OSError is raised after the repeats. Other exceptions of attempt() pass untouched and count neither way.
    `answered(status, body)`: a 5xx that is the service's own answer (a SOAP Fault): not repeated, and the service counts as alive.
    `read`: the call changes nothing, so the pause holds it back (by default: `safe`); a read made once (Avito's retry=False) is still a read."""
    pause_for = sleep if sleep is not None else default_sleep
    guard = _guard(service)
    if guard is not None:
        guard.before(safe if read is None else read)
    start = clock()
    tries = rate = 0

    def blip(took: float) -> bool:
        return policy.fast_attempt is None or took <= policy.fast_attempt

    def spare() -> bool:
        return policy.total_retries is None or tries + rate < policy.total_retries

    def fits(pause: float, limit: Optional[float]) -> bool:
        return limit is None or clock() - start + pause <= limit

    while True:
        began = clock()
        try:
            status, headers, raw = attempt()
        except (ServiceUnavailable, ResponseTooLarge):
            raise
        except OSError as err:
            if safe and tries < policy.retries and spare() and not _timeout(err) and blip(clock() - began):   # a timeout is never repeated
                pause = backoff(policy, tries, rand)
                window = policy.fast_window if policy.max_total is None else min(policy.fast_window, policy.max_total)
                if fits(pause, window):
                    tries += 1
                    pause_for(pause)
                    continue
            if guard is not None:
                guard.failed(_describe(err))
            raise
        if status == 429 and safe and rate < policy.rate_retries and spare():
            pause = retry_after((headers or {}).get("retry-after"), policy.max_retry_after, policy.default_retry_after)
            if fits(pause, policy.max_total):
                rate += 1
                pause_for(pause)
                continue
        down = status >= 500 and not (answered is not None and answered(status, raw))
        if down and safe and tries < policy.retries and spare() and status not in policy.no_retry and blip(clock() - began):
            pause = backoff(policy, tries, rand)
            if fits(pause, policy.max_total):
                tries += 1
                pause_for(pause)
                continue
        if guard is not None:
            if down:
                guard.failed("HTTP %s" % status)
            elif status != 429:
                guard.ok()
        return status, headers, raw


def status(conn, services: Tuple[str, ...] = ("avito", "webmim", "tochki"), now: Optional[float] = None) -> List[dict]:
    """The panel's line: each service with its state (`ok`, `pause`, `trial`: the pause is over, the next call tries), the pause's end and
    the last failure, in Moscow time. `webmim` stands for every WebMIM server the base has a record of (one line «WebMIM» when none)."""
    now = time.time() if now is None else now
    rows = {r[0][len(STATE_PREFIX):]: _parse(r[1]) for r in conn.execute(
        "SELECT key, value FROM app_state WHERE key LIKE ? ORDER BY key LIMIT 50", (STATE_PREFIX + "%",)).fetchall()}
    out: List[dict] = []
    for name in services:
        shown = len(out)
        for key in (sorted(k for k in rows if k.startswith("webmim:")) if name == "webmim" else ([name] if name in rows else [])):
            state = rows[key]
            if name == "webmim" and not state["fails"] and not state["alerted"] and now - state["last_error_at"] > STALE_LINE:
                continue                                                           # a server not failing for a week (an old address) leaves the line
            mode = "pause" if state["open_until"] > now else ("trial" if state["alerted"] else "ok")
            out.append({"service": key, "label": service_label(key), "state": mode,
                        "until": moscow(state["open_until"]) if state["open_until"] > now else "", "fails": state["fails"],
                        "last_error": state["last_error"],
                        "last_error_at": moscow(state["last_error_at"], "%d.%m %H:%M") if state["last_error_at"] else ""})
        if len(out) == shown:
            out.append({"service": name, "label": "WebMIM" if name == "webmim" else service_label(name), "state": "ok", "until": "",
                        "fails": 0, "last_error": "", "last_error_at": ""})
    return out
