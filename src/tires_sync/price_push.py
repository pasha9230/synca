"""The new price on Avito right after «Применить» (the owner 2026-11-03: «Цена на Авито сразу после «Применить»»).

«Применить» already rebuilds and publishes the feeds on our side (panel apply_publish); Avito reads the published feed only by its own
schedule. So the price that goes through the item API is the one of the published feed, never computed apart: before the rebuild the
prices of each live feed are noted, after the publication the ads whose price changed are queued (`avito_price`, one row per Avito ad).
Only ads the feed had before and still has, with one live Avito ad (avito_ad); new ads, removed ones and the stock are never touched.
A feed the gate stopped is not published, so it queues nothing.

Off unless switched on per account (connection kind `avito_price`): «off» makes not a single request, «dry» queues what would go and
sends nothing (the lead compares it with Avito before switching on), «on» sends. The queue is sent by a process of its own
(`tires-sync price-push`, started by the panel; the hourly cycle takes what is left): before each ad its price is read again from the
published feed (an ad gone, or a price changed since, is sent as the feed has it now or not at all), a pause between requests, 429 waits
as Retry-After says (the client), a refused token or a failing service stops the account, the rest waits for the next run."""
from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import connections, store
from .avito import AvitoAuthError, AvitoClient, AvitoError, RateLimited

KIND = "avito_price"
MODES = ("off", "dry", "on")
MODE_LABELS = {"off": "выключено", "dry": "проверка (ничего не отправляется)", "on": "включено"}
PAUSE = 0.5                  # seconds between two requests: at most 120 a minute, Avito allows 150 for the method
REFUSAL_STOP = 5             # ads in a row Avito refuses: it refuses the method itself, the account stops
WAITING = ("queued", "not_sent")
STATUS_TEXT = {"queued": "в очереди", "dry": "отправили бы (проверка)", "sent": "отправлено", "failed": "ошибка",
               "not_sent": "не отправлено, повтор", "skipped": "не отправлялось"}


def moscow_now() -> datetime:
    return datetime.utcnow() + timedelta(hours=3)


def mode(conn, account_id: int) -> str:
    value = connections.get_config(conn, KIND, account_id).get("mode")
    return value if value in MODES else "off"


def save_mode(conn, account_id: int, value: str, now: str, author: str) -> str:
    if value not in MODES:
        raise ValueError("Неизвестный режим цен.")
    connections.set_config(conn, KIND, account_id, {"mode": value}, now, author)
    store.write_audit(conn, now, author, "avito_price_config", {"account_id": account_id, "mode": value})
    conn.commit()
    return value


def feed_prices(path: Path) -> Optional[Dict[str, int]]:
    """Id → Price of a feed file; None when there is no readable file."""
    try:
        root = ET.parse(str(path)).getroot()
    except (OSError, ET.ParseError):
        return None
    out: Dict[str, int] = {}
    for ad in root.findall("Ad"):
        ad_id, price = (ad.findtext("Id") or "").strip(), (ad.findtext("Price") or "").strip()
        if ad_id and price.isdigit() and int(price) > 0:
            out[ad_id] = int(price)
    return out


def _live_path(feed_root, account: dict) -> Path:
    return Path(feed_root) / account["feed_token"] / "avito_feed.xml"


def snapshot(conn, feed_root) -> Dict[int, Dict[str, int]]:
    """The prices of the published feed of every account switched on (or on check), before a rebuild: nothing is read for the others."""
    out = {}
    for account in connections.list_feed_accounts(conn):
        if mode(conn, account["id"]) != "off":
            prices = feed_prices(_live_path(feed_root, account))
            if prices is not None:
                out[account["id"]] = prices
    return out


def _items_of(conn, account_id: int) -> Dict[str, List[int]]:
    """Our Id → the live Avito ads of it, as the last reading of the cabinet saw them (avito_ad)."""
    out: Dict[str, List[int]] = {}
    for avito_id, ad_id in conn.execute(
            "SELECT avito_id, ad_id FROM avito_ad WHERE account_id = ? AND status = 'active' AND ad_id IS NOT NULL AND ad_id != '' "
            "AND seen_at = (SELECT MAX(seen_at) FROM avito_ad WHERE account_id = ?)", (account_id, account_id)):
        out.setdefault(str(ad_id), []).append(int(avito_id))
    return out


