"""Avito's webhook of the messenger: Avito calls /hook/avito/<secret> with every new message of a cabinet, and the message goes into its
chat at once, kept as the reading every minute keeps it (chats.store_pushed). The reading stays: it brings what the webhook missed and
the chats the panel does not have yet. Public by necessity: Avito signs nothing (its schema has only the address), so the secret in the
address is the only guard: one per account, only its hash is kept. Nothing is sent to anybody from here; subscribing writes to Avito
and is done only by the command with --confirm (cli avito-webhook-subscribe).

Avito's schema (messenger, 2026-10-21): POST /messenger/v3/webhook {"url"} subscribes, POST /messenger/v1/webhook/unsubscribe {"url"}
unsubscribes, POST /messenger/v1/subscriptions lists {"subscriptions": [{"url", "version"}]}. A notification is
{"id", "version", "timestamp", "payload": {"type": "message", "value": {"id", "chat_id", "user_id", "author_id", "created", "type",
"content", "read", ...}}}; user_id is the cabinet the webhook belongs to. The address must answer 200 within 2 seconds (also to «{}»)."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta
from typing import Callable, List, Optional, Tuple

from . import chats, connections, store

HOOK_PATH = "/hook/avito/"
MAX_BODY = 64 * 1024              # a message is a few kilobytes; more is not Avito
KEEP_DAYS = 30
SECRET = re.compile(r"^[A-Za-z0-9_-]{20,100}$")


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def hook_url(base: str, secret: str) -> str:
    return base.rstrip("/") + HOOK_PATH + secret


def masked(url: str) -> str:
    """An address of ours with its secret hidden (for the screen and the logs); another address as it is."""
    at = url.find(HOOK_PATH)
    return url[:at + len(HOOK_PATH)] + "***" if at >= 0 else url


def ours(url: str, base: str) -> bool:
    return url.startswith(base.rstrip("/") + HOOK_PATH)


def new_secret(conn, account_id: int, now: str) -> str:
    """A fresh secret for the account (the old address stops answering at once); only its hash is kept. Returns the secret."""
    secret = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO avito_webhook (account_id, secret_hash, created_at, subscribed_at) VALUES (?,?,?,'') "
                 "ON CONFLICT(account_id) DO UPDATE SET secret_hash = excluded.secret_hash, created_at = excluded.created_at, subscribed_at = ''",
                 (account_id, _hash(secret), now))
    conn.commit()
    return secret


def mark_subscribed(conn, account_id: int, now: str) -> None:
    conn.execute("UPDATE avito_webhook SET subscribed_at = ? WHERE account_id = ?", (now, account_id))
    conn.commit()


def forget(conn, account_id: int) -> None:
    """After the unsubscription: no secret is accepted for the account any more."""
    conn.execute("UPDATE avito_webhook SET secret_hash = '', subscribed_at = '' WHERE account_id = ?", (account_id,))
    conn.commit()


def state(conn, account_id: int, now: datetime) -> dict:
    """What the panel shows: subscribed (when), the last event, the events of the last day before `now` (Moscow time)."""
    row = conn.execute("SELECT secret_hash, subscribed_at, last_event_at FROM avito_webhook WHERE account_id = ?", (account_id,)).fetchone()
    subscribed = bool(row and row[0] and row[1])
    since = (now - timedelta(days=1)).isoformat(timespec="seconds")
    day = conn.execute("SELECT COUNT(*) FROM avito_webhook_event WHERE account_id = ? AND at >= ?", (account_id, since)).fetchone()[0]
    return {"subscribed": subscribed, "subscribed_at": row[1] if subscribed else "", "last_event_at": row[2] if row else "", "day": day}


def account_of(conn, secret: str) -> Optional[int]:
    """The account whose secret this is, only while it reads chats; None otherwise."""
    if not SECRET.match(secret or ""):
        return None
    row = conn.execute("SELECT w.account_id FROM avito_webhook w JOIN account a ON a.id = w.account_id "
                       "WHERE w.secret_hash = ? AND w.secret_hash != '' AND a.chats_enabled = 1", (_hash(secret),)).fetchone()
    return row[0] if row else None


def message_of(body: bytes) -> Optional[dict]:
    """The message of a notification, or None for anything else («{}» of Avito's check, another kind, garbage)."""
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError):                       # a nesting deeper than Python's stack is garbage too
        return None
    payload = data.get("payload") if isinstance(data, dict) else None
    value = payload.get("value") if isinstance(payload, dict) and payload.get("type") == "message" else None
    if not isinstance(value, dict) or not value.get("id") or not value.get("chat_id"):
        return None
    if not isinstance(value.get("content") or {}, dict):
        return None
    if not isinstance(value.get("created"), int) or isinstance(value.get("created"), bool):
        return None                                                               # a message without its time is left to the reading
    return value


