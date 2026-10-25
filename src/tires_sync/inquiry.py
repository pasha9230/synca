"""The inquiry of a client and «Перезвонить» (the owner 2026-10-23, answers 8–12).

An inquiry is any request about buying tyres through a chat or a call of our Avito accounts (the accounts of the points are not ours). Its
status lives with the client (client_inquiry): «новое» when the client writes or calls (or a point hands them over), «в работе» when an
operator answers, talks, makes a deal or opens the chat, the call or the card, «заказал» whenever an order of the client is placed
(«Заказ оформлен» or later), «отказ» by itself after three days of silence (no message of the client, no answer of a person — Avito's
auto-answer is not one —, no call) with neither an order nor an open deal (the owner 2026-10-23: «3 дня тишины»). A client who writes
after «отказ» is «в работе» again; one who ordered and writes again after a pause (clients.NEW_INQUIRY_GAP_HOURS) with no order in progress
is a new inquiry. The reason of a refusal is not kept. Every change of a status is a record of the journal of actions (action «inquiry»).
The statuses are moved by `tick` (every minute with the chats) and by `touch` (a page opened in the panel). Nothing here writes to Avito,
to VK or to a client."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from . import clients, orders, store, users

STATUSES = {"new": "новое", "work": "в работе", "ordered": "заказал", "refused": "отказ"}
REFUSE_DAYS = 3
AUTHOR = "обращения"                                     # not a panel login: the changes made by the look of every minute
MESSAGE_MARK, CALL_MARK, HANDOFF_MARK = "inquiry_message_mark", "inquiry_call_mark", "inquiry_handoff_mark"
NOT_ORDERED = ("oth", orders.FROM_POINT[0])              # «На ОТХ» is a client who refused; «Передано с точки» is only the request
NOT_THE_CLIENT = ("system", "deleted")                   # Avito's own notes in a chat are not the client's words
RETRY_MARK = "inquiry_retry"                             # app_state: the events whose look failed, [what, key, failures], looked at again
RETRIES = 3                                              # at most so many times (the lead 2026-10-25), then a record of the journal of actions
HANDOFF_WINDOW = timedelta(minutes=10)                   # a handoff may reuse an older order (its number is no mark): read by time, remembered
NOTE_MAX = 300
DUE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")


log = logging.getLogger("tires_sync.inquiry")


class InquiryError(ValueError):
    pass


def gap_hours() -> int:
    """The pause after which a message of the client is a new inquiry: lane A's constant of the client card (24 h until it is there)."""
    return int(getattr(clients, "NEW_INQUIRY_GAP_HOURS", 24))


def _dt(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value)[:19]) if value else None
    except ValueError:
        return None


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


# ---- the status --------------------------------------------------------------------------------------------------------------------

FIELDS = ("client_id", "status", "new_at", "round_at", "last_in_at", "worked_at", "ordered_at", "refused_at", "updated_at", "active_at", "looked_at")
WRITTEN = FIELDS[:-1]                                    # looked_at is the look's own mark (_looked), never written over


def get(conn, client_id) -> Optional[dict]:
    row = conn.execute("SELECT %s FROM client_inquiry WHERE client_id = ?" % ", ".join(FIELDS), (client_id,)).fetchone()
    return dict(zip(FIELDS, row)) if row else None


def _put(conn, client_id: int, old: Optional[dict], now: str, why: str, author: str, **changes) -> bool:
    """Write the changes over the row as it was read (`old`): when the panel or another look changed it meanwhile, nothing is written and the
    next look starts from the new row. A new status is a record of the journal of actions. No commit unless the journal writes one."""
    new = dict(old or {f: "" for f in FIELDS}, **changes, client_id=client_id, updated_at=now)
    if old is None:
        cur = conn.execute("INSERT OR IGNORE INTO client_inquiry (%s) VALUES (%s)" % (", ".join(WRITTEN), ", ".join("?" * len(WRITTEN))),
                           [new[f] for f in WRITTEN])
    else:
        cur = conn.execute("UPDATE client_inquiry SET %s WHERE client_id = ? AND status = ? AND updated_at = ?" % ", ".join("%s = ?" % f for f in WRITTEN[1:]),
                           [new[f] for f in WRITTEN[1:]] + [client_id, old["status"], old["updated_at"]])
    if cur.rowcount != 1:
        return False
    if (old or {}).get("status") != new["status"]:
        store.write_audit(conn, now, author, "inquiry", {"client": client_id, "from": (old or {}).get("status", ""), "to": new["status"], "why": why})
    return True


