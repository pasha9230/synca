"""An ad deleted in the Avito cabinet cannot come back under its Id («to upload it as new, change the Id»): the owner can give such ads new Ids."""
from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from helpers import settings_dict
from tires_sync import connections, mirror_feed, store
from tires_sync.cli import main
from tires_sync.settings_store import init_settings

NOW = "2026-10-14T12:00:00"


def ad(ad_id):
    return '<Ad><Id>%s</Id><Title>Шина %s</Title><Price>5000</Price></Ad>' % (ad_id, ad_id)


def setup(tmp_path, *ids):
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(), NOW)
    account = connections.create_account(conn, "Партнёр", NOW, "o")
    mirror_feed.import_feed(conn, account, ('<Ads>%s</Ads>' % "".join(ad(i) for i in ids)).encode(), NOW)
    return conn, account


def test_the_ads_get_new_ids_everything_else_stays_and_the_supplier_code_survives(tmp_path):
    conn, account = setup(tmp_path, "100_20260716", "101", "102_20260801")
    done = mirror_feed.reissue_ids(conn, account, ["100_20260716", "101"], "20261014", NOW, "o")
    assert done == {"100_20260716": "100_20260716_r20261014", "101": "101_r20261014"}
    assert mirror_feed.stored_ids(conn, account) == ["100_20260716_r20261014", "101_r20261014", "102_20260801"]      # the order is kept
    raw = {i: ET.fromstring(r) for i, r in conn.execute("SELECT ad_id, raw FROM imported_ad WHERE account_id = ?", (account,))}
    assert raw["101_r20261014"].findtext("Id") == "101_r20261014" and raw["101_r20261014"].findtext("Title") == "Шина 101"
    assert {mirror_feed.supplier_code(i) for i in raw} == {"100", "101", "102"}                    # price, stock and models still find their position
    assert "mirror_reissue" in [r[0] for r in conn.execute("SELECT action FROM audit")]


def test_an_unknown_id_or_a_second_run_changes_nothing_and_says_so(tmp_path):
    conn, account = setup(tmp_path, "100", "101")
    with pytest.raises(ValueError, match="нет в файле"):
        mirror_feed.reissue_ids(conn, account, ["100", "999"], "20261014", NOW, "o")
    assert mirror_feed.stored_ids(conn, account) == ["100", "101"]                                    # all or nothing
    mirror_feed.reissue_ids(conn, account, ["100"], "20261014", NOW, "o")
    with pytest.raises(ValueError):
        mirror_feed.reissue_ids(conn, account, ["100_r20261014"], "20261014", NOW, "o")                 # the same stamp twice would collide
    assert mirror_feed.stored_ids(conn, account) == ["100_r20261014", "101"]


def test_the_command_shows_what_it_would_do_and_changes_only_with_confirm(tmp_path, capsys):
    conn, account = setup(tmp_path, "100", "101")
    conn.close()
    base = ["mirror-reissue", "--db", str(tmp_path / "t.db"), "--account", str(account), "--ids", "100,101", "--stamp", "20261014"]
    assert main(base) == 0
    assert "Ничего не изменено" in capsys.readouterr().out
    assert mirror_feed.stored_ids(store.connect(tmp_path / "t.db"), account) == ["100", "101"]
    assert main(base + ["--confirm"]) == 0
    assert mirror_feed.stored_ids(store.connect(tmp_path / "t.db"), account) == ["100_r20261014", "101_r20261014"]


def test_a_position_made_by_our_own_rules_can_be_reissued_too_and_stays_tracked_by_its_code(tmp_path):
    from datetime import date
    from helpers import catalog_xml, commodity_xml
    from tires_sync import cycle, publishing
    from tires_sync.catalog import parse_reference
    ref = parse_reference('<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>')
    conn, account = setup(tmp_path, "100_20260801")
    unload = catalog_xml(commodity_xml(NNOMMODIF="100"), commodity_xml(NNOMMODIF="200", SMNFCODE="NEW2"))
    path = tmp_path / "feed" / connections.get_account(conn, account)["feed_token"] / "next" / "avito_feed.xml"
    cycle.run_for_accounts(conn, unload, ref, date(2026, 10, 14), tmp_path / "feed")
    assert {a.findtext("Id") for a in ET.parse(path).getroot()} == {"100_20260801", "200"}
    assert mirror_feed.reissue_own(conn, account, ["200"], "20261014", NOW, "o") == {"200": "200_r20261014"}
    cycle.run_for_accounts(conn, unload, ref, date(2026, 10, 14), tmp_path / "feed")
    assert {a.findtext("Id") for a in ET.parse(path).getroot()} == {"100_20260801", "200_r20261014"}
    connections.set_live(conn, account, True, NOW, "o")
    conn.commit()
    assert publishing.publish_live_accounts(conn, tmp_path / "feed")[0]["ok"]
    assert store.get_snapshot(conn, "200", account) is not None                                        # tracked by the supplier code, not by the new Id
    cycle.run_for_accounts(conn, unload, ref, date(2026, 10, 15), tmp_path / "feed")
    assert {a.findtext("Id") for a in ET.parse(path).getroot()} == {"100_20260801", "200_r20261014"}   # and it stays
    with pytest.raises(ValueError):
        mirror_feed.reissue_own(conn, account, ["200"], "20261014", NOW, "o")                          # the same stamp twice


def test_a_reissued_ad_keeps_its_position_so_it_is_never_made_a_second_time(tmp_path):
    from datetime import date
    from helpers import catalog_xml, commodity_xml
    from tires_sync import cycle
    from tires_sync.catalog import parse_reference
    ref = parse_reference('<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>')
    conn, account = setup(tmp_path, "100_20260801")
    mirror_feed.reissue_ids(conn, account, ["100_20260801"], "20261014", NOW, "o")
    cycle.run_for_accounts(conn, catalog_xml(commodity_xml(NNOMMODIF="100")), ref, date(2026, 10, 14), tmp_path / "feed")
    path = tmp_path / "feed" / connections.get_account(conn, account)["feed_token"] / "next" / "avito_feed.xml"
    ids = [a.findtext("Id") for a in ET.parse(path).getroot()]
    assert ids == ["100_20260801_r20261014"]                                    # not also «100» as a new position: one ad per position


def test_positions_repeated_in_the_file_are_reported_and_never_dropped(tmp_path):
    conn, account = setup(tmp_path, "100_20260716", "100_20260801", "200_20260801", "300-x4_20260801", "300_20260801")
    assert mirror_feed.duplicate_positions(conn, account) == [("100", "x1")]       # a set of four and a single tire of one code are different ads
    assert mirror_feed.stored_ids(conn, account) == ["100_20260716", "100_20260801", "200_20260801", "300-x4_20260801", "300_20260801"]
