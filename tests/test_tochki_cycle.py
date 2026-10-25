"""4tochki in the hourly cycle (`run-cycle --fetch`): loaded after WebMIM, at most once an hour; its failure is reported and changes
nothing else: not the WebMIM unload, not the feeds, not the exit code. Not set up: not a single request to its address."""
from __future__ import annotations

import json

import pytest

from helpers import catalog_xml, commodity_xml, settings_dict
from test_tochki_feed import FakeTochki, envelope, item
from tires_sync import cli, connections, store, tochki_feed
from tires_sync.cli import main
from tires_sync.secretbox import create_key_file, load_box
from tires_sync.settings_store import init_settings
from tires_sync.tochki_client import ENDPOINT

NOW = "2026-10-21T10:00:00"
UNLOAD = catalog_xml(commodity_xml(NNOMMODIF="100", NREST="12", TERRITORY_RN="1001"))
REFERENCE = '<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>'


class Http:
    def __init__(self, tochki, folder):
        self.tochki = tochki
        self.folder = folder
        self.urls = []
        self.feeds_before_tochki = None

    def __call__(self, method, url, headers, body):
        self.urls.append(url)
        if url == ENDPOINT:
            if self.feeds_before_tochki is None:
                self.feeds_before_tochki = bool(list((self.folder / "feed").rglob("avito_feed.xml")))
            return self.tochki(method, url, headers, body)
        return 200, {}, UNLOAD


def prepared(tmp_path, monkeypatch, tochki, supplier=True, password="pw-SECRET-7"):
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(territory_rn="1001"), NOW)
    connections.create_account(conn, "Основной", NOW, "o")
    key_file = tmp_path / "secret.key"
    create_key_file(key_file)
    box = load_box(key_file)
    connections.set_config(conn, "webmim", 0, {"base_url": "https://a.example", "mode": "apikey", "login": ""}, NOW, "o")
    connections.set_secret(conn, box, "webmim", 0, "api_key", "K" * 32, NOW, "o")
    sid = connections.create_supplier(conn, "4Точки", NOW, "o") if supplier else None
    connections.set_config(conn, "tochki", 0, {"login": "api-login-7", "supplier_id": sid, "feed": False}, NOW, "o")
    if password:
        connections.set_secret(conn, box, "tochki", 0, "password", password, NOW, "o")
    conn.commit()
    conn.close()
    (tmp_path / "unload.xml").write_bytes(UNLOAD)
    (tmp_path / "reference.txt").write_text(REFERENCE, encoding="utf-8")
    http = Http(tochki, tmp_path)
    monkeypatch.setattr(cli, "_http", lambda: http)
    args = ["run-cycle", "--fetch", "--key-file", str(key_file), "--db", str(tmp_path / "t.db"), "--unload", str(tmp_path / "unload.xml"),
            "--reference", str(tmp_path / "reference.txt"), "--feed-root", str(tmp_path / "feed")]
    return args, http


