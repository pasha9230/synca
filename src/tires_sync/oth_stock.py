"""«На ОТХ» in the feed (the owner 2026-10-29): the tires of an order refused by its client lie at the point, they are ours. While the order
stands in «На ОТХ», the ad of that position, in every account of ours, starts with «Шины в наличии» and is not taken off for the supplier's
stock (decide: HOLD «oth:<reason>» on the saved ad, its last price); the check before publication does not judge it by stock. When the order
leaves the stage, or the whole position is marked «возвращено поставщику», the next cycle applies the usual rules. Only reads the orders."""
from __future__ import annotations

import sqlite3
from typing import Dict

FIRST_LINE = "Шины в наличии"
MAX_DESCRIPTION = 7500                                    # feed.validate_ad: a longer description is refused by Avito


def pieces(conn) -> Dict[str, int]:
    """goods_id → the pieces of it on «На ОТХ» that are not marked returned to the supplier. A tyre of the point without a code
    (orders.POINT_GOODS) has no ad. A base without the orders' tables: nothing."""
    from . import order_oth, orders
    try:
        found = order_oth.goods_on_oth(conn)
    except sqlite3.OperationalError:
        return {}
    return {code: n for code, n in found.items() if code != orders.POINT_GOODS and n > 0}


def add_line(text: str) -> str:
    """The description with «Шины в наличии» as its first line (once); left whole when the line would make it too long for Avito."""
    text = drop_line(text or "")
    marked = FIRST_LINE + "\n" + text
    return marked if len(marked) <= MAX_DESCRIPTION else text


def drop_line(text: str) -> str:
    return text[len(FIRST_LINE) + 1:] if (text or "").startswith(FIRST_LINE + "\n") else (text or "")
