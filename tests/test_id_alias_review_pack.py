from datetime import date
from helpers import catalog_xml, commodity_xml, settings_dict
from test_feed_account_rules import NOW, REF
from tires_sync import connections, cycle, mirror_feed, store, chat_deals, publishing
from tires_sync.settings_store import init_settings
import xml.etree.ElementTree as ET

UNLOAD = catalog_xml(commodity_xml(NNOMMODIF="100", SMNFCODE="TS1", NREST="20"), commodity_xml(NNOMMODIF="101", SMNFCODE="TS2", NREST="20"))


def test_an_aliased_account_publishes_packs_as_alias_and_minus_x4(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(pack4=True, pack4_rules=["*|*|*"]), NOW)
    first = connections.create_account(conn, "Основной", NOW, "o")
    second = connections.create_account(conn, "Партнёр-4", NOW, "o")
    for n in (first, second):
        connections.save_addresses(conn, n, "percent", None, [{"name": "Т", "address_id": "10%d" % n, "enabled": True, "share": 100, "address_text": ""}], NOW, "o")
        connections.set_live(conn, n, True, NOW, "o")
    store.set_state(conn, "public_base", "https://panel.example")
    conn.commit()
    mirror_feed.set_alias_scheme(conn, second, True, NOW, "t")
    res = cycle.run_for_accounts(conn, UNLOAD, REF, date(2026, 10, 14), tmp_path / "feed")
    assert all(r.ok for r in res), [r.error for r in res]                                      # the check before publication passes with packs too
    token = connections.get_account(conn, second)["feed_token"]
    got = [a.findtext("Id") for a in ET.parse(tmp_path / "feed" / token / "next" / "avito_feed.xml").getroot()]
    singles = [i for i in got if not i.endswith("-x4")]
    assert len(singles) == 2 and sorted(i + "-x4" for i in singles) == sorted(i for i in got if i.endswith("-x4"))


def test_the_retired_id_is_the_one_the_ad_has_in_the_feed(tmp_path):
    from test_id_alias import two_accounts, ids as ids2
    conn, first, second = two_accounts(tmp_path)
    for n in (first, second):
        connections.set_live(conn, n, True, NOW, "o")
    conn.commit()
    one = catalog_xml(commodity_xml(NNOMMODIF="100", SMNFCODE="TS1"), commodity_xml(NNOMMODIF="101", SMNFCODE="TS2"))
    cycle.run_for_accounts(conn, one, REF, date(2026, 10, 14), tmp_path / "feed"); publishing.publish_live_accounts(conn, tmp_path / "feed")
    mirror_feed.reissue_own(conn, second, ["100"], "20261015", NOW, "t")
    cycle.run_for_accounts(conn, one, REF, date(2026, 10, 15), tmp_path / "feed"); publishing.publish_live_accounts(conn, tmp_path / "feed")
    live = ids2(tmp_path, conn, second)
    assert any(i.endswith("_r20261015") for i in live)
    gone = catalog_xml(commodity_xml(NNOMMODIF="101", SMNFCODE="TS2"))
    cycle.run_for_accounts(conn, gone, REF, date(2026, 10, 16), tmp_path / "feed"); publishing.publish_live_accounts(conn, tmp_path / "feed")
    rows = list(conn.execute("select goods_id, ad_id from retired_ad where account_id = ?", (second,)))
    assert rows == [("100", [i for i in live if i.startswith("100_")][0])]                     # the Id the ad really had, not the one of an old snapshot


def test_the_portion_limit_holds_in_an_aliased_account(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(portion_limit=1), NOW)
    first = connections.create_account(conn, "Основной", NOW, "o")
    second = connections.create_account(conn, "Партнёр-4", NOW, "o")
    for n in (first, second):
        connections.save_addresses(conn, n, "percent", None, [{"name": "Т", "address_id": "10%d" % n, "enabled": True, "share": 100, "address_text": ""}], NOW, "o")
        connections.set_live(conn, n, True, NOW, "o")
    store.set_state(conn, "public_base", "https://panel.example")
    conn.commit()
    mirror_feed.set_alias_scheme(conn, second, True, NOW, "t")
    many = catalog_xml(*[commodity_xml(NNOMMODIF=str(100 + i), SMNFCODE="TS%d" % i, SWIDTH=str(175 + 10 * i)) for i in range(5)])
    for day, want in ((14, 1), (15, 2)):
        cycle.run_for_accounts(conn, many, REF, date(2026, 10, day), tmp_path / "feed")
        publishing.publish_live_accounts(conn, tmp_path / "feed")
        for acc in (first, second):
            token = connections.get_account(conn, acc)["feed_token"]
            assert len(ET.parse(tmp_path / "feed" / token / "next" / "avito_feed.xml").getroot()) == want   # the portion limit works with aliases too
