"""Owner, 2026-10-19: an ad made or switched on by hand in a cabinet is never taken off by us, and the feed does not put the same tire next to it."""
from __future__ import annotations

import xml.etree.ElementTree as ET

from helpers import catalog_xml, commodity_xml
from test_same_tire import ids, run, setup
from tires_sync import connections, mirror_feed, store

UNLOAD = catalog_xml(commodity_xml(NNOMMODIF="106", NREST="8", SMNFCODE="LOT-A", SLOAD="94", SWIDTH="205"),
                     commodity_xml(NNOMMODIF="200", NREST="9", SMNFCODE="O", SLOAD="91", SWIDTH="195"))


def title_of(tmp_path, conn, account, width):
    token = connections.get_account(conn, account)["feed_token"]
    ad = [a for a in ET.parse(tmp_path / "feed" / token / "avito_feed.xml").getroot().findall("Ad") if a.findtext("TireSectionWidth") == width][0]
    return "%s %s %s/%s R%s %s%s" % (ad.findtext("Brand"), ad.findtext("Model"), width, ad.findtext("TireAspectRatio"), ad.findtext("RimDiameter"),
                                     ad.findtext("LoadIndex"), ad.findtext("SpeedIndex"))


def test_a_tire_with_a_manual_ad_leaves_the_feed_of_that_account_and_comes_back_under_its_id(tmp_path):
    conn, account = setup(tmp_path)
    run(conn, tmp_path, UNLOAD)
    assert ids(tmp_path, conn, account) == ["106", "200"]
    assert mirror_feed.set_manual_ads(conn, account, [{"avito_id": 8376025244, "title": title_of(tmp_path, conn, account, "205")}])
    run(conn, tmp_path, UNLOAD, day=15)
    assert ids(tmp_path, conn, account) == ["200"]                                              # the manual ad sells this tire
    reasons = [r[0] for r in conn.execute("SELECT reason FROM ad_event WHERE account_id = ? AND goods_id = '106' AND event = 'removed'", (account,))]
    assert reasons == ["manual_ad"]
    assert "106" not in store.load_published(conn, account)
    assert not mirror_feed.set_manual_ads(conn, account, mirror_feed.manual_ads(conn, account))   # the same list: no change, no new build
    mirror_feed.set_manual_ads(conn, account, [])                                                # sold or taken off by hand
    run(conn, tmp_path, UNLOAD, day=16)
    assert ids(tmp_path, conn, account) == ["106", "200"]                                        # back under the same Id


def test_a_manual_ad_of_another_tire_or_an_unreadable_title_changes_nothing(tmp_path):
    conn, account = setup(tmp_path)
    run(conn, tmp_path, UNLOAD)
    mirror_feed.set_manual_ads(conn, account, [{"avito_id": 1, "title": "Nokian Hakkapeliitta 9 205/55 R16 94T"}, {"avito_id": 2, "title": "шины б/у"}])
    run(conn, tmp_path, UNLOAD, day=15)
    assert ids(tmp_path, conn, account) == ["106", "200"]
    assert set(mirror_feed.manual_keys(conn, account)) == {("NOKIANHAKKAPELIITTA9", "205", "55", "16", "94", "T")}


def test_a_manual_ad_in_one_account_does_not_touch_another(tmp_path):
    conn, account = setup(tmp_path)
    other = connections.create_account(conn, "Второй", "2026-10-14T00:00:00", "o")
    connections.set_live(conn, other, True, "2026-10-14T00:00:00", "o")
    conn.commit()
    run(conn, tmp_path, UNLOAD)
    mirror_feed.set_manual_ads(conn, other, [{"avito_id": 5, "title": title_of(tmp_path, conn, account, "205")}])
    run(conn, tmp_path, UNLOAD, day=15)
    assert ids(tmp_path, conn, account) == ["106", "200"]
    assert ids(tmp_path, conn, other) == ["200_a%d" % other]


def test_the_set_of_four_of_the_tire_leaves_with_its_single_ad(tmp_path):
    from helpers import settings_dict
    from tires_sync import store as st
    from tires_sync.settings_store import init_settings
    conn = st.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(pack4=True, pack4_rules=["*|*|*"]), "2026-10-14T00:00:00")
    account = connections.create_account(conn, "Основной", "2026-10-14T00:00:00", "o")
    connections.set_live(conn, account, True, "2026-10-14T00:00:00", "o")
    conn.commit()
    run(conn, tmp_path, UNLOAD)
    assert ids(tmp_path, conn, account) == ["106", "106-x4", "200", "200-x4"]
    mirror_feed.set_manual_ads(conn, account, [{"avito_id": 1, "title": title_of(tmp_path, conn, account, "205")}])
    run(conn, tmp_path, UNLOAD, day=15)
    assert ids(tmp_path, conn, account) == ["200", "200-x4"]
