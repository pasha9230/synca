"""4tochki with its switch «в фид» on, in the panel (run match4, lane B): the panel's set of positions (`panel.data.load_merged`) takes
4tochki exactly as the cycle does (`suppliers.load_all_parsed`), so the catalog shows them as ordinary positions, the photos of their ads
are served and the panel's publication builds the same feed as the cycle. With the switch off nothing changes (the golden tests of
`test_tochki_feed_unchanged.py` keep the feeds byte for byte)."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

from test_tochki_feed_unchanged import SNAPSHOT, TRIAL_1, build, fixed_clock, prepare  # noqa: F401  (fixed_clock: the golden day here too)
from test_tochki_panel import app_with, catalog_items, row
from tires_sync import connections, store, tochki_feed
from tires_sync.panel import service
from tires_sync.panel.data import load_merged
from tires_sync.settings_store import active_settings
from tires_sync.suppliers import load_all_parsed

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64


def fresh():
    return datetime.utcnow().isoformat(timespec="seconds")


def write_snapshot(tmp_path, fetched_at, rows):
    """A new snapshot file whose time of change differs from the previous one (a coarse clock of the file system cannot hide it)."""
    path = tmp_path / "tochki-snapshot.json"
    before = path.stat().st_mtime_ns if path.exists() else 0
    path.write_text(json.dumps({"version": 1, "fetched_at": fetched_at, "warehouse_id": 7, "items": rows}), encoding="utf-8")
    if path.stat().st_mtime_ns <= before:
        os.utime(path, ns=(before + 10**9, before + 10**9))


def set_feed(app, sid, feed):
    conn = store.connect(app.config["DB_PATH"])
    connections.set_config(conn, "tochki", 0, {"login": "api-user", "supplier_id": sid, "feed": feed}, "2026-10-25T10:00:00", "o")
    conn.commit()
    conn.close()


def both(app):
    """(the panel's set, the cycle's set) on the same base and files."""
    conn = store.connect(app.config["DB_PATH"])
    try:
        settings = active_settings(conn)
        return load_merged(conn, settings, app.config["UNLOAD_PATH"]), load_all_parsed(conn, settings, app.config["UNLOAD_PATH"])[0]
    finally:
        conn.close()


def tochki_of(parsed, sid):
    return {g: o for g, o in parsed.offers.items() if g.startswith("s%d-" % sid)}


def test_with_the_switch_on_the_panel_has_the_4tochki_positions_of_the_cycle(tmp_path):
    app, client, tok, sid = app_with(tmp_path, feed=True)
    write_snapshot(tmp_path, fresh(), [row("T1"), row("T2", width="215", load="95")])
    panel, cycle = both(app)
    assert set(tochki_of(panel, sid)) == {"s%d-T1" % sid, "s%d-T2" % sid}
    assert tochki_of(panel, sid) == tochki_of(cycle, sid)                       # the same positions, field by field
    assert sid not in panel.unavailable and panel.unavailable == cycle.unavailable
    assert set(panel.offers) == set(cycle.offers) and panel.issues == cycle.issues and panel.stocks == cycle.stocks


def test_with_the_switch_on_the_catalog_and_the_search_show_them_as_ordinary_positions(tmp_path):
    app, client, tok, sid = app_with(tmp_path, feed=True)
    write_snapshot(tmp_path, fresh(), [row("T1", rest=41)])
    items, parsed = catalog_items(app)
    one = items["s%d-T1" % sid]
    assert not one["view_only"] and one["reason"] != service.VIEW_ONLY_TEXT and one["stock_text"] == ">40"
    assert one["supplier_mark"]["text"] == "4Т"
    assert "s%d-T1" % sid in parsed.offers
    html = client.get("/catalog?n=500").get_data(as_text=True)
    assert "Ikon Tyres Test Model 205/55 R16 94T" in html and service.VIEW_ONLY_TEXT not in html
    found = client.get("/search.json", query_string={"q": "Test Model"}).get_data(as_text=True)
    assert "s%d-T1" % sid in found


def test_with_the_switch_on_an_old_empty_or_missing_load_is_held_as_by_the_cycle(tmp_path):
    app, client, tok, sid = app_with(tmp_path, feed=True)
    old = (datetime.utcnow() - timedelta(days=30)).isoformat(timespec="seconds")
    for case in ("old", "empty", "missing", "no date"):
        snapshot = tmp_path / "tochki-snapshot.json"
        if case == "old":
            write_snapshot(tmp_path, old, [row("T1", rest=2)])
        elif case == "empty":
            write_snapshot(tmp_path, fresh(), [])
        elif case == "no date":
            write_snapshot(tmp_path, "", [row("T1", rest=2)])
        elif snapshot.exists():
            snapshot.unlink()
        panel, cycle = both(app)
        assert not tochki_of(panel, sid), case
        assert sid in panel.unavailable and panel.unavailable == cycle.unavailable, case
        assert panel.stocks == cycle.stocks, case                                # a known fall below the threshold is known to both
        items, _parsed = catalog_items(app)
        assert not any(g.startswith("s%d-" % sid) for g in items), case
    write_snapshot(tmp_path, old, [row("T1", rest=2)])
    assert both(app)[0].stocks.get("s%d-T1" % sid) == 2


def test_a_load_time_with_a_zone_fails_no_page_and_holds_the_supplier(tmp_path):
    """The load writes UTC without a zone; a time with one (a file put by hand) is compared in UTC (`suppliers.file_is_stale`, run tails3).
    The panel must not fail on every page: an old one holds the supplier as any old load."""
    app, client, tok, sid = app_with(tmp_path, feed=True)
    write_snapshot(tmp_path, "2026-10-07T12:00:00+03:00", [row("T1", rest=2)])
    conn = store.connect(app.config["DB_PATH"])
    panel = load_merged(conn, active_settings(conn), app.config["UNLOAD_PATH"])
    conn.close()
    assert not tochki_of(panel, sid) and sid in panel.unavailable and panel.stocks.get("s%d-T1" % sid) == 2
    assert client.get("/catalog").status_code == 200 and client.get("/").status_code == 200



def test_a_fresh_load_time_with_a_zone_is_taken_by_the_panel_as_by_the_cycle(tmp_path):
    app, client, tok, sid = app_with(tmp_path, feed=True)
    zoned = (datetime.utcnow() - timedelta(hours=1)).replace(microsecond=0).isoformat() + "+00:00"
    write_snapshot(tmp_path, zoned, [row("T1", rest=2)])
    panel, cycle = both(app)
    assert set(tochki_of(panel, sid)) == set(tochki_of(cycle, sid)) == {"s%d-T1" % sid}
    assert sid not in panel.unavailable and sid not in cycle.unavailable
    assert client.get("/catalog").status_code == 200

def test_the_cache_follows_the_switch_and_the_snapshot(tmp_path):
    app, client, tok, sid = app_with(tmp_path, feed=False)
    write_snapshot(tmp_path, fresh(), [row("T1")])
    assert not tochki_of(both(app)[0], sid)                                     # off: what builds feeds never sees them
    set_feed(app, sid, True)
    assert set(tochki_of(both(app)[0], sid)) == {"s%d-T1" % sid}
    write_snapshot(tmp_path, fresh(), [row("T1"), row("T3", width="225")])
    assert set(tochki_of(both(app)[0], sid)) == {"s%d-T1" % sid, "s%d-T3" % sid}
    set_feed(app, sid, False)
    panel = both(app)[0]
    assert not tochki_of(panel, sid) and sid in panel.unavailable              # as before: no WebMIM file of that row


def test_a_load_that_grows_old_while_the_file_stays_is_held_at_once(tmp_path, monkeypatch):
    """The snapshot is not rewritten, only the clock goes on: the cached set must not keep publishing its prices past the cycle's limit."""
    from tires_sync.panel import data
    app, client, tok, sid = app_with(tmp_path, feed=True)
    conn = store.connect(app.config["DB_PATH"])
    days = max(1, active_settings(conn).stale_days)
    conn.close()
    start = datetime(2026, 10, 25, 12, 0, 0)
    write_snapshot(tmp_path, (start - timedelta(days=days, minutes=-1)).isoformat(timespec="seconds"), [row("T1")])

    def clock_at(moment):
        class Clock(datetime):
            @classmethod
            def utcnow(cls):
                return moment
        monkeypatch.setattr(data, "datetime", Clock)
    clock_at(start)
    assert set(tochki_of(both(app)[0], sid)) == {"s%d-T1" % sid}
    clock_at(start + timedelta(minutes=2))
    panel = both(app)[0]
    assert not tochki_of(panel, sid) and sid in panel.unavailable


