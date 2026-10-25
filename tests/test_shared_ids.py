"""An Id the cabinet already uses (the same as another account's) may be shared until the old ad's term ends; then the position gets its own Id."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date

from helpers import catalog_xml, commodity_xml, settings_dict
from tires_sync import connections, cycle, mirror_feed, store
from tires_sync.catalog import parse_reference
from tires_sync.settings_store import init_settings

NOW = "2026-10-14T12:00:00"
REF = parse_reference('<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>')
UNLOAD = catalog_xml(commodity_xml(NNOMMODIF="100"), commodity_xml(NNOMMODIF="101", SMNFCODE="TS2"))


def two(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(), NOW)
    first = connections.create_account(conn, "Основной", NOW, "o")
    second = connections.create_account(conn, "Второй", NOW, "o")
    for n in (first, second):
        connections.save_addresses(conn, n, "percent", None, [{"name": "Т", "address_id": "10%d" % n, "enabled": True, "share": 100, "address_text": ""}], NOW, "o")
    return conn, first, second


def ids(tmp_path, conn, account):
    token = connections.get_account(conn, account)["feed_token"]
    return {a.findtext("Id") for a in ET.parse(tmp_path / "feed" / token / "next" / "avito_feed.xml").getroot()}


def test_a_shared_id_is_used_until_the_date_and_then_the_position_gets_its_own(tmp_path):
    conn, first, second = two(tmp_path)
    cabinet = {"100": ["100"]}                                                     # the cabinet of the second account has a live ad with the plain Id
    done = mirror_feed.rebind_ids(conn, second, ["100_a%d" % second], cabinet, NOW, "o", shared={"100": "2026-11-14"}, taken={"100"})
    assert done == {"100": "100"}
    results = cycle.run_for_accounts(conn, UNLOAD, REF, date(2026, 10, 14), tmp_path / "feed")
    assert [r.ok for r in results] == [True, True], [r.error for r in results]      # the shared Id is not a violation of the cross-account rule
    assert "100" in ids(tmp_path, conn, first) and "100" in ids(tmp_path, conn, second)
    assert mirror_feed.adopted_maps(conn, second, today=date(2026, 11, 13))[0] == {"100": "100"}
    assert mirror_feed.adopted_maps(conn, second, today=date(2026, 11, 14))[0] == {}   # the term is over: its own Id again


def test_without_the_exception_a_colliding_id_is_not_bound(tmp_path):
    conn, first, second = two(tmp_path)
    done = mirror_feed.rebind_ids(conn, second, ["100_a%d" % second], {"100": ["100"]}, NOW, "o", taken={"100"})
    assert done == {}


def test_the_cli_binds_to_the_existing_ids_and_shares_an_id_only_when_asked_to(tmp_path, monkeypatch, capsys):
    from tires_sync import cli
    conn, first, second = two(tmp_path)
    cycle.run_for_accounts(conn, UNLOAD, REF, date(2026, 10, 14), tmp_path / "feed")
    from tires_sync import publishing
    for n in (first, second):
        connections.set_live(conn, n, True, NOW, "o")
    conn.commit()
    publishing.publish_live_accounts(conn, tmp_path / "feed")

    class Client:
        def items(self, status):
            return iter([{"id": 1}, {"id": 2}])

        def ad_ids_for(self, avito_ids):
            return {1: "100", 2: "101_20260716"}                  # the cabinet has «100» (the same as the first account) and «101_20260716»
    monkeypatch.setattr(cli, "_avito_client", lambda args, write: (conn, connections.get_account(conn, second), Client()))
    base = ["avito-ids-sync", "--db", str(tmp_path / "t.db"), "--account", str(second), "--feed-root", str(tmp_path / "feed")]
    assert cli.main(base + ["--confirm"]) == 0
    out = capsys.readouterr().out
    assert "привязано позиций к существующим Id: 1" in out and "не привязано из-за такого же Id у другого аккаунта: 1" in out
    assert mirror_feed.adopted_maps(conn, second)[0] == {"101": "101_20260716"}
    assert cli.main(base + ["--allow-shared-until", "2026-11-14", "--confirm"]) == 0
    assert mirror_feed.adopted_maps(conn, second, today=date(2026, 10, 20))[0] == {"101": "101_20260716", "100": "100"}
    assert mirror_feed.shared_ids(conn, second, today=date(2026, 10, 20)) == {"100"}
