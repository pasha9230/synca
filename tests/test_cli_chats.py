"""`chats-sync` and the accounts that only read messages."""
from __future__ import annotations

import json

import pytest

from http_fakes import ScriptedHttp
from tires_sync import chats, cli, connections, cycle, orders, store
from tires_sync.cli import main
from tires_sync.secretbox import create_key_file, load_box

NOW = "2026-10-13T12:00:00"
TOKEN = (200, {"access_token": "t", "expires_in": 3600})
TS = 1_791_900_000


@pytest.fixture
def env(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    orders.ensure_pipeline(conn, NOW)
    account = connections.create_account(conn, "Только сообщения", NOW, "o")
    key_file = tmp_path / "secret.key"
    create_key_file(key_file)
    box = load_box(key_file)
    connections.set_secret(conn, box, "avito", account, "client_id", "CLIENTID-12345678", NOW, "o")
    connections.set_secret(conn, box, "avito", account, "client_secret", "SECRETVALUE-XYZ", NOW, "o")
    connections.set_modes(conn, account, True, False, NOW, "o")
    conn.close()
    return tmp_path, ["--db", str(tmp_path / "t.db"), "--key-file", str(key_file)]


def replies():
    chat = {"id": "c1", "created": TS, "updated": TS, "context": {"type": "item", "value": {"id": 5, "title": "Ikon", "url": "https://www.avito.ru/x"}},
            "users": [{"id": 1, "name": "Я"}, {"id": 9, "name": "Татьяна"}],
            "last_message": {"id": "m1", "author_id": 9, "created": TS, "content": {"text": "Здравствуйте"}, "type": "text", "direction": "in", "read": 0}}
    message = {"id": "m1", "author_id": 9, "created": TS, "content": {"text": "Здравствуйте"}, "type": "text", "direction": "in", "isRead": False, "read": 0}
    return [TOKEN, (200, {"user_id": 1, "id": 1, "name": "Я"}), (200, {"chats": [chat], "meta": {"has_more": False}}), (200, {"messages": [message]})]


def test_chats_sync_reads_the_chat_makes_no_deal_by_itself_and_changes_nothing_at_avito(env, monkeypatch, capsys):
    tmp, base = env
    http = ScriptedHttp(*replies())
    monkeypatch.setattr(cli, "_http", lambda: http)
    monkeypatch.setattr(cli, "moscow_now", lambda: __import__("datetime").datetime(2026, 10, 13, 12, 0, 0))     # before the client's message
    assert main(["chats-sync"] + base) == 0
    assert {m for m, *_ in http.calls if "messenger" in _[0]} <= {"GET"} and all(c[0] == "GET" or "token" in c[1] for c in http.calls)
    conn = store.connect(tmp / "t.db")
    [chat] = chats.list_chats(conn)
    assert chat["peer_name"] == "Татьяна" and chat["order_id"] is None and conn.execute("SELECT COUNT(*) FROM order_card").fetchone()[0] == 0
    out = capsys.readouterr().out
    assert "чатов 1" in out and "Здравствуйте" not in out and "Татьяна" not in out                            # no client text in the output


def test_an_account_without_the_chats_mode_is_skipped(env, monkeypatch, capsys):
    tmp, base = env
    conn = store.connect(tmp / "t.db")
    connections.set_modes(conn, 1, False, True, NOW, "o")
    conn.close()
    http = ScriptedHttp()
    monkeypatch.setattr(cli, "_http", lambda: http)
    assert main(["chats-sync"] + base) == 0 and http.calls == []


def test_a_feed_free_account_is_not_in_the_cycle_the_stats_or_the_publishing(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    both = connections.create_account(conn, "Фид", NOW, "o")
    chat_only = connections.create_account(conn, "Чаты", NOW, "o")
    connections.set_modes(conn, chat_only, True, False, NOW, "o")
    assert [a["id"] for a in connections.list_feed_accounts(conn)] == [both]
    assert [a["id"] for a in connections.list_chat_accounts(conn)] == [chat_only]


def test_the_test_send_shows_the_plan_without_confirm_and_sends_one_message_with_it(env, monkeypatch, capsys):
    tmp, base = env
    http = ScriptedHttp(*replies())
    monkeypatch.setattr(cli, "_http", lambda: http)
    monkeypatch.setattr(cli, "moscow_now", lambda: __import__("datetime").datetime(2026, 10, 13, 12, 0, 0))
    assert main(["chats-sync"] + base) == 0
    capsys.readouterr()
    calls = len(http.calls)
    assert main(["chat-test-send"] + base + ["--chat", "1", "--text", "Тест"]) == 0
    assert "Ничего не отправлено" in capsys.readouterr().out and len(http.calls) == calls
    http.replies.extend([TOKEN, (200, {"user_id": 1, "id": 1, "name": "Я"}), (200, {"id": "m-test", "created": TS, "direction": "out", "type": "text", "content": {"text": "Тест"}})])
    assert main(["chat-test-send"] + base + ["--chat", "1", "--text", "Тест", "--confirm"]) == 0
    posts = [c for c in http.calls if c[0] == "POST" and "/messenger/" in c[1]]
    assert len(posts) == 1 and "Тест" not in capsys.readouterr().out
    assert main(["chat-test-send"] + base + ["--chat", "99", "--text", "x"]) != 0


def test_a_short_network_failure_is_an_alarm_only_after_fifteen_minutes_unread(env, monkeypatch, capsys):
    """Owner's word 2026-10-20: five misses in a row are no alarm any more (Avito's SSL/502 night); a long silence is."""
    import datetime as dt
    tmp, base = env
    clock = [dt.datetime(2026, 10, 13, 12, 0, 0)]
    monkeypatch.setattr(cli, "moscow_now", lambda: clock[0])

    def broken_http():
        def call(method, url, headers, body):
            raise OSError("handshake timed out")
        return call
    monkeypatch.setattr(cli, "_http", broken_http)
    assert [main(["chats-sync"] + base) for _ in range(5)] == [0, 0, 0, 0, 0]                             # five misses in a row: quiet
    clock[0] += dt.timedelta(minutes=16)
    assert main(["chats-sync"] + base) == 1                                                              # unread for over 15 minutes: the alarm
    http = ScriptedHttp(*replies())
    monkeypatch.setattr(cli, "_http", lambda: http)
    assert main(["chats-sync"] + base) == 0                                                              # a success clears the series
    monkeypatch.setattr(cli, "_http", broken_http)
    clock[0] += dt.timedelta(minutes=20)
    assert main(["chats-sync"] + base) == 0                                                              # a new series starts from this miss


def test_the_minute_runs_are_light_and_every_fifth_is_full_and_a_busy_lock_skips_quietly(env, monkeypatch, capsys):
    tmp, base = env
    monkeypatch.setattr(cli, "moscow_now", lambda: __import__("datetime").datetime(2026, 10, 13, 12, 0, 0))
    pages_asked = []

    class Http(ScriptedHttp):
        def __call__(self, method, url, headers, body):
            if "/messenger/v2/" in url:
                pages_asked.append(url.rsplit("offset=", 1)[-1])
            return super().__call__(method, url, headers, body)

    def run():
        http = Http(*replies())
        http.replies[2] = (200, {"chats": [], "meta": {"has_more": True}})                                   # a list that always has more
        http.replies = http.replies[:3] + [(200, {"chats": [], "meta": {"has_more": True}})] * 7
        monkeypatch.setattr(cli, "_http", lambda: http)
        pages_asked.clear()
        assert main(["chats-sync"] + base) == 0
        return len(pages_asked)
    counts = [run() for _ in range(5)]
    assert counts[:4] == [1, 1, 1, 1] or counts[0] > 1                                                      # the first run of a series is full, then four light ones
    assert max(counts) > 1 and min(counts) == 1
    from tires_sync import chats as chats_mod
    monkeypatch.setattr(chats_mod, "sync_lock", __import__("contextlib").contextmanager(lambda path: (yield False)))
    assert main(["chats-sync"] + base) == 0 and "уже идёт" in capsys.readouterr().out


@pytest.mark.parametrize("error", [KeyError("id"), AttributeError("list has no get"), ValueError("odd")])
def test_an_odd_answer_is_a_real_trouble_the_alarm_is_at_once_and_the_kind_is_written(env, monkeypatch, capsys, error):
    tmp, base = env
    monkeypatch.setattr(cli, "moscow_now", lambda: __import__("datetime").datetime(2026, 10, 13, 12, 0, 0))
    monkeypatch.setattr(cli, "_http", lambda: ScriptedHttp())

    def broken(conn, client, account_id, now, max_pages=1):
        raise error
    monkeypatch.setattr(chats, "sync_account", broken)
    assert main(["chats-sync"] + base) == 1                                                              # at the first one (owner's word 2026-10-20)
    err = capsys.readouterr().err
    assert type(error).__name__ in err and str(error.args[0]) not in err


def test_a_locked_base_is_a_short_trouble_like_a_network_one(env, monkeypatch, capsys):
    tmp, base = env
    monkeypatch.setattr(cli, "moscow_now", lambda: __import__("datetime").datetime(2026, 10, 13, 12, 0, 0))
    monkeypatch.setattr(cli, "_http", lambda: ScriptedHttp())
    monkeypatch.setattr(chats, "sync_account", lambda *a, **k: (_ for _ in ()).throw(__import__("sqlite3").OperationalError("database is locked")))
    assert [main(["chats-sync"] + base) for _ in range(5)] == [0] * 5
    assert "OperationalError" in capsys.readouterr().err


def test_one_odd_account_does_not_stop_the_others(env, monkeypatch, capsys):
    tmp, base = env
    conn = store.connect(tmp / "t.db")
    second = connections.create_account(conn, "Второй", NOW, "o")
    box = load_box(tmp / "secret.key")
    connections.set_secret(conn, box, "avito", second, "client_id", "CLIENTID-2222222", NOW, "o")
    connections.set_secret(conn, box, "avito", second, "client_secret", "SECRETVALUE-222", NOW, "o")
    connections.set_modes(conn, second, True, False, NOW, "o")
    conn.close()
    monkeypatch.setattr(cli, "moscow_now", lambda: __import__("datetime").datetime(2026, 10, 13, 12, 0, 0))
    monkeypatch.setattr(cli, "_http", lambda: ScriptedHttp())
    seen = []

    def sync(conn, client, account_id, now, max_pages=1):
        seen.append(account_id)
        if account_id == 1:
            raise KeyError("id")
        return {"chats": 0, "new_chats": 0, "messages": 0, "errors": 0}
    monkeypatch.setattr(chats, "sync_account", sync)
    assert main(["chats-sync"] + base) == 1 and seen == [1, second]                                       # the second is read although the first answered oddly


def test_a_busy_base_that_cannot_even_count_the_miss_does_not_crash_the_run(env, monkeypatch, capsys):
    import sqlite3
    tmp, base = env
    monkeypatch.setattr(cli, "moscow_now", lambda: __import__("datetime").datetime(2026, 10, 13, 12, 0, 0))
    monkeypatch.setattr(cli, "_http", lambda: ScriptedHttp())
    monkeypatch.setattr(chats, "sync_account", lambda *a, **k: (_ for _ in ()).throw(OSError("timed out")))
    real = store.get_state

    def busy(conn, key):
        if key.startswith("chats_fail_"):
            raise sqlite3.OperationalError("database is locked")
        return real(conn, key)
    monkeypatch.setattr(store, "get_state", busy)
    assert main(["chats-sync"] + base) == 0
    assert "чаты не прочитаны (timed out)" in capsys.readouterr().err


def test_the_text_of_an_odd_error_never_reaches_the_journal_and_what_was_half_written_is_rolled_back(env, monkeypatch, capsys):
    tmp, base = env
    monkeypatch.setattr(cli, "moscow_now", lambda: __import__("datetime").datetime(2026, 10, 13, 12, 0, 0))
    monkeypatch.setattr(cli, "_http", lambda: ScriptedHttp())

    def broken(conn, client, account_id, now, max_pages=1):
        conn.execute("INSERT INTO avito_chat (account_id, chat_id, peer_name) VALUES (?, 'half', 'Привет, это Татьяна')", (account_id,))     # written, not committed
        raise ValueError("Здравствуйте, мой телефон +7 900 000-00-17")
    monkeypatch.setattr(chats, "sync_account", broken)
    assert main(["chats-sync"] + base) == 1                                                              # an odd failure rings at once, but its text stays out of the journal
    err = capsys.readouterr().err
    assert "ValueError" in err and "Татьяна" not in err and "+7 911" not in err and "Здравствуйте" not in err
    conn = store.connect(tmp / "t.db")
    assert conn.execute("SELECT COUNT(*) FROM avito_chat WHERE chat_id = 'half'").fetchone()[0] == 0                       # the half written chat was rolled back


def test_a_key_that_cannot_be_read_for_one_account_does_not_stop_the_others_and_the_run_ends_with_a_failure_count(env, monkeypatch, capsys):
    from tires_sync.secretbox import SecretKeyError
    tmp, base = env
    conn = store.connect(tmp / "t.db")
    second = connections.create_account(conn, "Второй", NOW, "o")
    box = load_box(tmp / "secret.key")
    connections.set_secret(conn, box, "avito", second, "client_id", "CLIENTID-2222222", NOW, "o")
    connections.set_secret(conn, box, "avito", second, "client_secret", "SECRETVALUE-222", NOW, "o")
    connections.set_modes(conn, second, True, False, NOW, "o")
    conn.close()
    monkeypatch.setattr(cli, "moscow_now", lambda: __import__("datetime").datetime(2026, 10, 13, 12, 0, 0))
    monkeypatch.setattr(cli, "_http", lambda: ScriptedHttp())
    real = connections.get_secret

    def get_secret(conn, box_, kind, owner, name):
        if owner == 1:
            raise SecretKeyError("cannot decrypt secret")
        return real(conn, box_, kind, owner, name)
    monkeypatch.setattr(connections, "get_secret", get_secret)
    seen = []
    monkeypatch.setattr(chats, "sync_account", lambda conn, client, account_id, now, max_pages=1: seen.append(account_id) or {"chats": 0, "new_chats": 0, "messages": 0, "errors": 0})
    assert main(["chats-sync"] + base) == 1 and seen == [second]
    assert "ключи не читаются" in capsys.readouterr().err


def test_an_interrupt_of_the_person_or_the_system_is_never_swallowed(env, monkeypatch):
    tmp, base = env
    monkeypatch.setattr(cli, "moscow_now", lambda: __import__("datetime").datetime(2026, 10, 13, 12, 0, 0))
    monkeypatch.setattr(cli, "_http", lambda: ScriptedHttp())
    monkeypatch.setattr(chats, "sync_account", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        main(["chats-sync"] + base)


def test_a_busy_base_after_a_good_read_does_not_crash_the_run_and_the_deals_are_still_bound(env, monkeypatch, capsys):
    import sqlite3
    tmp, base = env
    monkeypatch.setattr(cli, "moscow_now", lambda: __import__("datetime").datetime(2026, 10, 13, 12, 0, 0))
    monkeypatch.setattr(cli, "_http", lambda: ScriptedHttp())
    monkeypatch.setattr(chats, "sync_account", lambda *a, **k: {"chats": 0, "new_chats": 0, "messages": 0, "errors": 0})
    real = store.set_state
    bound = []

    def busy(conn, key, value):
        if key.startswith("chats_fail_"):
            raise sqlite3.OperationalError("database is locked")
        return real(conn, key, value)
    monkeypatch.setattr(store, "set_state", busy)
    monkeypatch.setattr(cli.chat_deals, "process_all", lambda conn, now: bound.append(1) or {"created": 0, "linked": 0, "phones": 0})
    assert main(["chats-sync"] + base) == 0 and bound == [1]


@pytest.mark.parametrize("outage", ["network", "503"])
def test_a_long_avito_outage_is_one_alarm_and_one_note_when_it_is_back(env, monkeypatch, outage):
    """The lead 2026-10-24 (reliability): the chats' own alarm (tires-chats fails after 15 minutes unread → the unit's alert) tells a long Avito
    outage; Avito's breaker adds no second alarm (its opening goes to the journal) and says once when Avito answers again."""
    import datetime as dt
    from tires_sync import reliability
    tmp, base = env
    start = dt.datetime(2026, 10, 13, 12, 0, 0)
    minute = [0]
    monkeypatch.setattr(cli, "moscow_now", lambda: start + dt.timedelta(minutes=minute[0]))
    monkeypatch.setattr(cli, "_epoch", lambda: 1_791_900_000.0 + minute[0] * 60)
    monkeypatch.setattr(reliability, "wall", lambda: 1_791_900_000.0 + minute[0] * 60)
    tries = []

    def broken_http():
        def call(method, url, headers, body):
            tries.append(minute[0])
            if outage == "503":                                                       # the whole of Avito answers 503, its token too
                return 503, {}, b'{"error": {"code": 503, "message": "down"}}'
            raise OSError("handshake timed out")
        return call
    unit_alert = "Tires Sync: служба tires-chats.service завершилась с ошибкой. Откройте журнал: journalctl -u tires-chats.service"
    for minute[0] in range(45):                                                       # the timer: every minute; Avito is away for half an hour
        monkeypatch.setattr(cli, "_http", broken_http if minute[0] < 30 else (lambda: ScriptedHttp(*replies())))
        if main(["chats-sync"] + base):
            main(["alert"] + base + ["--message", unit_alert])                         # OnFailure=tires-alert@%n
    conn = store.connect(tmp / "t.db")
    told = [(a, json.loads(d)["text"]) for a, d in conn.execute("SELECT action, detail FROM audit WHERE action IN ('alert', 'notice', 'service_pause') ORDER BY id")]
    assert [a for a, _ in told] == ["service_pause", "alert", "notice"]
    assert told[1][1] == unit_alert and told[2][1] == "Tires Sync: Авито снова отвечает."
    assert tries.count(20) == {"network": 1, "503": 3}[outage]                        # while it rests: one trial call a minute (a read: its 503 thrice)