def feed(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    token = connections.get_account(conn, 1)["feed_token"]
    conn.close()
    return (tmp_path / "feed" / token / "next" / "avito_feed.xml").read_text(encoding="utf-8").replace(token, "TOKEN")


def journal(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    try:
        return " ".join(r[0] for r in conn.execute("SELECT detail FROM audit WHERE action IN ('alert', 'notice')"))
    finally:
        conn.close()


def baseline(tmp_path, monkeypatch):
    folder = tmp_path / "base"
    folder.mkdir()
    args, http = prepared(folder, monkeypatch, FakeTochki(), supplier=False)
    assert main(args) == 0
    text = feed(folder).replace(str(folder), "X")
    assert "<Id>100</Id>" in text
    return text


def test_the_cycle_loads_4tochki_after_webmim_and_the_feed_stays_the_same(tmp_path, monkeypatch, capsys):
    expected = baseline(tmp_path, monkeypatch)
    work = tmp_path / "work"
    work.mkdir()
    tochki = FakeTochki([item("C1", rest=41), item("C2")])
    args, http = prepared(work, monkeypatch, tochki)
    assert main(args) == 0
    assert http.urls[0] != ENDPOINT and ENDPOINT in http.urls and http.feeds_before_tochki     # WebMIM first, the feeds do not wait
    assert "4 точки: загружено позиций 2" in capsys.readouterr().out
    assert json.loads((work / "tochki-snapshot.json").read_text(encoding="utf-8"))["items"][0]["code"] == "C1"
    assert feed(work).replace(str(work), "X") == expected
    calls = len(tochki.calls)
    assert main(args) == 0 and len(tochki.calls) == calls                         # the next run within the hour asks nothing


@pytest.mark.parametrize("trouble", ["fault", "raises"])
def test_a_failed_4tochki_load_is_reported_and_changes_nothing_else(tmp_path, monkeypatch, capsys, trouble):
    expected = baseline(tmp_path, monkeypatch)
    work = tmp_path / "work"
    work.mkdir()
    tochki = FakeTochki([item("C1")])
    tochki.fail["GetWarehouses"] = (500, {}, envelope("<s:Fault><faultstring>api-login-7 pw-SECRET-7</faultstring></s:Fault>").encode())
    args, http = prepared(work, monkeypatch, tochki)
    if trouble == "raises":
        def broken(*a, **k):
            raise RuntimeError("pw-SECRET-7")
        monkeypatch.setattr(tochki_feed, "refresh", broken)
    assert main(args) == 0                                                        # the exit code is the cycle's own
    err = capsys.readouterr().err
    assert "4 точки: загрузка не удалась" in err and "pw-SECRET-7" not in err and "api-login-7" not in err
    assert "4 точки: загрузка не удалась" in journal(work) and "pw-SECRET-7" not in journal(work)
    assert feed(work).replace(str(work), "X") == expected
    assert (work / "unload.xml").read_bytes() == UNLOAD


@pytest.mark.parametrize("supplier,password", [(False, "pw"), (True, "")])
def test_without_a_chosen_supplier_or_a_password_4tochki_is_never_asked(tmp_path, monkeypatch, supplier, password):
    tochki = FakeTochki([item("C1")])
    args, http = prepared(tmp_path, monkeypatch, tochki, supplier=supplier, password=password)
    assert main(args) == 0
    assert ENDPOINT not in http.urls and tochki.calls == [] and not (tmp_path / "tochki-snapshot.json").exists()
    assert "4 точки" not in journal(tmp_path)


def test_the_load_is_the_last_step_and_its_hour_is_counted_from_the_start_of_the_cycle(tmp_path, monkeypatch):
    tochki = FakeTochki([item("C1")])
    args, http = prepared(tmp_path, monkeypatch, tochki)
    ticks = iter("2026-10-21T10:%02d:00" % m for m in range(40, 100))
    monkeypatch.setattr(cli, "_now", lambda: next(ticks))
    seen = {}
    real_reminders, real_refresh = cli._send_reminders, tochki_feed.refresh

    def reminders(conn, box):
        seen["reminders"] = True
        return real_reminders(conn, box)

    def refresh(conn, box, base, now, **kw):
        seen["after_reminders"], seen["now"] = seen.get("reminders", False), now
        return real_refresh(conn, box, base, now, **kw)
    monkeypatch.setattr(cli, "_send_reminders", reminders)
    monkeypatch.setattr(tochki_feed, "refresh", refresh)
    assert main(args) == 0
    assert seen["after_reminders"] and seen["now"].isoformat() == "2026-10-21T10:40:00"


def test_a_lasting_trouble_gives_one_and_the_same_alert_text(tmp_path, monkeypatch):
    tochki = FakeTochki([item("C1")])
    tochki.fail["GetWarehouses"] = (503, {}, b"busy")
    args, http = prepared(tmp_path, monkeypatch, tochki)
    assert main(args) == 0
    (tmp_path / "tochki-state.json").write_text("{}", encoding="utf-8")         # an hour later
    tochki.fail["GetWarehouses"] = (500, {}, b"other")
    assert main(args) == 0
    conn = store.connect(tmp_path / "t.db")
    texts = [r[0] for r in conn.execute("SELECT detail FROM audit WHERE action = 'alert' AND detail LIKE '%4 точки%'")]
    conn.close()
    assert len(texts) == 1                                                        # the second is the same text: not repeated