def statuses(conn, client_ids) -> Dict[int, dict]:
    """{client: {"code", "name"}} of the clients that have an inquiry."""
    ids = sorted({int(i) for i in client_ids if i})
    found = {}
    for start in range(0, len(ids), 500):
        part = ids[start:start + 500]
        for client_id, status in conn.execute("SELECT client_id, status FROM client_inquiry WHERE client_id IN (%s)" % ",".join("?" * len(part)), part):
            if status in STATUSES:
                found[client_id] = {"code": status, "name": STATUSES[status]}
    return found


def of_chats(conn, chat_rows) -> Dict[int, dict]:
    """{chat: status} through the client of every chat."""
    rows = sorted({int(r) for r in chat_rows if r})
    links = {}
    for start in range(0, len(rows), 500):
        part = rows[start:start + 500]
        links.update(conn.execute("SELECT chat_row, client_id FROM client_chat WHERE chat_row IN (%s)" % ",".join("?" * len(part)), part).fetchall())
    found = statuses(conn, links.values())
    return {chat: found[client] for chat, client in links.items() if client in found}


def of_calls(conn, call_rows) -> Dict[int, dict]:
    rows = sorted({int(r) for r in call_rows if r})
    links = {}
    for start in range(0, len(rows), 500):
        part = rows[start:start + 500]
        links.update(conn.execute("SELECT call_row, client_id FROM client_call WHERE call_row IN (%s)" % ",".join("?" * len(part)), part).fetchall())
    found = statuses(conn, links.values())
    return {call: found[client] for call, client in links.items() if client in found}


def _orders(conn, client_id: int) -> List[dict]:
    return [{"id": r[0], "created_at": r[1], "entered_at": r[2], "code": r[3], "kind": r[4], "position": r[5], "taken_at": r[6]} for r in conn.execute(
        "SELECT c.id, c.created_at, c.entered_at, g.code, g.kind, g.position, s.taken_at FROM client_order l JOIN order_card c ON c.id = l.order_id "
        "JOIN order_stage g ON g.id = c.stage_id LEFT JOIN order_handoff_source s ON s.order_id = c.id WHERE l.client_id = ?", (client_id,))]


def _placed_position(conn) -> Optional[int]:
    row = conn.execute("SELECT position FROM order_stage WHERE code = 'placed'").fetchone()
    return row[0] if row else None


def _is_placed(order: dict, placed: Optional[int]) -> bool:
    """«Заказ оформлен» or a stage after it (a user stage too: the rule of orders._needs_placed), not lost or closed, not «На ОТХ»."""
    if order["code"] in NOT_ORDERED or order["kind"] in ("lost", "closed"):
        return False
    return order["kind"] == "won" or orders._needs_placed(order, {"position": placed} if placed is not None else None)


def _live(order: dict) -> bool:
    """An order still in progress (an open deal): not finished, not cancelled, not «На ОТХ»."""
    return order["kind"] == "open" and order["code"] != "oth"


def _ordered_now(order: dict, row: dict, placed: Optional[int]) -> bool:
    """The order makes the client «заказал» in this round: placed and in progress, or finished within the round (a purchase finished before
    the client came again is the past)."""
    return _is_placed(order, placed) and (_live(order) or (order["entered_at"] or "") >= (row["round_at"] or ""))


def _answered_after(conn, client_id: int, at: str) -> Optional[str]:
    """The first answer of a person (not Avito's notes) in the client's chats after `at`: a message that came late (the history read
    later) is answered already when such an answer is there."""
    row = conn.execute("SELECT MIN(m.at) FROM avito_message m JOIN client_chat l ON l.chat_row = m.chat_row WHERE l.client_id = ? "
                       "AND m.direction = 'out' AND m.kind NOT IN (%s) AND m.at > ?" % ",".join("?" * len(NOT_THE_CLIENT)),
                       (client_id,) + NOT_THE_CLIENT + (at,)).fetchone()
    return row[0] if row and row[0] else None


