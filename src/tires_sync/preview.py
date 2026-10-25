from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Set, Tuple

from .candidates import evaluate
from .decide import decide
from .models import Action, PublishedItem, Settings
from .webmim import ParseResult

_LIVE = {Action.PUBLISH, Action.KEEP, Action.UPDATE, Action.HOLD}


@dataclass
class Preview:
    old_counts: Dict[str, int]
    new_counts: Dict[str, int]
    appear: List[str]
    disappear: List[str]
    price_changes: List[Tuple[str, int, int]]


def _run(parsed, ref, confirmed, published, suppressed, known_points, settings, today, brand_map=None, now=None):
    ev = evaluate(parsed.offers, parsed.issues, settings, ref, confirmed, brand_map)
    for gid, stock in getattr(parsed, "stocks", {}).items():            # a broken row with a known stock: below the threshold the cycle takes it off
        ev.stocks.setdefault(gid, stock)
    return decide(ev, dict(published), set(suppressed), dict(known_points), settings, today, now=now)


def preview_change(
    parsed: ParseResult,
    ref,
    confirmed,
    published: Dict[str, PublishedItem],
    suppressed: Set[str],
    known_points: Dict[str, str],
    old: Settings,
    new: Settings,
    today: date,
    brand_map=None,
    now=None,
) -> Preview:
    old_ds = _run(parsed, ref, confirmed, published, suppressed, known_points, old, today, brand_map, now)
    new_ds = _run(parsed, ref, confirmed, published, suppressed, known_points, new, today, brand_map, now)
    old_by = {d.goods_id: d for d in old_ds.decisions}
    new_by = {d.goods_id: d for d in new_ds.decisions}
    old_live = {g for g, d in old_by.items() if d.action in _LIVE}
    new_live = {g for g, d in new_by.items() if d.action in _LIVE}
    changes = sorted(
        (g, old_by[g].price, new_by[g].price)
        for g in old_live & new_live
        if old_by[g].price is not None and new_by[g].price is not None and old_by[g].price != new_by[g].price
    )
    return Preview(
        dict(Counter(d.action.value for d in old_ds.decisions)),
        dict(Counter(d.action.value for d in new_ds.decisions)),
        sorted(new_live - old_live),
        sorted(old_live - new_live),
        changes,
    )
