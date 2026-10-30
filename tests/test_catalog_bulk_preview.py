from __future__ import annotations

import re

from helpers import catalog_xml, commodity_xml
from panel_helpers import login, make_app, post
from tires_sync import connections, staging, store

T = "2026-10-07T10:00:00"
ROWS = (
    commodity_xml(NNOMMODIF="1", SMNFCODE="C1", NREST="12"),
    commodity_xml(NNOMMODIF="2", SMNFCODE="C2", NREST="2"),
    commodity_xml(NNOMMODIF="3", SMNFCODE="C3", SDIAMETR="13", NREST="9"),
)


def setup(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    tok = login(client)
    open(app.config["UNLOAD_PATH"], "wb").write(catalog_xml(*ROWS))
    return app, client, tok, store.connect(app.config["DB_PATH"])


def test_bulk_take_off_and_add_by_hand_for_selected_positions(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    r = client.post("/catalog/stage", data={"csrf": tok, "bulk": "off", "g": ["1", "2"], "next": "/catalog?q=ikon"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/catalog?q=ikon")
    assert staging.summary(conn)["changes"]["overrides"] == {"1:1": "off", "1:2": "off"}
    client.post("/catalog/stage", data={"csrf": tok, "bulk": "on", "g": ["3"]})
    assert staging.summary(conn)["changes"]["overrides"]["1:3"] == "on"
    client.post("/catalog/stage", data={"csrf": tok, "bulk": "clear", "g": ["1", "2", "3"]})
    assert staging.summary(conn)["count"] == 0


def test_bulk_without_a_selection_or_with_a_bad_mode_changes_nothing(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    client.post("/catalog/stage", data={"csrf": tok, "bulk": "off"})
    assert "Выберите позиции" in client.get("/catalog").get_data(as_text=True)
    client.post("/catalog/stage", data={"csrf": tok, "bulk": "weird", "g": ["1"]})
    assert staging.summary(conn)["count"] == 0


def test_catalog_rows_have_checkboxes_and_a_bulk_bar(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    html = client.get("/catalog").get_data(as_text=True)
    assert 'name="g" value="1" form="bulk"' in html
    assert 'id="bulk"' in html and "Снять выбранные" in html and "Добавить вручную выбранные" in html and "Вернуть выбранные по правилам" in html
    assert 'data-preview="1"' in html


def test_preview_shows_the_ad_as_it_will_be_built(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    a = connections.create_account(conn, "A", T, "owner")
    connections.save_addresses(conn, a, "percent", None, [
        {"name": "Бета", "address_id": "101", "enabled": True, "share": 100, "address_text": "СПб, ул. Примерова, 35А"}], T, "owner", address_mode="text")
    html = client.get("/catalog/preview/1").get_data(as_text=True)
    assert "4810" in html and "Ikon  205/55/16" in html and "C1" in html
    assert "СПб, ул. Примерова, 35А" in html and "В наличии: 12 шт." in html
    assert "Заголовок" in html and "Описание" in html and "Количество" in html
    assert "<html" not in html  # a fragment for the dialog


def test_preview_of_a_position_that_is_not_built_explains_why_and_unknown_is_404(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    html = client.get("/catalog/preview/3").get_data(as_text=True)
    assert "не собирается" in html and "диаметр меньше минимального" in html
    assert "Заголовок" in html and "Описание" in html and "Количество" in html          # the ad is still shown under the warning
    assert client.get("/catalog/preview/999").status_code == 404
    assert app.test_client().get("/catalog/preview/1").status_code == 302


def test_catalog_is_one_table_paged_by_a_hundred_and_served_as_a_fragment(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    html = client.get("/catalog").get_data(as_text=True)
    table = html.split('id="items"')[1]                 # the brand filter and «Бренды в фиде» above it fold (<details>); the table never does
    assert ">C1<" in html and 'id="items"' in html and "lazymodels" not in html
    assert not re.findall(r'<details(?! class="own-cell")', table)       # only the owner's price cell opens (inline-price 2026-10-30): no row folds
    from tires_sync.panel import routes_catalog
    routes_catalog.PAGE_SIZE = 2
    try:
        first, second = client.get("/catalog?o=name").get_data(as_text=True), client.get("/catalog?o=name&p=2").get_data(as_text=True)
        assert "страница 1 из 2" in first and "страница 2 из 2" in second and 'class="pager"' in first
        assert set(re.findall(r">(C\d)<", first)).isdisjoint(re.findall(r">(C\d)<", second))
        assert "страница 2 из 2" in client.get("/catalog?p=99").get_data(as_text=True) or "страница 2 из 2" in client.get("/catalog?p=99&o=name").get_data(as_text=True)
    finally:
        routes_catalog.PAGE_SIZE = 100
    fragment = client.get("/catalog/table?q=ikon").get_data(as_text=True)
    assert ">C1<" in fragment and 'value="off:1"' in fragment and 'name="g" value="1" form="bulk"' in fragment and "<html" not in fragment
    assert app.test_client().get("/catalog/table").status_code == 302


def test_preview_of_a_position_without_a_price_percent_still_shows_the_ad_with_the_warning(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    login(client)
    laufenn = commodity_xml(NNOMMODIF="7", SSEASON="Летняя", SMARKA="Лауфенн", SMODIFNAME="LAUFENN 195/65/15 G Fit", SMODEL="G Fit",
                            SDIAMETR="15", SWIDTH="195", SHEIGHT="65", SMNFCODE="C7", NREST="8")
    open(app.config["UNLOAD_PATH"], "wb").write(catalog_xml(laufenn))
    html = client.get("/catalog/preview/7").get_data(as_text=True)
    assert "Объявление не собирается:" in html                                              # the warning stays
    assert "Заголовок" in html and "код C7" in html and "Описание" in html                  # and the ad is shown
    assert "процент не задан" in html                                                       # the price is not invented


def test_preview_of_a_hand_added_position_with_no_price_percent_keeps_the_exact_warning_and_shows_the_ad(tmp_path):
    from datetime import datetime
    from werkzeug.security import generate_password_hash
    from helpers import settings_dict
    from panel_helpers import REF_TEXT
    from tires_sync.panel import create_app
    from tires_sync.settings_store import init_settings
    db = tmp_path / "t.db"
    conn = store.connect(db)
    init_settings(conn, settings_dict(pricing={}, enabled=[], overrides={"1:1": "on"}), T)       # added by hand, but no percent
    conn.close()
    unload, ref = tmp_path / "unload.xml", tmp_path / "reference.txt"
    unload.write_bytes(catalog_xml(commodity_xml(NNOMMODIF="1", SMNFCODE="C1", NREST="12")))
    ref.write_text(REF_TEXT, encoding="utf-8")
    app = create_app({"TESTING": True, "SECRET_KEY": "k", "PASSWORD_HASH": generate_password_hash("pw", method="pbkdf2:sha256:1000"),
                      "DB_PATH": str(db), "UNLOAD_PATH": str(unload), "REFERENCE_PATH": str(ref), "NOW": lambda: datetime(2026, 10, 7, 12, 0, 0)})
    client = app.test_client()
    login(client)
    html = client.get("/catalog/preview/1").get_data(as_text=True)
    assert "Объявление не собирается: не задан процент цены." in html
    assert "Ikon  205/55/16" in html and "Заголовок" in html and "Описание" in html
    assert "процент не задан" in html and "4810" not in html                               # no price is made up
