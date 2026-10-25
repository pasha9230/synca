from __future__ import annotations

import re

from helpers import catalog_xml, commodity_xml
from panel_helpers import login, make_app, post
from tires_sync import connections, cycle, publishing, staging, store
from tires_sync.catalog import parse_reference
from tires_sync.settings_store import active_settings, confirm_draft, create_draft, get_active

T = "2026-10-07T10:00:00"


def app_with(tmp_path, *rows, guard_min=50):
    app = make_app(tmp_path, FEED_ROOT=str(tmp_path / "feed"))
    client = app.test_client()
    tok = login(client)
    open(app.config["UNLOAD_PATH"], "wb").write(catalog_xml(*rows))
    conn = store.connect(app.config["DB_PATH"])
    confirm_draft(conn, create_draft(conn, {"guard_min": guard_min, "guard_fraction": 0.0}, T), T, "owner")
    return app, client, tok, conn


def many(n, **kw):
    return [commodity_xml(NNOMMODIF=str(i), SMNFCODE="C%d" % i, NREST="12", **kw) for i in range(1, n + 1)]


# M1: confirm applies exactly what was reviewed -------------------------------------------------------------------

def test_confirm_refuses_when_the_working_draft_changed_after_the_review(tmp_path):
    app, client, tok, conn = app_with(tmp_path, *many(3))
    staging.set_override(conn, "1", "off", T)
    d = staging.summary(conn)["draft_id"]
    review = client.get("/draft/%d" % d).get_data(as_text=True)
    rev = re.search(r'name="rev" value="([0-9a-f]+)"', review).group(1)
    staging.set_override(conn, "2", "on", T)                      # a second tab adds something meanwhile
    r = post(client, "/draft/%d/confirm" % d, tok, rev=rev)
    assert r.headers["Location"].endswith("/draft/%d" % d)
    assert active_settings(conn).overrides == {}
    fresh = client.get("/draft/%d" % d).get_data(as_text=True)
    assert "изменился после просмотра" in fresh
    rev2 = re.search(r'name="rev" value="([0-9a-f]+)"', fresh).group(1)
    post(client, "/draft/%d/confirm" % d, tok, rev=rev2)
    assert active_settings(conn).overrides == {"1:1": "off", "1:2": "on"}


# M2: staging is one transaction and never edits a closed draft --------------------------------------------------

def test_staging_after_the_draft_was_confirmed_starts_a_fresh_draft(tmp_path):
    app, client, tok, conn = app_with(tmp_path, *many(3))
    staging.set_override(conn, "1", "off", T)
    first = staging.summary(conn)["draft_id"]
    confirm_draft(conn, first, T, "owner")
    staging.set_override(conn, "2", "off", T)
    second = staging.summary(conn)["draft_id"]
    assert second != first
    rows = dict(conn.execute("SELECT id, status FROM draft").fetchall())
    assert rows[first] == "confirmed" and rows[second] == "open"
    assert conn.execute("SELECT COUNT(*) FROM draft WHERE status = 'open'").fetchone()[0] == 1


# M3: "return the brand" restores what was on before ------------------------------------------------------------

def test_returning_a_brand_restores_only_the_seasons_that_were_on(tmp_path):
    app, client, tok, conn = app_with(tmp_path, *many(3))
    staging.set_brand_state(conn, [("IKON TYRES", "summer", -3.0, -3.0, False)], T)   # a price rule exists but the season is off
    confirm_draft(conn, staging.summary(conn)["draft_id"], T, "owner")
    staging.set_brand_enabled(conn, "IKON TYRES", False, T)
    staging.set_brand_enabled(conn, "IKON TYRES", True, T)
    assert staging.summary(conn)["count"] == 0                          # back to exactly what was active


# M4: a manual "on" survives a brand switch-off, and the owner is told ----------------------------------------

