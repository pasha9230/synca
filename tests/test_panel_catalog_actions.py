from __future__ import annotations

from helpers import catalog_xml, commodity_xml
from panel_helpers import login, make_app, post
from tires_sync import staging, store
from tires_sync.settings_store import active_settings, confirm_draft, create_draft, get_active

T = "2026-10-07T10:00:00"
ROWS = (
    commodity_xml(NNOMMODIF="1", NREST="12", SMNFCODE="C1"),
    commodity_xml(NNOMMODIF="2", NREST="2", SMNFCODE="C2"),
    commodity_xml(NNOMMODIF="3", SDIAMETR="13", SMNFCODE="C3", NREST="9"),
)


def setup(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    tok = login(client)
    open(app.config["UNLOAD_PATH"], "wb").write(catalog_xml(*ROWS))
    return app, client, tok, store.connect(app.config["DB_PATH"])


def page(client, q=""):
    return client.get("/catalog" + q).get_data(as_text=True)


def test_position_buttons_exist_for_each_state(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    html = page(client)
    assert 'value="off:1"' in html          # in feed -> can be taken off
    assert 'value="on:2"' in html           # not in feed (low stock) -> can be added by hand
    assert 'value="on:3"' in html           # filtered out by diameter -> can be added by hand
    post(client, "/catalog/stage", tok, op="off:1")
    assert 'value="clear:1"' in page(client)


def test_taking_a_position_off_is_staged_and_applies_only_after_confirmation(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    r = post(client, "/catalog/stage", tok, op="off:1", next="/catalog?q=ikon")
    assert r.status_code == 302 and r.headers["Location"].endswith("/catalog?q=ikon")
    html = page(client)
    assert "Неподтверждённых изменений: 1" in html and "снято вами" in html
    assert active_settings(conn).overrides == {}
    review = client.get("/catalog/review")
    assert review.status_code == 302 and "/draft/" in review.headers["Location"]
    post(client, review.headers["Location"] + "/confirm", tok)
    assert active_settings(conn).overrides == {"1:1": "off"}
    assert 'id="pendbar" hidden' in page(client) and "снято вами" in page(client)


def test_adding_by_hand_and_clearing(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    post(client, "/catalog/stage", tok, op="on:3")
    assert "добавлено вручную" in page(client)
    post(client, "/catalog/stage", tok, op="clear:3")
    assert 'id="pendbar" hidden' in page(client)


def test_brand_season_switch_with_percents_and_whole_brand_buttons(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    post(client, "/settings/pricing", tok, scope="brand", supplier="1", brand="IKON TYRES",
         summer_form="1", summer_small="-2", summer_large="-2.5", summer_on="on", winter_form="1", winter_on="on")
    assert "Неподтверждённых изменений: 2" in page(client)
    post(client, "/catalog/cancel", tok)
    assert 'id="pendbar" hidden' in page(client)
    post(client, "/settings/pricing", tok, scope="brand_all", supplier="1", brand="IKON TYRES", enabled="off")
    assert staging.summary(conn)["changes"]["enabled"] == [["PIRELLI", "winter"]]
    post(client, "/settings/pricing", tok, scope="brand_all", supplier="1", brand="IKON TYRES", enabled="on")
    assert staging.summary(conn)["count"] == 0


def test_bad_input_is_explained_and_nothing_is_staged(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    post(client, "/settings/pricing", tok, scope="brand", supplier="1", brand="IKON TYRES", summer_small="", summer_large="3")
    assert staging.summary(conn)["count"] == 0
    assert "Процент" in client.get("/settings/brands").get_data(as_text=True)
    assert post(client, "/catalog/stage", tok, op="weird").status_code in (302, 400)
    assert staging.summary(conn)["count"] == 0


def test_stale_working_draft_blocks_staging_and_warns(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    post(client, "/catalog/stage", tok, op="off:1")
    confirm_draft(conn, create_draft(conn, {"min_stock": 6}, T), T, "owner")
    assert "устарел" in page(client)
    post(client, "/catalog/stage", tok, op="off:2")
    assert "1:2" not in str(staging.summary(conn)["changes"])
    assert "Отмените его" in page(client)
    post(client, "/catalog/cancel", tok)
    page(client)
    assert "Рабочий черновик устарел" not in page(client)


def test_manual_rules_without_a_position_are_listed(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    confirm_draft(conn, create_draft(conn, {"overrides": {"1:999": "on"}}, T), T, "owner")
    assert "Ручные правила без позиции" in page(client) and "999" in page(client)