def on_inbound(conn, client_id: int, at: str, now: str, why: str = "клиент написал") -> None:
    row = get(conn, client_id)
    last = _dt(row["last_in_at"]) if row else None
    fresh = last is None or (_dt(at) or last) - last >= timedelta(hours=gap_hours())
    last_in = max(at, row["last_in_at"] if row else "")
    active = max(at, row["active_at"] if row else "")                                     # the three days of silence start again
    answered = _answered_after(conn, client_id, at)                                       # хвост З13: a late message answered already
    new = {"status": "work", "worked_at": answered} if answered else {"status": "new"}
    if row is None:
        _put(conn, client_id, None, now, why, AUTHOR, new_at=at, round_at=at, last_in_at=last_in, active_at=active, **new)
    elif row["status"] == "refused":                                                    # the owner: back to «в работе» after any time
        _put(conn, client_id, row, now, why, AUTHOR, status="work", round_at=at, last_in_at=last_in, active_at=active)
    elif row["status"] == "ordered":
        if fresh and not any(_live(o) for o in _orders(conn, client_id)):                # a repeat: everything bought, and a pause
            _put(conn, client_id, row, now, "повторное обращение: " + why, AUTHOR, new_at=at, round_at=at, last_in_at=last_in,
                 active_at=active, **new)
        else:
            _put(conn, client_id, row, now, why, AUTHOR, last_in_at=last_in, active_at=active)
    else:
        _put(conn, client_id, row, now, why, AUTHOR, last_in_at=last_in, active_at=active, **({"round_at": at} if fresh else {}))   # a new round
    on_active(conn, client_id, at, now)                                                   # kept even when the write above lost to the panel


def on_handoff(conn, client_id: int, at: str, now: str) -> None:
    """A point handed the client over («Передать под заказ»): our inquiry, «новое» at once (the lead 2026-10-23), then the usual rules."""
    row = get(conn, client_id)
    if row is None or row["status"] != "new" or row["round_at"] != at:
        _put(conn, client_id, row, now, "передан точкой", AUTHOR, status="new", new_at=at, round_at=at, active_at=max(at, row["active_at"] if row else ""))
    on_active(conn, client_id, at, now)


def on_worked(conn, client_id: int, at: str, now: str, how: str, author: str = AUTHOR) -> bool:
    """An operator answered, talked, made a deal or opened the chat / call / card: «новое» becomes «в работе». True when it changed."""
    row = get(conn, client_id)
    if row is None or row["status"] != "new" or at < (row["new_at"] or ""):
        return False
    return _put(conn, client_id, row, now, how, author, status="work", worked_at=at)


def on_active(conn, client_id: int, at: str, now: str) -> None:
    """Our answer by a person, or a talk: the three days of silence before «отказ» start again (the owner 2026-10-23: «3 дня тишины»)."""
    conn.execute("UPDATE client_inquiry SET active_at = ?, updated_at = ? WHERE client_id = ? AND active_at < ?", (at, now, client_id, at))


def touch(conn, client_id, now: str, who: str, how: str) -> bool:
    """A page opened by a person who works the orders. Never raises: an opened page must open."""
    try:
        changed = on_worked(conn, int(client_id), now, now, how, who)
        conn.commit()
        return changed
    except Exception:                                    # noqa: BLE001  (a busy base: the status waits for the next look)
        conn.rollback()
        return False


