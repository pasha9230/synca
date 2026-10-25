from __future__ import annotations

from helpers import catalog_xml, commodity_xml
from panel_helpers import login, make_app, post
from tires_sync import store

ROWS = (
    commodity_xml(NNOMMODIF="1", SMNFCODE="C1"),                                   # Ikon, Test Model: matched by name
    commodity_xml(NNOMMODIF="2", SMODEL="Other", SMNFCODE="C2"),                   # Ikon, proposal
    commodity_xml(NNOMMODIF="3", SMODEL="Weird 9000", SMNFCODE="C3"),              # Ikon, nothing
    commodity_xml(NNOMMODIF="4", SSEASON="Летняя", SMARKA="Лауфенн", SMODIFNAME="LAUFENN 195/65/15 G Fit", SMODEL="G Fitt",
                  SDIAMETR="15", SWIDTH="195", SHEIGHT="65", SMNFCODE="C4"),         # Laufenn: brand and season switched off
)


def setup(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    tok = login(client)
    open(app.config["UNLOAD_PATH"], "wb").write(catalog_xml(*ROWS))
    return app, client, tok


def page(client, q=""):
    return client.get("/mappings" + q).get_data(as_text=True)


def test_every_unmatched_model_is_listed_even_for_switched_off_brands(tmp_path):
    app, client, tok = setup(tmp_path)
    html = page(client)
    assert 'class="t-name">Other ' in html and 'class="t-name">Weird 9000 ' in html and 'class="t-name">G Fitt ' in html
    assert 'class="t-name">Test Model ' not in html                                   # matched: hidden by default
    assert "3 не сопоставлено из 4" in html


def test_matched_models_show_with_the_filter_and_can_be_changed(tmp_path):
    app, client, tok = setup(tmp_path)
    html = page(client, "?a=all")
    assert 'class="t-name">Test Model ' in html and "сопоставлена" in html and ">Сохранить<" in html
    only = page(client, "?a=all&q=weird")
    assert 'class="t-name">Weird 9000 ' in only and 'class="t-name">Other ' not in only
    brand = page(client, "?b=LAUFENN")
    assert 'class="t-name">G Fitt ' in brand and 'class="t-name">Other ' not in brand


def test_positions_are_not_listed_on_the_mappings_page(tmp_path):
    app, client, tok = setup(tmp_path)
    html = page(client, "?a=all")
    assert ">C1<" not in html and "205/55" not in html


def test_saving_returns_to_the_same_filtered_list_at_the_brand(tmp_path):
    app, client, tok = setup(tmp_path)
    r = post(client, "/mappings/model", tok, brand="Ikon Tyres", model_raw="Other", avito_model="Other Model",
             next="/mappings?q=oth#b-12345678")
    assert r.status_code == 302 and r.headers["Location"].endswith("/mappings?q=oth#b-12345678")
    assert store.load_confirmed_mappings(store.connect(app.config["DB_PATH"]))[("Ikon Tyres", "OTHER")] == "Other Model"
    assert "Сопоставление сохранено" in page(client, "?q=oth")
    r = post(client, "/mappings/model", tok, brand="Ikon Tyres", model_raw="Weird 9000", avito_model="Other Model", next="//evil.example")
    assert r.headers["Location"].endswith("/mappings")


def test_mapped_model_can_be_pointed_to_another_avito_model(tmp_path):
    app, client, tok = setup(tmp_path)
    post(client, "/mappings/model", tok, brand="Ikon Tyres", model_raw="Other", avito_model="Other Model")
    post(client, "/mappings/model", tok, brand="Ikon Tyres", model_raw="Other", avito_model="Test Model")
    assert store.load_confirmed_mappings(store.connect(app.config["DB_PATH"]))[("Ikon Tyres", "OTHER")] == "Test Model"
