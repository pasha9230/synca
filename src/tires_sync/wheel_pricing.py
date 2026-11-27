"""Independent brand markups for wheel rims, kept out of tire pricing."""
from __future__ import annotations

import math
from .models import PricingRule
from .pricing import FEED_STEP, final_price, own_price
from .webmim import brand_key


def validate_rules(rules, limit=None):
    if not isinstance(rules, dict):
        raise ValueError('wheel_pricing: нужен список правил брендов')
    for brand, rule in rules.items():
        if not isinstance(brand, str) or not brand.strip() or not isinstance(rule, dict):
            raise ValueError('wheel_pricing: некорректный бренд или правило')
        if 'enabled' in rule and not isinstance(rule['enabled'], bool):
            raise ValueError('wheel_pricing: некорректный признак публикации')
        for key in ('pct_small', 'pct_large'):
            value = rule.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError('wheel_pricing: процент должен быть конечным числом')
            if limit is not None and abs(value) > limit:
                raise ValueError('wheel_pricing: процент превышает допустимый предел')


def price_for(wheel, rules, overrides=None):
    own = (overrides or {}).get('1:w-' + wheel.goods_id)
    if own:
        from .config import own_price_value
        return own_price(wheel.price, own_price_value(own))
    rule = rules.get(brand_key(wheel.brand))
    if not rule or wheel.price <= 0:
        return None
    try:
        diameter = int(wheel.diameter)
    except ValueError:
        return None
    if not 8 <= diameter <= 40:
        return None
    try:
        return final_price(wheel.price, PricingRule(rule['pct_small'], rule['pct_large']), diameter, step=FEED_STEP)
    except (ArithmeticError, OverflowError, ValueError):
        return None


def validate_accounts(accounts):
    if not isinstance(accounts, dict):
        raise ValueError('wheel_accounts: нужна таблица аккаунтов')
    from .connections import PRIMARY_ACCOUNT
    for account, addresses in accounts.items():
        if not isinstance(account, str) or account != str(PRIMARY_ACCOUNT):
            raise ValueError('wheel_accounts: некорректный аккаунт')
        if not isinstance(addresses, list) or any(not isinstance(a, str) or not a.strip() or len(a) > 128 for a in addresses):
            raise ValueError('wheel_accounts: некорректные адреса')
        if len(addresses) != len(set(addresses)):
            raise ValueError('wheel_accounts: повторяющийся адрес')
