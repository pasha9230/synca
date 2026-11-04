"""«Свой остаток (ОТХ)» (the owner 2026-11-03): tyres of ours lying at a point, entered by hand in the card of a catalog position (also one
the supplier has no more). They count as «На ОТХ» everywhere the tyres of a refused order do (oth_stock._rows): the ad is held or brought
back, «В наличии» and «Шины в наличии на …» of the description, «Взять с ОТХ» of an order (oth_sale). A position the supplier took away goes
to Avito on them only with an own price of the account (sync.plan_decisions). Only the owner and the partner enter them, from the office
(panel/routes_catalog_oth). Every change is in oth_manual_log and the journal of actions. The rows: oth_manual (store.py)."""
from __future__ import annotations

import hashlib
import json
from typing import Dict, List, Optional

MAX_PIECES = 10000
MAX_NOTE = 200


class ManualError(ValueError):
    pass


def _audit(conn, now: str, author: str, action: str, detail: dict) -> None:
    conn.execute("INSERT INTO audit (at, author, action, detail) VALUES (?,?,?,?)", (now, author, action, json.dumps(detail, ensure_ascii=False)))


def _log(conn, goods_id: str, point_id: int, before: int, after: int, now: str, author: str, note: str, why: str) -> None:
    conn.execute("INSERT INTO oth_manual_log (goods_id, point_id, before, after, at, author, note, why) VALUES (?,?,?,?,?,?,?,?)",
                 (goods_id, point_id, before, after, now, author, note, why))


def pieces_of(value) -> int:
    """A quantity typed in the card: a whole number from 0 (0 removes the row)."""
    text = str(value if value is not None else "").strip()
    if not (text.isascii() and text.isdigit()) or len(text) > 5 or int(text) > MAX_PIECES:
        raise ManualError("Количество: целое число от 0 до %d." % MAX_PIECES)
    return int(text)


def rows(conn, goods_id: Optional[str] = None) -> List[dict]:
    """The rows of manual stock (of one position, or all): {id, goods_id, point_id, point, qty, author, at, note}; a deleted point gives none."""
    sql = ("SELECT m.id, m.goods_id, m.point_id, COALESCE(p.name, ''), m.qty, m.author, m.at, m.note FROM oth_manual m "
           "JOIN point p ON p.id = m.point_id WHERE m.qty > 0 ")
    args: tuple = ()
    if goods_id is not None:
        sql += "AND m.goods_id = ? "
        args = (goods_id,)
    return [dict(zip(("id", "goods_id", "point_id", "point", "qty", "author", "at", "note"), r))
            for r in conn.execute(sql + "ORDER BY m.goods_id, p.name, m.id", args)]


def log_of(conn, goods_id: str, limit: int = 50) -> List[dict]:
    return [dict(zip(("point_id", "point", "before", "after", "at", "author", "note", "why"), r)) for r in conn.execute(
        "SELECT l.point_id, COALESCE(p.name, '№' || l.point_id), l.before, l.after, l.at, l.author, l.note, l.why FROM oth_manual_log l "
        "LEFT JOIN point p ON p.id = l.point_id WHERE l.goods_id = ? ORDER BY l.id DESC LIMIT ?", (goods_id, limit))]


def set_qty(conn, goods_id: str, point_id: int, qty: int, now: str, author: str, note: Optional[str] = "") -> int:
    """The pieces of the position at the point become `qty` (0: the row goes); `note` None keeps the row's note. A test point is refused.
    Commits; returns the pieces before."""
    from .oth_stock import _test_points
    goods_id = str(goods_id or "").strip()
    if not goods_id:
        raise ManualError("Неизвестная позиция.")
    point = conn.execute("SELECT id, name FROM point WHERE id = ?", (int(point_id),)).fetchone()
    if point is None:
        raise ManualError("Такой точки нет.")
    if point[0] in _test_points(conn):
        raise ManualError("Тестовая точка: её остаток не считается.")
    if qty < 0 or qty > MAX_PIECES:
        raise ManualError("Количество: целое число от 0 до %d." % MAX_PIECES)
    row = conn.execute("SELECT qty, note FROM oth_manual WHERE goods_id = ? AND point_id = ?", (goods_id, point[0])).fetchone()
    before = int(row[0]) if row else 0
    note = (row[1] if row else "") if note is None else " ".join(str(note).split())[:MAX_NOTE]
    if before == qty and (row is None or row[1] == note):
        return before
    if qty == 0:
        conn.execute("DELETE FROM oth_manual WHERE goods_id = ? AND point_id = ?", (goods_id, point[0]))
    else:
        conn.execute("INSERT INTO oth_manual (goods_id, point_id, qty, author, at, note) VALUES (?,?,?,?,?,?) ON CONFLICT(goods_id, point_id) "
                     "DO UPDATE SET qty = excluded.qty, author = excluded.author, at = excluded.at, note = excluded.note",
                     (goods_id, point[0], qty, author, now, note))
    _log(conn, goods_id, point[0], before, qty, now, author, note, "hand")
    _audit(conn, now, author, "oth_manual", {"goods_id": goods_id, "point": point[0], "before": before, "after": qty, "note": note})
    conn.commit()
    return before


