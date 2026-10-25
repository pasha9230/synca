"""`chats-sync` alarm: a short outside trouble waits 15 minutes, a real one rings at once (owner's plan 2026-10-20)."""
from __future__ import annotations

import datetime as dt
import ssl
import urllib.error

import pytest

from http_fakes import ScriptedHttp
from test_cli_chats import TOKEN, env, replies  # noqa: F401  (env is a fixture)
from tires_sync import chats, cli, store
from tires_sync.avito import AvitoAuthError, AvitoError, RateLimited
from tires_sync.cli import main

START = dt.datetime(2026, 10, 20, 22, 0, 0)


@pytest.fixture
def clock(monkeypatch):
    box = [START]
    monkeypatch.setattr(cli, "moscow_now", lambda: box[0])
    monkeypatch.setattr(cli, "_http", lambda: ScriptedHttp())
    return box


def fail_with(monkeypatch, error):
    monkeypatch.setattr(chats, "sync_account", lambda *a, **k: (_ for _ in ()).throw(error))


SHORT = [OSError("timed out"), ssl.SSLError(1, "[SSL: WRONG_VERSION_NUMBER] wrong version number"),
         urllib.error.URLError("connection reset"), TimeoutError("t"),
         AvitoError(502, None, "HTTP 502"), AvitoError(503, None, "HTTP 503"), AvitoError(504, None, "HTTP 504"),
         RateLimited(429, None, "HTTP 429")]
REAL = [AvitoAuthError(401, None, "unauthorized"), AvitoAuthError(403, None, "token request rejected"),
        AvitoError(403, None, "forbidden"), AvitoError(404, None, "HTTP 404"), AvitoError(400, None, "bad"),
        KeyError("id"), AttributeError("x"), ValueError("v")]


@pytest.mark.parametrize("error", SHORT, ids=lambda e: type(e).__name__ + str(getattr(e, "status", "")))
def test_short_failure_five_times_in_five_minutes_is_quiet(env, monkeypatch, clock, error):
    tmp, base = env
    fail_with(monkeypatch, error)
    codes = []
    for _ in range(5):
        codes.append(main(["chats-sync"] + base))
        clock[0] += dt.timedelta(minutes=1)
    assert codes == [0] * 5


@pytest.mark.parametrize("error", SHORT[:1] + SHORT[4:5] + SHORT[7:], ids=lambda e: type(e).__name__)
def test_short_failure_for_sixteen_minutes_rings(env, monkeypatch, clock, error):
    tmp, base = env
    fail_with(monkeypatch, error)
    codes = []
    for _ in range(17):
        codes.append(main(["chats-sync"] + base))
        clock[0] += dt.timedelta(minutes=1)
    assert codes[:15] == [0] * 15                                                  # 0..14 minutes since the first miss; 15 exactly is not "longer"
    assert codes[16] == 1                                                          # 16 minutes


def test_exactly_fifteen_minutes_is_not_yet_an_alarm(env, monkeypatch, clock):
    tmp, base = env
    fail_with(monkeypatch, OSError("t"))
    assert main(["chats-sync"] + base) == 0
    clock[0] += dt.timedelta(minutes=15)
    assert main(["chats-sync"] + base) == 0
    clock[0] += dt.timedelta(seconds=1)
    assert main(["chats-sync"] + base) == 1


def test_a_success_in_the_middle_resets_the_series(env, monkeypatch, clock):
    tmp, base = env
    fail_with(monkeypatch, OSError("t"))
    assert main(["chats-sync"] + base) == 0
    clock[0] += dt.timedelta(minutes=10)
    http = ScriptedHttp(*replies())
    monkeypatch.setattr(cli, "_http", lambda: http)
    monkeypatch.undo()                                                             # the real sync_account and a good Avito
    monkeypatch.setattr(cli, "moscow_now", lambda: clock[0])
    monkeypatch.setattr(cli, "_http", lambda: http)
    assert main(["chats-sync"] + base) == 0
    conn = store.connect(tmp / "t.db")
    assert store.get_state(conn, "chats_fail_1") == "0" and not store.get_state(conn, "chats_fail_1_since")
    conn.close()
    fail_with(monkeypatch, OSError("t"))
    clock[0] += dt.timedelta(minutes=10)                                           # 20 minutes after the first miss, but only 0 after the reset
    assert main(["chats-sync"] + base) == 0
    clock[0] += dt.timedelta(minutes=10)
    assert main(["chats-sync"] + base) == 0


