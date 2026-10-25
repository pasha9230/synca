"""«Убрать пустые карточки из «Нового заказа»»: deals made from Avito chats that nobody worked on go to «Удалённые» in one go.
A copy of the base is taken first; each card is checked again inside its own delete, so a card touched meanwhile stays. The chats stay."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from . import chat_deals, order_notify, order_wait, orders, store, trash
from .orders import OrderError

FRESH_DAYS = 2
SYSTEM_AUTHORS = {chat_deals.SYSTEM_AUTHOR, "система", order_notify.AUTHOR, order_wait.AUTHOR}
ACTION = "orders_cleanup_new"

# why a card stays, in the order they are checked (the first that fits is shown)
REASONS = {
    "stage": "не на этапе «Новый заказ»",
    "not_chat": "создана не из чата Авито (вручную в «Заказах»)",
    "by_person": "создана менеджером кнопкой «Создать сделку» в чате",
    "webmim": "есть номер или статус WebMIM",
    "sale": "записана продажа",
    "moved": "карточку переводили по этапам",
    "wants": "заполнено «Что нужно клиенту»",
    "fields": "заполнены дополнительные поля",
    "tasks": "есть выполненные, начатые или пропущенные задачи",
    "edited": "есть ручные правки, заметки или позиции, добавленные позже",
    "fresh": "свежая: создана или клиент писал за последние %d дня" % FRESH_DAYS,
}


def _system(author: str) -> bool:
    return author in SYSTEM_AUTHORS or author.startswith("правило №")


def cutoff_for(now_local: str) -> str:
    return (datetime.fromisoformat(now_local) - timedelta(days=FRESH_DAYS)).isoformat(timespec="seconds")


def keep_reason(conn, card: dict, cutoff: str) -> Optional[str]:
    """The key of REASONS why the card is a real deal (or may become one), or None when it is an empty card made from a chat.
    What was made together with the card (its first position, the chat link, the «Ранее писал» note) is not a person's work: it has the card's own time."""
    if card["stage_id"] != orders._first_stage(conn)["id"]:
        return "stage"
    chat = orders.CHAT_REF.match(card["avito_chat"] or "")
    if card["source"] != "avito" or not chat:
        return "not_chat"
    if card["created_by"] != chat_deals.SYSTEM_AUTHOR:                     # a person made it on purpose: only the automatic ones go
        return "by_person"
    if card["webmim_id"] or card["webmim_number"] or card["webmim_status"]:
        return "webmim"
    if json.loads(card["sale_ids"] or "[]"):
        return "sale"
    events = conn.execute("SELECT at, author, kind FROM order_event WHERE order_id = ?", (card["id"],)).fetchall()
    if any(kind == "stage" for _at, _author, kind in events):
        return "moved"
    if conn.execute("SELECT 1 FROM order_want WHERE order_id = ?", (card["id"],)).fetchone():
        return "wants"
    if conn.execute("SELECT 1 FROM order_field_value WHERE order_id = ? AND TRIM(value) != ''", (card["id"],)).fetchone():
        return "fields"
    if conn.execute("SELECT 1 FROM order_task WHERE order_id = ? AND status != 'pending'", (card["id"],)).fetchone():
        return "tasks"
    if any(not _system(author) and at != card["created_at"] for at, author, _kind in events):
        return "edited"
    last_in = conn.execute("SELECT MAX(at) FROM avito_message WHERE chat_row = ? AND direction = 'in'", (int(chat.group(1)),)).fetchone()[0] or ""
    if card["created_at"] >= cutoff or last_in >= cutoff:
        return "fresh"
    return None


def _cards(conn) -> List[dict]:
    cur = conn.execute("SELECT * FROM order_card WHERE stage_id = ? ORDER BY id", (orders._first_stage(conn)["id"],))
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def preview(conn, now_local: str) -> dict:
    """What the button would do now: the cards it removes and how many stay on «Новый заказ», by reason."""
    cutoff = cutoff_for(now_local)
    remove, kept = [], {}
    for card in _cards(conn):
        reason = keep_reason(conn, card, cutoff)
        if reason:
            kept[reason] = kept.get(reason, 0) + 1
        else:
            remove.append(card)
    return {"remove": remove, "kept": [(REASONS[k], kept[k]) for k in REASONS if k in kept], "total": len(remove) + sum(kept.values()),
            "sign": sign(remove)}


def sign(cards) -> str:
    """Which cards the person saw: the button removes exactly them, or nothing."""
    return hashlib.sha256(",".join(str(c["id"]) for c in cards).encode()).hexdigest()[:16]


def backup(db_path, stamp: str, what: str = "cleanup-new") -> Path:
    """A whole copy of the base next to it, `pre-<what>-<stamp>.db`, checked before anything is changed."""
    target = Path(db_path).resolve().parent / ("pre-%s-%s.db" % (what, stamp))
    if target.exists():
        raise OrderError("Копия %s уже есть: подождите минуту и нажмите снова." % target.name)
    source, copy = store.open_db(db_path), store.open_db(target)
    try:
        source.backup(copy)
        ok = copy.execute("PRAGMA quick_check").fetchone()[0]
    except BaseException:
        copy.close()
        target.unlink(missing_ok=True)                                      # no half-written copy is left behind
        raise
    finally:
        copy.close()
        source.close()
    os.chmod(str(target), 0o600)
    if ok != "ok":
        target.unlink()
        raise OrderError("Копия базы не прошла проверку: ничего не удалено.")
    return target


def run(conn, db_path, now_local: str, author: str, expected: str) -> dict:
    """Removes the cards the preview shows. `expected` is the `sign` of the list the person saw: when the list changed since, nothing is done."""
    planned = preview(conn, now_local)["remove"]
    if sign(planned) != expected:
        raise OrderError("Пока страница была открыта, список изменился (сейчас %d карточек). Проверьте его ещё раз." % len(planned))
    if not planned:
        raise OrderError("Убирать нечего.")
    copy = backup(db_path, datetime.fromisoformat(now_local).strftime("%Y%m%d-%H%M%S"))
    cutoff = cutoff_for(now_local)

    def still_empty(conn_, card):
        reason = keep_reason(conn_, card, cutoff)
        return "Карточку изменили: %s." % REASONS[reason] if reason else None

    removed, skipped, stopped = [], 0, ""
    for card in planned:
        try:
            trash.delete_order(conn, card["id"], now_local, author, only_if=still_empty)
            removed.append(card["id"])
        except OrderError:                                                    # changed or gone meanwhile: it stays as it is
            skipped += 1
        except sqlite3.Error as err:                                          # the base is busy too long: stop, what is removed stays removed
            stopped = str(err)
            break
    left = len(planned) - len(removed) - skipped
    trash._audit(conn, now_local, author, ACTION, {"removed": len(removed), "skipped": skipped, "left": left, "stopped": stopped,
                                                   "backup": copy.name, "first": removed[0] if removed else None, "last": removed[-1] if removed else None})
    conn.commit()
    return {"removed": len(removed), "skipped": skipped, "left": left, "backup": copy.name}
