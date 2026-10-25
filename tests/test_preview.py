from datetime import date

from tires_sync.catalog import parse_reference
from tires_sync.preview import preview_change
from tires_sync.webmim import parse_unload
from helpers import catalog_xml, commodity_xml, make_settings, settings_dict

REF = parse_reference(
    '<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n'
    '<make name="Pirelli">\n<model name="Test Model"/>\n</make>\n</Shiny>')
PARSED = parse_unload(catalog_xml(
    commodity_xml(NNOMMODIF="100"),
    commodity_xml(NNOMMODIF="200", SMARKA="Pirelli", SMODIFNAME="PIRELLI 205/55/16 T 94 Test Model"),
), "T1")
TODAY = date(2026, 10, 7)


def run(old, new, published=None):
    return preview_change(PARSED, REF, {}, published or {}, set(), {}, old, new, TODAY)


def test_changing_a_percent_shows_only_price_changes():
    pricing = settings_dict()["pricing"]
    pricing["IKON TYRES|winter"] = {"pct_small": -5.0, "pct_large": -5.0}
    result = run(make_settings(), make_settings(pricing=pricing))
    assert result.price_changes == [("100", 4810, 4750)]
    assert result.appear == [] and result.disappear == []


def test_disabling_a_brand_lists_disappearing_items():
    new = make_settings(enabled=[["PIRELLI", "winter"]])
    result = run(make_settings(), new)
    assert result.disappear == ["100"]
    assert result.new_counts == {"publish": 1}
    assert result.old_counts == {"publish": 2}


def test_enabling_a_brand_lists_appearing_items():
    old = make_settings(enabled=[["IKON TYRES", "winter"]])
    result = run(old, make_settings())
    assert result.appear == ["200"]


def test_preview_does_not_modify_inputs():
    from tires_sync.models import PublishedItem
    published = {"100": PublishedItem("100", 4810, 5000.0, "P1")}
    snapshot = dict(published)
    run(make_settings(), make_settings(min_stock=50), published)
    assert published == snapshot


def test_preview_shows_removals_after_switching_off_a_large_brand_despite_the_guard():
    from tires_sync.models import PublishedItem
    parsed = parse_unload(catalog_xml(*[commodity_xml(NNOMMODIF=str(i)) for i in range(12)]), "T1")
    published = {str(i): PublishedItem(str(i), 4810, 5000.0, "P1") for i in range(12)}
    result = preview_change(parsed, REF, {}, published, set(), {}, make_settings(),
                            make_settings(enabled=[["PIRELLI", "winter"]]), TODAY)
    assert len(result.disappear) == 12


def test_a_broken_row_whose_known_stock_falls_below_a_raised_threshold_disappears_as_in_the_cycle():
    """tails2 Ф5: a published position whose row is broken elsewhere (the price) is held on its saved ad, but its stock is known: below the
    threshold the cycle takes it off (sync.plan_decisions: parsed.stocks). The preview of raising the threshold shows it going too."""
    from tires_sync.models import PublishedItem
    parsed = parse_unload(catalog_xml(commodity_xml(NNOMMODIF="100"), commodity_xml(NNOMMODIF="300", NREST="5", NPRICE_DELAYED="abc")), "T1")
    published = {"300": PublishedItem("300", 4810, 5000.0, "P1")}
    result = preview_change(parsed, REF, {}, published, set(), {}, make_settings(min_stock=4), make_settings(min_stock=6), TODAY)
    assert result.disappear == ["300"]
