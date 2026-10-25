"""The calls through the Avito substitute numbers: reading (only reads, pages, pauses, once per call), the client of the number, the
recordings kept 7 days and then deleted, an account without access to the recordings, one account never stops the others."""
from __future__ import annotations

import json
import os
import stat

import pytest

from http_fakes import ScriptedHttp
from tires_sync import calls, clients, connections, order_points as points, orders, store
from tires_sync.avito import AvitoClient, AvitoError, RateLimited

NOW = "2026-10-23T15:00:00"
TOKEN = (200, {"access_token": "t", "expires_in": 3600})


def call(call_id, at="2026-10-23T13:54:30+03:00", talk=57, buyer="+79111234567", wait=4):
    """A call as Avito gave it on the server 2026-10-23 (the fields exactly, no itemId)."""
    return {"callId": call_id, "callTime": at, "talkDuration": talk, "waitingDuration": wait, "buyerPhone": buyer,
            "sellerPhone": "+79215550011", "virtualPhone": "+79990001122"}


class FakeClient:
    def __init__(self, pages=(), records=None):
        self.pages, self.records, self.asked = list(pages), dict(records or {}), []

    def calls(self, date_from, date_to, limit=100, offset=0, retry=True):
        self.asked.append(("calls", date_from, date_to, offset))
        page = offset // limit
        value = self.pages[page] if page < len(self.pages) else []
        if isinstance(value, Exception):
            raise value
        return value

    def call_record(self, call_id, retry=True):
        self.asked.append(("record", call_id))
        value = self.records.get(call_id, AvitoError(425, None, "Too early"))
        if isinstance(value, Exception):
            raise value
        return value