def _as_read(value: dict) -> dict:
    """The webhook's message in the shape of a message of the chat's history (the reading's): the direction from the author."""
    mine = value.get("author_id") is not None and str(value.get("author_id")) == str(value.get("user_id"))
    read = value.get("read")
    return {"id": str(value["id"]), "created": value["created"],
            "type": str(value.get("type") or "text")[:20], "content": value.get("content") or {}, "direction": "out" if mine else "in",
            "isRead": bool(read), "read": read if isinstance(read, int) else 0}


def accept(conn, account_id: int, value: dict, now: str) -> str:
    """Keep one pushed message. The event is recorded once per message: a repeat of Avito does nothing. Returns what happened."""
    known = connections.get_avito_identity(conn, account_id)["id"]
    if known and str(value.get("user_id")) != str(known):
        return "other_cabinet"                                                   # not the cabinet of this account: nothing is kept
    message = _as_read(value)
    fresh = conn.execute("INSERT OR IGNORE INTO avito_webhook_event (account_id, message_id, chat_id, at) VALUES (?,?,?,?)",
                         (account_id, message["id"], str(value["chat_id"])[:200], now)).rowcount == 1
    conn.execute("UPDATE avito_webhook SET last_event_at = ? WHERE account_id = ?", (now, account_id))
    if not fresh:
        conn.commit()
        return "repeat"
    result = chats.store_pushed(conn, account_id, str(value["chat_id"]), message, now)
    conn.execute("UPDATE avito_webhook_event SET result = ? WHERE account_id = ? AND message_id = ?", (result, account_id, message["id"]))
    conn.execute("DELETE FROM avito_webhook_event WHERE at < ?", ((datetime.fromisoformat(now) - timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds"),))
    conn.commit()
    return result


def handle(conn, secret: str, body: bytes, now: str, run: Optional[Callable[[Callable], None]] = None) -> Tuple[int, str]:
    """(HTTP status, text). 404 for an unknown secret, 413 for a body over MAX_BODY, otherwise 200 at once. The message is kept by
    `run(job)`, job(conn), after the answer has gone (Avito waits 2 seconds, the base may be busy for longer); without `run` here.
    `now`: Moscow time, as the reading of the chats."""
    account_id = account_of(conn, secret)
    if account_id is None:
        return 404, ""
    if len(body) > MAX_BODY:
        return 413, ""
    value = message_of(body)
    if value is not None:
        (run or (lambda work: work(conn)))(lambda db: accept(db, account_id, value, now))
    return 200, "ok"


# ---- Avito: the subscriptions of a cabinet (the commands) -----------------------------------------------------------------------------

def subscriptions(client) -> List[str]:
    """The addresses the cabinet's webhooks go to (a read, though Avito asks for a POST)."""
    data = client._request("POST", "/messenger/v1/subscriptions", safe=True)          # a read: repeated like the other reading POSTs
    return [str(s.get("url") or "") for s in (data or {}).get("subscriptions") or [] if isinstance(s, dict)]


def subscribe(client, url: str) -> None:
    client._require_write()
    client._request("POST", "/messenger/v3/webhook", payload={"url": url}, retry=False)


def unsubscribe(client, url: str) -> None:
    client._require_write()
    client._request("POST", "/messenger/v1/webhook/unsubscribe", payload={"url": url}, retry=False)
