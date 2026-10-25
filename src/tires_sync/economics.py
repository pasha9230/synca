"""Unit economics of a tire: what it costs us, what it brings and what is left.

The owner sells below the supplier price on purpose; the money is made on the supplier's terms: a discount at purchase,
a bonus (a percent of the price that comes back to the account) and a monthly bonus (ЕЖ) for cash purchases. All of it is
counted from the supplier's price (the price in the unload)."""
from __future__ import annotations

from typing import Dict, Iterable, Optional

SMALL_MAX = 17            # «до R17 вкл.» is small, everything above is large (the same split the pricing uses)


def size_class(diameter: int) -> str:
    return "small" if diameter <= SMALL_MAX else "large"


def pick_terms(rows: Iterable[dict], brand_key: str, season: str, diameter: int, day: str) -> Optional[dict]:
    """The supplier's terms in force on this day: the latest start date wins, and on the same date the exact size class beats «all»."""
    wanted = size_class(diameter)
    best, best_key = None, None
    for row in rows:
        if row["brand_key"] != brand_key or row["season"] != season or row["valid_from"] > day:
            continue
        if row["size_class"] not in (wanted, "all"):
            continue
        key = (row["valid_from"], row["size_class"] == wanted)
        if best_key is None or key > best_key:
            best, best_key = row, key
    return best


def unit(price: float, sale: float, terms: Optional[dict], fee_pct: float = 0.0, promo_pct: float = 0.0, pieces: int = 1) -> Dict[str, float]:
    """Money of one tire (pieces=1) or of a set (pieces=4): supplier price → what we pay and get back → margin."""
    t = terms or {}
    discount, bonus, monthly = (float(t.get(k) or 0.0) for k in ("discount_pct", "bonus_pct", "monthly_pct"))
    cost = price * (1 - discount / 100.0)
    bonus_rub, monthly_rub = price * bonus / 100.0, price * monthly / 100.0
    fee, promo = sale * fee_pct / 100.0, sale * promo_pct / 100.0
    margin = sale - cost + bonus_rub + monthly_rub - fee - promo
    out = {"price": price, "sale": sale, "cost": cost, "bonus": bonus_rub, "monthly": monthly_rub, "fee": fee, "promo": promo,
           "margin": margin}
    out = {key: round(value * pieces, 2) for key, value in out.items()}
    out["margin_pct_price"] = round(100.0 * margin / price, 2) if price else 0.0
    out["margin_pct_sale"] = round(100.0 * margin / sale, 2) if sale else 0.0
    out["has_terms"] = terms is not None
    return out
