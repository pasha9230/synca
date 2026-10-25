"""Several suppliers: where each unload lives and how they are merged into one set of positions."""
from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Tuple

from . import connections, tochki_feed
from .webmim import ParseResult, merge_parsed, parse_unload


def file_is_stale(fetched, now, stale_days: int) -> bool:
    """A secondary supplier's file is old after the wait before taking an ad off, but never sooner than a day: zero there means «at once», not «always».
    The loads write UTC without a zone; a time with a zone (a file put by hand) is taken in UTC without it, so the two always compare."""
    def utc(moment):
        return moment.astimezone(timezone.utc).replace(tzinfo=None) if moment.tzinfo is not None else moment
    return utc(now) - utc(fetched) > timedelta(days=max(1, stale_days))

def unload_path_for(base, supplier_id: int) -> Path:
    base = Path(base)
    if supplier_id == 1:
        return base
    return base.with_name("%s-%d%s" % (base.stem, supplier_id, base.suffix))


SECONDARY_ID = re.compile(r"s(\d+)-")


def supplier_of_id(goods_id: str) -> int:
    match = SECONDARY_ID.match(goods_id)
    return int(match.group(1)) if match else 1


def _fetched_at(path: Path):
    try:
        raw = json.loads(Path(str(path) + ".meta.json").read_text(encoding="utf-8")).get("fetched_at")
        return datetime.fromisoformat(raw) if raw else None
    except (OSError, ValueError):
        return None


def stale_load(path: Path, now, stale_days: int):
    """The load time of a secondary supplier's unload (`<file>.meta.json`) when it is old (`file_is_stale`), else None (fresh, or no
    time known: such a file is used). The cycle and the panel (`panel.data.load_merged`) decide with this one function."""
    fetched = _fetched_at(path)
    return fetched if fetched is not None and file_is_stale(fetched, now, stale_days) else None


def _stocks_only(part: ParseResult) -> ParseResult:
    """A stale unload gives neither positions nor prices, only the last known stock: a position known to have fallen below the threshold
    is not held on it (an unknown or sufficient stock is held, as before)."""
    stocks = dict(getattr(part, "stocks", {}) or {})
    stocks.update({gid: offer.stock for gid, offer in part.offers.items()})
    return ParseResult(stocks=stocks)


def load_all_parsed(conn, settings, base, now=None) -> Tuple[ParseResult, str, List[str]]:
    """Parse the unload of every supplier. The first one is required (any failure raises, as before). A secondary
    supplier that cannot be used (no file, broken, wrong territory, stale) is reported in the notes and marked
    unavailable, so that its published positions are held instead of being taken off."""
    parts, notes = [], []
    digest = hashlib.sha256()
    now = now or datetime.utcnow()
    tochki = tochki_feed.config(conn)
    for supplier in connections.list_suppliers(conn):
        sid = supplier["id"]
        name = supplier["name"]
        if tochki["feed"] and sid == tochki["supplier_id"]:          # 4tochki goes into the feeds only with its switch on (stage 2)
            _add_tochki(parts, notes, digest, sid, name, base, settings, now)
            continue
        path = unload_path_for(base, sid)
        configured = bool(connections.get_config(conn, "webmim", connections.supplier_owner(sid)).get("base_url"))

        def fail(note, part=None):
            marked = part or ParseResult()
            marked.unavailable.add(sid)
            parts.append(marked)
            if configured:
                notes.append("Поставщик «%s» не используется в этом цикле: %s" % (name, note))

        if sid != 1 and not path.exists():
            fail("нет файла выгрузки")
            continue
        try:
            territory = connections.supplier_territory(conn, sid, settings)
        except ValueError as err:
            if sid == 1:
                raise
            fail(str(err).rstrip(".").replace("У поставщика не задана территория выгрузки", "не задана территория выгрузки"))
            continue
        data = path.read_bytes()
        digest.update(data)
        try:
            part = parse_unload(data, territory, supplier=sid)
        except ET.ParseError:
            if sid == 1:
                raise
            fail("файл выгрузки повреждён")
            continue
        if sid != 1:
            if not part.offers:
                fail("в выгрузке нет позиций территории %s" % territory, part)
                continue
            fetched = stale_load(path, now, settings.stale_days)
            if fetched is not None:
                fail("выгрузка устарела (скачана %s)" % fetched.isoformat(timespec="minutes"), _stocks_only(part))
                continue
        elif not part.offers:
            part.unavailable.add(sid)
            notes.append("Поставщик «%s» не используется в этом цикле: в выгрузке нет позиций территории %s" % (name, territory))
        parts.append(part)
    return merge_parsed(parts), digest.hexdigest(), notes


def _add_tochki(parts, notes, digest, sid: int, name: str, base, settings, now) -> None:
    """The last good 4tochki load as the positions of supplier `sid`; none, empty or old: the supplier is unavailable and its ads are held."""
    loaded = tochki_feed.load_snapshot(base, sid)
    fetched = None
    if loaded is not None:
        try:
            fetched = datetime.fromisoformat(loaded[1]["fetched_at"])
        except (TypeError, ValueError):
            fetched = None
    if loaded is None or not loaded[0].offers:
        note = "нет загруженных позиций 4 точек"
    elif fetched is None or file_is_stale(fetched, now, settings.stale_days):
        note = "загрузка 4 точек устарела (%s)" % (fetched.isoformat(timespec="minutes") if fetched else "дата неизвестна")
    else:
        digest.update(tochki_feed.paths(base)["snapshot"].read_bytes())
        parts.append(ParseResult(offers=dict(loaded[0].offers), issues={g: list(v) for g, v in loaded[0].issues.items()},
                                 stocks=dict(getattr(loaded[0], "stocks", {}) or {})))
        return
    marked = _stocks_only(loaded[0]) if loaded is not None else ParseResult()
    marked.unavailable.add(sid)
    parts.append(marked)
    notes.append("Поставщик «%s» не используется в этом цикле: %s" % (name, note))
