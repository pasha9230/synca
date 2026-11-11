"""Commercial tires «C», the owner 2026-11-11 (it changes «Все «C»» of 2026-11-10): a «C» whose model is in the passenger reference of Avito
(its very name, as a passenger model is found without a pair) is published in the passenger section like any passenger tire; any other «C»
goes to the section «Шины для грузовиков и спецтехники» as before (tests/test_truck_section.py). «LT» is not published. The price, the stock
threshold, the Id are those of a passenger tire in both sections."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import date

import pytest

from helpers import catalog_xml, commodity_xml, make_offer, make_settings
from tires_sync import feed_check, truck
from tires_sync.candidates import Candidate, evaluate
from tires_sync.catalog import normalize_model, parse_reference
from tires_sync.cycle import TEMPLATE_PATH
from tires_sync.feed import ad_to_text, render_ad, validate_ad
from tires_sync.filters import exclusion_reason
from tires_sync.mappings import pending_mappings
from tires_sync.same_tire import identity, identity_of_ad

TODAY = date(2026, 11, 11)
BRANDS = ("IKON TYRES", "CORDIANT", "KUMHO", "GISLAVED", "LANDSAIL")
# the owner's examples: these models are in the passenger reference of Avito
REF = parse_reference(
    '<Shiny>\n'
    '<make name="Cordiant">\n<model name="Business CS"/>\n<model name="Business CA 2"/>\n<model name="Business CW 2"/>\n</make>\n'
    '<make name="Kumho">\n<model name="Winter PorTran CW51"/>\n<model name="Winter Portran CW11"/>\n<model name="PorTran KC53"/>\n</make>\n'
    '<make name="Gislaved">\n<model name="Nord Frost Van 2"/>\n</make>\n'
    '<make name="Landsail">\n<model name="LSV88+"/>\n<model name="Ice Star IS37"/>\n</make>\n'
    '<make name="Ikon Tyres">\n<model name="Autograph Eco C3"/>\n<model name="Test Model"/>\n</make>\n'
    '</Shiny>')
PASSENGER_TEMPLATE = feed_check.template_tags_of(TEMPLATE_PATH)


def settings():
    return make_settings(enabled=[[b, s] for b in BRANDS for s in ("winter", "summer", "all")],
                         pricing={"%s|%s" % (b, s): {"pct_small": 10.0, "pct_large": 10.0} for b in BRANDS for s in ("winter", "summer", "all")})


def c_offer(goods_id="7", brand_key="CORDIANT", brand_raw="Cordiant", model_raw="Business CS", car_type="C", **kw):
    values = dict(goods_id=goods_id, car_type=car_type, brand_key=brand_key, brand_raw=brand_raw, model_raw=model_raw,
                  name="%s 205/65/16 R 107/105C %s" % (brand_raw.upper(), model_raw),
                  name_orig="%s  205/65/16  R 107/105C  %s" % (brand_raw, model_raw), width=205, height=65, diameter=16, speed="R",
                  load="107/105", stock=12)
    values.update(kw)
    return make_offer(**values)


PASSENGER_C = [
    ("CORDIANT", "Cordiant", "Business CS", "Business CS"), ("CORDIANT", "Cordiant", "BUSINESS CA-2", "Business CA 2"),
    ("CORDIANT", "Cordiant", "Business CW 2", "Business CW 2"), ("KUMHO", "Kumho", "Winter PorTran CW51", "Winter PorTran CW51"),
    ("KUMHO", "Kumho", "WINTER PORTRAN CW11", "Winter Portran CW11"), ("KUMHO", "Kumho", "PorTran KC53", "PorTran KC53"),
    ("GISLAVED", "Gislaved", "Nord Frost Van 2", "Nord Frost Van 2"), ("LANDSAIL", "Landsail", "LSV88+", "LSV88+"),
    ("LANDSAIL", "Landsail", "Ice Star iS37", "Ice Star IS37"), ("IKON TYRES", "Ikon Tyres", "Ikon Autograph Eco C3", "Autograph Eco C3"),
]


@pytest.mark.parametrize("brand_key, brand_raw, model_raw, expected", PASSENGER_C)
def test_a_c_with_a_model_of_the_passenger_reference_is_a_passenger_ad(brand_key, brand_raw, model_raw, expected):
    ev = evaluate({"7": c_offer(brand_key=brand_key, brand_raw=brand_raw, model_raw=model_raw)}, {}, settings(), REF, {})
    cand = ev.ok["7"]
    assert (cand.avito_model, cand.truck_section) == (expected, False) and not ev.truck and not ev.unmatched
    ad = render_ad(cand, 6000, "P1", make_settings(), TODAY)
    assert ad.findtext("ProductType") == "Легковые шины" and ad.findtext("Model") == expected
    assert validate_ad(ad) == []


def test_the_passenger_ad_of_a_c_is_the_ad_of_the_same_tire_without_c_field_by_field():
    cand = evaluate({"7": c_offer()}, {}, settings(), REF, {}).ok["7"]
    as_passenger = Candidate("7", replace(cand.offer, car_type=""), cand.price, cand.avito_brand, cand.avito_model)
    for pack in (1, 4):
        ours = render_ad(cand, 6000, "P1", make_settings(), TODAY, pack=pack)
        plain = render_ad(as_passenger, 6000, "P1", make_settings(), TODAY, pack=pack)
        assert ad_to_text(ours) == ad_to_text(plain)
    ad = render_ad(cand, 6000, "P1", make_settings(), TODAY)
    assert ad.findtext("MultiItem") == "Да" and ad.findtext("FreeTireService") == "Нет" and ad.findtext("LoadIndex") == "107"
    assert ad.find("Quantity") is None and ad.find("VehicleType") is None
    assert set(c.tag for c in ad) <= PASSENGER_TEMPLATE


def test_a_c_without_a_passenger_model_goes_to_the_truck_section_as_before():
    offers = {"1": c_offer(goods_id="1", model_raw="Business CS 501"),                        # in the truck list only
              "2": c_offer(goods_id="2", brand_key="IKON TYRES", brand_raw="Ikon Tyres", model_raw="Ikon Character Ice C2"),
              "3": c_offer(goods_id="3", brand_key="IKON TYRES", brand_raw="Ikon Tyres", model_raw="Ikon Autograph Ice C9")}   # nowhere
    ev = evaluate(offers, {}, settings(), REF, {})
    assert (ev.ok["1"].avito_model, ev.ok["1"].truck_section) == ("Business CS 501", True)
    assert (ev.ok["2"].avito_model, ev.ok["2"].truck_section) == ("Character Ice C2", True)
    for code in ("1", "2"):
        ad = render_ad(ev.ok[code], 6000, "P1", make_settings(), TODAY)
        assert ad.findtext("ProductType") == truck.PRODUCT_TYPE and ad.findtext("VehicleType") == truck.VEHICLE_TYPE
    assert ev.unmatched == {"3": ("Ikon Tyres", "Ikon Autograph Ice C9")} and ev.truck == {"3"}
    assert [(p.model_raw, p.section) for p in pending_mappings(ev, REF)] == [("Ikon Autograph Ice C9", truck.SECTION)]


def test_only_the_very_name_counts_never_a_nearer_model_or_a_saved_pair():
    ref = {"Ikon Tyres": frozenset(["Autograph Ice C3", "Autograph Eco C3 Plus"]), "Cordiant": frozenset(["Business CS"])}
    confirmed = {("Ikon Tyres", normalize_model("Ikon Autograph Ice C4")): "Autograph Ice C3",       # another number
                 ("Cordiant", normalize_model("Бизнес CS")): "Business CS"}                          # a passenger pair: not read for a «C»
    assert truck.passenger_model("Ikon Tyres", "Ikon Autograph Ice C4", ref) is None
    assert truck.passenger_model("Ikon Tyres", "Autograph Eco C3", ref) is None                      # a part of a longer name
    assert truck.passenger_model("Cordiant", "Бизнес CS", ref) is None
    assert truck.passenger_model(None, "Business CS", ref) is None
    for raw in ("Ikon Autograph Ice C4", "Бизнес CS"):
        brand_key = "IKON TYRES" if raw.startswith("Ikon") else "CORDIANT"
        assert truck.resolve_c(brand_key, raw, ref, confirmed)[2] is True


def test_lt_is_still_not_published():
    assert exclusion_reason(c_offer(car_type="LT"), settings()) == "commercial"
    ev = evaluate({"7": c_offer(car_type="LT")}, {}, settings(), REF, {})
    assert ev.excluded == {"7": "commercial"} and not ev.ok


def test_guard_passenger_tires_are_what_they_were():
    offers = {"1": make_offer(goods_id="1", brand_key="CORDIANT", brand_raw="Cordiant", model_raw="Business CS"),
              "2": make_offer(goods_id="2", model_raw="Test Model"), "3": make_offer(goods_id="3", model_raw="Character Ice C2")}
    ev = evaluate(offers, {}, settings(), REF, {})
    assert {g: (c.avito_model, c.truck_section) for g, c in ev.ok.items()} == {"1": ("Business CS", False), "2": ("Test Model", False)}
    assert ev.unmatched == {"3": ("Ikon Tyres", "Character Ice C2")} and not ev.truck
    built = render_ad(ev.ok["2"], 5000, "P1", make_settings(), TODAY)
    direct = render_ad(Candidate("2", offers["2"], ev.ok["2"].price, "Ikon Tyres", "Test Model"), 5000, "P1", make_settings(), TODAY)
    assert ad_to_text(built) == ad_to_text(direct) and built.findtext("ProductType") == "Легковые шины"


def test_the_price_the_threshold_and_the_id_of_a_c_are_those_of_a_passenger_tire():
    from tires_sync.decide import decide
    s = settings()
    offers = {"1": c_offer(goods_id="1", stock=s.min_stock), "2": c_offer(goods_id="2", model_raw="Business CS 501", stock=s.min_stock),
              "3": make_offer(goods_id="3", brand_key="CORDIANT", brand_raw="Cordiant", model_raw="Business CS", stock=s.min_stock),
              "4": c_offer(goods_id="4", stock=s.min_stock - 1)}
    ev = evaluate(offers, {}, s, REF, {})
    assert ev.ok["1"].price == ev.ok["2"].price == ev.ok["3"].price
    made = {d.goods_id: d.action.value for d in decide(ev, {}, set(), {}, s, TODAY).decisions}
    assert made["1"] == made["2"] == made["3"] == "publish" and made["4"] != "publish"
    assert [render_ad(ev.ok[g], 6000, "P1", make_settings(), TODAY).findtext("Id") for g in ("1", "2")] == ["1", "2"]


def test_the_key_of_one_tire_of_a_passenger_c_is_the_key_of_its_saved_ad():
    cand = evaluate({"7": c_offer()}, {}, settings(), REF, {}).ok["7"]
    key = identity(cand)
    assert key is not None and "truck" not in key
    assert identity_of_ad(ad_to_text(render_ad(cand, 6000, "P1", make_settings(), TODAY))) == key


# --- the check before publication -------------------------------------------------------------------------------------------------


def ctx(**kw):
    base = dict(mode="generated", template_tags=PASSENGER_TEMPLATE, reference=REF, stocks={"1": 12, "2": 12, "3": 12}, min_stock=4,
                phone="+70000000000", pack4=True)
    base.update(kw)
    return feed_check.Context(**base)


def test_a_feed_with_a_passenger_c_a_truck_c_and_a_passenger_tire_passes_the_check():
    offers = {"1": c_offer(goods_id="1"), "2": c_offer(goods_id="2", model_raw="Business CS 501"),
              "3": make_offer(goods_id="3", model_raw="Test Model", stock=12)}
    ev = evaluate(offers, {}, settings(), REF, {})
    ads = [render_ad(ev.ok[g], 6000, "P1", make_settings(), TODAY, pack=p) for g in ("1", "2", "3") for p in (1, 4)]
    assert feed_check.check_feed(ads, ctx()) == []


def test_a_c_in_the_passenger_section_under_a_model_not_of_the_reference_is_still_stopped():
    ad = render_ad(evaluate({"1": c_offer(goods_id="1")}, {}, settings(), REF, {}).ok["1"], 6000, "P1", make_settings(), TODAY)
    ad.find("Model").text = "Business CS 501"                              # in the truck list, not in the passenger reference
    rules = {f.rule: f.level for f in feed_check.check_feed([ad], ctx())}
    assert rules.get("commercial") == "critical" and "model_in_reference" in rules
    assert "commercial" in {f.rule for f in feed_check.check_feed([render_ad(
        evaluate({"1": c_offer(goods_id="1")}, {}, settings(), REF, {}).ok["1"], 6000, "P1", make_settings(), TODAY)], ctx(reference={}))}


def test_the_cycle_puts_each_c_into_its_section_and_passes_the_gate(tmp_path):
    from helpers import settings_dict
    from tires_sync import connections, cycle, store
    from tires_sync.settings_store import init_settings
    ref = parse_reference('<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n<model name="Autograph Eco C3"/>\n</make>\n</Shiny>')
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(guard_min=10), "2026-11-11T12:00:00")
    connections.create_account(conn, "A", "2026-11-11T12:00:00", "o")
    c_row = lambda code, model: commodity_xml(
        NNOMMODIF=code, SCARTYPE="C", SLOAD="107/105", SSPEED="R", SHEIGHT="65", SMODEL=model,
        SMODIFNAME="IKON 205/65/16 R 107/105C %s" % model, SMODIFNAME_ORIG="Ikon  205/65/16  R 107/105C %s" % model)
    xml = catalog_xml(commodity_xml(NNOMMODIF="1"), c_row("2", "Ikon Character Ice C2"), c_row("3", "Ikon Autograph Eco C3"),
                      commodity_xml(NNOMMODIF="4", SCARTYPE="LT", SHEIGHT="65"))
    results = cycle.run_for_accounts(conn, xml, ref, TODAY, tmp_path / "feed")
    assert results[0].ok, results[0].error
    token = connections.list_accounts(conn)[0]["feed_token"]
    ads = {a.findtext("Id"): a for a in ET.parse(str(tmp_path / "feed" / token / "next" / "avito_feed.xml")).getroot().findall("Ad")}
    assert set(ads) == {"1", "2", "3"}                                      # «LT» is not published
    assert ads["2"].findtext("ProductType") == truck.PRODUCT_TYPE and ads["2"].findtext("Model") == "Character Ice C2"
    assert ads["3"].findtext("ProductType") == "Легковые шины" and ads["3"].findtext("Model") == "Autograph Eco C3"
    assert ads["1"].findtext("ProductType") == "Легковые шины"


def test_the_list_position_to_section_for_a_look_on_live_data():
    offers = {"1": c_offer(goods_id="1"), "2": c_offer(goods_id="2", model_raw="Business CS 501"),
              "3": c_offer(goods_id="3", brand_key="IKON TYRES", brand_raw="Ikon Tyres", model_raw="Ikon Autograph Ice C9"),
              "4": make_offer(goods_id="4"), "5": c_offer(goods_id="5", car_type="LT")}
    rows = {r["code"]: (r["section"], r["avito_model"]) for r in truck.sections_of(offers, REF, {})}
    assert rows == {"1": (truck.PASSENGER_LABEL, "Business CS"), "2": (truck.SECTION_LABEL, "Business CS 501"),
                    "3": (truck.SECTION_LABEL, None)}


# --- «Сопоставления» ---------------------------------------------------------------------------------------------------------------


def test_the_mappings_page_says_where_each_c_goes_and_a_passenger_c_waits_for_nothing(tmp_path):
    from test_mappings_review_fixes import setup
    app, client, _tok = setup(tmp_path)
    (tmp_path / "reference.txt").write_text(
        '<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n<model name="Autograph Eco C3"/>\n</make>\n</Shiny>', encoding="utf-8")
    open(app.config["UNLOAD_PATH"], "wb").write(catalog_xml(
        commodity_xml(NNOMMODIF="1", SMODEL="Ikon Character Ice C3", SCARTYPE="C", SMNFCODE="K1"),
        commodity_xml(NNOMMODIF="2", SMODEL="Ikon Autograph Eco C3", SCARTYPE="C", SMNFCODE="K2")))
    page = client.get("/mappings?a=all").get_data(as_text=True)
    passenger = page.split('class="t-name">Ikon Autograph Eco C3')[1].split("</tr>")[0]
    assert truck.PASSENGER_LABEL in passenger and truck.SECTION_LABEL not in passenger
    assert "без пары не публикуется" not in passenger and 'name="section"' not in passenger and "сопоставлена" in passenger
    waiting = page.split('class="t-name">Ikon Character Ice C3')[1].split("</tr>")[0]
    assert truck.SECTION_LABEL in waiting and "без пары не публикуется" in waiting and 'name="section" value="truck"' in waiting
    unmatched = client.get("/mappings").get_data(as_text=True)              # the list of models without a pair
    assert 'class="t-name">Ikon Character Ice C3' in unmatched and 'class="t-name">Ikon Autograph Eco C3' not in unmatched


def test_the_command_lists_the_sections_and_leaves_the_base_as_it_was(tmp_path, capsys):
    from helpers import settings_dict
    from tires_sync import cli, store
    from tires_sync.settings_store import init_settings
    db = tmp_path / "t.db"
    conn = store.connect(db)
    init_settings(conn, settings_dict(), "2026-11-11T12:00:00")
    conn.commit()
    conn.close()
    before = db.read_bytes()
    (tmp_path / "ref.txt").write_text('<Shiny>\n<make name="Ikon Tyres">\n<model name="Autograph Eco C3"/>\n</make>\n</Shiny>', encoding="utf-8")
    c_row = lambda code, model: commodity_xml(NNOMMODIF=code, SCARTYPE="C", SMODEL=model)
    (tmp_path / "unload.xml").write_bytes(catalog_xml(commodity_xml(NNOMMODIF="1"), c_row("2", "Ikon Character Ice C2"),
                                                      c_row("3", "Ikon Autograph Eco C3"), c_row("4", "Ikon Нет Такой")))
    code = cli.main(["commercial-sections", "--db", str(db), "--unload", str(tmp_path / "unload.xml"), "--reference", str(tmp_path / "ref.txt"),
                     "--dry-run"])
    out = capsys.readouterr().out
    assert code == 0 and db.read_bytes() == before
    lines = {line.split("\t")[0]: line.split("\t")[3:] for line in out.splitlines() if "\t" in line}
    assert lines == {"2": [truck.SECTION_LABEL, "Ikon Tyres / Character Ice C2"], "3": [truck.PASSENGER_LABEL, "Ikon Tyres / Autograph Eco C3"],
                     "4": [truck.SECTION_LABEL, "ждёт пары"]}
    assert "всего «C»: 3; легковой раздел: 1; грузовой раздел: 2 (из них ждут пары: 1)" in out
