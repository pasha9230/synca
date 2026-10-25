"""An account that carried another owner's file is moved to our own assembly without losing its ads: his Ids and his photo links stay."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date

from helpers import catalog_xml, commodity_xml, settings_dict
from tires_sync import connections, cycle, mirror_feed, store
from tires_sync.catalog import parse_reference
from tires_sync.cli import main
from tires_sync.settings_store import init_settings

NOW = "2026-10-14T12:00:00"
REF = parse_reference('<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>')
UNLOAD = catalog_xml(commodity_xml(NNOMMODIF="100"), commodity_xml(NNOMMODIF="101", SMNFCODE="TS2"), commodity_xml(NNOMMODIF="200", SMNFCODE="TS3"))


def setup(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(), NOW)
    connections.create_account(conn, "Основной", NOW, "o")
    account = connections.create_account(conn, "Партнёр-2", NOW, "o")
    file = ('<Ads><Ad><Id>100_20260815153645</Id><Title>T</Title><Price>1</Price><Images><Image url="https://old.example/100.jpg"/></Images></Ad>'
            '<Ad><Id>101_20260716165744</Id><Title>T</Title><Price>1</Price></Ad></Ads>').encode()
    mirror_feed.import_feed(conn, account, file, NOW)
    connections.set_avito_identity(conn, account, 2, "Партнёр-2", "79000000012")
    connections.set_phone_source(conn, account, "file", NOW, "o")
    return conn, account


def feed(tmp_path, conn, account):
    token = connections.get_account(conn, account)["feed_token"]
    return {a.findtext("Id"): a for a in ET.parse(tmp_path / "feed" / token / "next" / "avito_feed.xml").getroot()}


def test_the_plan_lists_what_will_be_kept(tmp_path):
    conn, account = setup(tmp_path)
    plan = mirror_feed.adopt_plan(conn, account)
    assert plan["ids"] == {"100": "100_20260815153645", "101": "101_20260716165744"}
    assert plan["images"] == {"100": "https://old.example/100.jpg"}


def test_after_the_move_his_ids_and_photos_stay_the_new_positions_get_our_own(tmp_path):
    conn, account = setup(tmp_path)
    store.set_state(conn, "public_base", "https://panel.example")
    mirror_feed.adopt(conn, account, NOW, "o")
    assert connections.get_account(conn, account)["feed_mode"] == "generated"
    results = cycle.run_for_accounts(conn, UNLOAD, REF, date(2026, 10, 14), tmp_path / "feed")
    assert [r.ok for r in results] == [True, True], [r.error for r in results]
    ads = feed(tmp_path, conn, account)
    assert set(ads) == {"100_20260815153645", "101_20260716165744", "200_a%d" % account}
    assert ads["100_20260815153645"].find("Images/Image").get("url") == "https://old.example/100.jpg"        # his link
    token = connections.get_account(conn, account)["feed_token"]
    assert ads["101_20260716165744"].find("Images/Image").get("url") == "https://panel.example/img/%s/101.jpg" % token   # none in his file: ours
    assert {a.findtext("ContactPhone") for a in ads.values()} == {"+79000000012"}                            # the cabinet's phone, never the general number


def test_a_second_run_changes_nothing_and_the_cli_shows_the_plan_first(tmp_path, capsys):
    conn, account = setup(tmp_path)
    conn.close()
    base = ["mirror-adopt", "--db", str(tmp_path / "t.db"), "--account", str(account)]
    assert main(base) == 0
    assert "Ничего не изменено" in capsys.readouterr().out
    assert connections.get_account(store.connect(tmp_path / "t.db"), account)["feed_mode"] == "mirror"
    assert main(base + ["--confirm"]) == 0
    assert connections.get_account(store.connect(tmp_path / "t.db"), account)["feed_mode"] == "generated"
    assert main(base + ["--confirm"]) != 0                                                                    # already moved


def test_the_ads_of_a_moved_account_are_never_held_back_by_the_limit_of_new_positions(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(), NOW)
    connections.create_account(conn, "Основной", NOW, "o")
    account = connections.create_account(conn, "Партнёр-2", NOW, "o")
    codes = list(range(1000, 1130))                                                    # more than the portion limit of 100
    file = ("<Ads>%s</Ads>" % "".join("<Ad><Id>%d_20260801</Id><Title>T</Title><Price>1</Price></Ad>" % n for n in codes)).encode()
    mirror_feed.import_feed(conn, account, file, NOW)
    connections.set_avito_identity(conn, account, 2, "Партнёр-2", "79000000012")
    mirror_feed.adopt(conn, account, NOW, "o")
    unload = catalog_xml(*[commodity_xml(NNOMMODIF=str(n), SMNFCODE="C%d" % n, NREST="12", SWIDTH=str(100 + n - 1000)) for n in codes])      # different tires
    results = cycle.run_for_accounts(conn, unload, REF, date(2026, 10, 14), tmp_path / "feed")
    second = [r for r in results if r.account_id == account][0]
    assert second.ok and second.feed_count == 130, second
    assert {a for a in feed(tmp_path, conn, account)} == {"%d_20260801" % n for n in codes}                 # all of them keep their Id and stay in the feed


def test_a_position_is_bound_to_the_id_of_the_ad_the_cabinet_already_has(tmp_path):
    conn, account = setup(tmp_path)
    cabinet = {"100": ["100_20260721151156", "100_20260817184409"], "101": ["101_20260716165744"], "300": ["300_x"]}
    changed = mirror_feed.rebind_ids(conn, account, ["100_20260815153645", "101_20260716165744", "100-x4", "999"], cabinet, NOW, "o")
    assert changed == {"100": "100_20260721151156"}                                       # 101 is already right, the set of four and an unknown code are left alone
    ids, _images = mirror_feed.adopted_maps(conn, account)
    assert ids["100"] == "100_20260721151156"
    assert mirror_feed.rebind_ids(conn, account, ["100_20260721151156"], cabinet, NOW, "o") == {}          # nothing more to change


def test_the_rebound_id_goes_into_the_feed(tmp_path):
    conn, account = setup(tmp_path)
    mirror_feed.adopt(conn, account, NOW, "o")
    mirror_feed.rebind_ids(conn, account, ["100_20260815153645"], {"100": ["100_20260721151156"]}, NOW, "o")
    cycle.run_for_accounts(conn, UNLOAD, REF, date(2026, 10, 14), tmp_path / "feed")
    assert set(feed(tmp_path, conn, account)) >= {"100_20260721151156", "101_20260716165744"}
