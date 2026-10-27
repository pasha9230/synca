"""The brands page of the 4Точки supplier before its feed switch: brands × seasons from the last snapshot, settable as for WebMIM, with a
banner; and the proof that nothing reaches the feeds while the switch is off (the owner 2026-10-27: he picks brands and markups first)."""
from __future__ import annotations

from datetime import datetime

from panel_helpers import post
from test_tochki_feed_on_panel import both, fresh, set_feed, tochki_of, write_snapshot
from test_tochki_panel import app_with, row
from tires_sync import staging, store
from tires_sync.panel import service
from tires_sync.settings_store import confirm_draft, create_draft

BANNER = "4Точки пока не в фиде: выбранные бренды и наценки начнут работать, когда включим фид 4Точек"
MISSING = "Снимок 4Точек ещё не загружен"
NOW = "2026-10-27T10:00:00"


def section(html, sid):
    start = html.index('id="pricing-%d"' % sid)
    end = html.find('<section class="card" id="pricing-', start + 10)
    return html[start:end if end > 0 else len(html)]


def test_switch_off_with_a_snapshot_lists_brands_and_seasons_with_counts_and_the_banner(tmp_path):
    app, client, tok, sid = app_with(tmp_path)
    write_snapshot(tmp_path, fresh(), [row("T1"), row("T2", width="215"), row("T3", season="s")])
    html = section(client.get("/settings/brands").get_data(as_text=True), sid)
    assert BANNER in html and MISSING not in html and "У поставщика нет позиций." not in html
    assert "IKON TYRES" in html and 'name="r0_winter_on"' in html and 'name="r0_summer_on"' in html
    conn = store.connect(app.config["DB_PATH"])
    rows = service.pricing_table(conn, sid, app.config["UNLOAD_PATH"])        # counts per season come from the snapshot
    assert [(r["brand"], r["seasons"]["winter"]["count"], r["seasons"]["summer"]["count"], r["seasons"]["all"]["count"]) for r in rows] == [
        ("S%d:IKON TYRES" % sid, 2, 1, 0)]


def test_switch_off_without_a_snapshot_says_so_and_still_has_the_banner(tmp_path):
    app, client, tok, sid = app_with(tmp_path)
    html = section(client.get("/settings/brands").get_data(as_text=True), sid)
    assert MISSING in html and BANNER in html and "У поставщика нет позиций." not in html


def test_switch_on_has_no_banner(tmp_path):
    app, client, tok, sid = app_with(tmp_path, feed=True)
    write_snapshot(tmp_path, fresh(), [row("T1")])
    html = section(client.get("/settings/brands").get_data(as_text=True), sid)
    assert BANNER not in html and "IKON TYRES" in html


def test_the_webmim_supplier_gets_no_banner_and_no_4tochki_brands(tmp_path):
    app, client, tok, sid = app_with(tmp_path)
    write_snapshot(tmp_path, fresh(), [row("T1")])
    first = section(client.get("/settings/brands").get_data(as_text=True), 1)
    assert BANNER not in first and MISSING not in first and "S%d:" % sid not in first


def test_brands_are_enabled_with_a_rule_through_the_same_flow_and_nothing_reaches_the_feed(tmp_path):
    app, client, tok, sid = app_with(tmp_path)
    write_snapshot(tmp_path, fresh(), [row("T1"), row("T2", width="215")])
    key = "S%d:IKON TYRES" % sid
    r = post(client, "/settings/pricing", tok, scope="brand", supplier=str(sid), brand=key, winter_small="-3", winter_large="-4",
             winter_form="1", winter_on="on", next="/settings/brands")
    assert r.status_code == 302
    assert client.get(r.headers["Location"]).status_code == 200            # the draft's confirmation screen opens with 4Точки brands in it
    conn = store.connect(app.config["DB_PATH"])
    changes = staging.summary(conn)["changes"]
    assert [key, "winter"] in changes["enabled"] and changes["pricing"][key + "|winter"] == {"pct_small": -3.0, "pct_large": -4.0}
    confirm_draft(conn, create_draft(conn, changes, NOW), NOW, "owner")           # confirmed: the brand is enabled in the active settings
    from tires_sync.settings_store import get_active
    assert [key, "winter"] in get_active(conn)[1]["enabled"]
    conn.close()
    html = section(client.get("/settings/brands").get_data(as_text=True), sid)
    assert BANNER in html
    panel, cycle = both(app)                                            # the switch is off: neither the panel's set nor the cycle's has 4Точки
    assert not tochki_of(panel, sid) and not tochki_of(cycle, sid)


def test_an_empty_snapshot_counts_as_missing(tmp_path):
    app, client, tok, sid = app_with(tmp_path)
    write_snapshot(tmp_path, fresh(), [])
    html = section(client.get("/settings/brands").get_data(as_text=True), sid)
    assert MISSING in html and BANNER in html


def test_the_exclusions_page_and_the_brand_card_open_for_4tochki_and_the_sets_stay_without_it(tmp_path):
    app, client, tok, sid = app_with(tmp_path)
    write_snapshot(tmp_path, fresh(), [row("T1")])
    assert client.get("/settings/exclusions").status_code == 200
    assert client.get("/settings/brands/card?supplier=%d&brand=S%d:IKON%%20TYRES" % (sid, sid)).status_code == 200
    panel, cycle = both(app)
    assert not tochki_of(panel, sid) and not tochki_of(cycle, sid) and sid in cycle.unavailable
