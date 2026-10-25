import json
import socket

import pytest
from cryptography.fernet import Fernet

from http_fakes import ScriptedHttp
from panel_helpers import login, make_app, post
from tires_sync import reliability, store, webmim_orders
from tires_sync.avito import AvitoAuthError, AvitoClient, AvitoError
from tires_sync.secretbox import SecretBox
from tires_sync.tochki_client import TochkiClient, TochkiError
from tires_sync.webmim_client import WebmimClient

TOKEN = (200, {"access_token": "t", "expires_in": 3600})


class Attempts:
    """attempt() for reliability.call: each item is a status, an exception to raise, or (status, headers)."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.count = 0

    def __call__(self):
        self.count += 1
        item = self.outcomes.pop(0)
        if isinstance(item, BaseException):
            raise item
        status, headers = item if isinstance(item, tuple) else (item, {})
        return status, headers, b"body"


def run(attempt, safe=True, policy=reliability.AVITO, **kw):
    sleeps = []
    kw.setdefault("rand", lambda: 1.0)
    try:
        return reliability.call(attempt, safe=safe, policy=policy, sleep=sleeps.append, **kw), sleeps
    except OSError as err:
        return err, sleeps


# ---- the repeats ---------------------------------------------------------------------------------------------------------------------

def test_a_read_after_5xx_goes_again_with_a_growing_pause_and_a_random_part():
    answer, sleeps = run(Attempts(503, 502, 200), rand=lambda: 0.5)
    assert answer[0] == 200 and sleeps == [1.25, 2.5]                 # base·2^k plus half of the jitter share
    answer, sleeps = run(Attempts(503, 503, 503, 200), rand=lambda: 0.0)
    assert answer[0] == 503 and sleeps == [1.0, 2.0]                  # two repeats, then the last answer as it is


def test_a_write_is_made_once_whatever_the_answer():
    for outcome in (503, (429, {"retry-after": "1"}), ConnectionResetError("reset")):
        attempt = Attempts(outcome, 200)
        _, sleeps = run(attempt, safe=False)
        assert attempt.count == 1 and sleeps == []


def test_429_waits_as_retry_after_says_within_the_ceiling():
    attempt = Attempts((429, {"retry-after": "7"}), (429, {"retry-after": "999"}), (429, {}), (429, {"retry-after": "1"}))
    answer, sleeps = run(attempt)
    assert answer[0] == 429 and sleeps == [7.0, 300.0, 5.0] and attempt.count == 4
    answer, sleeps = run(Attempts((429, {"retry-after": "60"}), 200), policy=reliability.WEBMIM)
    assert answer[0] == 200 and sleeps == [10.0]                      # WebMIM: one repeat, at most 10 s


def test_a_fast_network_failure_goes_again_a_timeout_never():
    answer, sleeps = run(Attempts(ConnectionRefusedError("refused"), 200))
    assert answer[0] == 200 and sleeps == [1.5]
    for timeout in (socket.timeout("timed out"), TimeoutError("timed out"), OSError("<urlopen error timed out>")):
        attempt = Attempts(timeout, 200)
        err, sleeps = run(attempt)
        assert isinstance(err, OSError) and attempt.count == 1 and sleeps == []


def test_a_network_failure_late_in_the_call_is_not_repeated():
    clock = iter([0.0, 0.0, 25.0, 25.0])                                  # the call's start, the attempt's, its end, the pause check
    attempt = Attempts(ConnectionResetError("reset"), 200)
    err, sleeps = run(attempt, clock=lambda: next(clock))
    assert isinstance(err, ConnectionResetError) and attempt.count == 1 and sleeps == []


def test_no_pause_starts_past_the_calls_limit():
    clock = iter([0.0, 17.0, 18.0, 18.0])                                 # a fast 503 at the 18th second
    attempt = Attempts(503, 200)
    answer, sleeps = run(attempt, policy=reliability.WEBMIM, clock=lambda: next(clock))
    assert answer[0] == 503 and attempt.count == 1 and sleeps == []   # 18 s + a 2-3 s pause > 20 s: the panel keeps its time


def test_a_5xx_that_is_the_services_own_answer_is_not_repeated():
    attempt = Attempts(500, 200)
    answer, _ = run(attempt, answered=lambda status, raw: True)
    assert answer[0] == 500 and attempt.count == 1


# ---- the breaker ---------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def bound(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    store.connect(path).close()
    alerts = []
    clock = {"t": 1_800_000_000.0}
    monkeypatch.setattr(reliability, "wall", lambda: clock["t"])
    reliability.bind(path, lambda conn, text, kind="alert": alerts.append((kind, text)))
    return {"path": path, "alerts": alerts, "clock": clock}


def state(path, service="avito"):
    conn = store.connect(path)
    try:
        raw = store.get_state(conn, reliability.STATE_PREFIX + service)
    finally:
        conn.close()
    return json.loads(raw) if raw else None


def fail(bound, n, service="tochki", outcome=503, step=240.0):
    """n failed calls, `step` seconds apart (the first at the current time)."""
    for i in range(n):
        if i:
            bound["clock"]["t"] += step
        run(Attempts(*[outcome] * 3), service=service)


def rests(service="tochki"):
    attempt = Attempts(200)
    answer = run(attempt, service=service)[0]
    return isinstance(answer, reliability.ServiceUnavailable) and attempt.count == 0


def test_five_failures_over_fifteen_minutes_rest_the_service_and_alert_once(bound):
    fail(bound, 4)
    assert state(bound["path"], "tochki")["fails"] == 4 and bound["alerts"] == []
    bound["clock"]["t"] += 240
    fail(bound, 1)                                                           # the fifth, 16 minutes after the first
    assert bound["alerts"] == [("alert", "Tires Sync: 4 точки не отвечает: 5 сбоев подряд за 16 мин (последний: HTTP 503). Запросы к нему на "
                                         "паузе, раз в минуту пробный; сообщу, когда заработает.")]
    bound["clock"]["t"] += 1
    err = run(Attempts(200), service="tochki")[0]
    assert isinstance(err, reliability.ServiceUnavailable) and isinstance(err, OSError)
    assert "4 точки: пауза после сбоев подряд до" in str(err) and "HTTP 503" in str(err)
    run(Attempts(200), service="avito")                                     # another service is not touched
    assert state(bound["path"], "avito") is None


def test_a_short_series_of_misses_is_no_pause_and_no_alarm(bound):
    """The owner 2026-10-20 (Avito's SSL/502 night): misses in a row are no alarm; only a long silence is."""
    fail(bound, 30, step=20.0)                                              # 30 misses within 10 minutes
    assert bound["alerts"] == [] and state(bound["path"], "tochki")["open_until"] == 0.0 and state(bound["path"], "tochki")["fails"] == 30
    bound["clock"]["t"] += 6 * 60
    fail(bound, 1)                                                           # the series is 16 minutes old now
    assert len(bound["alerts"]) == 1 and rests()


def test_while_resting_one_call_a_minute_tries(bound):
    fail(bound, 5)
    bound["clock"]["t"] += 10
    assert rests()
    bound["clock"]["t"] += reliability.TRIAL_EVERY
    fail(bound, 1)                                                           # the trial of the minute fails: rests again, no new alert
    assert rests() and len(bound["alerts"]) == 1
    bound["clock"]["t"] += reliability.TRIAL_EVERY
    attempt = Attempts(200)
    assert run(attempt, service="tochki")[0][0] == 200 and attempt.count == 1
    assert bound["alerts"][1] == ("notice", "Tires Sync: 4 точки снова отвечает.") and len(bound["alerts"]) == 2
    assert state(bound["path"], "tochki")["fails"] == 0 and not rests()
    run(Attempts(200), service="tochki")
    assert len(bound["alerts"]) == 2


def test_after_the_pause_without_calls_the_next_call_goes(bound):
    fail(bound, 5)
    bound["clock"]["t"] += reliability.BREAKER_PAUSE + 1
    assert run(Attempts(200), service="tochki")[0][0] == 200 and bound["alerts"][-1][0] == "notice"


def test_avitos_opening_goes_to_the_journal_only_its_recovery_is_a_note(bound):
    fail(bound, 5, service="avito")
    assert [k for k, _ in bound["alerts"]] == ["journal"] and rests("avito")
    bound["clock"]["t"] += reliability.TRIAL_EVERY
    run(Attempts(200), service="avito")
    assert bound["alerts"][-1] == ("notice", "Tires Sync: Авито снова отвечает.")


def test_an_answer_of_a_live_service_resets_the_count_429_counts_neither_way(bound):
    fail(bound, 4)
    run(Attempts(*[(429, {"retry-after": "1"})] * 4), service="tochki")
    assert state(bound["path"], "tochki")["fails"] == 4
    run(Attempts(404), service="tochki")
    assert state(bound["path"], "tochki")["fails"] == 0
    fail(bound, 4, outcome=ConnectionRefusedError("refused"))
    assert state(bound["path"], "tochki")["fails"] == 4 and state(bound["path"], "tochki")["last_error"] == "соединение отклонено"


def test_the_owners_check_goes_while_the_service_rests_and_closes_it(bound):
    fail(bound, 5)
    attempt = Attempts(200)
    with reliability.probe():
        assert run(attempt, service="tochki")[0][0] == 200
    assert attempt.count == 1 and state(bound["path"], "tochki")["open_until"] == 0.0 and bound["alerts"][-1][0] == "notice"


def test_a_broken_breaker_base_never_breaks_the_call(tmp_path):
    reliability.bind(tmp_path / "none" / "t.db", lambda conn, text, kind="alert": None)
    assert run(Attempts(503, 503, 503), service="avito")[0][0] == 503
    assert run(Attempts(200), service="avito")[0][0] == 200
    assert not (tmp_path / "none").exists()


def test_the_alert_failing_does_not_break_the_call(bound):
    reliability.bind(bound["path"], lambda conn, text, kind="alert": 1 / 0)
    fail(bound, 5)
    assert state(bound["path"], "tochki")["alerted"] is True and rests()


def test_without_bind_only_the_repeats_work(tmp_path):
    reliability.unbind()
    for _ in range(7):
        assert run(Attempts(503, 503, 503), service="avito")[0][0] == 503


def test_owner_alert_goes_through_the_commands_alert(monkeypatch, tmp_path):
    from tires_sync import cli
    seen = []
    monkeypatch.setattr(cli, "_alert", lambda conn, box, text, notice=False, recovery=False: seen.append((box, text, notice, recovery)))
    reliability.owner_alert(lambda: "box")(None, "x")
    reliability.owner_alert(lambda: 1 / 0)(None, "y", "notice")
    conn = store.connect(tmp_path / "j.db")
    reliability.owner_alert(lambda: "box")(conn, "z", "journal")
    assert seen == [("box", "x", False, False), (None, "y", True, True)]          # its notice is «снова отвечает»: a recovery (хвост N1)
    assert conn.execute("SELECT action, detail FROM audit").fetchall() == [("service_pause", '{"text": "z"}')]


def test_the_command_and_the_panel_bind_their_base(tmp_path, monkeypatch):
    from tires_sync import cli
    db = tmp_path / "c.db"
    store.connect(db).close()
    monkeypatch.setattr(cli, "_cmd_alert", lambda args: 0)
    cli.main(["alert", "--db", str(db), "--message", "x"])
    assert reliability._BOUND[0] == str(db)
    app = make_app(tmp_path)
    assert reliability._BOUND[0] == app.config["DB_PATH"]


# ---- the clients ---------------------------------------------------------------------------------------------------------------------

def avito(*replies, **kw):
    http = ScriptedHttp(TOKEN, *replies)
    sleeps = []
    return AvitoClient("cid", "secret", http=http, sleep=sleeps.append, rand=lambda: 0.0, **kw), http, sleeps


def test_avito_profile_set_is_repeated_as_before():
    down = (503, {"error": {"code": 503, "message": "down"}})
    client, http, sleeps = avito(down, down, down, allow_write=True)
    with pytest.raises(AvitoError):
        client.set_profile(feed_url="https://x/f.xml", report_email="a@b.ru", schedule=[{"rate": 1, "weekdays": [0], "time_slots": [0]}])
    assert len(http.calls) == 1 + 3 and sleeps == [1.0, 2.0]          # as before the common policy: the token and three attempts
    limited = (429, {"error": {"code": 429}}, {"Retry-After": "7"})
    client, http, sleeps = avito(limited, limited, limited, limited, allow_write=True)
    with pytest.raises(AvitoError):
        client.set_profile(feed_url="https://x/f.xml", report_email="a@b.ru", schedule=[{"rate": 1, "weekdays": [0], "time_slots": [0]}])
    assert len(http.calls) == 1 + 4 and sleeps == [7.0, 7.0, 7.0]


def test_avito_sends_and_reads_without_wait_go_once():
    down = (503, {"error": {"code": 503, "message": "down"}})
    client, http, sleeps = avito(down, allow_write=True)
    with pytest.raises(AvitoError):
        client.send_message(1, "c", "привет")
    assert len(http.calls) == 2 and sleeps == []
    client, http, sleeps = avito(down)
    with pytest.raises(AvitoError):
        client.chats(1, retry=False)
    assert len(http.calls) == 2 and sleeps == []


def test_avito_a_post_not_marked_safe_goes_once():
    client, http, sleeps = avito((503, {"error": {"code": 503}}), (200, {}), allow_write=True)
    with pytest.raises(AvitoError):
        client._request("POST", "/some/write", payload={"x": 1})
    assert len(http.calls) == 2 and sleeps == []


def test_avito_reading_posts_are_repeated():
    down = (503, {"error": {"code": 503}})
    client, http, sleeps = avito(down, (200, {"items": []}))
    client.item_stats(1, [5], "2026-10-01", "2026-10-02")
    assert len(http.calls) == 3 and sleeps == [1.0]
    client, http, sleeps = avito(down, (200, {"result": {"items": []}}))
    client.calls("2026-10-01T00:00:00", "2026-10-02T00:00:00")
    assert len(http.calls) == 3 and sleeps == [1.0]


def test_avito_a_401_still_refreshes_the_token_once():
    client, http, _ = avito((401, {}), TOKEN, (200, {"autoload_enabled": True}))
    assert client.get_profile()["autoload_enabled"] is True and len(http.calls) == 4


def test_webmim_orders_reads_are_repeated_writes_and_the_token_are_not():
    token = (200, {"accessToken": "tok"})
    http = ScriptedHttp(token, (500, {}), (200, {"version": "2.1"}))
    client = webmim_orders.OrdersClient(webmim_orders.SERVERS["test"], "u", "p", http=http)
    assert client.version() == "2.1" and len(http.calls) == 3
    http = ScriptedHttp(token, (500, {}), (200, {"id": 1}))
    client = webmim_orders.OrdersClient(webmim_orders.SERVERS["test"], "u", "p", http=http)
    with pytest.raises(webmim_orders.OrdersError):
        client.create_order({"x": 1})
    assert [c[0] for c in http.calls] == ["GET", "POST"]
    http = ScriptedHttp((500, {}), token)
    client = webmim_orders.OrdersClient(webmim_orders.SERVERS["test"], "u", "p", http=http)
    with pytest.raises(webmim_orders.OrdersError):
        client.token()
    assert len(http.calls) == 1


def test_webmim_login_is_made_once_the_unload_is_repeated(bound):
    http = ScriptedHttp((502, {}), (200, {}))
    client = WebmimClient("https://webmim.example", mode="login", login="u", password="p", http=http)
    with pytest.raises(Exception):
        client.api_key()
    assert len(http.calls) == 1
    http = ScriptedHttp((503, "x"), (200, "<x/>"))
    assert WebmimClient("https://webmim.example", mode="apikey", api_key="K", http=http).fetch_unload("1") == b"<x/>"
    assert state(bound["path"], "webmim:webmim.example")["fails"] == 0   # the login's 502 counted; the unload recovered within its call
    tries = []

    def broken(method, url, headers, body):
        tries.append(url)
        raise ConnectionResetError("reset")
    for i in range(5):
        bound["clock"]["t"] += 240
        with pytest.raises(OSError):
            WebmimClient("https://webmim.example", mode="apikey", api_key="K", http=broken).fetch_unload("1")
    assert len(tries) == 5 * 2                                              # one repeat (reliability.WEBMIM)
    assert state(bound["path"], "webmim:webmim.example")["fails"] == 5
    assert bound["alerts"][0][0] == "alert" and "WebMIM (webmim.example) не отвечает" in bound["alerts"][0][1]
    assert "access-token" not in bound["alerts"][0][1] and "K" not in state(bound["path"], "webmim:webmim.example")["last_error"]


def test_tochki_reads_are_repeated_a_fault_is_an_answer(bound):
    http = ScriptedHttp(*[(500, "<s:Envelope><s:Body><s:Fault>x</s:Fault></s:Body></s:Envelope>")] * 6)
    for _ in range(6):
        with pytest.raises(TochkiError):
            TochkiClient("u", "p", http=http).ping()
    assert len(http.calls) == 6 and state(bound["path"], "tochki") is None


# ---- the panel -----------------------------------------------------------------------------------------------------------------------

def test_the_connections_show_the_services_state(tmp_path, monkeypatch):
    app = make_app(tmp_path, SECRET_BOX=SecretBox(Fernet.generate_key()))
    clock = {"t": 1_800_000_000.0}
    monkeypatch.setattr(reliability, "wall", lambda: clock["t"])
    client = app.test_client()
    login(client)
    html = client.get("/connections/tochki").get_data(as_text=True)
    assert "Сервисы: <b>Авито</b> — работает · <b>WebMIM</b> — работает · <b>4 точки</b> — работает." in html
    reliability.bind(app.config["DB_PATH"], lambda conn, text, kind="alert": None)
    for _ in range(5):
        clock["t"] += 240
        run(Attempts(*[503] * 3), service="webmim:webmim.svrauto.ru")
    conn = store.connect(app.config["DB_PATH"])
    rows = reliability.status(conn, now=clock["t"])
    conn.close()
    pause = [r for r in rows if r["service"] == "webmim:webmim.svrauto.ru"][0]
    assert pause["state"] == "pause" and pause["until"] == reliability.moscow(clock["t"] + reliability.BREAKER_PAUSE)
    monkeypatch.setattr(reliability, "status", lambda conn, now=None: rows)
    html = client.get("/connections/avito").get_data(as_text=True)
    assert "<b>WebMIM (webmim.svrauto.ru)</b> — <span class=\"text-err\">пауза до %s</span> (5 сбоев подряд: HTTP 503)" % pause["until"] in html


def test_the_owners_check_of_4tochki_goes_while_it_rests(tmp_path, monkeypatch):
    ping = (200, '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><GetPingResponse xmlns="http://api-b2b.4tochki.ru/WCF/ClientService/">'
                 '<GetPingResult>true</GetPingResult></GetPingResponse></s:Body></s:Envelope>')
    http = ScriptedHttp(ping, *[(500, "boom")] * 20)
    app = make_app(tmp_path, SECRET_BOX=SecretBox(Fernet.generate_key()), TOCHKI_HTTP=http)
    client = app.test_client()
    tok = login(client)
    post(client, "/connections/tochki", tok, login="api-user", password="p")
    clock = {"t": 1_800_000_000.0}
    monkeypatch.setattr(reliability, "wall", lambda: clock["t"])
    for _ in range(5):
        clock["t"] += 240
        run(Attempts(*[503] * 3), service="tochki")
    clock["t"] += 1
    assert rests() and http.calls == []
    post(client, "/connections/tochki/check", tok)
    assert http.calls                                                   # the check went to 4 точки although the service rested


# ---- after the review (round 1) ------------------------------------------------------------------------------------------------------

def test_a_write_goes_even_while_the_service_rests(bound):
    """A send refused by the pause would be recorded as «unknown, may have gone» and never sent again: the pause holds back reads only."""
    fail(bound, 5)
    bound["clock"]["t"] += 1
    assert rests()
    attempt = Attempts(200)
    assert run(attempt, safe=False, service="tochki")[0][0] == 200 and attempt.count == 1
    assert bound["alerts"][-1] == ("notice", "Tires Sync: 4 точки снова отвечает.")


def test_avito_sends_while_avito_rests(bound):
    fail(bound, 5, service="avito")
    bound["clock"]["t"] += 1
    client, http, _ = avito((200, {"id": "m"}), allow_write=True)
    assert client.send_message(1, "c", "привет") == {"id": "m"} and [c[0] for c in http.calls] == ["POST", "POST"]


def test_avito_token_5xx_counts_as_a_failure_and_is_repeated_on_a_read(bound):
    down = (503, {"error": {"code": 503, "message": "down"}})
    for _ in range(5):
        http = ScriptedHttp(down, down, down)
        with pytest.raises(AvitoError) as err:
            AvitoClient("cid", "secret", http=http, sleep=lambda s: None).get_profile()
        assert err.value.status == 503 and len(http.calls) == 3          # the token asked three times, as any read
        bound["clock"]["t"] += 240
    assert state(bound["path"], "avito")["fails"] == 5 and state(bound["path"], "avito")["open_until"] > 0
    http = ScriptedHttp(down)
    with pytest.raises(AvitoError) as err:
        AvitoClient("cid", "secret", http=http, sleep=lambda s: None, allow_write=True).send_message(1, "c", "x")
    assert len(http.calls) == 1 and err.value.status == 503 and not isinstance(err.value, AvitoAuthError)   # a write: the token once
    http = ScriptedHttp((401, {}))
    with pytest.raises(AvitoError):
        AvitoClient("cid", "secret", http=http, sleep=lambda s: None).get_profile()


def test_webmim_does_not_repeat_a_gateway_timeout_or_a_slow_failure():
    for code in (504, 408):
        attempt = Attempts(code, 200)
        assert run(attempt, policy=reliability.WEBMIM)[0][0] == code and attempt.count == 1
    clock = iter([0.0, 0.0, 6.0, 6.0, 6.0])                                # the 502 came after 6 s: not a blip
    attempt = Attempts(502, 200)
    assert run(attempt, policy=reliability.WEBMIM, clock=lambda: next(clock))[0][0] == 502 and attempt.count == 1
    attempt = Attempts(502, 502, 200)
    assert run(attempt, policy=reliability.WEBMIM)[0][0] == 502 and attempt.count == 2   # one repeat at most: a press keeps its time
    attempt = Attempts(504, 200)
    assert run(attempt)[0][0] == 200 and attempt.count == 2                # Avito as before: 504 is a 5xx


def test_an_answer_too_large_is_neither_repeated_nor_a_failure(bound):
    from tires_sync.transport import ResponseTooLarge
    for _ in range(6):
        attempt = Attempts(ResponseTooLarge("ответ сервера слишком большой"), 200)
        assert isinstance(run(attempt, service="tochki")[0], ResponseTooLarge) and attempt.count == 1
        bound["clock"]["t"] += 240
    assert state(bound["path"], "tochki") is None


def test_the_trial_mark_is_the_claim_and_a_minutes_timer_gets_its_trial(bound):
    fail(bound, 5)
    bound["clock"]["t"] += 58                                              # the timer's own jitter: a minute is not exactly 60 s
    claimed = bound["clock"]["t"]

    def slow():
        bound["clock"]["t"] += 30
        raise ConnectionResetError("reset")
    run(slow, service="tochki")
    assert state(bound["path"], "tochki")["trial_at"] == claimed


def test_a_series_without_a_pause_is_not_shown_as_a_trial(bound):
    fail(bound, 6, step=20.0)
    conn = store.connect(bound["path"])
    [row] = [r for r in reliability.status(conn, now=bound["clock"]["t"]) if r["service"] == "tochki"]
    conn.close()
    assert row["state"] == "ok" and row["fails"] == 6


def test_an_old_webmim_server_leaves_the_line(bound):
    run(Attempts(503, 503, 503), service="webmim:old.example")
    run(Attempts(200), service="webmim:old.example")
    bound["clock"]["t"] += 8 * 86400
    conn = store.connect(bound["path"])
    labels = [r["label"] for r in reliability.status(conn, now=bound["clock"]["t"])]
    conn.close()
    assert labels == ["Авито", "WebMIM", "4 точки"]


# ---- after the review (round 2) ------------------------------------------------------------------------------------------------------

def test_avito_reads_without_wait_are_held_by_the_pause_but_for_the_trial(bound):
    """retry=False (the chats every minute) means «do not wait», not «a write»: while Avito rests they fail at once, one trial a minute."""
    fail(bound, 5, service="avito")
    bound["clock"]["t"] += 1
    client, http, _ = avito()
    with pytest.raises(reliability.ServiceUnavailable):
        client.chats(1, retry=False)
    assert http.calls == []
    bound["clock"]["t"] += reliability.TRIAL_EVERY
    client, http, _ = avito((200, {"chats": []}))
    assert client.chats(1, retry=False) == {"chats": []} and len(http.calls) == 2      # the trial: the token and the read
    assert state(bound["path"], "avito")["fails"] == 0


def test_a_failed_trial_without_wait_leaves_the_minute_to_itself(bound):
    fail(bound, 5, service="avito")
    bound["clock"]["t"] += reliability.TRIAL_EVERY
    client, http, _ = avito((503, {}))
    with pytest.raises(AvitoError):
        client.chats(1, retry=False)
    client, http, _ = avito()
    with pytest.raises(reliability.ServiceUnavailable):
        client.chats(1, retry=False)
    assert http.calls == []


def test_webmim_makes_one_repeat_in_all(bound):
    attempt = Attempts((429, {"retry-after": "1"}), 503, 200)
    assert run(attempt, policy=reliability.WEBMIM)[0][0] == 503 and attempt.count == 2   # 429 then a 5xx: the one repeat is spent


def test_webmim_login_is_held_by_the_pause_its_logout_is_not(bound):
    fail(bound, 5, service="webmim:webmim.example")
    bound["clock"]["t"] += 1
    http = ScriptedHttp()
    with pytest.raises(OSError):
        WebmimClient("https://webmim.example", mode="login", login="u", password="p", http=http).api_key()
    assert http.calls == []                                                 # no session made while it rests
    http = ScriptedHttp((200, {}))
    webmim_orders.OrdersClient("https://webmim.example", "u", "p", http=http)._call("/api/v3/auth/logout", {}, method="POST")
    assert len(http.calls) == 1                                             # freeing a session always goes


def test_a_success_resets_the_failures_another_process_wrote_while_it_was_in_flight(bound):
    """tails2 Ф10: the call read «healthy» before going; meanwhile another process recorded failures. Its success is fresh news: the series
    starts again (it did not: the write was skipped by the state read before the call)."""
    def attempt():
        fail(bound, 2)                                                        # another process, during this call
        return 200, {}, b""
    assert run(attempt, service="tochki")[0][0] == 200
    assert state(bound["path"], "tochki")["fails"] == 0