def test_another_supplier_row_for_4tochki_is_a_new_set(tmp_path):
    app, client, tok, sid = app_with(tmp_path, feed=True)
    write_snapshot(tmp_path, fresh(), [row("T1")])
    assert set(tochki_of(both(app)[0], sid)) == {"s%d-T1" % sid}
    conn = store.connect(app.config["DB_PATH"])
    other = connections.create_supplier(conn, "4 точки (второй)", "2026-10-25T10:00:00", "o")
    conn.commit()
    conn.close()
    set_feed(app, other, True)
    panel = both(app)[0]
    assert not tochki_of(panel, sid) and set(tochki_of(panel, other)) == {"s%d-T1" % other}


def test_a_row_chosen_by_itself_never_brings_4tochki_into_the_panels_set(tmp_path):
    app, client, tok, sid = app_with(tmp_path, feed=False)
    write_snapshot(tmp_path, fresh(), [row("T1")])
    conn = store.connect(app.config["DB_PATH"])
    connections.set_config(conn, "tochki", 0, {"login": "api-user", "feed": True}, "2026-10-25T10:00:00", "o")    # no saved row
    conn.commit()
    assert tochki_feed.config(conn)["auto"] and not tochki_feed.config(conn)["feed"]
    conn.close()
    assert not tochki_of(both(app)[0], sid)


