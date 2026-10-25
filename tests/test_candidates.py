from tires_sync.candidates import evaluate
from tires_sync.catalog import normalize_model, parse_reference
from tires_sync.models import Season
from helpers import make_offer, make_settings

REF = parse_reference('<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>')
S = make_settings()


def run(offers, issues=None, confirmed=None):
    return evaluate({o.goods_id: o for o in offers}, issues or {}, S, REF, confirmed or {})


def test_good_offer_becomes_candidate_with_price_and_avito_names():
    ev = run([make_offer(base_price=5000.0)])
    c = ev.ok["100"]
    assert (c.price, c.avito_brand, c.avito_model) == (4810, "Ikon Tyres", "Test Model")


def test_unmapped_model_is_a_problem_and_listed_as_unmatched():
    ev = run([make_offer(model_raw="Other")])
    assert ev.problems["100"] == ["model_needs_mapping:Other"]
    assert ev.unmatched["100"] == ("Ikon Tyres", "Other")
    assert ev.ok == {}


def test_confirmed_mapping_makes_candidate():
    confirmed = {("Ikon Tyres", normalize_model("Other")): "Test Model"}
    assert "100" in run([make_offer(model_raw="Other")], confirmed=confirmed).ok


def test_brand_missing_in_reference_is_a_problem():
    ev = run([make_offer(brand_key="PIRELLI", brand_raw="Pirelli")])
    assert ev.problems["100"] == ["brand_not_in_reference:PIRELLI"]


def test_zero_price_is_a_problem_never_a_candidate():
    ev = run([make_offer(base_price=0.0)])
    assert ev.problems["100"] == ["bad_price:0.0"]
    assert ev.ok == {}


def test_filtered_offers_are_excluded_with_reason():
    ev = run([make_offer(goods_id="1", car_type="C"), make_offer(goods_id="2", brand_key="KUMHO")])
    assert ev.excluded == {"1": "commercial", "2": "not_enabled"}


def test_unknown_season_is_a_problem():
    ev = run([make_offer(season=Season.UNKNOWN)])
    assert ev.problems["100"] == ["unknown_season"]


def test_parse_issues_are_carried_as_problems():
    ev = run([], issues={"7": ["bad_diameter:16C"]})
    assert ev.problems["7"] == ["bad_diameter:16C"]


def test_stocks_are_recorded_for_problem_offers_too():
    ev = run([make_offer(goods_id="1", stock=0, model_raw="Other"), make_offer(goods_id="2", stock=7)])
    assert ev.stocks == {"1": 0, "2": 7}


def test_brand_map_lets_an_unknown_supplier_brand_resolve():
    ref = parse_reference('<Shiny>\n<make name="Laufenn">\n<model name="Test Model"/>\n</make>\n</Shiny>')
    settings = make_settings(enabled=[["LAUFEN", "winter"]],
                             pricing={"LAUFEN|winter": {"pct_small": 0, "pct_large": 0}})
    offer = make_offer(brand_key="LAUFEN", brand_raw="Лауфен")
    assert "100" not in evaluate({"100": offer}, {}, settings, ref, {}).ok
    assert "100" in evaluate({"100": offer}, {}, settings, ref, {}, {"LAUFEN": "Laufenn"}).ok
