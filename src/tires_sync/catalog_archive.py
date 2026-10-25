"""The permanent base of catalog positions. The unload of the supplier shows only what he has today; a position he took away must not vanish from our
catalog with its name, size, photo and price. Every cycle writes what the unload has and marks what it no longer has. Display only: the feed, the prices
and the decisions to publish or take off never read this table."""
from __future__ import annotations

import dataclasses
import json
import re
from typing import List, Tuple

from . import store

from .models import Offer, Season

MIN_KNOWN_FOR_GUARD = 20          # a supplier with fewer known positions is judged by "nothing came" only
MAX_DROP = 0.5                    # more than this share of a supplier's known positions gone at once is not believed...
BELIEVE_AFTER = 18                # ...until it has been so in this many cycles in a row (the end of a season, a renumbering): then they are marked;
                                  # about 18 hours with the cycle every hour (2026-10-20; it was 3 cycles of 6 hours)


def _supplier(goods_id: str) -> int:
    match = re.match(r"s(\d+)-", goods_id or "")
    return int(match.group(1)) if match else 1


def _dump(offer: Offer) -> str:
    data = dataclasses.asdict(offer)
    data["season"] = offer.season.value
    return json.dumps(data, ensure_ascii=False)


def _load(text: str) -> Offer:
    data = json.loads(text)
    data["season"] = Season(data["season"])
    known = {f.name for f in dataclasses.fields(Offer)}
    return Offer(**{k: v for k, v in data.items() if k in known})


def _set_streak(conn, key: str, value: int) -> None:
    conn.execute("INSERT OR REPLACE INTO app_state VALUES (?, ?)", (key, str(value)))        # no commit of its own: record() commits once at the end


def record(conn, parsed, now: str) -> dict:
    """Write the positions of the unload and mark those that are no longer in it. Returns counts and the suppliers whose drop was not believed."""
    stats = {"seen": 0, "gone": 0, "returned": 0, "suspicious": [], "believed": [], "streak": {}}
    returned = {r[0] for r in conn.execute("SELECT goods_id FROM catalog_item WHERE gone_since != ''")} & set(parsed.offers)
    for goods_id, offer in parsed.offers.items():
        conn.execute(
            "INSERT INTO catalog_item (goods_id, supplier_id, data, first_seen, last_seen, last_stock, last_price, gone_since) VALUES (?,?,?,?,?,?,?, '') "
            "ON CONFLICT(goods_id) DO UPDATE SET data = excluded.data, last_seen = excluded.last_seen, last_stock = excluded.last_stock, "
            "last_price = excluded.last_price, gone_since = ''",
            (goods_id, _supplier(goods_id), _dump(offer), now, now, int(offer.stock), float(offer.base_price)))
        stats["seen"] += 1
    stats["returned"] = len(returned)
    present_now = {}
    for goods_id in parsed.offers:
        present_now.setdefault(_supplier(goods_id), set()).add(goods_id)
    known = {}
    for goods_id, supplier in conn.execute("SELECT goods_id, supplier_id FROM catalog_item WHERE gone_since = ''"):
        known.setdefault(supplier, set()).add(goods_id)
    for supplier, ids in known.items():
        if supplier in getattr(parsed, "unavailable", ()):
            continue                                          # his unload was not usable this time: nobody is marked
        key = "catalog_archive_suspect_%d" % supplier
        missing = ids - set(parsed.offers) - set(getattr(parsed, "issues", {}))      # a position the supplier still sends but with a broken field is not gone
        if not missing:
            _set_streak(conn, key, 0)
            continue
        if not present_now.get(supplier):
            continue                                          # nothing of this supplier came at all: a broken file, not an empty shop
        if len(ids) >= MIN_KNOWN_FOR_GUARD and len(missing) / len(ids) > MAX_DROP:
            streak = int(store.get_state(conn, key) or "0") + 1
            if streak < BELIEVE_AFTER:
                _set_streak(conn, key, streak)
                stats["suspicious"].append(supplier)
                stats["streak"][supplier] = streak
                continue
            stats["believed"].append(supplier)                # the same collapse in several cycles in a row: it is real
        _set_streak(conn, key, 0)
        conn.executemany("UPDATE catalog_item SET gone_since = ? WHERE goods_id = ? AND gone_since = ''", [(now, g) for g in missing])
        stats["gone"] += len(missing)
    conn.commit()
    return stats


def gone_offer(conn, goods_id: str):
    """One position the supplier took away, by its id (the offer as it was, stock 0), or None."""
    row = conn.execute("SELECT data FROM catalog_item WHERE goods_id = ? AND gone_since != ''", (goods_id,)).fetchone()
    if row is None:
        return None
    try:
        return dataclasses.replace(_load(row[0]), stock=0)
    except Exception:
        return None


def gone_offers(conn) -> List[Tuple[Offer, dict]]:
    """The positions the supplier took away: the offer as it was (stock 0) and what is known about it."""
    out = []
    for data, first, last, stock, price, gone in conn.execute(
            "SELECT data, first_seen, last_seen, last_stock, last_price, gone_since FROM catalog_item WHERE gone_since != '' ORDER BY goods_id"):
        try:
            offer = dataclasses.replace(_load(data), stock=0)
        except Exception:                                    # a row written by an older shape of Offer must not take the catalog page down
            continue
        out.append((offer, {"gone_since": gone, "last_stock": stock, "last_price": price, "last_seen": last, "first_seen": first}))
    return out