def take(conn, manual_id: int, pieces: int, now: str, author: str, order_id: int) -> bool:
    """«Взять с ОТХ» from a manual row: its quantity goes down by `pieces`, never below 0, in the statement itself (a take a moment ago in
    another tab). A row with 0 left stays (counted nowhere) for «Вернуть на ОТХ» to find its point. No commit: the caller's transaction (oth_sale.take)."""
    row = conn.execute("SELECT goods_id, point_id, qty FROM oth_manual WHERE id = ?", (int(manual_id),)).fetchone()
    if row is None or pieces < 1:
        return False
    if conn.execute("UPDATE oth_manual SET qty = qty - ? WHERE id = ? AND qty >= ?", (pieces, int(manual_id), pieces)).rowcount != 1:
        return False
    _log(conn, row[0], row[1], int(row[2]), int(row[2]) - pieces, now, author, "", "take:%d" % int(order_id))
    return True


def point_of(conn, manual_id: int) -> Optional[int]:
    row = conn.execute("SELECT point_id FROM oth_manual WHERE id = ?", (int(manual_id),)).fetchone()
    return int(row[0]) if row else None


def give_back(conn, goods_id: str, point_id: int, pieces: int, now: str, author: str, order_id: int) -> None:
    """«Вернуть на ОТХ» of tyres taken from a manual row: they lie at the point again (the row comes back if it went). No commit."""
    row = conn.execute("SELECT qty FROM oth_manual WHERE goods_id = ? AND point_id = ?", (goods_id, point_id)).fetchone()
    before = int(row[0]) if row else 0
    conn.execute("INSERT INTO oth_manual (goods_id, point_id, qty, author, at, note) VALUES (?,?,?,?,?,'') ON CONFLICT(goods_id, point_id) "
                 "DO UPDATE SET qty = qty + excluded.qty", (goods_id, point_id, pieces, author, now))
    _log(conn, goods_id, point_id, before, before + pieces, now, author, "", "untake:%d" % int(order_id))


def pieces(conn) -> Dict[str, int]:
    """goods_id → the manual pieces of it (all points but the test ones)."""
    from .oth_stock import manual_rows
    found: Dict[str, int] = {}
    for r in manual_rows(conn):
        found[r["goods_id"]] = found.get(r["goods_id"], 0) + int(r["qty"])
    return found


def gone_price(conn, goods_id: str, settings) -> Optional[int]:
    """The price of a position the supplier has no more in the account of `settings`: its own price only (a percent needs the last purchase
    the archive knows); None: no own price there, so it does not go to Avito on «Свой остаток»."""
    from . import catalog_archive
    from .pricing import own_price
    own = (getattr(settings, "own_prices", None) or {}).get(goods_id)
    if own is None:
        return None
    if own[0] == "price":
        return own_price(0, own)
    info = catalog_archive.item_info(conn, goods_id) or {}
    return own_price(float(info.get("last_price") or 0), own)


NO_PRICE_KEY = "oth_manual_no_price:%d"         # app_state: the codes of an account last told «нет своей цены» (told once, again after a change)
NO_ID_KEY = "oth_manual_no_id:%d"               # the same for «раньше не публиковалась в этом аккаунте» (no Id of its own to come back under)
_NOT_SENT = {"price": (NO_PRICE_KEY, "нет своей цены — на Авито не уходит", "oth_manual_no_price", "no_price"),
             "id": (NO_ID_KEY, "раньше не публиковалась в этом аккаунте (или под другим Id) — новый Id не выдаём, на Авито не уходит",
                    "oth_manual_no_id", "no_id")}


def retired_id(conn, account_id: int, goods_id: str) -> Optional[str]:
    """The Id a position taken off this account's feed had (retired_ad), its saved ad kept or not (taken off before 2026-10-29)."""
    row = conn.execute("SELECT ad_id FROM retired_ad WHERE account_id = ? AND goods_id = ?", (account_id, goods_id)).fetchone()
    return row[0] if row and row[0] else None


