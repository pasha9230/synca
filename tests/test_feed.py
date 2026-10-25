import xml.etree.ElementTree as ET
from datetime import date

import pytest

from tires_sync.candidates import Candidate
from tires_sync.feed import (
    FeedCheckError, ad_from_text, ad_to_text, assemble_feed, check_feed, render_ad, stable_date_end,
    validate_ad, write_atomic,
)
from tires_sync.models import Season
from helpers import make_offer, make_settings

S = make_settings()
TODAY = date(2026, 10, 6)


def cand(**kw):
    return Candidate("100", make_offer(**kw), 4810, "Ikon Tyres", "Test Model")


def tag(ad, name):
    return ad.findtext(name)


def test_render_has_required_fields_and_no_target_audience():
    ad = render_ad(cand(), 4810, "P1", S, TODAY)
    assert validate_ad(ad) == []
    assert (tag(ad, "Id"), tag(ad, "Price"), tag(ad, "SellerAddressID")) == ("100", "4810", "P1")
    assert (tag(ad, "Brand"), tag(ad, "Model")) == ("Ikon Tyres", "Test Model")
    assert ad.find("TargetAudience") is None
    assert ad.find("Quantity") is None                         # Avito's template of the category (2026-10-09) has no such tag


def test_tire_type_by_season_and_studs():
    assert tag(render_ad(cand(studded=True), 1, "P1", S, TODAY), "TireType") == "Зимние шипованные"
    assert tag(render_ad(cand(), 1, "P1", S, TODAY), "TireType") == "Зимние нешипованные"
    assert tag(render_ad(cand(season=Season.SUMMER), 1, "P1", S, TODAY), "TireType") == "Летние"
    assert tag(render_ad(cand(season=Season.ALL), 1, "P1", S, TODAY), "TireType") == "Всесезонные"


def test_runflat_only_when_explicit_and_load_index_first_part():
    assert render_ad(cand(), 1, "P1", S, TODAY).find("RunFlat") is None
    assert tag(render_ad(cand(runflat=True), 1, "P1", S, TODAY), "RunFlat") == "Да"
    assert tag(render_ad(cand(load="94/92"), 1, "P1", S, TODAY), "LoadIndex") == "94"


def test_the_year_of_production_is_written_only_when_the_supplier_gives_one_avito_accepts():
    year = lambda text: tag(render_ad(cand(mnf_year=text), 1, "P1", S, TODAY), "TireYear")
    assert year("2025") == "2025" and year(" 2026 ") == "2026" and year("2003") == "2003"
    assert year("") is None and year("старше 3-х лет") is None and year("Год производства") is None
    assert year("2002") is None and year("2027") is None and year("20255") is None                  # outside Avito's list: not written


def test_no_minimum_order_multi_item_and_no_wholesale_and_speed_and_load_index_follow_the_supplier():
    """The owner, 2026-10-20: MultiItem «Да», no MinimumOrderQuantity, wholesale «Нет», whatever the stock."""
    ad = render_ad(cand(stock=12, speed="T", load="94"), 1, "P1", S, TODAY)
    assert ad.find("MinimumOrderQuantity") is None and tag(ad, "FreeTireService") == "Нет"
    assert (tag(ad, "MultiItem"), tag(ad, "WholesaleType")) == ("Да", "Нет")
    assert (tag(ad, "SpeedIndex"), tag(ad, "LoadIndex")) == ("T", "94") and validate_ad(ad) == []
    thin = render_ad(cand(stock=3, speed="", load=""), 1, "P1", S, TODAY)
    assert thin.find("MinimumOrderQuantity") is None and thin.find("SpeedIndex") is None and thin.find("LoadIndex") is None
    twin = render_ad(cand(stock=12), 4810, "P1", S, TODAY, pack=4)
    assert twin.find("MinimumOrderQuantity") is None and tag(twin, "FreeTireService") == "Нет"
    assert (tag(twin, "MultiItem"), tag(twin, "WholesaleType")) == ("Да", "Нет")