def look_orders(conn, client_id: int, now: str, placed: Optional[int] = None) -> None:
    """What the orders of the client say: «заказал», a cancelled order, a deal made by an operator, three days without anything."""
    row = get(conn, client_id)
    if row is None:
        return
    found = _orders(conn, client_id)
    ordered = [o for o in found if _ordered_now(o, row, placed)]
    if ordered and row["status"] != "ordered":
        _put(conn, client_id, row, now, "заказ №%d" % ordered[0]["id"], AUTHOR, status="ordered", ordered_at=now)
        return
    if row["status"] == "ordered":
        if not ordered and any(_live(o) for o in found):                                  # the order went back, a deal is still open
            _put(conn, client_id, row, now, "заказ не оформлен", AUTHOR, status="work")
        elif not ordered:
            _put(conn, client_id, row, now, "заказ отменён", AUTHOR, status="refused", refused_at=now)
        return
    if row["status"] == "new":
        deal = next((o for o in found if _live(o) and o["code"] != orders.FROM_POINT[0] and (o["created_at"] or "") >= row["new_at"]), None)
        taken = next((o for o in found if (o["taken_at"] or "") >= row["new_at"] and o["taken_at"]), None)
        if deal or taken:
            on_worked(conn, client_id, now, now, "сделка №%d" % deal["id"] if deal else "клиент с точки взят в работу")
            row = get(conn, client_id)
    if row["status"] in ("new", "work") and not any(_live(o) for o in found):
        start = _dt(max(row["round_at"], row["new_at"], row["active_at"]))              # the owner: three days of silence, no open deal
        if start is not None and start + timedelta(days=REFUSE_DAYS) <= _dt(now):
            _put(conn, client_id, row, now, "%d дня тишины без заказа" % REFUSE_DAYS, AUTHOR, status="refused", refused_at=now)


def _looked(conn, client_id: int, now: str) -> None:
    """The orders of the client were looked at: «заказал» / «отказ» are looked at again only when an order of theirs changes after it."""
    conn.execute("UPDATE client_inquiry SET looked_at = ? WHERE client_id = ?", (now, client_id))


def _final(conn, client_id: int) -> Optional[int]:
    """The client a merged one lives on in (lane A's merge keeps the gone client, marked «объединён в»)."""
    seen = set()
    while client_id not in seen:
        seen.add(client_id)
        row = conn.execute("SELECT merged_into FROM client WHERE id = ?", (client_id,)).fetchone()
        if row is None:
            return None
        if not row[0]:
            return client_id
        client_id = row[0]
    return None


def _fold_merged(conn, now: str) -> None:
    """The inquiry of a merged client goes to the client it was merged into; when both have one, the more recent stays."""
    for client_id, updated in conn.execute("SELECT i.client_id, i.updated_at FROM client_inquiry i JOIN client c ON c.id = i.client_id "
                                           "WHERE c.merged_into IS NOT NULL AND c.merged_into != 0").fetchall():
        target = _final(conn, client_id)
        if target is None or target == client_id:
            continue
        kept, moved = get(conn, target), get(conn, client_id)
        words = [r for r in (kept, moved) if r]
        if kept is None or (updated or "") > (kept["updated_at"] or ""):
            conn.execute("DELETE FROM client_inquiry WHERE client_id = ?", (target,))
            conn.execute("UPDATE client_inquiry SET client_id = ?, looked_at = '' WHERE client_id = ?", (target, client_id))   # looked at again
        else:
            conn.execute("DELETE FROM client_inquiry WHERE client_id = ?", (client_id,))
            conn.execute("UPDATE client_inquiry SET looked_at = '' WHERE client_id = ?", (target,))
        conn.execute("UPDATE client_inquiry SET active_at = ?, last_in_at = ? WHERE client_id = ?",              # the last word of either stays
                     (max(r["active_at"] for r in words), max(r["last_in_at"] for r in words), target))
        store.write_audit(conn, now, AUTHOR, "inquiry_merged", {"from": client_id, "into": target})


# ---- the look of every minute ------------------------------------------------------------------------------------------------------

def _mark(conn, key: str) -> int:
    try:
        return int(store.get_state(conn, key) or "0")
    except ValueError:
        return 0


def _baselines(conn) -> Dict[int, str]:
    return {int(k.rsplit("_", 1)[1]): v or "" for k, v in conn.execute("SELECT key, value FROM app_state WHERE key LIKE 'chats\\_baseline\\_%' ESCAPE '\\'")
            if k.rsplit("_", 1)[1].isdigit()}


