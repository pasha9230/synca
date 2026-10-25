"""The calls through the Avito substitute numbers (calltracking): read from Avito (only reads), kept in avito_call, the client's number joins
the clients (clients.attach_call), the recording is kept on the server for 7 days and then deleted (the owner 2026-10-21: «Записи хранить
1 неделю»). Nothing is ever written to Avito.

Avito's answers (checked on the server 2026-10-23): a call is {callId, callTime "2026-10-23T13:54:30+03:00", talkDuration, waitingDuration,
buyerPhone, sellerPhone, virtualPhone[, itemId]}; no «missed» flag: a call without talk is a missed one. A recording comes up to ~30 minutes
after the call (425 or code 1005 until then); an account may have no access to the recordings at all (403, code 1002)."""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import clients, order_points, store
from .avito import AvitoError, RateLimited

RECORD_DAYS = 7                                  # the owner 2026-10-21: the recordings are kept one week
FIRST_DAYS = 7                                   # the first reading of an account: the last week
OVERLAP = timedelta(hours=1)                     # every reading goes back an hour: a call Avito shows late is not lost (a call is kept once)
PAGE = 100                                       # Avito's largest page
MAX_PAGES = 20
PAUSE = 1.0                                      # between two requests to Avito (429)
RECORDS_PER_RUN = 20
RECORD_PAUSE = 2.0
RETRY_WAIT = timedelta(minutes=10)               # a recording not ready yet is asked again after this
RETRY_DENIED = timedelta(days=1)                 # an account without access to the recordings is asked once a day
GIVE_UP_TRIES = 36                               # about six hours of «not ready»: Avito has none
NOT_FOUND_AFTER = timedelta(hours=1)             # a 404 for a call older than this: no recording
SYSTEM_AUTHOR = "звонки Авито"
STATES = {"": "нет записи", "wait": "ждём запись", "saved": "есть", "none": "записи нет", "denied": "нет доступа к записям",
          "deleted": "удалена (хранится %d дней)" % RECORD_DAYS}
FIELDS = ("id", "account_id", "call_id", "call_at", "buyer_phone", "seller_phone", "virtual_phone", "item_id", "talk_seconds", "wait_seconds",
          "answered", "point_id", "order_id", "record_state", "record_file", "record_size", "record_until", "record_deleted_at")


def _iso(at: datetime) -> str:
    return at.isoformat(timespec="seconds")


def moscow(value) -> str:
    """Avito's moment (RFC 3339, with an offset or Z) as the panel keeps time: Moscow, without the offset. Empty when it cannot be read."""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        at = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text)
    except ValueError:
        return ""
    if at.tzinfo is not None:
        at = (at - at.utcoffset()).replace(tzinfo=None) + timedelta(hours=3)
    return _iso(at)


def rfc(moscow_iso: str) -> str:
    return moscow_iso[:19] + "+03:00"


def _phone(raw) -> str:
    """A number as the clients keep it (E.164); the digits as Avito gave them when it is not a phone (a hidden number)."""
    return clients.normalize(raw) or "".join(ch for ch in str(raw or "") if ch.isdigit() or ch == "+")[:20]


def _seconds(value) -> int:
    try:
        return max(0, int(round(float(value or 0))))
    except (TypeError, ValueError):
        return 0


# ---- reading Avito -----------------------------------------------------------------------------------------------------------------

def sync_account(conn, client, account_id: int, now: str, sleep: Callable[[float], None], dry_run: bool = False) -> dict:
    """The new calls of one account since the last reading (an hour back), page by page with a pause. A call is kept once (account + callId);
    the mark is the time of the last whole reading (it never moves when a page was left unread), so a silent account keeps a short window."""
    key = "calls_since_%d" % account_id
    since = store.get_state(conn, key) or ""
    start = (datetime.fromisoformat(since) - OVERLAP) if since else (datetime.fromisoformat(now) - timedelta(days=FIRST_DAYS))
    date_from, date_to = rfc(_iso(start)), rfc(now)
    point = order_points.point_by_account(conn, account_id)
    own = clients.own_phones(conn)
    seen, new, offset, whole = 0, 0, 0, False
    for page_no in range(MAX_PAGES):
        if page_no:
            sleep(PAUSE)
        page = client.calls(date_from, date_to, limit=PAGE, offset=offset)
        seen += len(page)
        for call in page:
            at = moscow(call.get("callTime"))
            if dry_run or not at or not call.get("callId"):
                continue
            if _keep(conn, account_id, call, at, point["id"] if point else None, own, now):
                new += 1
        if not dry_run:
            conn.commit()
        if len(page) < PAGE:
            whole = True
            break
        offset += PAGE
    if whole and not dry_run:
        store.set_state(conn, key, now)
    return {"seen": seen, "new": new, "whole": whole}


