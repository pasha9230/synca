from __future__ import annotations

from datetime import date

import pytest

from helpers import catalog_xml, commodity_xml, settings_dict
from http_fakes import ScriptedHttp
from tires_sync import cli, connections, cycle, store, suppliers
from tires_sync.catalog import parse_reference
from tires_sync.cli import main
from tires_sync.secretbox import create_key_file, load_box
from tires_sync.settings_store import init_settings

NOW = "2026-10-14T10:00:00"
REF = parse_reference('<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>')
TODAY = date(2026, 10, 14)


def prepared(tmp_path, with_second=True):
    conn = store.connect(tmp_path / "t.db")
    both = {"enabled": [["IKON TYRES", "winter"], ["S2:IKON TYRES", "winter"]],
            "pricing": {"IKON TYRES|winter": {"pct_small": -3.0, "pct_large": -3.0},
                        "S2:IKON TYRES|winter": {"pct_small": 5.0, "pct_large": 5.0}}}
    init_settings(conn, settings_dict(territory_rn="1001", **both), NOW)
    connections.create_account(conn, "A", NOW, "owner")
    connections.create_account(conn, "B", NOW, "owner")
    if with_second:
        two = connections.create_supplier(conn, "Второй", NOW, "owner")
        connections.set_config(conn, "webmim", connections.supplier_owner(two),
                               {"base_url": "https://b.example", "mode": "apikey", "territory_rn": "2002"}, NOW, "owner")
    return conn


def files(tmp_path, first, second=None):
    (tmp_path / "unload.xml").write_bytes(first)
    if second is not None:
        (tmp_path / "unload-2.xml").write_bytes(second)
    (tmp_path / "reference.txt").write_text('<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>', encoding="utf-8")
    return ["--db", str(tmp_path / "t.db"), "--unload", str(tmp_path / "unload.xml"),
            "--reference", str(tmp_path / "reference.txt"), "--feed-root", str(tmp_path / "feed")]


ONE = catalog_xml(commodity_xml(NNOMMODIF="5", TERRITORY_RN="1001", NPRICE_DELAYED="5000"))
TWO = catalog_xml(commodity_xml(NNOMMODIF="5", TERRITORY_RN="2002", NPRICE_DELAYED="5000", SLOAD="100"))      # another lot, another load index: not the same tire as ONE


def feed_text(tmp_path, conn, account_id):
    token = connections.get_account(conn, account_id)["feed_token"]
    return (tmp_path / "feed" / token / "next" / "avito_feed.xml").read_text(encoding="utf-8")


def test_unload_paths_per_supplier(tmp_path):
    base = tmp_path / "unload.xml"
    assert suppliers.unload_path_for(base, 1) == base
    assert suppliers.unload_path_for(base, 2) == tmp_path / "unload-2.xml"


def test_every_account_feed_contains_the_positions_of_every_supplier_with_their_own_prices(tmp_path):
    conn = prepared(tmp_path)
    conn.close()
    assert main(["run-cycle"] + files(tmp_path, ONE, TWO)) == 0
    conn = store.connect(tmp_path / "t.db")
    for account in (1, 2):
        text = feed_text(tmp_path, conn, account)
        suffix = "" if account == 1 else "_a%d" % account                       # the first account keeps the plain Ids, the others have their own
        assert "<Id>5%s</Id>" % suffix in text and "<Id>s2-5%s</Id>" % suffix in text
        assert "<Price>4850</Price>" in text and "<Price>5250</Price>" in text   # -3 % vs +5 %, each supplier's own percent


def test_a_missing_file_of_a_secondary_supplier_does_not_stop_the_first_one(tmp_path):
    conn = prepared(tmp_path)
    conn.close()
    assert main(["run-cycle"] + files(tmp_path, ONE)) == 3      # reported, but the first supplier still builds
    conn = store.connect(tmp_path / "t.db")
    text = feed_text(tmp_path, conn, 1)
    assert "<Id>5</Id>" in text and "s2-" not in text


def test_a_secondary_supplier_without_a_territory_is_skipped_with_a_note(tmp_path, capsys):
    conn = prepared(tmp_path, with_second=False)
    sid = connections.create_supplier(conn, "Без территории", NOW, "owner")
    connections.set_config(conn, "webmim", sid, {"base_url": "https://b.example", "mode": "apikey"}, NOW, "owner")
    conn.close()
    assert main(["run-cycle"] + files(tmp_path, ONE, TWO)) == 3
    assert "территория" in capsys.readouterr().out
    conn = store.connect(tmp_path / "t.db")
    assert "s2-" not in feed_text(tmp_path, conn, 1)


