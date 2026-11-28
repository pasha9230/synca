"""Уведомления точек и «Сводки» точки (the owner 2026-11-25, plan parts D and E).

D. The notifications of a point (the sound and the browser's notification of notify.js) are on by default: a point without a row of
point_notify is on. The office (the owner, the partner, the chats' manager) switches them off and on for a point on «Уведомления точек»
(/points/notify); the point itself on «Рабочее место» — and to switch them off it writes who does it (a name). Every switch is a record of
point_event (who, when, which point, the name and the reason), as the actions of «Сводки» are; the office reads them on «Уведомления
точек». While they are on and the browser has not been answered, a person of the point is asked «Разрешите уведомления» at most
ASKS_A_DAY times a calendar day, ASK_GAP_MINUTES apart (point_notify_ask, per login and day; notify.js never asks while one types).

E. «Сводки» of a point (/my-point/notices): the last calendar week — the orders that came to the point, were handed out and the clients
it handed over; whether there is a return today (the button says «Есть возвраты!» that day); the open tasks the office set to the managers
of the point (a red «!» on the button from the task's day on, while it is open), with «Прочитал» and «Выполнено» — both recorded.

Nothing here sends anything to a client, Avito or WebMIM, and nothing of the purchase or the prices is read."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

ASKS_A_DAY = 3                     # «3 раза в день до одобрения» (the owner 2026-11-25)
ASK_GAP_MINUTES = 120              # not three asks in a row: two hours apart at least («не бесить»)
WHO_MAX, REASON_MAX = 60, 200
HISTORY_SHOWN = 500
KINDS = {"notify_off": "Выключил уведомления", "notify_on": "Включил уведомления", "task_read": "Прочитал задачу офиса",
         "task_done": "Выполнил задачу офиса", "allowed": "Разрешил уведомления в браузере", "refused": "Запретил уведомления в браузере",
         "arrival_remind": "Напоминание: отметить приезд"}     # arrival_remind (the owner 2026-11-27): a showing of the reminder
RETURNS_MARK = "Есть возвраты!"

log = logging.getLogger("tires_sync.point_notify")


class NotifyError(ValueError):
    """A refused switch; the text is for the person."""


def _line(text, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


# ---- on / off ----------------------------------------------------------------------------------------------------------------------------

def state(conn, point_id) -> dict:
    """{"enabled", "at", "author", "who", "reason", "task_sound"} of the point; no row: on, never switched. `task_sound`: the point's own
    switch of the sound of the office's tasks (task_sound, the owner 2026-11-28)."""
    from . import task_sound
    row = conn.execute("SELECT enabled, at, author, who, reason FROM point_notify WHERE point_id = ?", (point_id,)).fetchone()
    sound = task_sound.point_on(conn, point_id)
    if row is None:
        return {"enabled": True, "at": "", "author": "", "who": "", "reason": "", "task_sound": sound}
    return {"enabled": bool(row[0]), "at": row[1], "author": row[2], "who": row[3], "reason": row[4], "task_sound": sound}


def enabled(conn, point_id) -> bool:
    return point_id is None or state(conn, point_id)["enabled"]


def switch(conn, point_id, on: bool, now: str, author: str, who: str = "", reason: str = "", by_point: bool = False) -> dict:
    """Switch the notifications of the point; a point switching them off must say who does it (`who`). The same state again is refused
    (nothing written). The record of point_event goes with it; committed."""
    who, reason = _line(who, WHO_MAX), _line(reason, REASON_MAX)
    if conn.execute("SELECT 1 FROM point WHERE id = ?", (point_id,)).fetchone() is None:
        raise NotifyError("Такой точки нет.")
    if not on and by_point and not who:
        raise NotifyError("Впишите, кто выключает уведомления.")
    if state(conn, point_id)["enabled"] == bool(on):
        raise NotifyError("Уведомления уже включены." if on else "Уведомления уже выключены.")
    conn.execute("INSERT OR REPLACE INTO point_notify (point_id, enabled, at, author, who, reason) VALUES (?,?,?,?,?,?)",
                 (point_id, 1 if on else 0, now, author, who, reason))
    record(conn, now, point_id, "notify_on" if on else "notify_off", author, who, reason, commit=False)
    conn.commit()
    return state(conn, point_id)