def _keep(conn, account_id: int, call: dict, at: str, point_id, own: set, now: str) -> bool:
    talk = _seconds(call.get("talkDuration"))
    until = _iso(datetime.fromisoformat(at) + timedelta(days=RECORD_DAYS))
    try:
        call_id = int(call["callId"])
    except (TypeError, ValueError):
        return False
    cur = conn.execute(
        "INSERT OR IGNORE INTO avito_call (account_id, call_id, call_at, buyer_phone, seller_phone, virtual_phone, item_id, talk_seconds, "
        "wait_seconds, answered, point_id, record_state, record_until, synced_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (account_id, call_id, at, _phone(call.get("buyerPhone")), _phone(call.get("sellerPhone")), _phone(call.get("virtualPhone")),
         str(call.get("itemId") or ""), talk, _seconds(call.get("waitingDuration")), 1 if talk > 0 else 0, point_id,
         "wait" if talk > 0 else "", until, now))
    if cur.rowcount != 1:
        if talk > 0:                                                       # seen before it was answered (a call still ringing): the answer comes in
            conn.execute("UPDATE avito_call SET talk_seconds = ?, wait_seconds = ?, answered = 1, record_state = CASE WHEN record_state = '' "
                         "THEN 'wait' ELSE record_state END WHERE account_id = ? AND call_id = ? AND answered = 0",
                         (talk, _seconds(call.get("waitingDuration")), account_id, call_id))
        return False
    conn.execute("SAVEPOINT call_client")                                  # a client that cannot be found never loses the call
    try:
        clients.attach_call(conn, cur.lastrowid, now, SYSTEM_AUTHOR, own=own)
        conn.execute("RELEASE call_client")
    except Exception:      # noqa: BLE001
        conn.execute("ROLLBACK TO call_client")
        conn.execute("RELEASE call_client")
        logging.getLogger("tires_sync.calls").exception("the client of call %s could not be found", cur.lastrowid)
    return True


# ---- the recordings ------------------------------------------------------------------------------------------------------------------