@pytest.fixture
def db(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    account = connections.create_account(conn, "Основной", NOW, "o")
    chats_on(conn, account)
    return conn, account


def chats_on(conn, *accounts):
    """The calls are read for the accounts whose chats are read (the lead 2026-10-23: №4 «не используется» is not)."""
    for account in accounts:
        conn.execute("UPDATE account SET chats_enabled = 1 WHERE id = ?", (account,))
    conn.commit()


def sleeps():
    made = []
    return made, made.append


# ---- the client of Avito -------------------------------------------------------------------------------------------------------------

def test_the_client_asks_the_calls_with_a_post_read_and_the_recording_as_bytes():
    http = ScriptedHttp(TOKEN, (200, {"calls": [call(1)], "error": {"code": 0, "message": ""}}),
                        (200, b"ID3audio", {"Content-Type": "audio/mpeg;audio/mpeg3;audio/x-mpeg-3"}))
    client = AvitoClient("cid", "secret", http=http, now=lambda: 0.0)
    assert client.allow_write is False
    assert client.calls("2026-10-16T15:00:00+03:00", "2026-10-23T15:00:00+03:00", limit=100, offset=0)[0]["callId"] == 1
    assert client.call_record(1) == b"ID3audio"
    (m1, u1, _h, body), (m2, u2, h2, _b) = http.calls[1], http.calls[2]
    assert (m1, u1.split("avito.ru")[1]) == ("POST", "/calltracking/v1/getCalls/")
    assert json.loads(body) == {"dateTimeFrom": "2026-10-16T15:00:00+03:00", "dateTimeTo": "2026-10-23T15:00:00+03:00", "limit": 100, "offset": 0}
    assert (m2, u2.split("avito.ru")[1]) == ("GET", "/calltracking/v1/getRecordByCallId/?callId=1")


def test_an_error_inside_a_200_answer_and_the_closed_records_are_errors():
    http = ScriptedHttp(TOKEN, (200, {"calls": [], "error": {"code": 1002, "message": "auth"}}), (403, {"code": 1002, "message": "forbidden"}),
                        (425, {"code": 1005, "message": "not ready"}))
    client = AvitoClient("cid", "secret", http=http, now=lambda: 0.0)
    with pytest.raises(AvitoError) as err:
        client.calls("a", "b")
    assert err.value.code == 1002
    with pytest.raises(AvitoError) as err:
        client.call_record(5)
    assert (err.value.status, err.value.code) == (403, 1002)
    with pytest.raises(AvitoError) as err:
        client.call_record(5)
    assert (err.value.status, err.value.code) == (425, 1005)


def test_429_waits_and_asks_again():
    http = ScriptedHttp(TOKEN, (429, {}, {"Retry-After": "2"}), (200, {"calls": [call(1)], "error": {"code": 0}}))
    waited = []
    client = AvitoClient("cid", "secret", http=http, now=lambda: 0.0, sleep=waited.append)
    assert len(client.calls("a", "b")) == 1 and waited == [2.0]


def test_avitos_time_is_kept_as_moscow():
    assert calls.moscow("2026-10-23T13:54:30+03:00") == "2026-10-23T13:54:30"
    assert calls.moscow("2026-10-23T10:54:30Z") == "2026-10-23T13:54:30"
    assert calls.moscow("2026-10-23T10:54:30+00:00") == "2026-10-23T13:54:30"
    assert calls.moscow("") == "" and calls.moscow("вчера") == ""


# ---- reading the calls ---------------------------------------------------------------------------------------------------------------

def test_the_new_calls_are_kept_once_with_the_point_of_the_account_and_the_client(db):
    conn, account = db
    pid = points.save_point(conn, {"name": "Гамма", "manager_name": "А", "channel": "vk", "avito_account_id": account})
    fake = FakeClient([[call(1), call(2, at="2026-10-23T13:00:00+03:00", talk=0, buyer="8 (911) 765-43-21")]])
    made, sleep = sleeps()
    got = calls.sync_account(conn, fake, account, NOW, sleep)
    assert got == {"seen": 2, "new": 2, "whole": True}
    rows = calls.list_calls(conn)
    assert [(r["call_id"], r["answered"], r["record_state"], r["point_id"], r["buyer_phone"]) for r in rows] == [
        (1, 1, "wait", pid, "+79111234567"), (2, 0, "", pid, "+79117654321")]
    assert rows[0]["call_at"] == "2026-10-23T13:54:30" and rows[0]["record_until"] == "2026-10-30T13:54:30"
    assert rows[0]["talk_seconds"] == 57 and rows[0]["wait_seconds"] == 4 and rows[0]["seller_phone"] == "+79215550011"
    assert all(r["client_id"] for r in rows) and rows[0]["client_id"] != rows[1]["client_id"]
    assert clients.get_client(conn, rows[0]["client_id"])["phones"] == ["+79111234567"]
    again = calls.sync_account(conn, FakeClient([[call(1), call(2, at="2026-10-23T13:00:00+03:00", talk=0, buyer="+79117654321")]]), account, NOW, sleep)
    assert again["new"] == 0 and conn.execute("SELECT COUNT(*) FROM avito_call").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM client").fetchone()[0] == 2                       # a second reading makes no second client


def test_the_first_reading_is_a_week_and_the_next_one_from_the_last_call_an_hour_back(db):
    conn, account = db
    fake = FakeClient([[call(1)]])
    calls.sync_account(conn, fake, account, NOW, lambda s: None)
    assert fake.asked[0][1:3] == ("2026-10-16T15:00:00+03:00", "2026-10-23T15:00:00+03:00")
    assert store.get_state(conn, "calls_since_%d" % account) == NOW                              # the time of the reading (review 2026-10-23)
    fake = FakeClient([[]])
    calls.sync_account(conn, fake, account, "2026-10-23T15:02:00", lambda s: None)
    assert fake.asked[0][1:3] == ("2026-10-23T14:00:00+03:00", "2026-10-23T15:02:00+03:00")


def test_pages_with_a_pause_and_the_mark_stays_when_not_every_page_was_read(db, monkeypatch):
    conn, account = db
    monkeypatch.setattr(calls, "PAGE", 2)
    made, sleep = sleeps()
    fake = FakeClient([[call(1), call(2)], [call(3, at="2026-10-23T14:10:00+03:00")]])
    assert calls.sync_account(conn, fake, account, NOW, sleep)["new"] == 3
    assert [a[3] for a in fake.asked] == [0, 2] and made == [calls.PAUSE]
    monkeypatch.setattr(calls, "MAX_PAGES", 1)
    fake = FakeClient([[call(4, at="2026-10-23T14:50:00+03:00"), call(5, at="2026-10-23T14:51:00+03:00")], [call(6)]])
    got = calls.sync_account(conn, fake, account, "2026-10-23T15:30:00", sleep)
    assert got["whole"] is False and store.get_state(conn, "calls_since_%d" % account) == NOW


def test_an_account_without_calls_lives_with_an_empty_answer(db):
    conn, account = db
    assert calls.sync_account(conn, FakeClient([[]]), account, NOW, lambda s: None) == {"seen": 0, "new": 0, "whole": True}
    assert store.get_state(conn, "calls_since_%d" % account) == NOW


def test_our_own_number_and_a_hidden_one_join_no_client(db):
    conn, account = db
    second = connections.create_account(conn, "Партнёр-2", NOW, "o")                          # its cabinet phone is ours (conftest)
    own = connections.get_avito_identity(conn, second)["phone"]
    calls.sync_account(conn, FakeClient([[call(1, buyer=own), call(2, buyer="")]]), account, NOW, lambda s: None)
    assert conn.execute("SELECT COUNT(*) FROM avito_call").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM client_call").fetchone()[0] == 0 and conn.execute("SELECT COUNT(*) FROM client").fetchone()[0] == 0


def test_a_known_number_joins_its_client(db):
    conn, account = db
    orders.ensure_pipeline(conn, NOW)
    card = orders.create_card(conn, {"client_name": "Иван", "client_phone": "+7 911 123-45-67"}, NOW, "o")
    client_id = clients.client_of_order(conn, card)["id"]
    calls.sync_account(conn, FakeClient([[call(1)]]), account, NOW, lambda s: None)
    assert calls.list_calls(conn)[0]["client_id"] == client_id and calls.list_calls(conn)[0]["client_name"] == "Иван"
    assert [k["call_id"] for k in clients.get_client(conn, client_id)["calls"]] == [1]


def test_a_merge_takes_the_calls_along(db):
    conn, account = db
    calls.sync_account(conn, FakeClient([[call(1), call(2, buyer="+79117654321")]]), account, NOW, lambda s: None)
    a, b = [r["client_id"] for r in calls.list_calls(conn)]
    clients.merge(conn, a, b, NOW, "o")
    assert sorted(k["call_id"] for k in clients.get_client(conn, a)["calls"]) == [1, 2]


# ---- the recordings ------------------------------------------------------------------------------------------------------------------

def test_a_ready_recording_is_saved_0640_and_a_missed_call_has_none(db, tmp_path):
    conn, account = db
    folder = tmp_path / "calls"
    calls.sync_account(conn, FakeClient([[call(1), call(2, talk=0)]]), account, NOW, lambda s: None)
    fake = FakeClient(records={1: b"ID3-1"})
    assert calls.fetch_records(conn, fake, account, folder, NOW, lambda s: None)["saved"] == 1
    path = folder / str(account) / "1.mp3"
    assert path.read_bytes() == b"ID3-1" and stat.S_IMODE(os.stat(path).st_mode) == 0o640
    assert fake.asked == [("record", 1)]                                                        # the missed call is not asked
    first = [r for r in calls.list_calls(conn) if r["call_id"] == 1][0]
    assert (first["record_state"], first["record_file"], first["record_size"]) == ("saved", "%d/1.mp3" % account, 5)


def test_a_recording_not_ready_is_asked_again_after_ten_minutes_and_given_up_after_six_hours(db, tmp_path):
    conn, account = db
    calls.sync_account(conn, FakeClient([[call(1, at="2026-10-23T14:58:00+03:00")]]), account, NOW, lambda s: None)
    fake = FakeClient(records={1: AvitoError(425, 1005, "Too early")})
    assert calls.fetch_records(conn, fake, account, tmp_path, NOW, lambda s: None)["waiting"] == 1
    assert calls.fetch_records(conn, fake, account, tmp_path, "2026-10-23T15:05:00", lambda s: None)["waiting"] == 0       # too soon
    assert calls.fetch_records(conn, fake, account, tmp_path, "2026-10-23T15:10:00", lambda s: None)["waiting"] == 1
    conn.execute("UPDATE avito_call SET record_tries = ?", (calls.GIVE_UP_TRIES - 1,))
    assert calls.fetch_records(conn, fake, account, tmp_path, "2026-10-23T15:20:00", lambda s: None)["none"] == 1
    assert calls.list_calls(conn)[0]["record_state"] == "none"


def test_a_404_of_an_old_call_is_no_recording(db, tmp_path):
    conn, account = db
    calls.sync_account(conn, FakeClient([[call(1, at="2026-10-23T12:00:00+03:00")]]), account, NOW, lambda s: None)
    calls.fetch_records(conn, FakeClient(records={1: AvitoError(404, 1004, "not found")}), account, tmp_path, NOW, lambda s: None)
    assert calls.list_calls(conn)[0]["record_state"] == "none"


def test_an_account_without_access_to_the_records_is_asked_once_a_day_and_the_calls_stay(db, tmp_path):
    conn, account = db
    calls.sync_account(conn, FakeClient([[call(1), call(2, at="2026-10-23T14:00:00+03:00"), call(3, at="2026-10-23T14:10:00+03:00")]]),
                       account, NOW, lambda s: None)
    fake = FakeClient(records={1: AvitoError(403, 1002, "forbidden"), 2: AvitoError(403, 1002, "forbidden"), 3: AvitoError(403, 1002, "forbidden")})
    assert calls.fetch_records(conn, fake, account, tmp_path, NOW, lambda s: None)["denied"] == 1
    assert len(fake.asked) == 1                                                                   # one request, not one per call
    assert {r["record_state"] for r in calls.list_calls(conn)} == {"denied"} and len(calls.list_calls(conn)) == 3
    assert calls.list_calls(conn)[0]["record_text"] == "нет доступа к записям"
    calls.fetch_records(conn, fake, account, tmp_path, "2026-10-24T14:00:00", lambda s: None)
    assert len(fake.asked) == 1                                                                   # not a day yet
    calls.fetch_records(conn, fake, account, tmp_path, "2026-10-24T15:00:00", lambda s: None)
    assert len(fake.asked) == 2


def test_429_on_a_recording_leaves_the_rest_for_the_next_run(db, tmp_path):
    conn, account = db
    calls.sync_account(conn, FakeClient([[call(1), call(2, at="2026-10-23T14:00:00+03:00")]]), account, NOW, lambda s: None)
    fake = FakeClient(records={2: RateLimited(429, None, "slow"), 1: b"x"})
    made, sleep = sleeps()
    assert calls.fetch_records(conn, fake, account, tmp_path, NOW, sleep) == {"saved": 0, "waiting": 0, "none": 0, "denied": 0}
    assert len(fake.asked) == 1 and {r["record_state"] for r in calls.list_calls(conn)} == {"wait"}


def test_a_week_after_the_call_the_file_is_deleted_and_the_call_stays_marked(db, tmp_path):
    conn, account = db
    calls.sync_account(conn, FakeClient([[call(1)]]), account, NOW, lambda s: None)
    calls.fetch_records(conn, FakeClient(records={1: b"ID3"}), account, tmp_path, NOW, lambda s: None)
    path = tmp_path / str(account) / "1.mp3"
    assert calls.clean_records(conn, tmp_path, "2026-10-30T13:54:29") == 0 and path.exists()
    assert calls.clean_records(conn, tmp_path, "2026-10-30T13:54:30") == 1 and not path.exists()
    row = calls.list_calls(conn)[0]
    assert (row["record_state"], row["record_file"], row["record_deleted_at"]) == ("deleted", "", "2026-10-30T13:54:30")
    assert row["record_text"].startswith("удалена")
    assert calls.clean_records(conn, tmp_path, "2026-10-31T00:00:00") == 0                         # once
    assert calls.fetch_records(conn, FakeClient(records={1: b"x"}), account, tmp_path, "2026-10-31T00:00:00", lambda s: None)["saved"] == 0


def test_a_file_already_gone_is_still_marked_deleted(db, tmp_path):
    conn, account = db
    calls.sync_account(conn, FakeClient([[call(1)]]), account, NOW, lambda s: None)
    calls.fetch_records(conn, FakeClient(records={1: b"ID3"}), account, tmp_path, NOW, lambda s: None)
    (tmp_path / str(account) / "1.mp3").unlink()
    assert calls.clean_records(conn, tmp_path, "2026-11-01T00:00:00") == 1
    assert calls.list_calls(conn)[0]["record_state"] == "deleted"


def test_the_file_of_a_recording_is_only_ours():
    assert calls.record_path("/data/calls", "3/123.mp3") is not None
    for bad in ("../x.mp3", "3/../../etc.mp3", "3/1.wav", "a/1.mp3", "/etc/passwd", "", "3/1.mp3/x"):
        assert calls.record_path("/data/calls", bad) is None, bad


# ---- one run ---------------------------------------------------------------------------------------------------------------------------

def test_one_failing_account_never_stops_the_others_and_an_hour_of_failures_is_the_alarm(db, tmp_path):
    conn, account = db
    second = connections.create_account(conn, "Партнёр-2", NOW, "o")
    chats_on(conn, second, connections.create_account(conn, "Без ключей", NOW, "o"))
    fakes = {account: FakeClient([OSError("timeout")]), second: FakeClient([[call(9)]], records={9: b"ID3"})}
    said, warned, made = [], [], []
    rc = calls.run(conn, lambda a: fakes.get(a["id"]), NOW, tmp_path, made.append, say=said.append, warn=warned.append)
    assert rc == 0 and [r["call_id"] for r in calls.list_calls(conn)] == [9]
    assert any("Основной" in w for w in warned) and any("Партнёр-2" in s and "новых 1" in s and "записей скачано 1" in s for s in said)
    assert calls.PAUSE in made                                                                    # a pause between the accounts
    store.set_state(conn, "calls_fail_%d" % account, str(calls.FAIL_ALARM_RUNS - 1))
    assert calls.run(conn, lambda a: fakes.get(a["id"]), NOW, tmp_path, lambda s: None, say=said.append, warn=warned.append) == 1
    fakes[account] = FakeClient([[]])
    assert calls.run(conn, lambda a: fakes.get(a["id"]), NOW, tmp_path, lambda s: None, say=said.append, warn=warned.append) == 0
    assert store.get_state(conn, "calls_fail_%d" % account) == "0"


def test_the_dry_run_writes_nothing(db, tmp_path):
    conn, account = db
    said = []
    calls.run(conn, lambda a: FakeClient([[call(1), call(2)]]), NOW, tmp_path, lambda s: None, dry_run=True, say=said.append, warn=said.append)
    assert conn.execute("SELECT COUNT(*) FROM avito_call").fetchone()[0] == 0 and store.get_state(conn, "calls_since_%d" % account) in (None, "")
    assert "звонков прочитано 2, новых 0" in said[0] and not list(tmp_path.glob("**/*.mp3"))


def test_create_order_from_a_call_has_the_number_the_tag_and_the_point(db):
    conn, account = db
    pid = points.save_point(conn, {"name": "Гамма", "manager_name": "А", "channel": "vk", "avito_account_id": account})
    calls.sync_account(conn, FakeClient([[call(1)]]), account, NOW, lambda s: None)
    row = calls.list_calls(conn)[0]
    card_id = calls.create_order(conn, row["id"], NOW, "pavel")
    card = orders.get_card(conn, card_id)
    assert card["client_phone"] == "+79111234567" and card["point_id"] == pid and "Основной" in card["tags"]
    assert "Звонок 2026-10-23 13:54" in card["comment"]
    assert clients.client_of_order(conn, card_id)["id"] == row["client_id"]
    assert calls.create_order(conn, row["id"], NOW, "pavel") == card_id                           # a second press gives the same order
    assert conn.execute("SELECT COUNT(*) FROM order_card").fetchone()[0] == 1


def test_the_command_reads_the_calls_and_the_recording_with_reads_only_and_prints_no_number(tmp_path, monkeypatch, capsys):
    from datetime import datetime
    from tires_sync import cli
    from tires_sync.secretbox import create_key_file, load_box
    conn = store.connect(tmp_path / "t.db")
    account = connections.create_account(conn, "Основной", NOW, "o")
    key_file = tmp_path / "secret.key"
    create_key_file(key_file)
    box = load_box(key_file)
    connections.set_secret(conn, box, "avito", account, "client_id", "CLIENTID-12345678", NOW, "o")
    connections.set_secret(conn, box, "avito", account, "client_secret", "SECRETVALUE-XYZ", NOW, "o")
    chats_on(conn, account)
    conn.close()
    http = ScriptedHttp(TOKEN, (200, {"calls": [call(1)], "error": {"code": 0, "message": ""}}), (200, b"ID3", {"Content-Type": "audio/mpeg"}))
    monkeypatch.setattr(cli, "_http", lambda: http)
    monkeypatch.setattr(cli, "moscow_now", lambda: datetime(2026, 10, 23, 15, 0, 0))
    assert cli.main(["calls-sync", "--db", str(tmp_path / "t.db"), "--key-file", str(key_file)]) == 0
    assert [(m, u.split("avito.ru")[1].split("?")[0]) for m, u, _h, _b in http.calls[1:]] == [
        ("POST", "/calltracking/v1/getCalls/"), ("GET", "/calltracking/v1/getRecordByCallId/")]
    assert (tmp_path / "calls" / str(account) / "1.mp3").read_bytes() == b"ID3"
    out = capsys.readouterr().out
    assert "новых 1" in out and "записей скачано 1" in out and "1234567" not in out and "SECRET" not in out


# ---- the review of 2026-10-23 (guards) -----------------------------------------------------------------------------------------------

def test_a_json_error_inside_a_200_is_never_saved_as_a_recording(db, tmp_path):
    http = ScriptedHttp(TOKEN, (200, {"code": 1005, "message": "not ready"}, {"Content-Type": "application/json"}),
                        (200, b'{"error": {"code": 1005}}', {"Content-Type": "text/plain"}))
    client = AvitoClient("cid", "secret", http=http, now=lambda: 0.0)
    for _ in range(2):
        with pytest.raises(AvitoError) as err:
            client.call_record(5)
        assert err.value.code == 1005


def test_a_deleted_order_frees_the_call_and_a_restored_one_comes_back(db):
    from tires_sync import trash
    conn, account = db
    calls.sync_account(conn, FakeClient([[call(1)]]), account, NOW, lambda s: None)
    row = calls.list_calls(conn)[0]["id"]
    first = calls.create_order(conn, row, NOW, "pavel")
    record = trash.delete_order(conn, first, NOW, "pavel")
    assert calls.get_call(conn, row)["order_id"] is None                                        # no link to a page that is gone
    trash.restore_order(conn, record, NOW, "pavel")
    assert calls.get_call(conn, row)["order_id"] == first
    trash.delete_order(conn, first, NOW, "pavel")
    second = calls.create_order(conn, row, NOW, "pavel")
    assert second != first and orders.get_card(conn, second)["client_phone"] == "+79111234567"
    assert calls.create_order(conn, row, NOW, "pavel") == second


def test_an_account_without_calltracking_is_no_failure_and_a_long_failure_alarms_once(db, tmp_path):
    conn, account = db
    said, warned = [], []
    shut = FakeClient([AvitoError(403, 1002, "forbidden")])
    assert calls.run(conn, lambda a: shut, NOW, tmp_path, lambda s: None, say=said.append, warn=warned.append) == 0
    assert store.get_state(conn, "calls_fail_%d" % account) in (None, "", "0") and any("не подключ" in s for s in said)
    store.set_state(conn, "calls_fail_%d" % account, str(calls.FAIL_ALARM_RUNS - 1))
    assert calls.run(conn, lambda a: FakeClient([OSError("x")]), NOW, tmp_path, lambda s: None, say=said.append, warn=warned.append) == 1
    assert calls.run(conn, lambda a: FakeClient([OSError("x")]), NOW, tmp_path, lambda s: None, say=said.append, warn=warned.append) == 0
    store.set_state(conn, "calls_fail_%d" % account, str(calls.FAIL_ALARM_RUNS + calls.FAIL_ALARM_AGAIN - 1))
    assert calls.run(conn, lambda a: FakeClient([OSError("x")]), NOW, tmp_path, lambda s: None, say=said.append, warn=warned.append) == 1   # again ~6 h later


def test_after_a_whole_reading_the_mark_is_the_time_of_the_reading(db):
    conn, account = db
    calls.sync_account(conn, FakeClient([[call(1, at="2026-10-01T10:00:00+03:00")]]), account, NOW, lambda s: None)
    assert store.get_state(conn, "calls_since_%d" % account) == NOW
    fake = FakeClient([[]])
    calls.sync_account(conn, fake, account, "2026-10-24T15:00:00", lambda s: None)
    assert fake.asked[0][1] == "2026-10-23T14:00:00+03:00"                                        # the window never grows with silence


def test_a_call_seen_without_talk_and_later_answered_is_updated(db):
    conn, account = db
    calls.sync_account(conn, FakeClient([[call(1, talk=0)]]), account, NOW, lambda s: None)
    calls.sync_account(conn, FakeClient([[call(1, talk=40)]]), account, "2026-10-23T15:02:00", lambda s: None)
    row = calls.list_calls(conn)[0]
    assert (row["answered"], row["talk_seconds"], row["record_state"]) == (1, 40, "wait")
    calls.sync_account(conn, FakeClient([[call(1, talk=0)]]), account, "2026-10-23T15:04:00", lambda s: None)
    assert calls.list_calls(conn)[0]["answered"] == 1                                             # an answer is never taken back


def test_new_calls_of_an_account_without_access_wait_a_day_without_a_request(db, tmp_path):
    conn, account = db
    calls.sync_account(conn, FakeClient([[call(1)]]), account, NOW, lambda s: None)
    calls.fetch_records(conn, FakeClient(records={1: AvitoError(403, 1002, "forbidden")}), account, tmp_path, NOW, lambda s: None)
    calls.sync_account(conn, FakeClient([[call(2, at="2026-10-23T15:01:00+03:00")]]), account, "2026-10-23T15:02:00", lambda s: None)
    fake = FakeClient(records={2: b"ID3"})
    assert calls.fetch_records(conn, fake, account, tmp_path, "2026-10-23T15:02:00", lambda s: None)["denied"] == 0
    assert fake.asked == [] and {r["record_state"] for r in calls.list_calls(conn)} == {"denied"}
    assert calls.fetch_records(conn, fake, account, tmp_path, "2026-10-24T15:01:00", lambda s: None)["saved"] == 1


def test_a_file_that_cannot_be_written_stops_the_records_without_failing_the_account(db, tmp_path):
    conn, account = db
    calls.sync_account(conn, FakeClient([[call(1)]]), account, NOW, lambda s: None)
    blocked = tmp_path / "file"
    blocked.write_text("")                                                                        # the folder cannot be made inside a file
    got = calls.fetch_records(conn, FakeClient(records={1: b"ID3"}), account, blocked, NOW, lambda s: None)
    assert got["saved"] == 0 and calls.list_calls(conn)[0]["record_state"] == "wait"
    assert conn.execute("SELECT record_tries FROM avito_call").fetchone()[0] == 1


def test_create_order_takes_the_point_of_the_account_now_and_never_our_number(db):
    conn, account = db
    calls.sync_account(conn, FakeClient([[call(1)]]), account, NOW, lambda s: None)                # no point yet
    pid = points.save_point(conn, {"name": "Гамма", "manager_name": "А", "channel": "vk", "avito_account_id": account})
    card = orders.get_card(conn, calls.create_order(conn, calls.list_calls(conn)[0]["id"], NOW, "pavel"))
    assert card["point_id"] == pid
    second = connections.create_account(conn, "Партнёр-2", NOW, "o")
    own = connections.get_avito_identity(conn, second)["phone"]
    calls.sync_account(conn, FakeClient([[call(7, buyer=own)]]), account, "2026-10-23T15:02:00", lambda s: None)
    row = [r for r in calls.list_calls(conn) if r["call_id"] == 7][0]
    assert orders.get_card(conn, calls.create_order(conn, row["id"], NOW, "pavel"))["client_phone"] == ""


def test_a_claim_a_killed_process_left_is_freed_on_the_second_run(db, tmp_path):
    conn, account = db
    calls.sync_account(conn, FakeClient([[call(1)]]), account, NOW, lambda s: None)
    row = calls.list_calls(conn)[0]["id"]
    conn.execute("UPDATE avito_call SET order_id = -1 WHERE id = ?", (row,))
    conn.commit()
    quiet = dict(say=lambda t: None, warn=lambda t: None)
    calls.run(conn, lambda a: FakeClient([[]]), NOW, tmp_path, lambda s: None, **quiet)
    assert calls.get_call(conn, row)["order_id"] == -1                                            # seen once: maybe the panel is creating it now
    calls.run(conn, lambda a: FakeClient([[]]), NOW, tmp_path, lambda s: None, **quiet)
    assert calls.get_call(conn, row)["order_id"] is None


def test_a_merge_writes_the_calls_into_the_journal(db):
    conn, account = db
    calls.sync_account(conn, FakeClient([[call(1), call(2, buyer="+79117654321")]]), account, NOW, lambda s: None)
    a, b = [r["client_id"] for r in calls.list_calls(conn)]
    gone = [r["id"] for r in calls.list_calls(conn) if r["client_id"] == b]
    clients.merge(conn, a, b, NOW, "o")
    entry = json.loads(conn.execute("SELECT detail FROM audit WHERE action = 'client_merged'").fetchone()[0])
    assert entry["calls"] == gone and len(gone) == 1


def test_old_files_left_behind_are_swept_after_a_week(db, tmp_path):
    conn, account = db
    stale = tmp_path / str(account) / "5.mp3"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"x")
    (tmp_path / str(account) / "6.part").write_bytes(b"x")
    fresh = tmp_path / str(account) / "7.mp3"
    fresh.write_bytes(b"x")
    old = __import__("time").time() - 9 * 86400
    os.utime(stale, (old, old))
    os.utime(tmp_path / str(account) / "6.part", (old, old))
    calls.clean_records(conn, tmp_path, NOW, clock=lambda: old + 9 * 86400)
    assert not stale.exists() and not (tmp_path / str(account) / "6.part").exists() and fresh.exists()


