"""«Задачи» (the owner 2026-10-31; «Перезвонить» of inquiry.py before): a task of the panel — what to do («перезвонить», «оформить заказ»,
«переместить на ОТХ», «другое» + the text), to whom (a login: the owner, the office, a manager of a point), by when, of an order, a chat, a
client or of none. The table is callback_task (its old rows are «перезвонить»). A task sends nothing to anybody: no message to a client,
nothing to Avito nor WebMIM. It is not an alert: a due task is only counted and lit in «Задачи» (the owner 2026-10-31: «не показывать» in
«Уведомления» nor in the bell).

Who: the office (the owner, the partner, the chats' manager) sets a task to anybody of ASSIGNEES and closes any; a manager of a point sets
one only to itself (of an order of its own point or of none) and sees and closes only its own — the panel checks it on the server
(routes_tasks); the observer and the client set none. An order's task writes its line in the order's journal when it is set, done or
cancelled (grey: no alert)."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from . import store, users

KINDS = {"callback": "Перезвонить", "order": "Оформить заказ", "oth": "Переместить на ОТХ", "return": "Вернуть", "other": "Другое"}
FIELDS = ("id", "client_id", "order_id", "chat_row", "due_at", "assignee", "note", "created_at", "created_by", "done_at", "done_by",
          "cancelled_at", "kind", "title")
TITLE_MAX, NOTE_MAX, RESULT_MAX = 200, 300, 300
BRIEF_MAX, DONE_PER_PAGE = 80, 50
DUE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")
OFFICE_ROLES = ("owner", "partner", "chats")


class TaskError(ValueError):
    """A refused task; the text is for the person."""


def _row(row) -> dict:
    task = dict(zip(FIELDS, row))
    task["kind"] = task["kind"] if task["kind"] in KINDS else "callback"
    task["kind_label"] = KINDS[task["kind"]]
    return task


def get(conn, task_id) -> dict:
    row = conn.execute("SELECT %s FROM callback_task WHERE id = ?" % ", ".join(FIELDS), (task_id,)).fetchone()
    if row is None:
        raise TaskError("Такой задачи нет.")
    return _row(row)


def _dt(text: str) -> Optional[datetime]:
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M")
    except (TypeError, ValueError):
        return None


def _one_line(text, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


# ---- to whom ----------------------------------------------------------------------------------------------------------------------------

def office(conn, owner_login: str) -> set:
    """The office: the active people who work the orders (not a point, not an observer, not a client) and the owner."""
    return {u["login"] for u in users.list_users(conn) if u["active"] and u["role"] not in ("point", "viewer", "client")} | {owner_login}


def assignees(conn, owner_login: str, main_login: str = "") -> List[dict]:
    """Whom a task may go to (the form «Кому»): the owner, the office, the managers of the points (active), by group and name."""
    people = [{"login": owner_login, "name": users.person_name(conn, owner_login, main_login), "group": "Владелец"}]
    for u in users.list_users(conn):
        if not u["active"] or u["login"] == owner_login or u["role"] in ("viewer", "client"):
            continue
        group = "Менеджеры точек" if u["role"] == "point" else "Офис"
        people.append({"login": u["login"], "name": users.person_name(conn, u["login"], main_login), "group": group})
    return people


def default_assignee(conn, owner_login: str, author: str, order_id=None, chat_row=None) -> str:
    """As «Перезвонить» did: the one who answers for the deal (the responsible of the order, else of the chat) if they are of the office,
    else the author."""
    staff = office(conn, owner_login)
    candidates = []
    if order_id:
        row = conn.execute("SELECT responsible, created_by FROM order_card WHERE id = ?", (order_id,)).fetchone()
        if row:
            candidates.append(row[0] or row[1] or "")
    if chat_row:
        row = conn.execute("SELECT responsible FROM avito_chat WHERE id = ?", (chat_row,)).fetchone()
        if row:
            candidates.append(row[0] or "")
    return next((c for c in candidates if c in staff), author)


# ---- set, close ---------------------------------------------------------------------------------------------------------------------------

def add(conn, *, kind: str, title: str, due_at: str, note: str, now: str, author: str, assignee: Optional[str] = None, owner_login: str = "owner",
        client_id=None, order_id=None, chat_row=None, allowed: Optional[set] = None, journal_author: Optional[str] = None) -> int:
    """A task. `assignee` None: default_assignee; else one of `allowed` (the logins of assignees(); the caller narrows it for a point).
    «Перезвонить» needs whom to call (an order, a chat or a client); another kind may be of none. The order's journal gets its line.
    `journal_author`: who the journal and the audit name when not the login (panel/common.author: «<name> за точку «X»»)."""
    if kind not in KINDS:
        raise TaskError("Выберите, что сделать.")
    due = str(due_at or "").strip()[:16]
    if not DUE.match(due) or _dt(due) is None:
        raise TaskError("Укажите дату и время.")
    if due < now[:16]:
        raise TaskError("Это время уже прошло.")
    title = _one_line(title, TITLE_MAX)
    if kind == "other" and not title:
        raise TaskError("Напишите, что сделать.")
    if order_id:
        if conn.execute("SELECT 1 FROM order_card WHERE id = ?", (order_id,)).fetchone() is None:
            raise TaskError("Такого заказа нет.")
        found = conn.execute("SELECT client_id FROM client_order WHERE order_id = ?", (order_id,)).fetchone()
        client_id = client_id or (found[0] if found else None)
    if chat_row:
        if conn.execute("SELECT 1 FROM avito_chat WHERE id = ?", (chat_row,)).fetchone() is None:
            raise TaskError("Такого чата нет.")
        found = conn.execute("SELECT client_id FROM client_chat WHERE chat_row = ?", (chat_row,)).fetchone()
        client_id = client_id or (found[0] if found else None)
    if client_id and conn.execute("SELECT 1 FROM client WHERE id = ?", (client_id,)).fetchone() is None:
        raise TaskError("Такого клиента нет.")
    if kind == "callback" and not (client_id or order_id or chat_row):
        raise TaskError("Не указано, кому перезвонить.")
    if assignee in (None, ""):
        assignee = default_assignee(conn, owner_login, author, order_id, chat_row)
    elif allowed is not None and assignee not in allowed:
        raise TaskError("Такому исполнителю задачу поставить нельзя.")
    task_id = conn.execute("INSERT INTO callback_task (client_id, order_id, chat_row, due_at, assignee, note, created_at, created_by, kind, title) "
                           "VALUES (?,?,?,?,?,?,?,?,?,?)", (client_id or None, order_id or None, chat_row or None, due, assignee,
                                                           _one_line(note, NOTE_MAX), now, author, kind, title)).lastrowid
    if order_id:
        _journal(conn, order_id, now, journal_author or author, "Задача: %s — %s, до %s" % (_what(kind, title), users.person_name(conn, assignee),
                                                                                         _shown(due)))
    store.write_audit(conn, now, journal_author or author, "callback", {"task": task_id, "client": client_id, "order": order_id, "chat": chat_row, "due": due,
                                                     "to": assignee, "kind": kind})
    return task_id


def close(conn, task_id, how: str, now: str, who: str, result: str = "", journal_author: Optional[str] = None) -> dict:
    """«Выполнено» (how = "done", with what came of it) or «Отменить» (how = "cancel"); a closed task stays closed. The rights are the
    caller's (routes_tasks: a point only its own). `journal_author` as in add."""
    if how not in ("done", "cancel"):
        raise TaskError("Неизвестное действие.")
    task = get(conn, task_id)
    column = "done_at" if how == "done" else "cancelled_at"
    cur = conn.execute("UPDATE callback_task SET %s = ?, done_by = ? WHERE id = ? AND done_at IS NULL AND cancelled_at IS NULL" % column,
                       (now, who, task_id))
    if cur.rowcount != 1:
        conn.rollback()
        raise TaskError("Задача уже закрыта.")
    result = _one_line(result, RESULT_MAX)
    if task["order_id"]:
        _journal(conn, task["order_id"], now, journal_author or who, "Задача %s: %s%s" % ("выполнена" if how == "done" else "отменена",
                                                                       _what(task["kind"], task["title"]), (" — " + result) if result else ""))
    store.write_audit(conn, now, journal_author or who, "callback_" + how, {"task": int(task_id), "result": result} if result else {"task": int(task_id)})
    from . import chat_handover                                            # «Взять клиента» of a chat a point handed over (2026-11-02)
    try:                                                                   # the task is closed already: its light never spoils that
        chat_handover.task_closed(conn, task, who, now)
    except Exception:
        conn.rollback()
        logging.getLogger("tires_sync.tasks").exception("the chat handover of task %s was not marked taken", task_id)
    from . import order_oth                                                # «не возвращаем»: no move on the seventh day (2026-11-07)
    try:
        order_oth.task_closed(conn, task, how, result, journal_author or who, now)
    except Exception:
        conn.rollback()
        logging.getLogger("tires_sync.tasks").exception("the return decision of task %s was not kept", task_id)
    return task


