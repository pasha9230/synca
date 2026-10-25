from tires_sync.models import PricingRule
from tires_sync.pricing import final_price

RULE = PricingRule(pct_small=-5.85, pct_large=-6.2)


def test_rounds_up_to_ten_for_small_diameter():
    assert final_price(3808.35, RULE, 16) == 3590


def test_large_diameter_uses_large_percent():
    assert final_price(10000, RULE, 18) == 9380


def test_zero_percent_exact_value_is_not_bumped_by_float_noise():
    assert final_price(5440.0, PricingRule(0, 0), 16) == 5440


def test_r17_is_small_group_and_r18_is_large():
    rule = PricingRule(-10, 10)
    assert final_price(1000, rule, 17) == 900
    assert final_price(1000, rule, 18) == 1100
