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


def test_the_undo_button_says_what_it_undoes(tmp_path):
    """The owner 2026-10-31: no bare «Сбросить» in «Действия»: added by hand → «Снять ручные правила», taken off → «Вернуть в фид»."""
    app, client, tok, conn = setup(tmp_path)
    post(client, "/catalog/stage", tok, op="off:1")
    post(client, "/catalog/stage", tok, op="on:2")
    html = page(client)
    off = html.split('value="clear:1"', 1)[1].split("</button>", 1)[0]
    on = html.split('value="clear:2"', 1)[1].split("</button>", 1)[0]
    assert "Вернуть в фид" in off and "Ручное снятие отменится" in off
    assert "Снять ручные правила" in on and "Позиция пойдёт по правилам фида" in on
    assert "Вернуть выбранные по правилам" in html and ">Сбросить</button>" not in html


def test_taking_a_position_off_is_staged_and_applies_only_after_confirmation(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    r = post(client, "/catalog/stage", tok, op="off:1", next="/catalog?q=ikon")
    assert r.status_code == 302 and r.headers["Location"].endswith("/catalog?q=ikon")
    html = page(client)
    assert "Не применено: 1 правка" in html and "снято вами" in html      # the bar of the settings pages on the catalog too (2026-10-30)
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
    assert "Не применено: 2 правки" in page(client)
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
    rules = client.get("/settings/exclusions").get_data(as_text=True)            # «Настройки → Исключения» (the owner 2026-10-30)
    assert "Ручные правила без позиции (1)" in rules and 'data-orphan="999"' in rules
    assert "Ручные правила без позиции" not in page(client)


def test_a_rule_without_a_position_says_its_tyre_when_it_went_who_set_it_and_goes_by_the_draft(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    conn.execute("INSERT INTO catalog_item (goods_id, supplier_id, data, first_seen, last_seen, gone_since) VALUES ('999', 1, ?, ?, ?, ?)",
                 (_offer_json(), "2026-10-01T10:00:00", "2026-10-20T10:00:00", "2026-10-21T09:00:00"))
    confirm_draft(conn, create_draft(conn, {"overrides": {"1:999": "on", "1:998": "off"}}, T), T, "pavel")
    rules = client.get("/settings/exclusions").get_data(as_text=True)
    row = rules[rules.index('data-orphan="999"'):].split("</tr>")[0]
    assert "Старая шина" in row and "добавлено вручную" in row and "21.10" in row and "pavel" in row and "для всех аккаунтов" in row
    assert "снято вручную" in rules[rules.index('data-orphan="998"'):].split("</tr>")[0]
    assert '<details class="card" id="orphans"' in rules and "<details class=\"card\" id=\"orphans\" open" not in rules      # folded
    post(client, "/catalog/stage", tok, op="clear:999", next="/settings/exclusions#orphans")
    assert "1:999" not in staging.summary(conn)["changes"]["overrides"]          # into the working draft, as from the catalog
    post(client, "/catalog/stage", tok, bulk="clear", g=["999", "998"], next="/settings/exclusions#orphans")
    assert staging.summary(conn)["changes"]["overrides"] == {}


def _offer_json():
    from helpers import make_offer
    from tires_sync.catalog_archive import _dump
    return _dump(make_offer(goods_id="999", name="Старая шина", name_orig="Старая шина"))