def fetch_records(conn, client, account_id: int, folder: Path, now: str, sleep: Callable[[float], None], limit: int = RECORDS_PER_RUN) -> dict:
    """The recordings of the answered calls not older than a week: saved as <folder>/<account>/<callId>.mp3 (0640). Not ready: asked again
    later; no access for the account: every waiting call of it is marked and the account is asked again a day later (one request)."""
    at = datetime.fromisoformat(now)
    denied = store.get_state(conn, "calls_denied_%d" % account_id) or ""
    if denied and at - datetime.fromisoformat(denied) < RETRY_DENIED:      # the account has no access: its new calls wait for the next day
        conn.execute("UPDATE avito_call SET record_state = 'denied' WHERE account_id = ? AND record_state = 'wait'", (account_id,))
        conn.commit()
        return {"saved": 0, "waiting": 0, "none": 0, "denied": 0}
    rows = conn.execute(
        "SELECT id, call_id, call_at, record_tries FROM avito_call WHERE account_id = ? AND record_until > ? AND ("
        "(record_state = 'wait' AND record_tried_at <= ?) OR (record_state = 'denied' AND record_tried_at <= ?)) ORDER BY call_at DESC LIMIT ?",
        (account_id, now, _iso(at - RETRY_WAIT), _iso(at - RETRY_DENIED), limit)).fetchall()
    made = {"saved": 0, "waiting": 0, "none": 0, "denied": 0}
    for n, (row_id, call_id, call_at, tries) in enumerate(rows):
        if n:
            sleep(RECORD_PAUSE)
        try:
            data = client.call_record(call_id)
        except RateLimited:
            break                                                          # Avito asks to wait: the rest next time
        except AvitoError as err:
            if err.status == 403 or err.code == 1002:
                conn.execute("UPDATE avito_call SET record_state = 'denied', record_tried_at = ? WHERE account_id = ? AND record_state IN ('wait', 'denied')",
                             (now, account_id))
                store.set_state(conn, "calls_denied_%d" % account_id, now)
                conn.commit()
                made["denied"] += 1
                break
            gone = (err.status == 404 or err.code == 1004) and at - datetime.fromisoformat(call_at) > NOT_FOUND_AFTER
            state = "none" if gone or tries + 1 >= GIVE_UP_TRIES else "wait"
            if err.status >= 500:
                state = "wait"
            conn.execute("UPDATE avito_call SET record_state = ?, record_tries = record_tries + 1, record_tried_at = ? WHERE id = ?", (state, now, row_id))
            conn.commit()
            made["none" if state == "none" else "waiting"] += 1
            if err.status >= 500:
                break
            continue
        except OSError:
            break                                                          # the network: the rest next time
        if not data:
            conn.execute("UPDATE avito_call SET record_tries = record_tries + 1, record_tried_at = ? WHERE id = ?", (now, row_id))
            conn.commit()
            made["waiting"] += 1
            continue
        name = "%d/%d.mp3" % (account_id, int(call_id))
        try:
            _write(folder / name, data)
        except OSError as err:                                             # the disk or the folder: the rest next time, the call keeps waiting
            conn.execute("UPDATE avito_call SET record_tries = record_tries + 1, record_tried_at = ? WHERE id = ?", (now, row_id))
            conn.commit()
            logging.getLogger("tires_sync.calls").warning("the recording of call %s was not written (%s)", row_id, type(err).__name__)
            made["waiting"] += 1
            break
        if denied:
            store.set_state(conn, "calls_denied_%d" % account_id, "")
        conn.execute("UPDATE avito_call SET record_state = 'saved', record_file = ?, record_size = ?, record_tries = record_tries + 1, "
                     "record_tried_at = ? WHERE id = ?", (name, len(data), now, row_id))
        conn.commit()
        made["saved"] += 1
    return made


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".part")
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    os.chmod(str(temp), 0o640)
    os.replace(str(temp), str(path))


def clean_records(conn, folder: Path, now: str, clock: Callable[[], float] = time.time) -> int:
    """A week after the call its recording is deleted (the call stays, marked «удалена»). A file left behind (a cut writing, a base
    restored from a copy) is swept when it is older than the week and a day: nothing in the folder outlives the owner's rule."""
    rows = conn.execute("SELECT id, record_file FROM avito_call WHERE record_state = 'saved' AND record_until <= ?", (now,)).fetchall()
    for row_id, name in rows:
        path = record_path(folder, name)
        if path is not None:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        conn.execute("UPDATE avito_call SET record_state = 'deleted', record_file = '', record_deleted_at = ? WHERE id = ?", (now, row_id))
    conn.commit()
    edge = clock() - (RECORD_DAYS + 1) * 86400
    for path in Path(folder).glob("*/*"):
        try:
            if path.is_file() and path.suffix in (".mp3", ".part") and path.stat().st_mtime < edge:
                path.unlink()
        except OSError:
            continue
    return len(rows)


def record_path(folder: Path, name: str) -> Optional[Path]:
    """The file of a recording, only inside the folder of the recordings (the name is ours: <account>/<callId>.mp3)."""
    parts = str(name or "").split("/")
    if len(parts) != 2 or not all(p.replace(".mp3", "", 1).isdigit() for p in parts) or not parts[1].endswith(".mp3"):
        return None
    return Path(folder) / parts[0] / parts[1]


# ---- the panel ---------------------------------------------------------------------------------------------------------------------

def _row(r) -> dict:
    call = dict(zip(FIELDS, r[:len(FIELDS)]))
    call.update(client_id=r[len(FIELDS)], client_name=r[len(FIELDS) + 1] or "", account_name=r[len(FIELDS) + 2] or "",
                point_name=r[len(FIELDS) + 3] or "")
    call["record_text"] = STATES.get(call["record_state"], call["record_state"])
    return call