# ---- «Разрешите уведомления»: 3 asks a day ----------------------------------------------------------------------------------------------

def _gap_start(now: str) -> str:
    return (datetime.strptime(now[:19], "%Y-%m-%dT%H:%M:%S") - timedelta(minutes=ASK_GAP_MINUTES)).strftime("%Y-%m-%dT%H:%M:%S")


def asks_left(conn, login: str, now: str) -> int:
    """How many asks the person may still get today; 0 while the last one is less than ASK_GAP_MINUTES old."""
    row = conn.execute("SELECT shown, last_at FROM point_notify_ask WHERE login = ? AND day = ?", (login, now[:10])).fetchone()
    if row is None:
        return ASKS_A_DAY
    if row[1] and row[1] > _gap_start(now):
        return 0
    return max(0, ASKS_A_DAY - int(row[0]))


def ask(conn, login: str, now: str) -> bool:
    """One ask shown now, if it may be (asks_left): counted in one statement, so two tabs never make it four. Committed."""
    day = now[:10]
    conn.execute("INSERT OR IGNORE INTO point_notify_ask (login, day, shown, last_at) VALUES (?,?,0,'')", (login, day))
    done = conn.execute("UPDATE point_notify_ask SET shown = shown + 1, last_at = ? WHERE login = ? AND day = ? AND shown < ? "
                        "AND (last_at = '' OR last_at <= ?)", (now[:19], login, day, ASKS_A_DAY, _gap_start(now))).rowcount == 1
    conn.commit()
    return done


# ---- the history ---------------------------------------------------------------------------------------------------------------------

def record(conn, now: str, point_id, kind: str, author: str, who: str = "", text: str = "", task_id=None, commit: bool = True) -> None:
    """A record of «Уведомления точек»: who (the login or «<name> за точку …»), the name typed, the point, what and its text."""
    conn.execute("INSERT INTO point_event (at, point_id, kind, author, who, text, task_id) VALUES (?,?,?,?,?,?,?)",
                 (now, int(point_id), kind, str(author or "")[:120], _line(who, WHO_MAX), _line(text, 300), task_id))
    if commit:
        conn.commit()


def history(conn, point_id=None, since: str = "", until: str = "", kind: str = "", limit: int = HISTORY_SHOWN) -> List[dict]:
    """The records, newest first, of a point (None: all), from `since` to `until` (days, both included), of one kind or all."""
    where, args = [], []
    if point_id is not None:
        where.append("e.point_id = ?")
        args.append(int(point_id))
    if since:
        where.append("e.at >= ?")
        args.append(since[:10])
    if until:
        where.append("e.at < ?")
        args.append((date.fromisoformat(until[:10]) + timedelta(days=1)).isoformat())
    if kind:
        where.append("e.kind = ?")
        args.append(kind)
    sql = ("SELECT e.at, e.point_id, COALESCE(p.name, ''), e.kind, e.author, e.who, e.text, e.task_id FROM point_event e "
           "LEFT JOIN point p ON p.id = e.point_id")
    if where:
        sql += " WHERE " + " AND ".join(where)
    rows = conn.execute(sql + " ORDER BY e.at DESC, e.id DESC LIMIT ?", args + [limit]).fetchall()
    return [{"at": r[0], "point_id": r[1], "point": r[2] or "точка №%d" % r[1], "kind": r[3], "name": KINDS.get(r[3], r[3]),
             "author": r[4], "who": r[5], "text": r[6], "task_id": r[7]} for r in rows]


# ---- «Сводки» --------------------------------------------------------------------------------------------------------------------

def last_week(today: str) -> tuple:
    """The calendar week before this one: (Monday, Sunday) as days."""
    day = date.fromisoformat(today[:10])
    monday = day - timedelta(days=day.weekday() + 7)
    return monday.isoformat(), (monday + timedelta(days=6)).isoformat()


