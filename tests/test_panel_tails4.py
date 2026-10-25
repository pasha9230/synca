"""Run tails4, lane A: the panel decides like the cycle. A secondary WebMIM unload older than the cycle takes (`suppliers.stale_load`) gives
the panel only its last known stock, as the cycle (catalog, publication from the panel); the catalog mark «есть у другого поставщика» keeps
the owner's «это не пара» marks as the feed does, and a forgotten remembered pair (memory only) is still shown when both sides are here."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from werkzeug.security import generate_password_hash

from helpers import catalog_xml, commodity_xml
from test_multi_supplier_cycle import ONE, TWO, files, prepared
from test_tire_match import mim, mim_offers, tochki_offers, tochki_row
from test_tire_match_catalog import BADGE
from test_tochki_panel import app_with, row
from tires_sync import cli, connections, store, suppliers, tire_match
from tires_sync.panel import create_app, data
from tires_sync.panel.data import load_merged
from tires_sync.settings_store import active_settings

NOW = "2026-10-14T10:00:00"


def second(tmp_path, *items, fetched=None):
    (tmp_path / "unload-2.xml").write_bytes(catalog_xml(*(commodity_xml(**{"TERRITORY_RN": "2002", "NPRICE_DELAYED": "5000", **kw}) for kw in items)))
    if fetched is not None:
        (tmp_path / "unload-2.xml.meta.json").write_text(json.dumps({"fetched_at": fetched, "items": len(items)}), encoding="utf-8")


def old():
    return (datetime.utcnow() - timedelta(days=30)).isoformat(timespec="seconds")


def both(tmp_path):
    """(the panel's set, the cycle's set) of the same files."""
    conn = store.connect(tmp_path / "t.db")
    try:
        settings = active_settings(conn)
        return load_merged(conn, settings, tmp_path / "unload.xml"), suppliers.load_all_parsed(conn, settings, tmp_path / "unload.xml")[0]
    finally:
        conn.close()


def panel(tmp_path):
    return create_app({"TESTING": True, "SECRET_KEY": "test-secret", "PASSWORD_HASH": generate_password_hash("pw", method="pbkdf2:sha256:1000"),
                       "DB_PATH": str(tmp_path / "t.db"), "UNLOAD_PATH": str(tmp_path / "unload.xml"),
                       "REFERENCE_PATH": str(tmp_path / "reference.txt"), "FEED_ROOT": str(tmp_path / "feed"),
                       "NOW": datetime.utcnow})                   # the clock of the cycle runs here too


# --- 1: a stale second WebMIM unload ---------------------------------------------------------------------------------------------

def test_a_stale_second_unload_gives_the_panel_what_it_gives_the_cycle(tmp_path):
    prepared(tmp_path).close()
    files(tmp_path, ONE)
    second(tmp_path, dict(NNOMMODIF="5", NREST="2"), dict(NNOMMODIF="6", NREST="9"), fetched=old())
    in_panel, in_cycle = both(tmp_path)
    assert set(in_panel.offers) == set(in_cycle.offers) == {"5"}                  # no positions nor prices of the old file
    assert 2 in in_panel.unavailable and in_panel.unavailable == in_cycle.unavailable
    assert in_panel.stocks == in_cycle.stocks == {"s2-5": 2, "s2-6": 9}          # its last known stock, as the cycle


def test_a_fresh_second_unload_or_one_without_a_time_is_used_as_before(tmp_path):
    prepared(tmp_path).close()
    files(tmp_path, ONE)
    second(tmp_path, dict(NNOMMODIF="5"), fetched=datetime.utcnow().isoformat(timespec="seconds"))
    in_panel, in_cycle = both(tmp_path)
    assert set(in_panel.offers) == set(in_cycle.offers) == {"5", "s2-5"} and 2 not in in_panel.unavailable
    (tmp_path / "unload-2.xml.meta.json").unlink()                                # no time known: the file is used, as in the cycle
    in_panel, in_cycle = both(tmp_path)
    assert set(in_panel.offers) == set(in_cycle.offers) == {"5", "s2-5"}


def test_the_cached_set_holds_the_supplier_once_the_clock_makes_its_file_old(tmp_path, monkeypatch):
    """The file is not rewritten, only the clock goes on: the panel must not keep its prices past the cycle's limit."""
    prepared(tmp_path).close()
    files(tmp_path, ONE)
    conn = store.connect(tmp_path / "t.db")
    days = max(1, active_settings(conn).stale_days)
    conn.close()
    start = datetime(2026, 10, 25, 12, 0, 0)
    second(tmp_path, dict(NNOMMODIF="5"), fetched=(start - timedelta(days=days, minutes=-1)).isoformat(timespec="seconds"))

    def clock_at(moment):
        class Clock(datetime):
            @classmethod
            def utcnow(cls):
                return moment
        monkeypatch.setattr(data, "datetime", Clock)
    clock_at(start)
    assert "s2-5" in both(tmp_path)[0].offers
    clock_at(start + timedelta(minutes=2))
    found = both(tmp_path)[0]
    assert "s2-5" not in found.offers and 2 in found.unavailable


