"""Commercial tires «C» without a model of the passenger reference go to the Avito section «Шины для грузовиков и спецтехники» (owner,
2026-11-10, narrowed 2026-11-11: a «C» with a passenger model is a passenger ad, tests/test_commercial_split.py): the ad takes the fields of
that section's template, Brand and Model come only from its lists; Id, Title, Description, Price, phone, address are those of a passenger ad.
A passenger ad is what it was (the watchman). The check before publication judges a truck ad by its own template and lists."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date

import pytest

from helpers import make_offer, make_settings
from tires_sync import feed_check, store, truck
from tires_sync.candidates import Candidate, evaluate
from tires_sync.catalog import keeps_commercial_mark, normalize_model, parse_reference
from tires_sync.cycle import TEMPLATE_PATH
from tires_sync.feed import ad_from_text, ad_to_text, render_ad, sale_terms, validate_ad
from tires_sync.mappings import MappingError, confirm_mapping, pending_mappings
from tires_sync.models import Season
from tires_sync.same_tire import identity, identity_of_ad

TODAY = date(2026, 11, 10)
BRANDS = ("IKON TYRES", "CORDIANT", "КАМА", "NO SUCH BRAND")


def settings():
    return make_settings(enabled=[[b, s] for b in BRANDS for s in ("winter", "summer", "all")],
                         pricing={"%s|%s" % (b, s): {"pct_small": 10.0, "pct_large": 10.0} for b in BRANDS for s in ("winter", "summer", "all")})
REF = parse_reference('<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n<model name="Character Ice 8"/>\n'
                      '<model name="Autograph Ice C3"/>\n</make>\n<make name="Cordiant">\n<model name="Business CA"/>\n</make>\n</Shiny>')
PASSENGER_TEMPLATE = feed_check.template_tags_of(TEMPLATE_PATH)


def c_offer(**kw):
    values = dict(goods_id="7", car_type="C", name="IKON 205/65/16 R 107/105C Character Ice C2",
                  name_orig="Ikon  205/65/16  R 107/105C  Character Ice C2", model_raw="Ikon Character Ice C2", width=205, height=65,
                  diameter=16, speed="R", load="107/105", stock=12)
    values.update(kw)
    return make_offer(**values)


def c_ad(pack=1, **kw):
    ev = evaluate({"7": c_offer(**kw)}, {}, settings(), REF, {})
    return render_ad(ev.ok["7"], 6000, "P1", make_settings(), TODAY, pack=pack)


def test_a_c_ad_has_every_field_of_the_truck_section():
    ad = c_ad()
    fields = {child.tag: child.text for child in ad}
    assert fields["Category"] == "Запчасти и аксессуары"
    assert fields["GoodsType"] == "Шины, диски и колёса"
    assert fields["ProductType"] == "Шины для грузовиков и спецтехники"
    assert fields["AdType"] == "Товар приобретен на продажу"
    assert fields["Condition"] == "Новое"
    assert fields["Brand"] == "Ikon Tyres" and fields["Model"] == "Character Ice C2"
    assert (fields["TireSectionWidth"], fields["TireAspectRatio"], fields["RimDiameter"]) == ("205", "65", "16")
    assert fields["TireType"] == "Зимние нешипованные"
    assert fields["LoadIndex"] == "107" and fields["SpeedIndex"] == "R"
    assert fields["Quantity"] == "за 1 шт."
    assert fields["VehicleType"] == "Грузовая" and fields["Construction"] == "Радиальный" and fields["TubeType"] == "Бескамерная"
    lists = truck.values()
    for tag in ("Category", "GoodsType", "ProductType", "AdType", "Condition", "Brand", "Model", "TireSectionWidth", "TireAspectRatio",
                "RimDiameter", "TireType", "LoadIndex", "SpeedIndex", "Quantity", "VehicleType", "Construction", "TubeType"):
        assert fields[tag] in lists[tag], tag
    assert validate_ad(ad) == []


def test_a_c_ad_has_no_passenger_tags_and_only_tags_of_the_truck_template():
    ad = c_ad(runflat=True, homologation="MO", mnf_year="2025")
    tags = {child.tag for child in ad}
    assert not tags & {"MultiItem", "FreeTireService", "RunFlat", "Homologation", "TireYear", "MinimumOrderQuantity"}
    assert tags <= truck.template_tags()
    assert "Легковые шины" not in ad_to_text(ad)


def test_the_rest_of_a_c_ad_is_what_a_passenger_ad_of_the_account_has():
    cand = evaluate({"7": c_offer()}, {}, settings(), REF, {}).ok["7"]
    as_passenger = Candidate("7", c_offer(car_type=""), cand.price, cand.avito_brand, cand.avito_model)
    for pack in (1, 4):
        ours = render_ad(cand, 6000, "P1", make_settings(), TODAY, template="{title} за 1 шт. {price}", pack=pack, title_template="{brand} {size}")
        passenger = render_ad(as_passenger, 6000, "P1", make_settings(), TODAY, template="{title} за 1 шт. {price}", pack=pack,
                              title_template="{brand} {size}")
        for tag in ("Id", "Title", "Price", "Description", "ContactPhone", "SellerAddressID", "DateEnd", "WholesaleType"):
            assert ours.findtext(tag) == passenger.findtext(tag), tag
        assert [i.get("url") for i in ours.findall("Images/Image")] == [i.get("url") for i in passenger.findall("Images/Image")]


@pytest.mark.parametrize("season, studded, kind", [(Season.SUMMER, False, "Летние"), (Season.WINTER, True, "Зимние шипованные"),
                                                   (Season.WINTER, False, "Зимние нешипованные"), (Season.ALL, False, "Всесезонные")])
def test_the_tire_type_of_a_c_ad(season, studded, kind):
    assert c_ad(season=season, studded=studded).findtext("TireType") == kind


@pytest.mark.parametrize("speed, written", [("H", None), ("V", None), ("W", None), ("T", "T"), ("Q", "Q"), ("R", "R"), ("", None)])
def test_the_speed_index_only_from_the_list(speed, written):
    ad = c_ad(speed=speed)
    assert ad.findtext("SpeedIndex") == written
    assert validate_ad(ad) == []


@pytest.mark.parametrize("load, written", [("107/105", "107"), ("112/110C", "112"), ("104", "104"), ("190/188", None), ("", None)])
def test_the_load_index_is_the_first_of_a_double_one_and_only_from_the_list(load, written):
    assert c_ad(load=load).findtext("LoadIndex") == written


def test_quantity_of_a_single_tire_and_of_a_set_of_four():
    single, four = c_ad(), c_ad(pack=4)
    assert single.findtext("Quantity") == "за 1 шт." and single.findtext("Id") == "7"
    assert four.findtext("Quantity") == "за 4 шт." and four.findtext("Id") == "7-x4" and four.findtext("Price") == "24000"
    assert validate_ad(four) == []


def test_a_size_that_is_not_in_the_section_lists_makes_the_ad_invalid():
    assert "bad_value:TireAspectRatio" in validate_ad(c_ad(height=66))


def test_brand_and_model_come_only_from_the_truck_lists():
    # 2026-11-11: «Autograph Ice C3» of the passenger reference makes «2» a passenger ad (it waited for the truck list before)
    offers = {"1": c_offer(goods_id="1", brand_key="CORDIANT", brand_raw="Cordiant", model_raw="BUSINESS CW 2"),
              "2": c_offer(goods_id="2", brand_key="IKON TYRES", model_raw="Autograph Ice C3"),      # in the passenger reference only
              "3": c_offer(goods_id="3", brand_key="КАМА", brand_raw="КАМА", model_raw="Нет Такой"),
              "4": c_offer(goods_id="4", brand_key="NO SUCH BRAND", model_raw="Business CA")}
    ev = evaluate(offers, {}, settings(), REF, {})
    assert (ev.ok["1"].avito_brand, ev.ok["1"].avito_model, ev.ok["1"].truck_section) == ("Cordiant", "Business CW 2", True)
    assert (ev.ok["2"].avito_brand, ev.ok["2"].avito_model, ev.ok["2"].truck_section) == ("Ikon Tyres", "Autograph Ice C3", False)
    assert ev.unmatched["3"][0] == "КАМА (Нижнекамский шинный завод)"
    assert ev.problems["4"] == ["brand_not_in_reference:NO SUCH BRAND"]
    assert ev.truck == {"3"}


def test_a_model_with_another_commercial_number_is_never_taken():
    offer = c_offer(model_raw="Ikon Character Ice C3")                     # the list has «Character Ice C2», not C3
    passenger_pair = {("Ikon Tyres", normalize_model("Ikon Character Ice C3")): "Character Ice 8"}
    truck_pair = {(truck.pair_brand("Ikon Tyres"), normalize_model("Ikon Character Ice C3")): "Character Ice C2"}
    for confirmed in ({}, passenger_pair, truck_pair):
        ev = evaluate({"7": offer}, {}, make_settings(), REF, confirmed)
        assert "7" not in ev.ok and ev.problems["7"] == ["model_needs_mapping:Ikon Character Ice C3"] and ev.truck == {"7"}
    pending = pending_mappings(evaluate({"7": offer}, {}, make_settings(), REF, {}), REF)
    assert [(p.brand, p.model_raw, p.section) for p in pending] == [("Ikon Tyres", "Ikon Character Ice C3", "truck")]
    assert all(keeps_commercial_mark("Ikon Character Ice C3", s) for s in pending[0].suggestions)
    assert "Character Ice C2" not in pending[0].suggestions and pending[0].proposal is None


def test_a_passenger_pair_is_not_read_for_a_c_position_and_a_truck_pair_is_not_read_for_a_passenger_one():
    raw = "Ikon Char Ice C2 new"
    passenger_pair = {("Ikon Tyres", normalize_model(raw)): "Character Ice C2"}
    ev = evaluate({"7": c_offer(model_raw=raw)}, {}, make_settings(), REF, passenger_pair)
    assert "7" not in ev.ok and ev.truck == {"7"}
    truck_pair = {(truck.pair_brand("Ikon Tyres"), normalize_model(raw)): "Character Ice C2"}
    ev = evaluate({"7": c_offer(model_raw=raw), "8": make_offer(goods_id="8", model_raw=raw)}, {}, make_settings(), REF, truck_pair)
    assert ev.ok["7"].avito_model == "Character Ice C2"
    assert "8" not in ev.ok and ev.unmatched["8"] == ("Ikon Tyres", raw) and "8" not in ev.truck


def test_the_owner_confirms_a_c_model_only_from_the_truck_list(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    with pytest.raises(MappingError):
        confirm_mapping(conn, "Ikon Tyres", "Ikon Char Ice C2 new", "Autograph Ice C3", REF, "2026-11-10T12:00:00", "o", section="truck")
    with pytest.raises(MappingError):                                      # in the list, but another number: another tire
        confirm_mapping(conn, "Ikon Tyres", "Ikon Char Ice C3 new", "Character Ice C2", REF, "2026-11-10T12:00:00", "o", section="truck")
    confirm_mapping(conn, "Ikon Tyres", "Ikon Char Ice C2 new", "Character Ice C2", REF, "2026-11-10T12:00:00", "o", section="truck")
    saved = store.load_confirmed_mappings(conn)
    assert saved == {("truck:Ikon Tyres", normalize_model("Ikon Char Ice C2 new")): "Character Ice C2"}
    ev = evaluate({"7": c_offer(model_raw="Ikon Char Ice C2 new")}, {}, make_settings(), REF, saved)
    assert ev.ok["7"].avito_model == "Character Ice C2"


PASSENGER_TAGS = ["Id", "Category", "GoodsType", "AdType", "ProductType", "Condition", "DateEnd", "Title", "Price", "Brand", "Model",
                  "TireSectionWidth", "TireAspectRatio", "RimDiameter", "MultiItem", "WholesaleType", "FreeTireService", "SellerAddressID",
                  "ContactPhone", "TireType", "RunFlat", "SpeedIndex", "LoadIndex", "Homologation", "TireYear", "Description", "Images"]


def test_guard_a_passenger_ad_is_what_it_was():
    offer = make_offer(goods_id="9", runflat=True, homologation="MO", mnf_year="2025", load="94T")
    ad = render_ad(Candidate("9", offer, 5000, "Ikon Tyres", "Test Model"), 5000, "P1", make_settings(), TODAY)
    assert [child.tag for child in ad] == PASSENGER_TAGS
    fields = {child.tag: child.text for child in ad}
    assert (fields["ProductType"], fields["MultiItem"], fields["FreeTireService"], fields["LoadIndex"], fields["SpeedIndex"]) == (
        "Легковые шины", "Да", "Нет", "94T", "T")
    assert fields["Homologation"] == "Mercedes-Benz" and fields["TireYear"] == "2025" and fields["RunFlat"] == "Да"
    assert validate_ad(ad) == []
    four = render_ad(Candidate("9", make_offer(goods_id="9"), 5000, "Ikon Tyres", "Test Model"), 5000, "P1", make_settings(), TODAY, pack=4)
    assert four.find("Quantity") is None and four.findtext("ProductType") == "Легковые шины"


def test_a_saved_truck_ad_keeps_its_quantity_and_gets_no_multi_item():
    back = ad_from_text(ad_to_text(c_ad(pack=4)))
    assert back.findtext("Quantity") == "за 4 шт." and back.find("MultiItem") is None and back.findtext("WholesaleType") == "Нет"
    assert sale_terms(c_ad()).find("MultiItem") is None
    passenger = render_ad(Candidate("9", make_offer(goods_id="9"), 5000, "Ikon Tyres", "Test Model"), 5000, "P1", make_settings(), TODAY)
    ET.SubElement(passenger, "Quantity").text = "за 1 шт."
    back = ad_from_text(ad_to_text(passenger))
    assert back.find("Quantity") is None and back.findtext("MultiItem") == "Да"         # a passenger ad: as before


def test_a_c_tire_and_a_passenger_tire_of_one_size_are_two_tires():
    cand = evaluate({"7": c_offer(model_raw="Business CW 2", brand_key="CORDIANT", brand_raw="Cordiant")}, {},   # not a passenger model of REF
                    settings(), REF, {}).ok["7"]
    passenger = Candidate("8", make_offer(goods_id="8", width=205, height=65, diameter=16, speed="R", load="107/105", season=cand.offer.season),
                          5000, "Cordiant", "Business CW 2")
    assert identity(cand) != identity(passenger) and identity(cand)[:9] == identity(passenger)
    assert len(identity(passenger)) == 9                                   # a passenger key as retired_ad keeps it
    truck_ad = render_ad(cand, 6000, "P1", make_settings(), TODAY)
    assert identity_of_ad(ad_to_text(truck_ad)) == identity(cand)


# --- the check before publication -------------------------------------------------------------------------------------------------


def passenger_ad(goods_id="100", **kw):
    offer = make_offer(goods_id=goods_id, stock=12, **kw)
    return render_ad(Candidate(goods_id, offer, 4810, "Ikon Tyres", "Test Model"), 4810, "P1", make_settings(), TODAY)


def ctx(**kw):
    base = dict(mode="generated", template_tags=PASSENGER_TEMPLATE, reference=REF, stocks={"100": 12, "101": 12, "7": 12}, min_stock=4,
                phone="+70000000000")
    base.update(kw)
    return feed_check.Context(**base)


def test_a_feed_with_truck_ads_passes_the_check():
    assert feed_check.check_feed([passenger_ad(), c_ad(), c_ad(pack=4)], ctx(pack4=True)) == []


def test_truck_ads_do_not_change_the_verdict_on_passenger_ads():
    bad = passenger_ad("101", load="95")
    bad.find("Title").text = "Ikon 205/65 109/107 C Character"                 # a «C» in the passenger section: still stopped
    bad.find("Model").text = "Character Ice C2"                                 # in the truck list, not in the passenger reference
    ET.SubElement(bad, "Quantity").text = "за 1 шт."
    alone = feed_check.check_feed([passenger_ad(), bad], ctx())
    mixed = feed_check.check_feed([passenger_ad(), bad, c_ad()], ctx())
    assert sorted((f.rule, f.ad_id) for f in alone) == sorted((f.rule, f.ad_id) for f in mixed)
    assert {f.rule for f in alone if f.ad_id == "101"} == {"commercial", "model_in_reference", "template_tags"}


@pytest.mark.parametrize("change, rule", [
    (lambda a: ET.SubElement(a, "MultiItem").__setattr__("text", "Да"), "template_tags"),
    (lambda a: a.find("Model").__setattr__("text", "Test Model"), "model_in_reference"),
    (lambda a: a.find("Brand").__setattr__("text", "No Such Brand"), "model_in_reference"),
    (lambda a: a.find("VehicleType").__setattr__("text", "Легковая"), "required_fields"),
    (lambda a: a.find("Price").__setattr__("text", "0"), "price"),
    (lambda a: a.find("ContactPhone").__setattr__("text", "+79990000000"), "phone"),
])
def test_a_truck_ad_is_still_judged_by_the_rules(change, rule):
    ad = c_ad()
    change(ad)
    assert rule in {f.rule for f in feed_check.check_feed([passenger_ad(), ad], ctx())}


def test_a_c_tire_and_a_passenger_tire_of_one_size_are_not_the_same_tire_for_the_check():
    passenger = passenger_ad("101", width=205, height=65, diameter=16, speed="R", load="107/105")
    passenger.find("Model").text = "Business CW 2"
    passenger.find("Brand").text = "Cordiant"
    c = c_ad(brand_key="CORDIANT", brand_raw="Cordiant", model_raw="Business CW 2")                   # not a passenger model of REF
    assert "same_tire" not in {f.rule for f in feed_check.check_feed([passenger, c], ctx(reference={}))}


@pytest.mark.parametrize("speed, load", [("H", "107/105"), ("V", "190/188"), ("R", "112/110C")])
def test_the_key_of_a_c_tire_is_the_same_from_the_candidate_and_from_its_saved_ad(speed, load):
    """An index the section has not is not written: the saved ad gives the same key (retired_ad, the oldest Id), never None."""
    cand = evaluate({"7": c_offer(speed=speed, load=load)}, {}, settings(), REF, {}).ok["7"]
    key = identity(cand)
    assert key is not None and identity_of_ad(ad_to_text(render_ad(cand, 6000, "P1", make_settings(), TODAY))) == key


def test_two_c_lots_that_differ_only_by_an_unwritten_speed_are_one_tire_for_the_build_and_the_check():
    h = evaluate({"7": c_offer(speed="H")}, {}, settings(), REF, {}).ok["7"]
    v = evaluate({"8": c_offer(goods_id="8", speed="V")}, {}, settings(), REF, {}).ok["8"]
    assert identity(h) == identity(v)                                  # their ads are alike for Avito: one is published (same_tire.dedupe)
    ads = [render_ad(c, 6000, "P1", make_settings(), TODAY) for c in (h, v)]
    assert "same_tire" in {f.rule for f in feed_check.check_feed(ads, ctx(stocks={"7": 12, "8": 12}))}
