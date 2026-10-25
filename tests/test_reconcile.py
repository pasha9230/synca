"""A live ad of the cabinet under an Id our feed does not have is the first ad of that tire: the position takes its Id."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date

from helpers import catalog_xml, commodity_xml
from test_same_tire import NOW, REF, ids, run, setup
from tires_sync import mirror_feed, reconcile


def ad(ad_id, brand="Kumho", model="WinterCraft Ice Wi32", width="235", height="55", rim="19", load="105", speed="T"):
    node = ET.Element("Ad")
    for tag, value in (("Id", ad_id), ("Brand", brand), ("Model", model), ("TireSectionWidth", width), ("TireAspectRatio", height),
                       ("RimDiameter", rim), ("LoadIndex", load), ("SpeedIndex", speed)):
        ET.SubElement(node, tag).text = value
    return node


def test_the_title_of_avito_gives_the_same_key_as_the_ad_of_the_feed():
    assert reconcile.key_title("Kumho WinterCraft Ice Wi32 235/55 R19 105T") == reconcile.key_ad(ad("1"))
    assert reconcile.key_title("Ikon Tyres CHARACTER ICE 8 SUV 235/65 R17 108T") == reconcile.key_ad(
        ad("2", brand="Ikon Tyres", model="Character Ice 8 SUV", width="235", height="65", rim="17", load="108"))
    assert reconcile.key_title("Трансфер, аренда авто, аэропорт") is None


def test_a_live_ad_outside_the_feed_is_paired_with_the_same_tire_of_the_feed():
    feed = {"113105677504_a2_r1": ad("113105677504_a2_r1"), "other": ad("other", width="195")}
    cabinet = [{"ad_id": "125452776872_20260716", "status": "active", "title": "Kumho WinterCraft Ice Wi32 235/55 R19 105T"},
               {"ad_id": "113105677504_a2_r1", "status": "blocked", "title": "Kumho WinterCraft Ice Wi32 235/55 R19 105T"}]
    assert reconcile.plan(feed, cabinet) == [
        {"orphan": "125452776872_20260716", "feed_id": "113105677504_a2_r1", "title": "Kumho WinterCraft Ice Wi32 235/55 R19 105T"}]


def test_nothing_is_paired_when_the_feed_ad_is_itself_live_or_the_tire_is_ambiguous():
    title = "Kumho WinterCraft Ice Wi32 235/55 R19 105T"
    live_feed = {"A": ad("A")}
    assert reconcile.plan(live_feed, [{"ad_id": "A", "status": "active", "title": title},
                                      {"ad_id": "B", "status": "active", "title": title}]) == []      # the feed's own ad is alive: two ads, not a repeat to merge
    two = {"A": ad("A"), "C": ad("C")}
    assert reconcile.plan(two, [{"ad_id": "B", "status": "active", "title": title}]) == []             # two candidates: left to a human


def test_a_renewal_is_taken_back_when_the_position_takes_the_live_id(tmp_path):
    conn, account = setup(tmp_path)
    run(conn, tmp_path, catalog_xml(commodity_xml(NNOMMODIF="106", NREST="8", SMNFCODE="LOT-A", SLOAD="94", SWIDTH="205"),
                                    commodity_xml(NNOMMODIF="200", NREST="9", SMNFCODE="O", SLOAD="91", SWIDTH="195")))
    mirror_feed.reissue_own(conn, account, ["106"], "20261015", NOW, "t")
    run(conn, tmp_path, catalog_xml(commodity_xml(NNOMMODIF="106", NREST="8", SMNFCODE="LOT-A", SLOAD="94", SWIDTH="205"),
                                    commodity_xml(NNOMMODIF="200", NREST="9", SMNFCODE="O", SLOAD="91", SWIDTH="195")), day=15)
    assert "106_r20261015" in ids(tmp_path, conn, account)
    mirror_feed.bind_ids(conn, account, {"106": "106_20260716165744"}, NOW, "t")
    assert mirror_feed.unreissue_own(conn, account, ["106"], NOW, "t") == ["106"]
    run(conn, tmp_path, catalog_xml(commodity_xml(NNOMMODIF="106", NREST="8", SMNFCODE="LOT-A", SLOAD="94", SWIDTH="205"),
                                    commodity_xml(NNOMMODIF="200", NREST="9", SMNFCODE="O", SLOAD="91", SWIDTH="195")), day=16)
    assert "106_20260716165744" in ids(tmp_path, conn, account) and not any("_r2026" in i for i in ids(tmp_path, conn, account))


def test_sets_of_four_never_take_part_and_a_tire_with_two_feed_ads_is_left_to_a_human():
    title = "Kumho WinterCraft Ice Wi32 235/55 R19 105T"
    feed = {"B_a4": ad("B_a4"), "B-x4_a4": ad("B-x4_a4")}
    cabinet = [{"ad_id": "A-x4_a4", "status": "active", "title": title}]
    assert reconcile.plan(feed, cabinet) == []                                                  # a live set of four is not a single tire
    plain = {"B": ad("B")}
    assert reconcile.plan(plain, [{"ad_id": "A", "status": "active", "title": title}]) != []
    runflat = {"B": ad("B"), "B-rf": ad("B-rf")}                                                 # two ads with the same title key: RunFlat or not, studded or not
    assert reconcile.plan(runflat, [{"ad_id": "A", "status": "active", "title": title}]) == []
    two_live = [{"ad_id": "A1", "status": "active", "title": title}, {"ad_id": "A2", "status": "active", "title": title}]
    assert reconcile.plan(plain, two_live) == []                                                # two live ads of the tire: which one is the first is not known


def test_an_id_another_account_has_is_never_taken():
    title = "Kumho WinterCraft Ice Wi32 235/55 R19 105T"
    assert reconcile.plan({"B": ad("B")}, [{"ad_id": "A", "status": "active", "title": title}], foreign_ids={"A"}) == []