def _chat_client(conn, chat_row: int, order_id, now: str, make: bool) -> Optional[int]:
    row = conn.execute("SELECT client_id FROM client_chat WHERE chat_row = ?", (chat_row,)).fetchone()
    if row:
        return _final(conn, row[0])
    if not make:
        return None
    made = clients.attach_chat(conn, chat_row, order_id if order_id and order_id > 0 else None, now, AUTHOR)   # the lead 2026-10-23: B links it
    return _final(conn, made) if made else None


def _call_client(conn, call_row: int, now: str) -> Optional[int]:
    row = conn.execute("SELECT client_id FROM client_call WHERE call_row = ?", (call_row,)).fetchone()
    if row is None:
        made = clients.attach_call(conn, call_row, now, AUTHOR)              # calls-sync links it; a look that came first links it itself
        return _final(conn, made) if made else None
    return _final(conn, row[0])


def _handed(conn) -> dict:
    """{"mark": the newest handoff looked at, "done": [[order, created_at], …] of the last HANDOFF_WINDOW before it}."""
    try:
        value = json.loads(store.get_state(conn, HANDOFF_MARK) or "{}")
    except ValueError:
        value = {}
    value = value if isinstance(value, dict) else {}
    return {"mark": str(value.get("mark") or ""), "done": [list(d) for d in value.get("done") or [] if isinstance(d, list) and len(d) == 2]}


def _retries(conn) -> List[list]:
    try:
        value = json.loads(store.get_state(conn, RETRY_MARK) or "[]")
    except ValueError:
        value = []
    return [v for v in value if isinstance(v, list) and len(v) == 3 and v[0] in ("message", "call", "handoff")] if isinstance(value, list) else []


def _again(conn, what: str, key) -> Optional[tuple]:
    """A failed event read again by its key: (at, what, data) as the look builds it, None when it is gone."""
    if what == "message":
        row = conn.execute("SELECT m.chat_row, m.at, m.direction, m.kind, c.order_id FROM avito_message m JOIN avito_chat c ON c.id = m.chat_row "
                           "WHERE m.id = ?", (key,)).fetchone()
        return (row[1], "message", (row[0], row[2], row[3], row[4])) if row else None
    if what == "call":
        row = conn.execute("SELECT id, call_at, answered FROM avito_call WHERE id = ?", (key,)).fetchone()
        return (str(row[1])[:19], "call", (row[0], row[2])) if row else None
    return (key[1], "handoff", (key[0],)) if isinstance(key, list) and len(key) == 2 else None


