"""«Автосообщение клиенту» (the owner 2026-11-05/06): in the order or in its chat the manager sets a message to the client — a date, a time and
the text (the template's by default, order_templates.AUTOSEND_*_SAMPLE, which the manager may change); at that time the system writes it in the
order's Avito chat by itself. Two kinds:
- «Спросить, решил ли брать» (ask): cancelled when the client answered after it was set, or the order is closed;
- «Договорённость» (deal): the client's answer does not cancel it, only a closed order does.
The only channel is the order's Avito chat, by order_manager.send_to_client like the other messages that go by themselves (order_reserve,
order_wait): one send mark per message (`autosend-<order>-<id>`), never a second one when it is not known whether it went; a 402 is sent again
by the minute's sync as every send (chats.settle_sends), which stops when the message is cancelled meanwhile (stop_reason). What did not go
(no chat, no keys, a refusal, a 402, a busy base) is a task of «Задачи» for the responsible of the office (else the owner): nothing is sent
again around the send mark. The order's journal says who set it, for when, of what kind, and that it went or was cancelled and why; until it
goes the manager may change or cancel it. A manager of a point never sees any of it (the panel hides the block, the records and the alerts).
Run every minute after the chats are read (cli._autosend); nothing here writes to Avito but `deliver`."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Callable, List, Optional

from . import order_templates as templates, orders, store

KINDS = {"ask": "Спросить, решил ли брать", "deal": "Договорённость"}
SAMPLE = {"ask": templates.AUTOSEND_ASK_SAMPLE, "deal": templates.AUTOSEND_DEAL_SAMPLE}
STATUS_NAMES = {"pending": "ждёт", "sending": "отправляется", "waiting": "неизвестно, ушло ли", "sent": "отправлено", "cancelled": "отменено",
                "failed": "не отправлено"}
OPEN = ("pending", "sending", "waiting")
EVENT = "autosend"                                  # order_event.kind of every record here: hidden from a point (panel, order_alerts)
AUTHOR = "автосообщение"
TOKEN = "autosend-%d-%d"
TOKEN_RE = re.compile(r"autosend-(\d+)-(\d+)$")
TASK_TITLE = "Автосообщение клиенту не ушло"        # the start of the «Задачи» title; the panel hides its journal line from a point
DUE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")
LATE = timedelta(hours=2)                           # a message the system did not reach this long after its time does not go (a task instead)
AHEAD = timedelta(days=90)
MAX_TEXT = 2000                                     # chats.MAX_REPLY
FIELDS = "id, order_id, kind, due_at, text, status, reason, created_at, created_by, updated_at, updated_by, done_at, task_id"
Deliver = Callable[[int, str, str], None]
log = logging.getLogger("tires_sync.order_autosend")


class AutosendError(ValueError):
    """A refused set, change or cancel; the text is for the person."""


def is_token(value) -> bool:
    """The send mark of an auto message (TOKEN), as chats.send_reply reads a mark: stripped. A manager of a point never acts on one
    (lane autosend-guard: «Отправить снова», a send under its mark, «Отправил вручную» are refused on the server, not only hidden)."""
    return str(value or "").strip().startswith("autosend-")


def is_task(task: dict) -> bool:
    """The task «Автосообщение клиенту не ушло» (failed): the office's, never a point's to close."""
    return str(task.get("title") or "").startswith(TASK_TITLE)


def for_office(event: dict) -> bool:
    """A record of the order's journal that a manager of a point must not see: this module's own, and the «Задачи» line of its task."""
    return event.get("kind") == EVENT or (event.get("kind") == "task" and TASK_TITLE in str(event.get("text") or ""))


def office_sends(sends: List[dict], point: bool) -> List[dict]:
    """The unsettled sends shown above a chat and in the card (chats.pending_sends): without the system's (this module's among them,
    chats.is_system_mark, lane token-guard) for a manager of a point."""
    from . import chats
    return [s for s in sends if not chats.is_system_mark(s.get("token"))] if point else sends


def _row(row) -> dict:
    item = dict(zip([f.strip() for f in FIELDS.split(",")], row))
    item["kind_name"] = KINDS.get(item["kind"], item["kind"])
    item["status_name"] = STATUS_NAMES.get(item["status"], item["status"])
    item["token"] = TOKEN % (item["order_id"], item["id"])
    return item


def get(conn, autosend_id) -> dict:
    row = conn.execute("SELECT %s FROM order_autosend WHERE id = ?" % FIELDS, (autosend_id,)).fetchone()
    if row is None:
        raise AutosendError("Такого автосообщения нет.")
    return _row(row)


def of_order(conn, order_id) -> List[dict]:
    """The order's auto messages, the open ones first by their time, then the others newest first."""
    rows = [_row(r) for r in conn.execute("SELECT %s FROM order_autosend WHERE order_id = ? ORDER BY id DESC" % FIELDS, (order_id,))]
    return sorted([r for r in rows if r["status"] in OPEN], key=lambda r: r["due_at"]) + [r for r in rows if r["status"] not in OPEN]