def queue_changes(conn, feed_root, before: Dict[int, Dict[str, int]], published_ids, now: str, author: str) -> Dict[int, int]:
    """After the feeds were published: the ads of each published account (switched on or on check) whose price in the feed changed, queued
    with the new price of the feed. Returns account id → ads queued."""
    counts: Dict[int, int] = {}
    for account_id in published_ids:
        if account_id not in before:
            continue
        account = connections.get_account(conn, account_id)
        current = mode(conn, account_id)
        if account is None or current == "off":
            continue
        after = feed_prices(_live_path(feed_root, account)) or {}
        old = before[account_id]
        items = _items_of(conn, account_id)
        status = "dry" if current == "dry" else "queued"
        n = 0
        for ad_id, price in sorted(after.items()):
            if ad_id not in old or old[ad_id] == price or len(items.get(ad_id, ())) != 1:
                continue                                    # a new ad, the same price, no live Avito ad (or two: which one is unknown)
            conn.execute(
                "INSERT INTO avito_price (account_id, avito_id, ad_id, price, old_price, status, error, queued_at, at) "
                "VALUES (?,?,?,?,?,?,'',?,?) ON CONFLICT(account_id, avito_id) DO UPDATE SET ad_id = excluded.ad_id, price = excluded.price, "
                "old_price = excluded.old_price, status = excluded.status, error = '', queued_at = excluded.queued_at, at = excluded.at",
                (account_id, items[ad_id][0], ad_id, price, old[ad_id], status, now, now))
            n += 1
        if n:
            store.write_audit(conn, now, author, "avito_price_queued", {"account_id": account_id, "count": n, "mode": current})
            counts[account_id] = n
    conn.commit()
    return counts


def _mark(conn, account_id: int, avito_id: int, status: str, error: str, now: str, price: Optional[int] = None) -> None:
    if price is None:
        conn.execute("UPDATE avito_price SET status = ?, error = ?, at = ? WHERE account_id = ? AND avito_id = ?",
                     (status, error[:300], now, account_id, avito_id))
    else:
        conn.execute("UPDATE avito_price SET status = ?, error = ?, at = ?, price = ? WHERE account_id = ? AND avito_id = ?",
                     (status, error[:300], now, price, account_id, avito_id))
    conn.commit()


def _describe(err: BaseException) -> str:
    if isinstance(err, AvitoError):
        return ("%s %s" % (err.status, err.message))[:200]
    return ("%s: %s" % (type(err).__name__, err))[:200]


def _refused(err: AvitoError) -> bool:
    """Avito refused this ad (a bad id, a category without the method, a bad price), not the keys, the rate or the service."""
    return not isinstance(err, (AvitoAuthError, RateLimited)) and 400 <= err.status < 500 and err.status not in (401, 403, 429)


def push_account(conn, client: AvitoClient, account: dict, feed_root, now: Optional[datetime] = None,
                 sleep: Callable[[float], None] = time.sleep, author: str = "система") -> Dict[str, int]:
    """Send the waiting prices of one account (a writing client). Each price is the published feed's at the moment of sending."""
    now_iso = (now or moscow_now()).isoformat(timespec="seconds")
    counts = {"sent": 0, "failed": 0, "not_sent": 0, "skipped": 0}
    rows = conn.execute("SELECT avito_id, ad_id, price FROM avito_price WHERE account_id = ? AND status IN (?, ?) ORDER BY ad_id",
                        (account["id"],) + WAITING).fetchall()
    if not rows:
        return counts
    live = feed_prices(_live_path(feed_root, account))
    stopped = ""
    if live is None:
        stopped = "опубликованный фид не прочитан"
    else:
        try:
            client._bearer()                                 # the token first: refused keys stop the account before any price
        except (AvitoError, OSError) as err:
            stopped = "токен Авито: %s" % _describe(err)
    first, in_row = True, 0
    for avito_id, ad_id, queued in rows:
        if stopped:
            _mark(conn, account["id"], avito_id, "not_sent", stopped, now_iso)
            counts["not_sent"] += 1
            continue
        price = live.get(ad_id)
        if price is None:
            _mark(conn, account["id"], avito_id, "skipped", "объявления уже нет в опубликованном фиде", now_iso)
            counts["skipped"] += 1
            continue
        if not first:
            sleep(PAUSE)
        first = False
        try:
            client.update_price(int(avito_id), price)
        except (AvitoError, OSError) as err:
            if isinstance(err, AvitoError) and _refused(err):
                _mark(conn, account["id"], avito_id, "failed", _describe(err), now_iso, price)
                counts["failed"] += 1
                in_row += 1
                if in_row >= REFUSAL_STOP:
                    stopped = "остановлено: Авито отказывает подряд (%d раз): %s" % (in_row, _describe(err))
                continue
            _mark(conn, account["id"], avito_id, "not_sent", _describe(err), now_iso, price)
            counts["not_sent"] += 1
            stopped = "остановлено: %s" % _describe(err)          # 429 after the waits, the keys, 5xx, the network: the rest waits
            continue
        in_row = 0
        _mark(conn, account["id"], avito_id, "sent", "" if price == queued else "цена в фиде с тех пор %d" % price, now_iso, price)
        counts["sent"] += 1
    store.write_audit(conn, now_iso, author, "avito_price_push", dict(counts, account_id=account["id"]))
    conn.commit()
    return counts


def _client_for(conn, box, account: dict, http, sleep: Callable[[float], None] = time.sleep) -> Optional[AvitoClient]:
    client_id = connections.get_secret(conn, box, "avito", account["id"], "client_id")
    client_secret = connections.get_secret(conn, box, "avito", account["id"], "client_secret")
    if not client_id or not client_secret:
        return None
    return AvitoClient(client_id, client_secret, http=http, now=time.time, sleep=sleep, allow_write=True)