def test_the_phone_search_takes_a_number_typed_with_8(db):
    conn, account = db
    calls.sync_account(conn, FakeClient([[call(1)]]), account, NOW, lambda s: None)
    assert len(calls.list_calls(conn, phone=calls.search_digits("8 911 123-45-67"))) == 1
    assert len(calls.list_calls(conn, phone=calls.search_digits("4567"))) == 1


# ---- the lead's answers of 2026-10-23 ------------------------------------------------------------------------------------------------

def test_only_the_accounts_whose_chats_are_read_have_their_calls_read(db, tmp_path):
    conn, account = db
    unused = connections.create_account(conn, "Партнёр-4", NOW, "o")                                 # «не используется»: chats off
    asked = []
    calls.run(conn, lambda a: asked.append(a["id"]) or FakeClient([[]]), NOW, tmp_path, lambda s: None, say=lambda t: None, warn=lambda t: None)
    assert asked == [account] and unused not in asked


def test_an_account_that_had_calls_and_lost_the_access_is_an_alert(db, tmp_path):
    conn, account = db
    fresh = connections.create_account(conn, "Альфа", NOW, "o")
    chats_on(conn, fresh)
    calls.sync_account(conn, FakeClient([[call(1)]]), account, NOW, lambda s: None)
    alerts = []
    shut = lambda a: FakeClient([AvitoError(403, 1002, "forbidden")])
    assert calls.run(conn, shut, NOW, tmp_path, lambda s: None, say=lambda t: None, warn=lambda t: None, alert=alerts.append) == 0
    assert len(alerts) == 1 and "Основной" in alerts[0] and "Альфа" not in alerts[0]          # never had calls: no calltracking, no alert
    assert "+7911" not in alerts[0]


