from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from helpers import catalog_xml, commodity_xml, settings_dict
from tires_sync import connections, cycle, publish, store
from tires_sync.catalog import parse_reference
from tires_sync.models import PublishedItem
from tires_sync.settings_store import init_settings

NOW = "2026-10-10T12:00:00"
TODAY = date(2026, 10, 10)
REF = parse_reference('<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>')


def setup(tmp_path, accounts=1):
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(), NOW)
    for i in range(accounts):
        connections.create_account(conn, "A%d" % i, NOW, "owner")
    return conn


def folder(tmp_path, conn, account_id):
    return tmp_path / "feed" / connections.get_account(conn, account_id)["feed_token"]


def run(conn, tmp_path, *goods):
    xml = catalog_xml(*[commodity_xml(NNOMMODIF=g) for g in goods])
    return cycle.run_for_accounts(conn, xml, REF, TODAY, tmp_path / "feed")


def test_state_is_separate_per_account(tmp_path):
    conn = setup(tmp_path, 2)
    store.upsert_published(conn, PublishedItem("100", 4810, 5000.0, "P1"), "<Ad/>", account_id=1)
    assert set(store.load_published(conn, account_id=1)) == {"100"}
    assert store.load_published(conn, account_id=2) == {}
    store.delete_published(conn, "100", suppress=True, account_id=1)
    assert store.load_suppressed(conn, account_id=1) == {"100"} and store.load_suppressed(conn, account_id=2) == set()


def test_old_database_rows_move_to_the_first_account_on_migration(tmp_path):
    path = tmp_path / "old.db"
    raw = sqlite3.connect(str(path))
    raw.executescript("""
        CREATE TABLE published_state (goods_id TEXT PRIMARY KEY, price INTEGER NOT NULL, base_price REAL NOT NULL,
            point TEXT NOT NULL, location TEXT NOT NULL DEFAULT 'supplier', first_hold TEXT, snapshot TEXT NOT NULL);
        CREATE TABLE suppressed (goods_id TEXT PRIMARY KEY);
        INSERT INTO published_state VALUES ('100', 4810, 5000.0, 'P1', 'supplier', NULL, '<Ad/>');
        INSERT INTO suppressed VALUES ('7');
    """)
    raw.commit()
    raw.close()
    conn = store.connect(path)
    assert list(store.load_published(conn, account_id=1)) == ["100"] and store.get_snapshot(conn, "100", account_id=1) == "<Ad/>"
    assert store.load_suppressed(conn, account_id=1) == {"7"}
    conn.close()
    assert list(store.load_published(store.connect(path), account_id=1)) == ["100"]


def test_cycle_writes_pending_state_but_does_not_touch_published(tmp_path):
    conn = setup(tmp_path)
    run(conn, tmp_path, "100", "101")
    assert store.load_published(conn, account_id=1) == {}
    assert store.has_pending(conn, 1)


def test_committing_after_promotion_records_what_avito_will_see(tmp_path):
    conn = setup(tmp_path)
    run(conn, tmp_path, "100", "101")
    f = folder(tmp_path, conn, 1)
    publish.promote_feed(f)
    store.commit_pending(conn, 1, (f / "avito_feed.xml").read_bytes())
    pub = store.load_published(conn, account_id=1)
    assert set(pub) == {"100", "101"} and pub["100"].price == 4810 and pub["100"].base_price == 5000.0
    assert "<Id>100</Id>" in store.get_snapshot(conn, "100", account_id=1)
    assert not store.has_pending(conn, 1)


def test_next_cycle_removes_what_disappeared_and_suppresses_low_stock(tmp_path):
    conn = setup(tmp_path)
    run(conn, tmp_path, "100", "101")
    f = folder(tmp_path, conn, 1)
    publish.promote_feed(f)
    store.commit_pending(conn, 1, (f / "avito_feed.xml").read_bytes())
    xml = catalog_xml(commodity_xml(NNOMMODIF="100"), commodity_xml(NNOMMODIF="101", NREST=1))
    cycle.run_for_accounts(conn, xml, REF, TODAY, tmp_path / "feed")
    publish.promote_feed(f, allow_shrink=True)
    store.commit_pending(conn, 1, (f / "avito_feed.xml").read_bytes())
    assert set(store.load_published(conn, account_id=1)) == {"100"}
    assert store.load_suppressed(conn, account_id=1) == {"101"}


def test_a_changed_candidate_never_commits_stale_state(tmp_path):
    conn = setup(tmp_path)
    run(conn, tmp_path, "100")
    f = folder(tmp_path, conn, 1)
    publish.promote_feed(f)
    with pytest.raises(store.StateError):
        store.commit_pending(conn, 1, b"<Ads><Ad><Id>999</Id></Ad></Ads>")
    assert store.load_published(conn, account_id=1) == {} and store.has_pending(conn, 1)


def test_committing_twice_is_harmless(tmp_path):
    conn = setup(tmp_path)
    run(conn, tmp_path, "100")
    f = folder(tmp_path, conn, 1)
    publish.promote_feed(f)
    data = (f / "avito_feed.xml").read_bytes()
    store.commit_pending(conn, 1, data)
    with pytest.raises(store.StateError):
        store.commit_pending(conn, 1, data)
    assert set(store.load_published(conn, account_id=1)) == {"100"}


def test_each_account_keeps_its_own_published_state(tmp_path):
    conn = setup(tmp_path, 2)
    run(conn, tmp_path, "100")
    f1 = folder(tmp_path, conn, 1)
    publish.promote_feed(f1)
    store.commit_pending(conn, 1, (f1 / "avito_feed.xml").read_bytes())
    assert set(store.load_published(conn, account_id=1)) == {"100"} and store.load_published(conn, account_id=2) == {}
