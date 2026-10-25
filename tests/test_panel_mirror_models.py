"""«Сопоставления»: the models of a mirrored feed that Avito's reference does not have are shown with the full supplier name, and can be confirmed by hand."""
from __future__ import annotations

from xml.sax.saxutils import escape

from helpers import catalog_xml, commodity_xml, settings_dict
from panel_helpers import login, make_app, post
from tires_sync import connections, mirror_feed, store

NOW = "2026-10-14T12:00:00"
MARK = "Зеркальные фиды"


def car(ad_id, brand, model):
    brand, model = escape(brand), escape(model)
    return ('<Ad><Id>%s</Id><Brand>%s</Brand><Model>%s</Model><Title>Шина</Title><Price>5000</Price>'
            '<Description>Цена: 5000 руб.</Description></Ad>' % (ad_id, brand, model))


def with_mirror(tmp_path, *ads):
    app = make_app(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    account = connections.create_account(conn, "Партнёр", NOW, "o")
    feed = ('<Ads formatVersion="3" target="Avito.ru">%s</Ads>' % "".join(ads)).encode("utf-8")
    mirror_feed.import_feed(conn, account, feed, NOW)
    return app, conn, account


def block(page):
    """The part of the page with the mirror block (empty when there is none)."""
    return page.split(MARK, 1)[1].split("</section>", 1)[0] if MARK in page else ""


def test_a_model_outside_the_reference_is_shown_with_the_full_supplier_name(tmp_path):
    app, conn, _ = with_mirror(tmp_path, car("101_20260801", "IKON TYRES", "Test Model 2"), car("100_20260801", "IKON TYRES", "Test Model"))
    client = app.test_client()
    login(client)
    mirror = block(client.get("/mappings").get_data(as_text=True))
    assert "Test Model 2" in mirror and "101_20260801" in mirror
    assert "Ikon 205/55/16 T 94 Test Model" in mirror                                  # SMODIFNAME_ORIG of the position, spaces collapsed
    assert "100_20260801" not in mirror                                                # this model is in the reference: not a problem
    assert 'value="Test Model"' in mirror                                              # the suggestion comes from the full name


def test_nothing_is_shown_when_every_model_is_in_the_reference_or_there_is_no_mirror(tmp_path):
    app, _conn, _ = with_mirror(tmp_path, car("100_20260801", "IKON TYRES", "Test Model"))
    client = app.test_client()
    login(client)
    assert MARK not in client.get("/mappings").get_data(as_text=True)
    (tmp_path / "plain").mkdir()
    client = make_app(tmp_path / "plain").test_client()
    login(client)
    assert MARK not in client.get("/mappings").get_data(as_text=True)


def test_confirming_by_hand_stores_the_mapping_and_the_feed_gets_the_model(tmp_path):
    app, conn, account = with_mirror(tmp_path, car("101_20260801", "IKON TYRES", "Test Model 2"))
    client = app.test_client()
    tok = login(client)
    r = post(client, "/mappings/model", tok, brand="Ikon Tyres", model_raw="Test Model 2", avito_model="Other Model", next="/mappings")
    assert r.status_code == 302
    assert store.load_confirmed_mappings(conn)[("Ikon Tyres", "TESTMODEL2")] == "Other Model"
    assert MARK not in client.get("/mappings").get_data(as_text=True)
    from tires_sync.panel.data import load_reference
    models = mirror_feed.models_for(conn, account, load_reference(app.config["REFERENCE_PATH"]))
    assert models == {"101_20260801": "Other Model"}


def test_html_from_the_feed_and_the_supplier_is_escaped(tmp_path):
    app, _conn, _ = with_mirror(tmp_path, car("101_20260801", "IKON TYRES", "<b>x</b>"))
    (tmp_path / "unload.xml").write_bytes(catalog_xml(commodity_xml(NNOMMODIF="101", SMODIFNAME_ORIG="<script>alert(1)</script> 205/55/16")))
    client = app.test_client()
    login(client)
    mirror = block(client.get("/mappings").get_data(as_text=True))
    assert "<script>alert(1)</script>" not in mirror and "&lt;script&gt;" in mirror and "<b>x</b>" not in mirror


def test_the_model_list_is_sent_once_per_brand_however_many_rows_there_are(tmp_path):
    app, _conn, _ = with_mirror(tmp_path, car("101_1", "IKON TYRES", "Test Model 2"), car("102_1", "IKON TYRES", "Test Model 3"),
                                car("103_1", "IKON TYRES", "Test Model 4"))
    client = app.test_client()
    login(client)
    mirror = block(client.get("/mappings").get_data(as_text=True))
    assert mirror.count("<datalist") == 1 and mirror.count('name="avito_model"') == 3


def test_a_failure_of_the_mirror_block_does_not_take_the_page_down(tmp_path, monkeypatch):
    app, _conn, _ = with_mirror(tmp_path, car("101_1", "IKON TYRES", "Test Model 2"))
    from tires_sync.panel import service
    monkeypatch.setattr(service, "mirror_models_to_check", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    client = app.test_client()
    login(client)
    resp = client.get("/mappings")
    assert resp.status_code == 200 and "Модели, ждущие сопоставления" in resp.get_data(as_text=True)
    assert "Не удалось сверить модели зеркальных фидов" in client.get("/mappings").get_data(as_text=True)
