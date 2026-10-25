from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

import pytest

from helpers import catalog_xml, commodity_xml, settings_dict
from http_fakes import ScriptedHttp
from panel_helpers import login, make_app, post
from tires_sync import cli, connections, store
from tires_sync.cli import main
from tires_sync.catalog import resolve_brand
from tires_sync.mappings import confirm_brand_mapping
from tires_sync.webmim_client import WebmimClient, WebmimError, download_unload
from test_multi_supplier_cycle import ONE, TWO, feed_text, files, prepared

NOW = "2026-10-14T10:00:00"


# --- C2: the WebMIM tab markup is well formed --------------------------------------------------------------------

def test_the_tab_bar_is_well_formed_on_every_connections_page(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    login(client)
    connections.create_account(store.connect(app.config["DB_PATH"]), "A", NOW, "owner")
    for url in ("/connections/webmim", "/connections/telegram", "/connections/avito/1"):
        html = client.get(url).get_data(as_text=True)
        assert re.search(r'<a href="/connections/avito"[^>]*>Аккаунты Авито</a>', html), url
        assert 'href="/connections/avito"\n<nav' not in html and "</nav>\n>" not in html
        assert html.count("<nav") == html.count("</nav>")


# --- C1: a secondary supplier that cannot be read never loses its published ads ------------------------------------

def publish_both(tmp_path):
    conn = prepared(tmp_path)
    connections.set_live(conn, 1, True, NOW, "owner")
    conn.close()
    args = files(tmp_path, ONE, TWO)
    assert main(["run-cycle", "--publish"] + args) == 0
    conn = store.connect(tmp_path / "t.db")
    assert set(store.load_published(conn, account_id=1)) == {"5", "s2-5"}
    return conn, args


def second_survives(tmp_path, conn, args, capsys):
    code = main(["run-cycle", "--publish"] + args)
    published = set(store.load_published(store.connect(tmp_path / "t.db"), account_id=1))
    return code, published, capsys.readouterr().out


def test_wrong_territory_file_of_a_secondary_supplier_holds_its_ads(tmp_path, capsys):
    conn, args = publish_both(tmp_path)
    (tmp_path / "unload-2.xml").write_bytes(catalog_xml(commodity_xml(NNOMMODIF="5", TERRITORY_RN="9999")))
    code, published, out = second_survives(tmp_path, conn, args, capsys)
    assert code == 3 and published == {"5", "s2-5"} and "Второй" in out


def test_corrupt_file_and_missing_file_of_a_secondary_supplier_hold_its_ads(tmp_path, capsys):
    conn, args = publish_both(tmp_path)
    (tmp_path / "unload-2.xml").write_bytes(b"<CATALOG><broken")
    code, published, out = second_survives(tmp_path, conn, args, capsys)
    assert code == 3 and published == {"5", "s2-5"}
    (tmp_path / "unload-2.xml").unlink()
    code, published, out = second_survives(tmp_path, conn, args, capsys)
    assert code == 3 and published == {"5", "s2-5"} and "нет файла" in out


def test_clearing_the_territory_of_a_secondary_supplier_holds_its_ads_and_exits_non_zero(tmp_path, capsys):
    conn, args = publish_both(tmp_path)
    connections.set_config(conn, "webmim", 2, {"base_url": "https://b.example", "mode": "apikey", "territory_rn": ""}, NOW, "owner")
    code, published, out = second_survives(tmp_path, conn, args, capsys)
    assert code == 3 and published == {"5", "s2-5"} and "территория" in out


def test_an_empty_first_supplier_holds_its_ads_while_the_second_keeps_working(tmp_path, capsys):
    conn, args = publish_both(tmp_path)
    (tmp_path / "unload.xml").write_bytes(catalog_xml(commodity_xml(NNOMMODIF="5", TERRITORY_RN="9999")))
    code, published, out = second_survives(tmp_path, conn, args, capsys)
    assert code == 3 and published == {"5", "s2-5"}


def test_a_stale_file_of_a_secondary_supplier_is_held_and_reported(tmp_path, capsys):
    conn, args = publish_both(tmp_path)
    old = (datetime.utcnow() - timedelta(days=10)).isoformat(timespec="seconds")
    (tmp_path / "unload-2.xml.meta.json").write_text(json.dumps({"fetched_at": old, "items": 1}))
    code, published, out = second_survives(tmp_path, conn, args, capsys)
    assert code == 3 and published == {"5", "s2-5"} and "устарела" in out


def test_download_counts_only_items_of_the_requested_territory(tmp_path):
    class Client:
        def fetch_unload(self, territory):
            return catalog_xml(commodity_xml(NNOMMODIF="1", TERRITORY_RN="9999"))
    dest = tmp_path / "u.xml"
    with pytest.raises(WebmimError):
        download_unload(Client(), "1001", dest, NOW)
    assert not dest.exists()


# --- I1: a brand mapping saved for a secondary supplier's brand works ---------------------------------------------

def test_brand_mapping_of_a_prefixed_brand_is_shared_and_effective(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    ref = {"Foo Tyres": frozenset({"X"})}
    confirm_brand_mapping(conn, "S2:FOO", "Foo Tyres", ref, NOW, "owner")
    brand_map = store.load_brand_map(conn)
    assert brand_map == {"FOO": "Foo Tyres"}
    assert resolve_brand("S2:FOO", ref, brand_map) == "Foo Tyres" and resolve_brand("FOO", ref, brand_map) == "Foo Tyres"


# --- I2: readiness shows every supplier; alerts are not duplicated -----------------------------------------------

def test_readiness_has_a_line_per_secondary_supplier(tmp_path, monkeypatch):
    from tires_sync.panel import data

    class Clock(datetime):                     # the day of the file below: the panel ages a second unload by the clock as the cycle (run tails4)
        @classmethod
        def utcnow(cls):
            return datetime(2026, 10, 14, 10, 0, 0)
    monkeypatch.setattr(data, "datetime", Clock)
    app = make_app(tmp_path)
    client = app.test_client()
    login(client)
    conn = store.connect(app.config["DB_PATH"])
    two = connections.create_supplier(conn, "Второй", NOW, "owner")
    html = client.get("/").get_data(as_text=True)
    assert "Поставщик «Второй»" in html and "нет файла выгрузки" in html
    connections.set_config(conn, "webmim", two, {"base_url": "https://b.example", "mode": "apikey", "territory_rn": "T9"}, NOW, "owner")
    (tmp_path / "unload-2.xml").write_bytes(TWO.replace(b"2002", b"T9"))
    (tmp_path / "unload-2.xml.meta.json").write_text(json.dumps({"fetched_at": "2026-10-14T09:00:00", "items": 1}))
    from tires_sync.panel import service
    from tires_sync.panel.data import load_merged, load_reference
    from tires_sync.settings_store import active_settings
    settings = active_settings(conn)
    found = service.readiness(conn, app.config["UNLOAD_PATH"], load_merged(conn, settings, app.config["UNLOAD_PATH"]),
                              load_reference(app.config["REFERENCE_PATH"]), settings, now=Clock.utcnow())
    step = next(s for s in found if s["title"] == "Поставщик «Второй»")
    assert step["level"] == "ok" and "скачана 14.10.2026 12:00:00" in step["detail"]              # UTC shown in Moscow time (run6c)
    assert "Поставщик «Второй»" not in client.get("/").get_data(as_text=True)          # a ready step is not listed on the home page (2026-10-21)


def test_two_alerts_within_ten_minutes_send_one_message(tmp_path, monkeypatch):
    from tires_sync.secretbox import create_key_file, load_box
    conn = store.connect(tmp_path / "t.db")
    key_file = tmp_path / "secret.key"
    create_key_file(key_file)
    box = load_box(key_file)
    connections.set_config(conn, "telegram", 0, {"chat_id": "42"}, NOW, "owner")
    connections.set_secret(conn, box, "telegram", 0, "bot_token", "123456789:AAH-abcdefghijklmnopqrstuvwxyz012345", NOW, "owner")
    conn.close()
    http = ScriptedHttp((200, {"ok": True}), (200, {"ok": True}))
    monkeypatch.setattr(cli, "_http", lambda: http)
    base = ["alert", "--db", str(tmp_path / "t.db"), "--key-file", str(key_file)]
    main(base + ["--message", "первое"])
    main(base + ["--message", "второе"])
    assert len(http.calls) == 1


# --- I3: the panel publish needs the first supplier's unload ----------------------------------------------------

def test_panel_publish_refuses_when_the_first_suppliers_unload_is_unreadable(tmp_path):
    from tires_sync import publishing, staging
    from tires_sync.settings_store import confirm_draft, create_draft
    app = make_app(tmp_path, FEED_ROOT=str(tmp_path / "feed"))
    client = app.test_client()
    tok = login(client)
    conn = store.connect(app.config["DB_PATH"])
    connections.create_account(conn, "A", NOW, "owner")
    connections.set_live(conn, 1, True, NOW, "owner")
    two = connections.create_supplier(conn, "Второй", NOW, "owner")
    connections.set_config(conn, "webmim", two, {"base_url": "https://b.example", "mode": "apikey", "territory_rn": "T9"}, NOW, "owner")
    (tmp_path / "unload-2.xml").write_bytes(catalog_xml(commodity_xml(NNOMMODIF="5", TERRITORY_RN="T9")))
    (tmp_path / "unload.xml").write_bytes(b"<CATALOG><broken")
    staging.set_override(conn, "5", "off", NOW)
    d = staging.summary(conn)["draft_id"]
    post(client, "/draft/%d/confirm" % d, tok, publish="1")
    html = client.get("/publish/result").get_data(as_text=True)
    assert "первого поставщика" in html
    assert not (tmp_path / "feed").exists() or not list((tmp_path / "feed").rglob("avito_feed.xml"))


def test_a_secondary_suppliers_file_is_never_stale_within_a_day_even_if_the_wait_before_removal_is_zero():
    from tires_sync.suppliers import file_is_stale
    now = datetime(2026, 10, 9, 12, 0, 0)
    assert not file_is_stale(now - timedelta(hours=5), now, 0)
    assert not file_is_stale(now - timedelta(hours=5), now, 3)
    assert file_is_stale(now - timedelta(days=2), now, 0)
    assert file_is_stale(now - timedelta(days=4), now, 3) and not file_is_stale(now - timedelta(days=2), now, 3)
