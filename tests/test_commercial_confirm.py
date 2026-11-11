"""Commercial tires «C», the owner 2026-11-11: on «Сопоставления» the owner may confirm a model of the PASSENGER reference for a «C»; with it the
position goes to the passenger section (the passenger template of fields). Without it, as before (tests/test_commercial_split.py).
The order: the confirmed passenger pair > the very name in the passenger reference > the confirmed truck pair > waits for a pair.
A position that already has an ad in the truck section in this account (published, or taken off and kept with its Id) stays there:
Avito does not move an ad to another category under the same Id (store.truck_bound_codes, truck.resolve_c)."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date

import pytest

from helpers import catalog_xml, commodity_xml
from tires_sync import store, truck
from tires_sync.candidates import evaluate
from tires_sync.catalog import normalize_model
from tires_sync.feed import render_ad, validate_ad
from tires_sync.mappings import MappingError, confirm_mapping
from tires_sync.models import PublishedItem

from test_commercial_split import REF, c_offer, settings

TODAY = date(2026, 11, 11)
NOW = "2026-11-11T12:00:00"

# the owner's examples: another spelling of a passenger model, the very name is not in the passenger reference
OTHER_SPELLING = [
    ("KUMHO", "Kumho", "CW-51", "Winter PorTran CW51"), ("KUMHO", "Kumho", "CW-11", "Winter Portran CW11"),
    ("KUMHO", "Kumho", "KC-53", "PorTran KC53"), ("CORDIANT", "Cordiant", "Бизнес CA-2", "Business CA 2"),
    ("CORDIANT", "Cordiant", "Бизнес CW-2", "Business CW 2"), ("GISLAVED", "Gislaved", "NORD FROST Van 2 SD 8PR", "Nord Frost Van 2"),
]


def passenger_pair(brand, model_raw, avito_model):
    return {(truck.passenger_pair_brand(brand), normalize_model(model_raw)): avito_model}


def truck_pair(brand, model_raw, avito_model):
    return {(truck.pair_brand(brand), normalize_model(model_raw)): avito_model}


def truck_ad(goods_id):
    return "<Ad><Id>%s</Id><ProductType>%s</ProductType><Title>t</Title></Ad>" % (goods_id, truck.PRODUCT_TYPE)


def passenger_ad(goods_id):
    return "<Ad><Id>%s</Id><ProductType>Легковые шины</ProductType><Title>t</Title></Ad>" % goods_id


# --- the section ------------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("brand_key, brand_raw, model_raw, expected", OTHER_SPELLING)
def test_without_a_confirmed_pair_another_spelling_goes_to_the_truck_section(brand_key, brand_raw, model_raw, expected):
    assert truck.resolve_c(brand_key, model_raw, REF, {})[2] is True


@pytest.mark.parametrize("brand_key, brand_raw, model_raw, expected", OTHER_SPELLING)
def test_a_confirmed_passenger_model_puts_the_c_in_the_passenger_section_with_its_fields(brand_key, brand_raw, model_raw, expected):
    offer = c_offer(brand_key=brand_key, brand_raw=brand_raw, model_raw=model_raw)
    ev = evaluate({"7": offer}, {}, settings(), REF, passenger_pair(brand_raw, model_raw, expected))
    cand = ev.ok["7"]
    assert (cand.avito_model, cand.truck_section) == (expected, False) and not ev.truck and not ev.unmatched
    ad = render_ad(cand, cand.price, "P1", settings(), TODAY)
    assert ad.findtext("ProductType") == "Легковые шины" and ad.findtext("Model") == expected
    assert ad.findtext("VehicleType") is None and validate_ad(ad) == []


def test_a_confirmed_passenger_pair_wins_over_the_truck_pair():
    confirmed = {**truck_pair("Cordiant", "Бизнес CA-2", "Business CA"), **passenger_pair("Cordiant", "Бизнес CA-2", "Business CA 2")}
    assert truck.resolve_c("CORDIANT", "Бизнес CA-2", REF, confirmed) == ("Cordiant", "Business CA 2", False)
    assert truck.resolve_c("CORDIANT", "Бизнес CA-2", REF, truck_pair("Cordiant", "Бизнес CA-2", "Business CA")) == \
        ("Cordiant", "Business CA", True)
    assert truck.resolve_c("CORDIANT", "Бизнес CA-2", REF, {}) == ("Cordiant", None, True)             # waits for a pair


def test_a_confirmed_passenger_pair_wins_over_the_very_name_in_the_reference():
    confirmed = passenger_pair("Cordiant", "Business CS", "Business CA 2")
    assert truck.resolve_c("CORDIANT", "Business CS", REF, confirmed) == ("Cordiant", "Business CA 2", False)
    assert truck.resolve_c("CORDIANT", "Business CS", REF, {}) == ("Cordiant", "Business CS", False)


def test_a_bare_passenger_pair_of_the_same_name_does_not_move_a_c():
    """An old pair of a passenger tire (saved under the bare brand) stays without effect for a «C», as before."""
    confirmed = {("Kumho", normalize_model("CW-51")): "Winter PorTran CW51"}
    assert truck.resolve_c("KUMHO", "CW-51", REF, confirmed)[2] is True


def test_a_passenger_pair_out_of_the_reference_or_with_another_number_is_not_taken():
    assert truck.resolve_c("KUMHO", "CW-51", REF, passenger_pair("Kumho", "CW-51", "No Such Model"))[2] is True
    ref = dict(REF, **{"Ikon Tyres": frozenset({"Autograph Ice C3"})})
    found = truck.resolve_c("IKON TYRES", "Autograph Ice C4", ref, passenger_pair("Ikon Tyres", "Autograph Ice C4", "Autograph Ice C3"))
    assert found[2] is True and found[1] != "Autograph Ice C3"


def test_a_passenger_pair_does_not_touch_a_passenger_tire():
    from helpers import make_offer
    offer = make_offer(goods_id="8", brand_key="KUMHO", brand_raw="Kumho", model_raw="CW-51")
    ev = evaluate({"8": offer}, {}, settings(), REF, passenger_pair("Kumho", "CW-51", "Winter PorTran CW51"))
    assert "8" in ev.unmatched and "8" not in ev.ok


# --- a position that already has an ad in the truck section stays there --------------------------------------------------------------------


def test_a_c_with_an_ad_in_the_truck_section_stays_there_and_the_others_move():
    confirmed = {**truck_pair("Cordiant", "Бизнес CA-2", "Business CA"), **passenger_pair("Cordiant", "Бизнес CA-2", "Business CA 2")}
    offers = {"1": c_offer(goods_id="1", model_raw="Бизнес CA-2"), "2": c_offer(goods_id="2", model_raw="Бизнес CA-2", width=215)}
    ev = evaluate(offers, {}, settings(), REF, confirmed, truck_bound=frozenset({"1"}))
    assert (ev.ok["1"].avito_model, ev.ok["1"].truck_section) == ("Business CA", True)          # live: the same Id, the same section
    assert (ev.ok["2"].avito_model, ev.ok["2"].truck_section) == ("Business CA 2", False)       # not published yet: the passenger section
    live = render_ad(ev.ok["1"], ev.ok["1"].price, "P1", settings(), TODAY)
    assert live.findtext("ProductType") == truck.PRODUCT_TYPE


def test_a_truck_bound_c_without_a_truck_model_waits_and_is_never_moved():
    ev = evaluate({"1": c_offer(goods_id="1", model_raw="CW-51", brand_key="KUMHO", brand_raw="Kumho", stock=12)}, {}, settings(), REF,
                  passenger_pair("Kumho", "CW-51", "Winter PorTran CW51"), truck_bound=frozenset({"1"}))
    assert "1" not in ev.ok and "1" in ev.unmatched and "1" in ev.truck              # waits for a truck pair on «Сопоставления»


def test_the_truck_bound_codes_are_read_from_the_published_and_the_retired_ads(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    assert "<ProductType>%s</ProductType>" % truck.PRODUCT_TYPE in truck_ad("x")                 # the mark store looks for
    store.upsert_published(conn, PublishedItem("1", 5000, 4000.0, "P1"), truck_ad("1"), account_id=1)
    store.upsert_published(conn, PublishedItem("2", 5000, 4000.0, "P1"), passenger_ad("2"), account_id=1)
    store.upsert_published(conn, PublishedItem("3", 5000, 4000.0, "P1"), truck_ad("3"), account_id=2)
    conn.execute("INSERT INTO retired_ad (account_id, goods_id, ad_id, identity, removed_at, snapshot) VALUES (1, '4', '4', '[]', ?, ?)",
                 (NOW, truck_ad("4")))
    conn.commit()
    assert store.truck_bound_codes(conn, 1) == {"1", "4"}
    assert store.truck_bound_codes(conn, 2) == {"3"}
    assert store.truck_bound_codes(conn) == {"1", "3", "4"}


def test_a_code_returning_under_an_id_another_code_carried_in_the_truck_section_stays_there(tmp_path):
    """A's truck ad was taken over by B (same tire), B was taken off: A comes back under the Id «A» — no passenger ad under that Id."""
    conn = store.connect(tmp_path / "t.db")
    conn.execute("INSERT INTO retired_ad (account_id, goods_id, ad_id, identity, removed_at, snapshot) VALUES (1, 'B', 'A', '[]', ?, ?)",
                 (NOW, truck_ad("A")))
    store.upsert_published(conn, PublishedItem("C", 5000, 4000.0, "P1"), truck_ad("D"), account_id=1)
    conn.commit()
    assert {"A", "B", "C", "D"} <= store.truck_bound_codes(conn, 1)


