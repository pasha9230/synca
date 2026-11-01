import dataclasses

import pytest

from helpers import make_offer, make_settings
from tires_sync.models import PricingRule
from tires_sync.pricing import FEED_STEP, TENS, final_price, own_price, position_price

RULE = PricingRule(pct_small=-5.85, pct_large=-6.2)


def test_rounds_up_to_ten_for_small_diameter():
    assert final_price(3808.35, RULE, 16, step=TENS) == 3590


def test_large_diameter_uses_large_percent():
    assert final_price(10000, RULE, 18, step=TENS) == 9380


def test_zero_percent_exact_value_is_not_bumped_by_float_noise():
    assert final_price(5440.0, PricingRule(0, 0), 16, step=TENS) == 5440
    assert final_price(5450.0, PricingRule(0, 0), 16, step=FEED_STEP) == 5450


def test_r17_is_small_group_and_r18_is_large():
    rule = PricingRule(-10, 10)
    assert final_price(1000, rule, 17, step=TENS) == 900
    assert final_price(1000, rule, 18, step=TENS) == 1100


def test_the_feed_step_is_fifty():
    assert FEED_STEP == 50 and TENS == 10


@pytest.mark.parametrize("base, expected", [(4860, 4900), (4900, 4900), (4901, 4950), (4851, 4900), (4850, 4850),
                                            (4849.99, 4850), (4850.01, 4900), (1, 50)])
def test_the_feed_rounds_up_to_a_multiple_of_fifty(base, expected):
    """The owner, 2026-11-01: the brand × season price of the feed goes up to a multiple of 50 ₽ (4860 → 4900), a multiple stays."""
    assert final_price(base, PricingRule(0, 0), 16, step=FEED_STEP) == expected


def test_the_brand_price_of_a_position_goes_up_to_fifty_its_own_price_up_to_tens():
    offer = make_offer(base_price=5000.0)
    s = make_settings()
    assert position_price(offer, s) == 4850                                                            # −3,82 % бренда: 4809 → 4850
    assert position_price(offer, dataclasses.replace(s, own_prices={"100": ("pct", 10.01)})) == 5510   # своя % — вверх до десятков
    assert position_price(offer, dataclasses.replace(s, own_prices={"100": ("price", 6995)})) == 7000  # своя цена — вверх до десятков
    assert own_price(4860, ("pct", 0)) == 4860 and own_price(0, ("price", 4861)) == 4870