def test_the_catalog_shows_a_stale_second_unload_for_viewing_only_and_its_pages_answer(tmp_path):
    """Run tails5 (lead, 2026-10-25): the positions of an old file are shown for viewing only (tests/test_panel_stale_ux.py), not hidden;
    they still have no preview, no checkbox, no action, and the panel never publishes them (test_the_panel_publication_holds_...)."""
    prepared(tmp_path).close()
    files(tmp_path, ONE)
    second(tmp_path, dict(NNOMMODIF="5"), fetched=datetime.utcnow().isoformat(timespec="seconds"))
    app = panel(tmp_path)
    client = app.test_client()
    from panel_helpers import login
    login(client)
    assert 'data-preview="s2-5"' in client.get("/catalog").get_data(as_text=True)      # fresh: shown
    second(tmp_path, dict(NNOMMODIF="5"), fetched=old())
    html = client.get("/catalog").get_data(as_text=True)
    assert client.get("/").status_code == 200
    assert 'data-preview="s2-5"' not in html and "s2-5" not in html                  # nothing to act on the position by its id
    assert html.count("data-stale-unload>выгрузка устарела") == 1                    # shown, marked as an old unload


def test_readiness_says_a_stale_second_unload_is_old_not_empty(tmp_path):
    from tires_sync.panel import service
    prepared(tmp_path).close()
    files(tmp_path, ONE)
    second(tmp_path, dict(NNOMMODIF="5"), fetched="2026-09-25T09:00:00")
    conn = store.connect(tmp_path / "t.db")
    settings = active_settings(conn)
    steps = service.readiness(conn, tmp_path / "unload.xml", load_merged(conn, settings, tmp_path / "unload.xml"), {}, settings)
    conn.close()
    step = next(s for s in steps if s["title"] == "Поставщик «Второй»")
    assert step["level"] == "warn" and "выгрузка устарела (скачана 25.09.2026 12:00" in step["detail"] and "держатся" in step["detail"]


def test_holding_a_stale_file_leaves_the_cached_parse_of_it_whole(tmp_path):
    prepared(tmp_path).close()
    files(tmp_path, ONE)
    second(tmp_path, dict(NNOMMODIF="5", NREST="7"), fetched=old())
    assert 2 in both(tmp_path)[0].unavailable
    cached = data.load_parsed(tmp_path / "unload-2.xml", "2002", 2)
    assert set(cached.offers) == {"s2-5"} and 2 not in cached.unavailable


def test_a_broken_or_empty_second_file_is_taken_by_the_panel_as_by_the_cycle(tmp_path):
    prepared(tmp_path).close()
    files(tmp_path, ONE)
    for fetched in (None, old()):
        (tmp_path / "unload-2.xml").write_bytes(b"<broken")
        if fetched:
            (tmp_path / "unload-2.xml.meta.json").write_text(json.dumps({"fetched_at": fetched, "items": 1}), encoding="utf-8")
        in_panel, in_cycle = both(tmp_path)
        assert set(in_panel.offers) == set(in_cycle.offers) == {"5"} and in_panel.unavailable == in_cycle.unavailable
        (tmp_path / "unload-2.xml").write_bytes(catalog_xml(commodity_xml(NNOMMODIF="5", TERRITORY_RN="9999")))   # no row of its territory
        in_panel, in_cycle = both(tmp_path)
        assert set(in_panel.offers) == set(in_cycle.offers) == {"5"} and in_panel.unavailable == in_cycle.unavailable


