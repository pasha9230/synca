"""«Заказ должен был приехать — отметьте» and the office's watch over the late orders (the owner 2026-11-27).

1. The point's reminder (panel only: a plaque on «Рабочее место» and a notification of notify.js). The orders are those of «Требует действия
   сейчас» of «Сводки» (point_summary.to_act: on «Заказ оформлен» / «Едет на точку», no «Приехал», the delivery day of WebMIM LATE_DAYS and
   more ago, or — no delivery day — on «Едет на точку» MOVING_HOURS and more), so the page and «Сводки» never disagree. It is shown only in
   the working hours HOURS (Moscow), at most PER_DAY times a calendar day per point and GAP_HOURS apart; every showing is kept (SHOWN_KEY, per
   point and day) and recorded in point_event (kind «arrival_remind»), so it never rings again before its time. Not shown while the point's
   notifications are off (point_notify); «Сводки» list the orders always. Only the point's own person makes a showing (never the office working
   as the point); the office sees the last one.
2. The office's task: an order on «Заказ оформлен» / «Едет на точку» TASK_DAYS and more past its delivery day gets one task of «Задачи» (to
   its responsible of the office, else the owner), once per order and delivery day (TASK_KEY); it closes itself when the order leaves those
   stages (on the point, closed, cancelled).

Only app_state, point_event and callback_task are written: no table of its own. Nothing goes to a client, Avito, VK or WebMIM."""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from typing import List, Optional

HOURS = (10, 19)                         # from 10:00 to 19:00 (Moscow): 19:00 itself is out
PER_DAY = 2                              # showings a calendar day per point
GAP_HOURS = 3                            # between two showings
SHOWN_KEY = "arrival_remind:%d:%s"       # app_state, per point and day: {"shown": [at, ...], "orders": [the numbers of the last showing]}
KIND = "arrival_remind"                  # point_event
TITLE = "Заказ должен был приехать — отметьте"
BODY = "Шины уже на точке — нажмите «Приехал». Ещё не приехали — напишите в офис."
HREF = "/my-point?tab=moving"

TASK_DAYS = 3                            # the office's task: this many days and more past the delivery day
TASK_KEY = "arrival_late_task:%d:%s"     # app_state, per order and delivery day: the task's id (made once, even if closed by hand)
TASK_TITLE = "Заказ едет дольше срока"
WAY = ("placed", "moving")

log = logging.getLogger("tires_sync.arrival_remind")


def in_hours(now: str) -> bool:
    return HOURS[0] <= int(now[11:13]) < HOURS[1]


def _state(conn, key: str) -> tuple:
    row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None, {"shown": [], "orders": []}
    try:
        data = json.loads(row[0])
    except ValueError:
        data = {}
    return row[0], {"shown": list(data.get("shown") or []), "orders": list(data.get("orders") or [])}


def may_show(shown: List[str], now: str) -> bool:
    """A new showing now: fewer than PER_DAY today, the last one GAP_HOURS ago and more."""
    if len(shown) >= PER_DAY:
        return False
    if not shown:
        return True
    return datetime.fromisoformat(now[:19]) >= datetime.fromisoformat(shown[-1][:19]) + timedelta(hours=GAP_HOURS)


def current(conn, point_id, now: str, record: bool = True) -> Optional[dict]:
    """The reminder of the point as it stands now, None when there is none (no late order, out of the hours, the notifications off, not
    shown yet today and `record` False). `record`: the point's own person sees the page — a new showing is kept when it may be (may_show),
    in one statement (two tabs never make three). {"key" (new with every showing: notify.js rings by it), "n", "at", "orders", "ids",
    "title", "text", "href"}. Committed when it records."""
    from . import point_notify, point_summary
    if point_id is None or not in_hours(now) or not point_notify.enabled(conn, point_id):
        return None
    late = point_summary.to_act(conn, point_id, now)
    if not late:
        return None
    key = SHOWN_KEY % (int(point_id), now[:10])
    labels = [r["label"] for r in late]
    old, state = _state(conn, key)
    if record and may_show(state["shown"], now):
        state = {"shown": state["shown"] + [now[:19]], "orders": labels}
        value = json.dumps(state, ensure_ascii=False)
        if old is None:
            made = conn.execute("INSERT OR IGNORE INTO app_state (key, value) VALUES (?, ?)", (key, value)).rowcount == 1
        else:
            made = conn.execute("UPDATE app_state SET value = ? WHERE key = ? AND value = ?", (value, key, old)).rowcount == 1
        if made:
            point_notify.record(conn, now, point_id, KIND, "система", text="Заказы: " + ", ".join("№ %s" % n for n in labels), commit=False)
        conn.commit()
        if not made:                                                       # another tab was first: its showing stands
            state = _state(conn, key)[1]
    if not state["shown"]:
        return None
    n = len(state["shown"])
    return {"key": "arrival-%s-%d" % (now[:10].replace("-", ""), n), "n": n, "at": state["shown"][-1], "orders": labels,
            "ids": [r["id"] for r in late], "title": TITLE, "text": "%s Заказы: %s." % (BODY, ", ".join("№ %s" % x for x in labels)),
            "href": HREF}


