from __future__ import annotations

import re

import pytest

from helpers import catalog_xml, commodity_xml, settings_dict
from panel_helpers import login, make_app, post
from tires_sync import staging, store
from tires_sync.settings_store import confirm_draft, get_active, init_settings

T = "2026-10-07T10:00:00"
ROWS = (
    commodity_xml(NNOMMODIF="1", SMNFCODE="C1"),                                            # Ikon, winter
    commodity_xml(NNOMMODIF="2", SSEASON="Летняя", SMNFCODE="C2"),                           # Ikon, summer
    commodity_xml(NNOMMODIF="4", SSEASON="Летняя", SMARKA="Лауфенн", SMODIFNAME="LAUFENN 195/65/15 G Fit",
                  SMODEL="G Fit", SDIAMETR="15", SWIDTH="195", SHEIGHT="65", SMNFCODE="C4"),  # Laufenn, summer
)


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "t.db")
    init_settings(c, settings_dict(), T)
    return c


def test_pricing_is_set_without_touching_what_is_published(conn):
    staging.set_brand_state(conn, [("KUMHO", "summer", -2.0, -3.0, None), ("KUMHO", "winter", 1.0, 1.5, None)], T)
    changes = staging.summary(conn)["changes"]
    assert changes["pricing"]["KUMHO|summer"] == {"pct_small": -2.0, "pct_large": -3.0}
    assert changes["pricing"]["KUMHO|winter"] == {"pct_small": 1.0, "pct_large": 1.5}
    assert "enabled" not in changes
    staging.set_brand_state(conn, [("IKON TYRES", "winter", -3.82, -4.28, None)], T)       # the very same values: nothing new
    assert staging.summary(conn)["count"] == 2


@pytest.mark.parametrize("entry", [("K", "winter", None, 1.0, None), ("K", "spring", 1.0, 1.0, None), ("K", "winter", float("nan"), 1.0, None)])
def test_bad_pricing_is_refused_and_nothing_is_staged(conn, entry):
    with pytest.raises(ValueError):
        staging.set_brand_state(conn, [entry], T)
    assert staging.summary(conn)["count"] == 0


def test_season_switch_needs_a_price_rule_or_gets_it_in_the_same_change(conn):
    with pytest.raises(ValueError):
        staging.set_brand_state(conn, [("KUMHO", "summer", None, None, True)], T)
    staging.set_brand_state(conn, [("KUMHO", "summer", -2.0, -3.0, True)], T)
    assert ["KUMHO", "summer"] in staging.summary(conn)["changes"]["enabled"]
    staging.set_brand_state(conn, [("KUMHO", "summer", None, None, True)], T)          # the rule exists now
    staging.set_brand_state(conn, [("KUMHO", "summer", None, None, False)], T)
    assert ["KUMHO", "summer"] not in staging.summary(conn)["changes"].get("enabled", [])


