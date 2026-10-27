"""Stage 1 of the 4tochki supplier: its positions are loaded and shown, but the feeds stay byte for byte what they were.

The golden file was taken on `origin/run/2026-10-20-night` before the 4tochki loader existed (set REGENERATE_TOCHKI_GOLDEN=1 to retake it, only
on a known-good tree). The scenario holds everything that would put a 4tochki position into the feed if it leaked: the supplier is set up,
its snapshot is on disk with a tire the WebMIM unload also has and a tire only 4tochki has, its brand (S2:) is switched on and priced, and
the reference knows the model. Only the switch «в фид» is off."""
from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path

import pytest

from helpers import catalog_xml, commodity_xml, settings_dict
from tires_sync import cli, connections, store
from tires_sync.cli import main
from tires_sync.settings_store import init_settings

NOW = "2026-10-14T10:00:00"
GOLDEN = Path(__file__).with_name("test_tochki_feed_unchanged_golden.json")
REFERENCE = '<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>'

UNLOAD = catalog_xml(
    commodity_xml(NNOMMODIF="100", TERRITORY_RN="1001", NPRICE_DELAYED="5000", NREST="12"),
    commodity_xml(NNOMMODIF="101", TERRITORY_RN="1001", NPRICE_DELAYED="6000", NREST="5", SWIDTH="215", SMNFCODE="TS2",
                  SMODIFNAME="IKON 215/55/16 T 95 Test Model", SMODIFNAME_ORIG="Ikon 215/55/16 T 95 Test Model"))


def tochki_item(code, width, load, rest, price):
    return {"code": code, "name": "Ikon Tyres Test Model %d/55 R16 %sT" % (width, load), "marka": "Ikon Tyres", "model": "Test Model",
            "season": "w", "thorn": False, "type": "car", "quality": 0, "img_big_my": "", "img_big_pish": "https://img.4tochki.test/%s.jpg" % code,
            "price": price, "rest": rest, "width": str(width), "height": "55", "diameter": "16", "load_index": str(load), "speed_index": "T",
            "puncture": "", "strengthening": False, "omolog": "", "card": True}


class GoldenDay(date):
    """The day the golden file was taken: DateEnd and the cycle's clock follow the calendar, the feeds must not."""
    @classmethod
    def today(cls):
        return cls(2026, 10, 21)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(cli, "date", GoldenDay)
    monkeypatch.setattr(cli, "moscow_now", lambda: datetime(2026, 10, 21, 3, 0, 0))


SNAPSHOT = {"version": 1, "fetched_at": "2026-10-14T09:30:00", "warehouse_id": 7, "items": [
    tochki_item("TCH100", 205, 94, 41, 4500.0),       # the same tire as WebMIM 100 (brand, model, size, indices)
    tochki_item("TCH900", 225, 98, 10, 7000.0),       # only 4tochki has it
]}


TRIAL_1 = {"accounts": [1], "mode": "no_twin"}                       # the owner 2026-10-27: the trial in account 1 only


