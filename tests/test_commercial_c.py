"""Commercial tires of type «C» are published like passenger ones, «LT» is still not taken (owner's decision 2026-11-10, which replaces the
one of 2026-10-14 for «C»). A «C» position whose commercial model is not in the Avito reference is not published under the nearest other
model: it stays unmatched and waits on «Сопоставления»."""
from __future__ import annotations

from datetime import date

import pytest

from helpers import catalog_xml, commodity_xml, make_offer, make_settings
from tires_sync.candidates import Candidate, evaluate
from tires_sync.catalog import commercial_marks, keeps_commercial_mark, normalize_model, resolve_model
from tires_sync.feed import render_ad, validate_ad
from tires_sync.filters import exclusion_reason
from tires_sync.mappings import pending_mappings
from tires_sync.webmim import parse_unload

REF = {
    "Ikon Tyres": frozenset(["Autograph Ice C3", "Autograph Ice 9", "Autograph Snow C4", "Autograph Snow 3", "Character Ice 8",
                             "Character Ice 7", "Character Eco", "Character Aqua", "Test Model"]),
    "Cordiant": frozenset(["Business CS-2", "Business CA", "Business CA-2", "Business CW 2"]),
    "Kumho": frozenset(["Winter PorTran CW51", "Winter Portran CW11", "WinterCraft Ice WI51"]),
    "Viatti": frozenset(["Vettore Brina V-525", "Vettore Inverno V-524"]),
    "Landsail": frozenset(["Snow Star", "Ice Star iS37", "Ice Star iS33"]),
    "Nexen": frozenset(["Winguard WinSpike SUV", "Winguard WinSpike"]),
}
# What the pairs table may hold: pairs someone accepted by hand. The first three are the nearest OTHER model (the commercial one is not in
# the reference); the rest are right although the name is written differently.
WRONG = {("Ikon Tyres", "Ikon Autograph Ice C4"): "Autograph Ice C3", ("Ikon Tyres", "Ikon Character Ice C2"): "Character Ice 8",
         ("Ikon Tyres", "Ikon Character Eco C2"): "Character Eco"}
RIGHT_SAVED = {("Cordiant", "Бизнес CA-1"): "Business CA", ("Cordiant", "Бизнес CW-2"): "Business CW 2",
               ("Kumho", "CW-51"): "Winter PorTran CW51", ("Kumho", "CW-11"): "Winter Portran CW11"}
CONFIRMED = {(brand, normalize_model(model)): to for (brand, model), to in {**WRONG, **RIGHT_SAVED}.items()}
RIGHT = [
    ("Ikon Tyres", "Ikon Autograph Snow C4", "Autograph Snow C4"), ("Cordiant", "BUSINESS CS-2", "Business CS-2"),
    ("Cordiant", "Бизнес CA-1", "Business CA"), ("Cordiant", "Бизнес CW-2", "Business CW 2"),
    ("Kumho", "CW-51", "Winter PorTran CW51"), ("Kumho", "CW-11", "Winter Portran CW11"),
    ("Viatti", "Vettore Brina V-525", "Vettore Brina V-525"), ("Viatti", "Vettore Inverno V-524", "Vettore Inverno V-524"),
    ("Landsail", "SNOW STAR", "Snow Star"), ("Landsail", "ice STAR iS37", "Ice Star iS37"),
    ("Nexen", "WINGUARD WINSPIKE SUV", "Winguard WinSpike SUV"),
]


def test_c_is_taken_from_the_unload_and_lt_is_not():
    xml = catalog_xml(
        commodity_xml(NNOMMODIF="1", SCARTYPE=""), commodity_xml(NNOMMODIF="2", SCARTYPE="C"), commodity_xml(NNOMMODIF="3", SCARTYPE="lt"),
        commodity_xml(NNOMMODIF="4", SCARTYPE=" c ", NREST="не число"), commodity_xml(NNOMMODIF="5", SCARTYPE="LT", NREST="не число"))
    parsed = parse_unload(xml, "T1")
    assert set(parsed.offers) == {"1", "2"} and parsed.offers["2"].car_type == "C"
    assert set(parsed.issues) == {"4"}                      # a broken «C» row is reported like any other row; a broken «LT» one is not
    assert parsed.commercial_skipped == 2


def test_c_is_not_excluded_and_lt_is():
    settings = make_settings()
    assert exclusion_reason(make_offer(car_type="C"), settings) is None
    assert exclusion_reason(make_offer(car_type="c"), settings) is None
    assert exclusion_reason(make_offer(car_type="LT"), settings) == "commercial"
    assert exclusion_reason(make_offer(car_type="lt"), settings) == "commercial"


@pytest.mark.parametrize("brand, model, wrong", [(b, m, to) for (b, m), to in WRONG.items()])
def test_a_c_tire_is_never_given_the_nearest_other_model(brand, model, wrong):
    assert resolve_model(brand, model, {}, REF, commercial=True) is None                 # the name itself finds nothing
    assert resolve_model(brand, model, CONFIRMED, REF, commercial=True) is None          # and a saved pair with another model is not taken
    assert not keeps_commercial_mark(model, wrong)