def _what(kind: str, title: str) -> str:
    return "%s%s" % (KINDS.get(kind, KINDS["callback"]), (": " + title) if title else "")


def _shown(due: str) -> str:
    return "%s.%s %s" % (due[8:10], due[5:7], due[11:16])


def _journal(conn, order_id, now: str, author: str, text: str) -> None:
    """A line in the order's journal (grey: it is no alert — the owner 2026-10-31); in the caller's transaction (write_audit commits)."""
    from . import orders
    orders.add_event(conn, order_id, now, author, "task", text, level="grey")


# ---- lists --------------------------------------------------------------------------------------------------------------------------------

def of_place(conn, client_id=None, order_id=None, chat_row=None, only: Optional[str] = None) -> List[dict]:
    """The open tasks of a client (with those of its orders and chats), of an order or of a chat, the nearest first; `only`: of that
    assignee alone (a point sees its own)."""
    if client_id:
        sql, args = ("(client_id = ? OR order_id IN (SELECT order_id FROM client_order WHERE client_id = ?) "
                     "OR chat_row IN (SELECT chat_row FROM client_chat WHERE client_id = ?))", [client_id] * 3)
    elif order_id:
        sql, args = "order_id = ?", [order_id]
    elif chat_row:
        sql, args = "chat_row = ?", [chat_row]
    else:
        return []
    if only is not None:                                                    # one login, or several (a point's managers)
        only = [only] if isinstance(only, str) else list(only)
        sql, args = sql + " AND assignee IN (%s)" % ",".join("?" * len(only)) if only else sql + " AND 0", args + only
    return [_row(r) for r in conn.execute("SELECT %s FROM callback_task WHERE done_at IS NULL AND cancelled_at IS NULL AND %s ORDER BY due_at, id"
                                          % (", ".join(FIELDS), sql), args)]


