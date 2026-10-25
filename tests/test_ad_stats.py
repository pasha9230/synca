"""Daily Avito statistics: the list of our ads, the mapping to our ad ids, views / contacts / favourites by day."""
from __future__ import annotations

from datetime import date

import pytest

from http_fakes import ScriptedHttp
from tires_sync import ad_stats, store
from tires_sync.avito import AvitoClient, AvitoError

TOKEN = (200, {"access_token": "t", "expires_in": 3600})
SELF = (200, {"id": 111, "name": "Шины СПб"})


def client_for(*replies):
    http = ScriptedHttp(TOKEN, *replies)
    return AvitoClient("id", "secret", http=http, now=lambda: 0.0, sleep=lambda s: None), http


def items(*ids):
    return (200, {"meta": {"page": 1, "per_page": 100}, "resources": [{"id": i, "title": "Шина %d" % i, "price": 5000 + i, "status": "active"} for i in ids]})


def db(tmp_path):
    return store.connect(str(tmp_path / "x.db"))


def test_collect_stores_ads_mapping_and_daily_statistics(tmp_path):
    conn = db(tmp_path)
    client, http = client_for(SELF, items(1, 2), (200, {"items": [{"avito_id": 1, "ad_id": "A1"}, {"avito_id": 2, "ad_id": None}]}),
                              (200, {"result": {"items": [
                                  {"itemId": 1, "stats": [{"date": "2026-10-11", "uniqViews": 5, "uniqContacts": 1, "uniqFavorites": 2},
                                                          {"date": "2026-10-12", "uniqViews": 3, "uniqContacts": 0, "uniqFavorites": 0}]},
                                  {"itemId": 2, "stats": []}]}}))
    result = ad_stats.collect(conn, client, 1, date(2026, 10, 12), days=7, now_iso="2026-10-12T04:20:00")
    assert result == {"ads": 2, "mapped": 1, "days": 2}
    assert conn.execute("SELECT avito_id, ad_id, title, price FROM avito_ad ORDER BY avito_id").fetchall() == [(1, "A1", "Шина 1", 5001), (2, None, "Шина 2", 5002)]
    assert conn.execute("SELECT day, views, contacts, favorites FROM ad_stat WHERE avito_id = 1 ORDER BY day").fetchall() == [
        ("2026-10-11", 5, 1, 2), ("2026-10-12", 3, 0, 0)]
    assert ad_stats.totals(conn, 1, "2026-10-01") == {"views": 8, "contacts": 1, "favorites": 2}
    body = http.calls[-1][3].decode()
    assert '"dateFrom": "2026-10-06"' in body and '"dateTo": "2026-10-12"' in body and "uniqViews" in body


def test_a_rerun_updates_the_overlapping_days_and_does_not_duplicate_or_forget_the_mapping(tmp_path):
    conn = db(tmp_path)
    first, _ = client_for(SELF, items(1), (200, {"items": [{"avito_id": 1, "ad_id": "A1"}]}),
                          (200, {"result": {"items": [{"itemId": 1, "stats": [{"date": "2026-10-12", "uniqViews": 3, "uniqContacts": 0, "uniqFavorites": 0}]}]}}))
    ad_stats.collect(conn, first, 1, date(2026, 10, 12))
    second, http = client_for(items(1), (200, {"result": {"items": [{"itemId": 1, "stats": [{"date": "2026-10-12", "uniqViews": 9, "uniqContacts": 2, "uniqFavorites": 1}]}]}}))
    result = ad_stats.collect(conn, second, 1, date(2026, 10, 13))
    assert result["mapped"] == 0                                          # already mapped: no second mapping request
    assert conn.execute("SELECT views, contacts FROM ad_stat").fetchall() == [(9, 2)]
    assert conn.execute("SELECT ad_id FROM avito_ad").fetchone()[0] == "A1"
    assert not any("/ad_ids" in c[1] for c in http.calls)


def test_many_ads_are_requested_in_chunks_and_pages_are_followed(tmp_path):
    conn = db(tmp_path)
    page1 = (200, {"resources": [{"id": i, "title": "t", "price": 1, "status": "active"} for i in range(1, 101)]})
    page2 = (200, {"resources": [{"id": i, "title": "t", "price": 1, "status": "active"} for i in range(101, 131)]})
    client, http = client_for(SELF, page1, page2, (200, {"items": []}), (200, {"items": []}), (200, {"items": []}), (200, {"result": {"items": []}}))
    result = ad_stats.collect(conn, client, 1, date(2026, 10, 12))
    assert result["ads"] == 130
    assert sum("/ad_ids" in c[1] for c in http.calls) == 3                 # 130 ads, 50 per mapping request
    assert sum("/stats/v1/" in c[1] for c in http.calls) == 1


def test_old_days_are_dropped_and_errors_are_not_swallowed(tmp_path):
    conn = db(tmp_path)
    conn.execute("INSERT INTO ad_stat VALUES (1, 9, '2025-01-01', 1, 1, 1)")
    conn.execute("INSERT INTO ad_stat VALUES (1, 9, '2026-10-01', 2, 2, 2)")
    client, _ = client_for(SELF, items(), )
    ad_stats.collect(conn, client, 1, date(2026, 10, 12))
    assert [r[0] for r in conn.execute("SELECT day FROM ad_stat")] == ["2026-10-01"]
    failing, _ = client_for((403, {"error": {"code": 403, "message": "no scope"}}))
    with pytest.raises(AvitoError):
        ad_stats.collect(conn, failing, 1, date(2026, 10, 12))
    with pytest.raises(ValueError):
        ad_stats.collect(conn, client, 1, date(2026, 10, 12), days=0)


def test_the_statistics_client_methods_never_need_write_access():
    client, _ = client_for()
    assert client.allow_write is False