def week_counts(conn, point_id, start: str, end: str) -> Dict[str, int]:
    """The orders that came to the point (point_arrival: «Приехал» not undone, not «шины не приезжали») and the clients it handed over,
    from `start` to `end` (days, both included). The hand-outs are counted by the caller (routes_mypoint._issued, one rule with «Выдано»)."""
    after = (date.fromisoformat(end) + timedelta(days=1)).isoformat()
    came = conn.execute("SELECT COUNT(*) FROM order_arrival a LEFT JOIN order_card c ON c.id = a.order_id WHERE COALESCE(a.point_id, c.point_id) = ? "
                        "AND a.arrived_at >= ? AND a.arrived_at < ? AND a.undone_at IS NULL AND a.none_at IS NULL", (point_id, start, after)).fetchone()[0]
    handed = conn.execute("SELECT COUNT(*) FROM order_handoff WHERE point_id = ? AND created_at >= ? AND created_at < ?",
                          (point_id, start, after)).fetchone()[0]
    return {"received": int(came), "handed": int(handed)}


def returns_today(conn, point_id, today: str) -> List[str]:
    """The numbers of the point's orders agreed to go back to the supplier today, the act not signed yet (order_oth: the return day, told
    at 09:00 or not)."""
    from . import order_oth
    if point_id is None:
        return []
    rows = conn.execute("SELECT c.id, c.webmim_number " + order_oth.RETURN_DAY_SQL + "AND c.point_id = ? ORDER BY c.id",
                        (today[:10],) + tuple(order_oth.RETURNING) + (int(point_id),)).fetchall()
    return [str(r[1] or "панель %d" % r[0]) for r in rows]


def _managers(conn, point_id) -> List[str]:
    from . import tasks
    return tasks.point_managers(conn, point_id)


def office_tasks(conn, point_id, today: str) -> List[dict]:
    """The open tasks the office set to the managers of the point (not those a manager set to itself or another of its point; never
    «Автосообщение клиенту не ушло», the office's own), the nearest first; each knows `urgent` (its day has come) and when it was read."""
    from . import order_autosend, tasks
    managers = _managers(conn, point_id)
    if not managers:
        return []
    marks = ",".join("?" * len(managers))
    rows = conn.execute("SELECT %s FROM callback_task WHERE done_at IS NULL AND cancelled_at IS NULL AND assignee IN (%s) AND created_by NOT IN (%s) "
                        "ORDER BY due_at, id" % (", ".join(tasks.FIELDS), marks, marks), managers + managers).fetchall()
    found = []
    for row in rows:
        task = tasks._row(row)
        if order_autosend.is_task(task):
            continue
        read = conn.execute("SELECT at, author FROM point_task_read WHERE task_id = ?", (task["id"],)).fetchone()
        task.update(urgent=task["due_at"][:10] <= today[:10], read_at=read[0] if read else "", read_by=read[1] if read else "")
        found.append(task)
    return found


def office_task(conn, point_id, task_id, today: str) -> Optional[dict]:
    """The task if it is an open task of the office to this point (office_tasks), else None."""
    return next((t for t in office_tasks(conn, point_id, today) if t["id"] == task_id), None)


def mark_read(conn, point_id, task: dict, now: str, author: str) -> bool:
    """«Прочитал»: once per task (False when it was already read); recorded. Committed."""
    done = conn.execute("INSERT OR IGNORE INTO point_task_read (task_id, point_id, at, author) VALUES (?,?,?,?)",
                        (task["id"], point_id, now, author)).rowcount == 1
    if done:
        record(conn, now, point_id, "task_read", author, text=task_text(task), task_id=task["id"], commit=False)
    conn.commit()
    return done


def task_text(task: dict) -> str:
    return "%s%s, срок %s.%s %s" % (task["kind_label"], (": " + task["title"]) if task.get("title") else "", task["due_at"][8:10],
                                    task["due_at"][5:7], task["due_at"][11:16])


def marks(conn, point_id, today: str) -> Dict[str, bool]:
    """What the button «Сводки» says: `returns` — «Есть возвраты!» (a return today); `urgent` — the red «!» (an open task of the
    office whose day has come). Never raises: a busy base shows the button plain."""
    if point_id is None:
        return {"returns": False, "urgent": False}
    try:
        return {"returns": bool(returns_today(conn, point_id, today)), "urgent": any(t["urgent"] for t in office_tasks(conn, point_id, today))}
    except Exception:                                                     # noqa: BLE001 - the mark is a hint; the page itself opens
        log.exception("the marks of «Сводки» of point %s", point_id)
        return {"returns": False, "urgent": False}