def _scope(conn, *, tab: str, login: str, owner_login: str, point: bool, to: str, kind: str, as_point) -> tuple:
    """The conditions of the open list and of «Выполненные» alike: whose tasks the person sees (the rules in listing), `tab`, `to`, `kind`."""
    where, args = [], []
    if as_point is not None:
        where.append("assignee IN (%s)" % MANAGERS)
        args.append(as_point)
    elif point:
        where.append("assignee = ?")
        args.append(login)
    if tab == "mine" and not point:
        sql, more = _mine(conn, login, owner_login)
        where.append(sql)
        args += more
    elif tab == "by_me":
        where.append("created_by = ?")
        args.append(login)
    if to:
        where.append("assignee = ?")
        args.append(to)
    if kind in KINDS:
        where.append("kind = ?")
        args.append(kind)
    return where, args


def listing(conn, *, tab: str, login: str, owner_login: str, now: str, point: bool, to: str = "", due: str = "", kind: str = "",
            as_point=None) -> List[dict]:
    """The open tasks of the page «Задачи»: `tab` mine (to me), by_me (set by me), all (the office only); a point gets only its own
    whatever the tab. Filters: `to` (a login), `due` overdue|today|week, `kind`. Each task knows if it is late. `as_point`: the owner or
    the partner working as that point (panel/common.current_user) sees what its managers see: the tasks set to them (the lead 2026-11-02),
    never the person's own (those come back with «Выйти»)."""
    where, args = _scope(conn, tab=tab, login=login, owner_login=owner_login, point=point, to=to, kind=kind, as_point=as_point)
    where = ["done_at IS NULL", "cancelled_at IS NULL"] + where
    today_end = _day_end(now)
    if due == "overdue":
        where.append("due_at < ?")
        args.append(now[:16])
    elif due == "today":
        where.append("due_at < ?")
        args.append(today_end)
    elif due == "week":
        where.append("due_at < ?")
        args.append((datetime.strptime(today_end, "%Y-%m-%dT%H:%M") + timedelta(days=6)).strftime("%Y-%m-%dT%H:%M"))
    out = []
    for row in conn.execute("SELECT %s FROM callback_task WHERE %s ORDER BY due_at, id" % (", ".join(FIELDS), " AND ".join(where)), args):
        task = _row(row)
        task["late"] = task["due_at"] < now[:16]
        out.append(task)
    return out