@pytest.mark.parametrize("brand, model, expected", RIGHT)
def test_right_pairs_of_c_tires_stay(brand, model, expected):
    assert resolve_model(brand, model, CONFIRMED, REF, commercial=True) == expected
    assert resolve_model(brand, model, CONFIRMED, REF) == expected                       # the same as for a passenger tire


def test_a_saved_pair_of_a_c_tire_that_keeps_the_mark_or_has_none_works_as_before():
    saved = {("Ikon Tyres", normalize_model("Autograph Ice C-3 new")): "Autograph Ice C3", ("Ikon Tyres", normalize_model("Other")): "Test Model"}
    assert resolve_model("Ikon Tyres", "Autograph Ice C-3 new", saved, REF, commercial=True) == "Autograph Ice C3"
    assert resolve_model("Ikon Tyres", "Other", saved, REF, commercial=True) == "Test Model"


def test_the_model_appears_in_the_reference_and_the_c_tire_takes_it_over_the_wrong_saved_pair():
    ref = dict(REF, **{"Ikon Tyres": REF["Ikon Tyres"] | {"Autograph Ice C4"}})
    assert resolve_model("Ikon Tyres", "Ikon Autograph Ice C4", CONFIRMED, ref, commercial=True) == "Autograph Ice C4"


def test_marks_are_read_only_from_a_lone_c_with_a_number():
    assert commercial_marks("Ikon Autograph Ice C4") == {"C4"} and commercial_marks("ice с-4") == {"C4"}     # the second «с» is Cyrillic
    for name in ("BUSINESS CS-2", "Бизнес CA-1", "CW-51", "Winter PorTran CW51", "Vettore Brina V-525", "ice STAR iS37", "Character Ice 8"):
        assert commercial_marks(name) == frozenset()


def test_guard_for_passenger_tires_matching_is_what_it_was():
    """The watchman: without `commercial` (a passenger tire, every caller before 2026-11-10) a saved pair is taken as it is, even these."""
    for (brand, model), to in WRONG.items():
        assert resolve_model(brand, model, CONFIRMED, REF) == to
    ev = evaluate({"1": make_offer(goods_id="1", model_raw="Ikon Autograph Ice C4")}, {}, make_settings(), REF, CONFIRMED)
    assert ev.ok["1"].avito_model == "Autograph Ice C3" and ev.unmatched == {}


def test_a_c_position_without_its_model_is_unmatched_and_waits_on_the_mappings():
    offers = {"1": make_offer(goods_id="1", model_raw="Ikon Autograph Ice C4", car_type="C"),
              "2": make_offer(goods_id="2", model_raw="Ikon Autograph Snow C4", car_type="C")}
    ev = evaluate(offers, {}, make_settings(), REF, CONFIRMED)
    assert set(ev.ok) == {"2"} and ev.ok["2"].avito_model == "Autograph Snow C4"
    assert ev.unmatched == {"1": ("Ikon Tyres", "Ikon Autograph Ice C4")} and ev.problems["1"] == ["model_needs_mapping:Ikon Autograph Ice C4"]
    assert [(p.brand, p.model_raw, p.goods_ids) for p in pending_mappings(ev, REF)] == [("Ikon Tyres", "Ikon Autograph Ice C4", ["1"])]


def test_a_c_candidate_is_priced_and_passes_the_stock_threshold_like_a_passenger_tire():
    from tires_sync.decide import decide
    settings = make_settings()
    offers = {"1": make_offer(goods_id="1", car_type="C", stock=settings.min_stock), "2": make_offer(goods_id="2", car_type="C", stock=settings.min_stock - 1),
              "3": make_offer(goods_id="3", stock=settings.min_stock), "4": make_offer(goods_id="4", stock=settings.min_stock - 1)}
    ev = evaluate(offers, {}, settings, REF, {})
    assert ev.ok["1"].price == ev.ok["3"].price
    made = {d.goods_id: (d.action.value, d.reason) for d in decide(ev, {}, set(), {}, settings, date(2026, 11, 10)).decisions}
    assert made["1"] == made["3"] and made["1"][0] == "publish"
    assert made["2"] == made["4"] and made["2"][0] != "publish"


