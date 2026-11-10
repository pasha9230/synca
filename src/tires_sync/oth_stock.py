"""«На ОТХ» in the feed (the owner 2026-10-29): the tires of an order refused by its client lie at the point, they are ours. While the order
stands in «На ОТХ» (the owner 2026-11-04: not on «На возврат»), the ad of that position, in every account of ours, starts with «Шины в наличии на Юноне, в Янино» (the setting «Где» of each point; the owner 2026-11-02), shows the pieces on OTH plus
the supplier's stock, is not taken off for the supplier's stock and is published again if it is not on Avito (decide, sync.plan_decisions);
an excluded brand or a season switched off do not stop it (candidates.evaluate). When the order leaves the stage, or the whole position is
marked «возвращено поставщику», or its return is agreed with the supplier, the next cycle applies the usual rules. Orders at a test point
give nothing. «Свой остаток (ОТХ)» entered by hand in the catalog (oth_manual, the owner 2026-11-03) counts the same way. Only reads."""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Dict, Iterable, Tuple

FIRST_LINE = "Шины в наличии"
MAX_DESCRIPTION = 7500                                    # feed.validate_ad: a longer description is refused by Avito
TEST_POINTS_KEY = "oth_test_points"                       # state: JSON list of the ids of test points, besides the names that start with «тест»
_LINE = re.compile(r"Шины в наличии(?: (?:на|во?) [^\n]*)?\n")
WHERE = re.compile(r"(?:на|во?) [^,\n]*[^,\s]")              # «Где (для описания)» of a point: what the line says and drop_line takes back
_COUNT = re.compile(r"(В наличии:\s*)(?:более\s+)?\d+(\s*шт)")      # a count of {stock}: «6» or «более 4» (description.stock_text)


def _test_points(conn) -> frozenset:
    """The points whose orders are tests: a name that starts with «тест» (any case), or an id the state lists."""
    found = {row[0] for row in conn.execute("SELECT id, name FROM point") if (row[1] or "").strip().casefold().startswith("тест")}
    from .store import get_state
    try:
        found |= {int(i) for i in json.loads(get_state(conn, TEST_POINTS_KEY) or "[]")}
    except (TypeError, ValueError):
        pass
    return frozenset(found)


def _order_rows(conn) -> list:
    """The positions still ours on «На ОТХ» (order_oth.stock), without the test points, the tyres of a point that have no code, the orders on
    «На возврат» in any label (the owner 2026-11-04: from «Согласуем», an agreement cancelled too) and the orders on «На ОТХ» whose return is agreed
    with the supplier (the orders of before the stage came)."""
    from . import order_oth, orders
    try:
        rows = order_oth.stock(conn)["stock"]
        test = _test_points(conn)
        agreed = {r[0] for r in conn.execute("SELECT order_id FROM oth_return_plan")}
    except sqlite3.OperationalError:                      # a base without the orders' tables
        return []
    rows = [dict(r, qty=int(r["qty"]) - int(r.get("marked_qty") or 0)) for r in rows]     # the owner 2026-11-10: the pieces of a mark «На возврат»
    return [dict(r, manual=None) for r in rows if r["goods_id"] and r["goods_id"] != orders.POINT_GOODS and r["point_id"] not in test    # are sold by
            and int(r["qty"]) > 0 and r["order_id"] not in agreed and r["stage_code"] == "oth"]                                         # the point only, not on Avito


def manual_rows(conn) -> list:
    """«Свой остаток (ОТХ)» entered by hand (oth_manual, the owner 2026-11-03) in the shape of _order_rows: `item_id` is minus the row's id,
    `order_id` 0, `manual` the row's id. No test point."""
    from . import oth_manual
    try:
        found = oth_manual.rows(conn)
        test = _test_points(conn)
    except sqlite3.OperationalError:                      # a base without the table
        return []
    return [{"item_id": -r["id"], "order_id": 0, "goods_id": r["goods_id"], "code": "", "name": "", "ordered": r["qty"], "point_id": r["point_id"],
             "point": r["point"], "since": r["at"], "until": "", "returned_at": None, "returned_by": None, "returned_qty": 0, "left": r["qty"],
             "qty": r["qty"], "manual": r["id"]} for r in found if r["point_id"] not in test and int(r["qty"]) > 0]


def _rows(conn, manual: bool = True) -> list:
    """Our tyres at the points: those of the orders on «На ОТХ» first, then the manual ones (`manual` False: the orders' only)."""
    return _order_rows(conn) + (manual_rows(conn) if manual else [])


