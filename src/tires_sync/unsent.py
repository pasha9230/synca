"""«Не дошло клиенту» in the order card (the owner 2026-10-31: «чтобы я мог его скопировать и отправить вручную»): a message to the client
that did not go — Avito refused it for good (a 402 after the retries, another refusal), «можно забирать» that could not go (no chat of
Avito and the like) — with when, why and its whole text; and the message tasks of the rules in «ошибка» that have their text. Nothing is
sent from here: the person copies the text, writes it to the client and presses «Отправил вручную» (the order's journal and the audit say
who and when; the block is gone and never comes back).

order_unsent keeps what chat_send_log drops on «failed» (its text): one row per send token, written where the send is marked «не
отправлено», inside a savepoint — a failure here never touches the send, its retries nor its records. A send that went after all (the
token «sent» in chat_send_log) is never shown."""
from __future__ import annotations

import logging
from typing import List

from . import store

log = logging.getLogger("tires_sync.unsent")
SOURCES = {"chat": "чат Авито", "arrived": "«можно забирать»"}
NO_CHAT = "нет чата Авито"                         # order_engine.send_target: the panel cannot send it, only a person can
TEXT_MAX, WHY_MAX = 4000, 500


class UnsentError(ValueError):
    """A refused «Отправил вручную»; the text is for the person."""


def record(conn, order_id, token: str, at: str, why: str, text: str, source: str = "chat") -> None:
    """The message to the client of `order_id` did not go: kept with its text and reason (one row per token: a send that failed again comes back with its last text and reason). Never raises
    and never commits: in the caller's transaction, by a savepoint — the caller's own writes stay as they are whatever happens here."""
    text = str(text or "").strip()
    try:
        if not order_id or int(order_id) <= 0 or not token or not text:
            return
        conn.execute("SAVEPOINT unsent")
        try:
            conn.execute("INSERT INTO order_unsent (order_id, token, at, why, text, source) VALUES (?,?,?,?,?,?) ON CONFLICT (token) DO UPDATE SET "
                         "order_id = excluded.order_id, at = excluded.at, why = excluded.why, text = excluded.text, source = excluded.source, "
                         "closed_at = NULL, closed_by = ''",   # the same send failed again (a task's token is one for every press): back, its last
                         (int(order_id), str(token)[:200], at, str(why or "")[:WHY_MAX], text[:TEXT_MAX], source if source in SOURCES else "chat"))
            conn.execute("RELEASE SAVEPOINT unsent")
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT unsent")
            conn.execute("RELEASE SAVEPOINT unsent")
            raise
    except Exception:
        log.warning("the unsent message %s of order %s could not be kept for the card", token, order_id, exc_info=True)


def orders_of(conn, order_id) -> List[int]:
    """The orders whose kept sends the card of `order_id` shows: its own and those of the orders merged into it (order_merge: their tyres,
    chats and calls are there; «Разъединить» gives them back by itself — nothing is moved). A source merged into another shows none."""
    from . import order_merge
    order_id = int(order_id)
    if order_merge.moved_into(conn, order_id) is not None:
        return []
    return [order_id] + [r[0] for r in conn.execute(
        "SELECT s.order_id FROM order_merge_source s JOIN order_merge m ON m.id = s.merge_id WHERE m.into_order = ? AND s.ref = 0 AND "
        + order_merge._ACTIVE, (order_id,))]