def manual_of(conn, codes) -> Dict[str, int]:
    """code → its manual pieces (no test point), for the codes given."""
    from .oth_stock import manual_rows
    wanted, found = set(codes), {}
    for r in manual_rows(conn):
        if r["goods_id"] in wanted:
            found[r["goods_id"]] = found.get(r["goods_id"], 0) + int(r["qty"])
    return found


def note_no_price(conn, account_id: int, found: Dict[str, int], now: str, kind: str = "price") -> None:
    """The cycle of an account did not send «Свой остаток» of gone positions (`kind` "price": no own price; "id": never published in this
    account, so no Id of its own): a line of the log and the journal per position newly so (the owner sees why the ad did not come back);
    a position that got it or lost its rows is forgotten."""
    from . import store
    key_of, text, action, why = _NOT_SENT[kind]
    key = key_of % int(account_id)
    try:
        before = set(json.loads(store.get_state(conn, key) or "[]"))
    except (TypeError, ValueError):
        before = set()
    from .connections import display_name
    from .oth_stock import manual_rows
    for code in sorted(set(found) - before):
        for row in (r for r in manual_rows(conn) if r["goods_id"] == code):
            _log(conn, code, row["point_id"], row["qty"], row["qty"], now, "cycle", "%s: %s" % (display_name(conn, int(account_id)), text),
                 "%s:%d" % (why, int(account_id)))
        _audit(conn, now, "cycle", action, {"account": int(account_id), "goods_id": code, "qty": int(found[code])})
    if set(found) != before:
        conn.execute("INSERT OR REPLACE INTO app_state (key, value) VALUES (?, ?)", (key, json.dumps(sorted(found))))
    conn.commit()


CARRIED_KEY = "oth_manual_carried:%d"           # app_state: {code: the Id of the ad of the same tire that shows its manual pieces} in the published feed
CARRIED_NEXT_KEY = "oth_manual_carried_next:%d"  # app_state: {"sha": the feed the last cycle built, "ids": the same in that feed}


def _feed_sha(feed_bytes: bytes) -> str:
    return hashlib.sha256(feed_bytes).hexdigest()


def _set(conn, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO app_state (key, value) VALUES (?, ?)", (key, value))


def _next(conn, account_id: int) -> Optional[dict]:
    from . import store
    try:
        found = json.loads(store.get_state(conn, CARRIED_NEXT_KEY % int(account_id)) or "null")
    except (TypeError, ValueError):
        return None
    return found if isinstance(found, dict) and isinstance(found.get("ids"), dict) else None


def remember_carried(conn, account_id: int, carried: Dict[str, str], feed_bytes: bytes) -> None:
    """What the feed a cycle just built does with «Свой остаток» of gone lots handed to another lot of the same tire (its Ids as the feed has
    them). Not on Avito yet: publish_carried, when this very feed is published. The feed itself never reads it."""
    _set(conn, CARRIED_NEXT_KEY % int(account_id), json.dumps({"sha": _feed_sha(feed_bytes), "ids": dict(sorted(carried.items()))}, ensure_ascii=False))
    conn.commit()


def rebind_carried(conn, account_id: int, feed_bytes: bytes) -> None:
    """A mirror added the file's ads to the feed of the cycle: the same plan, the larger feed (store.rebind_pending). No commit."""
    found = _next(conn, account_id)
    if found is not None:
        _set(conn, CARRIED_NEXT_KEY % int(account_id), json.dumps(dict(found, sha=_feed_sha(feed_bytes)), ensure_ascii=False))


def publish_carried(conn, account_id: int, published_feed: bytes) -> None:
    """The feed went out (store.commit_pending): if it is the one the last cycle built, what that cycle remembered is what the feed shows now.
    The light stock pass publishes a feed of its own, which leaves it as it was. No commit."""
    found = _next(conn, account_id)
    if found is not None and found.get("sha") == _feed_sha(published_feed):
        _set(conn, CARRIED_KEY % int(account_id), json.dumps(found["ids"], ensure_ascii=False))


def carried_by(conn, account_id: int) -> Dict[str, str]:
    """{code: the Id} in the feed published now."""
    from . import store
    try:
        found = json.loads(store.get_state(conn, CARRIED_KEY % int(account_id)) or "{}")
    except (TypeError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def carried_next(conn, account_id: int) -> Dict[str, str]:
    """{code: the Id} in the feed of the last cycle; no such cycle yet: as published."""
    found = _next(conn, account_id)
    return found["ids"] if found is not None else carried_by(conn, account_id)
