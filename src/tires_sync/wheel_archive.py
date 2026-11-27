"""Last known WebMIM wheel data, separate from tire goods IDs."""
from __future__ import annotations

from dataclasses import asdict, replace
from decimal import Decimal, InvalidOperation
import json
from . import store
from .webmim_wheels import Wheel

KEY = 'wheel_catalog_archive_v1'


def load(conn):
    try:
        raw = json.loads(store.get_state(conn, KEY) or '{}')
        if not isinstance(raw, dict):
            return {}
        result = {}
        for gid, data in raw.items():
            try:
                item = dict(data)
                item['price'] = Decimal(item['price'])
                item.pop('last_seen', None)
                wheel = Wheel(**item)
                if wheel.goods_id == gid and wheel.price.is_finite():
                    result[gid] = wheel
            except (ValueError, TypeError, KeyError, InvalidOperation):
                continue
        return result
    except (TypeError, ValueError):
        return {}


def remember(conn, offers, seen, *, only_missing=False):
    if not offers:
        return
    # Reserve the SQLite writer before reading: the timer and an edit can run
    # concurrently and must not overwrite each other's archive entries.
    conn.execute('SAVEPOINT wheel_archive_update')
    try:
        conn.execute("UPDATE app_state SET value=value WHERE key=?", (KEY,))
        try:
            raw = json.loads(store.get_state(conn, KEY) or '{}')
            if not isinstance(raw, dict):
                raise ValueError('Архив дисков повреждён')
        except (TypeError, ValueError):
            raise ValueError('Архив дисков повреждён') from None
        for gid, wheel in offers.items():
            if only_missing and gid in raw:
                continue
            item = asdict(wheel)
            item['price'] = str(wheel.price)
            item['last_seen'] = seen
            raw[gid] = item
        conn.execute('INSERT OR REPLACE INTO app_state (key, value) VALUES (?, ?)',
                     (KEY, json.dumps(raw, ensure_ascii=False, separators=(',', ':'))))
        conn.execute('RELEASE SAVEPOINT wheel_archive_update')
    except Exception:
        conn.execute('ROLLBACK TO SAVEPOINT wheel_archive_update')
        conn.execute('RELEASE SAVEPOINT wheel_archive_update')
        raise


def merged(conn, current, territory):
    archive = load(conn)
    current_offers = current.offers if current else {}
    for gid, wheel in archive.items():
        if wheel.territory == territory and gid not in current_offers:
            current_offers = dict(current_offers)
            current_offers[gid] = replace(wheel, stock=0)
    return current_offers
