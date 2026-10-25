import pytest

from tires_sync.models import Season
from tires_sync.webmim import brand_key, parse_unload, season_from_text
from helpers import catalog_xml, commodity_xml


def test_parses_a_normal_row():
    result = parse_unload(catalog_xml(commodity_xml()), "T1")
    o = result.offers["100"]
    assert (o.brand_key, o.season, o.diameter, o.width, o.height) == ("IKON TYRES", Season.WINTER, 16, 205, 55)
    assert (o.stock, o.base_price, o.territory_rn) == (10, 5000.0, "T1")
    assert result.issues == {}


def test_other_territories_are_counted_not_parsed():
    xml = catalog_xml(commodity_xml(), commodity_xml(NNOMMODIF="200", TERRITORY_RN="T2"))
    result = parse_unload(xml, "T1")
    assert list(result.offers) == ["100"]
    assert result.other_territories == {"T2": 1}


def test_non_numeric_diameter_becomes_issue_not_offer():
    result = parse_unload(catalog_xml(commodity_xml(NNOMMODIF="101", SDIAMETR="16C")), "T1")
    assert "101" not in result.offers
    assert result.issues["101"] == ["bad_diameter:16C"]


def test_empty_stock_and_bad_price_are_issues():
    xml = catalog_xml(commodity_xml(NNOMMODIF="102", NREST=""), commodity_xml(NNOMMODIF="103", NPRICE_DELAYED="abc"))
    result = parse_unload(xml, "T1")
    assert result.issues["102"] == ["bad_stock:"]
    assert result.issues["103"] == ["bad_price:abc"]


def test_duplicate_goods_id_in_one_territory_is_dropped_as_issue():
    xml = catalog_xml(commodity_xml(NREST="5"), commodity_xml(NREST="300"))
    result = parse_unload(xml, "T1")
    assert "100" not in result.offers
    assert "duplicate_in_territory" in result.issues["100"]


def test_russian_brand_names_map_to_keys():
    assert brand_key("Кумхо") == "KUMHO"
    assert brand_key("Пирелли") == "PIRELLI"
    assert brand_key("Ikon Tyres") == "IKON TYRES"


def test_season_text():
    assert season_from_text("Зимняя") is Season.WINTER
    assert season_from_text("Летняя") is Season.SUMMER
    assert season_from_text("Всесезонная") is Season.ALL
    assert season_from_text("") is Season.UNKNOWN


def test_studded_and_runflat_flags():
    xml = catalog_xml(commodity_xml(SMODIFNAME="IKON 205/55/16 Test Ш.", SRUNFLAT="RunFlat"))
    o = parse_unload(xml, "T1").offers["100"]
    assert o.studded is True and o.runflat is True