def test_switching_a_brand_off_warns_about_positions_added_by_hand(tmp_path):
    app, client, tok, conn = app_with(tmp_path, *many(3))
    confirm_draft(conn, create_draft(conn, {"overrides": {"1:2": "on"}}, T), T, "owner")
    r = post(client, "/settings/pricing", tok, scope="brand_all", supplier="1", brand="IKON TYRES", enabled="off")
    page = client.get(r.headers["Location"]).get_data(as_text=True)
    assert "добавленных вручную" in page and "останутся в фиде" in page


# M5: a held / published position can always be taken off ----------------------------------------------------

def test_a_published_position_with_a_problem_still_offers_take_off(tmp_path):
    app, client, tok, conn = app_with(tmp_path, *many(2))
    from tires_sync.models import PublishedItem
    connections.create_account(conn, "A", T, "owner")
    store.upsert_published(conn, PublishedItem("1", 4810, 5000.0, "P1"), "<Ad><Id>1</Id></Ad>", account_id=1)
    open(app.config["UNLOAD_PATH"], "wb").write(catalog_xml(
        commodity_xml(NNOMMODIF="1", SMNFCODE="C1", NREST="1"), commodity_xml(NNOMMODIF="2", SMNFCODE="C2", NREST="1")))
    page = client.get("/catalog").get_data(as_text=True)
    assert 'value="off:1"' in page and 'value="on:2"' in page


# M6: there is no hysteresis any more: a forced "on" position never waits for extra stock ------------------------------------

def test_forced_item_that_was_just_removed_for_low_stock_does_not_wait_for_extra_stock(tmp_path):
    app, client, tok, conn = app_with(tmp_path, commodity_xml(NNOMMODIF="1", SMNFCODE="C1", NREST="1"))
    confirm_draft(conn, create_draft(conn, {"overrides": {"1:1": "on"}}, T), T, "owner")
    connections.create_account(conn, "A", T, "owner")
    conn.execute("INSERT INTO suppressed (account_id, goods_id) VALUES (1, '1')")
    conn.commit()
    page = client.get("/catalog").get_data(as_text=True)
    assert "ждёт восстановления остатка" not in page


# M7: the guard threshold is the active one, not the draft's own ---------------------------------------------

def test_a_draft_cannot_loosen_its_own_removal_guard(tmp_path):
    app, client, tok, conn = app_with(tmp_path, *many(4), guard_min=0)
    d = create_draft(conn, {"enabled": [], "guard_min": 100}, T)
    html = client.get("/draft/%d" % d).get_data(as_text=True)
    assert 'name="understand"' in html


# M8: a rollback is guarded like any other change --------------------------------------------------------------

def test_rollback_that_removes_many_ads_needs_the_extra_confirmation(tmp_path):
    app, client, tok, conn = app_with(tmp_path, *many(4), guard_min=0)
    confirm_draft(conn, create_draft(conn, {"enabled": []}, T), T, "owner")
    off_version = get_active(conn)[0]
    confirm_draft(conn, create_draft(conn, {"enabled": [["IKON TYRES", "winter"]]}, T), T, "owner")
    refused = post(client, "/history/rollback/%d" % off_version, tok, confirm="on")
    assert refused.status_code == 302
    assert active_settings(conn).enabled != frozenset()                 # nothing rolled back
    assert "больше порога защиты" in client.get("/history").get_data(as_text=True)
    post(client, "/history/rollback/%d" % off_version, tok, confirm="on", understand="on")
    assert active_settings(conn).enabled == frozenset()


# M9/M10: catalog shows sizes in models and searches by the Avito brand name -------------------------------------

def test_models_table_shows_sizes_with_stock_and_search_matches_the_avito_brand(tmp_path):
    app, client, tok, conn = app_with(tmp_path, commodity_xml(NNOMMODIF="1", SMNFCODE="C1", NREST="12"))
    page = client.get("/catalog").get_data(as_text=True)
    assert "12 шт." in page and "205/55/16" in page
    assert ">C1<" in client.get("/catalog?q=ikon tyres").get_data(as_text=True)
    assert ">C1<" in client.get("/catalog?q=Ikon").get_data(as_text=True)