def test_saving_a_truck_pair_over_a_passenger_one_says_the_passenger_one_wins(tmp_path):
    app, client, tok = _page(tmp_path, commodity_xml(NNOMMODIF="1", SMARKA="Kumho", SMODEL="CW-51", SCARTYPE="C", SMNFCODE="K1"))
    conn = store.connect(app.config["DB_PATH"])
    store.upsert_mapping(conn, truck.passenger_pair_brand("Kumho"), "CW-51", "Winter PorTran CW51")
    from panel_helpers import post
    post(client, "/mappings/model", tok, brand="Kumho", truck_brand="Kumho", passenger_brand="Kumho", section="truck",
         model_raw="CW-51", avito_model="Winter PorTran CW51", next="/mappings")
    assert "у модели есть подтверждённая легковая" in client.get("/mappings").get_data(as_text=True)


def test_a_c_of_a_brand_out_of_the_truck_list_can_still_take_a_passenger_model(tmp_path):
    rows = commodity_xml(NNOMMODIF="1", SMARKA="Ikon Tyres", SMODEL="Ikon Test Model X", SCARTYPE="C", SMNFCODE="K1")
    app, client, tok = _page(tmp_path, rows)
    page = client.get("/mappings").get_data(as_text=True)
    row = _row(page, "Ikon Test Model X")
    if "Ikon Tyres" not in truck.values()["Brand"]:
        assert 'name="truck_brand" value=""' in row
    assert '<option value="c_passenger"' in row


