"""Avito chats of the accounts that work with messages: read them into the panel's database (reads only, nothing is sent)."""
from __future__ import annotations

import fcntl
import hashlib
import html
import io
import logging
import re
import sqlite3
import traceback
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from . import connections, reliability, store, trash
from .avito import AvitoError

MSK = timezone(timedelta(hours=3))
STATUSES = {"open": "открыта", "closed": "закрыта", "no_reply": "не требует ответа"}
RETENTION_DAYS = 180
REREAD_BUDGET = 20                                   # unread, unchanged chats read again per run (the newest first)
CHAT_FIELDS = ("id, account_id, chat_id, item_id, item_title, item_url, price_string, peer_id, peer_name, last_text, last_at, last_dir, unread, status, "
               "status_at, order_id, unlinked, history_ok, updated_ts, created_ts, synced_at, responsible")
log = logging.getLogger("tires_sync.chats")
MAX_REPLY = 2000
PAYWALL = "подписку с API мессенджера"                    # the stub Avito puts instead of the history of a chat the subscription does not cover


class ChatError(ValueError):
    pass


class ChatSendUnknown(ChatError):
    """It is not known whether the message left. The panel must not put the text back into the form: one press would send it a second time."""


class ChatSendRetry(ChatSendUnknown):
    """Avito refused the send with a 402 (24–25.10, a second account: the same text went a minute later): the minute's sync sends it again by
    itself (settle_sends). To every caller it is a ChatSendUnknown: nobody sends it by hand meanwhile."""


class ChatSendLost(ChatSendUnknown):
    """The sync looked into the chat and the send is not there («lost»): it goes again only by a press of «Отправить снова» (one place to
    send it from: a caller says «look at the chat», never «send it by hand» next to the button)."""


def iso(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts), tz=MSK).replace(tzinfo=None).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _chat_row(row) -> dict:
    return dict(zip([f.strip() for f in CHAT_FIELDS.split(",")], row))


SEARCH_MAX = 80


def find(conn, q: str, since: str = "", point: Optional[int] = None) -> set:
    """The chats a search finds: the client's name, the text of the messages, a phone (digits, in the order or in the messages) and the number
    of the order (the panel's or WebMIM's). `since`: the messages from that moment only (the text has no index: every message is read);
    `point`: an order finds its chat only when it is of that point (the manager of a point never learns another point's order through a
    chat). Returns the chat rows; the caller keeps the chats the person may see."""
    q = " ".join(str(q or "").split())[:SEARCH_MAX]
    folded, digits = q.casefold(), re.sub(r"\D", "", q)
    conn.create_function("fold", 1, lambda text: (text or "").casefold())
    conn.create_function("digits", 1, lambda text: re.sub(r"\D", "", text or ""))
    like = "%" + folded.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    recent, recent_args = ("at >= ? AND ", (since,)) if since else ("", ())     # the date first: the text of an older message is never folded
    own, own_args = (" AND o.point_id = ?", (point,)) if point is not None else ("", ())
    found = {r[0] for r in conn.execute("SELECT id FROM avito_chat WHERE fold(peer_name) LIKE ? ESCAPE '\\'", (like,))}
    found |= {r[0] for r in conn.execute("SELECT DISTINCT chat_row FROM avito_message WHERE " + recent + "fold(text) LIKE ? ESCAPE '\\'",
                                         recent_args + (like,))}
    if digits and digits == q.replace(" ", "").lstrip("№#"):                     # a number: the order (the panel's or WebMIM's)
        found |= {r[0] for r in conn.execute("SELECT c.id FROM avito_chat c JOIN order_card o ON o.id = c.order_id "
                                             "WHERE (o.id = ? OR digits(o.webmim_number) = ?)" + own, (int(digits[:9]), digits) + own_args)}
    if len(digits) >= 5:                                                         # a phone, written any way: in the order or in the messages
        tail = digits[-10:]
        found |= {r[0] for r in conn.execute("SELECT c.id FROM avito_chat c JOIN order_card o ON o.id = c.order_id "
                                             "WHERE digits(o.client_phone) LIKE ?" + own, ("%" + tail + "%",) + own_args)}
        found |= {r[0] for r in conn.execute("SELECT DISTINCT chat_row FROM avito_message WHERE " + recent + "digits(text) LIKE ?",
                                             recent_args + ("%" + tail + "%",))}
    return found


def list_chats(conn, account_id: Optional[int] = None) -> List[dict]:
    sql, args = "SELECT %s FROM avito_chat" % CHAT_FIELDS, ()
    if account_id is not None:
        sql, args = sql + " WHERE account_id = ?", (account_id,)
    return [_chat_row(r) for r in conn.execute(sql + " ORDER BY last_at DESC, id DESC", args)]


def get_chat(conn, chat_row) -> Optional[dict]:
    row = conn.execute("SELECT %s FROM avito_chat WHERE id = ?" % CHAT_FIELDS, (chat_row,)).fetchone()
    return _chat_row(row) if row else None


def list_messages(conn, chat_row) -> List[dict]:
    return [dict(zip(("id", "message_id", "at", "direction", "kind", "text", "is_read", "media"), r)) for r in conn.execute(
        "SELECT id, message_id, at, direction, kind, text, is_read, media FROM avito_message WHERE chat_row = ? ORDER BY ts, id", (chat_row,))]


def set_status(conn, chat_row, status: str, now: str) -> None:
    if status not in STATUSES:
        raise ValueError("Неизвестный статус беседы.")
    conn.execute("UPDATE avito_chat SET status = ?, status_at = ? WHERE id = ?", (status, now, chat_row))
    conn.commit()


def set_responsible(conn, chat_row, login: str, now: str, author: str) -> None:
    """Who answers the chat ('' = nobody): a panel login, written to the journal of actions."""
    login = str(login or "").strip()[:60]
    row = conn.execute("SELECT responsible FROM avito_chat WHERE id = ?", (chat_row,)).fetchone()
    if row is None:
        raise ValueError("Такого чата нет.")
    if row[0] == login:
        return
    conn.execute("UPDATE avito_chat SET responsible = ? WHERE id = ?", (login, chat_row))
    store.write_audit(conn, now, author, "chat_assign", {"chat": chat_row, "from": row[0], "to": login})
    conn.commit()


def wait_starts(conn, chat_rows) -> dict:
    """{chat: the first message of the client after the last answer of a person, or None when nobody waits} for the chats that have their
    messages here (a chat without them, or with only the stand-in of its preview, is left out: its preview is all there is). Avito's auto-answer is not an answer."""
    rows = sorted({int(r) for r in chat_rows if r})
    found = {}
    for start in range(0, len(rows), 500):
        part = rows[start:start + 500]
        for chat_row, first, has in conn.execute(
                "SELECT c.id, (SELECT at FROM avito_message WHERE chat_row = c.id AND direction = 'in' AND kind NOT IN ('system', 'deleted') "
                "AND ts > COALESCE((SELECT MAX(ts) FROM avito_message WHERE chat_row = c.id AND direction = 'out' AND kind NOT IN ('system', 'deleted')), 0) "
                "ORDER BY ts, id LIMIT 1), EXISTS (SELECT 1 FROM avito_message WHERE chat_row = c.id AND message_id != 'preview') FROM avito_chat c WHERE c.id IN (%s)"
                % ",".join("?" * len(part)), part):
            if has:
                found[chat_row] = first
    return found


