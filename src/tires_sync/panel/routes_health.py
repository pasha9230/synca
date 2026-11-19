"""/health for the outside watchdog (.github/workflows/watch.yml): open without a login, so it tells only `ok` or `fail`
with short codes of what is wrong, never a time, a number or a name. Checked: the panel answers and reads the base, the
last cycle is at most 2 hours old, each live feed was published within 3 hours, and chats were read within 15 minutes."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from flask import Response, current_app

from .. import connections
from .common import get_conn, now

CYCLE_MAX = timedelta(hours=2)                  # the cycle runs every hour at :40
CHATS_MAX = timedelta(minutes=15)               # the chats are read every minute
FEED_MAX = timedelta(hours=3)                    # two missed hourly publications
MOSCOW = timedelta(hours=3)                     # run.started is UTC, avito_chat.synced_at is Moscow time


def _stamp(value) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value or "")[:19])
    except ValueError:
        return None


def _stale(last: Optional[datetime], current: datetime, limit: timedelta) -> bool:
    return last is None or current - last > limit


def problems(conn, utc_now: datetime) -> List[str]:
    found = []
    if _stale(_stamp(conn.execute("SELECT MAX(started) FROM run").fetchone()[0]), utc_now, CYCLE_MAX):
        found.append("cycle")
    feed_root = current_app.config.get("FEED_ROOT")
    if feed_root:
        for account in connections.list_feed_accounts(conn):
            if not account["live"]:
                continue
            path = Path(feed_root) / account["feed_token"] / "avito_feed.xml"
            try:
                published = datetime.utcfromtimestamp(path.stat().st_mtime)
            except OSError:
                published = None
            if _stale(published, utc_now, FEED_MAX):
                found.append("feed")
                break
    accounts = [a["id"] for a in connections.list_chat_accounts(conn)]
    if accounts:
        last = conn.execute("SELECT MAX(synced_at) FROM avito_chat WHERE synced_at != '' AND account_id IN (%s)"
                            % ",".join("?" for _ in accounts), accounts).fetchone()[0]
        if _stale(_stamp(last), utc_now + MOSCOW, CHATS_MAX):
            found.append("chats")
    return found


def register(app) -> None:
    @app.route("/health")
    def health():
        try:
            found = problems(get_conn(), now())
        except sqlite3.Error:                                            # a busy or broken base: the watchdog only needs to know
            found = ["db"]
        return Response("fail %s\n" % " ".join(found) if found else "ok\n", status=503 if found else 200,
                        mimetype="text/plain", headers={"Cache-Control": "no-store"})