def test_the_cycle_keeps_a_published_truck_c_in_the_truck_section(tmp_path):
    from tires_sync import sync
    from tires_sync.webmim import parse_unload
    conn = store.connect(tmp_path / "t.db")
    from helpers import settings_dict
    from tires_sync.settings_store import init_settings
    init_settings(conn, settings_dict(enabled=[["CORDIANT", "winter"]], pricing={"CORDIANT|winter": {"pct_small": 10.0, "pct_large": 10.0}}), NOW)
    rows = [commodity_xml(NNOMMODIF=code, SMARKA="Cordiant", SMODEL="Бизнес CA-2", SCARTYPE="C", SWIDTH=w, SMNFCODE="K" + code)
            for code, w in (("101", "205"), ("102", "215"))]
    (tmp_path / "u.xml").write_bytes(catalog_xml(*rows))
    store.upsert_mapping(conn, truck.pair_brand("Cordiant"), "Бизнес CA-2", "Business CA")
    store.upsert_mapping(conn, truck.passenger_pair_brand("Cordiant"), "Бизнес CA-2", "Business CA 2")
    store.upsert_published(conn, PublishedItem("101", 5000, 4000.0, "P1"), truck_ad("101"), account_id=1)
    conn.commit()
    from tires_sync.settings_store import get_active
    from tires_sync.config import settings_from_dict
    eff = settings_from_dict(get_active(conn)[1])
    parsed = parse_unload((tmp_path / "u.xml").read_bytes(), eff.territory_rn)
    _published, ev, _ds = sync.plan_decisions(conn, parsed, REF, eff, 1, TODAY)
    assert (ev.ok["101"].avito_model, ev.ok["101"].truck_section) == ("Business CA", True)
    assert (ev.ok["102"].avito_model, ev.ok["102"].truck_section) == ("Business CA 2", False)