@pytest.mark.parametrize("error", REAL, ids=lambda e: type(e).__name__ + str(getattr(e, "status", "")))
def test_a_real_trouble_rings_at_the_first_failure_without_secrets(env, monkeypatch, clock, capsys, error):
    tmp, base = env
    fail_with(monkeypatch, error)
    assert main(["chats-sync"] + base) == 1
    err = capsys.readouterr().err
    assert "нужно внимание" in err and "CLIENTID-12345678" not in err and "SECRETVALUE-XYZ" not in err


def test_every_failure_is_still_printed_to_the_journal(env, monkeypatch, clock, capsys):
    tmp, base = env
    fail_with(monkeypatch, OSError("handshake timed out"))
    for _ in range(3):
        main(["chats-sync"] + base)
        clock[0] += dt.timedelta(minutes=1)
    err = capsys.readouterr().err
    assert err.count("чаты не прочитаны (handshake timed out)") == 3 and "подряд 3, уже 2 мин" in err


def test_a_real_trouble_in_a_short_series_still_rings_and_does_not_hide_behind_it(env, monkeypatch, clock):
    tmp, base = env
    fail_with(monkeypatch, OSError("t"))
    assert main(["chats-sync"] + base) == 0
    clock[0] += dt.timedelta(minutes=1)
    fail_with(monkeypatch, AvitoAuthError(403, None, "token request rejected"))
    assert main(["chats-sync"] + base) == 1


@pytest.mark.parametrize("junk", ["garbage", "2026-10-20T22:00:00+03:00", "2030-01-01T00:00:00"], ids=["broken", "with-zone", "future"])
def test_an_unusable_start_time_starts_the_series_over_and_never_hides_the_alarm(env, monkeypatch, clock, junk):
    tmp, base = env
    conn = store.connect(tmp / "t.db")
    store.set_state(conn, "chats_fail_1_since", junk)
    conn.close()
    fail_with(monkeypatch, OSError("t"))
    assert main(["chats-sync"] + base) == 0                                        # restarted from now, no crash
    clock[0] += dt.timedelta(minutes=16)
    assert main(["chats-sync"] + base) == 1                                        # and the long silence still rings


def test_a_real_trouble_rings_even_when_the_base_is_busy_and_the_miss_cannot_be_counted(env, monkeypatch, clock):
    import sqlite3
    tmp, base = env
    fail_with(monkeypatch, AvitoError(403, None, "forbidden"))
    real = store.get_state

    def busy(conn, key):
        if key.startswith("chats_fail_"):
            raise sqlite3.OperationalError("database is locked")
        return real(conn, key)
    monkeypatch.setattr(store, "get_state", busy)
    assert main(["chats-sync"] + base) == 1


def test_a_long_series_of_one_account_does_not_make_the_other_ring(env, monkeypatch, clock):
    from tires_sync import connections
    from tires_sync.secretbox import load_box
    tmp, base = env
    conn = store.connect(tmp / "t.db")
    second = connections.create_account(conn, "Второй", "2026-10-13T12:00:00", "o")
    box = load_box(tmp / "secret.key")
    connections.set_secret(conn, box, "avito", second, "client_id", "CLIENTID-2222222", "2026-10-13T12:00:00", "o")
    connections.set_secret(conn, box, "avito", second, "client_secret", "SECRETVALUE-222", "2026-10-13T12:00:00", "o")
    connections.set_modes(conn, second, True, False, "2026-10-13T12:00:00", "o")
    conn.close()

    seen = []

    def sync(conn, client, account_id, now, max_pages=1):
        seen.append(account_id)
        if account_id == 1:
            raise OSError("t")
        return {"chats": 0, "new_chats": 0, "messages": 0, "errors": 0}
    monkeypatch.setattr(chats, "sync_account", sync)
    assert main(["chats-sync"] + base) == 0
    clock[0] += dt.timedelta(minutes=16)
    assert main(["chats-sync"] + base) == 1                                        # only the first account is unread
    assert second in seen
    conn = store.connect(tmp / "t.db")
    assert not store.get_state(conn, "chats_fail_%d_since" % second)


def test_a_busy_base_on_the_write_of_the_start_time_does_not_crash_the_run(env, monkeypatch, clock):
    import sqlite3
    tmp, base = env
    fail_with(monkeypatch, OSError("t"))
    real = store.set_state

    def busy(conn, key, value):
        if key.endswith("_since"):
            raise sqlite3.OperationalError("database is locked")
        return real(conn, key, value)
    monkeypatch.setattr(store, "set_state", busy)
    assert main(["chats-sync"] + base) == 0
