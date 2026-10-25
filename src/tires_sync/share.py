"""Links for clients: a snapshot of chosen positions behind a random token, valid for a week, revocable.
Only the hash of the token is stored; the link itself is shown once, at creation."""
from __future__ import annotations

import hashlib
import json
import secrets
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from . import share_photos

TTL_DAYS = 7
KEEP_AFTER_END_HOURS = 24
MAX_ITEMS = 60
MAX_ACTIVE = 50
MAX_STORED = 100      # active plus ended ones not yet purged: a cap on the disk used by photos
MAX_NOTE = 200
PHOTO_BUDGET_SECONDS = 40.0

# what a client may see about a position; nothing else is ever put into a snapshot
ITEM_FIELDS = ("name", "brand", "model", "size", "season", "studded", "load", "speed", "runflat", "country", "year", "price")


class ShareError(Exception):
    pass


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def active_count(conn, now: datetime) -> int:
    return conn.execute("SELECT COUNT(*) FROM share_link WHERE revoked_at IS NULL AND expires_at > ?", (_iso(now),)).fetchone()[0]


def clean_item(item: dict) -> dict:
    return {key: item.get(key) for key in ITEM_FIELDS}


def create(conn, share_root, items: List[dict], show_price: bool, note: str, now: datetime, author: str,
           fetch: Callable[[str], Tuple[bytes, str]] = share_photos.fetch,
           clock: Callable[[], float] = None) -> Tuple[str, int, int]:
    """(token, link id, positions without a photo). `items` carry the snapshot fields and, apart from them, `image` (the supplier's photo address)."""
    clock = clock or time.monotonic
    if not items:
        raise ShareError("Не выбрано ни одной позиции.")
    if len(items) > MAX_ITEMS:
        raise ShareError("В одной ссылке не больше %d позиций: выбрано %d." % (MAX_ITEMS, len(items)))
    if active_count(conn, now) >= MAX_ACTIVE:
        raise ShareError("Уже действует %d ссылок: отзовите ненужные." % MAX_ACTIVE)
    if conn.execute("SELECT COUNT(*) FROM share_link").fetchone()[0] >= MAX_STORED:
        raise ShareError("Накопилось много старых ссылок: подождите сутки, пока отозванные и истёкшие удалятся.")
    # 1) photos first: no database transaction is open while the network is slow
    root = Path(share_root)
    staging = root / (".tmp-" + secrets.token_hex(8))
    started, photos = clock(), []
    try:
        for item in items:
            photo = None
            if item.get("image") and clock() - started < PHOTO_BUDGET_SECONDS:
                try:
                    data, ext = fetch(item["image"])
                    staging.mkdir(parents=True, exist_ok=True)
                    photo = "%s.%s" % (secrets.token_hex(12), ext)
                    (staging / photo).write_bytes(data)
                except (share_photos.PhotoError, OSError):
                    photo = None
            photos.append(photo)
        # 2) one short transaction for the link and its items
        token = secrets.token_urlsafe(24)
        cursor = conn.execute(
            "INSERT INTO share_link (token_hash, created_at, created_by, expires_at, show_price, note) VALUES (?,?,?,?,?,?)",
            (token_hash(token), _iso(now), author, _iso(now + timedelta(days=TTL_DAYS)), 1 if show_price else 0, (note or "").strip()[:MAX_NOTE]))
        link_id = cursor.lastrowid
        for pos, (item, photo) in enumerate(zip(items, photos)):
            conn.execute("INSERT INTO share_item (link_id, pos, data, photo) VALUES (?,?,?,?)",
                         (link_id, pos, json.dumps(clean_item(item), ensure_ascii=False), photo))
        # 3) the photos get their final place before the link becomes visible
        if staging.exists():
            staging.rename(root / str(link_id))
        conn.commit()
    except BaseException:
        conn.rollback()
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return token, link_id, sum(1 for photo in photos if photo is None)


