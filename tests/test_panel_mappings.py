from tires_sync import store
from panel_helpers import login, make_app, post


def test_page_lists_pending_model_pair_with_the_suggestion_prefilled(tmp_path):
    client = make_app(tmp_path).test_client()
    login(client)
    page = client.get("/mappings").get_data(as_text=True)
    assert "Test Model 2" in page and 'value="Test Model"' in page and 'list="dl-' in page and "required" in page


def test_confirming_a_model_stores_it_and_clears_the_queue(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    tok = login(client)
    r = post(client, "/mappings/model", tok, brand="Ikon Tyres", model_raw="Test Model 2", avito_model="Test Model")
    assert r.status_code == 302
    conn = store.connect(app.config["DB_PATH"])
    assert list(store.load_confirmed_mappings(conn).values()) == ["Test Model"]
    assert store.list_audit(conn)[0]["action"] == "mapping_confirm"
    assert "Нет моделей, ожидающих" in client.get("/mappings").get_data(as_text=True)


def test_confirming_a_model_missing_from_reference_is_refused_with_message(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    tok = login(client)
    r = post(client, "/mappings/model", tok, brand="Ikon Tyres", model_raw="Test Model 2", avito_model="Nope")
    page = client.get(r.headers["Location"]).get_data(as_text=True)
    assert "нет у бренда" in page
    conn = store.connect(app.config["DB_PATH"])
    assert store.load_confirmed_mappings(conn) == {}


def test_unresolved_brand_is_listed_and_can_be_mapped_by_typing_a_name(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    tok = login(client)
    page = client.get("/mappings").get_data(as_text=True)
    assert "НЕИЗВЕСТНЫЙ" in page.split("Бренды без сопоставления")[1]
    post(client, "/mappings/brand", tok, brand_key="НЕИЗВЕСТНЫЙ", avito_brand="", avito_brand_manual="Pirelli")
    conn = store.connect(app.config["DB_PATH"])
    assert store.load_brand_map(conn) == {"НЕИЗВЕСТНЫЙ": "Pirelli"}
    after = client.get("/mappings").get_data(as_text=True)
    assert 'name="brand_key" value="НЕИЗВЕСТНЫЙ"' not in after and "Бренды без сопоставления" not in after


def test_html_in_supplier_model_names_is_escaped(tmp_path):
    from helpers import catalog_xml, commodity_xml
    app = make_app(tmp_path)
    (tmp_path / "unload.xml").write_bytes(catalog_xml(commodity_xml(SMODEL="<script>alert(1)</script>")))
    client = app.test_client()
    login(client)
    page = client.get("/mappings").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page
