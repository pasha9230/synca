"""Daily collection of Avito statistics (views, contacts, favourites) for our ads, kept by day: the base of the card funnel."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from . import connections, store

STATS_CHUNK = 200       # ads per statistics request
MAPPING_CHUNK = 50      # ads per request that maps an Avito id to our ad id
KEEP_DAYS = 400
LOOKUP_PAUSE = 300      # seconds without single lookups of an account after Avito answered 429 (AvitoError keeps no Retry-After)


def _chunks(values: List, size: int):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def collect(conn, client, account_id: int, today: date, days: int = 7, now_iso: str = "", stats: bool = True) -> Dict[str, int]:
    """Refresh the list of our ads and the statistics of the last `days` days (the window overlaps on purpose: Avito corrects recent days).
    `stats=False`: only the list and the mapping (the runs between the daily one: an ad published in the morning is known within hours).
    The list is always read whole: avito_stock takes the rows of the newest seen_at as what the cabinet shows now."""
    if not 1 <= days <= 270:
        raise ValueError("days must be between 1 and 270")
    user_id = connections.cabinet_user_id(conn, client, account_id) if stats else None
    items = [item for item in client.items("active") if item.get("id")]
    known = {row[0]: row[1] for row in conn.execute("SELECT avito_id, ad_id FROM avito_ad WHERE account_id = ?", (account_id,))}
    for item in items:
        conn.execute(
            "INSERT INTO avito_ad (account_id, avito_id, ad_id, title, price, status, seen_at, first_seen) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(account_id, avito_id) DO UPDATE SET title = excluded.title, price = excluded.price, "
            "status = excluded.status, seen_at = excluded.seen_at",
            (account_id, item["id"], known.get(item["id"]), str(item.get("title") or "")[:200],
             int(item["price"]) if isinstance(item.get("price"), (int, float)) else None, str(item.get("status") or ""), now_iso, now_iso))
    unmapped = [item["id"] for item in items if not known.get(item["id"])]
    mapped = 0
    for chunk in _chunks(unmapped, MAPPING_CHUNK):
        for avito_id, ad_id in client.ad_ids_for(chunk).items():
            if ad_id:
                conn.execute("UPDATE avito_ad SET ad_id = ? WHERE account_id = ? AND avito_id = ?", (str(ad_id), account_id, avito_id))
                mapped += 1
    rows = 0
    conn.commit()                                                         # the list stands even when the statistics fail below
    if not stats:
        return {"ads": len(items), "mapped": mapped, "days": 0}
    first, last = (today - timedelta(days=days - 1)).isoformat(), today.isoformat()
    for chunk in _chunks([item["id"] for item in items], STATS_CHUNK):
        for entry in client.item_stats(user_id, chunk, first, last):
            for stat in entry.get("stats") or []:
                conn.execute(
                    "INSERT OR REPLACE INTO ad_stat (account_id, avito_id, day, views, contacts, favorites) VALUES (?,?,?,?,?,?)",
                    (account_id, entry["itemId"], str(stat["date"])[:10], int(stat.get("uniqViews") or 0),
                     int(stat.get("uniqContacts") or 0), int(stat.get("uniqFavorites") or 0)))
                rows += 1
    conn.execute("DELETE FROM ad_stat WHERE account_id = ? AND day < ?", (account_id, (today - timedelta(days=KEEP_DAYS)).isoformat()))
    conn.commit()
    return {"ads": len(items), "mapped": mapped, "days": rows}


def _pause_key(account_id: int) -> str:
    return "ad_lookup_pause:%d" % account_id


def lookup_one(conn, client, account_id: int, avito_id, title: str, now: datetime) -> Optional[str]:
    """Our Id of one ad the list does not know yet (published after the last reading of the cabinet): one request, never repeated, and kept
    in avito_ad. None: Avito does not call it ours. Raises AvitoError / OSError (and RateLimited while the pause after a 429 lasts).
    The row is not a reading of the cabinet: no status, and the seen_at of the last reading (avito_stock and the funnel take the newest
    seen_at for the whole cabinet and status 'active'); the next reading fills the rest and keeps the Id."""
    from .avito import RateLimited
    avito_id = int(avito_id)
    until = store.get_state(conn, _pause_key(account_id))
    if until and until > now.isoformat(timespec="seconds"):
        raise RateLimited(429, None, "пауза после ответа 429 до %s" % until[11:16])
    try:
        ad_id = client.ad_ids_for([avito_id], retry=False).get(avito_id)
    except RateLimited:
        store.set_state(conn, _pause_key(account_id), (now + timedelta(seconds=LOOKUP_PAUSE)).isoformat(timespec="seconds"))
        raise
    if not ad_id:
        return None
    stamp = now.isoformat(timespec="seconds")
    last = conn.execute("SELECT MAX(seen_at) FROM avito_ad WHERE account_id = ?", (account_id,)).fetchone()[0]
    conn.execute(
        "INSERT INTO avito_ad (account_id, avito_id, ad_id, title, price, status, seen_at, first_seen) VALUES (?,?,?,?,NULL,'',?,?) "
        "ON CONFLICT(account_id, avito_id) DO UPDATE SET ad_id = excluded.ad_id WHERE avito_ad.ad_id IS NULL OR avito_ad.ad_id = ''",
        (account_id, avito_id, str(ad_id), str(title or "")[:200], last or stamp, stamp))
    conn.commit()
    return str(ad_id)


def totals(conn, account_id: int, since: str) -> Dict[str, int]:
    row = conn.execute(
        "SELECT COALESCE(SUM(views),0), COALESCE(SUM(contacts),0), COALESCE(SUM(favorites),0) FROM ad_stat WHERE account_id = ? AND day >= ?",
        (account_id, since)).fetchone()
    return {"views": row[0], "contacts": row[1], "favorites": row[2]}
