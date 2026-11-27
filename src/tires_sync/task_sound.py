"""The sound of the important tasks (the owner 2026-11-28): an open important task rings the open panel in the working day only — the
office at most OFFICE_PER_DAY times a calendar day per person, the panel of a point at most POINT_PER_DAY times a day per point, GAP_HOURS
apart, while the task is open. The hours and the gap are those of «Заказ должен был приехать» (arrival_remind: 10:00–19:00 Moscow, 3 hours).

Important: the office — its open tasks titled «Важное · …» (the mark of the system's urgent tasks: «Позвонить…» of order_call, «Google-таблица
не приняла заказ» of order_sheet, the buyer and «Водитель не забрал» of oth_point_return); a plain task due or overdue is not an alert (tasks.py,
the owner 2026-10-31). The point — the open tasks of the office to its managers whose day has come (the red «!» of «Сводки», point_notify).

How: a ring is a moment kept per scope and day in app_state (RING_KEY, no new table), taken in one statement (two tabs never make three).
notify.js rings when a key is new: a task rung by the last ring has its key «…-<day>-<n>»; a task newer than it waits quietly («…-wait») for
the next ring the limit allows (none left today: tomorrow from 10:00). Nothing here sends anything to a client, Avito or WebMIM."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import List, Optional

from .arrival_remind import GAP_HOURS, HOURS, in_hours  # noqa: F401 - the same working day and gap

OFFICE_PER_DAY = 2
POINT_PER_DAY = 3
RING_KEY = "task_sound:%s:%s"            # app_state, per scope («office:<login>», «point:<id>») and day: {"shown": [the moments]}
POINT_SWITCH = "task_sound_point:%d"     # app_state "0"/"1": the point's own switch of the sound of the office's tasks (none: on)
IMPORTANT = "Важное"


def _shown(conn, key: str) -> tuple:
    row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None, []
    try:
        return row[0], [str(x)[:19] for x in (json.loads(row[0]).get("shown") or [])]
    except (ValueError, AttributeError):
        return row[0], []


def may_ring(shown: List[str], now: str, per_day: int) -> bool:
    """A new ring now: in the working hours, fewer than `per_day` today, the last one GAP_HOURS ago and more."""
    if not in_hours(now) or len(shown) >= per_day:
        return False
    return not shown or datetime.fromisoformat(now[:19]) >= datetime.fromisoformat(shown[-1]) + timedelta(hours=GAP_HOURS)


def rings(conn, scope: str, now: str, per_day: int, due: bool) -> List[str]:
    """Today's rings of the scope; one more taken now when `due` (an open important task waits for one) and may_ring. Committed when taken."""
    key = RING_KEY % (scope, now[:10])
    old, shown = _shown(conn, key)
    if due and may_ring(shown, now, per_day):
        value = json.dumps({"shown": shown + [now[:19]]})
        if old is None:
            made = conn.execute("INSERT OR IGNORE INTO app_state (key, value) VALUES (?, ?)", (key, value)).rowcount == 1
        else:
            made = conn.execute("UPDATE app_state SET value = ? WHERE key = ? AND value = ?", (value, key, old)).rowcount == 1
        conn.commit()
        shown = shown + [now[:19]] if made else _shown(conn, key)[1]      # another tab was first: its ring stands
    return shown


def keyed(conn, scope: str, now: str, per_day: int, items: List[dict]) -> List[dict]:
    """The items of notify.js of the open important tasks (each with "base" — the task's own key — and "created"): rung by the last ring
    or waiting quietly for the next one. The "base" and "created" are left out of what is returned."""
    shown = rings(conn, scope, now, per_day, due=bool(items))             # an open task: it rings again while open, as the limit allows
    out = []
    for item in items:
        item = dict(item)
        base, created = item.pop("base"), item.pop("created", "")
        if shown and str(created)[:19] <= shown[-1]:
            item["key"] = "%s-%s-%d" % (base, now[:10].replace("-", ""), len(shown))
        else:
            item["key"], item["quiet"] = "%s-wait" % base, True
        out.append(item)
    return out


def point_on(conn, point_id: Optional[int]) -> bool:
    """The point's own switch «Звук задач офиса» (none chosen: on)."""
    if point_id is None:
        return False
    row = conn.execute("SELECT value FROM app_state WHERE key = ?", (POINT_SWITCH % int(point_id),)).fetchone()
    return row is None or row[0] != "0"


def point_put(conn, point_id: int, on: bool) -> bool:
    conn.execute("INSERT OR REPLACE INTO app_state (key, value) VALUES (?, ?)", (POINT_SWITCH % int(point_id), "1" if on else "0"))
    conn.commit()
    return point_on(conn, point_id)


def point_items(conn, point_id: int, now: str) -> List[dict]:
    """The items of notify.js of the office's tasks to the point whose day has come (the red «!»): at most POINT_PER_DAY rings a working
    day per point; the point's switch off — none."""
    from . import point_notify
    if not point_on(conn, point_id):
        return []
    items = [{"base": "office-task-%d" % t["id"], "created": max(str(t["created_at"])[:19], t["due_at"][:10]), "title": "Задача от офиса",
              "text": point_notify.task_text(t)[:200], "href": "/my-point/notices"}
             for t in point_notify.office_tasks(conn, point_id, now) if t["urgent"]]
    return keyed(conn, "point:%d" % int(point_id), now, POINT_PER_DAY, items)
