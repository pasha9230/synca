from __future__ import annotations

from datetime import date

import pytest

from helpers import catalog_xml, commodity_xml, settings_dict
from panel_helpers import login, make_app, post
from tires_sync import connections, cycle, publishing, staging, store
from tires_sync.catalog import parse_reference
from tires_sync.feed import FeedCheckError
from tires_sync.publish import PublishError, promote_feed
from tires_sync.settings_store import active_settings, confirm_draft, create_draft, init_settings

T = "2026-10-07T10:00:00"
REF = parse_reference('<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>')
TODAY = date(2026, 10, 7)


def goods(n, start=1):
    return catalog_xml(*[commodity_xml(NNOMMODIF=str(i), NREST="12", SMNFCODE="C%d" % i) for i in range(start, start + n)])


def panel(tmp_path, count=8, guard_min=2):
    app = make_app(tmp_path, FEED_ROOT=str(tmp_path / "feed"))
    client = app.test_client()
    tok = login(client)
    open(app.config["UNLOAD_PATH"], "wb").write(goods(count))
    conn = store.connect(app.config["DB_PATH"])
    confirm_draft(conn, create_draft(conn, {"guard_min": guard_min, "guard_fraction": 0.0, "portion_limit": 100}, T), T, "owner")
    connections.create_account(conn, "A", T, "owner")
    connections.set_live(conn, 1, True, T, "owner")
    folder = tmp_path / "feed" / connections.get_account(conn, 1)["feed_token"]
    # publish everything once so that there is something to remove
    cycle.run_for_accounts(conn, goods(count), REF, TODAY, tmp_path / "feed")
    publishing.publish_live_accounts(conn, tmp_path / "feed")
    return app, client, tok, conn, folder


# --- C1: many manual offs are a deliberate removal, not a guard trip -------------------------------------------------

def test_many_manual_offs_are_confirmed_on_the_review_screen_and_really_remove_the_ads(tmp_path):
    app, client, tok, conn, folder = panel(tmp_path)
    for g in ("1", "2", "3"):
        staging.set_override(conn, g, "off", T)
    d = staging.summary(conn)["draft_id"]
    review = client.get("/draft/%d" % d).get_data(as_text=True)
    assert "Исчезнет" in review and 'name="understand"' in review
    refused = post(client, "/draft/%d/confirm" % d, tok, publish="1")
    assert refused.headers["Location"].endswith("/draft/%d" % d)
    assert len(store.load_published(conn, account_id=1)) == 8
    post(client, "/draft/%d/confirm" % d, tok, publish="1", understand="on")
    text = (folder / "avito_feed.xml").read_text(encoding="utf-8")
    assert all("<Id>%s</Id>" % g not in text for g in ("1", "2", "3")) and "<Id>4</Id>" in text
    assert set(store.load_published(conn, account_id=1)) == {"4", "5", "6", "7", "8"}


# --- C2 / I1: the acknowledgement is bounded by what was reviewed -----------------------------------------------------

def test_the_cycle_shrink_check_allows_exactly_the_reviewed_removals(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(portion_limit=100), T)
    connections.create_account(conn, "A", T, "owner")
    cycle.run_for_accounts(conn, goods(10), REF, TODAY, tmp_path / "feed")
    publishing.publish_live_accounts(conn, tmp_path / "feed")  # not live: nothing recorded
    from tires_sync.sync import run_cycle
    from tires_sync.models import PublishedItem
    for i in range(1, 11):
        store.upsert_published(conn, PublishedItem(str(i), 4810, 5000.0, "P1"), "<Ad><Id>%d</Id></Ad>" % i, account_id=1)
    s = active_settings(conn)
    with pytest.raises(FeedCheckError):
        run_cycle(goods(4), s, REF, conn, TODAY, out_dir=tmp_path / "o1", account_id=1)
    with pytest.raises(FeedCheckError):
        run_cycle(goods(4), s, REF, conn, TODAY, out_dir=tmp_path / "o2", account_id=1, expected_removals=3)
    run_cycle(goods(4), s, REF, conn, TODAY, out_dir=tmp_path / "o3", account_id=1, expected_removals=6)


def test_promote_allows_a_big_shrink_only_up_to_the_expected_removals(tmp_path):
    folder = tmp_path / "f"
    (folder / "next").mkdir(parents=True)
    (folder / "avito_feed.xml").write_text("<Ads>" + "<Ad/>" * 10 + "</Ads>")
    (folder / "next" / "avito_feed.xml").write_text("<Ads>" + "<Ad/>" * 4 + "</Ads>")
    with pytest.raises(PublishError):
        promote_feed(folder)
    with pytest.raises(PublishError):
        promote_feed(folder, expected_removals=3)
    assert promote_feed(folder, expected_removals=6) == {"new": 4, "old": 10}


def test_understand_no_longer_switches_off_the_mass_removal_guard(tmp_path):
    app, client, tok, conn, folder = panel(tmp_path)
    for g in ("1", "2", "3"):
        staging.set_override(conn, g, "off", T)
    d = staging.summary(conn)["draft_id"]
    post(client, "/draft/%d/confirm" % d, tok, publish="1", understand="on")
    assert active_settings(conn).allow_mass_removal is False


def test_a_confirmed_big_removal_without_publish_does_not_break_the_next_cycles(tmp_path):
    app, client, tok, conn, folder = panel(tmp_path, count=10, guard_min=2)
    for g in range(1, 9):
        staging.set_override(conn, str(g), "off", T)
    d = staging.summary(conn)["draft_id"]
    post(client, "/draft/%d/confirm" % d, tok, understand="on")           # plain apply, no publish
    results = cycle.run_for_accounts(conn, goods(10), REF, TODAY, tmp_path / "feed")
    assert all(r.ok for r in results), [r.error for r in results]            # the timer cycle must not fail
    out = publishing.publish_live_accounts(conn, tmp_path / "feed")
    assert out[0]["ok"] and len(store.load_published(conn, account_id=1)) == 2
    # once published the acknowledgement is spent; an unrelated shrink fails again
    conn2 = store.connect(app.config["DB_PATH"])
    assert publishing.expected_removals(conn2, active_settings(conn2).version) == 0


# --- I3: the review says which positions are touched ---------------------------------------------------------------------

def test_review_lists_each_manual_rule_by_name_and_warns_about_low_stock(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    tok = login(client)
    open(app.config["UNLOAD_PATH"], "wb").write(catalog_xml(
        commodity_xml(NNOMMODIF="1", SMNFCODE="C1"), commodity_xml(NNOMMODIF="2", NREST="2", SMNFCODE="C2")))
    conn = store.connect(app.config["DB_PATH"])
    staging.set_override(conn, "1", "off", T)
    staging.set_override(conn, "2", "on", T)
    html = client.get("/draft/%d" % staging.summary(conn)["draft_id"]).get_data(as_text=True)
    assert "Снять: " in html and "Добавить вручную: " in html and "(C1)" in html and "(C2)" in html
    assert "остаток 2 ниже порога" in html
    assert "overrides: —" not in html