def default_text(conn, kind: str) -> str:
    """The text of the form: the owner's template of that name, else the sample."""
    from . import order_manager
    return order_manager.template_body(conn, SAMPLE[kind])


def _shown(due: str) -> str:
    return "%s.%s.%s %s" % (due[8:10], due[5:7], due[0:4], due[11:16])


def _closed(card: dict) -> str:
    """Why the order counts as closed ('' when open): any stage that is not «обычный» — «Выполнено», «Отмена», «Вернули поставщику»."""
    return "заказ закрыт («%s»)" % card["stage"]["name"] if card["stage"].get("kind") != "open" else ""


def _clean(conn, kind, due_at, text, now: str) -> tuple:
    if kind not in KINDS:
        raise AutosendError("Выберите тип автосообщения.")
    due = str(due_at or "").strip()[:16].replace(" ", "T")
    try:
        moment = datetime.strptime(due, "%Y-%m-%dT%H:%M") if DUE.match(due) else None
    except ValueError:
        moment = None
    if moment is None:
        raise AutosendError("Укажите дату и время.")
    if due <= now[:16]:
        raise AutosendError("Это время уже прошло: укажите время позже текущего.")
    if moment - datetime.fromisoformat(now[:16]) > AHEAD:
        raise AutosendError("Не дальше чем на %d дней вперёд." % AHEAD.days)
    from .chats import CONTROL
    body = CONTROL.sub("", str(text or "").replace("\r", "")).strip()
    if not body or len(body) > MAX_TEXT:
        raise AutosendError("Текст: от 1 до %d знаков." % MAX_TEXT)
    left = templates.unfilled(body)
    if left:
        raise AutosendError("В тексте остались незаполненные поля %s." % ", ".join(left))
    return due, body


def add(conn, order_id, kind: str, due_at: str, text: str, now: str, who: str) -> int:
    """A new auto message of an open order; a grey record of the journal (no alert) and the audit."""
    card = orders.get_card(conn, int(order_id))
    why = _closed(card)
    if why:
        raise AutosendError("Автосообщение не поставлено: %s." % why)
    due, body = _clean(conn, kind, due_at, text, now)
    autosend_id = conn.execute("INSERT INTO order_autosend (order_id, kind, due_at, text, status, created_at, created_by, updated_at, updated_by) "
                               "VALUES (?,?,?,?, 'pending', ?,?,?,?)", (card["id"], kind, due, body, now, who, now, who)).lastrowid
    orders.add_event(conn, card["id"], now, who, EVENT, "Автосообщение клиенту поставлено: «%s» на %s. Текст: %s" % (KINDS[kind], _shown(due), body),
                     level="grey")
    store.write_audit(conn, now, who, "autosend_add", {"order": card["id"], "autosend": autosend_id, "kind": kind, "due": due})
    conn.commit()
    return autosend_id


def update(conn, autosend_id, due_at: str, text: str, now: str, who: str, order_id=None) -> None:
    """A change of the time and the text, only while it waits (never once it is being sent); the kind stays."""
    item = get(conn, autosend_id)
    if order_id is not None and item["order_id"] != int(order_id):
        raise AutosendError("Такого автосообщения нет.")
    due, body = _clean(conn, item["kind"], due_at, text, now)
    if conn.execute("UPDATE order_autosend SET due_at = ?, text = ?, updated_at = ?, updated_by = ? WHERE id = ? AND status = 'pending'",
                    (due, body, now, who, item["id"])).rowcount != 1:
        conn.rollback()
        raise AutosendError("Автосообщение уже не ждёт (%s): изменить нельзя." % get(conn, autosend_id)["status_name"])
    orders.add_event(conn, item["order_id"], now, who, EVENT, "Автосообщение клиенту изменено: «%s» на %s. Текст: %s" % (item["kind_name"], _shown(due), body),
                     level="grey")
    store.write_audit(conn, now, who, "autosend_update", {"order": item["order_id"], "autosend": item["id"], "due": due})
    conn.commit()


