from __future__ import annotations

from decimal import Decimal, ROUND_CEILING

from .models import PricingRule


def final_price(base: float, rule: PricingRule, diameter: int) -> int:
    pct = rule.pct_small if diameter <= 17 else rule.pct_large
    value = Decimal(str(base)) * (Decimal(1) + Decimal(str(pct)) / Decimal(100))
    tens = (value / Decimal(10)).to_integral_value(rounding=ROUND_CEILING)
    return int(tens) * 10
