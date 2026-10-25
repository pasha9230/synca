"""Rules of the owner (2026-10-14): every account has its own Ids and its own photo links; titles are clean and not longer than 50 signs."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date

from helpers import catalog_xml, commodity_xml, make_offer, make_settings, settings_dict
from tires_sync import connections, cycle, funnel, store
from tires_sync.candidates import Candidate
from tires_sync.catalog import parse_reference
from tires_sync.feed import render_ad
from tires_sync.settings_store import init_settings

NOW = "2026-10-14T12:00:00"
REF = parse_reference('<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>')
UNLOAD = catalog_xml(commodity_xml(NNOMMODIF="100"), commodity_xml(NNOMMODIF="101", SMNFCODE="TS2"))


def two_accounts(tmp_path, base="https://panel.example"):
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(), NOW)
    first = connections.create_account(conn, "Основной", NOW, "o")
    second = connections.create_account(conn, "Партнёр-4", NOW, "o")
    for n in (first, second):
        connections.save_addresses(conn, n, "percent", None, [{"name": "Точка", "address_id": "10%d" % n, "enabled": True, "share": 100, "address_text": ""}], NOW, "o")
    if base:
        store.set_state(conn, "public_base", base)
    return conn, first, second


def feed(tmp_path, conn, account):
    token = connections.get_account(conn, account)["feed_token"]
    return ET.parse(tmp_path / "feed" / token / "next" / "avito_feed.xml").getroot()


def test_the_main_account_keeps_the_plain_id_and_the_others_get_their_own(tmp_path):
    conn, first, second = two_accounts(tmp_path)
    cycle.run_for_accounts(conn, UNLOAD, REF, date(2026, 10, 14), tmp_path / "feed")
    one = {a.findtext("Id") for a in feed(tmp_path, conn, first)}
    two = {a.findtext("Id") for a in feed(tmp_path, conn, second)}
    assert one == {"100", "101"} and two == {"100_a%d" % second, "101_a%d" % second}
    assert not one & two


def test_a_second_cycle_keeps_the_suffix_and_does_not_double_it(tmp_path):
    conn, first, second = two_accounts(tmp_path)
    cycle.run_for_accounts(conn, UNLOAD, REF, date(2026, 10, 14), tmp_path / "feed")
    from tires_sync import publishing
    for n in (first, second):
        connections.set_live(conn, n, True, NOW, "o")
    conn.commit()
    publishing.publish_live_accounts(conn, tmp_path / "feed")
    cycle.run_for_accounts(conn, UNLOAD, REF, date(2026, 10, 15), tmp_path / "feed")
    assert {a.findtext("Id") for a in feed(tmp_path, conn, second)} == {"100_a%d" % second, "101_a%d" % second}


def test_the_photo_link_is_the_account_s_own(tmp_path):
    conn, first, second = two_accounts(tmp_path)
    cycle.run_for_accounts(conn, UNLOAD, REF, date(2026, 10, 14), tmp_path / "feed")
    tokens = {n: connections.get_account(conn, n)["feed_token"] for n in (first, second)}
    for n in (first, second):
        urls = {a.findtext("Id").split("_")[0]: a.find("Images/Image").get("url") for a in feed(tmp_path, conn, n)}
        assert urls == {"100": "https://panel.example/img/%s/100.jpg" % tokens[n], "101": "https://panel.example/img/%s/101.jpg" % tokens[n]}
    assert tokens[first] != tokens[second]


def test_without_a_public_address_the_supplier_photo_stays(tmp_path):
    conn, first, second = two_accounts(tmp_path, base="")
    cycle.run_for_accounts(conn, UNLOAD, REF, date(2026, 10, 14), tmp_path / "feed")
    assert {a.find("Images/Image").get("url") for a in feed(tmp_path, conn, first)} == {"https://example.test/1.jpg"}


def test_titles_are_clean_and_not_longer_than_seventy_signs():
    long_name = "Gislaved  185/55/15  T 86 SOFT FROST 200  XL Ш. Очень длинное дополнительное название"
    cand = Candidate("1", make_offer(goods_id="1", name_orig=long_name, stock=12), 4810, "Ikon Tyres", "Test Model")
    title = render_ad(cand, 4810, "P1", make_settings(), date(2026, 10, 14)).findtext("Title")
    assert "  " not in title and len(title) <= 70 and title.startswith("Gislaved 185 55 R15 SOFT FROST 200")  # owner's format 2026-10-14: no «Ш.», no «T 86» when it does not fit
    assert title.endswith(" в наличии")
    assert not title.endswith(" ") and title == title.strip()
    short = Candidate("2", make_offer(goods_id="2", name_orig="Kumho  205/55/16  H 91  KH27", stock=12), 4810, "Kumho", "Test")
    assert render_ad(short, 4810, "P1", make_settings(), date(2026, 10, 14)).findtext("Title") == "Kumho 205 55 R16 H 91 KH27 в наличии"


def test_the_funnel_finds_the_position_behind_a_suffixed_id():
    assert funnel.base_goods_id("102398094791_a4") == "102398094791"
    assert funnel.base_goods_id("102398094791-x4_a4") == "102398094791"
    assert funnel.base_goods_id("102398094791_20260716165744_r20261014") == "102398094791"
    assert funnel.base_goods_id("102398094791") == "102398094791"


def test_the_general_number_of_the_owner_goes_only_into_the_primary_account(tmp_path, bare_accounts):
    conn, first, second = two_accounts(tmp_path)
    cycle.run_for_accounts(conn, UNLOAD, REF, date(2026, 10, 14), tmp_path / "feed")
    one = {a.findtext("ContactPhone") for a in feed(tmp_path, conn, first)}
    assert one == {"+70000000000"}                                                              # the general number: the primary account
    results = cycle.run_for_accounts(conn, UNLOAD, REF, date(2026, 10, 14), tmp_path / "feed")
    other = [r for r in results if r.account_id == second][0]
    assert not other.ok                                                                      # the second has no phone of its own: not built (no ad is valid), never with the general one
    connections.set_avito_identity(conn, second, None, "n", "79000000012")
    again = cycle.run_for_accounts(conn, UNLOAD, REF, date(2026, 10, 14), tmp_path / "feed")
    assert [r.ok for r in again] == [True, True]
    assert {a.findtext("ContactPhone") for a in feed(tmp_path, conn, second)} == {"+79000000012"}