def cancel(conn, autosend_id, now: str, who: str, order_id=None) -> None:
    """«Отменить» by a person, only while it waits."""
    item = get(conn, autosend_id)
    if order_id is not None and item["order_id"] != int(order_id):
        raise AutosendError("Такого автосообщения нет.")
    if not _cancel(conn, item, "отменено вручную", now, who, ("pending",)):
        conn.rollback()
        raise AutosendError("Автосообщение уже не ждёт (%s): отменить нельзя." % get(conn, autosend_id)["status_name"])
    store.write_audit(conn, now, who, "autosend_cancel", {"order": item["order_id"], "autosend": item["id"]})
    conn.commit()


def _cancel(conn, item: dict, reason: str, now: str, who: str, frm=OPEN) -> bool:
    """Cancelled once (one UPDATE from the states `frm`): its grey record. No commit."""
    if conn.execute("UPDATE order_autosend SET status = 'cancelled', reason = ?, updated_at = ?, updated_by = ?, done_at = ? WHERE id = ? AND status IN (%s)"
                    % ",".join("?" * len(frm)), (reason, now, who, now, item["id"]) + tuple(frm)).rowcount != 1:
        return False
    orders.add_event(conn, item["order_id"], now, who, EVENT, "Автосообщение клиенту отменено («%s» на %s): %s" % (item["kind_name"], _shown(item["due_at"]), reason),
                     level="grey")
    return True


def _answered(conn, card: dict, since: str) -> bool:
    """The client answered after `since` (order_wait._answered_anywhere: a message in a chat of the order or of its client, a call that took place)."""
    from . import order_manager, order_wait
    chat, _why = order_manager._client_chat(conn, card["id"])
    return order_wait._answered_anywhere(conn, card, (chat or 0, "", since))


def _why_not(conn, item: dict) -> str:
    """Why this message must not go now ('' when it may): the order deleted or closed; of kind «ask», the client answered since it was set."""
    try:
        card = orders.get_card(conn, item["order_id"])
    except orders.OrderError:
        return "заказ удалён"
    why = _closed(card)
    if why:
        return why
    if item["kind"] == "ask" and _answered(conn, card, item["created_at"]):
        return "клиент ответил"
    return ""


def stop_reason(conn, token: str) -> str:
    """For the sync's retry of a 402 (chats._stopped): why it must not be sent again ('' when it may)."""
    found = TOKEN_RE.match(str(token or ""))
    if not found:
        return ""
    row = conn.execute("SELECT %s FROM order_autosend WHERE id = ? AND order_id = ?" % FIELDS, (int(found.group(2)), int(found.group(1)))).fetchone()
    if row is None:
        return "автосообщение удалено"
    item = _row(row)
    if item["status"] == "cancelled":
        return "автосообщение отменено: %s" % (item["reason"] or "вручную")
    return _why_not(conn, item)


def _task(conn, item: dict, why: str, note: str, now: str) -> None:
    """The task of the responsible of the office (tasks.default_assignee: the order's responsible, else the owner), once per message. No raise."""
    if item.get("task_id"):
        return
    if get(conn, item["id"])["status"] == "cancelled":               # cancelled meanwhile (the client changed): no task with its old text
        return
    mark = conn.execute("SELECT c.order_id FROM chat_send_log l LEFT JOIN avito_chat c ON c.id = l.chat_row WHERE l.token = ?", (item["token"],)).fetchone()
    if mark is not None and mark[0] != item["order_id"]:             # its chat is no longer the order's (oth_swap: another client now): never
        return                                                       # «напишите клиенту вручную» with the old client's text (night-build)
    from . import order_notify, tasks
    try:
        owner = order_notify.get_owner_login(conn)
        task_id = tasks.add(conn, kind="other", title="%s: заказ №%d" % (TASK_TITLE, item["order_id"]), due_at=now[:16],
                            note="%s. %s Текст: %s" % (why.rstrip(". "), note, item["text"]), now=now, author=AUTHOR, owner_login=owner,
                            assignee=tasks.default_assignee(conn, owner, owner, item["order_id"]), order_id=item["order_id"])
        conn.execute("UPDATE order_autosend SET task_id = ? WHERE id = ?", (task_id, item["id"]))
        conn.commit()
    except Exception:
        conn.rollback()
        log.exception("the task about auto message %s could not be made", item["id"])


