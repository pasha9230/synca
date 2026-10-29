"""«На ОТХ» in the feed (the owner 2026-10-29): the tires of an order refused by its client lie at the point, they are ours. While the order
stands in «На ОТХ», the ad of that position, in every account of ours, starts with «Шины в наличии на <точка>», shows the pieces on OTH plus
the supplier's stock, is not taken off for the supplier's stock and is published again if it is not on Avito (decide, sync.plan_decisions);
an excluded brand or a season switched off do not stop it (candidates.evaluate). When the order leaves the stage, or the whole position is
marked «возвращено поставщику», or its return is agreed with the supplier, the next cycle applies the usual rules. Orders at a test point
give nothing. Only reads the orders."""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Dict, Iterable, Tuple

FIRST_LINE = "Шины в наличии"
MAX_DESCRIPTION = 7500                                    # feed.validate_ad: a longer description is refused by Avito
TEST_POINTS_KEY = "oth_test_points"                       # state: JSON list of the ids of test points, besides the names that start with «тест»
_LINE = re.compile(r"Шины в наличии(?: на [^\n]*)?\n")
_COUNT = re.compile(r"(В наличии:\s*)\d+(\s*шт)")


def _test_points(conn) -> frozenset:
    """The points whose orders are tests: a name that starts with «тест» (any case), or an id the state lists."""
    found = {row[0] for row in conn.execute("SELECT id, name FROM point") if (row[1] or "").strip().casefold().startswith("тест")}
    from .store import get_state
    try:
        found |= {int(i) for i in json.loads(get_state(conn, TEST_POINTS_KEY) or "[]")}
    except (TypeError, ValueError):
        pass
    return frozenset(found)


def _rows(conn) -> list:
    """The positions still ours on «На ОТХ» (order_oth.stock), without the test points, the tyres of a point that have no code and the orders
    whose return is agreed with the supplier (order_oth.agree, the owner 2026-10-29: they leave the ad at once, before the act МХ-3; an
    agreement cancelled — its row gone — counts them again)."""
    from . import order_oth, orders
    try:
        rows = order_oth.stock(conn)["stock"]
        test = _test_points(conn)
        agreed = {r[0] for r in conn.execute("SELECT order_id FROM oth_return_plan")}
    except sqlite3.OperationalError:                      # a base without the orders' tables
        return []
    return [r for r in rows if r["goods_id"] and r["goods_id"] != orders.POINT_GOODS and r["point_id"] not in test and int(r["qty"]) > 0
            and r["order_id"] not in agreed]


def by_point(conn) -> Dict[Tuple[str, int], int]:
    """(goods_id, point id) → the pieces of it on «На ОТХ» at that point, not returned to the supplier: what a new order can take from OTH."""
    found: Dict[Tuple[str, int], int] = {}
    for r in _rows(conn):
        key = (r["goods_id"], r["point_id"])
        found[key] = found.get(key, 0) + int(r["qty"])
    return found


def pieces(conn) -> Dict[str, int]:
    """goods_id → the pieces of it on «На ОТХ» (all points), not returned to the supplier."""
    found: Dict[str, int] = {}
    for r in _rows(conn):
        found[r["goods_id"]] = found.get(r["goods_id"], 0) + int(r["qty"])
    return found


def points(conn) -> Dict[str, Tuple[str, ...]]:
    """goods_id → the names of the points it lies at on «На ОТХ», as the clients see them, in alphabetical order."""
    found: Dict[str, set] = {}
    for r in _rows(conn):
        if (r["point"] or "").strip():
            found.setdefault(r["goods_id"], set()).add(r["point"].strip())
    return {code: tuple(sorted(names)) for code, names in found.items()}


def line_of(names: Iterable[str]) -> str:
    named = sorted({n.strip() for n in names or () if n and n.strip()})
    return "%s на %s" % (FIRST_LINE, ", ".join(named)) if named else FIRST_LINE


def add_line(text: str, names: Iterable[str] = ()) -> str:
    """The description with «Шины в наличии на <точки>» as its first line (once); left whole when the line would make it too long for Avito."""
    text = drop_line(text or "")
    marked = line_of(names) + "\n" + text
    return marked if len(marked) <= MAX_DESCRIPTION else text


def drop_line(text: str) -> str:
    match = _LINE.match(text or "")
    return text[match.end():] if match else (text or "")


def with_count(text: str, count: int) -> str:
    """«В наличии: N шт.» of a saved ad with `count`, when the text says it exactly once; otherwise as it is (a template of the owner's own)."""
    if len(_COUNT.findall(text or "")) != 1:
        return text
    return _COUNT.sub(lambda m: "%s%d%s" % (m.group(1), count, m.group(2)), text)
