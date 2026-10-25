"""Reminders for changes agreed in advance: the day before and on the day, once each."""
from __future__ import annotations

from datetime import date

from helpers import settings_dict
from tires_sync import cli, connections, mirror_feed, reminders, store
from tires_sync.settings_store import init_settings

NOW = "2026-10-14T12:00:00"


def setup(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(), NOW)
    connections.create_account(conn, "Основной", NOW, "o")
    second = connections.create_account(conn, "Партнёр-2", NOW, "o")
    file = '<Ads><Ad><Id>100_1</Id><Title>T</Title><Price>1</Price></Ad><Ad><Id>101_1</Id><Title>T</Title><Price>1</Price></Ad></Ads>'.encode()
    mirror_feed.import_feed(conn, second, file, NOW)
    mirror_feed.adopt(conn, second, NOW, "o")
    mirror_feed.rebind_ids(conn, second, ["100_a2", "101_a2"], {"100": ["100"], "101": ["101"]}, NOW, "o",
                           shared={"100": "2026-11-14", "101": "2026-11-14"}, taken={"100", "101"})
    return conn, second


def test_nothing_is_due_long_before_the_date(tmp_path):
    conn, _ = setup(tmp_path)
    assert reminders.due(conn, date(2026, 10, 20)) == []


def test_the_day_before_and_on_the_day_each_gives_one_reminder(tmp_path):
    conn, second = setup(tmp_path)
    before = reminders.due(conn, date(2026, 11, 13))
    assert len(before) == 1 and "завтра (14.11.2026)" in before[0][1] and "2 позиций" in before[0][1]
    on = reminders.due(conn, date(2026, 11, 14))
    assert len(on) == 1 and "вышел" in on[0][1] and on[0][0] != before[0][0]
    assert len(reminders.due(conn, date(2026, 11, 20))) == 1                # still reminded until it is crossed off (the key is set when sent)


def test_a_sent_reminder_is_not_repeated_and_a_failed_one_is_tried_again(tmp_path, monkeypatch):
    conn, second = setup(tmp_path)
    monkeypatch.setattr(cli, "moscow_now", lambda: __import__("datetime").datetime(2026, 11, 13, 7, 0))
    answers = iter([False, True])
    seen = []
    monkeypatch.setattr(cli, "_alert", lambda c, b, text, **kw: (seen.append(text), next(answers))[1])
    assert cli._send_reminders(conn, None) == 0                              # Telegram did not take it: nothing is marked
    assert cli._send_reminders(conn, None) == 1                              # next cycle: sent and marked
    assert cli._send_reminders(conn, None) == 0 and len(seen) == 2          # and never again