_COLUMNS = ", ".join("CASE WHEN k.order_id = -1 OR EXISTS (SELECT 1 FROM order_card o WHERE o.id = k.order_id) THEN k.order_id END"
                     if f == "order_id" else "k." + f for f in FIELDS)            # a deleted order is no link (back when it is restored)
_SELECT = ("SELECT %s, l.client_id, c.name, a.name, p.name FROM avito_call k LEFT JOIN client_call l ON l.call_row = k.id "
           "LEFT JOIN client c ON c.id = l.client_id LEFT JOIN account a ON a.id = k.account_id LEFT JOIN point p ON p.id = k.point_id"
           % _COLUMNS)


def get_call(conn, call_row) -> Optional[dict]:
    row = conn.execute(_SELECT + " WHERE k.id = ?", (call_row,)).fetchone()
    return _row(row) if row else None


def list_calls(conn, account_id=None, point_id=None, missed: bool = False, since: str = "", phone: str = "", limit: int = 300) -> List[dict]:
    where, args = [], []
    if account_id is not None:
        where.append("k.account_id = ?")
        args.append(account_id)
    if point_id is not None:
        where.append("k.point_id = ?")
        args.append(point_id)
    if missed:
        where.append("k.answered = 0")
    if since:
        where.append("k.call_at >= ?")
        args.append(since)
    if phone:
        where.append("k.buyer_phone LIKE ?")
        args.append("%" + phone + "%")
    sql = _SELECT + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY k.call_at DESC, k.id DESC LIMIT ?"
    return [_row(r) for r in conn.execute(sql, args + [limit])]


def search_digits(text) -> str:
    """The digits of a typed number as the phones are kept: «8 911 …» is «7911…»."""
    digits = "".join(ch for ch in str(text or "") if ch.isdigit())[:15]
    return "7" + digits[1:] if len(digits) == 11 and digits.startswith("8") else digits


def calls_of_client(conn, client_id) -> List[dict]:
    """Every call of the client, the newest first: a join records each call it moves and the card counts them all (хвост З12: no limit)."""
    return [_row(r) for r in conn.execute(_SELECT + " WHERE l.client_id = ? ORDER BY k.call_at DESC", (client_id,))]


def client_links(conn, client_ids, account_id=None, point_id=None) -> dict:
    """{client: {"chat": row, "order": id}} for «Звонки» (the owner 2026-10-23): the client's newest chat and its newest open order (the newest
    one when none is open), None when there is none. `account_id`: the chats of that account only; `point_id`: the orders of that point only."""
    ids = sorted({c for c in client_ids if c})
    found = {c: {"chat": None, "order": None} for c in ids}
    if not ids:
        return found
    marks = ",".join("?" * len(ids))
    only = " AND a.account_id = ?" if account_id is not None else ""
    for client_id, chat_row in conn.execute("SELECT l.client_id, a.id FROM client_chat l JOIN avito_chat a ON a.id = l.chat_row WHERE l.client_id IN (%s)%s "
                                            "ORDER BY a.last_at, a.id" % (marks, only), ids + ([account_id] if only else [])):
        found[client_id]["chat"] = chat_row                                     # ascending: the newest is the last one written
    only = " AND c.point_id = ?" if point_id is not None else ""
    for client_id, order_id in conn.execute("SELECT l.client_id, c.id FROM client_order l JOIN order_card c ON c.id = l.order_id JOIN order_stage s ON s.id = c.stage_id "
                                            "WHERE l.client_id IN (%s)%s ORDER BY s.kind = 'open', c.id" % (marks, only), ids + ([point_id] if only else [])):
        found[client_id]["order"] = order_id                                    # the open ones come last: the newest open, else the newest
    return found


def accounts_with_calls(conn) -> List[dict]:
    return [{"id": r[0], "name": r[1]} for r in conn.execute(
        "SELECT a.id, a.name FROM account a WHERE EXISTS (SELECT 1 FROM avito_call k WHERE k.account_id = a.id) ORDER BY a.id")]


