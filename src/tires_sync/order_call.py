"""«Позвонить» of an order without an Avito chat (the owner 2026-11-12). The panel writes nothing to such a client (no chat, no SMS); its
responsible gets a task instead, marked «Важное», with the client, the phone, the positions (name, article, pieces), the sum, the dates
(made, arrived, the last day of the reservation) and the link to the order:
- «Позвонить, товары приехали» when the order enters «На точке, ждём клиента» (orders.move_card, and the tick for a way round it);
- «Позвонить, истекает срок резерва» on the last day of the reservation from 12:30 (Moscow), once per stay and date.
An order without a chat: order_card.avito_chat empty and no avito_chat bound to it (no_chat). Not an order to another city (no client comes).
The task goes to tasks.default_assignee (the order's responsible, else its maker, of the office; else the owner), as the other tasks of the
system. No doubles (`arrived`): an open task is kept; a done «товары приехали» to the same phone is not made again (the client was called; a
move back by hand); a task a person cancelled in this stay is not made again; one the system cancelled today (the order left «На точке» and
came back: a move by mistake, «Отменить» of the point) is opened again, not doubled. The order leaves «На точке»: its open tasks «Позвонить…»
are cancelled as no longer needed (tasks.close by «система»: the journal and the audit). The tick (hourly and 12:30) makes what a move
past orders.move_card left out (a bulk move, a merge, an undo of the point), for the stays since the first tick of this code only (SINCE_KEY:
no tasks for the orders standing there before). An order of a point's own Avito account (order_points.of_account) is the point's: no task
(order_arrived tells the point, the owner 2026-10-26). The order is never moved to «На ОТХ» from here: a person decides.
The sound (notify.js): an open task rings its assignee's open panel every 2 hours from its creation, day and night (the owner's decision),
a new key per 2 hours (sounds); a done or cancelled task rings no more."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import List, Optional

from . import orders

MARK = "Важное"
ARRIVED = "%s · Позвонить, товары приехали" % MARK
EXPIRES = "%s · Позвонить, истекает срок резерва" % MARK
TITLES = (ARRIVED, EXPIRES)
AUTHOR = "система"
STAGE = "at_point"
EXPIRES_AT = "12:30"                                # the last day of the reservation, from 12:30 (Moscow)
EXPIRES_KEY = "call_task_expires:%d:%s:%s"         # app_state, per order, stay and date: made once
SOUND_EVERY = timedelta(hours=2)
NOTE_MAX = 1000                                     # the system's own text: longer than a person's note (tasks.NOTE_MAX)
LEFT = "заказ ушёл с этапа «%s», задача не нужна"
REOPENED = "Задача снова открыта: %s — заказ вернулся на этап «%s»"
SINCE_KEY = "call_tasks_since"                      # app_state: the first tick of the tasks «Позвонить» (no tasks for older stays)
log = logging.getLogger("tires_sync.orders")


def no_chat(conn, card: dict) -> bool:
    """No Avito chat: the field «Чат Авито» is empty and no chat of «Чаты» is bound to the order."""
    if str(card.get("avito_chat") or "").strip():
        return False
    return conn.execute("SELECT 1 FROM avito_chat WHERE order_id = ? LIMIT 1", (card["id"],)).fetchone() is None


def called(conn, card: dict) -> bool:
    """The client of the order is called, not written to: no Avito chat, and not an order of a point's own account (the point's, order_arrived).
    The alerts these tasks replace are not made for such an order (order_manager, order_arrived, order_reserve)."""
    if not no_chat(conn, card):
        return False
    from . import order_points
    return order_points.of_account(conn, card["id"]) is None


def _wanted(conn, card: dict) -> bool:
    return card["stage"].get("code") == STAGE and not (card.get("intercity") or {}).get("on") and called(conn, card)


def _when(iso) -> str:
    text = str(iso or "")
    if len(text) < 10:
        return "нет"
    return "%s.%s.%s" % (text[8:10], text[5:7], text[:4]) + (" " + text[11:16] if len(text) >= 16 else "")


def note(conn, card: dict) -> str:
    """What the task says: the client, the phone, the positions, the sum, the dates, the link."""
    from . import connections, order_oth
    items = [i for i in card["items"] if int(i["qty"]) >= 1]
    lines = "; ".join("%s (арт. %s), %d шт." % (i["name"], i.get("code") or i.get("goods_id") or "—", int(i["qty"])) for i in items)
    return ("Клиент: %s, телефон %s. Состав: %s. Сумма: %s ₽. Оформлен: %s; прибыл на точку: %s; резерв до: %s. Заказ: %s" % (
        card.get("client_name") or "имя не указано", connections.format_phone(card.get("client_phone") or "") or "не указан",
        lines or "позиций нет", "{:,.0f}".format(orders.totals(items)["sale"]).replace(",", " "), _when(card.get("created_at")),
        _when(card.get("entered_at")), _when(card.get("reserve_until")), order_oth.order_link(conn, card["id"])))[:NOTE_MAX]


def _open(conn, card_id, title: str) -> Optional[int]:
    row = conn.execute("SELECT id FROM callback_task WHERE order_id = ? AND title = ? AND done_at IS NULL AND cancelled_at IS NULL ORDER BY id LIMIT 1",
                       (card_id, title)).fetchone()
    return row[0] if row else None


def _make(conn, card: dict, title: str, now: str) -> int:
    from . import order_notify, tasks
    owner = order_notify.get_owner_login(conn)
    text = note(conn, card)
    task_id = tasks.add(conn, kind="other", title=title, due_at=now[:16], note=text, now=now, author=AUTHOR, owner_login=owner,
                        assignee=tasks.default_assignee(conn, owner, owner, card["id"]), order_id=card["id"])
    conn.execute("UPDATE callback_task SET note = ? WHERE id = ?", (text, task_id))     # the whole text (tasks.add cuts a person's note)
    conn.commit()
    return task_id


def _reopen(conn, card: dict, task_id: int, now: str) -> int:
    from . import store, tasks
    conn.execute("UPDATE callback_task SET cancelled_at = NULL, done_by = '' WHERE id = ? AND cancelled_at IS NOT NULL", (task_id,))
    orders.add_event(conn, card["id"], now, AUTHOR, "task", REOPENED % (tasks._what("other", ARRIVED), orders.stage_name(conn, STAGE)), level="grey")
    store.write_audit(conn, now, AUTHOR, "callback", {"task": int(task_id), "order": card["id"], "reopened": True})
    return task_id


def arrived(conn, card_id, now: str) -> Optional[int]:
    """The order stands on «На точке»: «Позвонить, товары приехали» (made, or opened again), unless it needs none (the doubles: the module's
    text). Returns the task's id when one was made or opened, else None. Never raises."""
    from . import connections
    try:
        card = orders.get_card(conn, card_id)
        if not _wanted(conn, card) or _open(conn, card["id"], ARRIVED):
            return None
        phone = connections.format_phone(card.get("client_phone") or "") or "не указан"
        stay = str(card.get("entered_at") or "")
        for task_id, text, created, done, cancelled, by in conn.execute(
                "SELECT id, note, created_at, done_at, cancelled_at, done_by FROM callback_task WHERE order_id = ? AND title = ? ORDER BY id DESC",
                (card["id"], ARRIVED)).fetchall():
            if done and "телефон %s." % phone in str(text or ""):
                return None                                               # this client was called: a move back calls nobody again
            if cancelled and by != AUTHOR and str(created) >= stay:
                return None                                               # a person cancelled it in this stay: their decision
            if cancelled and by == AUTHOR and str(cancelled)[:10] == now[:10]:
                return _reopen(conn, card, task_id, now)                  # left and came back today: the same task again
        return _make(conn, card, ARRIVED, now)
    except Exception:
        conn.rollback()
        log.exception("the task «%s» of card %s was not made", ARRIVED, card_id)
        return None