# ---- 2. the office's task -------------------------------------------------------------------------------------------------------------

def _day(iso: str) -> str:
    return "%s.%s.%s" % (iso[8:10], iso[5:7], iso[:4])


def late_tasks(conn, now: str) -> int:
    """The tick, any hour: one task per late order and delivery day (TASK_DAYS and more past it); then close_tasks. Returns how many made."""
    from . import order_manager, order_notify, order_points, orders, tasks
    limit = (date.fromisoformat(now[:10]) - timedelta(days=TASK_DAYS)).isoformat()
    rows = conn.execute("SELECT c.id, c.point_id, c.delivery_date FROM order_card c JOIN order_stage s ON s.id = c.stage_id "
                        "WHERE s.code IN (?, ?) AND c.point_id IS NOT NULL AND c.delivery_date != '' AND c.delivery_date <= ? ORDER BY c.id",
                        WAY + (limit,)).fetchall()
    made, own, owner = 0, {}, None
    for card_id, point_id, planned in rows:
        key = TASK_KEY % (card_id, str(planned)[:10])
        if conn.execute("SELECT 1 FROM app_state WHERE key = ?", (key,)).fetchone():
            continue
        if point_id not in own:
            own[point_id] = order_points.own_sales(conn, point_id)
        if card_id in own[point_id]:
            continue
        try:
            card = orders.get_card(conn, card_id)
            owner = owner or order_notify.get_owner_login(conn)
            point = (card.get("point") or {}).get("name") or "точка №%s" % point_id
            title = "%s: №%s, точка «%s», доставка %s" % (TASK_TITLE, order_manager.label(card), point, _day(str(planned)[:10]))
            task_id = tasks.add(conn, kind="other", title=title[:tasks.TITLE_MAX], due_at=now[:16], now=now, author="система", owner_login=owner,
                                note="Заказ не отмечен «Приехал» %d дн. и больше после даты доставки. Узнайте у точки и поставщика, где шины." % TASK_DAYS,
                                assignee=tasks.default_assignee(conn, owner, owner, card_id), order_id=card_id, commit=False)
            conn.execute("INSERT OR IGNORE INTO app_state (key, value) VALUES (?, ?)", (key, str(task_id)))
            conn.commit()
            made += 1
        except Exception:                                                  # noqa: BLE001 - one order never stops the others
            conn.rollback()
            log.exception("the task of the late order %s", card_id)
    close_tasks(conn, now)
    return made


def close_tasks(conn, now: str) -> int:
    """The open tasks of late_tasks whose order left «Заказ оформлен» / «Едет на точку»: done, with where the order is now."""
    from . import tasks
    closed = 0
    for task_id, stage in conn.execute(
            "SELECT t.id, s.name FROM app_state a JOIN callback_task t ON t.id = CAST(a.value AS INTEGER) JOIN order_card c ON c.id = t.order_id "
            "JOIN order_stage s ON s.id = c.stage_id WHERE a.key LIKE 'arrival_late_task:%' AND t.done_at IS NULL AND t.cancelled_at IS NULL "
            "AND s.code NOT IN (?, ?)", WAY).fetchall():
        try:
            tasks.close(conn, task_id, "done", now, "система", "заказ перешёл на этап «%s»" % stage)
            closed += 1
        except tasks.TaskError:                                            # closed meanwhile
            conn.rollback()
    return closed