def test_date_end_is_stable_within_renew_period_and_within_bounds():
    a = stable_date_end(date(2026, 10, 6), 29, 7)
    b = stable_date_end(date(2026, 10, 7), 29, 7)
    assert a == b
    assert 23 <= (a - date(2026, 10, 6)).days <= 29


def test_render_is_identical_across_days_in_one_renew_period():
    a = ad_to_text(render_ad(cand(), 4810, "P1", S, date(2026, 10, 6)))
    b = ad_to_text(render_ad(cand(), 4810, "P1", S, date(2026, 10, 7)))
    assert a == b


def test_validate_flags_empty_and_not_allowed_values():
    ad = render_ad(cand(), 4810, "P1", S, TODAY)
    ad.find("Title").text = ""
    ad.find("TireType").text = "Какие-то"
    errors = validate_ad(ad)
    assert "empty:Title" in errors and "bad_value:TireType" in errors


def test_assemble_feed_root_and_snapshot_roundtrip():
    ad = render_ad(cand(), 4810, "P1", S, TODAY)
    data = assemble_feed([ad_from_text(ad_to_text(ad))])
    root = ET.fromstring(data)
    assert (root.tag, root.get("formatVersion"), root.get("target")) == ("Ads", "3", "Avito.ru")
    assert len(root.findall("Ad")) == 1


def test_check_feed_rejects_empty_and_collapsed_feeds():
    check_feed(10, 0)
    check_feed(5, 10)
    with pytest.raises(FeedCheckError):
        check_feed(0, 10)
    with pytest.raises(FeedCheckError):
        check_feed(4, 10)


def test_write_atomic_replaces_file_and_leaves_no_temp(tmp_path):
    target = tmp_path / "avito_feed.xml"
    write_atomic(target, b"one")
    write_atomic(target, b"two")
    assert target.read_bytes() == b"two"
    assert [p.name for p in tmp_path.iterdir()] == ["avito_feed.xml"]


def test_check_feed_allows_deliberate_shrink_but_never_empty():
    check_feed(4, 10, allow_shrink=True)
    with pytest.raises(FeedCheckError):
        check_feed(0, 10, allow_shrink=True)


def test_pack_of_four_ad_has_its_own_id_price_and_description():
    single = render_ad(cand(), 4810, "P1", S, TODAY)
    pack = render_ad(cand(), 4810, "P1", S, TODAY, pack=4)
    assert validate_ad(pack) == []
    assert (tag(pack, "Id"), tag(pack, "Price")) == ("100-x4", "19240") and pack.find("Quantity") is None
    assert tag(pack, "MultiItem") == "Да" and tag(single, "MultiItem") == "Да"
    assert "19240 руб. за 4 шт." in tag(pack, "Description") and "за 1 шт." not in tag(pack, "Description")
    assert (tag(single, "Id"), tag(single, "Price")) == ("100", "4810")


def test_pack_of_four_ad_is_told_apart_by_title_and_article():
    pack = render_ad(cand(code="TS72198"), 4810, "P1", S, TODAY, pack=4)
    single = render_ad(cand(code="TS72198"), 4810, "P1", S, TODAY)
    assert tag(single, "Title").endswith(" в наличии")                 # a single tire: «в наличии» at the end
    assert tag(pack, "Title") == tag(single, "Title")[:-len(" в наличии")] + " комплект 4 шт."   # a pack: its own ending, not «в наличии»
    assert "Артикул: TS72198-x4" in tag(pack, "Description") and "Артикул: TS72198\n" in tag(single, "Description")


def test_description_longer_than_avito_limit_makes_the_ad_invalid():
    ad = render_ad(cand(), 4810, "P1", S, TODAY, template="x" * 7501)
    assert "too_long:Description" in validate_ad(ad)
    assert validate_ad(render_ad(cand(), 4810, "P1", S, TODAY, template="x" * 7500)) == []


