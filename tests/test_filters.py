from tires_sync.filters import exclusion_reason
from tires_sync.models import Season
from helpers import make_offer, make_settings

S = make_settings()


def test_enabled_offer_passes():
    assert exclusion_reason(make_offer(), S) is None


def test_commercial_tires_are_excluded():
    assert exclusion_reason(make_offer(car_type="C"), S) == "commercial"
    assert exclusion_reason(make_offer(car_type="lt"), S) == "commercial"


def test_old_production_in_year_or_name():
    assert exclusion_reason(make_offer(mnf_year="старше 3-х лет"), S) == "old_production"
    assert exclusion_reason(make_offer(name="X старше 5-ти лет"), S) == "old_production"


def test_small_diameter_is_excluded_by_setting():
    assert exclusion_reason(make_offer(diameter=14), S) == "diameter"
    assert exclusion_reason(make_offer(diameter=14), make_settings(min_diameter=14)) is None


def test_not_enabled_brand_or_season():
    assert exclusion_reason(make_offer(brand_key="KUMHO"), S) == "not_enabled"
    assert exclusion_reason(make_offer(season=Season.SUMMER), S) == "not_enabled"


def test_unknown_season_of_enabled_brand_is_a_problem_not_an_exclusion():
    assert exclusion_reason(make_offer(season=Season.UNKNOWN), S) == "unknown_season"
    assert exclusion_reason(make_offer(season=Season.UNKNOWN, brand_key="KUMHO"), S) == "not_enabled"
