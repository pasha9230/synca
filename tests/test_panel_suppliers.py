from __future__ import annotations

import re

from helpers import catalog_xml, commodity_xml, settings_dict
from panel_helpers import login, make_app, post
from tires_sync import connections, staging, store
from tires_sync.settings_store import confirm_draft, create_draft

T = "2026-10-14T10:00:00"
ONE = catalog_xml(commodity_xml(NNOMMODIF="5", TERRITORY_RN="T1", SMNFCODE="A1"))
TWO = catalog_xml(commodity_xml(NNOMMODIF="5", TERRITORY_RN="T9", SMNFCODE="B1"))


def setup(tmp_path, second=True):
    app = make_app(tmp_path)
    client = app.test_client()
    tok = login(client)
    open(app.config["UNLOAD_PATH"], "wb").write(ONE)
    conn = store.connect(app.config["DB_PATH"])
    if second:
        two = connections.create_supplier(conn, "Второй", T, "owner")
        connections.set_config(conn, "webmim", connections.supplier_owner(two),
                               {"base_url": "https://b.example", "mode": "apikey", "territory_rn": "T9"}, T, "owner")
        (tmp_path / "unload-2.xml").write_bytes(TWO)
        confirm_draft(conn, create_draft(conn, {
            "enabled": [["IKON TYRES", "winter"], ["S2:IKON TYRES", "winter"]],
            "pricing": {"IKON TYRES|winter": {"pct_small": -3.0, "pct_large": -3.0},
                        "S2:IKON TYRES|winter": {"pct_small": 5.0, "pct_large": 5.0}}}, T), T, "owner")
    return app, client, tok, conn


def page(client, q=""):
    return client.get("/catalog" + q).get_data(as_text=True)


def test_catalog_shows_supplier_tabs_only_when_there_are_several(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    html = page(client)
    assert 'class="tabs"' in html and "Все <span" in html and "WebMIM <span" in html and "Второй <span" in html
    assert ">A1<" in html and ">B1<" in html
    single = make_app(tmp_path / "x") if (tmp_path / "x").mkdir() is None else None
    c2 = single.test_client()
    login(c2)
    assert "Все <span" not in c2.get("/catalog").get_data(as_text=True)


def test_supplier_tab_filters_positions_and_brand_prefix_is_hidden(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    first = page(client, "?sup=1")
    assert ">A1<" in first and ">B1<" not in first
    second = page(client, "?sup=2")
    assert ">B1<" in second and ">A1<" not in second
    assert "S2:" not in second and '>IKON TYRES<' in second
    assert 'sup=2' in second  # the tab stays selected in links and forms


def test_positions_of_a_secondary_supplier_have_their_own_ids_and_rules(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    html = page(client, "?sup=2")
    assert 'value="off:s2-5"' in html and 'data-preview="s2-5"' in html
    post(client, "/catalog/stage", tok, op="off:s2-5")
    assert staging.summary(conn)["changes"]["overrides"] == {"1:s2-5": "off"}
    assert "снято вами" in page(client, "?sup=2") and "снято вами" not in page(client, "?sup=1")
    preview = client.get("/catalog/preview/s2-5").get_data(as_text=True)
    assert "B1" in preview


def test_webmim_tab_lists_suppliers_and_each_has_its_own_connection(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    html = client.get("/connections/webmim").get_data(as_text=True)
    assert 'class="tabs2"' in html and 'href="/connections/webmim/2"' in html and "Добавить поставщика" in html
    r = post(client, "/connections/suppliers", tok, name="Третий")
    assert r.status_code == 302 and r.headers["Location"].endswith("/connections/webmim/3")
    third = client.get("/connections/webmim/3").get_data(as_text=True)
    assert "Территория" in third and "Третий" in third
    post(client, "/connections/webmim", tok, supplier="3", base_url="https://c.example", mode="apikey", login="",
         territory_rn="777", api_key="K" * 32)
    cfg = connections.get_config(conn, "webmim", 3)
    assert cfg["base_url"] == "https://c.example" and cfg["territory_rn"] == "777"
    assert connections.get_config(conn, "webmim", 0) == {}            # the first supplier is untouched
    assert "K" * 32 not in client.get("/connections/webmim/3").get_data(as_text=True)
    assert client.get("/connections/webmim/99").status_code == 404


def test_the_first_supplier_territory_comes_from_general_settings_not_the_form(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    html = client.get("/connections/webmim/1").get_data(as_text=True)
    assert 'name="territory_rn"' not in html and "общих настройках" in html


def test_a_secondary_supplier_can_be_switched_off_by_an_empty_address_but_the_first_cannot(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    two = [sp for sp in connections.list_suppliers(conn) if sp["id"] != 1][0]["id"]
    assert post(client, "/connections/webmim", tok, supplier=str(two), base_url="", mode="apikey").status_code == 302
    assert connections.get_config(conn, "webmim", connections.supplier_owner(two)).get("base_url") == ""
    post(client, "/connections/webmim", tok, supplier="1", base_url="", mode="apikey")
    assert connections.get_config(conn, "webmim", 0).get("base_url", "x") != ""            # the first supplier keeps its address
    post(client, "/connections/webmim", tok, supplier=str(two), base_url="http://plain.example", mode="apikey")
    assert connections.get_config(conn, "webmim", connections.supplier_owner(two)).get("base_url") == ""   # a non-https address is still refused