def tick(conn, now: str) -> dict:
    """The look of every minute (Moscow `now`): the new messages and calls since the last look (only of the last three days: the older ones
    are the past), the clients the points handed over, then the orders of every client with an inquiry. Returns {status: how many came to
    it}. Each client on its own: one broken client never stops the rest."""
    before = dict(conn.execute("SELECT client_id, status FROM client_inquiry").fetchall())
    border = _iso(_dt(now) - timedelta(days=REFUSE_DAYS))
    _fold_merged(conn, now)
    conn.commit()
    points = {r[0] for r in conn.execute("SELECT avito_account_id FROM point WHERE avito_account_id IS NOT NULL")}
    baselines = _baselines(conn)
    top_message = conn.execute("SELECT COALESCE(MAX(id), 0) FROM avito_message").fetchone()[0]
    top_call = conn.execute("SELECT COALESCE(MAX(id), 0) FROM avito_call").fetchone()[0]
    events = []
    for message_row, chat_row, at, direction, kind, account_id, order_id in conn.execute(
            "SELECT m.id, m.chat_row, m.at, m.direction, m.kind, c.account_id, c.order_id FROM avito_message m JOIN avito_chat c ON c.id = m.chat_row "
            "WHERE m.id > ? AND m.id <= ? AND m.at >= ?", (_mark(conn, MESSAGE_MARK), top_message, border)).fetchall():
        if account_id in points or at < baselines.get(account_id, "") or kind in NOT_THE_CLIENT:     # nor are they our answer
            continue
        events.append((at, "message", (chat_row, direction, kind, order_id), message_row))
    for call_row, at, answered, account_id, point_id in conn.execute(
            "SELECT id, call_at, answered, account_id, point_id FROM avito_call WHERE id > ? AND id <= ? AND call_at >= ?",
            (_mark(conn, CALL_MARK), top_call, border)).fetchall():
        if point_id or account_id in points:
            continue
        events.append((str(at)[:19], "call", (call_row, answered), call_row))
    handed = _handed(conn)
    since = max(border, _iso(_dt(handed["mark"]) - HANDOFF_WINDOW) if handed["mark"] else border)
    for order_id, at in conn.execute("SELECT order_id, created_at FROM order_handoff WHERE created_at >= ?", (since,)).fetchall():
        if [order_id, at] not in handed["done"]:
            events.append((at, "handoff", (order_id,), [order_id, at]))
            handed["done"].append([order_id, at])
            handed["mark"] = max(handed["mark"], at)
    failures = {}
    for what, key, count in _retries(conn):              # an event whose look failed before (хвост З13): below the marks, so only here
        found = _again(conn, what, key)
        if found is not None:
            events.append(found + (key,))
            failures[json.dumps([what, key])] = count
    events.sort(key=lambda e: e[0])
    again, given_up = [], []
    for at, what, data, key in events:
        try:
            if what == "message":
                chat_row, direction, kind, order_id = data
                client_id = _chat_client(conn, chat_row, order_id, now, direction == "in")
                if client_id is None:
                    continue
                if direction == "in":
                    on_inbound(conn, client_id, at, now, "клиент звонил" if kind == "call" else "клиент написал")
                else:
                    on_worked(conn, client_id, at, now, "ответ в чате")
                    on_active(conn, client_id, at, now)
            elif what == "call":
                call_row, answered = data
                client_id = _call_client(conn, call_row, now)
                if client_id is None:
                    continue
                on_inbound(conn, client_id, at, now, "звонок")
                if answered:
                    on_worked(conn, client_id, at, now, "разговор по звонку")
            else:
                found = conn.execute("SELECT client_id FROM client_order WHERE order_id = ?", data).fetchone()
                client_id = _final(conn, found[0]) if found else clients.attach_order(conn, data[0], now, AUTHOR)
                if client_id is None:
                    continue
                on_handoff(conn, client_id, at, now)
            conn.commit()
        except Exception as err:                         # noqa: BLE001
            conn.rollback()
            log.exception("the inquiry event %s %s could not be looked at", what, data)
            count = failures.get(json.dumps([what, key]), 0) + 1
            (again if count <= RETRIES else given_up).append([what, key, count, str(err)[:200] or err.__class__.__name__])
    store.set_state(conn, RETRY_MARK, json.dumps([item[:3] for item in again]))
    for what, key, count, why in given_up:              # the status of that client waits for its next event or a look at its page
        store.write_audit(conn, now, AUTHOR, "inquiry_failed", {"event": what, "id": key, "tries": count, "why": why})
    for key, top in ((MESSAGE_MARK, top_message), (CALL_MARK, top_call)):
        store.set_state(conn, key, str(top))
    keep = _iso(_dt(handed["mark"]) - HANDOFF_WINDOW) if handed["mark"] else ""
    store.set_state(conn, HANDOFF_MARK, json.dumps({"mark": handed["mark"], "done": [d for d in handed["done"] if d[1] >= keep]}))
    placed = _placed_position(conn)
    for (client_id,) in conn.execute(                    # «новое» / «в работе» always (time runs); the others when an order of theirs changed
            "SELECT i.client_id FROM client_inquiry i WHERE i.status IN ('new', 'work') OR EXISTS (SELECT 1 FROM client_order l "
            "JOIN order_card c ON c.id = l.order_id WHERE l.client_id = i.client_id AND (c.updated_at >= i.looked_at OR l.at >= i.looked_at)) "
            "OR EXISTS (SELECT 1 FROM order_oth_client o JOIN order_card c ON c.id = o.order_id WHERE o.client_id = i.client_id "    # unlinked on «На ОТХ»
            "AND (c.updated_at >= i.looked_at OR o.detached_at >= i.looked_at))").fetchall():
        try:
            look_orders(conn, client_id, now, placed)
            if (get(conn, client_id) or {}).get("status") in ("ordered", "refused"):      # «новое» / «в работе» are looked at every minute anyway
                _looked(conn, client_id, now)
            conn.commit()
        except Exception:                                # noqa: BLE001
            conn.rollback()
            log.exception("the orders of client %s could not be looked at", client_id)
    after = dict(conn.execute("SELECT client_id, status FROM client_inquiry").fetchall())
    made = {code: 0 for code in STATUSES}
    for client_id, status in after.items():
        if before.get(client_id) != status and status in made:
            made[status] += 1
    return made


