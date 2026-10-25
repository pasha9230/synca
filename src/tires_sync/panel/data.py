from __future__ import annotations

import hashlib
import os
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Dict, FrozenSet

from ..catalog import parse_reference
from .. import connections, suppliers, tochki_feed
from ..suppliers import unload_path_for
from ..webmim import ParseResult, merge_parsed, parse_unload

_cache: dict = {}


def _mtime(path):
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def _remember(kind: str, path, stamp, value, extra=()):
    for key in [k for k in _cache if k[0] == kind and k[1] == str(path)]:
        del _cache[key]
    _cache[(kind, str(path), stamp) + tuple(extra)] = value
    return value


def load_reference(path) -> Dict[str, FrozenSet[str]]:
    stamp = _mtime(path)
    if stamp is None:
        return {}
    key = ("ref", str(path), stamp)
    if key in _cache:
        return _cache[key]
    try:
        with open(path, encoding="utf-8") as handle:
            return _remember("ref", path, stamp, parse_reference(handle.read()))
    except (OSError, UnicodeDecodeError):
        return {}


def _unavailable(supplier: int) -> ParseResult:
    result = ParseResult()
    result.unavailable.add(supplier)
    return result


def load_parsed(path, territory: str, supplier: int = 1) -> ParseResult:
    stamp = _mtime(path)
    if stamp is None:
        return _unavailable(supplier)
    key = ("xml", str(path), stamp, territory, supplier)
    if key in _cache:
        return _cache[key]
    try:
        with open(path, "rb") as handle:
            parsed = parse_unload(handle.read(), territory, supplier)
    except (ET.ParseError, OSError):
        parsed = _unavailable(supplier)
    if not parsed.offers:
        parsed.unavailable.add(supplier)
    return _remember("xml", path, stamp, parsed, (territory, supplier))


def _tochki_stamp(base, sid: int, settings, now):
    """What the 4tochki part of `load_merged` depends on: the snapshot file and whether the cycle takes it as old now. A load time with a
    zone (a file put by hand) is compared in UTC by `suppliers.file_is_stale`, so no time can fail the comparison."""
    path = tochki_feed.paths(base)["snapshot"]
    stamp = _mtime(path)
    loaded = tochki_feed.load_snapshot(base, sid) if stamp is not None else None
    try:
        fetched = datetime.fromisoformat(loaded[1]["fetched_at"]) if loaded is not None else None
    except (TypeError, ValueError):
        fetched = None
    return stamp, fetched is None or suppliers.file_is_stale(fetched, now, settings.stale_days)


def _tochki_part(base, sid: int, name: str, settings, now) -> ParseResult:
    """The 4tochki positions exactly as the cycle takes them with «в фид» on (`suppliers._add_tochki`)."""
    parts = []
    suppliers._add_tochki(parts, [], hashlib.sha256(), sid, name, base, settings, now)
    return parts[0]


def _held(part: ParseResult, sid: int) -> ParseResult:
    """A stale secondary unload as the cycle takes it (`suppliers.load_all_parsed`): no positions nor prices, only the last known stock,
    and the supplier unavailable, so its published ads are held. A new object: the cached parse of the file stays as it is."""
    held = suppliers._stocks_only(part)
    held.unavailable.add(sid)
    return held


def _stale_at(part: ParseResult, path, sid: int, now, settings):
    """The load time of a secondary WebMIM unload that the cycle holds as old (`suppliers.load_all_parsed`: a file with positions of its
    territory, `suppliers.stale_load`), else None. `load_merged` and `stale_unload` decide with this one function."""
    if sid == 1 or not part.offers:
        return None
    return suppliers.stale_load(path, now, settings.stale_days)


def stale_unload(conn, settings, base, sid: int, now=None):
    """(the raw parse of the file, its load time) of a secondary WebMIM unload held as old (see `load_merged`), else None. For viewing
    only (settings lists, the catalog's view, the photo of a held ad): its positions never go into the set the panel publishes from.
    The parse is the cached object: callers read it, never change it."""
    tochki = tochki_feed.config(conn)
    if sid == 1 or (tochki["feed"] and sid == tochki["supplier_id"]):
        return None
    path = unload_path_for(base, sid)
    try:
        territory = connections.supplier_territory(conn, sid, settings)
    except ValueError:
        return None
    if _mtime(path) is None:
        return None
    part = load_parsed(path, territory, sid)
    fetched = _stale_at(part, path, sid, now or datetime.utcnow(), settings)
    return (part, fetched) if fetched is not None else None


def load_merged(conn, settings, base) -> ParseResult:
    """The positions of every supplier in one set (cached until one of the files changes or a load grows old). As in the cycle
    (`suppliers.load_all_parsed`), so that the panel's publication, photos and catalog see what the feed has: a secondary WebMIM unload
    older than the cycle takes (`suppliers.stale_load`) gives only its last known stock; 4tochki with its switch «в фид» on comes from
    its own load; with the switch off nothing of it is here (the catalog adds it for viewing, `tochki_feed.catalog_view`)."""
    parts, stamps = [], []
    tochki = tochki_feed.config(conn)
    now = datetime.utcnow()
    for supplier in connections.list_suppliers(conn):
        sid = supplier["id"]
        if tochki["feed"] and sid == tochki["supplier_id"]:
            stamp = _tochki_stamp(base, sid, settings, now)
            parts.append(lambda sid=sid, name=supplier["name"]: _tochki_part(base, sid, name, settings, now))   # built only on a miss
            stamps.append((sid, "tochki") + stamp)
            continue
        path = unload_path_for(base, sid)
        try:
            territory = connections.supplier_territory(conn, sid, settings)
        except ValueError:
            parts.append(_unavailable(sid))
            stamps.append((sid, "", None))
            continue
        stamp = _mtime(path)
        if stamp is None:
            parts.append(_unavailable(sid))
            stamps.append((sid, territory, None))
            continue
        part = load_parsed(path, territory, sid)
        stale = _stale_at(part, path, sid, now, settings) is not None
        stamps.append((sid, territory, stamp, stale))       # the clock alone makes a file old: the cached set follows it
        parts.append((lambda part=part, sid=sid: _held(part, sid)) if stale else part)
    key = ("merged", str(base), tuple(stamps))
    found = _cache.get(key)                       # the panel serves several requests at once: read the cache once, never twice
    if found is None:
        parts = [p() if callable(p) else p for p in parts]
        found = merge_parsed(parts) if len(parts) != 1 else parts[0]
        for old in [k for k in list(_cache) if k[0] == "merged" and k[1] == str(base)]:
            _cache.pop(old, None)
        _cache[key] = found
    return found