def by_point(conn) -> Dict[Tuple[str, int], int]:
    """(goods_id, point id) → the pieces of it on «На ОТХ» at that point, not returned to the supplier: what a new order can take from OTH."""
    found: Dict[Tuple[str, int], int] = {}
    for r in _rows(conn):
        key = (r["goods_id"], r["point_id"])
        found[key] = found.get(key, 0) + int(r["qty"])
    return found


def pieces(conn, manual: bool = True) -> Dict[str, int]:
    """goods_id → the pieces of it on «На ОТХ» (all points), not returned to the supplier; with the manual ones unless `manual` is False."""
    found: Dict[str, int] = {}
    for r in _rows(conn, manual):
        found[r["goods_id"]] = found.get(r["goods_id"], 0) + int(r["qty"])
    return found


def points(conn, manual: bool = True) -> Dict[str, Tuple[str, ...]]:
    """goods_id → the names of the points it lies at on «На ОТХ», as the clients see them, in alphabetical order."""
    found: Dict[str, set] = {}
    for r in _rows(conn, manual):
        if (r["point"] or "").strip():
            found.setdefault(r["goods_id"], set()).add(r["point"].strip())
    return {code: tuple(sorted(names)) for code, names in found.items()}


def _where(conn) -> Dict[int, str]:
    """Point id → its «Где» («на Юноне», order_points), in the order of the points; a point without one is not named."""
    try:
        rows = conn.execute("SELECT id, where_text FROM point ORDER BY id").fetchall()
    except sqlite3.OperationalError:                      # a base without the table
        return {}
    return {r[0]: r[1].strip() for r in rows if WHERE.fullmatch((r[1] or "").strip())}


def places(conn, manual: bool = True) -> Dict[str, Tuple[str, ...]]:
    """goods_id → the «Где» of the points it lies at on «На ОТХ» now: the names of the first line of its ad, in the order of the points."""
    rows = _rows(conn, manual)
    where = _where(conn) if rows else {}
    found: Dict[str, set] = {}
    for r in rows:
        if r["point_id"] in where:
            found.setdefault(r["goods_id"], set()).add(r["point_id"])
    return {code: tuple(dict.fromkeys(where[p] for p in sorted(ids))) for code, ids in found.items()}


def in_order(conn, named: Iterable[str]) -> Tuple[str, ...]:
    """The «Где» of several lots of one tire together, in the order of the points."""
    order = {p: i for i, p in reversed(list(enumerate(_where(conn).values())))}
    return tuple(sorted(set(named), key=lambda p: (order.get(p, len(order)), p)))


def line_of(named: Iterable[str]) -> str:
    named = list(dict.fromkeys(n.strip() for n in named or () if n and n.strip()))
    return "%s %s" % (FIRST_LINE, ", ".join(named)) if named else FIRST_LINE


def named_in(text: str) -> Tuple[str, ...]:
    """What the first line of a description names: () without the line or with the bare «Шины в наличии». Pure: for the check of a feed."""
    match = _LINE.match(text or "")
    tail = match.group(0)[len(FIRST_LINE):].strip() if match else ""
    return tuple(n.strip() for n in tail.split(",") if n.strip())


def add_line(text: str, named: Iterable[str] = ()) -> str:
    """The description with «Шины в наличии на Юноне, в Янино» as its first line (once); left whole when the line would make it too long for Avito."""
    text = drop_line(text or "")
    marked = line_of(named) + "\n" + text
    return marked if len(marked) <= MAX_DESCRIPTION else text


def drop_line(text: str) -> str:
    match = _LINE.match(text or "")
    return text[match.end():] if match else (text or "")


def with_count(text: str, count: int, template: str = None) -> str:
    """A saved ad with `count` written as {stock} writes it («более 4» above 4): in the lines of the account's template, whatever their wording
    (description.set_stock). Saved under another template: «В наличии: N шт.» when the text says it exactly once, otherwise as it is."""
    from .description import DEFAULT_TEMPLATE, set_stock, stock_text
    found = set_stock(text or "", template or DEFAULT_TEMPLATE, count)
    if found is not None:
        return found
    if len(_COUNT.findall(text or "")) != 1:
        return text
    return _COUNT.sub(lambda m: "%s%s%s" % (m.group(1), stock_text(count), m.group(2)), text)