def test_the_panel_publication_holds_a_stale_second_unload_as_the_cycle(tmp_path):
    from tires_sync.panel.routes_drafts import apply_publish
    conn = prepared(tmp_path)
    connections.set_live(conn, 1, True, NOW, "owner")
    conn.close()
    args = files(tmp_path, ONE, TWO)
    assert cli.main(["run-cycle", "--publish"] + args) == 0
    second(tmp_path, dict(NNOMMODIF="5", SLOAD="100", NPRICE_DELAYED="9000"), dict(NNOMMODIF="6", SMNFCODE="C6"), fetched=old())
    conn = store.connect(tmp_path / "t.db")
    before = store.load_published(conn, 1)
    conn.close()
    app = panel(tmp_path)
    with app.test_request_context():
        apply_publish(app)
    conn = store.connect(tmp_path / "t.db")
    after = store.load_published(conn, 1)
    token = connections.get_account(conn, 1)["feed_token"]
    conn.close()
    assert set(after) == set(before) == {"5", "s2-5"}                            # the new position of the old file is not published
    feed = (tmp_path / "feed" / token / "avito_feed.xml").read_text(encoding="utf-8")
    assert "9000" not in feed and "9 000" not in feed and "9450" not in feed      # nor the price of the old file for the held one
    assert cli.main(["run-cycle", "--publish"] + args) == 3                       # the cycle on the same files: the same set
    conn = store.connect(tmp_path / "t.db")
    assert set(store.load_published(conn, 1)) == {"5", "s2-5"}
    conn.close()


# --- 2: the catalog mark and the owner's marks on pairs --------------------------------------------------------------------------

def test_a_not_a_pair_mark_takes_the_catalog_mark_off_and_its_undo_brings_it_back(tmp_path):
    app, client, tok, sid = app_with(tmp_path, rows=[row("T1", rest=41)])         # WebMIM 100 and 4tochki T1: one tire
    assert len(BADGE.findall(client.get("/catalog").get_data(as_text=True))) == 2
    conn = store.connect(app.config["DB_PATH"])
    store.mark_not_pair(conn, "100", "s%d-T1" % sid, True, NOW, "owner")
    conn.commit()
    assert not BADGE.findall(client.get("/catalog").get_data(as_text=True))
    store.mark_not_pair(conn, "100", "s%d-T1" % sid, False, NOW, "owner")
    conn.commit()
    conn.close()
    assert len(BADGE.findall(client.get("/catalog").get_data(as_text=True))) == 2


def test_a_forgotten_pair_with_both_sides_here_keeps_its_mark_as_the_feed_merges_it(tmp_path):
    """«Забыть» acts on the memory only (`usable_pairs`): with both sides in the unload `for_feed` still pairs them, so the mark stays."""
    app, client, tok, sid = app_with(tmp_path, rows=[row("T1", rest=41)])
    conn = store.connect(app.config["DB_PATH"])
    store.add_tire_pairs(conn, [("100", "s%d-T1" % sid, "a", "b")], NOW)
    assert store.mark_tire_pair(conn, "100", "s%d-T1" % sid, True, NOW, "owner")
    conn.commit()
    assert store.forgotten_tire_pairs(conn)
    offers = {**mim_offers(mim()), **tochki_offers(tochki_row("T1"), supplier=sid)}
    assert ("100", "s%d-T1" % sid) in tire_match.for_feed(conn, offers).groups
    conn.close()
    assert len(BADGE.findall(client.get("/catalog").get_data(as_text=True))) == 2


def test_cached_pairs_refuses_a_group_met_through_a_third_position_as_pairs_does(tmp_path):
    class View:
        def __init__(self, offers):
            self.offers = offers
    view = View({**mim_offers(mim()), **tochki_offers(tochki_row()), **mim_offers(mim(), supplier=3)})
    assert tire_match.cached_pairs(view, {}, 2).groups == [("100", "s2-TCH1", "s3-100")]
    apart = {("100", "s2-TCH1")}
    found = tire_match.cached_pairs(view, {}, 2, apart)
    assert found.groups == [] and found.refused == [("100", "s2-TCH1", "s3-100")]
    assert (found.groups, found.refused) == (lambda r: (r.groups, r.refused))(tire_match.pairs(view.offers, {}, 2, not_pairs=apart))
    assert tire_match.cached_pairs(view, {}, 2, apart) is found                   # kept while the marks are the same
    assert tire_match.cached_pairs(view, {}, 2).groups == [("100", "s2-TCH1", "s3-100")]

