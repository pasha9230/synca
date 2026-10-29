from __future__ import annotations

from decimal import Decimal, ROUND_CEILING
from typing import Optional

from .models import PricingRule


def _up_to_tens(value: Decimal) -> int:
    return int((value / Decimal(10)).to_integral_value(rounding=ROUND_CEILING)) * 10


def final_price(base: float, rule: PricingRule, diameter: int) -> int:
    pct = rule.pct_small if diameter <= 17 else rule.pct_large
    return _up_to_tens(Decimal(str(base)) * (Decimal(1) + Decimal(str(pct)) / Decimal(100)))


def own_price(base: float, own) -> Optional[int]:
    """The price of one tyre by its own price: ("pct", percent to the purchase) or ("price", the sale price); up to tens, as every price of the
    feed. A percent of a bad purchase price gives none."""
    kind, value = own
    if kind == "price":
        return _up_to_tens(Decimal(str(value)))
    if not base or base <= 0:
        return None
    return _up_to_tens(Decimal(str(base)) * (Decimal(1) + Decimal(str(value)) / Decimal(100)))


def position_price(offer, settings) -> Optional[int]:
    """The sale price of one tyre of the position in the account of `settings` (connections.effective_settings): its own price of that account
    first (in place of the brand's percent and the account's corrections), else the brand × season rule; None: neither, or a bad purchase price.
    General settings (no account) carry no own prices: a position is priced there by its brand only."""
    own = getattr(settings, "own_prices", {}).get(getattr(offer, "goods_id", None))
    if own is not None:
        return own_price(offer.base_price, own)
    rule = settings.pricing.get((offer.brand_key, offer.season))
    if rule is None or offer.base_price <= 0:
        return None
    return final_price(offer.base_price, rule, offer.diameter)