def create_order(conn, call_row: int, now: str, author: str, own_point=None) -> int:
    """«Создать заказ» from a call: the client's number is in the order, the account's tag, the point of the account (or of the manager who
    pressed it). A call with an order already gives that order; two presses at once make one."""
    from . import chat_deals, orders
    call = get_call(conn, call_row)
    if call is None:
        raise orders.OrderError("Такого звонка нет.")
    orders.ensure_pipeline(conn, now)
    claim = conn.execute("UPDATE avito_call SET order_id = -1 WHERE id = ? AND order_id IS NULL", (call_row,))
    conn.commit()
    if claim.rowcount != 1:
        current = conn.execute("SELECT order_id FROM avito_call WHERE id = ?", (call_row,)).fetchone()[0]
        if current and current > 0 and conn.execute("SELECT 1 FROM order_card WHERE id = ?", (current,)).fetchone():
            return current
        if current is None or current == -1 or not conn.execute("UPDATE avito_call SET order_id = -1 WHERE id = ? AND order_id = ?",
                                                                (call_row, current)).rowcount:   # its order was deleted: a new one may be made
            raise orders.OrderError("Заказ для этого звонка уже создаётся.")
        conn.commit()
    try:
        comment = "Звонок %s на аккаунт «%s» (%s)" % (call["call_at"].replace("T", " ")[:16], call["account_name"],
                                                       "принят" if call["answered"] else "пропущен")
        phone = clients.usable(clients.normalize(call["buyer_phone"]), clients.own_phones(conn))         # never our own number
        card_id = orders.create_card(conn, {"client_name": call["client_name"], "client_phone": phone, "source": "avito", "comment": comment,
                                            "tags": chat_deals._account_tag(conn, call["account_id"]), "responsible": orders.default_responsible(conn)},
                                     now, author, require_contact=False, commit=False)
        orders.add_event(conn, card_id, now, author, "call", "Создан из звонка %s" % comment[len("Звонок "):])
        conn.execute("UPDATE avito_call SET order_id = ? WHERE id = ?", (card_id, call_row))
        conn.commit()                                     # the order and its link to the call together: a killed panel leaves neither (хвост З10)
    except BaseException:
        conn.rollback()
        conn.execute("UPDATE avito_call SET order_id = NULL WHERE id = ? AND order_id = -1", (call_row,))       # give the claim back
        conn.commit()
        raise
    orders._fire_rules(conn, card_id, now, ())                              # the rules of the first stage, as create_card does after its save
    of_account = order_points.point_by_account(conn, call["account_id"])     # the point of the account now, not when the call was read
    point = of_account["id"] if of_account else own_point
    name = of_account["name"] if of_account else (order_points.get_point(conn, point)["name"] if point else "")
    if point and chat_deals.claim_point(conn, card_id, point, now, SYSTEM_AUTHOR if of_account else author,
                                        "Привязан к точке «%s» по звонку" % name, auto=True):
        conn.commit()
        chat_deals.fire_point_rules(conn, card_id, now)
    return card_id


# ---- one run of the timer (the command calls-sync) ---------------------------------------------------------------------------------

FAIL_ALARM_RUNS = 30                              # about an hour of the 2-minute timer: a longer silence of one account is an alarm
FAIL_ALARM_AGAIN = 180                            # then again about every 6 hours (a lost alarm is not the only word)
LOST_ALERT_AGAIN = timedelta(hours=6)             # «Авито закрыл доступ к звонкам»: once per account in 6 hours


