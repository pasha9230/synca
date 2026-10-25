from datetime import date

from tires_sync import store
from tires_sync.models import PublishedItem


def test_published_roundtrip_and_delete_with_suppression(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    item = PublishedItem("100", 4810, 5000.0, "P1", "supplier", date(2026, 10, 6))
    store.upsert_published(conn, item, "<Ad/>")
    assert store.load_published(conn) == {"100": item}
    assert store.get_snapshot(conn, "100") == "<Ad/>"
    store.delete_published(conn, "100", suppress=True)
    assert store.load_published(conn) == {}
    assert store.load_suppressed(conn) == {"100"}


def test_known_point_and_mapping_normalization(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    assert store.load_known_points(conn) == {}
    store.upsert_mapping(conn, "Ikon Tyres", "Ikon Character Ice 7", "Character Ice 7")
    confirmed = store.load_confirmed_mappings(conn)
    assert confirmed[("Ikon Tyres", "IKONCHARACTERICE7")] == "Character Ice 7"


def test_save_run_returns_incrementing_id(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    a = store.save_run(conn, "2026-10-06T10:00:00", True, "abc", "{}")
    b = store.save_run(conn, "2026-10-06T16:00:00", True, "abd", "{}")
    assert b == a + 1


def test_audit_log_roundtrip_newest_first(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    store.write_audit(conn, "2026-10-07T10:00:00", "owner", "settings_confirm", {"draft": 1})
    store.write_audit(conn, "2026-10-07T11:00:00", "owner", "mapping_confirm", {"model": "X"})
    rows = store.list_audit(conn, limit=10)
    assert [r["action"] for r in rows] == ["mapping_confirm", "settings_confirm"]
    assert rows[1]["detail"] == {"draft": 1}


def test_brand_map_roundtrip_and_replace(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    store.upsert_brand_map(conn, "LAUFEN", "Laufenn")
    store.upsert_brand_map(conn, "LAUFEN", "Laufenn Tyres")
    assert store.load_brand_map(conn) == {"LAUFEN": "Laufenn Tyres"}


def test_new_positions_are_goods_first_seen_in_that_run_and_first_run_is_baseline(tmp_path):
    from helpers import make_offer
    conn = store.connect(tmp_path / "t.db")
    r1 = store.save_run(conn, "2026-10-08T10:00:00", True, "a", '{"publish": 1}')
    store.record_seen(conn, r1, [make_offer(goods_id="1")])
    r2 = store.save_run(conn, "2026-10-08T16:00:00", True, "b", '{"publish": 2}')
    store.record_seen(conn, r2, [make_offer(goods_id="1"), make_offer(goods_id="2", brand_key="KUMHO")])
    first = store.new_positions(conn, r1)
    second = store.new_positions(conn, r2)
    assert first["baseline"] is True and [g["goods_id"] for g in first["goods"]] == ["1"]
    assert second["baseline"] is False and [g["goods_id"] for g in second["goods"]] == ["2"]
    assert second["brands"] == ["KUMHO"]
    assert store.last_run(conn)["id"] == r2 and store.last_run(conn)["summary"] == {"publish": 2}


def test_last_run_is_none_before_any_run(tmp_path):
    assert store.last_run(store.connect(tmp_path / "t.db")) is None


def test_new_since_collects_across_runs_excludes_the_baseline_and_respects_the_marker(tmp_path):
    from helpers import make_offer
    conn = store.connect(tmp_path / "t.db")
    r1 = store.save_run(conn, "2026-10-01T10:00:00", True, "a", "{}")
    store.record_seen(conn, r1, [make_offer(goods_id="1")])
    r2 = store.save_run(conn, "2026-10-05T10:00:00", True, "b", "{}")
    store.record_seen(conn, r2, [make_offer(goods_id="1"), make_offer(goods_id="2", brand_key="KUMHO")])
    r3 = store.save_run(conn, "2026-10-09T10:00:00", True, "c", "{}")
    store.record_seen(conn, r3, [make_offer(goods_id="3")])
    allnew = store.new_since(conn, 0)
    assert [g["goods_id"] for g in allnew["goods"]] == ["3", "2"] and allnew["brands"] == ["KUMHO"]
    assert allnew["goods"][0]["seen_at"] == "2026-10-09T10:00:00"
    store.mark_new_seen(conn, r2)
    assert store.get_new_marker(conn) == r2
    assert [g["goods_id"] for g in store.new_since(conn, store.get_new_marker(conn))["goods"]] == ["3"]
    assert store.run_before(conn, "2026-10-06T00:00:00") == r2 and store.run_before(conn, "2026-09-01T00:00:00") == 0
