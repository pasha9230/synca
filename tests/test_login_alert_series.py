"""The alert about guessing of the panel password (the owner 2026-10-27): every series that crosses the threshold is reported, not once an
hour. One series is one alert: it goes on while the failed sign-ins of the window stay above half the threshold, and a new one starts after
the count fell to half or lower. The lock of an address and the journal without passwords stay as they are."""
from __future__ import annotations

from datetime import datetime, timedelta

from panel_helpers import make_app
from tires_sync import store
from tires_sync.panel import common


def told(monkeypatch):
    from tires_sync import notify
    said = []
    monkeypatch.setattr(notify, "notifier_for", lambda conn, box, http=None: said.append)
    return said


def panel(tmp_path, clock):
    app = make_app(tmp_path, TRUST_PROXY=1, NOW=lambda: clock[0])
    client = app.test_client()
    import re
    tok = re.search(r'name="csrf" value="([0-9a-f]+)"', client.get("/login").get_data(as_text=True)).group(1)
    seq = [0]

    def guess(password="bad"):
        seq[0] += 1                                                                        # every try from an address of its own: no lock
        n = seq[0]
        return client.post("/login", data={"csrf": tok, "login": "", "password": password},
                           headers={"X-Forwarded-For": "10.%d.%d.%d" % (n // 65536 % 256, n // 256 % 256, n % 256)}).status_code
    return app, guess


def test_one_series_is_one_alert_however_long_it_goes(tmp_path, monkeypatch):
    said = told(monkeypatch)
    clock = [datetime(2026, 10, 7, 12, 0, 0)]
    app, guess = panel(tmp_path, clock)
    for _ in range(common.LOGIN_ALERT_FAILURES * 3):
        guess()
    assert len(said) == 1


def test_a_second_series_within_the_hour_is_reported_again(tmp_path, monkeypatch):
    said = told(monkeypatch)
    clock = [datetime(2026, 10, 7, 12, 0, 0)]
    app, guess = panel(tmp_path, clock)
    for _ in range(common.LOGIN_ALERT_FAILURES + 2):
        guess()
    assert len(said) == 1
    clock[0] += timedelta(minutes=20)                                                      # the window (15 min) emptied: the series is over
    for _ in range(common.LOGIN_ALERT_FAILURES + 2):
        guess()
    assert len(said) == 2 and all("больше 30 неудачных входов" in s for s in said)


def test_a_series_that_never_fell_to_half_stays_one_even_after_an_hour(tmp_path, monkeypatch):
    """Hovering around the threshold (about 31 failures in any 15 minutes for two hours) is one series, not an alert per failure."""
    said = told(monkeypatch)
    clock = [datetime(2026, 10, 7, 12, 0, 0)]
    app, guess = panel(tmp_path, clock)
    for _ in range(250):
        guess()
        clock[0] += timedelta(seconds=29)
    assert len(said) == 1


def test_a_count_that_fell_to_half_starts_a_new_series(tmp_path, monkeypatch):
    said = told(monkeypatch)
    clock = [datetime(2026, 10, 7, 12, 0, 0)]
    app, guess = panel(tmp_path, clock)
    for _ in range(common.LOGIN_ALERT_FAILURES + 2):
        guess()
    clock[0] += timedelta(minutes=16)
    guess()                                                                                # the window holds 1: at or below half, the series ended
    clock[0] += timedelta(seconds=1)
    assert len(said) == 1
    for _ in range(common.LOGIN_ALERT_FAILURES):
        guess()
    assert len(said) == 2


def test_the_series_another_worker_reported_is_not_reported_again(tmp_path, monkeypatch):
    said = told(monkeypatch)
    clock = [datetime(2026, 10, 7, 12, 0, 0)]
    app, guess = panel(tmp_path, clock)
    conn = store.connect(app.config["DB_PATH"])
    for n in range(common.LOGIN_ALERT_FAILURES + 1):                                        # the other worker's failures and its alert
        store.add_login_failure(conn, "10.200.0.%d" % n, "2026-10-07T11:59:00")
    store.set_state(conn, common.LOGIN_ALERT_KEY, "2026-10-07T11:59:00")
    for _ in range(5):
        guess()
    assert said == []


def test_the_address_lock_and_a_journal_without_passwords_stay(tmp_path, monkeypatch):
    told(monkeypatch)
    clock = [datetime(2026, 10, 7, 12, 0, 0)]
    app = make_app(tmp_path, TRUST_PROXY=1, NOW=lambda: clock[0])
    client = app.test_client()
    import re
    tok = re.search(r'name="csrf" value="([0-9a-f]+)"', client.get("/login").get_data(as_text=True)).group(1)
    codes = [client.post("/login", data={"csrf": tok, "login": "", "password": "секрет-%d" % n}, headers={"X-Forwarded-For": "10.9.9.9"}).status_code
             for n in range(app.config["MAX_LOGIN_FAILURES"] + 1)]
    assert codes[-1] == 429 and codes[0] == 401
    rows = store.connect(app.config["DB_PATH"]).execute("SELECT detail FROM audit").fetchall()
    assert rows and not any("секрет" in (r[0] or "") for r in rows)