def find(conn, token: str, now: datetime) -> Optional[dict]:
    """The live link for a token, or None: unknown, expired and revoked are indistinguishable."""
    if not token or len(token) > 100:
        return None
    row = conn.execute(
        "SELECT id, created_at, expires_at, revoked_at, show_price, note FROM share_link WHERE token_hash = ?", (token_hash(token),)).fetchone()
    if row is None or row[3] is not None or row[2] <= _iso(now):
        return None
    return {"id": row[0], "created_at": row[1], "expires_at": row[2], "show_price": bool(row[4]), "note": row[5]}


def items_of(conn, link_id: int) -> List[dict]:
    out = []
    for data, photo in conn.execute("SELECT data, photo FROM share_item WHERE link_id = ? ORDER BY pos", (link_id,)):
        item = clean_item(json.loads(data))
        item["photo"] = photo
        out.append(item)
    return out


def photo_path(share_root, link_id: int, name: str) -> Optional[Path]:
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    path = Path(share_root) / str(link_id) / name
    return path if path.is_file() else None


def photo_of(conn, link_id: int, name: str) -> bool:
    return conn.execute("SELECT 1 FROM share_item WHERE link_id = ? AND photo = ?", (link_id, name)).fetchone() is not None


def count_view(conn, link_id: int, now: datetime) -> None:
    conn.execute("UPDATE share_link SET views = views + 1, last_view_at = ? WHERE id = ?", (_iso(now), link_id))
    conn.commit()


def list_links(conn, now: datetime) -> List[dict]:
    rows = conn.execute(
        "SELECT l.id, l.created_at, l.created_by, l.expires_at, l.revoked_at, l.show_price, l.note, l.views, l.last_view_at, "
        "(SELECT COUNT(*) FROM share_item i WHERE i.link_id = l.id) FROM share_link l ORDER BY l.id DESC").fetchall()
    out = []
    for r in rows:
        status = "revoked" if r[4] else ("expired" if r[3] <= _iso(now) else "active")
        out.append({"id": r[0], "created_at": r[1], "created_by": r[2], "expires_at": r[3], "show_price": bool(r[5]), "note": r[6],
                    "views": r[7], "last_view_at": r[8], "count": r[9], "status": status})
    return out


def revoke(conn, link_id: int, now: datetime) -> bool:
    cursor = conn.execute("UPDATE share_link SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (_iso(now), link_id))
    conn.commit()
    return cursor.rowcount == 1


def set_show_price(conn, link_id: int, show: bool, now: datetime) -> bool:
    """Only for a link that is still alive."""
    cursor = conn.execute("UPDATE share_link SET show_price = ? WHERE id = ? AND revoked_at IS NULL AND expires_at > ?",
                          (1 if show else 0, link_id, _iso(now)))
    conn.commit()
    return cursor.rowcount == 1


def purge(conn, share_root, now: datetime) -> int:
    """Forget links that ended more than a day ago, together with their photos."""
    border = _iso(now - timedelta(hours=KEEP_AFTER_END_HOURS))
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM share_link WHERE expires_at <= ? OR (revoked_at IS NOT NULL AND revoked_at <= ?)", (border, border))]
    for link_id in ids:
        conn.execute("DELETE FROM share_item WHERE link_id = ?", (link_id,))
        conn.execute("DELETE FROM share_link WHERE id = ?", (link_id,))
        shutil.rmtree(Path(share_root) / str(link_id), ignore_errors=True)
    conn.commit()
    root = Path(share_root)
    if root.is_dir():                                   # folders of links that no longer exist (an interrupted creation)
        known = {str(r[0]) for r in conn.execute("SELECT id FROM share_link")}
        for child in root.iterdir():
            if child.is_dir() and child.name not in known and (child.name.isdigit() or child.name.startswith(".tmp-")) \
                    and time.time() - child.stat().st_mtime > 3600:      # not one that another request is creating right now
                shutil.rmtree(child, ignore_errors=True)
    return len(ids)