# --- confirming on «Сопоставления» --------------------------------------------------------------------------------------------------------


def test_confirm_mapping_checks_the_passenger_reference_and_keeps_the_truck_pair(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    confirm_mapping(conn, "Cordiant", "Бизнес CA-2", "Business CA", REF, NOW, "owner", section=truck.SECTION)
    confirm_mapping(conn, "Cordiant", "Бизнес CA-2", "Business CA 2", REF, NOW, "owner", section=truck.PASSENGER_SECTION)
    pairs = store.load_confirmed_mappings(conn)
    assert pairs[(truck.passenger_pair_brand("Cordiant"), normalize_model("Бизнес CA-2"))] == "Business CA 2"
    assert pairs[(truck.pair_brand("Cordiant"), normalize_model("Бизнес CA-2"))] == "Business CA"    # kept for an ad already in the truck section
    assert ("Cordiant", normalize_model("Бизнес CA-2")) not in pairs                                 # no bare passenger pair
    with pytest.raises(MappingError):
        confirm_mapping(conn, "Cordiant", "Бизнес CA-2", "Business CA", REF, NOW, "owner", section=truck.PASSENGER_SECTION)  # truck list only
    ref = dict(REF, **{"Ikon Tyres": frozenset({"Autograph Ice C3"})})
    with pytest.raises(MappingError):
        confirm_mapping(conn, "Ikon Tyres", "Autograph Ice C4", "Autograph Ice C3", ref, NOW, "owner", section=truck.PASSENGER_SECTION)


PAGE_REF = ('<Shiny>\n<make name="Kumho">\n<model name="Winter PorTran CW51"/>\n</make>\n'
            '<make name="Cordiant">\n<model name="Business CA 2"/>\n</make>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>')


def _page(tmp_path, *rows):
    from panel_helpers import login, make_app
    app = make_app(tmp_path)
    client = app.test_client()
    tok = login(client)
    (tmp_path / "reference.txt").write_text(PAGE_REF, encoding="utf-8")
    open(app.config["UNLOAD_PATH"], "wb").write(catalog_xml(*rows))
    return app, client, tok


def _row(page, name):
    return page.split('class="t-name">%s' % name)[1].split("</tr>")[0]


def test_the_page_hints_a_passenger_model_for_a_c_and_says_where_it_goes(tmp_path):
    app, client, tok = _page(tmp_path, commodity_xml(NNOMMODIF="1", SMARKA="Kumho", SMODEL="CW-51", SCARTYPE="C", SMNFCODE="K1"))
    row = _row(client.get("/mappings").get_data(as_text=True), "CW-51")
    assert "<b>Winter PorTran CW51</b>" in row and 'value="Winter PorTran CW51"' in row
    assert "после «Подтвердить» — легковой раздел" in row
    assert '<option value="c_passenger" selected>' in row and 'name="passenger_brand" value="Kumho"' in row
    from panel_helpers import post
    post(client, "/mappings/model", tok, brand="Kumho", truck_brand="Kumho", passenger_brand="Kumho", section="c_passenger",
         model_raw="CW-51", act="accept", proposal="Winter PorTran CW51", next="/mappings")
    conn = store.connect(app.config["DB_PATH"])
    assert store.load_confirmed_mappings(conn)[(truck.passenger_pair_brand("Kumho"), normalize_model("CW-51"))] == "Winter PorTran CW51"
    done = _row(client.get("/mappings?a=all").get_data(as_text=True), "CW-51")
    assert truck.PASSENGER_LABEL in done and "легковая модель подтверждена владельцем" in done and "сопоставлена" in done
    assert 'name="section" value="c_passenger"' in done


def test_the_page_offers_a_passenger_model_over_a_truck_pair_and_says_what_stays(tmp_path):
    app, client, tok = _page(tmp_path, commodity_xml(NNOMMODIF="1", SMARKA="Cordiant", SMODEL="Бизнес CA-2", SCARTYPE="C", SMNFCODE="K1"),
                             commodity_xml(NNOMMODIF="2", SMARKA="Cordiant", SMODEL="Бизнес CA-2", SCARTYPE="C", SMNFCODE="K2", SWIDTH="215"))
    conn = store.connect(app.config["DB_PATH"])
    store.upsert_mapping(conn, truck.pair_brand("Cordiant"), "Бизнес CA-2", "Business CA")
    store.upsert_published(conn, PublishedItem("1", 5000, 4000.0, "P1"), truck_ad("1"), account_id=1)
    conn.commit()
    row = _row(client.get("/mappings").get_data(as_text=True), "Бизнес CA-2")        # shown though it has a truck pair
    assert "сейчас грузовая пара «Business CA»" in row and "<b>Business CA 2</b>" in row and "Подтвердить легковую" in row
    assert '<option value="c_passenger" selected>' in row and 'value="Business CA 2"' in row
    assert "1 позиция с объявлением в грузовом разделе остаётся в нём" in row
    from panel_helpers import post
    post(client, "/mappings/model", tok, brand="Cordiant", truck_brand="Cordiant", passenger_brand="Cordiant", section="c_passenger",
         model_raw="Бизнес CA-2", avito_model="Business CA 2", next="/mappings")
    pairs = store.load_confirmed_mappings(store.connect(app.config["DB_PATH"]))
    assert pairs[(truck.passenger_pair_brand("Cordiant"), normalize_model("Бизнес CA-2"))] == "Business CA 2"
    assert pairs[(truck.pair_brand("Cordiant"), normalize_model("Бизнес CA-2"))] == "Business CA"
    done = _row(client.get("/mappings?a=all").get_data(as_text=True), "Бизнес CA-2")
    assert truck.PASSENGER_LABEL in done and "1 позиция с объявлением в грузовом разделе остаётся в нём" in done


def test_the_page_still_confirms_a_truck_model_when_the_owner_picks_the_truck_section(tmp_path):
    app, client, tok = _page(tmp_path, commodity_xml(NNOMMODIF="1", SMARKA="Kumho", SMODEL="CW-51", SCARTYPE="C", SMNFCODE="K1"))
    from panel_helpers import post
    post(client, "/mappings/model", tok, brand="Kumho", truck_brand="Kumho", passenger_brand="Kumho", section="truck",
         model_raw="CW-51", avito_model="Winter PorTran CW51", next="/mappings")
    pairs = store.load_confirmed_mappings(store.connect(app.config["DB_PATH"]))
    assert pairs == {(truck.pair_brand("Kumho"), normalize_model("CW-51")): "Winter PorTran CW51"}
