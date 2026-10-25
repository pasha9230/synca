"""The time of a load with a zone (run tails3, lane feed-tails): `suppliers.file_is_stale` compared it with the cycle's UTC time without a zone
and raised TypeError, the cycle fell (`_add_tochki`, docs/runs/2026-10-25-match4/report-tochki-catalog.md, tail 2). Now a time with a zone
is taken in UTC; the loads' own times (UTC, no zone) are judged as before; a time that cannot be read holds the supplier, as before."""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone

import pytest

from helpers import make_settings
from test_tochki_panel import row
from tires_sync import suppliers

NOW = datetime(2026, 10, 25, 12, 0, 0)                                    # the cycle's clock: UTC, no zone
MSK = timezone(timedelta(hours=3))


def test_naive_times_are_judged_as_before():
    assert not suppliers.file_is_stale(NOW - timedelta(hours=23), NOW, 0)
    assert suppliers.file_is_stale(NOW - timedelta(days=1, seconds=1), NOW, 0)
    assert not suppliers.file_is_stale(NOW - timedelta(days=3), NOW, 3) and suppliers.file_is_stale(NOW - timedelta(days=3, seconds=1), NOW, 3)


@pytest.mark.parametrize("fetched, stale", [
    (datetime(2026, 10, 25, 14, 0, 0, tzinfo=MSK), False),               # 11:00 UTC: an hour ago
    (datetime(2026, 10, 24, 15, 0, 1, tzinfo=MSK), False),               # 12:00:01 UTC of the day before: just within a day
    (datetime(2026, 10, 24, 14, 59, 59, tzinfo=MSK), True),              # a second over a day in UTC
    (datetime(2026, 10, 24, 11, 0, 0, tzinfo=timezone.utc), True),
    (datetime(2026, 10, 25, 11, 0, 0, tzinfo=timezone.utc), False),
])
def test_a_time_with_a_zone_is_compared_in_utc_and_raises_nothing(fetched, stale):
    assert suppliers.file_is_stale(fetched, NOW, 0) is stale
    assert suppliers.file_is_stale(fetched, NOW.replace(tzinfo=timezone.utc), 0) is stale       # a `now` with a zone too
    assert suppliers.file_is_stale(fetched.replace(tzinfo=None), NOW.replace(tzinfo=timezone.utc), 0) is (
        NOW - fetched.replace(tzinfo=None) > timedelta(days=1))                                 # a naive file time is UTC already


def add_tochki(tmp_path, fetched_at):
    """`_add_tochki` on a snapshot of one position loaded at `fetched_at`: (the part it adds, the notes)."""
    (tmp_path / "tochki-snapshot.json").write_text(json.dumps({"version": 1, "fetched_at": fetched_at, "warehouse_id": 7,
                                                               "items": [row("T1", rest=6)]}), encoding="utf-8")
    parts, notes = [], []
    suppliers._add_tochki(parts, notes, hashlib.sha256(), 3, "4 точки", tmp_path / "unload.xml", make_settings(stale_days=0), NOW)
    assert len(parts) == 1
    return parts[0], notes


@pytest.mark.parametrize("fetched_at", ["2026-10-25T14:00:00+03:00", "2026-10-25T11:00:00+00:00", "2026-10-25T11:00:00"])
def test_a_fresh_load_with_or_without_a_zone_is_taken(tmp_path, fetched_at):
    part, notes = add_tochki(tmp_path, fetched_at)
    assert notes == [] and set(part.offers) == {"s3-T1"} and not part.unavailable


@pytest.mark.parametrize("fetched_at", ["2026-10-24T14:00:00+03:00", "2026-10-07T12:00:00+03:00", "2026-10-24T11:00:00"])
def test_an_old_load_with_or_without_a_zone_holds_the_supplier(tmp_path, fetched_at):
    part, notes = add_tochki(tmp_path, fetched_at)
    assert not part.offers and part.unavailable == {3} and part.stocks.get("s3-T1") == 6
    assert len(notes) == 1 and "загрузка 4 точек устарела (2026-10-" in notes[0]


@pytest.mark.parametrize("fetched_at", ["мусор", "", 123, None, ["2026-10-25"], "2026-13-40T99:00:00"])
def test_a_time_that_cannot_be_read_holds_the_supplier_without_failing(tmp_path, fetched_at):
    part, notes = add_tochki(tmp_path, fetched_at)
    assert not part.offers and part.unavailable == {3}
    assert len(notes) == 1 and "дата неизвестна" in notes[0]


def test_a_utc_mark_z_is_held_as_unknown_on_python_3_9_and_taken_where_it_is_read(tmp_path):
    """«Z» is read by fromisoformat only from Python 3.11: before, the date is unknown and the supplier held, as before the change."""
    part, notes = add_tochki(tmp_path, "2026-10-25T11:00:00Z")
    if sys.version_info < (3, 11):
        assert not part.offers and part.unavailable == {3} and "дата неизвестна" in notes[0]
    else:
        assert notes == [] and set(part.offers) == {"s3-T1"}