def expiring(conn, card: dict, now: str, past: bool = True) -> Optional[int]:
    """The last day of the reservation from 12:30: «Позвонить, истекает срок резерва», once per stay and date; `past`: a date already gone
    too (the tick missed the day; not for the stays before SINCE_KEY, told by the old alert of the reservation). Never raises."""
    try:
        until = str(card.get("reserve_until") or "")
        if not _wanted(conn, card) or not until or until > now[:10] or (until == now[:10] and now[11:16] < EXPIRES_AT) or (until < now[:10] and not past):
            return None
        key = EXPIRES_KEY % (card["id"], re.sub(r"\D", "", card["entered_at"] or ""), until)
        if conn.execute("SELECT 1 FROM app_state WHERE key = ?", (key,)).fetchone():
            return None
        conn.execute("INSERT OR IGNORE INTO app_state (key, value) VALUES (?, ?)", (key, now))
        if _open(conn, card["id"], EXPIRES):
            conn.commit()
            return None
        return _make(conn, card, EXPIRES, now)                             # tasks.add saves the key with the task
    except Exception:
        conn.rollback()
        log.exception("the task «%s» of card %s was not made", EXPIRES, card.get("id"))
        return None


def close_left(conn, card_id, now: str, who: str = AUTHOR) -> int:
    """The order is no longer on «На точке»: its open tasks «Позвонить…» are cancelled (a line in its journal each). Never raises."""
    from . import tasks
    closed = 0
    try:
        if conn.execute("SELECT 1 FROM order_card c JOIN order_stage s ON s.id = c.stage_id WHERE c.id = ? AND s.code = ?", (card_id, STAGE)).fetchone():
            return 0
        result = LEFT % orders.stage_name(conn, STAGE)
        for (task_id,) in conn.execute("SELECT id FROM callback_task WHERE order_id = ? AND title IN (?, ?) AND done_at IS NULL AND cancelled_at IS NULL "
                                       "ORDER BY id", (card_id,) + TITLES).fetchall():
            try:
                tasks.close(conn, task_id, "cancel", now, who, result)
                closed += 1
            except tasks.TaskError:                                       # closed meanwhile
                conn.rollback()
    except Exception:
        conn.rollback()
        log.exception("the tasks «Позвонить» of card %s were not closed", card_id)
    return closed