def of_order(conn, order_id) -> List[dict]:
    """What did not reach the client of the order and is not marked «Отправил вручную»: the kept sends (not those that went after all) and
    the message tasks to the client in «ошибка» with their text, the oldest first."""
    out = []
    ids = orders_of(conn, order_id)
    if not ids:
        return out
    for rid, token, at, why, text, source in conn.execute(
            "SELECT u.id, u.token, u.at, u.why, u.text, u.source FROM order_unsent u WHERE u.order_id IN (%s) AND u.closed_at IS NULL AND NOT EXISTS "
            "(SELECT 1 FROM chat_send_log l WHERE l.token = u.token AND l.status = 'sent') ORDER BY u.at, u.id" % ",".join("?" * len(ids)), ids):
        out.append({"kind": "send", "id": rid, "at": at, "why": why, "text": text, "source": SOURCES.get(source, source)})
    from . import order_engine
    for task in order_engine.list_tasks(conn, order_id, ("error", "pending")):
        if task["kind"] != "message" or not (task["body"] or "").strip() or order_engine.audience(conn, task) != "client":
            continue
        why = task["result"]
        if task["status"] == "pending":                   # waits — shown only when the panel cannot send it at all: the order has no chat
            _chat, why = order_engine.send_target(conn, task)
            if NO_CHAT not in why:
                continue
        out.append({"kind": "task", "id": task["id"], "at": task["created_at"], "why": why, "text": task["body"],
                    "source": "сообщение «%s»" % task["title"] if task["title"] else "сообщение по правилу"})
    out.sort(key=lambda u: u["at"] or "")
    return out


def sent_by_hand(conn, order_id, kind: str, item_id, now: str, who: str) -> None:
    """«Отправил вручную»: the row (or the rule's task) is closed once, a line in the order's journal and the audit. UnsentError when it is
    not of this order or closed already."""
    from . import order_engine, orders
    order_id = int(order_id)
    if kind == "send":
        ids = orders_of(conn, order_id) or [-1]
        cur = conn.execute("UPDATE order_unsent SET closed_at = ?, closed_by = ? WHERE id = ? AND order_id IN (%s) AND closed_at IS NULL"
                           % ",".join("?" * len(ids)), [now, who, int(item_id)] + ids)
        if cur.rowcount != 1:
            conn.rollback()
            raise UnsentError("Это сообщение уже отмечено или не этого заказа.")
        token = conn.execute("SELECT token FROM order_unsent WHERE id = ?", (int(item_id),)).fetchone()[0]
        task = order_engine.task_of_token(conn, token)       # the rule's message of that send (the review): ticked off too — never a second
        if task is not None and task["status"] in ("pending", "error"):    # «Отправить клиенту в Авито» of the same text afterwards
            conn.execute("UPDATE order_task SET status = 'done', result = ?, done_at = ? WHERE id = ? AND status IN ('pending', 'error')",
                         ("отправлено вручную", now, task["id"]))
    elif kind == "task":
        try:
            task = order_engine.get_task(conn, int(item_id))
        except Exception:
            raise UnsentError("Это сообщение уже отмечено или не этого заказа.") from None
        shown = {t["id"] for t in of_order(conn, order_id) if t["kind"] == "task"}     # only what the block shows (the review: never
        if task["order_id"] != order_id or task["id"] not in shown:                      # another message of the order, to the manager or
            raise UnsentError("Это сообщение уже отмечено или не этого заказа.")       # one the panel can send)
        cur = conn.execute("UPDATE order_task SET status = 'done', result = ?, done_at = ? WHERE id = ? AND status IN ('error', 'pending')",
                           ("отправлено вручную", now, int(item_id)))                # as «Отправлено вручную» of the window ticks it off
        if cur.rowcount != 1:
            conn.rollback()
            raise UnsentError("Это сообщение уже отмечено.")
    else:
        raise UnsentError("Неизвестное сообщение.")
    orders.add_event(conn, order_id, now, who, "chat", "Сообщение клиенту отправлено вручную (не дошло из панели)", level="grey")
    store.write_audit(conn, now, who, "unsent_by_hand", {"order": order_id, "kind": kind, "id": int(item_id)})
    if kind == "send":                                        # after the mark is saved (it commits or rolls back on its own): the lines of
        from . import chats                                   # «Входящие» about the send are answered — nobody writes it to the client twice
        chats._inbox_close(conn, token, now, "Итог: отправлено вручную из карточки заказа (%s)." % who, alerts=True)
