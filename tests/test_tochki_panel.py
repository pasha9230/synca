"""4tochki in the panel: the supplier row and the load state on its connection page, the switch «в фид» that the panel cannot turn on,
and the catalog that shows its positions for viewing only (with the supplier's badge and «>40»)."""
from __future__ import annotations

import json

from cryptography.fernet import Fernet

from panel_helpers import login, make_app, post
from tires_sync import connections, store, tochki_feed
from tires_sync.panel import service
from tires_sync.secretbox import SecretBox
from tires_sync.settings_store import active_settings

NOW = "2026-10-21T10:00:00"


def row(code, rest=12, price=4500.0, width="205", load="94", model="Test Model", season="w"):
    return {"code": code, "name": "Ikon Tyres %s %s/55 R16 %sT" % (model, width, load), "marka": "Ikon Tyres", "model": model,
            "season": season, "thorn": False, "type": "car", "quality": 0, "img_big_my": "", "img_big_pish": "https://img.4tochki.test/%s.jpg" % code,
            "price": price, "rest": rest, "width": width, "height": "55", "diameter": "16", "load_index": load, "speed_index": "T",
            "puncture": "", "strengthening": False, "omolog": "", "card": True}


def app_with(tmp_path, supplier=True, feed=False, rows=None, webmim_address=False):
    app = make_app(tmp_path, SECRET_BOX=SecretBox(Fernet.generate_key()))
    conn = store.connect(app.config["DB_PATH"])
    sid = None
    if supplier:
        sid = connections.create_supplier(conn, "4Точки", NOW, "o")
        if webmim_address:
            connections.set_config(conn, "webmim", sid, {"base_url": "https://b.example", "mode": "apikey", "territory_rn": "T1"}, NOW, "o")
    connections.set_config(conn, "tochki", 0, {"login": "api-user", "supplier_id": sid, "feed": feed}, NOW, "o")
    conn.commit()
    conn.close()
    if rows is not None:
        (tmp_path / "tochki-snapshot.json").write_text(json.dumps({"version": 1, "fetched_at": NOW, "warehouse_id": 7, "items": rows}), encoding="utf-8")
    client = app.test_client()
    return app, client, login(client), sid


def tochki_config(app):
    conn = store.connect(app.config["DB_PATH"])
    try:
        return connections.get_config(conn, "tochki", 0)
    finally:
        conn.close()


def test_the_page_offers_suppliers_without_a_webmim_address_and_keeps_the_switch(tmp_path):
    app, client, tok, sid = app_with(tmp_path, supplier=False)
    conn = store.connect(app.config["DB_PATH"])
    free = connections.create_supplier(conn, "4Точки", NOW, "o")
    busy = connections.create_supplier(conn, "Второй WebMIM", NOW, "o")
    connections.set_config(conn, "webmim", busy, {"base_url": "https://b.example", "mode": "apikey"}, NOW, "o")
    conn.commit()
    conn.close()
    html = client.get("/connections/tochki").get_data(as_text=True)
    assert "№%d · 4Точки" % free in html and "Второй WebMIM" not in html and "не закреплён (берётся единственная подходящая строка)" in html
    assert 'type="checkbox" disabled' in html and "Включается на этапе 2" in html
    assert "Выбрана автоматически по имени: №%d · 4Точки" % free in html                       # the only row that fits (the owner 2026-10-21)
    assert tochki_config(app)["supplier_id"] is None                                        # reading the page saves nothing

    post(client, "/connections/tochki", tok, login="new-user", supplier_id=str(free), feed="on")     # feed=on is not a field: ignored
    assert tochki_config(app) == {"login": "new-user", "supplier_id": free, "feed": False}
    for bad in (str(busy), "1", "999"):
        post(client, "/connections/tochki", tok, login="other", supplier_id=bad)
        assert tochki_config(app)["supplier_id"] == free and tochki_config(app)["login"] == "new-user"
    post(client, "/connections/tochki", tok, login="new-user", supplier_id="")
    assert tochki_config(app) == {"login": "new-user", "supplier_id": None, "feed": False}


