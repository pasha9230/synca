from __future__ import annotations

import io

from helpers import catalog_xml, commodity_xml
from panel_helpers import login, make_app, post
from tires_sync import store


def setup(tmp_path, *commodities):
    app = make_app(tmp_path)
    client = app.test_client()
    tok = login(client)
    open(app.config["UNLOAD_PATH"], "wb").write(catalog_xml(*commodities))
    return app, client, tok


IKON_PREFIX = commodity_xml(NNOMMODIF="200", SMODEL="Ikon Test Model")
IKON_CODE = commodity_xml(NNOMMODIF="202", SMODEL="Other")
IKON_NONE = commodity_xml(NNOMMODIF="201", SMODEL="Weird 9000")
PIRELLI_PREFIX = commodity_xml(NNOMMODIF="300", SMARKA="Pirelli", SMODEL="Pirelli Test Model")


def confirmed(app):
    return store.load_confirmed_mappings(store.connect(app.config["DB_PATH"]))


def test_page_marks_confident_pairs_and_offers_to_accept_them_in_bulk(tmp_path):
    app, client, tok = setup(tmp_path, IKON_PREFIX, IKON_CODE, IKON_NONE)
    html = client.get("/mappings").get_data(as_text=True)
    assert html.count("уверенно") == 1 and "нет подсказки" in html
    assert "Принять все уверенные (1)" in html and "Ikon Test Model" not in html
    assert 'href="/mappings/export.csv"' in html and 'action="/mappings/import"' in html


def test_accept_confident_confirms_only_confident_pairs(tmp_path):
    app, client, tok = setup(tmp_path, IKON_PREFIX, IKON_CODE, IKON_NONE)
    r = post(client, "/mappings/accept-confident", tok)
    assert r.status_code == 302
    assert sorted(confirmed(app).values()) == ["Other Model"]
    html = client.get("/mappings").get_data(as_text=True)
    assert "Weird 9000" in html and "Принято сопоставлений: 1" in html
    audit = store.list_audit(store.connect(app.config["DB_PATH"]), 10)
    assert sum(1 for e in audit if e["action"] == "mapping_confirm") == 1


def test_accept_confident_can_be_limited_to_one_brand(tmp_path):
    ikon = commodity_xml(NNOMMODIF="210", SMODEL="Test")
    pirelli = commodity_xml(NNOMMODIF="310", SMARKA="Pirelli", SMODEL="Test")
    app, client, tok = setup(tmp_path, ikon, pirelli)
    post(client, "/mappings/accept-confident", tok, brand="Ikon Tyres")
    assert list(confirmed(app)) == [("Ikon Tyres", "TEST")]
    assert "Pirelli" in client.get("/mappings").get_data(as_text=True)


def test_csv_export_lists_pending_pairs_with_proposals(tmp_path):
    app, client, tok = setup(tmp_path, IKON_PREFIX, IKON_CODE, IKON_NONE)
    r = client.get("/mappings/export.csv")
    assert r.status_code == 200 and r.mimetype == "text/csv"
    body = r.get_data()
    assert body.startswith(b"\xef\xbb\xbf")
    lines = body.decode("utf-8-sig").splitlines()
    assert lines[0] == "brand;supplier_model;goods;avito_model;tier"
    assert "Ikon Tyres;Other;1;Other Model;code" in lines
    assert not any("Ikon Test Model" in line for line in lines)
    assert "Ikon Tyres;Weird 9000;1;;none" in lines


def csv_file(text):
    return (io.BytesIO(text.encode("utf-8")), "m.csv")


def test_csv_import_applies_valid_rows_and_reports_the_rest(tmp_path):
    app, client, tok = setup(tmp_path, IKON_PREFIX, IKON_NONE)
    text = ("brand;supplier_model;goods;avito_model;tier\n"
            "Ikon Tyres;Weird 9000;1;Other Model;none\n"
            "Ikon Tyres;Ikon Test Model;1;;prefix\n"
            "Ikon Tyres;Broken;1;No Such Model;none\n"
            "NoBrand;X;1;Y;none\n")
    r = client.post("/mappings/import", data={"csrf": tok, "file": csv_file(text)}, content_type="multipart/form-data")
    assert r.status_code == 302
    assert sorted(confirmed(app).values()) == ["Other Model"]
    html = client.get("/mappings").get_data(as_text=True)
    assert "Принято: 1" in html and "пропущено пустых: 1" in html and "ошибок: 2" in html and "No Such Model" in html


def test_csv_import_accepts_comma_delimiter_and_rejects_missing_columns_and_big_files(tmp_path):
    app, client, tok = setup(tmp_path, IKON_NONE)
    ok = client.post("/mappings/import", data={"csrf": tok, "file": csv_file(
        "brand,supplier_model,avito_model\nIkon Tyres,Weird 9000,Test Model\n")}, content_type="multipart/form-data")
    assert ok.status_code == 302 and list(confirmed(app).values()) == ["Test Model"]
    client.post("/mappings/import", data={"csrf": tok, "file": csv_file("foo;bar\n1;2\n")}, content_type="multipart/form-data")
    assert "нет столбцов" in client.get("/mappings").get_data(as_text=True)
    big = "brand;supplier_model;avito_model\n" + ("Ikon Tyres;X;Test Model\n" * 60000)
    client.post("/mappings/import", data={"csrf": tok, "file": csv_file(big)}, content_type="multipart/form-data")
    assert "слишком большой" in client.get("/mappings").get_data(as_text=True)
    client.post("/mappings/import", data={"csrf": tok}, content_type="multipart/form-data")
    assert "выберите файл" in client.get("/mappings").get_data(as_text=True)


def test_csv_import_survives_empty_file_and_rejects_non_utf8(tmp_path):
    app, client, tok = setup(tmp_path, IKON_NONE)
    r = client.post("/mappings/import", data={"csrf": tok, "file": csv_file("")}, content_type="multipart/form-data")
    assert r.status_code == 302
    assert "нет столбцов" in client.get("/mappings").get_data(as_text=True)
    cp1251 = (io.BytesIO("brand;supplier_model;avito_model\nИкон;Х;Y\n".encode("cp1251")), "m.csv")
    r = client.post("/mappings/import", data={"csrf": tok, "file": cp1251}, content_type="multipart/form-data")
    assert r.status_code == 302
    assert "UTF-8" in client.get("/mappings").get_data(as_text=True) and confirmed(app) == {}


def test_csv_export_neutralizes_formulas_and_import_reads_them_back(tmp_path):
    from tires_sync.panel.routes_mappings import csv_safe, csv_unsafe
    assert csv_safe("=1+1") == "'=1+1" and csv_safe("-5") == "'-5" and csv_safe("@x") == "'@x" and csv_safe("+7") == "'+7"
    assert csv_safe("Ikon") == "Ikon" and csv_safe("") == ""
    assert csv_unsafe("'=1+1") == "=1+1" and csv_unsafe("'ok") == "'ok" and csv_unsafe("Ikon") == "Ikon"
    app, client, tok = setup(tmp_path, commodity_xml(NNOMMODIF="400", SMODEL="=HYPERLINK(1)"))
    body = client.get("/mappings/export.csv").get_data().decode("utf-8-sig")
    assert ";'=HYPERLINK(1);" in body and ";=HYPERLINK(1);" not in body
