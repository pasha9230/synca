"""«Срок хранения ОТХ истекает завтра» for the office (the owner 2026-11-27).

The term of storage of «На ОТХ» is the seventh day (order_oth.ask_due: the day it entered is the first, from 12:30 the order goes to
«На возврат» by itself). The day before that day the office sees it: a mark on the row of the page «На ОТХ» and one task of «Задачи» (to the
order's responsible of the office, else the owner: order_oth.office_assignee), once per order and stay on «На ОТХ» (TASK_KEY); the task
closes itself when the order left that stay (on «На возврат», sold, back to its client). The same orders as the seventh day moves
(order_oth._HELD_SQL: no agreed return, not to another city, not «не возвращаем», not a point's own sale).

Only app_state and callback_task are written. Nothing goes to a client, Avito, VK or WebMIM; the point sees nothing of it."""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from typing import Dict

TASK_KEY = "oth_ends_task:%d:%s"         # app_state, per order and stay on «На ОТХ» (its entered_at, digits): the task's id
TASK_TITLE = "Срок хранения ОТХ истекает завтра"
MARK = "Срок хранения истекает завтра"

log = logging.getLogger("tires_sync.oth_expiry")


def _stay(entered: str) -> str:
    return re.sub(r"\D", "", entered or "")


def last_day(entered: str) -> str:
    """The day the order goes to «На возврат» (the seventh day of order_oth.ask_due), '' when unknown."""
    from .order_oth import HOLD_DAYS
    try:
        return (date.fromisoformat(str(entered or "")[:10]) + timedelta(days=HOLD_DAYS)).isoformat()
    except ValueError:
        return ""


def ends_tomorrow(conn, now: str) -> Dict[int, str]:
    """The orders on «На ОТХ» whose seventh day is tomorrow: {order id: that day}."""
    from . import order_oth, order_points, orders
    tomorrow = (date.fromisoformat(now[:10]) + timedelta(days=1)).isoformat()
    since = order_oth.store_value(conn, order_oth.SINCE_KEY) or ""
    found, own = {}, {}
    for card_id, entered in conn.execute(order_oth._HELD_SQL + "AND c.point_id IS NOT NULL ORDER BY c.id", (since,)).fetchall():
        if last_day(entered) != tomorrow:
            continue
        card = orders.get_card(conn, card_id)
        if card["point_id"] not in own:
            own[card["point_id"]] = order_points.own_sales(conn, card["point_id"])
        if card_id in own[card["point_id"]] or order_oth._kept(conn, card):
            continue
        found[card_id] = tomorrow
    return found


def tick(conn, now: str) -> int:
    """Any hour: one task per order and stay ending tomorrow; then the tasks of the stays that are over close. Returns how many made."""
    from . import order_manager, order_notify, order_oth, orders, tasks
    made, owner = 0, None
    for card_id, day in ends_tomorrow(conn, now).items():
        try:
            card = orders.get_card(conn, card_id)
            key = TASK_KEY % (card_id, _stay(card["entered_at"]))
            if conn.execute("SELECT 1 FROM app_state WHERE key = ?", (key,)).fetchone():
                continue
            owner = owner or order_notify.get_owner_login(conn)
            point = (card.get("point") or {}).get("name") or "точка №%s" % card["point_id"]
            title = "%s: заказ №%s, точка «%s»" % (TASK_TITLE, order_manager.label(card), point)
            task_id = tasks.add(conn, kind="other", title=title[:tasks.TITLE_MAX], due_at=now[:16], now=now, author="система", owner_login=owner,
                                note="%s.%s в 12:30 заказ сам перейдёт «На возврат». Продать с ОТХ или решить, что делать, — сегодня." % (day[8:10], day[5:7]),
                                assignee=order_oth.office_assignee(conn, card_id), order_id=card_id, commit=False)
            conn.execute("INSERT OR IGNORE INTO app_state (key, value) VALUES (?, ?)", (key, str(task_id)))
            conn.commit()
            made += 1
        except Exception:                                                  # noqa: BLE001 - one order never stops the others
            conn.rollback()
            log.exception("the task of the end of storage of order %s", card_id)
    close_tasks(conn, now)
    return made


def close_tasks(conn, now: str) -> int:
    """The open tasks of tick whose stay on «На ОТХ» is over (another stage, or a new stay): done."""
    from . import tasks
    closed = 0
    for key, task_id, code, entered, stage in conn.execute(
            "SELECT a.key, t.id, s.code, c.entered_at, s.name FROM app_state a JOIN callback_task t ON t.id = CAST(a.value AS INTEGER) "
            "JOIN order_card c ON c.id = t.order_id JOIN order_stage s ON s.id = c.stage_id WHERE a.key LIKE 'oth_ends_task:%' "
            "AND t.done_at IS NULL AND t.cancelled_at IS NULL").fetchall():
        if code == "oth" and key.endswith(":" + _stay(entered)):
            continue
        try:
            tasks.close(conn, task_id, "done", now, "система", "заказ ушёл с «На ОТХ»: этап «%s»" % stage)
            closed += 1
        except tasks.TaskError:
            conn.rollback()
    return closed