def test_the_lost_access_alert_of_each_account_waits_its_own_six_hours(db, tmp_path):
    conn, account = db
    second = connections.create_account(conn, "Партнёр-2", NOW, "o")
    chats_on(conn, second)
    calls.sync_account(conn, FakeClient([[call(1)]]), account, NOW, lambda s: None)
    calls.sync_account(conn, FakeClient([[call(2)]]), second, NOW, lambda s: None)
    alerts, quiet = [], dict(say=lambda t: None, warn=lambda t: None)
    shut = lambda a: FakeClient([AvitoError(403, 1002, "forbidden")])
    for minute in ("15:00", "15:02", "15:04"):
        calls.run(conn, shut, "2026-10-23T%s:00" % minute, tmp_path, lambda s: None, alert=alerts.append, **quiet)
    assert len(alerts) == 2                                                                        # one per account, not one per run
    calls.run(conn, shut, "2026-10-23T21:00:00", tmp_path, lambda s: None, alert=alerts.append, **quiet)
    assert len(alerts) == 4                                                                        # six hours later: again
    calls.run(conn, lambda a: FakeClient([[]]), "2026-10-23T21:02:00", tmp_path, lambda s: None, alert=alerts.append, **quiet)
    calls.run(conn, shut, "2026-10-23T21:04:00", tmp_path, lambda s: None, alert=alerts.append, **quiet)
    assert len(alerts) == 6                                                                        # the access came back and went again: said at once