def moved(conn, card_id, was: str, to: str, now: str, make: bool = True) -> None:
    """orders.move_card, after the move is saved: into «На точке» the task (not with `make` False: the bulk move); out of it the open
    tasks closed. Never raises."""
    if to == STAGE and was != STAGE and make:
        arrived(conn, card_id, now)
    elif was == STAGE and to != STAGE:
        close_left(conn, card_id, now)


def _since(conn, now: str) -> str:
    conn.execute("INSERT OR IGNORE INTO app_state (key, value) VALUES (?, ?)", (SINCE_KEY, now))
    conn.commit()
    return conn.execute("SELECT value FROM app_state WHERE key = ?", (SINCE_KEY,)).fetchone()[0]


def tick(conn, now: str) -> int:
    """The hourly tick (and 12:30): the open tasks of orders gone from «На точке» closed; for the stays at the point since the first tick, the
    task of the arrival a move past orders.move_card left out and the last day of the reservation. Returns how many tasks were made or opened."""
    made = 0
    since = _since(conn, now)
    for (card_id,) in conn.execute("SELECT DISTINCT order_id FROM callback_task WHERE title IN (?, ?) AND done_at IS NULL AND cancelled_at IS NULL "
                                   "AND order_id IS NOT NULL", TITLES).fetchall():
        close_left(conn, card_id, now)
    for (card_id,) in conn.execute("SELECT c.id FROM order_card c JOIN order_stage s ON s.id = c.stage_id WHERE s.code = ? ORDER BY c.id",
                                   (STAGE,)).fetchall():
        try:
            card = orders.get_card(conn, card_id)
        except Exception:
            conn.rollback()
            log.exception("card %s was not read for the tasks «Позвонить»", card_id)
            continue
        new = str(card.get("entered_at") or "") >= since                  # came after the first tick: not an order standing there before
        if new:
            made += arrived(conn, card_id, now) is not None
        made += expiring(conn, card, now, past=new) is not None
    return made


def sounds(conn, login: str, now: str) -> List[dict]:
    """The items of notify.js of the open tasks «Позвонить…» set to `login`: a new key every 2 hours since the task was made."""
    out = []
    try:
        moment = datetime.fromisoformat(now[:19])
    except ValueError:
        return out
    from . import order_manager
    for task_id, order_id, title, created in conn.execute(
            "SELECT id, order_id, title, created_at FROM callback_task WHERE assignee = ? AND title IN (?, ?) AND done_at IS NULL AND cancelled_at IS NULL "
            "ORDER BY id", (login,) + TITLES).fetchall():
        try:
            step = max(0, int((moment - datetime.fromisoformat(str(created)[:19])).total_seconds() // SOUND_EVERY.total_seconds()))
        except ValueError:
            step = 0
        try:
            label = order_manager.label(orders.get_card(conn, order_id)) if order_id else ""
        except Exception:                                                 # noqa: BLE001 - the sound still rings
            conn.rollback()
            label = order_id
        out.append({"key": "call-task-%d-%d" % (task_id, step), "title": title, "text": "Заказ №%s" % label,
                    "href": "/orders/%d" % order_id if order_id else "/tasks"})
    return out
