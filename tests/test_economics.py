from __future__ import annotations

import pytest

from tires_sync import economics

GISLAVED = {"markdown_pct": -8.5, "discount_pct": 0.0, "bonus_pct": 13.0, "monthly_pct": 10.0}


def test_the_owners_example_gislaved_for_10000():
    u = economics.unit(10000, 9150, GISLAVED)
    assert (u["cost"], u["bonus"], u["monthly"]) == (10000.0, 1300.0, 1000.0)
    assert u["margin"] == 1450.0 and u["margin_pct_price"] == 14.5 and round(u["margin_pct_sale"], 2) == 15.85
    assert u["has_terms"] is True


def test_a_discount_lowers_the_cost_and_a_bonus_comes_back_on_top():
    cordiant = {"markdown_pct": 2.5, "discount_pct": 9.0, "bonus_pct": 0.0, "monthly_pct": 10.0}
    u = economics.unit(10000, 10250, cordiant)
    assert u["cost"] == 9100.0 and u["margin"] == 2150.0 and u["margin_pct_price"] == 21.5


def test_commission_and_promotion_are_percents_of_the_sale_price():
    u = economics.unit(10000, 9150, GISLAVED, fee_pct=5, promo_pct=2)
    assert u["fee"] == 457.5 and u["promo"] == 183.0 and u["margin"] == 1450.0 - 457.5 - 183.0


def test_without_terms_only_the_purchase_counts_and_it_is_flagged():
    u = economics.unit(10000, 9150, None)
    assert u["margin"] == -850.0 and u["has_terms"] is False and u["bonus"] == 0.0


def test_a_set_of_four_is_four_times_one_tire():
    one = economics.unit(10000, 9150, GISLAVED)
    four = economics.unit(10000, 9150, GISLAVED, pieces=4)
    assert four["margin"] == one["margin"] * 4 and four["sale"] == 9150 * 4 and four["margin_pct_price"] == one["margin_pct_price"]


def test_size_class_follows_the_panel_split_at_r17():
    assert [economics.size_class(d) for d in (15, 17, 18, 22)] == ["small", "small", "large", "large"]


ROWS = [
    {"brand_key": "GISLAVED", "season": "summer", "size_class": "all", "valid_from": "2026-09-01", "bonus_pct": 11},
    {"brand_key": "GISLAVED", "season": "summer", "size_class": "small", "valid_from": "2026-09-01", "bonus_pct": 13},
    {"brand_key": "GISLAVED", "season": "summer", "size_class": "small", "valid_from": "2026-10-01", "bonus_pct": 12},
    {"brand_key": "NEXEN", "season": "winter", "size_class": "all", "valid_from": "2026-09-01", "bonus_pct": 5},
]


@pytest.mark.parametrize("brand,season,diameter,day,bonus", [
    ("GISLAVED", "summer", 16, "2026-09-15", 13),      # the exact size class wins over «all»
    ("GISLAVED", "summer", 16, "2026-10-02", 12),      # a newer valid_from replaces the older one
    ("GISLAVED", "summer", 19, "2026-10-02", 11),      # a large tire falls back to «all»
    ("NEXEN", "winter", 20, "2026-09-02", 5),
    ("GISLAVED", "summer", 16, "2026-08-31", None),    # not valid yet
    ("GISLAVED", "winter", 16, "2026-09-15", None),    # another season
])
def test_the_right_terms_for_a_position_and_a_day(brand, season, diameter, day, bonus):
    found = economics.pick_terms(ROWS, brand, season, diameter, day)
    assert (found["bonus_pct"] if found else None) == bonus