class _Lock:
    def __init__(self, handle):
        self.handle = handle

    def close(self) -> None:
        try:
            fcntl.flock(self.handle, fcntl.LOCK_UN)
        finally:
            self.handle.close()


def _try_lock(feed_root) -> Optional[_Lock]:
    path = Path(feed_root) / ".price-push.lock"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(str(path), "a")
    except OSError:
        return None
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return _Lock(handle)


def run(conn, box, feed_root, http, now: Optional[datetime] = None, sleep: Callable[[float], None] = time.sleep, out=None) -> Dict[int, dict]:
    """Every account with waiting prices: switched on → sent; switched off or on check since they were queued → nothing goes (not a
    request). One process at a time: another one sending makes this a no-op."""
    out = out or sys.stdout
    lock = _try_lock(feed_root)
    if lock is None:
        print("цены на Авито: другой процесс уже отправляет", file=out)
        return {}
    done: Dict[int, dict] = {}
    try:
        now = now or moscow_now()
        now_iso = now.isoformat(timespec="seconds")
        ids = [row[0] for row in conn.execute("SELECT DISTINCT account_id FROM avito_price WHERE status IN (?, ?)", WAITING)]
        for account_id in ids:
            account = connections.get_account(conn, account_id)
            current = mode(conn, account_id) if account is not None else "off"
            if account is None or current != "on" or not account["live"] or not account["feed_enabled"]:
                why = "отправка цен выключена" if current != "on" else "публикация аккаунта выключена"
                conn.execute("UPDATE avito_price SET status = 'skipped', error = ?, at = ? WHERE account_id = ? AND status IN (?, ?)",
                             (why, now_iso, account_id) + WAITING)
                conn.commit()
                continue
            client = _client_for(conn, box, account, http, sleep) if box is not None else None
            if client is None:
                conn.execute("UPDATE avito_price SET status = 'not_sent', error = ?, at = ? WHERE account_id = ? AND status IN (?, ?)",
                             ("ключи Авито не заданы или мастер-ключ недоступен", now_iso, account_id) + WAITING)
                conn.commit()
                continue
            try:
                counts = push_account(conn, client, account, feed_root, now=now, sleep=sleep)
            except Exception as err:      # noqa: BLE001 - one account never stops the others
                conn.rollback()
                print("%s: цены на Авито не отправлены (%s: %s)" % (account["name"], type(err).__name__, str(err)[:120]), file=out)
                continue
            done[account_id] = counts
            print("%s: цены на Авито: %s" % (account["name"], ", ".join("%s %d" % kv for kv in counts.items())), file=out)
    finally:
        lock.close()
    return done


def summary(conn, account_id: int, limit: int = 50) -> dict:
    """The last batch of an account for its card: counts by status and the rows that did not go (or would go, on check)."""
    row = conn.execute("SELECT MAX(queued_at) FROM avito_price WHERE account_id = ?", (account_id,)).fetchone()
    last = row[0] if row else None
    if not last:
        return {"at": None, "counts": {}, "rows": []}
    counts = dict(conn.execute("SELECT status, COUNT(*) FROM avito_price WHERE account_id = ? AND queued_at = ? GROUP BY status",
                               (account_id, last)).fetchall())
    rows = [dict(zip(("avito_id", "ad_id", "price", "old_price", "status", "error", "at"), r)) for r in conn.execute(
        "SELECT avito_id, ad_id, price, old_price, status, error, at FROM avito_price WHERE account_id = ? AND queued_at = ? "
        "AND status != 'sent' ORDER BY status, ad_id LIMIT ?", (account_id, last, limit))]
    for r in rows:
        r["status_text"] = STATUS_TEXT.get(r["status"], r["status"])
    return {"at": last, "counts": counts, "rows": rows}


def line(counts: Dict[int, int]) -> str:
    """The flash after «Применить»: what was queued (empty when nothing)."""
    total = sum(counts.values())
    return "Цены на Авито: в очереди на отправку %d объявлений." % total if total else ""


_children: list = []


def start(args: List[str], popen=subprocess.Popen) -> Optional[int]:
    """Start a command of ours apart from the web request (its own session: it outlives the request); the ones started before and ended are
    reaped here. The pid, or None if it did not start."""
    _children[:] = [child for child in _children if child.poll() is None]
    try:
        proc = popen(args, stdin=subprocess.DEVNULL, start_new_session=True, close_fds=True, env=dict(os.environ))
    except OSError:
        return None
    _children.append(proc)
    return proc.pid


def spawn(db_path, feed_root, key_file: str = "", popen=subprocess.Popen) -> Optional[int]:
    """Start `tires-sync price-push` (the queue of prices) apart from the web request."""
    args = [sys.executable, "-m", "tires_sync", "price-push", "--db", str(db_path), "--feed-root", str(feed_root)]
    if key_file:
        args += ["--key-file", str(key_file)]
    return start(args, popen)