BY_HAND = "Проверьте чат Авито заказа и напишите клиенту вручную."
NOT_TWICE = "Проверьте чат Авито заказа: если сообщения там нет, напишите клиенту вручную. Повторно само не отправится."
RETRY = "Система повторит сама в течение 30 минут; если не выйдет, в ленте заказа будет «не отправлено». Вручную не дублируйте, пока не проверите чат."
LOST = "не дошло (в чате Авито его нет): отправьте его кнопкой «Отправить снова» в чате или напишите клиенту вручную"


def _send(conn, item: dict, deliver: Optional[Deliver], now: str, started: bool) -> str:
    """Claim (one UPDATE from pending, or a «sending» left by a stopped run), send once by its mark, write the outcome. `started`: its send
    mark exists already (a run stopped in the middle): only the mark settles it, never «too late»."""
    from . import order_manager
    if conn.execute("UPDATE order_autosend SET status = 'sending', updated_at = ? WHERE id = ? AND status = ? AND updated_at = ?",
                    (now, item["id"], item["status"], item["updated_at"])).rowcount != 1:
        conn.rollback()
        return ""                                                    # another run, a change or a cancel took it
    conn.commit()
    item = get(conn, item["id"])                                     # the words as they are now
    if item["status"] != "sending":                                  # cancelled meanwhile (the client changed, oth_swap): nothing goes (night-build)
        return ""
    card = orders.get_card(conn, item["order_id"])
    if not started and datetime.fromisoformat(now[:16]) - datetime.fromisoformat(item["due_at"]) > LATE:
        return _failed(conn, item, "не ушло вовремя: система не отправляла сообщения с %s" % _shown(item["due_at"]), BY_HAND, now)
    if started:                                                      # a refusal or «не дошло» already settled by its mark: final, no new try
        mark = conn.execute("SELECT status, error FROM chat_send_log WHERE token = ?", (item["token"],)).fetchone()
        if mark and mark[0] == "failed":
            return _failed(conn, item, "Авито не принял сообщение%s" % (": " + str(mark[1])[:200] if mark[1] else ""), BY_HAND, now)
        if mark and mark[0] == "lost":
            return _failed(conn, item, LOST, BY_HAND, now)
    status, why, _copy = order_manager.send_to_client(conn, card, item["text"], item["token"], deliver, now)
    if status == "sent":
        return _sent(conn, item, now)
    if status == "failed":
        return _failed(conn, item, why, BY_HAND, now)
    mark = conn.execute("SELECT status FROM chat_send_log WHERE token = ?", (item["token"],)).fetchone()
    retry = bool(mark) and mark[0] == "retry"
    conn.execute("UPDATE order_autosend SET status = 'waiting', reason = ?, updated_at = ? WHERE id = ?", (why, now, item["id"]))
    orders.add_event(conn, item["order_id"], now, AUTHOR, EVENT, "Автосообщение клиенту («%s» на %s): %s" % (item["kind_name"], _shown(item["due_at"]), why),
                     level="yellow" if retry else "red")
    conn.commit()
    _task(conn, item, "Автосообщение клиенту: %s" % why, RETRY if retry else NOT_TWICE, now)
    return "waiting"


def _sent(conn, item: dict, now: str) -> str:
    conn.execute("UPDATE order_autosend SET status = 'sent', reason = '', updated_at = ?, done_at = ? WHERE id = ?", (now, now, item["id"]))
    orders.add_event(conn, item["order_id"], now, AUTHOR, EVENT, "Автосообщение клиенту отправлено («%s» на %s, поставил(а) %s): %s" % (
        item["kind_name"], _shown(item["due_at"]), item["created_by"], item["text"]), level="green")
    conn.commit()
    _close_task(conn, item, "done", "ушло само", now)
    return "sent"


def _close_task(conn, item: dict, how: str, result: str, now: str) -> None:
    """The task of the responsible about this message, once it went by itself or may no longer go. No raise (closed by a person already)."""
    if not item.get("task_id"):
        return
    from . import tasks
    try:
        tasks.close(conn, item["task_id"], how, now, AUTHOR, result)
        conn.commit()
    except Exception:
        conn.rollback()