def panel(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    tok = login(client)
    open(app.config["UNLOAD_PATH"], "wb").write(catalog_xml(*ROWS))
    return app, client, tok, store.connect(app.config["DB_PATH"])


def test_settings_page_lists_brands_with_their_percents_per_season(tmp_path):
    app, client, tok, conn = panel(tmp_path)
    html = client.get("/settings/brands").get_data(as_text=True)
    assert "Бренды: калькуляция и публикация" in html and 'action="/settings/pricing"' in html
    assert re.search(r'name="r\d+_winter_small" value="-3.82"', html) and re.search(r'name="r\d+_winter_large" value="-4.28"', html)
    assert 'name="r0_summer_small"' in html and 'value="bulk"' in html and 'value="table"' in html


def test_one_brand_is_changed_alone(tmp_path):
    app, client, tok, conn = panel(tmp_path)
    r = post(client, "/settings/pricing", tok, scope="brand", supplier="1", brand="IKON TYRES",
             winter_small="-2", winter_large="-2,5", summer_small="1", summer_large="2", all_small="", all_large="")
    assert r.status_code == 302 and r.headers["Location"].endswith("/draft/1")          # straight to the confirmation
    pricing = staging.summary(conn)["changes"]["pricing"]
    assert pricing["IKON TYRES|winter"] == {"pct_small": -2.0, "pct_large": -2.5}
    assert pricing["IKON TYRES|summer"] == {"pct_small": 1.0, "pct_large": 2.0}
    assert pricing["PIRELLI|winter"] == {"pct_small": -5.85, "pct_large": -6.2} and "IKON TYRES|all" not in pricing
    assert staging.summary(conn)["count"] == 2
    assert post(client, "/draft/1/cancel", tok).headers["Location"].endswith("/settings/brands#pricing-1")


def test_bulk_change_covers_every_brand_and_season_the_supplier_has(tmp_path):
    app, client, tok, conn = panel(tmp_path)
    post(client, "/settings/pricing", tok, scope="bulk", supplier="1", season="summer", small="-4", large="-5")
    pricing = staging.summary(conn)["changes"]["pricing"]
    assert pricing["IKON TYRES|summer"] == {"pct_small": -4.0, "pct_large": -5.0}
    assert pricing["LAUFENN|summer"] == {"pct_small": -4.0, "pct_large": -5.0}
    assert pricing["IKON TYRES|winter"] == {"pct_small": -3.82, "pct_large": -4.28}   # winter was not asked for
    post(client, "/settings/pricing", tok, scope="bulk", supplier="1", season="*", small="-1", large="-1")
    pricing = staging.summary(conn)["changes"]["pricing"]
    assert pricing["IKON TYRES|winter"] == {"pct_small": -1.0, "pct_large": -1.0}
    assert "LAUFENN|winter" not in pricing and "LAUFENN|all" not in pricing       # this brand has no winter goods


def test_bad_percent_is_explained_and_changes_nothing(tmp_path):
    app, client, tok, conn = panel(tmp_path)
    post(client, "/settings/pricing", tok, scope="bulk", supplier="1", season="*", small="abc", large="1")
    assert staging.summary(conn)["count"] == 0
    assert "число" in client.get("/settings/brands").get_data(as_text=True)
    assert post(client, "/settings/pricing", tok, scope="bulk", supplier="99", season="*", small="1", large="1").status_code == 404


def test_catalog_has_no_brand_controls_any_more(tmp_path):
    app, client, tok, conn = panel(tmp_path)
    html = client.get("/catalog").get_data(as_text=True)
    assert 'name="small"' not in html and 'name="large"' not in html and 'name="enabled"' not in html
    assert "/settings/brands" in html and "/mappings" in html
    assert post(client, "/catalog/stage", tok, action="brand_season", brand="IKON TYRES", season="summer", enabled="on").status_code == 302
    assert staging.summary(conn)["count"] == 0


def test_odd_pricing_requests_are_refused_without_touching_the_draft(tmp_path):
    app, client, tok, conn = panel(tmp_path)
    assert post(client, "/settings/pricing", tok, scope="bulk", supplier="x", season="*", small="1", large="1").status_code == 404
    post(client, "/settings/pricing", tok, scope="weird", supplier="1")
    post(client, "/settings/pricing", tok, scope="brand", supplier="1", brand="NOBODY", winter_small="1", winter_large="1")
    post(client, "/settings/pricing", tok, scope="brand", supplier="1", brand="IKON TYRES", winter_small="1", winter_large="")
    post(client, "/settings/pricing", tok, scope="bulk", supplier="1", season="spring", small="1", large="1")
    assert staging.summary(conn)["count"] == 0


# --- publishing switches live next to the percents ------------------------------------------------------------------

def test_brand_state_sets_percents_and_switches_in_one_change(conn):
    staging.set_brand_state(conn, [("KUMHO", "summer", -2.0, -3.0, True), ("IKON TYRES", "winter", None, None, False)], T)
    changes = staging.summary(conn)["changes"]
    assert ["KUMHO", "summer"] in changes["enabled"] and ["IKON TYRES", "winter"] not in changes["enabled"]
    assert changes["pricing"]["KUMHO|summer"] == {"pct_small": -2.0, "pct_large": -3.0}


def test_switching_on_without_any_percent_is_refused(conn):
    with pytest.raises(ValueError):
        staging.set_brand_state(conn, [("KUMHO", "summer", None, None, True)], T)
    assert staging.summary(conn)["count"] == 0


def test_settings_row_has_a_publish_checkbox_per_season(tmp_path):
    app, client, tok, conn = panel(tmp_path)
    html = client.get("/settings/brands").get_data(as_text=True)
    assert re.search(r'name="r\d+_winter_on" checked', html) and 'name="r0_summer_on"' in html and re.search(r'name="r\d+_winter_form"', html)
    assert "Снять с фида" in html and "Добавить в фид" in html       # IKON is in the feed, LAUFENN is not


def test_row_form_switches_a_season_on_and_off(tmp_path):
    app, client, tok, conn = panel(tmp_path)
    post(client, "/settings/pricing", tok, scope="brand", supplier="1", brand="IKON TYRES",
         winter_form="1", summer_form="1", summer_small="-1", summer_large="-1", summer_on="on")
    enabled = staging.summary(conn)["changes"]["enabled"]
    assert ["IKON TYRES", "summer"] in enabled and ["IKON TYRES", "winter"] not in enabled      # winter box was left empty
    assert staging.summary(conn)["count"] == 3


def test_whole_brand_buttons_switch_every_season(tmp_path):
    app, client, tok, conn = panel(tmp_path)
    post(client, "/settings/pricing", tok, scope="brand_all", supplier="1", brand="IKON TYRES", enabled="off")
    assert staging.summary(conn)["changes"]["enabled"] == [["PIRELLI", "winter"]]
    post(client, "/settings/pricing", tok, scope="brand_all", supplier="1", brand="IKON TYRES", enabled="on")
    assert staging.summary(conn)["count"] == 0


def test_bulk_switch_touches_only_seasons_with_a_price_rule(tmp_path):
    app, client, tok, conn = panel(tmp_path)
    post(client, "/settings/pricing", tok, scope="bulk", supplier="1", season="*", small="", large="", publish="on")
    assert staging.summary(conn)["count"] == 0                      # winter is on already, summer has no percents yet
    assert "без процентов" in client.get("/settings/brands").get_data(as_text=True)
    post(client, "/settings/pricing", tok, scope="bulk", supplier="1", season="summer", small="-1", large="-1", publish="on")
    enabled = {tuple(i) for i in staging.summary(conn)["changes"]["enabled"]}
    assert ("IKON TYRES", "summer") in enabled and ("LAUFENN", "summer") in enabled
    post(client, "/settings/pricing", tok, scope="bulk", supplier="1", season="summer", small="", large="", publish="off")
    assert ("LAUFENN", "summer") not in {tuple(i) for i in staging.summary(conn)["changes"].get("enabled", [])}


def test_brand_row_shows_how_many_models_wait_for_a_pair_with_a_link(tmp_path):
    app, client, tok, conn = panel(tmp_path)
    open(app.config["UNLOAD_PATH"], "wb").write(catalog_xml(
        commodity_xml(NNOMMODIF="1", SMNFCODE="C1"), commodity_xml(NNOMMODIF="2", SMODEL="Weird 9000", SMNFCODE="C2"),
        commodity_xml(NNOMMODIF="3", SMARKA="Неизвестный", SMODIFNAME="UNKNOWN 205/55/16 X", SMODEL="X", SMNFCODE="C3")))
    html = client.get("/settings/brands").get_data(as_text=True)
    assert 'href="/mappings?b=IKON%20TYRES"' in html and "не сопоставлено: 1" in html
    assert 'href="/mappings#brands"' in html and "бренд не сопоставлен" in html


# --- review fixes -----------------------------------------------------------------------------------------------------

def test_bulk_switch_off_also_reaches_a_season_that_has_no_goods_today(tmp_path):
    app, client, tok, conn = panel(tmp_path)
    open(app.config["UNLOAD_PATH"], "wb").write(catalog_xml(commodity_xml(NNOMMODIF="1", SMNFCODE="C1")))     # Ikon winter only; Laufenn gone
    from tires_sync.settings_store import confirm_draft, create_draft
    confirm_draft(conn, create_draft(conn, {"enabled": [["IKON TYRES", "winter"], ["LAUFENN", "summer"]],
                                            "pricing": {"IKON TYRES|winter": {"pct_small": -3.0, "pct_large": -3.0},
                                                        "LAUFENN|summer": {"pct_small": -3.0, "pct_large": -3.0}}}, T), T, "owner")
    html = client.get("/settings/brands").get_data(as_text=True)
    assert "LAUFENN" in html and re.search(r'name="r\d+_summer_on" checked', html)                  # visible and switchable off
    post(client, "/settings/pricing", tok, scope="bulk", supplier="1", season="*", small="", large="", publish="off")
    assert staging.summary(conn)["changes"]["enabled"] == []


def test_nothing_changed_says_so_instead_of_silence(tmp_path):
    app, client, tok, conn = panel(tmp_path)
    r = post(client, "/settings/pricing", tok, scope="brand_all", supplier="1", brand="IKON TYRES", enabled="on")
    assert r.headers["Location"].endswith("/settings/brands#pricing-1")
    assert "Изменений нет" in client.get("/settings/brands").get_data(as_text=True)


def test_one_percent_without_the_other_is_explained_in_plain_words(tmp_path):
    app, client, tok, conn = panel(tmp_path)
    post(client, "/settings/pricing", tok, scope="brand", supplier="1", brand="IKON TYRES", summer_small="1", summer_large="")
    assert "оба процента" in client.get("/settings/brands").get_data(as_text=True)
    post(client, "/settings/pricing", tok, scope="bulk", supplier="1", season="*", small="2", large="")
    assert staging.summary(conn)["count"] == 0


def test_switching_on_without_percents_names_the_brand_and_the_season(conn):
    with pytest.raises(ValueError) as err:
        staging.set_brand_state(conn, [("S2:KUMHO", "summer", None, None, True)], T)
    assert "KUMHO" in str(err.value) and "лето" in str(err.value) and "S2:" not in str(err.value)


def test_the_confirmation_screen_leads_back_to_the_page_where_you_were_working(tmp_path):
    app, client, tok, conn = panel(tmp_path)
    r = post(client, "/settings/pricing", tok, scope="brand", supplier="1", brand="IKON TYRES", summer_form="1",
             summer_small="1", summer_large="1", summer_on="on")
    page = client.get(r.headers["Location"]).get_data(as_text=True)
    assert 'href="/settings/brands#pricing-1">Бренды и наценки<' in page and "Вернуться к правке" in page


def test_controls_of_a_brand_row_name_the_brand_for_screen_readers(tmp_path):
    app, client, tok, conn = panel(tmp_path)
    html = client.get("/settings/brands").get_data(as_text=True)
    assert 'aria-label="IKON TYRES, зима: публиковать"' in html and 'aria-label="Снять бренд IKON TYRES с фида"' in html
    assert 'aria-label="Применить только для бренда IKON TYRES"' in html and 'aria-current="page"' in html


# --- brand card -------------------------------------------------------------------------------------------------------

def test_every_brand_has_a_card_with_its_three_seasons(tmp_path):
    app, client, tok, conn = panel(tmp_path)
    table = client.get("/settings/brands").get_data(as_text=True)
    assert 'href="/settings/brands/card?supplier=1&brand=IKON%20TYRES"' in table
    html = client.get("/settings/brands/card?supplier=1&brand=IKON%20TYRES").get_data(as_text=True)
    assert "Настройки бренда IKON TYRES" in html and "Снять весь бренд" in html and "Вернуть весь бренд" in html
    assert html.count('class="season-card') == 3 and 'name="winter_small" value="-3.82"' in html
    assert 'name="winter_on" checked' in html and 'name="summer_on"' in html and "Зима" in html and "Лето" in html and "Всесезонные" in html
    assert client.get("/settings/brands/card?supplier=1&brand=NOPE").status_code == 404
    assert client.get("/settings/brands/card?supplier=9&brand=IKON%20TYRES").status_code == 404
    assert client.get("/settings/brands/card?supplier=x&brand=IKON%20TYRES").status_code == 404
    assert app.test_client().get("/settings/brands/card?supplier=1&brand=IKON%20TYRES").status_code == 302


def test_a_change_made_on_the_card_returns_to_the_card(tmp_path):
    app, client, tok, conn = panel(tmp_path)
    card = "/settings/brands/card?supplier=1&brand=IKON%20TYRES"
    r = post(client, "/settings/pricing", tok, scope="brand", supplier="1", brand="IKON TYRES", next=card,
             summer_form="1", summer_small="-1", summer_large="-2", summer_on="on")
    assert r.headers["Location"].endswith("/draft/1")
    assert post(client, "/draft/1/cancel", tok).headers["Location"].endswith(card)
    nothing = post(client, "/settings/pricing", tok, scope="brand_all", supplier="1", brand="IKON TYRES", enabled="on", next=card)
    assert nothing.headers["Location"].endswith(card)
    evil = post(client, "/settings/pricing", tok, scope="brand_all", supplier="1", brand="IKON TYRES", enabled="on", next="//evil.example")
    assert evil.headers["Location"].endswith("/settings/brands#pricing-1")
    other = post(client, "/settings/pricing", tok, scope="brand_all", supplier="1", brand="IKON TYRES", enabled="on", next="/history")
    assert other.headers["Location"].endswith("/settings/brands#pricing-1")


def test_pending_changes_are_marked_on_the_card(tmp_path):
    app, client, tok, conn = panel(tmp_path)
    post(client, "/settings/pricing", tok, scope="brand", supplier="1", brand="IKON TYRES",
         summer_form="1", summer_small="-1", summer_large="-2", summer_on="on")
    html = client.get("/settings/brands/card?supplier=1&brand=IKON%20TYRES").get_data(as_text=True)
    assert "изменено, не подтверждено" in html and "is-pend" in html


# --- the whole table in one go ---------------------------------------------------------------------------------------

def table_form(**fields):
    base = {"scope": "table", "supplier": "1", "r0_brand": "IKON TYRES", "r1_brand": "LAUFENN"}
    base.update(fields)
    return base


def test_several_brands_and_seasons_are_chosen_in_one_submit(tmp_path):
    app, client, tok, conn = panel(tmp_path)
    r = post(client, "/settings/pricing", tok, **table_form(
        r0_winter_form="1", r0_summer_form="1", r0_summer_small="-1", r0_summer_large="-2", r0_summer_on="on",   # Ikon: winter off, summer on
        r1_summer_form="1", r1_summer_small="3", r1_summer_large="3", r1_summer_on="on"))                          # Laufenn summer on
    assert r.headers["Location"].endswith("/draft/1")
    enabled = {tuple(i) for i in staging.summary(conn)["changes"]["enabled"]}
    assert enabled == {("PIRELLI", "winter"), ("IKON TYRES", "summer"), ("LAUFENN", "summer")}


def test_only_one_row_is_applied_when_its_own_button_is_pressed(tmp_path):
    app, client, tok, conn = panel(tmp_path)
    post(client, "/settings/pricing", tok, **table_form(
        only="1", r0_winter_form="1", r1_summer_form="1", r1_summer_small="3", r1_summer_large="3", r1_summer_on="on"))
    enabled = {tuple(i) for i in staging.summary(conn)["changes"]["enabled"]}
    assert enabled == {("IKON TYRES", "winter"), ("PIRELLI", "winter"), ("LAUFENN", "summer")}      # Ikon winter untouched, though unticked


def test_a_ticked_season_without_percents_is_refused_with_the_brand_named(tmp_path):
    app, client, tok, conn = panel(tmp_path)
    post(client, "/settings/pricing", tok, **table_form(r1_summer_form="1", r1_summer_on="on"))
    assert staging.summary(conn)["count"] == 0
    html = client.get("/settings/brands").get_data(as_text=True)
    assert "LAUFENN" in html and "не заданы проценты" in html


def test_an_untouched_table_changes_nothing_and_a_foreign_brand_is_refused(tmp_path):
    app, client, tok, conn = panel(tmp_path)
    r = post(client, "/settings/pricing", tok, **table_form(r0_winter_form="1", r0_winter_on="on", r0_winter_small="-3.82", r0_winter_large="-4.28"))
    assert r.headers["Location"].endswith("/settings/brands#pricing-1") and staging.summary(conn)["count"] == 0
    post(client, "/settings/pricing", tok, **table_form(r1_brand="NOBODY", r1_summer_form="1", r1_summer_on="on"))
    assert staging.summary(conn)["count"] == 0 and "нет бренда" in client.get("/settings/brands").get_data(as_text=True)
    post(client, "/settings/pricing", tok, scope="table", supplier="1")
    assert "Нечего применять" in client.get("/settings/brands").get_data(as_text=True)


# --- one toggle per brand row: «Снять с фида» / «Добавить в фид», removed rows greyed ---------------------------------

def _row_of(html, brand):
    rows = re.findall(r"<tr[^>]*>.*?</tr>", html, re.S)
    return next(r for r in rows if 'value="%s"' % brand in r and "_brand" in r)


def test_brand_row_has_one_toggle_that_follows_the_state(tmp_path):
    app, client, tok, conn = panel(tmp_path)
    html = client.get("/settings/brands").get_data(as_text=True)
    row = _row_of(html, "IKON TYRES")
    assert "Снять с фида" in row and "Добавить в фид" not in row
    assert 'aria-label="Снять бренд IKON TYRES с фида"' in row
    assert "is-off" not in row.split(">", 1)[0]
    assert "Вернуть бренд" not in html and ">Снять бренд<" not in html
    post(client, "/settings/pricing", tok, scope="brand_all", supplier="1", brand="IKON TYRES", enabled="off")
    html = client.get("/settings/brands").get_data(as_text=True)
    row = _row_of(html, "IKON TYRES")
    assert "Добавить в фид" in row and "Снять с фида" not in row
    assert 'aria-label="Добавить бренд IKON TYRES в фид"' in row
    assert "is-off" in row.split(">", 1)[0]
    laufenn = _row_of(html, "LAUFENN")                                    # never switched on: removed from the start
    assert "is-off" in laufenn.split(">", 1)[0] and "Добавить в фид" in laufenn


def test_toggle_buttons_post_exactly_what_the_old_buttons_posted(tmp_path):
    app, client, tok, conn = panel(tmp_path)
    html = client.get("/settings/brands").get_data(as_text=True)
    row = _row_of(html, "IKON TYRES")
    form_id = re.search(r'form="(ba-1-\d+)"', row).group(1)
    assert re.search(r'name="enabled" value="off"', row)
    assert re.search(r'<form id="%s" method="post" action="/settings/pricing"' % form_id, html)
    assert 'name="scope" value="brand_all"' in html.split('id="%s"' % form_id, 1)[1][:400]
    post(client, "/settings/pricing", tok, scope="brand_all", supplier="1", brand="IKON TYRES", enabled="off")
    row = _row_of(client.get("/settings/brands").get_data(as_text=True), "IKON TYRES")
    assert re.search(r'name="enabled" value="on"', row) and 'form="%s"' % form_id in row


def test_partner_sees_removed_brand_grey_with_a_label_and_no_button(tmp_path):
    from tires_sync import users
    app, client, tok, conn = panel(tmp_path)
    post(client, "/settings/pricing", tok, scope="brand_all", supplier="1", brand="IKON TYRES", enabled="off")
    users.add_user(conn, "partner1", "длинный-пароль-1", T, role="partner")
    viewer = app.test_client()
    with viewer.session_transaction() as session:
        session["user"], session["epoch"], session["csrf"] = "partner1", store.get_session_epoch(conn, "partner1"), "tok"
    page = viewer.get("/settings/brands")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    row = _row_of(html, "IKON TYRES")
    assert "is-off" in row.split(">", 1)[0] and "снят с фида" in row
    assert "Добавить в фид" not in html and "Снять с фида" not in html
    denied = viewer.post("/settings/pricing", data={"csrf": "tok", "scope": "brand_all", "supplier": "1", "brand": "LAUFENN", "enabled": "off"})
    assert denied.status_code == 403
