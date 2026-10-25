"""One clock in the source (lane 6D, 2026-10-21): the server's own zone (Moscow on tires-server) never reaches the base. The commands write
UTC like the panel; the life of positions and the retired ads stay in Moscow time, now explicit, so the feed is the same whatever the zone."""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta

import pytest

from panel_helpers import login, make_app
from test_funnel import seed
from test_same_tire import ids, run, setup
from test_tochki_feed import NOW as TOCHKI_NOW, FakeTochki, item, refresh, setup_db
from helpers import catalog_xml, commodity_xml
from tires_sync import cli, store, tochki_feed


@pytest.fixture
def zone():
    """Switch the process zone (datetime.now / localtime follow it) and put it back."""
    before = os.environ.get("TZ")

    def switch(name):
        os.environ["TZ"] = name
        time.tzset()
        offset = {"UTC": 0, "Europe/Moscow": 3, "Asia/Vladivostok": 10}[name]
        assert time.localtime().tm_gmtoff == offset * 3600, "no zone database: the zone did not switch"
    yield switch
    if before is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = before
    time.tzset()


def near(stamp: str, expected: datetime) -> bool:
    return abs(datetime.fromisoformat(stamp) - expected) < timedelta(minutes=1)


@pytest.mark.parametrize("name", ["UTC", "Europe/Moscow", "Asia/Vladivostok"])
def test_the_commands_write_utc_and_the_life_of_positions_moscow_whatever_the_zone(zone, name):
    zone(name)
    assert near(cli._now(), datetime.utcnow())
    assert near(store.moscow_iso(), datetime.utcnow() + timedelta(hours=3))


