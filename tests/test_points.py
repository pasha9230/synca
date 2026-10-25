from collections import Counter

from tires_sync.points import assign_points
from helpers import make_settings


def test_percent_mode_from_empty_gives_40_40_20():
    result = assign_points(10, {}, make_settings())
    assert Counter(result) == {"P1": 4, "P2": 4, "P3": 2}


def test_existing_counts_are_respected_and_not_moved():
    result = assign_points(5, {"P1": 4, "P2": 0, "P3": 1}, make_settings())
    assert Counter(result) == {"P2": 4, "P3": 1}


def test_count_mode_exact_numbers_and_rest_point_takes_extra():
    s = make_settings(point_mode="count", point_values={"P1": 40, "P2": 40, "P3": 20}, point_rest="P3")
    assert Counter(assign_points(100, {}, s)) == {"P1": 40, "P2": 40, "P3": 20}
    assert Counter(assign_points(120, {}, s)) == {"P1": 40, "P2": 40, "P3": 40}


def test_count_mode_with_fewer_items_goes_to_largest_deficits_first():
    s = make_settings(point_mode="count", point_values={"P1": 40, "P2": 40, "P3": 20}, point_rest="P3")
    assert Counter(assign_points(10, {}, s)) == {"P1": 5, "P2": 5}


def test_zero_new_items_returns_empty():
    assert assign_points(0, {"P1": 3}, make_settings()) == []