def test_a_secondary_supplier_with_an_empty_address_is_switched_off_without_a_note_or_a_failed_cycle(tmp_path, capsys):
    conn = prepared(tmp_path, with_second=False)
    sid = connections.create_supplier(conn, "Выключенный", NOW, "owner")
    connections.set_config(conn, "webmim", sid, {"base_url": "", "mode": "apikey"}, NOW, "owner")
    conn.close()
    assert main(["run-cycle"] + files(tmp_path, ONE, TWO)) == 0
    out = capsys.readouterr()
    assert "не используется" not in out.out and "error" not in out.err


def test_same_article_at_two_suppliers_keeps_two_separate_published_items(tmp_path):
    conn = prepared(tmp_path)
    connections.set_live(conn, 1, True, NOW, "owner")
    conn.close()
    assert main(["run-cycle", "--publish"] + files(tmp_path, ONE, TWO)) == 0
    conn = store.connect(tmp_path / "t.db")
    assert set(store.load_published(conn, account_id=1)) == {"5", "s2-5"}
    assert store.load_published(conn, account_id=2) == {}      # account B is not live


def test_a_failed_fetch_of_a_secondary_supplier_alerts_and_keeps_its_previous_positions(tmp_path, monkeypatch, capsys):
    conn = prepared(tmp_path)
    key_file = tmp_path / "secret.key"
    create_key_file(key_file)
    box = load_box(key_file)
    connections.set_config(conn, "webmim", 0, {"base_url": "https://a.example", "mode": "apikey", "login": ""}, NOW, "owner")
    connections.set_secret(conn, box, "webmim", 0, "api_key", "K" * 32, NOW, "owner")
    connections.set_secret(conn, box, "webmim", 2, "api_key", "L" * 32, NOW, "owner")
    conn.close()
    args = files(tmp_path, ONE, TWO)

    class Http:
        def __init__(self):
            self.calls = []

        def __call__(self, method, url, headers, body):
            self.calls.append(url)
            if "b.example" in url:
                return 401, {}, b"no"
            return 200, {}, ONE

    http = Http()
    monkeypatch.setattr(cli, "_http", lambda: http)
    code = main(["run-cycle", "--fetch", "--key-file", str(key_file)] + args)
    out = capsys.readouterr()
    conn = store.connect(tmp_path / "t.db")
    assert code == 3 and "Второй" in out.err
    text = feed_text(tmp_path, conn, 1)
    assert "<Id>5</Id>" in text and "<Id>s2-5</Id>" in text     # the old file of supplier 2 was kept


def test_mappings_pending_with_unload_lists_every_suppliers_pairs(tmp_path, capsys):
    conn = prepared(tmp_path)
    args = files(tmp_path, catalog_xml(commodity_xml(NNOMMODIF="5", TERRITORY_RN="1001", SMODEL="First Odd")),
                 catalog_xml(commodity_xml(NNOMMODIF="5", TERRITORY_RN="2002", SMODEL="Second Odd")))
    conn.close()
    assert main(["mappings", "pending", "--db", args[1], "--unload", args[3], "--reference", args[5]]) == 0
    out = capsys.readouterr().out
    assert "First Odd" in out and "Second Odd" in out


def test_a_cycle_keeps_the_positions_the_supplier_took_away_in_the_catalog_base_and_the_feed_does_not_get_them(tmp_path):
    from tires_sync import catalog_archive
    conn = prepared(tmp_path, with_second=False)
    conn.close()
    both = catalog_xml(commodity_xml(NNOMMODIF="5", TERRITORY_RN="1001", NPRICE_DELAYED="5000"),
                       commodity_xml(NNOMMODIF="6", TERRITORY_RN="1001", NPRICE_DELAYED="5000", SMODIFNAME="IKON 195/65/15 T 91 Old Model", SLOAD="91"))
    assert main(["run-cycle"] + files(tmp_path, both)) in (0, 3)
    conn = store.connect(tmp_path / "t.db")
    assert catalog_archive.gone_offers(conn) == [] and conn.execute("SELECT COUNT(*) FROM catalog_item").fetchone()[0] == 2
    conn.close()
    assert main(["run-cycle"] + files(tmp_path, ONE)) in (0, 3)
    conn = store.connect(tmp_path / "t.db")
    [(offer, info)] = catalog_archive.gone_offers(conn)
    assert offer.goods_id == "6" and offer.stock == 0 and info["last_stock"] == 10
    assert "<Id>6</Id>" not in feed_text(tmp_path, conn, 1) and "<Id>5</Id>" in feed_text(tmp_path, conn, 1)         # display only: the feed never sees it