# ---- «Перезвонить» -----------------------------------------------------------------------------------------------------------------

CALLBACK_FIELDS = ("id", "client_id", "order_id", "chat_row", "due_at", "assignee", "note", "created_at", "created_by", "done_at", "done_by",
                   "cancelled_at")


def _callback(row) -> dict:
    return dict(zip(CALLBACK_FIELDS, row))


def get_callback(conn, task_id) -> dict:
    row = conn.execute("SELECT %s FROM callback_task WHERE id = ?" % ", ".join(CALLBACK_FIELDS), (task_id,)).fetchone()
    if row is None:
        raise InquiryError("Такой задачи нет.")
    return _callback(row)


def add_callback(conn, due_at: str, note: str, now: str, author: str, client_id=None, order_id=None, chat_row=None) -> int:
    """«Перезвонить» at `due_at` (YYYY-MM-DDTHH:MM, Moscow) for the client of the order, of the chat or the client itself. It goes to the one
    who answers for the deal: the responsible of the order, else of the chat, else the author."""
    due = str(due_at or "").strip()[:16]
    if not DUE.match(due) or _dt(due) is None:
        raise InquiryError("Укажите дату и время звонка.")
    if due < now[:16]:
        raise InquiryError("Это время уже прошло.")
    from . import order_notify
    staff = callers(conn, order_notify.get_owner_login(conn))
    candidates = []                                          # the one who answers for the deal, if they work the orders in the panel
    if order_id:
        row = conn.execute("SELECT responsible, created_by FROM order_card WHERE id = ?", (order_id,)).fetchone()
        if row is None:
            raise InquiryError("Такого заказа нет.")
        candidates.append(row[0] or row[1] or "")             # as the card shows it: the maker until another manager is named
        found = conn.execute("SELECT client_id FROM client_order WHERE order_id = ?", (order_id,)).fetchone()
        client_id = client_id or (found[0] if found else None)
    if chat_row:
        row = conn.execute("SELECT responsible FROM avito_chat WHERE id = ?", (chat_row,)).fetchone()
        if row is None:
            raise InquiryError("Такого чата нет.")
        candidates.append(row[0] or "")
        found = conn.execute("SELECT client_id FROM client_chat WHERE chat_row = ?", (chat_row,)).fetchone()
        client_id = client_id or (found[0] if found else None)
    if client_id and conn.execute("SELECT 1 FROM client WHERE id = ?", (client_id,)).fetchone() is None:
        raise InquiryError("Такого клиента нет.")
    if not (client_id or order_id or chat_row):
        raise InquiryError("Не указано, кому перезвонить.")
    assignee = next((c for c in candidates if c in staff), author)
    task_id = conn.execute("INSERT INTO callback_task (client_id, order_id, chat_row, due_at, assignee, note, created_at, created_by) VALUES (?,?,?,?,?,?,?,?)",
                           (client_id or None, order_id or None, chat_row or None, due, assignee, " ".join(str(note or "").split())[:NOTE_MAX], now,
                            author)).lastrowid
    store.write_audit(conn, now, author, "callback", {"task": task_id, "client": client_id, "order": order_id, "chat": chat_row, "due": due,
                                                     "to": assignee})
    return task_id


def close_callback(conn, task_id, how: str, now: str, who: str) -> None:
    """«Сделано» (how = "done") or «Отменить» (how = "cancel"); a closed task stays closed."""
    if how not in ("done", "cancel"):
        raise InquiryError("Неизвестное действие.")
    get_callback(conn, task_id)
    column = "done_at" if how == "done" else "cancelled_at"
    cur = conn.execute("UPDATE callback_task SET %s = ?, done_by = ? WHERE id = ? AND done_at IS NULL AND cancelled_at IS NULL" % column,
                       (now, who, task_id))
    if cur.rowcount != 1:
        conn.rollback()
        raise InquiryError("Задача уже закрыта.")
    store.write_audit(conn, now, who, "callback_" + how, {"task": int(task_id)})


