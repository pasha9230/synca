from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta
from typing import Callable, Optional, Tuple

from . import connections
from .secretbox import SecretBox, SecretKeyError
from .transport import Transport, urllib_transport

TELEGRAM_LIMIT = 4000
_TOKEN = re.compile(r"[0-9]+:[A-Za-z0-9_-]{10,}")


class NotifyError(Exception):
    pass


def _checked(token: str, chat_id: str):
    token = (token or "").strip()
    chat_id = str(chat_id or "").strip()
    if not token or not chat_id:
        raise NotifyError("Telegram не настроен: нужны токен бота и chat_id.")
    if not _TOKEN.fullmatch(token):
        raise NotifyError("Неверный формат токена бота.")
    return token, chat_id


def _post_telegram(http: Transport, token: str, method: str, headers: dict, body: bytes, what: str) -> None:
    """POST to the bot API; any failure is a NotifyError that never carries the token."""
    try:
        status, _, raw = http("POST", "https://api.telegram.org/bot%s/%s" % (token, method), headers, body)
    except OSError:
        raise NotifyError("Telegram недоступен: нет связи с сервером.") from None
    if status != 200:
        raise NotifyError("Telegram отклонил %s (код %s)." % (what, status))
    try:
        accepted = json.loads(raw.decode("utf-8")).get("ok") is True
    except (ValueError, AttributeError, UnicodeError):
        accepted = False
    if not accepted:
        raise NotifyError("Telegram не принял %s." % what)


SENT_PREFIX = "telegram-sent:"
SENT_WINDOW = timedelta(hours=1)          # as VK's random_id with the hour (order_notify.deliver): the same alert within the hour is a repeat


def _sent_key(key: str) -> str:
    return SENT_PREFIX + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _window_start(now: str) -> str:
    return (datetime.fromisoformat(now[:19]) - SENT_WINDOW).isoformat(timespec="seconds")


def sent_recently(conn, key: str, now: str) -> bool:
    """Telegram has no key against a repeat: the same message (`key`: what it is and to whom) went within the hour, so it is not sent again.
    A sending that is repeated is one whose caller failed after it (a busy base before its own mark): the next look sends it once more."""
    row = conn.execute("SELECT value FROM app_state WHERE key = ?", (_sent_key(key),)).fetchone()
    return bool(row) and str(row[0]) > _window_start(now)


def remember_sent(conn, key: str, now: str) -> None:
    """Mark the message as sent (in the caller's commit, with its own record of it); the marks older than the hour go, so they never pile up."""
    conn.execute("DELETE FROM app_state WHERE key LIKE ? AND value <= ?", (SENT_PREFIX + "%", _window_start(now)))
    conn.execute("INSERT OR REPLACE INTO app_state (key, value) VALUES (?, ?)", (_sent_key(key), now[:19]))


def send_message(token: str, chat_id: str, text: str, http: Transport = urllib_transport) -> None:
    token, chat_id = _checked(token, chat_id)
    body = json.dumps({"chat_id": chat_id, "text": text[:TELEGRAM_LIMIT], "disable_web_page_preview": True}).encode("utf-8")
    _post_telegram(http, token, "sendMessage", {"Content-Type": "application/json"}, body, "сообщение")


CAPTION_LIMIT = 1024


def send_photo(token: str, chat_id: str, caption: str, jpeg: bytes, http: Transport = urllib_transport) -> None:
    token, chat_id = _checked(token, chat_id)
    boundary = "tiresphoto" + secrets.token_hex(12)
    parts = []
    for name, value in (("chat_id", chat_id), ("caption", caption[:CAPTION_LIMIT])):
        parts.append(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (boundary, name, value)).encode("utf-8"))
    parts.append(('--%s\r\nContent-Disposition: form-data; name="photo"; filename="sheet.jpg"\r\nContent-Type: image/jpeg\r\n\r\n' % boundary).encode("utf-8"))
    parts.append(jpeg + b"\r\n")
    parts.append(("--%s--\r\n" % boundary).encode("utf-8"))
    _post_telegram(http, token, "sendPhoto", {"Content-Type": "multipart/form-data; boundary=" + boundary}, b"".join(parts), "фото")


def _owner_telegram(conn, box: Optional[SecretBox]) -> Optional[Tuple[str, str]]:
    """(token, chat_id) of the owner's bot, or None if Telegram is not configured or unreadable."""
    if box is None:
        return None
    chat_id = connections.get_config(conn, "telegram", 0).get("chat_id")
    try:
        token = connections.get_secret(conn, box, "telegram", 0, "bot_token")
    except SecretKeyError:
        return None
    return (token, chat_id) if chat_id and token else None