@pytest.mark.parametrize("name, season, studded", [
    ("IKON 205/55/16 Test Ш.", "Зимняя", True),                         # the WebMIM mark of studs
    ("IKON 205/55/16 Test шип.", "Зимняя", True),
    ("IKON 205/55/16 Test Шипованная", "Зимняя", True),
    ("IKON 205/55/16 Test", "Зимняя шипованная", True),
    ("IKON 205/55/16 Test Зимние шипованные", "Зимние шипованные", True),   # «ние шип» holds «не шип»: a letter before «не» is no negation
    ("IKON 205/55/16 Test Внешипованная", "Зимняя", True),
    ("IKON 205/55/16 Test НЕШИП.", "Зимняя", False),
    ("IKON 205/55/16 Test Без Шипов", "Зимняя", False),
    ("IKON 205/55/16 Test нешип. Ш.", "Зимняя", True),                   # the supplier's own mark «Ш.» wins
    ("IKON 205/55/16 Test", "Зимняя", False),
    ("IKON 205/55/16 Test нешип.", "Зимняя", False),                     # the finding of the run «match»: «нешип.» was studded
    ("IKON 205/55/16 Test нешип", "Зимняя", False),
    ("IKON 205/55/16 Test (нешип.)", "Зимняя", False),
    ("IKON 205/55/16 Test не шип.", "Зимняя", False),
    ("IKON 205/55/16 Test Нешипованная", "Зимняя", False),
    ("IKON 205/55/16 Test не шипованные", "Зимняя", False),
    ("IKON 205/55/16 Test без шипов", "Зимняя", False),
    ("IKON 205/55/16 Test", "Зима нешип.", False),
    # run «match2»: a tire made for studs, sold without them, and «Шипы: нет» are no studs either
    ("IKON 205/55/16 Test под шип", "Зимняя", False),
    ("IKON 205/55/16 Test под шипы", "Зимняя", False),
    ("IKON 205/55/16 Test (под шип.)", "Зимняя", False),
    ("IKON 205/55/16 Test Шипуемая", "Зимняя", False),
    ("IKON 205/55/16 Test шипуемые", "Зимняя", False),
    ("IKON 205/55/16 Test Шипы: нет", "Зимняя", False),
    ("IKON 205/55/16 Test Шипы:нет", "Зимняя", False),
    ("IKON 205/55/16 Test шипы - нет", "Зимняя", False),
    ("IKON 205/55/16 Test шипы — нет", "Зимняя", False),
    ("IKON 205/55/16 Test шипы нет", "Зимняя", False),
    ("IKON 205/55/16 Test нет шипов", "Зимняя", False),
    ("IKON 205/55/16 Test", "Зимняя, шипы: нет", False),
    ("IKON 205/55/16 Test Шипы: да", "Зимняя", True),
    ("IKON 205/55/16 Test шипы есть", "Зимняя", True),
    ("IKON 205/55/16 Test шипуемая Ш.", "Зимняя", True),                 # the supplier's own mark «Ш.» wins
    ("IKON 205/55/16 Test Шипованная, под шип", "Зимняя", True),          # «шипованная» stays: a negation takes out its own words only
    ("IKON 205/55/16 Test шипы нетто", "Зимняя", True),                   # «нет» is a word of its own
    ("IKON 205/55/16 Test шип: нет", "Зимняя", False),
    ("IKON 205/55/16 Test шипованная нет", "Зимняя", True),               # «нет» after a studded word is no negation (the feed review)
])
def test_the_negations_of_studs_are_not_studs(name, season, studded):
    """«нешип», «не шип», «без шип…» are no studs (they hold «шип»); in the feed such a tire is «Зимние нешипованные»."""
    from tires_sync.feed import _tire_type
    o = parse_unload(catalog_xml(commodity_xml(SMODIFNAME=name, SMODIFNAME_ORIG=name, SSEASON=season)), "T1").offers["100"]
    assert o.studded is studded
    assert _tire_type(o.season, o.studded) == ("Зимние шипованные" if studded else "Зимние нешипованные")


def test_non_finite_prices_are_issues_not_crashes():
    xml = catalog_xml(commodity_xml(NNOMMODIF="1", NPRICE_DELAYED="nan"), commodity_xml(NNOMMODIF="2", NPRICE_DELAYED="inf"))
    result = parse_unload(xml, "T1")
    assert result.offers == {}
    assert result.issues["1"] == ["bad_price:nan"] and result.issues["2"] == ["bad_price:inf"]


def test_real_supplier_brand_spellings_map_to_reference_names():
    assert brand_key("Лауфенн") == "LAUFENN"
    assert brand_key("Мишелин") == "MICHELIN"
    assert brand_key("ТУНГА") == "TUNGA"


def test_runflat_is_also_read_from_the_name_and_the_model_not_only_from_the_runflat_column():
    def runflat(**kw):
        return parse_unload(catalog_xml(commodity_xml(**dict({"SRUNFLAT": ""}, **kw))), "T1").offers["100"].runflat
    assert runflat(SMODIFNAME_ORIG="Landsail  225/40/19  W 89 LS588  RunFlat") is True
    assert runflat(SMODIFNAME_ORIG="Bridgestone 245/45/19 Y 98 Turanza T005 RFT") is True
    assert runflat(SMODIFNAME_ORIG="Michelin 225/55/17 W 97 Primacy 4 ZP") is True
    assert runflat(SMODIFNAME_ORIG="Pirelli 245/40/20 Y 99 P Zero Run-Flat") is True
    assert runflat(SMODIFNAME_ORIG="Ikon  205/55/16  T 94 Test Model") is False
    assert runflat(SMODIFNAME_ORIG="Nokian 205/55/16 T 94 ZPLUS", SMODEL="Zplus") is False          # a word that only contains the letters


def test_the_homologation_code_of_the_supplier_is_kept_on_the_offer():
    result = parse_unload(catalog_xml(commodity_xml(SHOMOLOGATION="MO", NNOMMODIF="100"), commodity_xml(NNOMMODIF="101")), "T1")
    assert result.offers["100"].homologation == "MO" and result.offers["101"].homologation == ""


def test_a_runflat_column_that_says_no_is_not_runflat():
    def runflat(text):
        return parse_unload(catalog_xml(commodity_xml(SRUNFLAT=text)), "T1").offers["100"].runflat
    assert runflat("RunFlat") is True and runflat("Run Flat") is True
    assert runflat("Нет") is False and runflat("нет") is False and runflat("0") is False and runflat("-") is False and runflat("") is False