def run(conn, client_for, now: str, folder: Path, sleep: Callable[[float], None], account: int = 0, dry_run: bool = False,
        say: Callable[[str], None] = print, warn: Callable[[str], None] = print, alert: Callable[[str], None] = lambda text: None) -> int:
    """Every account whose chats are read (the lead 2026-10-23: an account «не используется» is not; `client_for(account)` gives its client
    or None): the new calls, then the recordings, then the cleaning of the old recordings. One account that fails never stops the others;
    1 when an account has failed for about an hour (the alarm). An account that had calls and lost the access to them is an `alert`
    (the messages of the service: the same text at most once in 6 hours)."""
    from . import connections
    alarm, first = False, True
    for acc in connections.list_chat_accounts(conn):
        if account and acc["id"] != account:
            continue
        try:
            client = client_for(acc)
        except Exception as err:      # noqa: BLE001 — a key that cannot be read: this account only
            warn("аккаунт «%s»: ключи не читаются (%s), пропущен" % (acc["name"], type(err).__name__))
            continue
        if client is None:
            continue
        if not first:
            sleep(PAUSE)
        first = False
        key = "calls_fail_%d" % acc["id"]
        try:
            try:
                got = sync_account(conn, client, acc["id"], now, sleep, dry_run=dry_run)
            except AvitoError as err:
                if err.status == 403 or err.code == 1002:                  # no calltracking for the account: no calls, never an alarm
                    conn.rollback()
                    say("аккаунт «%s»: коллтрекинг Авито не подключён (нет доступа к звонкам), пропущен" % acc["name"])
                    lost = "calls_lost_alert_%d" % acc["id"]                    # its own pause: two accounts never re-send each other's alert
                    said = store.get_state(conn, lost) or ""
                    if not dry_run and conn.execute("SELECT 1 FROM avito_call WHERE account_id = ? LIMIT 1", (acc["id"],)).fetchone() \
                            and (not said or datetime.fromisoformat(now) - datetime.fromisoformat(said) >= LOST_ALERT_AGAIN):
                        store.set_state(conn, lost, now)
                        alert("Tires Sync: аккаунт «%s»: Авито закрыл доступ к звонкам (коллтрекинг), новые звонки не читаются. "
                              "Проверьте подменный номер и доступ к API в кабинете." % acc["name"])
                    if not dry_run and store.get_state(conn, key) not in (None, "", "0"):
                        store.set_state(conn, key, "0")
                    continue
                raise
            records = {} if dry_run else fetch_records(conn, client, acc["id"], folder, now, sleep)
        except Exception as err:      # noqa: BLE001 — a network miss, an odd answer, a busy base: one account never stops the others
            conn.rollback()
            err = err if isinstance(err, (AvitoError, OSError)) else type(err).__name__          # of an odd failure only its kind is written
            if dry_run:
                warn("аккаунт «%s»: звонки не прочитаны (%s)" % (acc["name"], err))
                continue
            misses = int(store.get_state(conn, key) or "0") + 1
            store.set_state(conn, key, str(misses))
            alarm = alarm or (misses >= FAIL_ALARM_RUNS and (misses - FAIL_ALARM_RUNS) % FAIL_ALARM_AGAIN == 0)   # an hour, then every ~6 h
            warn("аккаунт «%s»: звонки не прочитаны (%s), подряд %d" % (acc["name"], err, misses))
            continue
        if not dry_run and store.get_state(conn, key) not in (None, "", "0"):
            store.set_state(conn, key, "0")
        if not dry_run and store.get_state(conn, "calls_lost_alert_%d" % acc["id"]):
            store.set_state(conn, "calls_lost_alert_%d" % acc["id"], "")             # the access is back: a new loss is said at once
        say("аккаунт «%s»: звонков прочитано %d, новых %d%s%s" % (
            acc["name"], got["seen"], got["new"], "" if got["whole"] else " (не все страницы: отметка не сдвинута)",
            "" if dry_run else ", записей скачано %d, ждут %d, нет %d, нет доступа %d" % (
                records.get("saved", 0), records.get("waiting", 0), records.get("none", 0), records.get("denied", 0))))
    if not dry_run:
        _repair_claims(conn)
        gone = clean_records(conn, folder, now)
        if gone:
            say("записей удалено (старше %d дней): %d" % (RECORD_DAYS, gone))
    return 1 if alarm else 0


def _repair_claims(conn) -> None:
    """A claim of «Создать заказ» (order_id = -1) that a killed panel left: seen in two runs in a row, the call is freed (a claim of right now
    is gone in a moment, so it is never seen twice)."""
    import json
    stuck = [r[0] for r in conn.execute("SELECT id FROM avito_call WHERE order_id = -1")]
    seen = set(json.loads(store.get_state(conn, "calls_stale_claims") or "[]"))
    for call_row in stuck:
        if call_row in seen:
            conn.execute("UPDATE avito_call SET order_id = NULL WHERE id = ? AND order_id = -1", (call_row,))
    store.set_state(conn, "calls_stale_claims", json.dumps([c for c in stuck if c not in seen]))
    conn.commit()