def notifier_for(conn, box: Optional[SecretBox], http: Transport = urllib_transport) -> Optional[Callable[[str], None]]:
    """A function that sends text to the owner, or None if Telegram is not configured or unreadable."""
    owner = _owner_telegram(conn, box)
    return None if owner is None else (lambda text: send_message(owner[0], owner[1], text, http=http))


def notifier_photo_for(conn, box: Optional[SecretBox], http: Transport = urllib_transport) -> Optional[Callable[[str, bytes], None]]:
    """A function that sends a picture with a caption to the owner, or None if Telegram is not configured or unreadable."""
    owner = _owner_telegram(conn, box)
    return None if owner is None else (lambda caption, jpeg: send_photo(owner[0], owner[1], caption, jpeg, http=http))


MAX_FILE = 20 * 1024 * 1024


def _safe_name(name: str) -> str:
    name = re.sub(r"[^\w.\- ]+", "_", str(name or "file"), flags=re.UNICODE).strip(" .") or "file"
    return name[:80]


def read_inbox(token: str, owner_chat_id: str, http: Transport = urllib_transport, limit: int = 100, download_to=None) -> dict:
    """What the owner sent to the bot: the messages of the owner's chat only (a stranger who found the bot is counted, never read).
    With `download_to` the files and photos are saved there FIRST; only then the updates are acknowledged, so nothing is lost when a download fails."""
    from pathlib import Path
    token = (token or "").strip()
    if not _TOKEN.fullmatch(token):
        raise NotifyError("Неверный формат токена бота.")

    def call(method, query=""):
        try:
            status, _, raw = http("GET", "https://api.telegram.org/bot%s/%s%s" % (token, method, query), {}, None)
        except OSError:
            raise NotifyError("Telegram недоступен: нет связи с сервером.") from None
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError):
            data = {}
        if status != 200 or not data.get("ok"):
            raise NotifyError("Telegram не отдал данные (код %s)." % status)
        return data.get("result")

    def fetch(file_id, name):
        info = call("getFile", "?file_id=" + file_id) or {}
        path = info.get("file_path")
        if not path:
            raise NotifyError("Telegram не отдал путь файла.")
        try:
            status, _, body = http("GET", "https://api.telegram.org/file/bot%s/%s" % (token, path), {}, None)
        except OSError:
            raise NotifyError("Telegram недоступен: нет связи с сервером.") from None
        if status != 200 or len(body) > MAX_FILE:
            raise NotifyError("Файл не скачан (код %s)." % status)
        target = Path(download_to) / name
        target.write_bytes(body)
        return str(target)
    updates = call("getUpdates", "?limit=%d" % limit) or []
    messages, ignored, last = [], 0, None
    for upd in updates:
        last = max(last or 0, int(upd.get("update_id") or 0))
        msg = upd.get("message") or upd.get("channel_post") or {}
        if str((msg.get("chat") or {}).get("id")) != str(owner_chat_id):
            ignored += 1
            continue
        text = str(msg.get("text") or msg.get("caption") or "")
        files = []
        document = msg.get("document")
        if document:
            text = (text + " " if text else "") + "[файл: %s]" % (document.get("file_name") or document.get("file_id"))
            files.append({"kind": "document", "file_id": document.get("file_id"), "name": _safe_name(document.get("file_name") or "document_%s" % upd.get("update_id"))})
        if msg.get("photo"):
            text = (text + " " if text else "") + "[фото]"
            biggest = max(msg["photo"], key=lambda p: (p.get("width") or 0) * (p.get("height") or 0))
            files.append({"kind": "photo", "file_id": biggest.get("file_id"), "name": "photo_%s.jpg" % upd.get("update_id")})
        if msg.get("voice") or msg.get("audio"):
            text = (text + " " if text else "") + "[голосовое или аудио: текст не читается]"
        if download_to:
            for f in files:
                f["path"] = fetch(f["file_id"], f["name"])
        messages.append({"at": msg.get("date"), "text": text, "files": files,
                         "forwarded": bool(msg.get("forward_origin") or msg.get("forward_date") or msg.get("forward_from"))})
    if last is not None:
        call("getUpdates", "?limit=%d&offset=%d" % (limit, last + 1))
    return {"messages": messages, "ignored": ignored, "last_update_id": last}