def test_the_ad_of_a_c_tire_has_the_first_load_index_and_the_owners_title():
    offer = make_offer(goods_id="7", car_type="C", name="CORDIANT 205/65/16 R 107/105C Бизнес CA-1",
                       name_orig="CORDIANT  205/65/16  R 107/105C  Бизнес CA-1", brand_raw="Cordiant", brand_key="CORDIANT",
                       model_raw="Бизнес CA-1", width=205, height=65, diameter=16, speed="R", load="107/105")
    ad = render_ad(Candidate("7", offer, 6000, "Cordiant", "Business CA"), 6000, "P1", make_settings(), date(2026, 11, 10))
    assert ad.findtext("LoadIndex") == "107" and ad.findtext("SpeedIndex") == "R" and ad.findtext("Model") == "Business CA"
    assert ad.findtext("Title") == "CORDIANT 205 65 R16 R 107/105C Бизнес CA-1 в наличии"
    assert validate_ad(ad) == []
    glued = render_ad(Candidate("7", make_offer(goods_id="7", car_type="C", load="112/110C"), 6000, "Ikon Tyres", "Test Model"), 6000, "P1",
                      make_settings(), date(2026, 11, 10))
    assert glued.findtext("LoadIndex") == "112"


def _mappings_page(tmp_path, monkeypatch=None):
    from tires_sync import store
    from test_mappings_review_fixes import setup
    app, client, _tok = setup(tmp_path)
    (tmp_path / "reference.txt").write_text(
        '<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n<model name="Autograph Ice C3"/>\n<model name="Autograph Snow C4"/>\n</make>\n</Shiny>',
        encoding="utf-8")
    open(app.config["UNLOAD_PATH"], "wb").write(catalog_xml(
        commodity_xml(NNOMMODIF="1", SMODEL="Ikon Autograph Ice C4", SCARTYPE="C", SMNFCODE="K1"),
        commodity_xml(NNOMMODIF="2", SMODEL="Ikon Autograph Snow C4", SCARTYPE="C", SMNFCODE="K2")))
    return app, client, store


def test_the_mappings_page_shows_the_waiting_c_model_and_never_hints_at_the_other_model(tmp_path):
    app, client, store = _mappings_page(tmp_path)
    page = client.get("/mappings?a=all").get_data(as_text=True)
    row = page.split('class="t-name">Ikon Autograph Ice C4')[1].split("</tr>")[0]
    assert ">C</span>" in row and "коммерческая шина: без пары не публикуется, ждёт решения владельца" in row
    assert "пара с моделью без «C4» в названии не действует" in row
    assert 'value="Autograph Ice C3"' not in row                                         # the nearest other model is not offered as the hint
    found = page.split('class="t-name">Ikon Autograph Snow C4')[1].split("</tr>")[0]
    assert "без пары не публикуется" not in found and "Autograph Snow C4" in found


def test_a_wrong_pair_saved_for_a_c_model_leaves_it_waiting_on_the_page(tmp_path):
    app, client, store = _mappings_page(tmp_path)
    with app.app_context():
        from tires_sync.panel.common import get_conn
        store.upsert_mapping(get_conn(), "Ikon Tyres", "Ikon Autograph Ice C4", "Autograph Ice C3")
    page = client.get("/mappings").get_data(as_text=True)                                 # the list of models without a pair
    assert 'class="t-name">Ikon Autograph Ice C4' in page and 'class="t-name">Ikon Autograph Snow C4' not in page


@pytest.mark.xfail(strict=True, reason="feed_check rule `commercial` (critical for our own feed) still stops a feed with a «C» ad: "
                                       "the rule is changed only by the owner's word (docs/feed-check-rules.md)")
def test_a_feed_with_a_c_ad_passes_the_check_before_publication(tmp_path):
    from helpers import settings_dict
    from tires_sync import connections, cycle, store
    from tires_sync.catalog import parse_reference
    from tires_sync.settings_store import init_settings
    ref = parse_reference('<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>')
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(guard_min=10), "2026-11-10T12:00:00")
    connections.create_account(conn, "A", "2026-11-10T12:00:00", "o")
    xml = catalog_xml(commodity_xml(NNOMMODIF="1"), commodity_xml(
        NNOMMODIF="2", SCARTYPE="C", SLOAD="107/105", SMODIFNAME="IKON 205/65/16 R 107/105C Test Model",
        SMODIFNAME_ORIG="Ikon  205/65/16  R 107/105C Test Model"))
    results = cycle.run_for_accounts(conn, xml, ref, date(2026, 11, 10), tmp_path / "feed")
    assert results[0].ok, results[0].error


def test_the_clients_shop_and_the_panel_catalog_open_with_a_c_position(tmp_path):
    from test_shop import make_shop, page, prices
    rows = (commodity_xml(NNOMMODIF="1", NPRICE_DELAYED="5000"),
            commodity_xml(NNOMMODIF="2", SCARTYPE="C", SLOAD="112/110", NPRICE_DELAYED="6000", SMODIFNAME="IKON 205/65/16 R 112/110C Test Model",
                          SMODIFNAME_ORIG="Ikon  205/65/16  R 112/110C Test Model", SHEIGHT="65", SSPEED="R"))
    app, client, conn, _sid = make_shop(tmp_path, rows=rows)
    assert sorted(prices(page(client))) == [5500, 6600]                                  # the «C» position is sold like the passenger one: +10 %
    assert page(client, "/shop?q=205/65") != ""