def test_with_the_switch_on_the_photo_of_a_4tochki_ad_is_served(tmp_path):
    app, client, tok, sid = app_with(tmp_path, feed=True)
    write_snapshot(tmp_path, fresh(), [row("T1")])
    calls = []

    def fetch(url):
        calls.append(url)
        return JPEG, "jpg"
    app.config.update(PHOTO_FETCH=fetch, IMG_ROOT=str(tmp_path / "img"))
    conn = store.connect(app.config["DB_PATH"])
    account = connections.create_account(conn, "Партнёр-2", "2026-10-25T10:00:00", "o")
    token = connections.get_account(conn, account)["feed_token"]
    conn.close()
    answer = client.get("/img/%s/s%d-T1.jpg" % (token, sid))
    assert answer.status_code == 200 and answer.data == JPEG and calls == ["https://img.4tochki.test/T1.jpg"]
    set_feed(app, sid, False)
    assert client.get("/img/%s/s%d-T2.jpg" % (token, sid)).status_code == 404     # off: not a position of any feed


def _panel_feeds_on(tmp_path) -> dict:
    """The panel's publication (publish_now) with the switch on, the clock of the golden day as the cycle's."""
    from werkzeug.security import generate_password_hash
    from tires_sync.panel import create_app
    from tires_sync.panel.routes_drafts import publish_now
    prepare(tmp_path, feed_switch=True, trial=TRIAL_1)
    app = create_app({"TESTING": True, "SECRET_KEY": "s", "PASSWORD_HASH": generate_password_hash("pw", method="pbkdf2:sha256:1000"),
                      "DB_PATH": str(tmp_path / "t.db"), "UNLOAD_PATH": str(tmp_path / "unload.xml"), "REFERENCE_PATH": str(tmp_path / "reference.txt"),
                      "FEED_ROOT": str(tmp_path / "feed"), "NOW": lambda: datetime(2026, 10, 21, 0, 0, 0)})
    with app.test_request_context():
        lines = publish_now(app) or []
    # «Не публикуется: …, публикация выключена» is expected: the accounts of the scenario are not live, their drafts (next/) are compared
    trouble = ("не собран", "не пересобран", "остановлен", "не осталось", "обновляется другим процессом")
    assert not [line for line in lines if any(word in line for word in trouble)], lines
    conn = store.connect(tmp_path / "t.db")
    out = {}
    for account in connections.list_accounts(conn):
        token = account["feed_token"]
        out[account["name"]] = (tmp_path / "feed" / token / "next" / "avito_feed.xml").read_text(encoding="utf-8").replace(token, "TOKEN")
    conn.close()
    return out


def test_with_the_switch_on_the_panels_publication_builds_the_feed_of_the_cycle(tmp_path):
    (tmp_path / "cycle").mkdir()
    (tmp_path / "panel").mkdir()
    SNAPSHOT["fetched_at"] = fresh()
    try:
        cycle = build(tmp_path / "cycle", feed_switch=True, trial=TRIAL_1)
        panel = _panel_feeds_on(tmp_path / "panel")
    finally:
        SNAPSHOT["fetched_at"] = "2026-10-14T09:30:00"
    assert set(panel) == set(cycle) == {"Основной", "Партнёр-2"}
    assert "<Id>s2-TCH900</Id>" in panel["Основной"] and "s2-" not in panel["Партнёр-2"]          # the trial: account 1 only
    for name in cycle:
        assert panel[name] == cycle[name], name