AVITO_TEMPLATE_TAGS = {
    "Address", "Id", "DateEnd", "ContactPhone", "SellerAddressID", "Category", "Description", "Images", "Image", "Title", "Condition",
    "Price", "MinimumOrderQuantity", "FreeTireService", "GoodsType", "AdType", "ProductType", "MultiItem", "WholesaleType", "Brand", "Model",
    "TireSectionWidth", "RimDiameter", "TireAspectRatio", "TireType", "LoadIndex", "SpeedIndex", "RunFlat", "TireYear", "Homologation",
}                                                           # tags of «Шины, диски и колёса - Легковые шины - Шаблон 09-10-2026.xml' that the feed uses


def test_the_ad_uses_only_tags_of_avitos_template_of_the_category():
    full = render_ad(cand(stock=12, runflat=True, speed="T", load="94", mnf_year="2025"), 4810, "P1", S, TODAY)
    used = {el.tag for el in full.iter() if el is not full}
    assert used <= AVITO_TEMPLATE_TAGS, sorted(used - AVITO_TEMPLATE_TAGS)
    assert {"FreeTireService", "RunFlat", "TireYear", "SpeedIndex", "LoadIndex", "MultiItem", "WholesaleType"} <= used


def test_homologation_names_the_carmaker_for_the_suppliers_code_and_skips_what_it_cannot_name():
    code = lambda text: tag(render_ad(cand(homologation=text), 1, "P1", S, TODAY), "Homologation")
    assert code("BMW") == "BMW" and code("MO") == "Mercedes-Benz" and code("MOE") == "Mercedes-Benz" and code("MO1") == "Mercedes-Benz"
    assert code("N0") == "Porsche" and code("AO") == "Audi" and code("L") == "Lamborghini"
    assert code("BMW MO") == "BMW" and code("bmw moe") == "BMW"                            # the first carmaker named
    assert code("K1") == "Ferrari"
    assert code("") is None and code("VY") is None and code("KS") is None                    # no carmaker behind these (or none found)


def test_every_homologation_value_we_can_write_is_checked_against_the_allowed_list():
    from tires_sync.feed import HOMOLOGATION
    ad = render_ad(cand(homologation="MO"), 1, "P1", S, TODAY)
    assert validate_ad(ad) == []
    ad.find("Homologation").text = "Marsmobile"
    assert "bad_value:Homologation" in validate_ad(ad)
    assert set(HOMOLOGATION.values()) == {"BMW", "Mercedes-Benz", "Porsche", "Audi", "Lamborghini", "Ferrari"}


def test_a_saved_snapshot_never_brings_back_tags_the_category_no_longer_has():
    """Held positions are published from their snapshot; one saved before the format fix still has <Quantity>, which Avito rejects."""
    from tires_sync.feed import OBSOLETE_TAGS, ad_from_text, ad_to_text
    old = ("<Ad><Id>1</Id><Title>t</Title><Price>5560</Price><Quantity>за 1 шт.</Quantity><MultiItem>Нет</MultiItem>"
           "<Quantity>за 4 шт.</Quantity><Images><Image url=\"https://x/1.jpg\" /></Images></Ad>")
    ad = ad_from_text(old)
    for name in OBSOLETE_TAGS:
        assert ad.find(name) is None
    assert ad.findtext("Id") == "1" and ad.findtext("MultiItem") == "Да" and ad.find("Images/Image") is not None and ad.findtext("Price") == "5560"
    assert "Quantity" not in ad_to_text(ad)
    held = ad_from_text("<Ad><Id>2</Id><MinimumOrderQuantity>по 4 шт.</MinimumOrderQuantity><MultiItem>Нет</MultiItem>"
                        "<WholesaleType>Да</WholesaleType><WholesaleMinOrderCount>10</WholesaleMinOrderCount><DiscountLadderList>x</DiscountLadderList><MultiItem>Нет</MultiItem></Ad>")
    assert held.find("MinimumOrderQuantity") is None and held.find("WholesaleMinOrderCount") is None and held.find("DiscountLadderList") is None   # the owner's terms, 2026-10-20
    assert [n.text for n in held.findall("MultiItem")] == ["Да"] and [n.text for n in held.findall("WholesaleType")] == ["Нет"]