def test_a_press_broken_between_the_order_and_its_link_leaves_no_order_and_the_next_makes_one(db, monkeypatch):
    """Хвост З10: the order and its link to the call are saved together; a press cut off between them leaves nothing, so the next press
    makes the only order (before: the first order stayed without the link and the next press made a second one)."""
    conn, account = db
    calls.sync_account(conn, FakeClient([[call(1)]]), account, NOW, lambda s: None)
    row = calls.list_calls(conn)[0]["id"]
    real = orders.add_event

    def cut(conn_, card_id, now, author, kind, text, level=""):
        if kind == "call":
            raise KeyboardInterrupt("the panel is stopped")
        return real(conn_, card_id, now, author, kind, text, level)
    monkeypatch.setattr(orders, "add_event", cut)
    with pytest.raises(KeyboardInterrupt):
        calls.create_order(conn, row, NOW, "pavel")
    monkeypatch.setattr(orders, "add_event", real)
    assert conn.execute("SELECT COUNT(*) FROM order_card").fetchone()[0] == 0 and calls.get_call(conn, row)["order_id"] is None
    card_id = calls.create_order(conn, row, NOW, "pavel")
    assert conn.execute("SELECT COUNT(*) FROM order_card").fetchone()[0] == 1 and calls.get_call(conn, row)["order_id"] == card_id
    assert [e["text"] for e in orders.list_events(conn, card_id)][-1].startswith("Заказ создан")


