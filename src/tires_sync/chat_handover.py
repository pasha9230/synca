"""«Передать клиента» from a chat of a point (the owner 2026-11-02, instead of «Передать под заказ» there): the manager of the point hands the
client of a chat of its account to the owner and the partner (Заман). No order is made. The chat is lit in their «Чаты» («передан с точки
<точка>»), each of them gets a task «Взять клиента» (the chat's number, the point, the link of the task leads to the chat) and a Telegram
message. The light goes out when somebody of the office presses «Взять клиента» in the chat (both tasks are done) or closes one of the tasks
(the other one is cancelled). A chat is handed over once until it is taken; then it may be handed over again. Nothing goes to the client,
to Avito nor to WebMIM. The calls and /my-point/handed keep «Передать под заказ» (order_manager.hand_over)."""
from __future__ import annotations

import json
from typing import Dict, Iterable, List, Optional

from . import chats, order_points as points, store, tasks, users

TITLE = "Взять клиента"


class HandoverError(ValueError):
    """A refused handover; the text is for the person."""


def recipients(conn) -> List[str]:
    """Whom the client goes to: the owner's login of the alerts and every active partner (Заман: by the role, never a written login)."""
    from .order_notify import get_owner_login
    owner = get_owner_login(conn)
    partners = sorted(u["login"] for u in users.list_users(conn) if u["role"] == "partner" and u["active"] and u["login"] != owner)
    return [owner] + partners


def open_of(conn, chat_rows: Optional[Iterable[int]] = None) -> Dict[int, dict]:
    """The chats handed over and not taken yet: {chat_row: {"id", "point", "at", "by"}}; all of them without `chat_rows`."""
    sql, args = "SELECT id, chat_row, point_name, at, handed_by FROM chat_handover WHERE taken_at IS NULL", []
    if chat_rows is not None:
        rows = [int(r) for r in chat_rows]
        if not rows:
            return {}
        sql, args = sql + " AND chat_row IN (%s)" % ",".join("?" * len(rows)), rows
    return {r[1]: {"id": r[0], "point": r[2], "at": r[3], "by": r[4]} for r in conn.execute(sql + " ORDER BY id", args)}


def hand(conn, point_id: int, chat_row: int, who: str, now: str) -> dict:
    """The press of the point's manager. Returns {"id", "point", "tasks", "to"}; raises HandoverError with what is wrong (another account's
    chat, a chat handed over already and not taken). Two presses at once make one handover (the insert asks inside itself)."""
    point = points.get_point(conn, point_id)
    chat = chats.get_chat(conn, chat_row)
    if chat is None:
        raise HandoverError("Такого чата нет.")
    if chat["account_id"] != point.get("avito_account_id"):
        raise HandoverError("Это чат не аккаунта вашей точки: передать из него клиента нельзя.")
    to = recipients(conn)
    cur = conn.execute("INSERT INTO chat_handover (chat_row, point_id, point_name, at, handed_by) SELECT ?, ?, ?, ?, ? "
                       "WHERE NOT EXISTS (SELECT 1 FROM chat_handover WHERE chat_row = ? AND taken_at IS NULL)",
                       (chat_row, point["id"], point["name"], now, who, chat_row))
    if cur.rowcount != 1:
        conn.rollback()
        raise HandoverError("Клиент из этого чата уже передан, его ещё не взяли.")
    handover_id = cur.lastrowid
    ref = "Ч-%d" % chat_row
    title = "%s: чат %s, точка «%s»" % (TITLE, ref, point["name"])
    note = " · ".join(p for p in (chat.get("peer_name") or "", chat.get("item_title") or "") if p)
    made = []
    try:
        for login in to:
            made.append(tasks.add(conn, kind="other", title=title, due_at=now[:16], note=note, now=now, author=who, assignee=login,
                                  owner_login=to[0], chat_row=chat_row))
            conn.execute("UPDATE chat_handover SET tasks = ? WHERE id = ?", (json.dumps(made), handover_id))   # with each task: a stop between
        store.write_audit(conn, now, who, "chat_handover", {"handover": handover_id, "chat": chat_row, "point": point["id"], "to": to, "tasks": made})
    except BaseException:
        conn.rollback()
        conn.execute("DELETE FROM chat_handover WHERE id = ?", (handover_id,))           # tasks.add commits on its way: nothing half-made stays
        if made:
            conn.execute("DELETE FROM callback_task WHERE id IN (%s)" % ",".join("?" * len(made)), made)
        conn.commit()
        raise
    return {"id": handover_id, "point": point["name"], "tasks": made, "to": to}


def message(point_name: str, chat_row: int, link: str = "", handover_id: int = 0) -> str:
    """The Telegram text to the owner and the partner; the number of the handover keeps a new handover of the same chat from being a repeat
    (order_notify.tell_people sends one text to one chat once an hour)."""
    return "Точка «%s» передала клиента из чата Ч-%d%s: возьмите его в работу.%s" % (
        point_name, chat_row, " (передача №%d)" % handover_id if handover_id else "", (" " + link) if link else "")


def _task_ids(raw) -> List[int]:
    try:
        return [int(t) for t in json.loads(raw or "[]")]
    except (TypeError, ValueError):
        return []


def take(conn, chat_row: int, who: str, now: str) -> bool:
    """«Взять клиента» in the chat (the office): the light goes out, the open tasks of the handover are done. False when there is nothing to take."""
    row = conn.execute("SELECT id, tasks FROM chat_handover WHERE chat_row = ? AND taken_at IS NULL ORDER BY id LIMIT 1", (chat_row,)).fetchone()
    if row is None:
        return False
    if conn.execute("UPDATE chat_handover SET taken_at = ?, taken_by = ?, taken_how = 'take' WHERE id = ? AND taken_at IS NULL",
                    (now, who, row[0])).rowcount != 1:
        conn.rollback()
        return False
    conn.commit()                                                          # taken before the tasks: a task closed already rolls back only itself
    _close_tasks(conn, _task_ids(row[1]), "done", who, now)
    store.write_audit(conn, now, who, "chat_handover_taken", {"handover": row[0], "chat": chat_row, "how": "take"})
    return True


def task_closed(conn, task: dict, who: str, now: str) -> None:
    """A task «Взять клиента» closed in «Задачи» (tasks.close): the handover is taken and the other open task of it is cancelled."""
    if not task.get("chat_row"):
        return
    for handover_id, raw in conn.execute("SELECT id, tasks FROM chat_handover WHERE chat_row = ? AND taken_at IS NULL", (task["chat_row"],)).fetchall():
        ids = _task_ids(raw)
        if int(task["id"]) not in ids:
            continue
        if conn.execute("UPDATE chat_handover SET taken_at = ?, taken_by = ?, taken_how = 'task' WHERE id = ? AND taken_at IS NULL",
                        (now, who, handover_id)).rowcount == 1:
            conn.commit()
            _close_tasks(conn, [t for t in ids if t != int(task["id"])], "cancel", who, now)
            store.write_audit(conn, now, who, "chat_handover_taken", {"handover": handover_id, "chat": task["chat_row"], "how": "task", "task": int(task["id"])})


def _close_tasks(conn, ids: List[int], how: str, who: str, now: str) -> None:
    for task_id in ids:
        try:
            tasks.close(conn, task_id, how, now, who)
        except tasks.TaskError:                                            # closed already by hand
            pass
