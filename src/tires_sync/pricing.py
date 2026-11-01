from __future__ import annotations

from decimal import Decimal, ROUND_CEILING
from typing import Optional

from .models import PricingRule


FEED_STEP = 50      # the brand × season price of the feed: up to a multiple of 50 ₽ (the owner, 2026-11-01: 4860 → 4900)
TENS = 10           # own prices of a position and the client catalog: up to tens, as before


def _up_to(value: Decimal, step: int) -> int:
    return int((value / Decimal(step)).to_integral_value(rounding=ROUND_CEILING)) * step


def final_price(base: float, rule: PricingRule, diameter: int, *, step: int) -> int:
    """The purchase `base` with the rule's percent of the diameter, rounded up to a multiple of `step` (FEED_STEP for the feed, TENS for the
    client catalog); the step is always named by the caller."""
    pct = rule.pct_small if diameter <= 17 else rule.pct_large
    return _up_to(Decimal(str(base)) * (Decimal(1) + Decimal(str(pct)) / Decimal(100)), step)


def own_price(base: float, own) -> Optional[int]:
    """The price of one tyre by its own price: ("pct", percent to the purchase) or ("price", the sale price); up to tens (the brand's price of
    the feed goes up to 50: the owner, 2026-11-01). A percent of a bad purchase price gives none."""
    kind, value = own
    if kind == "price":
        return _up_to(Decimal(str(value)), TENS)
    if not base or base <= 0:
        return None
    return _up_to(Decimal(str(base)) * (Decimal(1) + Decimal(str(value)) / Decimal(100)), TENS)


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
    return final_price(offer.base_price, rule, offer.diameter, step=FEED_STEP)
