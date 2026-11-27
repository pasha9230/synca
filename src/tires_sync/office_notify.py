"""«Мои уведомления» of a person of the office (the owner 2026-11-27): the sound and the notification of the browser (notify.js) of one's
own tasks «Позвонить…» (order_call.sounds). The office does not need a ring for every stage of a fresh order: a new task rings once (on by
default), its repeat every 2 hours while it is open is off by default. Kept per login in app_state (no new table)."""
from __future__ import annotations

from . import store

KEY = "office_notify:%s"              # app_state: "<calls>,<repeat>" as "1"/"0", e.g. "1,0"
DEFAULT = {"calls": True, "repeat": False}


def get(conn, login: str) -> dict:
    """{"calls", "repeat"} of the person; the defaults when nothing was chosen (or the value is odd)."""
    raw = store.get_state(conn, KEY % login) if login else None
    parts = (raw or "").split(",")
    if len(parts) != 2 or any(p not in ("0", "1") for p in parts):
        return dict(DEFAULT)
    return {"calls": parts[0] == "1", "repeat": parts[0] == "1" and parts[1] == "1"}


def calls(conn, login: str, now: str) -> list:
    """The items «Позвонить…» of notify.js for the person, by their choice: switched off — "quiet" (the page still knows them, no ring)."""
    from . import order_call
    mine = get(conn, login)
    items = order_call.sounds(conn, login, now, repeat=mine["repeat"])
    if not mine["calls"]:
        for item in items:
            item["quiet"] = True
    return items


def put(conn, login: str, calls: bool, repeat: bool) -> dict:
    store.set_state(conn, KEY % login, "%d,%d" % (bool(calls), bool(calls) and bool(repeat)))
    return get(conn, login)