def open_callbacks(conn, client_id=None, order_id=None, chat_row=None) -> List[dict]:
    """The open «Перезвонить» of a client (with those of its orders and chats), of an order or of a chat, the nearest first."""
    if client_id:
        sql, args = ("(client_id = ? OR order_id IN (SELECT order_id FROM client_order WHERE client_id = ?) "
                     "OR chat_row IN (SELECT chat_row FROM client_chat WHERE client_id = ?))", [client_id] * 3)
    elif order_id:
        sql, args = "order_id = ?", [order_id]
    elif chat_row:
        sql, args = "chat_row = ?", [chat_row]
    else:
        return []
    return [_callback(r) for r in conn.execute("SELECT %s FROM callback_task WHERE done_at IS NULL AND cancelled_at IS NULL AND %s ORDER BY due_at, id"
                                               % (", ".join(CALLBACK_FIELDS), sql), args)]


def _who_called(conn, task: dict) -> tuple:
    """(name, phone) for the text of the reminder."""
    name, phone = "", ""
    if task["client_id"]:
        row = conn.execute("SELECT name FROM client WHERE id = ?", (task["client_id"],)).fetchone()
        name = row[0] if row else ""
        row = conn.execute("SELECT phone FROM client_phone WHERE client_id = ? ORDER BY phone LIMIT 1", (task["client_id"],)).fetchone()
        phone = row[0] if row else ""
    if task["order_id"] and not (name and phone):
        row = conn.execute("SELECT client_name, client_phone FROM order_card WHERE id = ?", (task["order_id"],)).fetchone()
        if row:
            name, phone = name or row[0], phone or row[1]
    if task["chat_row"] and not name:
        row = conn.execute("SELECT peer_name FROM avito_chat WHERE id = ?", (task["chat_row"],)).fetchone()
        name = row[0] if row else ""
    return name or "", phone or ""


def callers(conn, owner_login: str) -> set:
    """Who may get a «Перезвонить»: the active people of the panel who work the orders (not a point, not an observer), and the owner."""
    return {u["login"] for u in users.list_users(conn) if u["active"] and u["role"] not in ("point", "viewer")} | {owner_login}


def due_rows(conn, login: str, now: str, owner_login: str, panel_logins: set) -> List[dict]:
    """The rows of «Уведомления» for the «Перезвонить» that are due for this user (`panel_logins`: callers(); a task of somebody who left
    the panel or was switched off: the owner's). Red an hour after the time."""
    rows = []
    late = _iso(_dt(now) - timedelta(hours=1))
    for row in conn.execute("SELECT %s FROM callback_task WHERE done_at IS NULL AND cancelled_at IS NULL AND due_at <= ? ORDER BY due_at"
                            % ", ".join(CALLBACK_FIELDS), (now[:16],)):
        task = _callback(row)
        if not (task["assignee"] == login or (task["assignee"] not in panel_logins and login == owner_login)):
            continue
        name, phone = _who_called(conn, task)
        text = "Перезвонить%s%s" % (": " + clients.display(phone) if phone else "", " — " + task["note"] if task["note"] else "")
        href = ("/orders/%d" % task["order_id"] if task["order_id"] else "/chats/%d" % task["chat_row"] if task["chat_row"]
                else "/clients/%d" % task["client_id"])
        title = "Заказ №%d" % task["order_id"] if task["order_id"] else "Чат" if task["chat_row"] else "Клиент"
        rows.append({"id": "callback-%d" % task["id"], "order_id": None, "chat_row": None, "callback": task["id"], "client": name,
                     "at": task["due_at"] + ":00", "author": "", "kind": "callback", "level": "red" if task["due_at"] <= late[:16] else "yellow",
                     "more": 0, "text": text, "href": href, "title": title})
    return rows
