from __future__ import annotations

from helpers import catalog_xml, commodity_xml
from panel_helpers import login, make_app, post
from tires_sync import store

ROWS = (
    commodity_xml(NNOMMODIF="1", SMNFCODE="C1"),                                                   # Ikon, matched
    commodity_xml(NNOMMODIF="4", SSEASON="Летняя", SMARKA="Лауфенн", SMODIFNAME="LAUFENN 195/65/15 Fit Pro",
                  SMODEL="Fit Pro", SDIAMETR="15", SWIDTH="195", SHEIGHT="65", SMNFCODE="C4"),     # Laufenn: switched off, confident proposal
)


def setup(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    tok = login(client)
    open(app.config["UNLOAD_PATH"], "wb").write(catalog_xml(*ROWS))
    (tmp_path / "reference.txt").write_text(
        '<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n'
        '<make name="Laufenn">\n<model name="G Fit"/>\n<model name="Fit Pro XL"/>\n</make>\n</Shiny>', encoding="utf-8")
    return app, client, tok


def test_confident_models_of_a_switched_off_brand_can_be_accepted_in_one_click(tmp_path):
    app, client, tok = setup(tmp_path)
    html = client.get("/mappings").get_data(as_text=True)
    assert "Принять уверенные (1)" in html and "Принять все уверенные (1)" in html
    r = post(client, "/mappings/accept-confident", tok, brand="Laufenn", q="", next="/mappings#b-1")
    assert r.headers["Location"].endswith("/mappings#b-1")
    assert store.load_confirmed_mappings(store.connect(app.config["DB_PATH"]))[("Laufenn", "FITPRO")] == "Fit Pro XL"
    assert "Принято сопоставлений: 1." in client.get("/mappings").get_data(as_text=True)


def test_accepting_by_filter_touches_only_what_the_filter_shows(tmp_path):
    app, client, tok = setup(tmp_path)
    post(client, "/mappings/accept-confident", tok, brand="", q="nothing-like-this", next="/mappings?q=nothing-like-this")
    assert store.load_confirmed_mappings(store.connect(app.config["DB_PATH"])) == {}
    post(client, "/mappings/accept-confident", tok, brand="Ikon Tyres", q="", next="//evil.example")
    assert store.load_confirmed_mappings(store.connect(app.config["DB_PATH"])) == {}        # another brand: nothing to take
    r = post(client, "/mappings/accept-confident", tok, brand="", q="fit", next="//evil.example")
    assert r.headers["Location"].endswith("/mappings")
    assert len(store.load_confirmed_mappings(store.connect(app.config["DB_PATH"]))) == 1


def test_the_search_finds_a_brand_by_the_supplier_spelling_and_an_empty_result_says_so(tmp_path):
    app, client, tok = setup(tmp_path)
    assert 'class="t-name">Fit Pro ' in client.get("/mappings?q=laufenn").get_data(as_text=True)
    assert 'class="t-name">Fit Pro ' in client.get("/mappings?q=LAUFENN").get_data(as_text=True)
    empty = client.get("/mappings?q=zzz").get_data(as_text=True)
    assert "Ничего не найдено" in empty and "Нет моделей, ожидающих" not in empty
    assert "Ничего не найдено" in client.get("/mappings?b=NOPE").get_data(as_text=True)


def test_the_page_stays_light_one_list_of_avito_models_per_brand(tmp_path):
    app, client, tok = setup(tmp_path)
    html = client.get("/mappings?a=all").get_data(as_text=True)
    assert html.count('<datalist id="dl-') == 2 and html.count("<select") == 1          # the brand filter only


def test_export_lists_every_unmatched_model_including_switched_off_brands(tmp_path):
    app, client, tok = setup(tmp_path)
    body = client.get("/mappings/export.csv").get_data().decode("utf-8-sig")
    assert "Laufenn;Fit Pro;1;Fit Pro XL;code" in body and "Test Model" not in body.split("\n", 1)[1]


def test_menu_and_home_count_the_same_unmatched_models(tmp_path):
    app, client, tok = setup(tmp_path)
    home = client.get("/").get_data(as_text=True)
    assert "1 модель (1 позиция)" in home and "Модели ждут сопоставления" in home
    assert 'Сопоставления<span class="sb-count">1</span>' in home


def test_catalog_filter_no_pair_shows_positions_of_unmatched_models_with_a_link_to_the_mappings(tmp_path):
    app, client, tok = setup(tmp_path)
    open(app.config["UNLOAD_PATH"], "wb").write(catalog_xml(
        commodity_xml(NNOMMODIF="1", SMNFCODE="C1"), commodity_xml(NNOMMODIF="2", SMODEL="Weird 9000", SMNFCODE="C2")))
    html = client.get("/catalog?st=nopair").get_data(as_text=True)
    assert ">C2<" in html and ">C1<" not in html
    assert 'href="/mappings?b=IKON%20TYRES&q=Weird%209000"' in html


def test_models_show_season_studs_and_commercial_marks_on_mappings_and_catalog(tmp_path):
    app, client, tok = setup(tmp_path)
    open(app.config["UNLOAD_PATH"], "wb").write(catalog_xml(
        commodity_xml(NNOMMODIF="1", SMODEL="Weird 9000", SMODIFNAME="IKON 205/55/16 Weird 9000 Ш.", SMNFCODE="C1"),
        commodity_xml(NNOMMODIF="2", SMODEL="Weird 9000", SSEASON="Летняя", SCARTYPE="C", SMNFCODE="C2"),
        commodity_xml(NNOMMODIF="3", SMODEL="Plain 1", SSEASON="Летняя", SMNFCODE="C3")))
    page = client.get("/mappings").get_data(as_text=True)
    row = page.split('class="t-name">Weird 9000')[1].split("</td>")[0]
    assert 'title="Зима' in row and 'title="Лето"' not in row                            # only the winter studded position of this model is here: the commercial one is not taken
    assert ">C</span>" not in row                                                         # commercial tires (C, LT) are not taken from the unload at all (2026-10-14)
    plain = page.split('class="t-name">Plain 1')[1].split("</td>")[0]
    assert 'title="Лето"' in plain and "badge--neutral" not in plain
    catalog = client.get("/catalog?q=ikon").get_data(as_text=True)
    assert ">C2<" not in catalog and ">C1<" in catalog


def test_account_page_has_a_copy_button_for_the_feed_address(tmp_path):
    from tires_sync import connections
    app, client, tok = setup(tmp_path)
    connections.create_account(store.connect(app.config["DB_PATH"]), "A", "2026-10-07T10:00:00", "owner")
    html = client.get("/connections/avito/1").get_data(as_text=True)
    assert 'data-copy="#feed-url"' in html and 'id="feed-url"' in html and "Скопировать" in html


def test_accept_confident_respects_the_brand_filter_and_the_supplier_key(tmp_path):
    app, client, tok = setup(tmp_path)
    post(client, "/mappings/accept-confident", tok, b="IKON TYRES", q="", next="/mappings")
    assert store.load_confirmed_mappings(store.connect(app.config["DB_PATH"])) == {}            # Laufenn's proposal is not touched
    post(client, "/mappings/accept-confident", tok, b="LAUFENN", q="", next="/mappings")
    assert len(store.load_confirmed_mappings(store.connect(app.config["DB_PATH"]))) == 1
    html = client.get("/mappings?b=LAUFENN").get_data(as_text=True)
    assert 'name="b" value="LAUFENN"' in html or "Нет моделей" in html


def test_brand_typed_in_any_case_is_accepted_and_errors_are_in_russian(tmp_path):
    app, client, tok = setup(tmp_path)
    post(client, "/mappings/brand", tok, brand_key="НЕИЗВЕСТНЫЙ", avito_brand="", avito_brand_manual="laufenn")
    assert store.load_brand_map(store.connect(app.config["DB_PATH"])) == {"НЕИЗВЕСТНЫЙ": "Laufenn"}
    post(client, "/mappings/brand", tok, brand_key="ДРУГОЙ", avito_brand="", avito_brand_manual="Nonesuch")
    assert "Бренда «Nonesuch» нет в справочнике Авито." in client.get("/mappings").get_data(as_text=True)


def test_the_percent_of_a_russian_name_is_judged_by_sound_and_the_file_field_is_reachable(tmp_path):
    from tires_sync.catalog import loose_form
    import difflib
    assert round(100 * difflib.SequenceMatcher(None, loose_form("Хакка Блю"), loose_form("Hakka Blue")).ratio()) >= 70
    app, client, tok = setup(tmp_path)
    html = client.get("/mappings").get_data(as_text=True)
    assert 'type="file" name="file" accept=".csv,text/csv" class="sr-only"' in html and "<noscript>" in html
    assert html.index("</table>") < html.index("<datalist id=\"dl-")              # lists live outside the table body


def test_journal_names_the_service_in_russian_and_cli_is_a_reserved_login(tmp_path):
    from tires_sync import users
    from tires_sync.panel.texts import author_label
    assert author_label("system") == "Система" and author_label("cli") == "Сервер" and author_label("worker2tyres") == "worker2tyres"
    assert "cli" in users.RESERVED
