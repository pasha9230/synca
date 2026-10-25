"""Make the freshly built feed of every live account public on our side and record what it contains."""
from __future__ import annotations

import fcntl
import json
import time
from contextlib import contextmanager
from pathlib import Path
from typing import List

from . import connections, store
from .publish import PublishError, promote_feed
from .settings_store import active_settings

EXPECTED_KEY = "expected_removals"


def remember_expected(conn, version: int, count: int) -> None:
    """The owner reviewed and confirmed a change that removes this many ads; shrink checks allow exactly that."""
    store.set_state(conn, EXPECTED_KEY, json.dumps({"version": version, "count": int(count)}))


def expected_removals(conn, version: int) -> int:
    raw = store.get_state(conn, EXPECTED_KEY)
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        return 0
    return int(data.get("count", 0)) if data.get("version") == version else 0


def expected_for(conn, settings) -> int:
    """Removals the owner confirmed for the active version, in ads (the review already counts the «за 4 шт.» twins)."""
    return expected_removals(conn, settings.version)


def clear_expected(conn) -> None:
    store.set_state(conn, EXPECTED_KEY, "")


class _Held:
    def __init__(self, handle):
        self._handle = handle

    def close(self) -> None:
        try:
            fcntl.flock(self._handle, fcntl.LOCK_UN)
        finally:
            self._handle.close()


class FeedBusy(ValueError):
    """Another process holds the feed lock and did not let go within the wait: the work must not go on without it."""


def _lock_state(feed_root):
    """(handle, state): `held` we have the lock, `busy` somebody else has it, `unusable` the lock file cannot be opened at all."""
    path = Path(feed_root) / ".lock"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(str(path), "a")
    except OSError:
        return None, "unusable"
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None, "busy"
    return _Held(handle), "held"


def try_feed_lock(feed_root):
    """Take the feed lock without waiting; None if somebody else holds it (or the lock cannot be opened)."""
    return _lock_state(feed_root)[0]


@contextmanager
def feed_lock(feed_root, wait: float = 300.0, strict: bool = True):
    """Only one process builds or publishes feeds at a time (timer cycle and panel use the same root).
    Waits up to `wait` seconds. If the lock file cannot be used at all the work goes on unlocked; if somebody else still holds the lock after the
    wait, `strict` raises FeedBusy, otherwise the block gets False and must not do the work."""
    deadline = time.time() + wait
    held, state = _lock_state(feed_root)
    while state == "busy" and time.time() < deadline:
        time.sleep(0.2)
        held, state = _lock_state(feed_root)
    if state == "busy" and strict:
        raise FeedBusy("Фид сейчас собирает или публикует другой процесс: повторите позже.")
    try:
        yield state != "busy"
    finally:
        if held is not None:
            held.close()


def publish_live_accounts(conn, feed_root, skip_ids=()) -> List[dict]:
    settings = active_settings(conn)
    expected = expected_for(conn, settings)
    results = []
    for account in connections.list_feed_accounts(conn):
        if not account["live"]:
            results.append({"id": account["id"], "name": connections.display_name(conn, account["id"]), "ok": False, "skipped": True,
                            "message": "публикация выключена, фид у нас не обновлён"})
            continue
        name = connections.display_name(conn, account["id"])
        if account["id"] in skip_ids:
            results.append({"id": account["id"], "name": name, "ok": False, "skipped": True,
                            "message": "фид не собран, публикация пропущена"})
            continue
        folder = Path(feed_root) / account["feed_token"]
        try:
            done = promote_feed(folder, expected_removals=expected)
            feed_bytes = (folder / "avito_feed.xml").read_bytes()
            # a mirror carries the plan only of its new positions: a plan of another feed (an older cycle) is not recorded, and is no error
            if store.has_pending(conn, account["id"]) and (account["feed_mode"] != "mirror" or store.pending_matches(conn, account["id"], feed_bytes)):
                store.commit_pending(conn, account["id"], feed_bytes)
            clear_expected(conn)
            results.append({"id": account["id"], "name": name, "ok": True, "skipped": False,
                            "message": "объявлений %d (было %d)" % (done["new"], done["old"])})
        except (PublishError, store.StateError) as err:
            results.append({"id": account["id"], "name": name, "ok": False, "skipped": False, "message": str(err)})
    return results