def waiting_minutes(chat: dict, now) -> Optional[int]:
    """How long the client waits for an answer, the chat being open: since `wait_since` when the caller read it (wait_starts: the first
    message of the client after a person's answer), else since the last message when it is theirs; None when nobody waits.
    `now`: Moscow time, as the times of the messages."""
    if chat.get("status") != "open":
        return None
    if "wait_since" in chat:
        last = chat["wait_since"]
    else:
        last = chat.get("last_at") if chat.get("last_dir") == "in" else None
    if not last:
        return None
    try:
        since = datetime.fromisoformat(str(last)[:19])
    except ValueError:
        return None
    return max(0, int((now - since).total_seconds() // 60))


def is_paywall(message: dict) -> bool:
    return PAYWALL in str((message.get("content") or {}).get("text") or "")


UNSUPPORTED = "Сообщение не поддерживается"               # Avito's stub for a message its API does not show (a voice, a file, a button…)
_CHANNEL = re.compile(r"https://(?:www\.|m\.)?avito\.ru/profile/messenger/channel/[A-Za-z0-9_~-]+")


def unsupported_link(text, chat_id: str = "") -> Optional[str]:
    """For Avito's stub «Сообщение не поддерживается. … перейдите в Авито мессенджер <link>»: the link to the chat in Avito's messenger (only that
    link of avito.ru; else the one of this chat by its id, "" without it). None for any other text."""
    text = str(text or "")
    if not text.lstrip().startswith(UNSUPPORTED):
        return None
    found = _CHANNEL.search(text)
    if found:
        return found.group(0)
    chat_id = str(chat_id or "")
    return "https://www.avito.ru/profile/messenger/channel/%s" % chat_id if re.fullmatch(r"[A-Za-z0-9_~-]+", chat_id) else ""


def purge_paywall(conn) -> int:
    """Remove the stubs of Avito that an earlier version stored as messages."""
    cur = conn.execute("DELETE FROM avito_message WHERE text LIKE ?", ("%" + PAYWALL + "%",))
    conn.execute("UPDATE avito_chat SET last_text = '' WHERE last_text LIKE ?", ("%" + PAYWALL + "%",))
    conn.commit()
    return cur.rowcount


def _is_read(message: dict) -> int:
    return 1 if message.get("isRead") or int(message.get("read") or 0) > 0 else 0


PHOTO_URL = re.compile(r"^https://[a-z0-9.-]+\.(?:avito\.st|avito\.ru)/[^\s<>\"']*$", re.I)
CALL_STATUS = {"missed": "пропущен", "rejected": "отклонён", "finished": "состоялся", "answered": "состоялся"}


def photo_url(message: dict) -> str:
    """The biggest picture of a client's photo (Avito: content.image.sizes, «1280x960» → link), only a link to Avito's own storage."""
    sizes = ((message.get("content") or {}).get("image") or {}).get("sizes") or {}
    best, area = "", -1
    for key, url in sizes.items() if isinstance(sizes, dict) else ():
        found = re.match(r"^(\d+)x(\d+)$", str(key))
        size = int(found.group(1)) * int(found.group(2)) if found else 0
        if isinstance(url, str) and PHOTO_URL.match(url) and size > area:
            best, area = url, size
    return best


def message_text(message: dict) -> str:
    """What a message says: its text; for a call «Звонок через Авито» and how it went (a photo's link is kept apart: photo_url)."""
    kind = message.get("type") or "text"
    if kind == "call":
        status = str(((message.get("content") or {}).get("call") or {}).get("status") or "")
        return "Звонок через Авито" + (": %s" % CALL_STATUS[status] if status in CALL_STATUS else "")
    return (message.get("content") or {}).get("text") or ""


def _store_message(conn, chat_row: int, message: dict, oldest: str = "") -> int:
    """Insert a message; a message already there only gets its read mark refreshed (and a photo stored before without its link gets it).
    Returns 1 for a new message."""
    text = message_text(message)
    media = photo_url(message) if message.get("type") == "image" else ""
    at = iso(message.get("created"))
    if oldest and at and at < oldest:
        return 0                                                       # older than we keep: the cleaning would only remove it again
    mid = str(message.get("id"))
    cur = conn.execute("INSERT OR IGNORE INTO avito_message (chat_row, message_id, at, ts, direction, kind, text, is_read, media) VALUES (?,?,?,?,?,?,?,?,?)",
                       (chat_row, mid, at, int(message.get("created") or 0), message.get("direction") or "in", message.get("type") or "text", text, _is_read(message), media))
    if cur.rowcount == 0:
        conn.execute("UPDATE avito_message SET is_read = ? WHERE chat_row = ? AND message_id = ? AND is_read != ?", (_is_read(message), chat_row, mid, _is_read(message)))
        if media:
            conn.execute("UPDATE avito_message SET media = ? WHERE chat_row = ? AND message_id = ? AND media = ''", (media, chat_row, mid))
        if text and message.get("type") == "call":
            conn.execute("UPDATE avito_message SET text = ? WHERE chat_row = ? AND message_id = ? AND text = ''", (text, chat_row, mid))
    return cur.rowcount


# the moment of our last reply in a chat (SQL, the chat row put in place of %s): Avito's auto-answer (an outgoing «system» message, on since
# 2026-10-13) and a deleted message are no reply
OUR_LAST_REPLY = "COALESCE((SELECT MAX(ts) FROM avito_message WHERE chat_row = %s AND direction = 'out' AND kind NOT IN ('system', 'deleted')), 0)"


def _unread(conn, chat_row: int) -> int:
    """Messages of the client that wait for us: not read, and newer than our last reply (a chat we answered is not waiting). Avito's
    auto-answer is no reply: read together with the client's message it must not wipe the count (and with it «Клиент написал»)."""
    return conn.execute("SELECT COUNT(*) FROM avito_message WHERE chat_row = ? AND direction = 'in' AND kind = 'text' AND is_read = 0 "
                        "AND ts > " + OUR_LAST_REPLY % "?", (chat_row, chat_row)).fetchone()[0]


SAFE_URL = re.compile(r"^https://(www\.|m\.)?avito\.ru/[^\s<>\"']*$")


def _safe_url(url) -> str:
    """The ad's link only when it points to Avito: whatever else the data holds is never put into a link."""
    text = str(url or "")[:500]
    return text if SAFE_URL.match(text) else ""


def _upsert_chat(conn, account_id: int, item: dict, uid, now: str) -> Optional[tuple]:
    """The chat row of an item of Avito's list; returns (row id, existed, changed), or None for a chat deleted in the panel (it does not come back)."""
    if trash.is_deleted_chat(conn, account_id, str(item["id"])):
        return None
    context = (item.get("context") or {}).get("value") or {}
    peer = next((u for u in item.get("users") or [] if u.get("id") != uid), {})
    last = item.get("last_message") or {}
    fields = dict(item_id=str(context.get("id") or ""), item_title=str(context.get("title") or "")[:300], item_url=_safe_url(context.get("url")),
                  price_string=str(context.get("price_string") or "")[:60], peer_id=str(peer.get("id") or ""), peer_name=str(peer.get("name") or "")[:120],
                  last_text="" if is_paywall(last) else ("Фото" if last.get("type") == "image" else message_text(last))[:500], last_at=iso(last.get("created") or item.get("updated")),
                  last_dir=str(last.get("direction") or ""), created_ts=int(item.get("created") or 0), synced_at=now)
    row = conn.execute("SELECT id, updated_ts, unread, status, history_ok FROM avito_chat WHERE account_id = ? AND chat_id = ?", (account_id, str(item["id"]))).fetchone()
    updated = int(item.get("updated") or 0)
    if row is None:
        keys = sorted(fields)
        cur = conn.execute("INSERT INTO avito_chat (account_id, chat_id, updated_ts, %s) VALUES (?,?,0,%s)" % (", ".join(keys), ", ".join("?" for _ in keys)),
                           [account_id, str(item["id"])] + [fields[k] for k in keys])
        if trash.is_deleted_chat(conn, account_id, str(item["id"])):          # deleted in the panel between the check above and the insert
            conn.rollback()
            return None
        return cur.lastrowid, False, "changed"
    conn.execute("UPDATE avito_chat SET %s WHERE id = ?" % ", ".join("%s = ?" % k for k in fields), list(fields.values()) + [row[0]])
    # changed: read now. An open chat with unread messages and a readable history may be read again within the budget (Avito may not bump `updated` when it is read)
    return row[0], True, "changed" if row[1] != updated else ("again" if row[2] > 0 and row[3] == "open" and row[4] == 1 else "")


def sync_account(conn, client, account_id: int, now: str, page: int = 50, max_pages: int = 6, history: int = 100) -> dict:
    """Pull the account's chats and the messages of the chats that changed. One broken chat never stops the rest."""
    uid = connections.cabinet_user_id(conn, client, account_id)
    stats = {"chats": 0, "new_chats": 0, "messages": 0, "errors": 0}
    if store.get_state(conn, "chats_baseline_%d" % account_id) is None:       # the first sync of the account: what is here now is the past
        store.set_state(conn, "chats_baseline_%d" % account_id, now)
    oldest = (datetime.fromisoformat(now) - timedelta(days=RETENTION_DAYS)).isoformat(timespec="seconds")
    purge_paywall(conn)
    backfill_previews(conn, account_id)
    offset = 0
    rereads = 0
    for _ in range(max_pages):
        try:
            data = client.chats(uid, limit=page, offset=offset, retry=False)
        except AvitoError as err:
            if err.status != 429:
                raise
            stats["errors"] += 1                                                   # asked to slow down: the next run goes on
            return stats
        for item in data.get("chats") or []:
            upserted = _upsert_chat(conn, account_id, item, uid, now)
            if upserted is None:
                continue
            chat_row, existed, changed = upserted
            conn.commit()                                                          # no write stays open while we wait for Avito: the panel keeps working
            stats["chats"] += 1
            stats["new_chats"] += 0 if existed else 1
            if changed == "again":
                if rereads >= REREAD_BUDGET:
                    changed = ""
                else:
                    rereads += 1
            if changed:
                try:
                    messages = client.chat_messages(uid, str(item["id"]), limit=history, retry=False)
                    ok = 1
                except AvitoError as err:
                    if err.status != 402:
                        stats["errors"] += 1
                        log.warning("messages of chat %s failed: %s", chat_row, err.status)
                        conn.commit()
                        if err.status == 429:                                             # Avito asks to slow down: the rest waits for the next run
                            return stats
                        continue
                    messages, ok = [dict(item.get("last_message") or {})], 0          # no subscription for this chat: only its last message
                if ok and messages and all(is_paywall(m) for m in messages):        # Avito answered with its own stub instead of the history
                    messages, ok = [dict(item.get("last_message") or {})], 0
                messages = [m for m in messages if not is_paywall(m)]
                real = [m for m in messages if m.get("id")]
                if real:
                    conn.execute("DELETE FROM avito_message WHERE chat_row = ? AND message_id = 'preview'", (chat_row,))      # a real message takes the preview's place
                newest = max(real, key=lambda m: int(m.get("created") or 0)) if real else None
                added = [_store_message(conn, chat_row, m, "" if m is newest else oldest) for m in real]                       # the newest message is always kept
                new_in = any(r for r, m in zip(added, [m for m in messages if m.get("id")]) if r and m.get("direction") == "in")
                stats["messages"] += sum(added)
                waited = conn.execute("SELECT unread, status FROM avito_chat WHERE id = ?", (chat_row,)).fetchone()   # before this read (None: deleted meanwhile)
                conn.execute("UPDATE avito_chat SET unread = ?, history_ok = ?, updated_ts = ? WHERE id = ?",
                             (_unread(conn, chat_row), ok, int(item.get("updated") or 0), chat_row))
                if new_in and existed:
                    conn.execute("UPDATE avito_chat SET status = 'open', status_at = ? WHERE id = ? AND status != 'open'", (now, chat_row))
                    if waited is not None and (not waited[0] or waited[1] != "open"):     # nothing waited, or it was set aside: a new alert
                        _note_client_wrote(conn, chat_row, now)
                    for r, m in zip(added, real):
                        if r and m.get("direction") == "in" and m.get("type") == "call":
                            _note_client_called(conn, chat_row, message_text(m), now)
                if conn.execute("SELECT 1 FROM avito_chat WHERE id = ?", (chat_row,)).fetchone() is None:
                    conn.rollback()                                                   # deleted in the panel while its messages were read: keep nothing of it
                    continue
            conn.commit()
        if not (data.get("meta") or {}).get("has_more"):
            break
        offset += page
    return stats


CLIENT_WROTE_AUTHOR = "чаты Авито"         # as chat_deals.SYSTEM_AUTHOR: not a panel login, so it is an alert for everybody


def _note_client_wrote(conn, chat_row: int, now: str) -> None:
    """The alert «Клиент написал» in the journal of the chat's open order: one per chat until it is read or answered (written only when the
    chat had nothing waiting before this read, or was closed or marked «Не требует ответа»). A closed order has its own note «после закрытия» (chat_deals); a chat without an order has no journal."""
    from . import orders
    row = conn.execute("SELECT order_id, unread FROM avito_chat WHERE id = ?", (chat_row,)).fetchone()
    if not row or not row[0] or row[0] <= 0 or not row[1]:
        return
    try:
        card = orders.get_card(conn, row[0])
    except orders.OrderError:
        return
    if card["stage"]["kind"] != "open":
        return
    last = conn.execute("SELECT text FROM avito_message WHERE chat_row = ? AND direction = 'in' AND kind = 'text' ORDER BY ts DESC, id DESC LIMIT 1",
                        (chat_row,)).fetchone()
    text = " ".join((last[0] if last else "").split())
    orders.add_event(conn, card["id"], now, CLIENT_WROTE_AUTHOR, "chat", "Клиент написал: %s" % (text[:80] + ("…" if len(text) > 80 else "")), level="yellow")


def _note_client_called(conn, chat_row: int, text: str, now: str) -> None:
    """A call of the client through Avito: a yellow record «Клиент звонил» in the journal of the chat's open order, one per call."""
    from . import orders
    row = conn.execute("SELECT order_id FROM avito_chat WHERE id = ?", (chat_row,)).fetchone()
    if not row or not row[0] or row[0] <= 0:
        return
    try:
        card = orders.get_card(conn, row[0])
    except orders.OrderError:
        return
    if card["stage"]["kind"] == "open":
        orders.add_event(conn, card["id"], now, CLIENT_WROTE_AUTHOR, "chat", "Клиент звонил: %s" % text, level="yellow")


def store_pushed(conn, account_id: int, chat_id: str, message: dict, now: str) -> str:
    """One message Avito pushed (its webhook, avito_webhook) into a chat the panel already has, kept as the reading every minute keeps it:
    the message (a repeat is not stored twice, and the reading finds it there), the chat's preview and unread count; a client's message
    opens a chat set aside and gives the alerts «Клиент написал» / «Клиент звонил» by the rules of sync_account. A chat the panel does not
    have yet, or deleted in the panel, is left to the reading. Sends nothing. The caller commits.
    Returns "stored", "known" (the message is there), "no_chat", "deleted" or "skipped"."""
    if not message.get("id") or is_paywall(message):
        return "skipped"
    if trash.is_deleted_chat(conn, account_id, str(chat_id)):
        return "deleted"
    row = conn.execute("SELECT id, unread, status, last_at FROM avito_chat WHERE account_id = ? AND chat_id = ?", (account_id, str(chat_id))).fetchone()
    if row is None:
        return "no_chat"
    chat_row, waited_unread, waited_status, last_at = row
    if not _store_message(conn, chat_row, message):
        return "known"
    at = iso(message.get("created"))
    if at and at >= (last_at or ""):
        conn.execute("UPDATE avito_chat SET last_text = ?, last_at = ?, last_dir = ? WHERE id = ?",
                     (("Фото" if message.get("type") == "image" else message_text(message))[:500], at, str(message.get("direction") or ""), chat_row))
    conn.execute("UPDATE avito_chat SET unread = ? WHERE id = ?", (_unread(conn, chat_row), chat_row))
    if message.get("direction") == "in":
        conn.execute("UPDATE avito_chat SET status = 'open', status_at = ? WHERE id = ? AND status != 'open'", (now, chat_row))
        if not waited_unread or waited_status != "open":                     # nothing waited, or it was set aside: a new alert
            _note_client_wrote(conn, chat_row, now)
        if message.get("type") == "call":
            _note_client_called(conn, chat_row, message_text(message), now)
    return "stored"


def backfill_previews(conn, account_id: int) -> int:
    """A chat that shows a last message in the list but has no message of its own (the first version dropped what was older than the keeping time)
    gets that last message from its own card, without asking Avito. A real message replaces it when the chat is read next."""
    rows = conn.execute("SELECT id, last_text, last_at, last_dir FROM avito_chat c WHERE account_id = ? AND last_text != '' AND last_text NOT LIKE '%подписку с API мессенджера%' "
                        "AND NOT EXISTS (SELECT 1 FROM avito_message m WHERE m.chat_row = c.id)", (account_id,)).fetchall()
    for chat_row, text, at, direction in rows:
        conn.execute("INSERT OR IGNORE INTO avito_message (chat_row, message_id, at, ts, direction, kind, text, is_read) VALUES (?,?,?,0,?,'text',?,1)",
                     (chat_row, "preview", at, direction or "in", text))
    conn.commit()
    return len(rows)


def clean_old(conn, now: str, days: int = RETENTION_DAYS) -> int:
    cutoff = (datetime.fromisoformat(now) - timedelta(days=days)).isoformat(timespec="seconds")
    cur = conn.execute("DELETE FROM avito_message WHERE at != '' AND at < ? AND EXISTS "
                       "(SELECT 1 FROM avito_message n WHERE n.chat_row = avito_message.chat_row AND (n.at > avito_message.at OR (n.at = avito_message.at AND n.id > avito_message.id)))", (cutoff,))
    conn.commit()
    return cur.rowcount


def chats_of_order(conn, order_id) -> List[dict]:
    return [_chat_row(r) for r in conn.execute("SELECT %s FROM avito_chat WHERE order_id = ? ORDER BY last_at DESC" % CHAT_FIELDS, (order_id,))]


def unread_total(conn, without_points: bool = False) -> int:
    """Chats of enabled accounts that wait for an answer: unread and still open. `without_points`: not the chats of the points' accounts
    (the staff's counters, the owner 2026-10-26; order_points.account_chats)."""
    from .order_points import POINT_ACCOUNTS
    skip = " AND c.account_id NOT IN (" + POINT_ACCOUNTS + ")" if without_points else ""
    return conn.execute("SELECT COUNT(*) FROM avito_chat c JOIN account a ON a.id = c.account_id WHERE a.chats_enabled = 1 AND c.unread > 0 AND c.status = 'open'"
                        + skip).fetchone()[0]


CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _definitely_not_sent(err: AvitoError) -> bool:
    """Avito answered that it did not take the message (a 4xx); anything else (a 5xx, a timeout, a strange body) may have left the message."""
    return 400 <= int(err.status or 0) < 500


AVITO_SAID = 160                                                         # characters of Avito's text shown to a person
KEYLIKE = re.compile(r"(?i)\bbearer\s+\S+|[A-Za-z0-9_\-.=+/]{32,}")         # a token or a key in Avito's answer is never shown nor kept
REQUEST_ID = re.compile(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.I)   # Avito's request id: kept, the support asks for it
TAG = re.compile(r"<[^>]*>")                                                # a proxy's HTML page instead of Avito's JSON: its words only
ECHO = 12                                                                   # characters of our message that, found in Avito's answer, hide it
AGAIN = "Повторите через минуту; если повторяется — напишите владельцу."
NO_RIGHT = "Проверьте право приложения на отправку сообщений."
NO_CHAT = "Чат у Авито не найден (удалён или закрыт): повтор не поможет."
NOT_TAKEN = "Авито не принял запрос: повтор не поможет — напишите владельцу."


ESCAPED = re.compile(r'\\(u[dD][89abAB][0-9a-fA-F]{2}\\u[dD][c-fC-F][0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|["\\/bfnrt])')   # a JSON body as it came
SIMPLE = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


def _unescaped(code: str) -> str:
    """One JSON escape opened: «u0418», a surrogate pair «ud83d\\ude0a» (an emoji) as one character, «n», «"»…"""
    if code[0] not in "uU":
        return SIMPLE[code]
    if len(code) > 5:
        high, low = int(code[1:5], 16), int(code[-4:], 16)
        return chr(0x10000 + ((high - 0xD800) << 10) + (low - 0xDC00))
    return chr(int(code[1:], 16))


def _quotes(said: str, ours: str) -> bool:
    """Avito's answer holds a piece of our message (a check quoting the field): the text of a reply never goes to the journals. The answer is
    looked at as text: JSON escapes (\\uXXXX, \\n, \\") and HTML entities (&#1048;, &quot;) are opened first."""
    said = html.unescape(ESCAPED.sub(lambda code: _unescaped(code.group(1)), said))
    ours, said = " ".join(ours.lower().split()), " ".join(said.lower().split())
    if len(ours) < 6:
        return False
    size = min(ECHO, len(ours))
    return any(ours[i:i + size] in said for i in range(len(ours) - size + 1))


def _avito_said(err: AvitoError, ours: str = "") -> str:
    """Avito's answer for a person and the journals: «код 402: <its text>», or «код 402» when it said nothing. Its JSON text, else the start
    of the body as it came; tags dropped, spaces squeezed, cut to AVITO_SAID, anything like a key hidden (not a request id); an answer that
    quotes `ours` (the text sent) is hidden whole."""
    said = str(err.message or "")
    if said == "HTTP %s" % err.status:                                       # _error_from's stand-in: the body was not Avito's JSON
        said = str(getattr(err, "body", "") or "")
    if ours and _quotes(said, ours):
        return "код %s: текст ответа скрыт, в нём наше сообщение" % err.status
    said = " ".join(CONTROL.sub(" ", TAG.sub(" ", said)).split())
    said = KEYLIKE.sub(lambda run: run.group(0) if REQUEST_ID.fullmatch(run.group(0)) or run.group(0).startswith("//") else "…", said)   # «//»: a link
    if len(said) > AVITO_SAID:
        said = said[:AVITO_SAID - 1] + "…"
    return "код %s: %s" % (err.status, said) if said else "код %s" % err.status


def _refused_hint(err: AvitoError, upload: bool = False) -> str:
    """A 403 means the app has no right to write; a 404 (no such chat) and a 400 (a request Avito does not take) are not helped by a repeat
    (the lead 2026-10-25); any other refusal (a 402 of 24.10 went seconds later) is tried again, then told the owner. The upload of a photo
    names no chat: its 404 is a request Avito does not take."""
    return {403: NO_RIGHT, 404: NOT_TAKEN if upload else NO_CHAT, 400: NOT_TAKEN}.get(err.status, AGAIN)


def _send_record(chat_row, err: BaseException, ours: str = "") -> str:
    """What chat_send_log keeps of a failed try and the service log says: Avito's status, its own code when it differs, its text; for no
    answer (the network) only the kind of the failure (its text may hold anything); for a failure of our own its kind, and the log gets where it
    broke (the lines of the code, never the error's text). Never the text of the message (`ours`)."""
    if isinstance(err, AvitoError):
        own = KEYLIKE.sub("…", " ".join(CONTROL.sub(" ", str(err.code)).split()))[:20]
        code = " (код Авито %s)" % own if err.code not in (None, "", err.status) else ""
        record = "HTTP %s%s%s" % (err.status, code, _avito_said(err, ours)[len("код %s" % err.status):])
    elif isinstance(err, reliability.ServiceUnavailable):               # the breaker held the call: no request went, the pause is said as it is
        record = " ".join(CONTROL.sub(" ", str(err)).split())[:AVITO_SAID]
    elif isinstance(err, OSError):
        record = "нет ответа (%s)" % type(err).__name__
    else:                                                                # not the network: where it broke goes to the log (code lines, no values)
        record = "непредвиденная ошибка (%s)" % type(err).__name__
        log.warning("a send to chat %s did not go: %s\n%s", chat_row, record, "".join(traceback.format_tb(err.__traceback__)).rstrip())
        return record
    log.warning("a send to chat %s did not go: %s", chat_row, record)
    return record


def _not_delivered(conn, chat: dict, now: str, author: str, what: str, err: ChatError) -> ChatError:
    """A reply or a photo did not reach the client (or nobody knows whether it did): a red record in the order of the chat, if it has one.
    The text of the reply never goes there. Whatever happens to the record, the error itself is what the caller raises."""
    if chat["order_id"] and chat["order_id"] > 0:
        try:
            from . import orders
            verdict = "не подтверждено" if isinstance(err, ChatSendUnknown) and not isinstance(err, ChatSendRetry) else "не отправлено"
            orders.add_event(conn, chat["order_id"], now, author, "chat", "%s клиенту %s: %s" % (what, verdict, err))
            conn.commit()
        except Exception:
            conn.rollback()
            log.warning("a failed send to chat %s could not be noted in the journal of its order", chat["id"])
    return err


STALE_SENDING = 300                     # seconds: a send mark «sending» older than this was cut off (the panel was killed in the middle of a send)
CLOCK_SLACK = 120                       # seconds: Avito's stamp of a message may run behind our clock
RECHECK = 100                           # messages of the chat read at Avito before an interrupted send is sent again (Avito's page at most)


class _Recheck(Exception):
    """The chat could not be checked now (Avito asked to slow down, did not answer, the base is busy): try again in a minute."""


def sending_is_stale(at: str, now: str) -> bool:
    """A mark «sending» set at `at` is older than STALE_SENDING at `now` (an unreadable time counts as old)."""
    try:
        return (datetime.fromisoformat(now) - datetime.fromisoformat(at)).total_seconds() > STALE_SENDING
    except (TypeError, ValueError):
        return True


def _ours_since(conn, chat_row: int, since: int) -> List[tuple]:
    """(id, kind, text) of our messages in the chat as the panel keeps it, from `since` (unix) on."""
    return conn.execute("SELECT message_id, kind, text FROM avito_message WHERE chat_row = ? AND direction = 'out' AND ts >= ? ORDER BY ts",
                        (chat_row, since)).fetchall()


def _ours_at_avito(conn, client, chat: dict, since: int) -> List[tuple]:
    """(id, kind, text) of our messages in the chat as Avito has it now, from `since` on; nothing is kept (the reading stores the chat and
    alerts about the client's messages). Raises _Recheck for a passing trouble; ChatError when Avito cannot show the chat (refused, its
    stub) or the page read does not reach back to `since` in a way that holds whatever the order of Avito's answer. The cabinet's id is
    asked first: its answer without the id is a passing trouble too (хвост Q3), its refusal is not."""
    try:
        user_id = connections.cabinet_user_id(conn, client, chat["account_id"], retry=False)
    except AvitoError as err:
        if err.status == 429 or int(err.status or 0) >= 500 or (err.status == 200 and err.message == connections.NO_CABINET_ID):
            raise _Recheck() from None
        raise ChatError("chat not readable") from None
    except (OSError, TimeoutError):
        raise _Recheck() from None
    try:
        found = client.chat_messages(user_id, chat["chat_id"], limit=RECHECK, retry=False)
    except AvitoError as err:
        if err.status == 429 or int(err.status or 0) >= 500:
            raise _Recheck() from None
        raise ChatError("chat not readable") from None
    except (OSError, TimeoutError):
        raise _Recheck() from None
    messages = [m for m in found or [] if isinstance(m, dict) and m.get("id")]
    if any(is_paywall(m) for m in messages):
        raise ChatError("Avito's stub instead of the chat")
    if len(messages) >= RECHECK:                                                 # a full page: it must hold the start of that send and our newest
        stamps = [int(m.get("created") or 0) for m in messages]
        newest = conn.execute("SELECT MAX(ts) FROM avito_message WHERE chat_row = ?", (chat["id"],)).fetchone()[0] or 0
        if not (min(stamps) < since and max(stamps) >= newest):
            raise ChatError("the page does not cover the time of that send")
    return [(str(m["id"]), m.get("type") or "text", message_text(m)) for m in messages
            if m.get("direction") == "out" and int(m.get("created") or 0) >= since]


def _resume_interrupted(conn, client, chat: dict, token: str, now: str, author: str, what: str, text: Optional[str]) -> str:
    """A press on a mark that is still «sending». Fresh: "busy" (it is being sent). Older than STALE_SENDING the send was cut off; what
    became of it is looked up in the chat as the panel keeps it and as Avito has it now (the reading every minute may stand still):
    "sent" when Avito's answer was recorded or our message of the same text is there since the start of that send; "unknown" when Avito
    cannot show the chat, it has no history, or another message of ours (any photo, for a photo) came since then — it may have gone: never
    a second one; "recheck" when the check cannot be made now (the mark stays); "retaken" otherwise: the mark is ours again (one press of
    two) and the message is sent now. `text` None: a photo. The chat is looked at from the first start of the send (orig_at): a send again
    of the sync that was cut off moved `at`, and the first one may be shown late (tails5)."""
    row = conn.execute("SELECT at, message_id, orig_at FROM chat_send_log WHERE token = ? AND status = 'sending'", (token,)).fetchone()
    if row is None or not sending_is_stale(row[0], now):
        return "busy"
    at, known, first = row

    def settle(status: str, message_id=None) -> str:
        done = conn.execute("UPDATE chat_send_log SET status = ?, message_id = COALESCE(?, message_id) WHERE token = ? AND status = 'sending' AND at = ?",
                            (status, message_id, token, at))
        conn.commit()
        return status if done.rowcount == 1 else "busy"                         # another press resolved or took it meanwhile
    try:
        if known:
            return settle("sent")
        try:
            since = int(datetime.fromisoformat(first or at).replace(tzinfo=MSK).timestamp()) - CLOCK_SLACK
        except (TypeError, ValueError):
            since = 0
        try:
            ours = _ours_since(conn, chat["id"], since) + _ours_at_avito(conn, client, chat, since)
        except ChatError:
            return settle("unknown")
        same = [m for m, kind, body in ours if text is not None and kind == "text" and " ".join(body.split()) == " ".join(text.split())]
        if same:
            return settle("sent", same[0])
        history = conn.execute("SELECT history_ok FROM avito_chat WHERE id = ?", (chat["id"],)).fetchone()
        if ours or not history or not history[0]:
            return settle("unknown")
        retaken = conn.execute("UPDATE chat_send_log SET at = ? WHERE token = ? AND status = 'sending' AND at = ?", (now, token, at))
        conn.commit()
    except (_Recheck, sqlite3.Error):
        conn.rollback()
        return "recheck"
    if retaken.rowcount != 1:
        return "busy"                                                            # another press took it first
    if chat["order_id"] and chat["order_id"] > 0:
        try:
            from . import orders
            orders.add_event(conn, chat["order_id"], now, author, "chat", "%s клиенту: прошлая отправка прервалась (панель перезапускалась), "
                             "в чате его нет — отправляем снова" % what)
            conn.commit()
        except Exception:
            conn.rollback()
            log.warning("the resumed send to chat %s could not be noted in the journal of its order", chat["id"])
    return "retaken"


OWN_RECORD_TOKENS = ("reserve-",)                 # the order's journal has the green record of these sends already (order_reserve.tell_client):
                                                   # no second «Отправлен ответ клиенту» (хвост З18)


RETRY_AFTER = 60                  # seconds: a send Avito refused with a 402 is sent again by the minute's sync after this
RETRIES = 3                       # the sync's tries after the first 402 (the owner 2026-10-25); then «не отправлено» and an alert
RETRY_FOR = 30 * 60               # seconds after the first 402: a send not gone by then is not tried any more (no message hours late)
CHECK_FIRST = 60                  # seconds after an unknown send: the first look into the chat
CHECK_LAST = 300                  # and the last: nothing of it after CHECKS looks, the last one this late, is «не дошло»
CHECKS = 2
OLD_UNKNOWN = 48 * 3600           # an unknown send older than this is not looked up (the ones before 2026-10-25, without a text, are)
SETTLE_BUDGET = 10                # sends settled per account and run: each one is a read of Avito
SYSTEM = "система"
CHECKING = "Система сама проверит чат через 1–2 минуты: итог появится в чате и в журнале заказа. До итога не отправляйте его ещё раз."
UNREADABLE = "Авито не показывает историю этого чата"
OTHERS = "после этой отправки в чате есть другие наши сообщения"


def _later(now: str, seconds: int) -> str:
    return (datetime.fromisoformat(now) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _retry_late(first_at: str, now: str) -> bool:
    """A 402 refused first at `first_at` is not tried any more at `now` (RETRY_FOR; an unreadable time is late)."""
    try:
        return (datetime.fromisoformat(first_at) + timedelta(seconds=RETRY_FOR)).isoformat(timespec="seconds") < now
    except (TypeError, ValueError):
        return True


def _retry_said(tries: int, late: bool = False) -> str:
    if late:
        return "Авито отказал в отправке (код 402), а система не повторила за %d минут (чтение чатов не шло): проверьте чат и отправьте вручную." % (RETRY_FOR // 60)
    return "Авито отказал в отправке (код 402), система повторит сама через минуту (попытка %d из %d). Вручную не отправляйте." % (max(tries, 1), RETRIES)


def _lost_said(kept: bool) -> str:
    return "Прошлая отправка не дошла: в чате её нет. %s" % (
        "Отправьте её снова кнопкой «Отправить снова» над полем ответа в чате." if kept else "Напишите клиенту вручную из чата.")


def _unknown_said(conn, token: str) -> str:
    """What a press on a send whose outcome is unknown is told: the sync is checking the chat, or why it cannot."""
    row = conn.execute("SELECT verdict FROM chat_send_log WHERE token = ?", (token,)).fetchone()
    if row and row[0]:
        return "Прошлая отправка не подтверждена, проверить нельзя: %s. Проверьте чат в кабинете Авито; если её там нет — напишите клиенту вручную." % row[0]
    return "Прошлая отправка ещё не подтверждена: система проверяет чат, итог — через несколько минут. Не отправляйте её ещё раз."


def _mark(conn, token: str, status: str, error: Optional[str] = None, **more) -> None:
    """The send mark after Avito's answer. «sent» and «failed» drop the kept text (nothing is checked or sent again by itself); «sent» drops
    Avito's answer to an earlier try too (25.10 12:32:59: a «sent» kept the text of the 402 before it)."""
    fields = {"status": status}
    if error is not None:
        fields["error"] = error
    if status in ("sent", "failed"):
        fields.update(text="", next_at="")
    if status == "sent":
        fields["error"] = ""
    fields.update(more)
    conn.execute("UPDATE chat_send_log SET %s WHERE token = ?" % ", ".join("%s = ?" % name for name in fields), tuple(fields.values()) + (token,))
    conn.commit()


def _alert_not_sent(conn, chat: dict, token: str, now: str, why: str, text: str, ending: str = "Отправьте вручную.") -> None:
    """The minute's sync could not send it (nobody is on the page): a line of «Входящие» from «система», with the text to copy (as «можно
    забирать» that did not go). Whatever happens to the line, the send is what it is."""
    try:
        from .order_manager import COPY_MARK
        card = conn.execute("SELECT point_id, webmim_number FROM order_card WHERE id = ?", (chat["order_id"],)).fetchone() \
            if chat["order_id"] and chat["order_id"] > 0 else None
        where = "Заказ №%s" % (card[1] or chat["order_id"]) if card else "Чат Ч-%d" % chat["id"]
        conn.execute("INSERT OR IGNORE INTO manager_message (message_id, at, point_id, sender, text, status) VALUES (?,?,?,?,?,'new')",
                     ("send-%s-%s" % (token, re.sub(r"\D", "", now)), now, card[0] if card else None, SYSTEM,
                      "%s: клиенту не отправлено — %s %s" % (where, why, ending) + (COPY_MARK + text if text else "")))
        conn.commit()
    except Exception:
        conn.rollback()
        log.warning("the failed send %s to chat %s could not be put into «Входящие»", token, chat["id"])


def _deliver(conn, client, chat: dict, user_id, token: str, now: str, author: str, tries: int, text: str, photo: bool = False,
             by_sync: bool = False, note: str = "") -> None:
    """The send itself once its mark is «sending», for a press (tries 0) and for the minute's sync (tries: the 402s before it); `text` is
    Avito's id of the uploaded picture for a photo. Avito's answer becomes the mark: «sent»; a 402 «retry» (the sync sends it again after
    RETRY_AFTER, RETRIES times, then «failed»); another 4xx «failed»; anything else «unknown» (the sync reads the chat). The journal of the
    order gets the first refusal and the end, not every try; a failed send of the sync (`by_sync`: nobody is on the page) goes to «Входящие»
    too; `note` names a send of the sync in its green record. Raises what the person sees."""
    chat_row = chat["id"]
    what, ours = ("Фото", "") if photo else ("Сообщение", text)
    try:
        sent = client.send_image(user_id, chat["chat_id"], text) if photo else client.send_message(user_id, chat["chat_id"], text)
    except AvitoError as err:
        record = _send_record(chat_row, err, ours)
        if err.status == 402 and tries < RETRIES:
            _mark(conn, token, "retry", record, tries=tries + 1, next_at=_later(now, RETRY_AFTER), **({} if tries else {"first_at": now}))
            said = ChatSendRetry("Авито отказал (%s), повторю сам через минуту (попытка %d из %d)." % (_avito_said(err, ours), tries + 1, RETRIES))
            raise (said if tries else _not_delivered(conn, chat, now, author, what, said)) from None
        if _definitely_not_sent(err):
            _mark(conn, token, "failed", record)
            hint = ("Авито отказал %d раза подряд, система больше не повторяет: отправьте вручную позже; если повторяется — напишите владельцу."
                    % (tries + 1) if err.status == 402 else _refused_hint(err))
            said = ChatError("Авито не принял %s (%s). %s" % ("сообщение с фото" if photo else "сообщение", _avito_said(err, ours), hint))
            if by_sync:
                _alert_not_sent(conn, chat, token, now, str(said), "" if photo else text)
            raise _not_delivered(conn, chat, now, author, what, said) from None
        _mark(conn, token, "unknown", record, tries=0, next_at=_later(now, CHECK_FIRST))
        raise _not_delivered(conn, chat, now, author, what, ChatSendUnknown(
            "Неизвестно, ушло ли %s (ответ Авито: %s). %s" % ("фото" if photo else "сообщение", _avito_said(err, ours), CHECKING))) from None
    except Exception as err:                                                         # a timeout, a cut connection: the message may have left
        _mark(conn, token, "unknown", _send_record(chat_row, err, ours), tries=0, next_at=_later(now, CHECK_FIRST))
        raise _not_delivered(conn, chat, now, author, what, ChatSendUnknown(
            "Неизвестно, ушло ли %s (нет ответа от Авито). %s" % ("фото" if photo else "сообщение", CHECKING))) from None
    kind = "photo" if photo else "reply"
    try:
        _mark(conn, token, "sent")                                                   # out: whatever happens to our own records, it is not sent again
    except Exception:                                                                # a locked database must not turn a delivered message into an error
        conn.rollback()
        log.warning("the %s to chat %s went out but its send record could not be marked; the token stays `sending` and is not sent again", kind, chat_row)
    try:
        mid = sent.get("id") if isinstance(sent, dict) else None
        created = (sent.get("created") if isinstance(sent, dict) else None) or int(datetime.fromisoformat(now).replace(tzinfo=MSK).timestamp())
        shown = "📷 Фото отправлено" if photo else text
        if mid:                                                                      # without Avito's id the next sync brings the message itself
            _store_message(conn, chat_row, {"id": mid, "created": created, "direction": "out", "type": "text", "content": {"text": shown}, "isRead": False})
            conn.execute("UPDATE chat_send_log SET message_id = ? WHERE token = ?", (str(mid), token))
        conn.execute("UPDATE avito_chat SET last_text = ?, last_at = ?, last_dir = 'out' WHERE id = ?", (shown[:500], iso(created) or now, chat_row))
        conn.execute("UPDATE avito_chat SET unread = ? WHERE id = ?", (_unread(conn, chat_row), chat_row))
        conn.commit()
        if chat["order_id"] and chat["order_id"] > 0:
            from . import orders
            again = note or (" (с повтора: Авито до этого отказал, код 402)" if tries else "")
            if photo:
                orders.add_event(conn, chat["order_id"], now, author, "chat", "Отправлено фото клиенту" + again)
            elif again or not str(token or "").startswith(OWN_RECORD_TOKENS):     # a send of the sync: its caller wrote «не отправлено» only
                orders.add_event(conn, chat["order_id"], now, author, "chat", "Отправлен ответ клиенту%s: %s" % (again, text[:80] + ("…" if len(text) > 80 else "")))
            conn.commit()
    except Exception:
        conn.rollback()
        log.warning("the %s to chat %s went out but its record failed; the next sync will bring it", kind, chat_row)


def _lost_is_there(conn, client, chat: dict, token: str, at: str, text: str) -> bool:
    """Before «Отправить снова»: the chat looked at once more (the verdict «lost» may be old — the message came late, or the same text was
    written by hand); there — the mark is «sent», nothing goes. A chat that cannot be looked at now does not hold the press back."""
    try:
        same = _same_text(_ours_since(conn, chat["id"], _since(at)) + _ours_at_avito(conn, client, chat, _since(at)), text)
    except (_Recheck, ChatError, sqlite3.Error):
        conn.rollback()
        return False
    if not same:
        return False
    conn.execute("UPDATE chat_send_log SET status = 'sent', message_id = ?, text = '', error = '' WHERE token = ? AND status = 'lost'", (same[0], token))
    conn.commit()
    return True


def _failed_is_there(conn, client, chat: dict, token: str, first: str, at: str, text: Optional[str], now: str) -> bool:
    """A new press of a send that did not go («failed»: Avito refused it, or the sync stopped it) under its own mark — a second «приехал»,
    the task pressed again (tails6): the chat is looked at from the first start of that send, a person may have written it by hand meanwhile
    (from the text of «Входящие»). Our text message of the same text there: the mark is «sent», nothing goes (True). Another message of
    ours since (_others): nothing goes, the person looks (ChatSendUnknown: no text to copy, a red record in the order's journal). Either
    way the open lines of «Входящие» about this send — the caller's and the sync's alerts (`send-…`), with their text to copy — are answered
    with the outcome: nobody copies it once more (the review of tails6, 1–2). A chat that cannot be read now does not hold the press back
    (False): a refusal means Avito did not take the first one. A busy base after the read: nothing goes, «попробуйте через минуту».
    `text` None: a photo, only another message counts."""
    since = _since(first)
    try:
        ours = _ours_since(conn, chat["id"], since) + _ours_at_avito(conn, client, chat, since)
    except (_Recheck, ChatError, sqlite3.Error):
        conn.rollback()
        return False
    except Exception:      # noqa: BLE001                                     # any other trouble of the read: the press goes as before
        conn.rollback()
        log.exception("the chat %s could not be read before a send that did not go was pressed again", chat["id"])
        return False
    same = _same_text(ours, text) if text is not None else []
    try:
        if same:
            done = conn.execute("UPDATE chat_send_log SET status = 'sent', message_id = ?, text = '', error = '' WHERE token = ? AND status = 'failed' "
                                "AND at = ?", (same[0], token, at))
            conn.commit()
            if done.rowcount != 1:
                mark = conn.execute("SELECT status FROM chat_send_log WHERE token = ?", (token,)).fetchone()
                if mark and mark[0] == "sent":
                    return True                                                  # another press found it first
                raise ChatError("Это сообщение уже отправляется.")
        others = not same and _others(conn, chat["id"], token, first, ours)
    except sqlite3.Error:
        conn.rollback()
        raise ChatSendUnknown("Перед повторной отправкой не удалось проверить чат (база занята): ничего не отправлено, попробуйте через минуту.") from None
    if same:
        _note_order(conn, chat, now, "Сообщение клиенту уже есть в чате Авито: повторно не отправлено", "green")
        _inbox_close(conn, token, now, "Итог: сообщение уже есть в чате Авито (найдено при повторной отправке) — отправлять не нужно.", alerts=True)
        return True
    if others:
        what, how = ("Фото", "отправили") if text is None else ("Сообщение", "написали")
        said = "%s не отправлено повторно: %s — возможно, его уже %s вручную. Проверьте чат в кабинете Авито; если его там нет — " \
               "напишите клиенту вручную." % (what, OTHERS, how)
        _note_order(conn, chat, now, "%s клиенту не отправлено повторно: %s — возможно, его уже %s вручную. Проверьте чат в кабинете Авито; "
                    "если его там нет — напишите клиенту вручную." % (what, OTHERS, how), "red")
        _inbox_close(conn, token, now, "Итог: " + said[:1].lower() + said[1:], alerts=True)
        raise ChatSendUnknown(said)
    return False


def send_reply(conn, client, chat_row, text, token, now: str, author: str, force: bool = False, again: bool = False) -> Optional[str]:
    """Send a reply to the client of a chat from the panel.

    Once per token: a double press sends nothing twice. A 402 is sent again by the minute's sync (settle_sends). When the outcome is unknown
    (a timeout, a 5xx) the sync reads the chat: the message there is «sent», nothing of it «lost»; a lost one goes again only by a press of
    «Отправить снова» (`again`), never by itself nor by another press of its token; "found" when that press found it in the chat already
    (nothing went). The text never goes to the logs."""
    text = CONTROL.sub("", str(text or "").replace("\r", "")).strip()
    chat = get_chat(conn, chat_row)
    if chat is None:
        raise ChatError("Такого чата нет.")
    if not text or len(text) > MAX_REPLY:
        raise ChatError("Текст ответа: от 1 до %d знаков." % MAX_REPLY)
    allowed = conn.execute("SELECT chats_enabled, chats_send FROM account WHERE id = ?", (chat["account_id"],)).fetchone()
    if not allowed or not allowed[0]:
        raise ChatError("У аккаунта выключено чтение чатов: включите его в «Подключения → Авито → аккаунт».")
    if not force and not allowed[1]:
        raise ChatError("Отправка ответов выключена для этого аккаунта: включите её в «Подключения → Авито → аккаунт».")
    token = str(token or "").strip()[:64]
    if not token:
        raise ChatError("Нет метки отправки: обновите страницу.")
    claim = conn.execute("INSERT OR IGNORE INTO chat_send_log (token, chat_row, at, author, status, text, orig_at) VALUES (?,?,?,?, 'sending', ?, ?)",
                         (token, chat_row, now, author, text, now))
    conn.commit()
    if claim.rowcount != 1:
        row = conn.execute("SELECT status, chat_row, tries, text, at, first_at, orig_at FROM chat_send_log WHERE token = ?", (token,)).fetchone()
        if row[1] != chat_row:
            raise ChatError("Метка отправки относится к другому чату: обновите страницу.")
        if row[0] == "sent":
            return                                                                   # the same press delivered twice: it is already out
        if row[0] == "retry":
            raise ChatSendRetry(_retry_said(row[2], _retry_late(row[5] or row[4], now)))
        if row[0] == "lost" and not again:
            raise ChatSendLost(_lost_said(bool(row[3])))
        if row[0] == "lost" and _lost_is_there(conn, client, chat, token, row[6] or row[4], text):    # from the first start (M1)
            return "found"                                                           # «Отправить снова» of a message that came meanwhile
        resumed = _resume_interrupted(conn, client, chat, token, now, author, "Сообщение", text) if row[0] == "sending" else ""
        if resumed == "sent":
            conn.execute("UPDATE chat_send_log SET text = '' WHERE token = ?", (token,))
            conn.commit()
            return                                                                   # a send cut off by a killed panel had gone out
        if resumed == "busy":
            raise ChatError("Это сообщение уже отправляется.")
        if resumed == "recheck":
            raise ChatError("Прошлая отправка прервалась, а проверить у Авито, ушло ли сообщение, сейчас не удалось: попробуйте через минуту.")
        if row[0] == "unknown" or resumed == "unknown":
            raise ChatSendUnknown(_unknown_said(conn, token))
        if row[0] == "failed" and _failed_is_there(conn, client, chat, token, row[6] or row[4], row[4], text, now):    # tails6: no copy
            return "found"                                                           # written by hand meanwhile: nothing went
        if resumed != "retaken":
            retaken = conn.execute("UPDATE chat_send_log SET status = 'sending', at = ?, orig_at = ?, again_at = CASE WHEN ? AND again_at = '' "
                                   "THEN ? ELSE again_at END WHERE token = ? AND status = ?",     # a new press: a new start; after «Отправить снова»
                                   (now, now, 1 if again else 0, now, token, "lost" if again else "failed"))    # the sync never sends it by itself
            conn.commit()
            if retaken.rowcount != 1:
                raise ChatError("Это сообщение уже отправляется.")
        conn.execute("UPDATE chat_send_log SET text = ?, photo = 0, tries = 0, next_at = '', verdict = '' WHERE token = ?", (text, token))
        conn.commit()
    try:
        user_id = connections.cabinet_user_id(conn, client, chat["account_id"], retry=False)
    except Exception as err:                                                         # asked before the send: nothing reached the client
        _mark(conn, token, "failed", _send_record(chat_row, err, text))
        why = (_avito_said(err, text) if isinstance(err, AvitoError) else str(err) if isinstance(err, reliability.ServiceUnavailable)
               else "нет ответа" if isinstance(err, OSError) else "непредвиденная ошибка %s" % type(err).__name__)
        raise _not_delivered(conn, chat, now, author, "Сообщение", ChatError(
            "Сообщение не отправлено: Авито не отдал данные кабинета (%s). Повторите позже." % why)) from None
    _deliver(conn, client, chat, user_id, token, now, author, 0, text)


MAX_IMAGE_BYTES = 15 * 1024 * 1024        # what we accept from the manager
MAX_IMAGE_SIDE = 1600                     # what goes to the client: the long side, never enlarged
MAX_IMAGE_PIXELS = 60_000_000             # a picture of more pixels is refused before it is decoded (a bomb); checked by us, the global of Pillow is not touched


def prepare_image(data: bytes) -> bytes:
    """A picture from the manager as a plain JPEG for the client: the long side at most 1600 px, no camera data (the place, the device) and no
    transparency. Whatever is not a picture of a usual kind, or is too big in pixels, is refused."""
    from PIL import Image, ImageOps
    if not data:
        raise ChatError("Файл пустой.")
    if len(data) > MAX_IMAGE_BYTES:
        raise ChatError("Файл слишком большой: не больше %d МБ." % (MAX_IMAGE_BYTES // 1024 // 1024))
    try:
        image = Image.open(io.BytesIO(data))
        if image.format not in ("JPEG", "PNG", "WEBP", "GIF", "BMP", "MPO"):
            raise ChatError("Нужна фотография: JPEG, PNG, WEBP, GIF или BMP.")
        if image.width * image.height > MAX_IMAGE_PIXELS:                        # our own limit, checked before the picture is decoded
            raise ChatError("Фото слишком большое по числу точек (не больше %d Мп)." % (MAX_IMAGE_PIXELS // 1_000_000))
        if image.format == "JPEG":
            image.draft("RGB", (2 * MAX_IMAGE_SIDE, 2 * MAX_IMAGE_SIDE))         # decodes a big JPEG at a smaller scale: far less memory
        image.load()
        profile = image.info.get("icc_profile")
        image = ImageOps.exif_transpose(image)
        if image.mode in ("RGBA", "LA", "PA", "P") or "transparency" in image.info:
            rgba = image.convert("RGBA")
            image = Image.new("RGB", rgba.size, (255, 255, 255))
            image.paste(rgba, mask=rgba.split()[-1])
        else:
            image = image.convert("RGB")
        image.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
        out = io.BytesIO()
        rgb_profile = bool(profile) and bytes(profile[16:20]) == b"RGB "                # a CMYK or grey profile does not fit the RGB data we write
        image.save(out, "JPEG", quality=85, optimize=True, **({"icc_profile": profile} if rgb_profile else {}))      # a new file: no metadata but the colours
        return out.getvalue()
    except ChatError:
        raise
    except MemoryError:
        raise ChatError("Фото слишком большое: не хватает памяти.") from None
    except Exception:                                                              # a broken file of any kind: a plain refusal, never a server error
        raise ChatError("Файл не открывается как фотография.") from None


def send_image(conn, client, chat_row, data: bytes, token, now: str, author: str, force: bool = False) -> None:
    """Send a photo to the client of a chat: once per token, like a reply. A refusal at the upload means nothing was sent (the token may be tried
    again); a 402 of the sending itself is sent again by the minute's sync (the picture uploaded already); an unsure outcome is checked by it,
    never sent again by itself."""
    chat = get_chat(conn, chat_row)
    if chat is None:
        raise ChatError("Такого чата нет.")
    allowed = conn.execute("SELECT chats_enabled, chats_send FROM account WHERE id = ?", (chat["account_id"],)).fetchone()
    if not allowed or not allowed[0]:
        raise ChatError("У аккаунта выключено чтение чатов: включите его в «Подключения → Авито → аккаунт».")
    if not force and not allowed[1]:
        raise ChatError("Отправка ответов выключена для этого аккаунта: включите её в «Подключения → Авито → аккаунт».")
    token = str(token or "").strip()[:64]
    if not token:
        raise ChatError("Нет метки отправки: обновите страницу.")
    picture = prepare_image(data)
    claim = conn.execute("INSERT OR IGNORE INTO chat_send_log (token, chat_row, at, author, status, photo, orig_at) VALUES (?,?,?,?, 'sending', 1, ?)",
                         (token, chat_row, now, author, now))
    conn.commit()
    if claim.rowcount != 1:
        row = conn.execute("SELECT status, chat_row, tries, at, first_at, orig_at FROM chat_send_log WHERE token = ?", (token,)).fetchone()
        if row[1] != chat_row:
            raise ChatError("Метка отправки относится к другому чату: обновите страницу.")
        if row[0] == "sent":
            return
        if row[0] == "retry":
            raise ChatSendRetry(_retry_said(row[2], _retry_late(row[4] or row[3], now)))
        if row[0] == "lost":
            raise ChatSendLost(_lost_said(False))
        resumed = _resume_interrupted(conn, client, chat, token, now, author, "Фото", None) if row[0] == "sending" else ""
        if resumed == "sent":
            return
        if resumed == "busy":
            raise ChatError("Это фото уже отправляется.")
        if resumed == "recheck":
            raise ChatError("Прошлая отправка прервалась, а проверить у Авито, ушло ли фото, сейчас не удалось: попробуйте через минуту.")
        if row[0] == "unknown" or resumed == "unknown":
            raise ChatSendUnknown(_unknown_said(conn, token))
        if row[0] == "failed":
            _failed_is_there(conn, client, chat, token, row[5] or row[3], row[3], None, now)    # tails6: another message of ours stops it
        if resumed != "retaken":
            retaken = conn.execute("UPDATE chat_send_log SET status = 'sending', at = ?, orig_at = ? WHERE token = ? AND status = 'failed'", (now, now, token))
            conn.commit()
            if retaken.rowcount != 1:
                raise ChatError("Это фото уже отправляется.")
        conn.execute("UPDATE chat_send_log SET text = '', photo = 1, tries = 0, next_at = '', verdict = '' WHERE token = ?", (token,))
        conn.commit()
    try:
        user_id = connections.cabinet_user_id(conn, client, chat["account_id"], retry=False)
        image_id = client.upload_image(user_id, "photo.jpg", picture)
    except AvitoError as err:
        _mark(conn, token, "failed", _send_record(chat_row, err))                    # the upload is not a message: nothing reached the client
        raise _not_delivered(conn, chat, now, author, "Фото", ChatError("Авито не принял фото (%s). %s" % (_avito_said(err), _refused_hint(err, upload=True)))) from None
    except Exception as err:
        _mark(conn, token, "failed", _send_record(chat_row, err))
        raise _not_delivered(conn, chat, now, author, "Фото", ChatError(
            "Фото не загружено в Авито (нет ответа). Сообщение клиенту не уходило, можно повторить.")) from None
    conn.execute("UPDATE chat_send_log SET text = ? WHERE token = ?", (str(image_id), token))       # what the sync sends again after a 402
    conn.commit()
    _deliver(conn, client, chat, user_id, token, now, author, 0, str(image_id), photo=True)


@contextmanager
def sync_lock(path):
    """Only one chat sync runs at a time (the minute timer and the page's own refresh): yields True with the lock, False when somebody holds it."""
    handle = None
    try:
        handle = open(str(path), "a")
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        held = True
    except OSError:
        held = False
    try:
        yield held
    finally:
        if handle is not None:
            if held:
                try:
                    fcntl.flock(handle, fcntl.LOCK_UN)
                except OSError:
                    pass
            handle.close()


def signature(conn, chat_row=None) -> str:
    """A short mark of what the chats page shows: it changes when a message or a chat arrives, a chat is read or answered."""
    total, last, unread = conn.execute("SELECT COUNT(*), COALESCE(MAX(last_at), ''), COALESCE(SUM(unread), 0) FROM avito_chat").fetchone()
    mine = conn.execute("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM avito_message WHERE chat_row = ?", (chat_row or 0,)).fetchone()
    return hashlib.sha1(("%s|%s|%s|%s|%s" % (total, last, unread, mine[0], mine[1])).encode()).hexdigest()[:12]


def retry_history(conn, client, account_id: int, now: str, limit: int = 10) -> dict:
    """Ask again for the history of chats that Avito did not give before (the access may be opened later): the ones tried longest ago first,
    a few per run. A chat whose history comes is healed: its messages are stored and it stops being marked as unavailable."""
    rows = conn.execute("SELECT id, chat_id FROM avito_chat WHERE account_id = ? AND history_ok = 0 ORDER BY history_tried_at, id LIMIT ?", (account_id, limit)).fetchall()
    stats = {"tried": 0, "opened": 0}
    if not rows:
        return stats
    uid = connections.cabinet_user_id(conn, client, account_id)
    oldest = (datetime.fromisoformat(now) - timedelta(days=RETENTION_DAYS)).isoformat(timespec="seconds")
    for chat_row, chat_id in rows:
        stats["tried"] += 1
        conn.execute("UPDATE avito_chat SET history_tried_at = ? WHERE id = ?", (now, chat_row))
        conn.commit()
        try:
            messages = client.chat_messages(uid, chat_id, limit=100, retry=False)
        except AvitoError as err:
            if err.status == 429:
                break                                                          # asked to slow down: the rest next time
            continue
        real = [m for m in messages if m.get("id") and not is_paywall(m)]
        if not real:
            continue
        conn.execute("DELETE FROM avito_message WHERE chat_row = ? AND message_id = 'preview'", (chat_row,))
        newest = max(real, key=lambda m: int(m.get("created") or 0))
        for m in real:
            _store_message(conn, chat_row, m, "" if m is newest else oldest)
        conn.execute("UPDATE avito_chat SET history_ok = 1, unread = ? WHERE id = ?", (_unread(conn, chat_row), chat_row))
        if conn.execute("SELECT 1 FROM avito_chat WHERE id = ?", (chat_row,)).fetchone() is None:
            conn.rollback()                                                    # deleted in the panel meanwhile
            continue
        conn.commit()
        stats["opened"] += 1
    return stats


def _since(at: str) -> int:
    """The unix time from which the chat is looked at for a send made at `at` (Avito's stamp may run behind our clock)."""
    try:
        return int(datetime.fromisoformat(at).replace(tzinfo=MSK).timestamp()) - CLOCK_SLACK
    except (TypeError, ValueError):
        return 0


def _same_text(ours: List[tuple], text: str) -> List[str]:
    """The ids of our text messages that are `text` (spaces squeezed), as _resume_interrupted finds them."""
    return [m for m, kind, body in ours if kind == "text" and " ".join(body.split()) == " ".join(text.split())]


def _note_order(conn, chat: dict, now: str, text: str, level: str) -> None:
    """A record in the journal of the chat's order, if it has one; the text of the message never goes there."""
    if chat["order_id"] and chat["order_id"] > 0:
        try:
            from . import orders
            orders.add_event(conn, chat["order_id"], now, SYSTEM, "chat", text, level=level)
            conn.commit()
        except Exception:
            conn.rollback()
            log.warning("the outcome of a send to chat %s could not be noted in the journal of its order", chat["id"])


def _others(conn, chat_row: int, token: str, before: str, ours: List[tuple]) -> bool:
    """Something else of ours may stand for this send (Avito changed its text, a person wrote it again by hand): a message of ours in the
    chat that no send of our records is (from Avito's app; Avito's system lines aside), or another send of this chat started after
    `before` (the first refusal or the send itself) that may have reached the client — whether it is there yet or still waits on its own
    402. Our sends started before it (a photo of the same press, a reply a minute earlier) do not count, by their first start (orig_at,
    which the sync's send again does not move; a row of before it without orig_at: its send again does not count): two refusals of the
    same moment do not stop each other, the later of two does."""
    logged = {r[0] for r in conn.execute("SELECT message_id FROM chat_send_log WHERE chat_row = ? AND token != ? AND message_id != ''", (chat_row, token))}
    if any(m[0] not in logged and m[1] not in ("system", "deleted") for m in ours):
        return True
    return conn.execute("SELECT 1 FROM chat_send_log WHERE chat_row = ? AND token != ? AND status IN ('sending', 'retry', 'unknown', 'sent') "
                        "AND (orig_at != '' OR again_at = '') AND COALESCE(NULLIF(orig_at, ''), NULLIF(first_at, ''), at) > ? LIMIT 1",
                        (chat_row, token, before)).fetchone() is not None


def _what(text: str, photo: bool) -> str:
    """The name of a send in the journal: a send of before 2026-10-25 kept neither its text nor whether it was a photo."""
    return "Фото" if photo else "Сообщение" if text else "Сообщение (или фото)"


def _claim_by_sync(conn, token: str, status: str, at: str, now: str, **more) -> bool:
    """The sync takes a send to send it (from `status` set at `at`): «sending» since `now`, with a `next_at` (a send the sync took and that
    stays «sending» is its own to settle later, settle_sends); False when a press or another run moved it first."""
    fields = dict(status="sending", at=now, next_at=now, **more)
    done = conn.execute("UPDATE chat_send_log SET %s WHERE token = ? AND status = ? AND at = ?%s" % (
        ", ".join("%s = ?" % k for k in fields), " AND again_at = ''" if "again_at" in more else ""), tuple(fields.values()) + (token, status, at))
    conn.commit()
    return done.rowcount == 1


AUTO_TOKENS = ("task-", "reserve-", "arrived-")    # the order's own messages (a task, «срок резерва», «можно забирать»)


def _stopped(conn, chat: dict, token: str) -> str:
    """Why the sync must not send this one again now (the review of send-retry, L4), '' when it may: the task of a `task-…` mark was
    skipped, ticked off («отправлено вручную») or is gone; the order of one of the order's own messages is closed (a stage of kind «отказ»
    or «завершающий»: «Отмена», «Отказ/невыкуп», «Возврат»; «Выполнено» is not) or deleted. A reply written in the chat is never stopped by
    the stage of the order: it may be about the cancellation itself; nor is a task made by the stage the order is in still (the same stay)."""
    token = str(token or "")
    if not token.startswith(AUTO_TOKENS):
        return ""
    card_id = chat["order_id"] if chat["order_id"] and chat["order_id"] > 0 else None
    stay = None
    if token.startswith("task-"):
        from . import order_engine
        task = order_engine.task_of_token(conn, token)
        if task is None:
            return "задача удалена"
        if task["status"] in ("skipped", "done"):
            return "задача пропущена" if task["status"] == "skipped" else "задача уже закрыта"
        card_id, stay = task["order_id"], (task["stage_id"], task["entered_at"])
    else:
        own = re.match(r"(?:reserve|arrived)-(\d+)-", token)
        card_id = int(own.group(1)) if own else card_id
    if card_id is None:
        return ""
    stage = conn.execute("SELECT s.kind, s.name, c.stage_id, c.entered_at FROM order_card c LEFT JOIN order_stage s ON s.id = c.stage_id WHERE c.id = ?",
                         (card_id,)).fetchone()
    if stage is None:
        return "заказ удалён"
    if stay is not None and (stage[2], stage[3]) == stay:
        return ""                                     # the task of that very stage («Клиенту: заказ отменён» on «Отмена»): it is what the person sent
    return "заказ в «%s»" % stage[1] if stage[0] in ("lost", "closed") else ""


def _halt(conn, chat: dict, row: dict, status: str, now: str, why: str, head: str) -> str:
    """A send the sync stopped (_stopped): «failed» with the reason, nothing kept or sent, a record in the order's journal, no «Входящие»
    (nobody has to write it by hand: the task was skipped, the order closed)."""
    done = conn.execute("UPDATE chat_send_log SET status = 'failed', text = '', next_at = '', error = ? WHERE token = ? AND status = ? AND at = ?",
                        ("остановлено: " + why, row["token"], status, row["at"]))
    conn.commit()
    if done.rowcount != 1:
        return "busy"
    _note_order(conn, chat, now, "%s: %s — система больше не повторяет." % (head, why), "yellow")
    return "stopped"


def _close_task(conn, token: str, text: str, now: str, how: str) -> None:
    """The task of a `task-…` mark is done once the sync delivered its message or found it in the chat: it is not left «ждёт». A failure here
    never touches the send."""
    if not str(token or "").startswith("task-"):
        return
    try:
        from . import order_engine
        order_engine.close_after_send(conn, token, text, now, how)
    except Exception:      # noqa: BLE001
        conn.rollback()
        log.warning("the task of the send %s was not closed after the sync delivered it", token)


def _inbox_done(conn, token: str, now: str, how: str) -> None:
    """The send of `token` reached the client by the sync (sent again or found in the chat): the open lines of «Входящие» its caller wrote
    about it («…система повторит сама», «…проверьте чат»: order_reserve, order_manager._tell_arrived; their id is the token, or the token and
    the moment) are answered with the outcome, nobody sends it by hand (tails5). The sync's own alerts (`send-…`) and the lines a person
    answered are not touched. A failure here never touches the send."""
    token = str(token or "")
    if not token:
        return
    try:
        conn.execute("UPDATE manager_message SET status = 'answered', answer = ?, answered_at = ? WHERE sender = ? AND status = 'new' "
                     "AND (message_id = ? OR substr(message_id, 1, ?) = ?)",
                     ("Итог: сообщение клиенту дошло (%s). Делать ничего не нужно." % how, now, SYSTEM, token, len(token) + 1, token + "-"))
        conn.commit()
    except Exception:      # noqa: BLE001
        conn.rollback()
        log.warning("the lines of «Входящие» about the send %s were not closed after the sync delivered it", token)


def _inbox_close(conn, token: str, now: str, answer: str, alerts: bool = False) -> None:
    """The open lines of «Входящие» about the send of `token` are answered with `answer` (tails6): its caller's (the token, or the token and
    the moment, as _inbox_done finds them) and with `alerts` the sync's own (`send-<token>-…`, _alert_not_sent). An unknown send of a skipped
    task or a closed order that cannot be checked (cannot()), a new press of a failed one that found it in the chat or another message of
    ours (_failed_is_there). The lines a person answered are not touched. A failure here never touches the send."""
    token = str(token or "")
    if not token:
        return
    alert = "send-%s-" % token
    try:
        conn.execute("UPDATE manager_message SET status = 'answered', answer = ?, answered_at = ? WHERE sender = ? AND status = 'new' "
                     "AND (message_id = ? OR substr(message_id, 1, ?) = ? OR (? AND substr(message_id, 1, ?) = ?))",
                     (answer, now, SYSTEM, token, len(token) + 1, token + "-", 1 if alerts else 0, len(alert), alert))
        conn.commit()
    except Exception:      # noqa: BLE001
        conn.rollback()
        log.warning("the lines of «Входящие» about the send %s were not closed", token)


def _again_said(orig_at: str, again_at: str) -> str:
    """Who sent a send again (tails5): «Отправить снова» (or a new press of a failed one) sets the first start (orig_at) at that moment, the
    sync's send again keeps it and comes later; a row of before orig_at was sent again by the sync only."""
    if not again_at:
        return ""
    return " и после повторной отправки из панели" if orig_at and orig_at >= again_at else " и после автоматического повтора"


def _check_unknown(conn, client, writer, chat: dict, row: dict, now: str, clock) -> str:
    """Look into the chat for a send whose outcome is unknown (the owner 2026-10-25): its text there since the send is «sent»; nothing of
    ours there after CHECKS looks, the last CHECK_LAST after the send: a text is sent again by the sync itself, once (`again_at`, the owner
    25.10: «отправить снова сама, один раз, и записать это в журнал заказа»), under the same mark — its outcome is settled as any send's;
    a text sent again already, a photo or a send of before 2026-10-25 without its text is «lost» (a press of «Отправить снова» for a text).
    The chat Avito does not show, a chat without its history or another message of ours since is a verdict: it stays «unknown», the person
    looks. A passing trouble of Avito: next minute."""
    token, text, photo = row["token"], row["text"], bool(row["photo"])
    since, what = _since(row.get("orig_at") or row["at"]), _what(text, photo)       # from the first start: a send again moves `at` (M1)

    def update(**fields) -> bool:
        done = conn.execute("UPDATE chat_send_log SET %s WHERE token = ? AND status = 'unknown' AND at = ?" % ", ".join("%s = ?" % k for k in fields),
                            tuple(fields.values()) + (token, row["at"]))
        conn.commit()
        return done.rowcount == 1

    def cannot(why: str) -> str:
        if update(verdict=why, next_at="", text=""):
            try:
                stop = _stopped(conn, chat, token)                              # the task skipped, the order closed: nobody writes it by hand
            except Exception:      # noqa: BLE001                               # cannot tell: the advice as before
                conn.rollback()
                stop = ""
            if stop:
                _note_order(conn, chat, now, "%s клиенту не подтверждено, проверить нельзя: %s. %s — писать клиенту не нужно." % (
                    what, why, stop[:1].upper() + stop[1:]), "yellow")
                _inbox_close(conn, token, now, "Итог: проверить нельзя; %s — писать клиенту не нужно." % stop)    # nobody writes it by hand (tails6)
            else:
                _note_order(conn, chat, now, "%s клиенту не подтверждено, проверить нельзя: %s. Проверьте чат в кабинете Авито; если его там нет — "
                            "напишите клиенту вручную." % (what, why), "red")
        return "unknown"
    try:
        ours = _ours_since(conn, chat["id"], since) + _ours_at_avito(conn, client, chat, since)
    except _Recheck:
        update(next_at=_later(now, RETRY_AFTER))
        return "recheck"
    except ChatError:
        return cannot(UNREADABLE)
    same = _same_text(ours, text) if text and not photo else []
    if same:
        if update(status="sent", message_id=same[0], text="", next_at="", error=""):
            _note_order(conn, chat, now, "Сообщение клиенту подтверждено: дошло (найдено в чате Авито)", "green")
            _close_task(conn, token, text, now, "подтверждено: дошло")
            _inbox_done(conn, token, now, "найдено в чате Авито")
            return "sent"
        return "busy"
    history = conn.execute("SELECT history_ok FROM avito_chat WHERE id = ?", (chat["id"],)).fetchone()
    if not history or not history[0]:
        return cannot(UNREADABLE)
    if _others(conn, chat["id"], token, row["first_at"] or row["at"], ours):
        return cannot(OTHERS)
    tries = int(row["tries"] or 0) + 1
    try:
        last = (datetime.fromisoformat(row["at"]) + timedelta(seconds=CHECK_LAST)).isoformat(timespec="seconds")
    except (TypeError, ValueError):                                              # an unreadable time: the window counts from now
        last = _later(now, CHECK_LAST)
    if tries < CHECKS or now < last:
        update(tries=tries, next_at=max(last, _later(now, RETRY_AFTER)))
        return "wait"
    kept = bool(text) and not photo
    stop = _stopped(conn, chat, token)                                          # the task was skipped or the order closed (L4): no send, no button
    if stop:
        return _halt(conn, chat, row, "unknown", now, stop, "%s клиенту не дошло (в чате Авито его нет) %s" % (
            what, _again_said(row.get("orig_at") or "", row["again_at"]).strip() or "и не отправлено повторно"))
    allowed = conn.execute("SELECT chats_send FROM account WHERE id = ?", (chat["account_id"],)).fetchone()
    if kept and not row["again_at"] and allowed and allowed[0]:
        fresh = clock()
        if not _claim_by_sync(conn, token, "unknown", row["at"], fresh, again_at=fresh, tries=0, verdict=""):
            return "busy"                                                        # a press, another run, or sent again already
        _note_order(conn, chat, now, "Сообщение клиенту не дошло (в чате Авито его нет) — отправляем повторно автоматически, один раз", "yellow")
        try:
            sender = writer()
            user_id = connections.cabinet_user_id(conn, sender, chat["account_id"], retry=False)
        except Exception as err:      # noqa: BLE001                             # nothing went: the person sends it by the button
            _mark(conn, token, "lost", _send_record(chat["id"], err, text), next_at="", again_at="")    # nothing went: not «after the send again»
            _note_order(conn, chat, now, "Сообщение клиенту не отправлено повторно: Авито не отдал данные кабинета. Отправьте его кнопкой "
                        "«Отправить снова» в чате или в карточке заказа.", "red")
            return "lost"
        try:
            _deliver(conn, sender, chat, user_id, token, fresh, row["author"], 0, text, by_sync=True, note=" (повторно автоматически: первая отправка не дошла)")
        except ChatSendRetry:
            return "retry"
        except ChatSendUnknown:
            return "unknown"
        except ChatError:
            return "failed"
        _close_task(conn, token, text, fresh, "повторно автоматически")
        _inbox_done(conn, token, fresh, "повторно автоматически")
        return "resent"
    if not update(status="lost", tries=tries, next_at="", text=text if kept else ""):
        return "busy"
    _note_order(conn, chat, now, "%s клиенту не дошло%s: в чате Авито его нет. %s" % (what, _again_said(row.get("orig_at") or "", row["again_at"]), (
        "Отправьте его снова кнопкой «Отправить снова» в чате или в карточке заказа." if kept else "Отправьте вручную из чата.")), "red")
    return "lost"


def _retry_refused(conn, client, writer, chat: dict, row: dict, now: str, clock) -> str:
    """Send again a send Avito refused with a 402 (the owner 2026-10-25), after a look into the chat each time: its text there already is
    «sent»; another message of ours since the refusal, or another send started since, stops it («failed», a person may have written it
    again by hand: no double; _others); a
    passing trouble of the look puts it off a minute. Avito cannot show the chat at all: it goes again all the same (a 402 is Avito's «not
    taken»). RETRY_FOR after the first refusal it ends («failed» and «Входящие»): no message hours late. `writer`: () -> the client that may
    write."""
    token, text, photo, tries = row["token"], row["text"], bool(row["photo"]), int(row["tries"] or 0)
    what = _what(text, photo)

    def end(record: str, said: str, ending: str) -> str:
        if conn.execute("UPDATE chat_send_log SET status = 'failed', text = '', next_at = '', error = ? WHERE token = ? AND status = 'retry' AND at = ?",
                        (record, token, row["at"])).rowcount != 1:
            conn.commit()
            return "busy"
        conn.commit()
        _note_order(conn, chat, now, "%s клиенту не отправлено: %s" % (what, said), "red")
        _alert_not_sent(conn, chat, token, now, said, "" if photo or ending != "Отправьте вручную." else text, ending)
        return "failed"
    late = _retry_late(row["first_at"] or row["at"], now)
    since = _since(row.get("orig_at") or row["at"])                             # from the first start: a send again moves `at` (M1)

    def too_late() -> str:
        return end("повтор после 402 не успел за %d минут" % (RETRY_FOR // 60), "Авито отказал (402), а повторить за %d минут не удалось "
                   "(чтение чатов не шло). Система больше не повторяет." % (RETRY_FOR // 60), "Отправьте вручную.")
    try:
        ours = _ours_since(conn, chat["id"], since) + _ours_at_avito(conn, client, chat, since)
    except _Recheck:
        if late:
            stop = _stopped(conn, chat, token)
            return _halt(conn, chat, row, "retry", now, stop, "%s клиенту не отправлено повторно" % what) if stop else too_late()
        conn.execute("UPDATE chat_send_log SET next_at = ? WHERE token = ? AND status = 'retry' AND at = ?", (_later(now, RETRY_AFTER), token, row["at"]))
        conn.commit()
        return "recheck"
    except ChatError:
        ours = []
    same = _same_text(ours, text) if not photo else []
    if same:
        if conn.execute("UPDATE chat_send_log SET status = 'sent', message_id = ?, text = '', next_at = '', error = '' WHERE token = ? "
                        "AND status = 'retry' AND at = ?", (same[0], token, row["at"])).rowcount == 1:
            conn.commit()
            _note_order(conn, chat, now, "Сообщение клиенту подтверждено: дошло (найдено в чате Авито, повтор не нужен)", "green")
            _close_task(conn, token, text, now, "подтверждено: дошло")
            _inbox_done(conn, token, now, "найдено в чате Авито")
            return "sent"
        conn.commit()
        return "busy"
    stop = _stopped(conn, chat, token)                                          # the task was skipped or the order closed meanwhile (L4)
    if stop:
        return _halt(conn, chat, row, "retry", now, stop, "%s клиенту не отправлено повторно" % what)
    if _others(conn, chat["id"], token, row["first_at"] or row["at"], ours):    # looked first: the same text is «sent», another stops it
        return end("повтор остановлен: в чате есть другое наше сообщение", "повтор после отказа Авито (402) остановлен — в чате после отказа уже "
                   "есть другое наше сообщение (возможно, клиенту написали вручную).", "Проверьте чат: если клиенту уже написали, ничего не делайте.")
    if late:
        return too_late()
    allowed = conn.execute("SELECT chats_send FROM account WHERE id = ?", (chat["account_id"],)).fetchone()
    reason = "" if text and allowed and allowed[0] else ("отправка ответов выключена для аккаунта" if text else "нет текста для повтора")
    sender = None
    if not reason:
        try:
            sender = writer()
        except Exception as err:      # noqa: BLE001
            reason = "ключи Авито недоступны (%s)" % type(err).__name__
    if reason:
        return end(reason, "система не смогла повторить отправку после отказа Авито (402): %s." % reason, "Отправьте вручную.")
    stop = _stopped(conn, chat, token)                                          # once more right before: a skip during the look into the chat
    if stop:
        return _halt(conn, chat, row, "retry", now, stop, "%s клиенту не отправлено повторно" % what)
    fresh = clock()
    if not _claim_by_sync(conn, token, "retry", row["at"], fresh):
        return "busy"                                                            # a press or another run took it
    try:
        user_id = connections.cabinet_user_id(conn, sender, chat["account_id"], retry=False)
    except Exception as err:      # noqa: BLE001                                 # nothing went: counted as a refusal, then the end
        if tries < RETRIES:
            _mark(conn, token, "retry", _send_record(chat["id"], err, text), tries=tries + 1, next_at=_later(fresh, RETRY_AFTER))
            return "retry"
        _mark(conn, token, "failed", _send_record(chat["id"], err, text))
        said = "Авито не отдал данные кабинета, система больше не повторяет."
        _note_order(conn, chat, now, "%s клиенту не отправлено: %s" % (what, said), "red")
        _alert_not_sent(conn, chat, token, now, said, "" if photo else text)
        return "failed"
    try:
        _deliver(conn, sender, chat, user_id, token, fresh, row["author"], tries, text, photo=photo, by_sync=True)
    except ChatSendRetry:
        return "retry"
    except ChatSendUnknown:
        return "unknown"
    except ChatError:
        return "failed"
    _close_task(conn, token, text, fresh, "повтор после отказа Авито, код 402")
    _inbox_done(conn, token, fresh, "повтор после отказа Авито, код 402")
    return "sent"


def settle_sends(conn, client, writer, account_id: int, now: str, clock=None) -> dict:
    """The minute's sync (tires-chats) settles the sends of the account that wait on it (the owner 2026-10-25, «проверка чата и автоповтор
    при коде 402»): a 402 is sent again (_retry_refused), an unknown one looked up in the chat (_check_unknown; the ones of the last
    OLD_UNKNOWN hours, those of before this release too); a send the sync took and that stayed «sending» (the run was killed) becomes
    unknown. The 402s first, then by their time. `client` reads, `writer()` gives the client that may write (made once per run), `clock()`
    the time of a send (default `now`: a long run must not make its own fresh mark look cut off). One send never stops the others.
    Returns the count of each outcome."""
    clock = clock or (lambda: now)
    cutoff = (datetime.fromisoformat(now) - timedelta(seconds=OLD_UNKNOWN)).isoformat(timespec="seconds")
    stale = (datetime.fromisoformat(now) - timedelta(seconds=STALE_SENDING)).isoformat(timespec="seconds")
    week = (datetime.fromisoformat(now) - timedelta(seconds=PENDING_SAYS)).isoformat(timespec="seconds")
    mine = "SELECT id FROM avito_chat WHERE account_id = ?"
    conn.execute("UPDATE chat_send_log SET status = 'unknown', tries = 0, next_at = '' WHERE status = 'sending' AND next_at != '' AND at <= ? "
                 "AND chat_row IN (%s)" % mine, (stale, account_id))
    conn.execute("UPDATE chat_send_log SET text = '' WHERE status IN ('lost', 'unknown') AND text != '' AND at < ? AND chat_row IN (%s)" % mine,
                 (week, account_id))                                                     # the button is not shown after a week: the text goes
    conn.commit()
    names = ("token", "chat_row", "at", "author", "status", "text", "photo", "tries", "again_at", "first_at", "orig_at")
    rows = [dict(zip(names, r)) for r in conn.execute(
        "SELECT l.%s FROM chat_send_log l JOIN avito_chat c ON c.id = l.chat_row " % ", l.".join(names) +
        "WHERE c.account_id = ? AND l.next_at <= ? AND (l.status = 'retry' OR l.status = 'unknown' AND l.verdict = '' AND l.at >= ?) "
        "ORDER BY l.status = 'retry' DESC, l.next_at, l.at LIMIT ?", (account_id, now, cutoff, SETTLE_BUDGET))]
    made, kept = {}, []

    def write_client():                                                          # one client that may write per run, made when a send needs it
        if not kept:
            kept.append(writer())
        return kept[0]
    for row in rows:
        chat = get_chat(conn, row["chat_row"])
        try:
            if chat is None:                                                     # the chat was deleted in the panel: nothing to send it to
                conn.execute("UPDATE chat_send_log SET status = CASE status WHEN 'retry' THEN 'failed' ELSE status END, verdict = ?, text = '', "
                             "next_at = '' WHERE token = ?", ("чат удалён", row["token"]))
                conn.commit()
                outcome = "gone"
            elif row["status"] == "retry":
                outcome = _retry_refused(conn, client, write_client, chat, row, now, clock)
            else:
                outcome = _check_unknown(conn, client, write_client, chat, row, now, clock)
        except Exception as err:      # noqa: BLE001                             # this one next minute; the others go on
            conn.rollback()
            log.warning("the send %s to chat %s was not settled: %s", row["token"], row["chat_row"], type(err).__name__)
            try:
                conn.execute("UPDATE chat_send_log SET next_at = ? WHERE token = ? AND status IN ('retry', 'unknown')", (_later(now, RETRY_AFTER), row["token"]))
                conn.commit()
            except sqlite3.Error:
                conn.rollback()
            outcome = "error"
        made[outcome] = made.get(outcome, 0) + 1
    return made


PENDING_SAYS = 7 * 24 * 3600          # the sends shown above the chat: the ones of the last week that wait on the sync or on a person


def pending_sends(conn, chat_rows, now: str) -> List[dict]:
    """The sends of these chats that are not settled (the owner 2026-10-25): sent again after a 402, checked, not delivered, or nobody can
    tell. Newest first; each with what the person is told and whether «Отправить снова» may be pressed."""
    rows = [int(r) for r in chat_rows or [] if r]
    if not rows:
        return []
    since = (datetime.fromisoformat(now) - timedelta(seconds=PENDING_SAYS)).isoformat(timespec="seconds")
    looked = (datetime.fromisoformat(now) - timedelta(seconds=OLD_UNKNOWN)).isoformat(timespec="seconds")
    late = (datetime.fromisoformat(now) - timedelta(seconds=RETRY_FOR)).isoformat(timespec="seconds")
    found = []
    for token, chat_row, at, status, text, photo, tries, verdict, again_at, first_at, orig_at in conn.execute(
            "SELECT token, chat_row, at, status, text, photo, tries, verdict, again_at, first_at, orig_at FROM chat_send_log WHERE chat_row IN (%s) "
            "AND status IN ('retry', 'unknown', 'lost') AND at >= ? ORDER BY at DESC LIMIT 20" % ",".join("?" * len(rows)), rows + [since]):
        again = status == "lost" and bool(text) and not photo
        if status == "retry" and (first_at or at) < late:
            say = "Авито отказал (402), а система не повторила (чтение чатов не идёт). Отправьте вручную."
        elif status == "retry":
            say = "Авито отказал (402), повторю сам через минуту (попытка %d из %d)." % (max(tries, 1), RETRIES)
        elif status == "lost":
            say = "Не дошло%s: в чате Авито его нет. %s" % (_again_said(orig_at, again_at),
                                                           "Можно отправить снова." if again else "Отправьте вручную.")
        elif verdict:
            say = "Неизвестно, ушло ли; проверить нельзя: %s. Посмотрите чат в кабинете Авито." % verdict
        elif at < looked:
            say = "Неизвестно, ушло ли: отправка старше двух суток, система её не проверяет. Посмотрите чат в кабинете Авито."
        else:
            say = "Неизвестно, ушло ли: система проверяет чат, итог — через несколько минут."
        shown = "📷 Фото" if photo else (text[:80] + ("…" if len(text) > 80 else "") if text else "")
        found.append({"token": token, "chat_row": chat_row, "at": at, "status": status, "say": say, "text": shown, "again": again})
    return found