def test_saving_the_login_keeps_the_supplier_and_the_switch(tmp_path):
    app, client, tok, sid = app_with(tmp_path, feed=True)        # a switch set by stage 2 must survive a login change
    post(client, "/connections/tochki", tok, login="changed", supplier_id=str(sid))
    assert tochki_config(app) == {"login": "changed", "supplier_id": sid, "feed": True}


def test_the_page_shows_the_state_of_the_last_load(tmp_path):
    app, client, tok, sid = app_with(tmp_path)
    html = client.get("/connections/tochki").get_data(as_text=True)
    assert "Ещё не было" in html
    (tmp_path / "tochki-state.json").write_text(json.dumps({
        "attempt_at": "2026-10-21T11:00:00", "ok_at": "2026-10-21T10:00:00", "items": 4321, "pages": 3, "seconds": 42.5,
        "without_card": 12, "skipped_quality": 2, "error": "4 точки: GetFindTyre: код 503.", "error_at": "2026-10-21T11:00:00"}), encoding="utf-8")
    html = client.get("/connections/tochki").get_data(as_text=True)
    assert "позиций 4321" in html and "страниц 3" in html and "42.5 с" in html and "без карточки (ждут следующего часа) 12" in html
    assert "GetFindTyre: код 503." in html and "Позиции остаются от последней удачной загрузки" in html


def catalog_items(app, view=True):
    conn = store.connect(app.config["DB_PATH"])
    from tires_sync.panel.data import load_merged, load_reference
    settings = active_settings(conn)
    parsed = load_merged(conn, settings, app.config["UNLOAD_PATH"])
    shown, only = tochki_feed.catalog_view(conn, parsed, app.config["UNLOAD_PATH"]) if view else (parsed, frozenset())
    groups = service.catalog_groups(conn, settings, shown, load_reference(app.config["REFERENCE_PATH"]), view_only=only,
                                    capped_supplier=tochki_feed.config(conn)["supplier_id"])
    conn.close()
    return {i["goods_id"]: i for i in service.flat_items(groups)}, parsed


def test_the_catalog_view_adds_4tochki_for_viewing_only(tmp_path):
    app, client, tok, sid = app_with(tmp_path, rows=[row("T1", rest=41), row("T2", rest=3, width="215", load="95")])
    items, parsed = catalog_items(app)
    one, two = items["s%d-T1" % sid], items["s%d-T2" % sid]
    assert one["view_only"] and one["state"] == "no" and one["reason"] == service.VIEW_ONLY_TEXT and one["stock_text"] == ">40"
    assert two["stock_text"] == "3" and one["supplier_mark"]["text"] == "4Т" and one["code"] == ""
    assert items["100"]["supplier_mark"]["text"] == "MIM" and not items["100"]["view_only"] and items["100"]["stock_text"] == "10"
    assert not any(g.startswith("s%d-" % sid) for g in parsed.offers)            # what builds feeds never sees them


def test_no_view_without_a_supplier_with_the_switch_on_or_over_webmim_positions(tmp_path):
    for case, kwargs in (("nosup", {"supplier": False}), ("on", {"feed": True}), ("busy", {"webmim_address": True})):
        folder = tmp_path / case
        folder.mkdir()
        app, client, tok, sid = app_with(folder, rows=[row("T1")], **kwargs)
        if case == "busy":
            (folder / ("unload-%d.xml" % sid)).write_bytes((folder / "unload.xml").read_bytes().replace(b"<TERRITORY_RN>T1", b"<TERRITORY_RN>T1"))
        items, _ = catalog_items(app)
        assert not any(i["view_only"] for i in items.values()), case
        assert "s%s-T1" % sid not in items, case


def test_the_catalog_page_shows_4tochki_rows_with_its_tab_and_no_actions(tmp_path):
    app, client, tok, sid = app_with(tmp_path, rows=[row("T1", rest=41)])
    html = client.get("/catalog").get_data(as_text=True)
    assert "4Точки" in html and "&gt;40 шт." in html and "только просмотр" in html and service.VIEW_ONLY_TEXT in html
    assert "goods_id=s%d-T1" % sid not in html and 'value="s%d-T1"' not in html and 'data-preview="s%d-T1"' not in html
    only = client.get("/catalog?sup=%d" % sid).get_data(as_text=True)
    assert "Ikon Tyres Test Model 205/55 R16 94T" in only and "<td class=\"t-code\">TS1</td>" not in only
    assert "s%d-T1" % sid not in client.get("/catalog?st=feed").get_data(as_text=True)