def test_the_rules_of_the_first_stage_run_for_an_order_from_a_call(db):
    from tires_sync import order_rules as rules, order_templates as templates
    conn, account = db
    calls.sync_account(conn, FakeClient([[call(1)]]), account, NOW, lambda s: None)
    orders.ensure_pipeline(conn, NOW)
    first = next(st["id"] for st in orders.list_stages(conn) if st["code"] == "new")
    rules.add_rule(conn, first, "enter", 0, "remind", None, "Перезвонить клиенту", "ask", None)
    conn.commit()
    card_id = calls.create_order(conn, calls.list_calls(conn)[0]["id"], NOW, "pavel")
    from tires_sync import order_engine as engine
    assert [t["title"] for t in engine.list_tasks(conn, card_id)] == ["Перезвонить клиенту"]


def test_a_client_with_more_than_two_hundred_calls_has_them_all(db):
    """Хвост З12: the card counts every call and a join records every call it moves (before: the newest 200; the rest stayed behind)."""
    conn, account = db
    many = [call(i, at="2026-10-%02dT%02d:%02d:00+03:00" % (1 + i // 100, (i // 60) % 24, i % 60)) for i in range(1, 206)]
    calls.sync_account(conn, FakeClient([many[:100], many[100:200], many[200:]]), account, "2026-10-23T15:00:00", lambda s: None)
    client_id = calls.list_calls(conn)[0]["client_id"]
    assert client_id and len(calls.calls_of_client(conn, client_id)) == 205
    other = clients._new_client(conn, "Другой", NOW, "o")
    conn.commit()
    clients.merge(conn, other, client_id, NOW, "o")
    conn.commit()
    assert len(calls.calls_of_client(conn, other)) == 205
    clients.unmerge(conn, other, client_id, NOW, "o")
    conn.commit()
    assert len(calls.calls_of_client(conn, client_id)) == 205 and calls.calls_of_client(conn, other) == []   # the undoing gives every call back
