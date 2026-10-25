"""4tochki in the catalog, its search and the dashboard: only the brands we work with (the owner 2026-10-24, task 3: «от 4tochki показывать
только бренды, с которыми работаем — тот же фильтр, что уже выбран для МИМ»). A brand is worked when WebMIM has a brand of the same Avito
name switched on in any season, or the 4tochki brand itself is switched on. WebMIM, the mappings page and the economics are not filtered."""
from __future__ import annotations

import json

from test_tochki_panel import app_with, row
from tires_sync import store, tochki_feed
from tires_sync.panel import service
from tires_sync.panel.data import load_merged, load_reference
from tires_sync.settings_store import active_settings, confirm_draft, create_draft, get_active

NOW = "2026-10-25T10:00:00"


def other(code, marka, model="Other Model", **kw):
    r = row(code, model=model, **kw)
    r.update(marka=marka, name="%s %s 205/55 R16 94T" % (marka, model))
    return r


def pair_ikon(app):
    conn = store.connect(app.config["DB_PATH"])
    store.upsert_brand_map(conn, "IKON", "Ikon Tyres")
    conn.close()


def switch(app, enabled=None, excluded=None):
    conn = store.connect(app.config["DB_PATH"])
    changes = {}
    if enabled is not None:
        raw = get_active(conn)[1]
        changes["enabled"] = enabled
        changes["pricing"] = dict(raw["pricing"], **{"%s|%s" % (b, season): {"pct_small": 10.0, "pct_large": 10.0} for b, season in enabled})
    if excluded is not None:
        changes["excluded_brands"] = excluded
    confirm_draft(conn, create_draft(conn, changes, NOW), NOW, "owner")
    conn.close()


def catalog_ids(client):
    return client.get("/catalog?n=500").get_data(as_text=True)


def search_text(client, q):
    response = client.get("/search.json", query_string={"q": q})
    assert response.status_code == 200
    return json.dumps(response.get_json(), ensure_ascii=False)


def test_a_4tochki_brand_switched_on_at_webmim_by_its_avito_name_is_shown_and_others_are_not(tmp_path):
    # «IKON» of 4tochki (paired with the Avito brand on the mappings page) and «IKON TYRES» of WebMIM (switched on for winter in the test
    # settings) are one Avito brand «Ikon Tyres»
    app, client, tok, sid = app_with(tmp_path, rows=[other("T1", "IKON", model="Test Model"), other("T2", "Zeta Tyres"), other("T3", "Ikon Tyres", season="s")])
    pair_ikon(app)
    html = catalog_ids(client)
    assert "IKON Test Model 205/55 R16 94T" in html
    assert "Ikon Tyres Other Model 205/55 R16 94T" in html                    # the season does not matter: the brand is worked
    assert "Zeta Tyres" not in html
    assert "Zeta Tyres" not in search_text(client, "Zeta")
    assert "s%d-T1" % sid in search_text(client, "IKON Test Model")
    table = client.get("/catalog/table")
    assert table.status_code == 200
    assert "IKON Test Model 205/55 R16 94T" in table.get_data(as_text=True) and "Zeta" not in table.get_data(as_text=True)


def test_a_4tochki_brand_switched_on_for_4tochki_itself_is_shown(tmp_path):
    app, client, tok, sid = app_with(tmp_path, rows=[other("T2", "Zeta Tyres")])
    assert "Zeta Tyres" not in catalog_ids(client)
    switch(app, enabled=[["IKON TYRES", "winter"], ["PIRELLI", "winter"], ["S%d:ZETA TYRES" % sid, "summer"]])
    assert "Zeta Tyres Other Model 205/55 R16 94T" in catalog_ids(client)
    assert "s%d-T2" % sid in search_text(client, "Zeta")


def test_webmim_brands_that_are_off_stay_in_the_catalog(tmp_path):
    app, client, tok, sid = app_with(tmp_path, rows=[other("T2", "Zeta Tyres")])
    switch(app, enabled=[["PIRELLI", "winter"]])                         # IKON TYRES of WebMIM off: its WebMIM rows are still listed
    html = catalog_ids(client)
    assert ">TS1<" in html                                               # the WebMIM position 100 of IKON TYRES


def test_a_webmim_brand_switched_off_hides_the_4tochki_brand_of_the_same_avito_name(tmp_path):
    app, client, tok, sid = app_with(tmp_path, rows=[other("T1", "IKON", model="Test Model")])
    pair_ikon(app)
    assert "IKON Test Model 205/55 R16 94T" in catalog_ids(client)
    switch(app, enabled=[["PIRELLI", "winter"]])
    assert "IKON Test Model 205/55 R16 94T" not in catalog_ids(client)