def test_the_all_suppliers_catalog_marks_each_position_with_its_supplier_and_one_supplier_tab_does_not(tmp_path):
    import re
    app, client, tok, sid = app_with(tmp_path, rows=[row("T1", rest=41)])
    marks = lambda html: re.findall(r'<span class="sup-mark sup-mark--(\w+)" title="[^"]*">([^<]+)</span>', html)
    page = client.get("/catalog").get_data(as_text=True)
    found = marks(page)
    assert ("t4", "4Т") in found and ("mim", "MIM") in found and found.count(("t4", "4Т")) == 1     # one 4tochki position, the WebMIM ones are MIM
    assert 'class="badge badge--neutral" title="Поставщик"' not in page                                # the grey name plate is replaced, not doubled
    assert marks(client.get("/catalog?sup=%d" % sid).get_data(as_text=True)) == []
    assert marks(client.get("/catalog?sup=1").get_data(as_text=True)) == []
    assert marks(client.get("/catalog/table").get_data(as_text=True)) == found                          # the table refresh keeps the signs


def test_a_single_supplier_catalog_has_no_signs_and_a_gone_position_keeps_the_sign_of_its_supplier(tmp_path):
    from tires_sync import catalog_archive
    from helpers import make_offer
    from tires_sync.webmim import ParseResult
    app, client, tok, sid = app_with(tmp_path, supplier=False)
    assert "sup-mark--" not in client.get("/catalog").get_data(as_text=True)
    (tmp_path / "two").mkdir()
    app, client, tok, sid = app_with(tmp_path / "two", rows=[row("T1")])
    conn = store.connect(app.config["DB_PATH"])
    old = make_offer(goods_id="s%d-G1" % sid, code="G1", name_orig="Ikon Gone 205/55 R16", name="Ikon Gone 205/55 R16", model_raw="Gone")
    keep = make_offer(goods_id="s%d-K1" % sid, code="K1")
    catalog_archive.record(conn, ParseResult(offers={old.goods_id: old, keep.goods_id: keep}), "2026-10-10T10:00:00")
    catalog_archive.record(conn, ParseResult(offers={keep.goods_id: keep}), "2026-10-15T10:00:00")
    conn.commit()
    conn.close()
    html = client.get("/catalog?st=gone").get_data(as_text=True)
    assert 'sup-mark--t4" title="Поставщик: 4 точки">4Т<' in html


def test_the_mappings_page_lists_4tochki_brands_and_models(tmp_path):
    app, client, tok, sid = app_with(tmp_path, rows=[row("T1", model="Brand New Model")])
    html = client.get("/mappings").get_data(as_text=True)
    assert "Brand New Model" in html
    without = tmp_path / "without"
    without.mkdir()
    app2, client2, _, _ = app_with(without, rows=[row("T1", model="Brand New Model")], feed=True)
    # stage 2 brings them another way: with the switch on (and a load the cycle takes) they come from the panel's set (load_merged)
    assert "Brand New Model" not in client2.get("/mappings").get_data(as_text=True)                       # the test load is old: held
    from datetime import datetime
    (without / "tochki-snapshot.json").write_text(json.dumps({"version": 1, "fetched_at": datetime.utcnow().isoformat(timespec="seconds"),
                                                              "warehouse_id": 7, "items": [row("T1", model="Brand New Model")]}), encoding="utf-8")
    assert "Brand New Model" in client2.get("/mappings").get_data(as_text=True)


def test_a_refused_supplier_keeps_the_password_unchanged_too(tmp_path):
    app, client, tok, sid = app_with(tmp_path)
    post(client, "/connections/tochki", tok, login="api-user", supplier_id="1", password="new-pw-SECRET")
    conn = store.connect(app.config["DB_PATH"])
    assert conn.execute("SELECT COUNT(*) FROM secret WHERE kind = 'tochki'").fetchone()[0] == 0
    conn.close()
