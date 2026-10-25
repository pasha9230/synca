import json

import pytest

from tires_sync.config import load_env, load_settings, settings_from_dict
from tires_sync.models import Season
from helpers import settings_dict


def test_load_env_parses_pairs_and_ignores_comments(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# comment\nA=1\n\nB = two words \nbroken\n", encoding="utf-8")
    assert load_env(p) == {"A": "1", "B": "two words"}


def test_load_env_missing_file_is_empty(tmp_path):
    assert load_env(tmp_path / "none") == {}


def test_load_settings_roundtrip(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps(settings_dict()), encoding="utf-8")
    s = load_settings(p)
    assert s.min_stock == 4
    assert ("IKON TYRES", Season.WINTER) in s.enabled
    assert s.pricing[("PIRELLI", Season.WINTER)].pct_large == -6.2
    assert s.point_values == {"P1": 40, "P2": 40, "P3": 20}


def test_enabled_pair_without_pricing_rule_is_rejected():
    raw = settings_dict(enabled=[["IKON TYRES", "summer"]])
    with pytest.raises(ValueError, match="pricing"):
        settings_from_dict(raw)


def test_percent_outside_limit_is_rejected():
    raw = settings_dict()
    raw["pricing"]["IKON TYRES|winter"]["pct_small"] = -60
    with pytest.raises(ValueError, match="limit"):
        settings_from_dict(raw)


def test_percent_points_must_sum_to_100():
    with pytest.raises(ValueError, match="100"):
        settings_from_dict(settings_dict(point_values={"P1": 50, "P2": 40}))


def test_count_mode_rest_must_be_a_known_point():
    with pytest.raises(ValueError, match="rest"):
        settings_from_dict(settings_dict(point_mode="count", point_rest="X"))


def test_percent_limit_can_be_waived_for_strong_confirmation():
    raw = settings_dict()
    raw["pricing"]["IKON TYRES|winter"]["pct_small"] = -60
    assert settings_from_dict(raw, enforce_pct_limit=False).pricing[("IKON TYRES", Season.WINTER)].pct_small == -60


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_are_rejected_everywhere(bad):
    raw = settings_dict()
    raw["pricing"]["IKON TYRES|winter"]["pct_small"] = bad
    with pytest.raises(ValueError, match="finite"):
        settings_from_dict(raw, enforce_pct_limit=False)
    with pytest.raises(ValueError, match="finite"):
        settings_from_dict(settings_dict(point_values={"P1": bad, "P2": 50, "P3": 50}), enforce_pct_limit=False)
    for key in ("price_jump_limit", "guard_fraction", "pct_limit"):
        with pytest.raises(ValueError, match="finite"):
            settings_from_dict(settings_dict(**{key: bad}), enforce_pct_limit=False)
