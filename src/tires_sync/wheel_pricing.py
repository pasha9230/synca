"""Independent brand markups for wheel rims, kept out of tire pricing."""
from __future__ import annotations

import math
from .models import PricingRule
from .pricing import FEED_STEP, final_price
from .webmim import brand_key


def validate_rules(rules, limit=None):
    if not isinstance(rules, dict):
        raise ValueError('wheel_pricing: нужен список правил брендов')
    for brand, rule in rules.items():
        if not isinstance(brand, str) or not brand.strip() or not isinstance(rule, dict):
            raise ValueError('wheel_pricing: некорректный бренд или правило')
        for key in ('pct_small', 'pct_large'):
            value = rule.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError('wheel_pricing: процент должен быть конечным числом')
            if limit is not None and abs(value) > limit:
                raise ValueError('wheel_pricing: процент превышает допустимый предел')


def price_for(wheel, rules):
    rule = rules.get(brand_key(wheel.brand))
    if not rule or wheel.price <= 0:
        return None
    try:
        diameter = int(wheel.diameter)
    except ValueError:
        return None
    return final_price(wheel.price, PricingRule(rule['pct_small'], rule['pct_large']), diameter, step=FEED_STEP)