def prepare(tmp_path, feed_switch=False, trial=None):
    conn = store.connect(tmp_path / "t.db")
    priced = {"enabled": [["IKON TYRES", "winter"], ["S2:IKON TYRES", "winter"]],
              "pricing": {"IKON TYRES|winter": {"pct_small": -3.0, "pct_large": -3.0},
                          "S2:IKON TYRES|winter": {"pct_small": 5.0, "pct_large": 5.0}}}
    init_settings(conn, settings_dict(territory_rn="1001", **priced), NOW)
    connections.create_account(conn, "Основной", NOW, "owner")
    connections.create_account(conn, "Партнёр-2", NOW, "owner")
    two = connections.create_supplier(conn, "4Точки", NOW, "owner")              # on the server: a WebMIM supplier row without an address
    connections.set_config(conn, "tochki", 0, dict({"login": "api-user", "supplier_id": two, "feed": feed_switch}, **(trial or {})), NOW, "owner")
    conn.commit()
    conn.close()
    (tmp_path / "unload.xml").write_bytes(UNLOAD)
    (tmp_path / "tochki-snapshot.json").write_text(json.dumps(SNAPSHOT, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "reference.txt").write_text(REFERENCE, encoding="utf-8")
    return ["--db", str(tmp_path / "t.db"), "--unload", str(tmp_path / "unload.xml"),
            "--reference", str(tmp_path / "reference.txt"), "--feed-root", str(tmp_path / "feed")]


def build(tmp_path, feed_switch=False, trial=None) -> dict:
    assert main(["run-cycle"] + prepare(tmp_path, feed_switch, trial)) == 0
    conn = store.connect(tmp_path / "t.db")
    out = {}
    for account in connections.list_accounts(conn):
        token = account["feed_token"]
        out[account["name"]] = (tmp_path / "feed" / token / "next" / "avito_feed.xml").read_text(encoding="utf-8").replace(token, "TOKEN")
    conn.close()
    return out


def test_feeds_of_both_accounts_are_unchanged_with_the_switch_off(tmp_path):
    built = build(tmp_path)
    if os.environ.get("REGENERATE_TOCHKI_GOLDEN"):
        GOLDEN.write_text(json.dumps(built, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert set(built) == set(golden) == {"Основной", "Партнёр-2"}
    for key in golden:
        assert built[key] == golden[key], key
    for text in built.values():
        assert "<Ad>" in text and "TCH" not in text and "s2-" not in text


def test_the_scenario_would_catch_a_leak_the_switch_on_changes_the_feeds(tmp_path):
    """Stage 2 path: with the switch on and account 1 in the trial the same setup does publish 4tochki there, so the golden test above
    really guards the positions out of the feeds. Only the tire WebMIM has not: TCH100 is WebMIM 100 (strict entry, plan §4б). The
    other account is byte for byte what it was without 4tochki (plan §7, test 1)."""
    from datetime import datetime
    SNAPSHOT["fetched_at"] = datetime.utcnow().isoformat(timespec="seconds")       # a fresh load: an old one is not used
    try:
        built = build(tmp_path, feed_switch=True, trial=TRIAL_1)
    finally:
        SNAPSHOT["fetched_at"] = "2026-10-14T09:30:00"
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert built["Основной"] != golden["Основной"] and "<Id>s2-TCH900</Id>" in built["Основной"]
    assert "TCH100" not in built["Основной"] and "<Id>100</Id>" in built["Основной"]
    assert built["Партнёр-2"] == golden["Партнёр-2"]


@pytest.mark.parametrize("trial", [None, {"accounts": [], "mode": "no_twin"}, {"accounts": [1], "mode": "off"}, {"accounts": [1]},
                                   {"accounts": [3], "mode": "merge"}, {"accounts": ["1"], "mode": "no_twin"}])
def test_the_switch_on_without_an_account_in_the_trial_changes_no_feed(tmp_path, trial):
    """«в фид» alone used to put 4tochki into every account at once: now it loads them, and no feed has any (plan §4а)."""
    from datetime import datetime
    SNAPSHOT["fetched_at"] = datetime.utcnow().isoformat(timespec="seconds")
    try:
        built = build(tmp_path, feed_switch=True, trial=trial)
    finally:
        SNAPSHOT["fetched_at"] = "2026-10-14T09:30:00"
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert built == golden


def test_an_old_or_missing_load_with_the_switch_on_holds_the_supplier(tmp_path):
    args = prepare(tmp_path, feed_switch=True)                        # the snapshot is a week old
    assert main(["run-cycle"] + args) == 3                            # reported like a secondary supplier without its file
    conn = store.connect(tmp_path / "t.db")
    token = connections.get_account(conn, 1)["feed_token"]
    text = (tmp_path / "feed" / token / "next" / "avito_feed.xml").read_text(encoding="utf-8")
    assert "s2-" not in text and "<Id>100</Id>" in text
    journal = " ".join(r[0] for r in conn.execute("SELECT detail FROM audit WHERE action = 'alert'"))
    assert "загрузка 4 точек устарела" in journal


def _panel_feeds(tmp_path, with_tochki: bool) -> dict:
    """The panel's own way to build the feeds (a confirmed change → publish_now), after the catalog and the mappings were opened."""
    from datetime import timedelta
    from werkzeug.security import generate_password_hash
    from tires_sync.panel import create_app
    from tires_sync.panel.routes_drafts import publish_now
    from panel_helpers import login
    args = prepare(tmp_path)
    if not with_tochki:
        (tmp_path / "tochki-snapshot.json").unlink()
    app = create_app({"TESTING": True, "SECRET_KEY": "s", "PASSWORD_HASH": generate_password_hash("pw", method="pbkdf2:sha256:1000"),
                      "DB_PATH": str(tmp_path / "t.db"), "UNLOAD_PATH": str(tmp_path / "unload.xml"), "REFERENCE_PATH": str(tmp_path / "reference.txt"),
                      "FEED_ROOT": str(tmp_path / "feed"), "NOW": lambda: datetime(2026, 10, 21, 0, 0, 0)})
    client = app.test_client()
    login(client)
    catalog = client.get("/catalog").get_data(as_text=True)
    assert ("TCH900" in catalog or "Ikon Tyres Test Model 225/55" in catalog) == with_tochki          # the catalog does show them
    client.get("/mappings")
    with app.test_request_context():
        publish_now(app)
    conn = store.connect(tmp_path / "t.db")
    out = {}
    for account in connections.list_accounts(conn):
        token = account["feed_token"]
        out[account["name"]] = (tmp_path / "feed" / token / "next" / "avito_feed.xml").read_text(encoding="utf-8").replace(token, "TOKEN")
    conn.close()
    return out


def test_the_panels_publication_ignores_4tochki_too(tmp_path):
    (tmp_path / "with").mkdir()
    (tmp_path / "without").mkdir()
    shown, plain = _panel_feeds(tmp_path / "with", True), _panel_feeds(tmp_path / "without", False)
    assert shown == plain and set(shown) == {"Основной", "Партнёр-2"}
    for text in shown.values():
        assert "<Id>100" in text and "TCH" not in text and "s2-" not in text
