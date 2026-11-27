"""«Мои уведомления» of a person of the office (the owner 2026-11-27, 2026-11-28): the sound and the notification of the browser (notify.js) of
one's own important tasks — the open tasks «Важное · …» set to the person («Позвонить…» of order_call and the other urgent tasks of the
system, task_sound). They ring in the working day only, at most task_sound.OFFICE_PER_DAY times a day, 3 hours apart, while open; the
office gets no ring for the stages of the orders. The person switches the sound on (the default) or off. Kept per login in app_state."""
from __future__ import annotations

from typing import List

from . import store, task_sound

KEY = "office_notify:%s"              # app_state: "1"/"0" (the sound); an older "<calls>,<repeat>" is read by its first part
DEFAULT = {"sound": True}


def get(conn, login: str) -> dict:
    """{"sound"} of the person; the default when nothing was chosen (or the value is odd)."""
    raw = store.get_state(conn, KEY % login) if login else None
    first = (raw or "").split(",")[0]
    if first not in ("0", "1"):
        return dict(DEFAULT)
    return {"sound": first == "1"}


def important(conn, login: str) -> List[dict]:
    """The open tasks «Важное · …» set to the person, oldest first: {"id", "order_id", "title", "created_at"}."""
    rows = conn.execute("SELECT id, order_id, title, created_at FROM callback_task WHERE assignee = ? AND title LIKE ? AND done_at IS NULL "
                        "AND cancelled_at IS NULL ORDER BY id", (login, task_sound.IMPORTANT + "%")).fetchall()
    return [{"id": r[0], "order_id": r[1], "title": r[2], "created_at": r[3]} for r in rows]


def calls(conn, login: str, now: str) -> list:
    """The items of notify.js of the person's important tasks: rung at most OFFICE_PER_DAY times a working day (task_sound); the sound
    switched off — all quiet (the page still knows them) and no ring is taken."""
    from . import order_manager, orders
    items = []
    for task in important(conn, login):
        label = ""
        if task["order_id"]:
            try:
                label = order_manager.label(orders.get_card(conn, task["order_id"]))
            except Exception:                                             # noqa: BLE001 - the sound still rings
                conn.rollback()
                label = task["order_id"]
        items.append({"base": "call-task-%d" % task["id"], "created": task["created_at"], "title": task["title"],
                      "text": "Заказ №%s" % label if label else "Задача", "href": "/orders/%d" % task["order_id"] if task["order_id"] else "/tasks"})
    if not get(conn, login)["sound"]:
        return [dict({k: v for k, v in i.items() if k not in ("base", "created")}, key=i["base"], quiet=True) for i in items]
    return task_sound.keyed(conn, "office:%s" % login, now, task_sound.OFFICE_PER_DAY, items)


def put(conn, login: str, sound: bool) -> dict:
    store.set_state(conn, KEY % login, "1" if sound else "0")
    return get(conn, login)