def done_listing(conn, *, tab: str, login: str, owner_login: str, point: bool, to: str = "", kind: str = "", as_point=None, page: int = 1,
                 per_page: int = DONE_PER_PAGE) -> dict:
    """«Выполненные» (the owner 2026-11-04): the done tasks the person may see (the same rules as listing), the newest first, one page:
    {"rows", "total", "page", "pages"}; a page past the end is the last one. Each row knows its `brief` and `spent`."""
    where, args = _scope(conn, tab=tab, login=login, owner_login=owner_login, point=point, to=to, kind=kind, as_point=as_point)
    where = " AND ".join(["done_at IS NOT NULL", "cancelled_at IS NULL"] + where)
    total = conn.execute("SELECT COUNT(*) FROM callback_task WHERE %s" % where, args).fetchone()[0]
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(max(1, page), pages)
    rows = []
    for row in conn.execute("SELECT %s FROM callback_task WHERE %s ORDER BY done_at DESC, id DESC LIMIT ? OFFSET ?" % (", ".join(FIELDS), where),
                            args + [per_page, (page - 1) * per_page]):
        task = _row(row)
        task["brief"] = _brief(task["title"] or task["note"])
        task["spent"] = spent(task["created_at"], task["done_at"])
        rows.append(task)
    return {"rows": rows, "total": total, "page": page, "pages": pages}


def _brief(text) -> str:
    text = _one_line(text, NOTE_MAX)
    return text if len(text) <= BRIEF_MAX else text[:BRIEF_MAX - 1].rstrip() + "…"


def _stamp(text) -> Optional[datetime]:
    text = str(text or "").strip().replace(" ", "T")
    for size, pattern in ((19, "%Y-%m-%dT%H:%M:%S"), (16, "%Y-%m-%dT%H:%M")):
        try:
            return datetime.strptime(text[:size], pattern)
        except ValueError:
            continue
    return None


def spent(start, end) -> str:
    """From setting to doing in plain words: «меньше минуты», «40 мин», «2 ч 15 мин», «3 дня», «1 день 4 ч»; a stamp missing or broken, or
    the end before the start: «—»."""
    begun, ended = _stamp(start), _stamp(end)
    if begun is None or ended is None or ended < begun:
        return "—"
    minutes = int((ended - begun).total_seconds()) // 60
    days, hours, minutes = minutes // 1440, minutes // 60 % 24, minutes % 60
    if days:
        return "%d %s" % (days, _days_word(days)) + (" %d ч" % hours if hours else "")
    if hours:
        return "%d ч" % hours + (" %d мин" % minutes if minutes else "")
    return "%d мин" % minutes if minutes else "меньше минуты"


def _days_word(days: int) -> str:
    if days % 10 == 1 and days % 100 != 11:
        return "день"
    return "дня" if days % 10 in (2, 3, 4) and days % 100 not in (12, 13, 14) else "дней"


MANAGERS = "SELECT login FROM panel_user_role WHERE role = 'point' AND point_id = ?"     # the managers of a point (one argument)


def point_managers(conn, point_id, active_only: bool = False) -> List[str]:
    """The logins of the managers of a point, by login; `active_only`: those who can sign in (whom to set a task to)."""
    return [r[0] for r in conn.execute(MANAGERS + (" AND active = 1" if active_only else "") + " ORDER BY login", (point_id,))]


def _mine(conn, login: str, owner_login: str) -> tuple:
    if login != owner_login:
        return "assignee = ?", [login]
    working = sorted(office(conn, owner_login) | {u["login"] for u in users.list_users(conn) if u["active"] and u["role"] == "point"})
    return "(assignee = ? OR assignee NOT IN (%s))" % ",".join("?" * len(working)), [login] + working


def _day_end(now: str) -> str:
    return (datetime.strptime(now[:10], "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%dT00:00")


def counter(conn, login: str, owner_login: str, now: str, point: bool, as_point=None) -> Dict[str, int]:
    """The menu's number: my open tasks late or due today; «late» — some of them are past their time (red)."""
    tasks = listing(conn, tab="mine", login=login, owner_login=owner_login, now=now, point=point, due="today", as_point=as_point)
    return {"count": len(tasks), "late": sum(1 for t in tasks if t["late"])}