def test_an_excluded_4tochki_brand_stays_hidden_even_when_switched_on(tmp_path):
    app, client, tok, sid = app_with(tmp_path, rows=[other("T2", "Zeta Tyres")])
    switch(app, enabled=[["S%d:ZETA TYRES" % sid, "summer"]], excluded=["S%d:ZETA TYRES" % sid])
    assert "Zeta Tyres" not in catalog_ids(client)


def test_the_mappings_page_still_lists_a_4tochki_brand_nobody_switched_on(tmp_path):
    app, client, tok, sid = app_with(tmp_path, rows=[other("T2", "Ikon Tyres", model="Brand New Model")])
    switch(app, enabled=[["PIRELLI", "winter"]])
    assert "Ikon Tyres Brand New Model" not in catalog_ids(client)
    assert "Brand New Model" in client.get("/mappings").get_data(as_text=True)


def test_a_brand_without_an_avito_name_is_compared_by_its_key(tmp_path):
    app, client, tok, sid = app_with(tmp_path, rows=[other("T2", "Zeta Tyres")])
    switch(app, enabled=[["IKON TYRES", "winter"], ["ZETA TYRES", "summer"]])          # a WebMIM brand Avito does not know
    assert "Zeta Tyres Other Model 205/55 R16 94T" in catalog_ids(client)


def test_without_conn_hide_excluded_is_what_it_was(tmp_path):
    """The economics and the cycle's picture call it without conn: nothing of 4tochki is filtered there."""
    app, client, tok, sid = app_with(tmp_path, rows=[other("T2", "Zeta Tyres")])
    conn = store.connect(app.config["DB_PATH"])
    parsed = load_merged(conn, active_settings(conn), app.config["UNLOAD_PATH"])
    view, _ = tochki_feed.catalog_view(conn, parsed, app.config["UNLOAD_PATH"])
    raw = get_active(conn)[1]
    assert service.hide_excluded(view, raw) is view
    kept = service.hide_excluded(view, raw, conn, load_reference(app.config["REFERENCE_PATH"]))
    assert "s%d-T2" % sid in view.offers and "s%d-T2" % sid not in kept.offers
    assert set(kept.offers) == {g for g in view.offers if not g.startswith("s%d-" % sid)}
    conn.close()


def test_the_dashboard_counts_the_catalog_as_it_is_shown(tmp_path):
    from tires_sync.panel import routes_main
    app, client, tok, sid = app_with(tmp_path, rows=[other("T1", "IKON", model="Test Model"), other("T2", "Zeta Tyres")])
    pair_ikon(app)
    conn = store.connect(app.config["DB_PATH"])
    parsed = load_merged(conn, active_settings(conn), app.config["UNLOAD_PATH"])
    _, shown = routes_main.catalog_scope(app, conn, parsed)
    assert "s%d-T1" % sid in shown.offers and "s%d-T2" % sid not in shown.offers
    conn.close()


def test_without_a_reference_the_brands_are_compared_by_their_keys(tmp_path):
    app, client, tok, sid = app_with(tmp_path, rows=[other("T1", "Ikon Tyres"), other("T2", "Zeta Tyres")])
    conn = store.connect(app.config["DB_PATH"])
    parsed = load_merged(conn, active_settings(conn), app.config["UNLOAD_PATH"])
    view, _ = tochki_feed.catalog_view(conn, parsed, app.config["UNLOAD_PATH"])
    for ref in (None, {}):
        kept = service.hide_excluded(view, get_active(conn)[1], conn, ref)
        assert "s%d-T1" % sid in kept.offers and "s%d-T2" % sid not in kept.offers, ref
    conn.close()


def test_a_brand_switched_on_for_another_supplier_does_not_make_the_4tochki_brand_worked(tmp_path):
    app, client, tok, sid = app_with(tmp_path, rows=[other("T2", "Zeta Tyres")])
    switch(app, enabled=[["IKON TYRES", "winter"], ["S%d:ZETA TYRES" % (sid + 5), "summer"]])
    assert "Zeta Tyres" not in catalog_ids(client)


def test_with_the_switch_on_the_4tochki_positions_of_the_feed_follow_the_same_rule(tmp_path):
    """«в фид» on: 4tochki comes from load_merged, not from the view; the catalog keeps only the worked brands too."""
    from helpers import make_offer
    app, client, tok, sid = app_with(tmp_path, rows=[other("T2", "Zeta Tyres")], feed=True)
    conn = store.connect(app.config["DB_PATH"])
    parsed = service._Filtered({o.goods_id: o for o in (
        make_offer(), make_offer(goods_id="s%d-A" % sid, brand_key="S%d:IKON TYRES" % sid),
        make_offer(goods_id="s%d-B" % sid, brand_key="S%d:ZETA TYRES" % sid))})
    kept = service.hide_excluded(parsed, get_active(conn)[1], conn, load_reference(app.config["REFERENCE_PATH"]))
    assert set(kept.offers) == {"100", "s%d-A" % sid}
    conn.close()