def _failed(conn, item: dict, why: str, note: str, now: str) -> str:
    conn.execute("UPDATE order_autosend SET status = 'failed', reason = ?, updated_at = ?, done_at = ? WHERE id = ?", (why, now, now, item["id"]))
    if item.get("task_id"):                                          # its task from «неизвестно, ушло ли» says now what to do
        conn.execute("UPDATE callback_task SET note = ? WHERE id = ? AND done_at IS NULL AND cancelled_at IS NULL",
                     (("Не отправлено: %s. %s Текст: %s" % (why.rstrip(". "), note, item["text"]))[:300], item["task_id"]))
    orders.add_event(conn, item["order_id"], now, AUTHOR, EVENT, "Автосообщение клиенту не отправлено («%s» на %s): %s" % (
        item["kind_name"], _shown(item["due_at"]), why), level="red")
    conn.commit()
    _task(conn, item, "Не отправлено: %s" % why, note, now)
    return "failed"


def _settle(conn, item: dict, now: str) -> str:
    """A message whose outcome was not known (a 402 the sync sends again, a lost answer): what its send mark says now."""
    mark = conn.execute("SELECT status, error FROM chat_send_log WHERE token = ?", (item["token"],)).fetchone()
    if mark is not None and mark[0] == "lost":                       # not in the chat: it goes again only by a person's «Отправить снова»
        return _failed(conn, item, LOST, BY_HAND, now)
    if mark is None or mark[0] not in ("sent", "failed"):
        return ""
    if mark[0] == "sent":
        return _sent(conn, item, now)
    error = str(mark[1] or "")
    if error.startswith("остановлено: "):                            # the sync stopped it: cancelled or the order closed (stop_reason)
        if _cancel(conn, item, error[len("остановлено: "):], now, AUTHOR, ("waiting",)):
            conn.commit()
            _close_task(conn, item, "cancel", "автосообщение отменено: " + error[len("остановлено: "):], now)
            return "cancelled"
        return ""
    return _failed(conn, item, "Авито не принял сообщение и система больше не повторяет%s" % (": " + error[:200] if error else ""), BY_HAND, now)


def run(conn, now: str, deliver: Optional[Deliver]) -> dict:
    """Every minute: cancel what may no longer go, send what is due, settle what waits. One message never stops the others; a busy base
    leaves the message as it was for the next run (its send mark keeps it from going twice). Returns the counts of the outcomes."""
    from . import chats
    made = {"sent": 0, "failed": 0, "waiting": 0, "cancelled": 0}
    rows = [_row(r) for r in conn.execute("SELECT %s FROM order_autosend WHERE status IN ('pending', 'sending', 'waiting') ORDER BY due_at, id" % FIELDS)]
    rows += [_row(r) for r in conn.execute(                          # «не отправлено», then sent by a person's «Отправить снова» under its mark
        "SELECT %s FROM order_autosend a WHERE a.status = 'failed' AND EXISTS (SELECT 1 FROM chat_send_log l WHERE l.status = 'sent' AND "
        "l.token = 'autosend-' || a.order_id || '-' || a.id) ORDER BY a.id" % FIELDS)]
    for item in rows:
        try:
            if item["status"] == "failed":                           # went after all: one green record, its task closed
                outcome = _sent(conn, item, now)
            elif item["status"] == "waiting":
                outcome = _settle(conn, item, now)
            elif item["status"] == "sending" and not chats.sending_is_stale(item["updated_at"], now):
                continue                                             # another run is at it
            else:
                logged = conn.execute("SELECT 1 FROM chat_send_log WHERE token = ?", (item["token"],)).fetchone() is not None
                why = "" if logged else _why_not(conn, item)         # a send already started is settled by its mark only
                if why:
                    outcome = "cancelled" if _cancel(conn, item, why, now, AUTHOR, ("pending", "sending")) else ""
                    conn.commit()
                elif item["due_at"] <= now[:16]:
                    outcome = _send(conn, item, deliver, now, logged)
                else:
                    continue
            if outcome in made:
                made[outcome] += 1
        except Exception:
            conn.rollback()
            log.exception("auto message %s", item["id"])
    return made