def test_the_avito_statistics_are_stamped_by_the_commands_clock(tmp_path, monkeypatch):
    store.connect(tmp_path / "t.db").close()
    seen = {}
    monkeypatch.setattr(cli, "load_box", lambda key_file: object())
    monkeypatch.setattr(cli.connections, "list_feed_accounts", lambda conn: [{"id": 1, "name": "A"}])
    monkeypatch.setattr(cli.connections, "get_secret", lambda *a, **k: "x")
    monkeypatch.setattr(cli, "AvitoClient", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_collect_avito_reports", lambda conn, box: None)
    monkeypatch.setattr(cli, "_now", lambda: "2026-10-21T01:20:00")

    def collect(conn, client, account_id, today, days=7, now_iso=""):
        seen["now_iso"] = now_iso
        return {"ads": 0, "mapped": 0, "days": 0}
    monkeypatch.setattr(cli.ad_stats, "collect", collect)
    assert cli.main(["avito-stats", "--db", str(tmp_path / "t.db")]) == 0
    assert seen["now_iso"] == "2026-10-21T01:20:00"


def _lot(code, mnf):
    return commodity_xml(NNOMMODIF=code, NREST="8", SMNFCODE=mnf, SLOAD="94", SWIDTH="205")


def _story(tmp_path, zones, zone):
    """Three cycles of one tire under the given zones: lot A published; another lot of the same tire is taken off under Id 099 just before
    the switch (the old clock, Moscow zone); A is taken off; lot C comes and takes the Id of the latest retired ad, A's 106.
    The feed after each cycle, the retired ads and the stamps of the events."""
    conn, account = setup(tmp_path)
    other = commodity_xml(NNOMMODIF="200", NREST="9", SMNFCODE="OTHER", SLOAD="91", SWIDTH="195")
    unloads = [catalog_xml(_lot("106", "LOT-A"), other), catalog_xml(other), catalog_xml(_lot("130", "LOT-C"), other)]
    feeds = []
    for day, (unload, name) in enumerate(zip(unloads, zones), start=14):
        if day == 15:
            zone("Europe/Moscow")
            store.retire_ad(conn, account, "099", store.get_snapshot(conn, "106", account),
                          (datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds"), "099")     # a minute before A
            conn.commit()
        zone(name)
        run(conn, tmp_path, unload, day=day)
        token = cli.connections.get_account(conn, account)["feed_token"]
        feeds.append((tmp_path / "feed" / token / "next" / "avito_feed.xml").read_bytes())
    retired = conn.execute("SELECT goods_id, ad_id, removed_at FROM retired_ad WHERE goods_id <> '099' ORDER BY goods_id").fetchall()
    events = conn.execute("SELECT at FROM ad_event ORDER BY id").fetchall()
    return feeds, retired, events, ids(tmp_path, conn, account)


def test_the_feed_is_the_same_before_and_after_the_switch_of_the_clock(tmp_path, zone):
    """«Before»: the server in Moscow all along (the old datetime.now() was Moscow there). «After»: the zone flips to UTC right after
    a lot of the tire was taken off, the worst case for a stamp taken from the server's zone (A's removal would look three hours older
    than 099's and lot C would take 099). The feeds must be byte for byte the same."""
    (tmp_path / "before").mkdir()
    (tmp_path / "after").mkdir()
    before = _story(tmp_path / "before", ["Europe/Moscow"] * 3, zone)
    after = _story(tmp_path / "after", ["Europe/Moscow", "UTC", "UTC"], zone)
    assert before[3] == after[3] == ["106", "200"]                                             # lot C under the Id of the latest retired ad
    assert before[0] == after[0]                                                               # every feed of every cycle
    assert [r[:2] for r in before[1]] == [r[:2] for r in after[1]]
    moscow = datetime.utcnow() + timedelta(hours=3)
    assert all(near(r[2], moscow) for r in after[1]) and all(near(e[0], moscow) for e in after[2])     # Moscow, not the zone of the server


def test_a_retired_ad_stamped_after_the_switch_is_later_than_one_stamped_before(tmp_path, zone, monkeypatch):
    """The reader at risk (same_tire picks the latest retired ad of a tire by removed_at): a row of the old code (datetime.now() in
    Moscow) and a later row of the new code with the server in UTC must keep their order."""
    zone("Europe/Moscow")
    old = datetime.now().isoformat(timespec="seconds")
    zone("UTC")
    assert store.moscow_iso() >= old
    assert datetime.now().isoformat(timespec="seconds") < old                                   # what a plain switch to the zone's clock would have done


def test_a_last_attempt_of_4tochki_ahead_of_now_is_no_attempt(tmp_path):
    """The old Moscow stamp is three hours ahead of the new UTC «now»: the hourly load runs at once instead of waiting three hours."""
    conn, box, base = setup_db(tmp_path)
    tochki_feed.paths(base)["state"].write_text('{"attempt_at": "%s"}' % (TOCHKI_NOW + timedelta(hours=2, minutes=50)).isoformat(),
                                                encoding="utf-8")
    assert refresh(conn, box, base, FakeTochki([item("C1")]))["status"] == "ok"
    again = FakeTochki([item("C1")])
    assert refresh(conn, box, base, again, now=TOCHKI_NOW + timedelta(minutes=30))["status"] == "wait" and again.calls == []
    tochki_feed.paths(base)["state"].write_text('{"attempt_at": "%s"}' % (TOCHKI_NOW + timedelta(minutes=5)).isoformat(), encoding="utf-8")
    assert refresh(conn, box, base, again)["status"] == "wait" and again.calls == []          # a cycle that started a bit later: still once an hour


def test_the_events_of_positions_are_shown_in_moscow_and_the_statistics_in_utc(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    login(client)
    conn = store.connect(app.config["DB_PATH"])
    store.record_event(conn, "2026-10-15T06:11:00", 1, "G1", "Шина Kumho", "published")
    conn.commit()
    html = client.get("/history/ads").get_data(as_text=True)
    assert "06:11" in html and "09:11" not in html
    (tmp_path / "f").mkdir()
    app = make_app(tmp_path / "f", NOW=lambda: datetime(2026, 10, 12, 12, 0, 0))
    client = app.test_client()
    login(client)
    conn = store.connect(app.config["DB_PATH"])
    seed(conn)
    conn.execute("UPDATE avito_ad SET seen_at = '2026-10-12T01:20:00'")                          # the stats timer at 04:20 Moscow
    conn.commit()
    html = client.get("/economics/funnel").get_data(as_text=True)
    assert "04:20" in html and "01:20" not in html
