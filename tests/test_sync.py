from datetime import date

import pytest

from tires_sync import store
from tires_sync.catalog import parse_reference
from tires_sync.models import PublishedItem
from tires_sync.sync import SupplierDataError, run_cycle
from helpers import catalog_xml, commodity_xml, make_settings

REF = parse_reference('<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>')
TODAY = date(2026, 10, 6)


def xml(*rows):
    return catalog_xml(*rows)


def test_dry_run_builds_feed_reports_unmatched_and_does_not_touch_published(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    data = xml(
        commodity_xml(NNOMMODIF="100"),
        commodity_xml(NNOMMODIF="101", SMODEL="Test Model 2"),
        commodity_xml(NNOMMODIF="200", TERRITORY_RN="OTHER"),
    )
    report = run_cycle(data, make_settings(), REF, conn, TODAY, out_dir=tmp_path)
    assert report.counts == {"publish": 1, "skip": 1}
    assert report.other_territories == {"OTHER": 1}
    assert report.unmatched[0]["goods_id"] == "101"
    assert report.unmatched[0]["suggestions"] == ["Test Model"]
    assert store.load_published(conn) == {}
    feed = (tmp_path / "avito_feed.xml").read_text(encoding="utf-8")
    assert "<Id>100</Id>" in feed and "<Id>101</Id>" not in feed
    assert report.run_id == 1


def test_empty_supplier_data_with_published_items_stops_the_cycle(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    store.upsert_published(conn, PublishedItem("100", 4810, 5000.0, "P1"), "<Ad/>")
    with pytest.raises(SupplierDataError):
        run_cycle(xml(commodity_xml(TERRITORY_RN="OTHER")), make_settings(), REF, conn, TODAY, out_dir=tmp_path)
    assert not (tmp_path / "avito_feed.xml").exists()


def test_problem_on_published_item_keeps_snapshot_in_feed(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    snapshot = "<Ad><Id>100</Id><Title>OLD</Title></Ad>"
    store.upsert_published(conn, PublishedItem("100", 4810, 5000.0, "P1"), snapshot)
    store.upsert_published(conn, PublishedItem("101", 4810, 5000.0, "P1"), "<Ad><Id>101</Id></Ad>")
    data = xml(commodity_xml(NNOMMODIF="100", SDIAMETR="16C"), commodity_xml(NNOMMODIF="101"))
    report = run_cycle(data, make_settings(), REF, conn, TODAY, out_dir=tmp_path)
    assert report.counts["hold"] == 1
    feed = (tmp_path / "avito_feed.xml").read_text(encoding="utf-8")
    assert "<Title>OLD</Title>" in feed


def _published_four(conn):
    for gid in ("101", "102", "103", "104"):
        store.upsert_published(conn, PublishedItem(gid, 4810, 5000.0, "P1"), "<Ad><Id>%s</Id></Ad>" % gid)


def test_deliberate_mass_removal_is_not_blocked_by_feed_shrink_check(tmp_path):
    from tires_sync.feed import FeedCheckError
    conn = store.connect(tmp_path / "t.db")
    _published_four(conn)
    data = xml(commodity_xml(NNOMMODIF="101"))
    with pytest.raises(FeedCheckError):
        run_cycle(data, make_settings(), REF, conn, TODAY, out_dir=tmp_path)
    report = run_cycle(data, make_settings(allow_mass_removal=True), REF, conn, TODAY, out_dir=tmp_path)
    assert report.counts["remove"] == 3


def test_invalid_rendered_ad_on_published_item_becomes_a_hold_not_a_silent_keep(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    store.upsert_published(conn, PublishedItem("100", 4810, 5000.0, "P1"), "<Ad><Id>100</Id><Title>OLD</Title></Ad>")
    store.upsert_published(conn, PublishedItem("101", 4810, 5000.0, "P1"), "<Ad><Id>101</Id></Ad>")
    data = xml(commodity_xml(NNOMMODIF="100", SMODIFNAME="", SMODIFNAME_ORIG=""), commodity_xml(NNOMMODIF="101"))
    report = run_cycle(data, make_settings(), REF, conn, TODAY, out_dir=tmp_path)
    assert report.counts == {"hold": 1, "keep": 1}
    assert report.invalid_ads["100"] == ["empty:Title"]
    held = [d for d in report.decisions if d.goods_id == "100"][0]
    assert held.first_hold == TODAY and held.reason.startswith("invalid_ad:")


def test_invalid_rendered_ad_is_removed_after_stale_days(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    from datetime import timedelta
    store.upsert_published(conn, PublishedItem("100", 4810, 5000.0, "P1", first_hold=TODAY - timedelta(days=3)), "<Ad><Id>100</Id></Ad>")
    store.upsert_published(conn, PublishedItem("101", 4810, 5000.0, "P1"), "<Ad><Id>101</Id></Ad>")
    data = xml(commodity_xml(NNOMMODIF="100", SMODIFNAME="", SMODIFNAME_ORIG=""), commodity_xml(NNOMMODIF="101"))
    report = run_cycle(data, make_settings(), REF, conn, TODAY, out_dir=tmp_path)
    assert report.counts == {"remove": 1, "keep": 1}
    assert "<Id>100</Id>" not in (tmp_path / "avito_feed.xml").read_text(encoding="utf-8")


def test_confirmed_brand_mapping_is_used_by_the_cycle(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    ref = parse_reference(
        '<Shiny>\n<make name="Ikon Tyres Official">\n<model name="Test Model"/>\n</make>\n'
        '<make name="Pirelli">\n<model name="Test Model"/>\n</make>\n</Shiny>')
    data = xml(commodity_xml(NNOMMODIF="100"),
               commodity_xml(NNOMMODIF="200", SMARKA="Pirelli", SMODIFNAME="PIRELLI 205/55/16 T 94 Test Model"))
    first = run_cycle(data, make_settings(), ref, conn, TODAY, out_dir=tmp_path)
    assert first.counts == {"publish": 1, "skip": 1}
    store.upsert_brand_map(conn, "IKON TYRES", "Ikon Tyres Official")
    second = run_cycle(data, make_settings(), ref, conn, TODAY, out_dir=tmp_path)
    assert second.counts == {"publish": 2}


def test_cycle_records_new_positions_between_runs(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    run_cycle(xml(commodity_xml(NNOMMODIF="100")), make_settings(), REF, conn, TODAY, out_dir=tmp_path)
    second = run_cycle(xml(commodity_xml(NNOMMODIF="100"), commodity_xml(NNOMMODIF="105")),
                       make_settings(), REF, conn, TODAY, out_dir=tmp_path)
    report = store.new_positions(conn, second.run_id)
    assert [g["goods_id"] for g in report["goods"]] == ["105"] and report["baseline"] is False


def test_pack_of_four_ads_only_when_enabled_and_stock_allows(tmp_path):
    def ids(settings, stock):
        conn = store.connect(tmp_path / ("t%s%d.db" % (settings.pack4, stock)))
        out = tmp_path / ("o%s%d" % (settings.pack4, stock))
        run_cycle(xml(commodity_xml(NNOMMODIF="100", NREST=stock)), settings, REF, conn, TODAY, out_dir=out)
        feed = (out / "avito_feed.xml").read_text(encoding="utf-8")
        return [i for i in ("<Id>100</Id>", "<Id>100-x4</Id>") if i in feed]

    assert ids(make_settings(), 10) == ["<Id>100</Id>"]
    assert ids(make_settings(pack4=True), 10) == ["<Id>100</Id>", "<Id>100-x4</Id>"]
    assert ids(make_settings(pack4=True, min_stock=2), 3) == ["<Id>100</Id>"]


def test_an_ad_that_stands_on_avito_keeps_its_title_and_only_new_ads_get_the_new_format(tmp_path):
    """Owner, 2026-10-14: the new title format is for new ads; the titles of the ads that Avito already reads do not change."""
    import xml.etree.ElementTree as ET
    conn = store.connect(tmp_path / "t.db")
    folder = tmp_path / "token"
    (folder / "next").mkdir(parents=True)
    (folder / "avito_feed.xml").write_text(
        '<Ads><Ad><Id>100</Id><Title>OLD 205/55/16 Ш.</Title></Ad></Ads>', encoding="utf-8")        # what Avito reads now
    run_cycle(xml(commodity_xml(NNOMMODIF="100"), commodity_xml(NNOMMODIF="101")), make_settings(), REF, conn, TODAY, out_dir=folder / "next")
    titles = {a.findtext("Id"): a.findtext("Title") for a in ET.parse(str(folder / "next" / "avito_feed.xml")).getroot().findall("Ad")}
    assert titles["100"] == "OLD 205/55/16 Ш."                       # the ad that is on Avito: as it was
    assert titles["101"].endswith(" в наличии") and "205 55 R16" in titles["101"]   # a new ad: the new format


def _kept_title_cycle(tmp_path, conn, live_title, name, folder="token"):
    """One cycle of account 1 with `live_title` in the file Avito reads and `name` from the supplier: (title in the new feed, saved basis)."""
    import xml.etree.ElementTree as ET
    from tires_sync.sync import title_basis
    base = tmp_path / folder
    (base / "next").mkdir(parents=True, exist_ok=True)
    (base / "avito_feed.xml").write_text("<Ads><Ad><Id>100</Id><Title>%s</Title></Ad></Ads>" % live_title, encoding="utf-8")
    run_cycle(xml(commodity_xml(NNOMMODIF="100", SLOAD="94", SMODIFNAME_ORIG=name)), make_settings(), REF, conn, TODAY,
              out_dir=base / "next", account_id=1)
    titles = {a.findtext("Id"): a.findtext("Title") for a in ET.parse(str(base / "next" / "avito_feed.xml")).getroot().findall("Ad")}
    return titles["100"], title_basis(conn, 1)


def test_a_kept_title_stays_while_the_supplier_name_is_the_same_and_follows_a_rename(tmp_path):
    """Owner, 2026-10-19: the ad keeps its title while the supplier's name of the tire does not change; a renamed tire gets the fresh title."""
    conn = store.connect(tmp_path / "t.db")
    first = "Ikon 205/55/16 T 94 Test Model"
    assert _kept_title_cycle(tmp_path, conn, "OLD", first) == ("OLD", {"100": ["100", first]})       # first sight: the title stays, the name is remembered
    assert _kept_title_cycle(tmp_path, conn, "OLD", first)[0] == "OLD"                      # same name: same title
    assert _kept_title_cycle(tmp_path, conn, "OLD", "Ikon  205/55/16 T 94 Test Model KOREA")[0] == "OLD"   # spaces and origin: not a rename
    renamed = "Ikon 205/55/16 T 94 Test Model 2"
    fresh, basis = _kept_title_cycle(tmp_path, conn, "OLD", renamed)
    assert fresh != "OLD" and "Test Model 2" in fresh and fresh.endswith(" в наличии")    # the supplier renamed it: the fresh title
    assert basis == {"100": ["100", first]}                                                           # not published yet: the rename is not forgotten
    assert _kept_title_cycle(tmp_path, conn, "OLD", renamed)[0] == fresh                    # a cycle that was not published repeats it
    assert _kept_title_cycle(tmp_path, conn, fresh, renamed) == (fresh, {"100": ["100", renamed]})   # published: the new name is the basis now
    assert _kept_title_cycle(tmp_path, conn, fresh, renamed)[0] == fresh


def test_a_set_of_four_of_account_two_follows_a_rename_by_its_own_id(tmp_path):
    import xml.etree.ElementTree as ET
    from tires_sync.sync import title_basis
    conn = store.connect(tmp_path / "t.db")
    (tmp_path / "next").mkdir()
    (tmp_path / "avito_feed.xml").write_text(
        "<Ads><Ad><Id>100_a2</Id><Title>OLD</Title></Ad><Ad><Id>100-x4_a2</Id><Title>OLD4</Title></Ad></Ads>", encoding="utf-8")

    def titles(name):
        run_cycle(xml(commodity_xml(NNOMMODIF="100", SLOAD="94", NREST=10, SMODIFNAME_ORIG=name)), make_settings(pack4=True), REF, conn, TODAY,
                  out_dir=tmp_path / "next", account_id=2, id_suffix="_a2")
        return {a.findtext("Id"): a.findtext("Title") for a in ET.parse(str(tmp_path / "next" / "avito_feed.xml")).getroot().findall("Ad")}

    first = "Ikon 205/55/16 T 94 Test Model"
    assert titles(first) == {"100_a2": "OLD", "100-x4_a2": "OLD4"}
    assert title_basis(conn, 2) == {"100_a2": ["100", first], "100-x4_a2": ["100", first]}
    fresh = titles("Ikon 205/55/16 T 94 Test Model 2")
    assert "Test Model 2" in fresh["100_a2"] and fresh["100-x4_a2"].endswith("комплект 4 шт.")


def test_an_unreadable_published_file_does_not_forget_a_rename(tmp_path):
    from tires_sync.sync import title_basis
    conn = store.connect(tmp_path / "t.db")
    _kept_title_cycle(tmp_path, conn, "OLD", "Ikon 205/55/16 T 94 Test Model")
    from tires_sync import mirror_feed
    mirror_feed.set_bound_titles(conn, 1, {"999": "OTHER"})
    (tmp_path / "token" / "avito_feed.xml").write_text("<Ads><Ad>", encoding="utf-8")
    run_cycle(xml(commodity_xml(NNOMMODIF="100", SLOAD="94")), make_settings(), REF, conn, TODAY, out_dir=tmp_path / "token" / "next", account_id=1)
    assert title_basis(conn, 1) == {"100": ["100", "Ikon 205/55/16 T 94 Test Model"]}


def test_many_renames_at_once_keep_the_titles_and_the_basis(tmp_path):
    """Owner, 2026-10-19: when the supplier renames many tires at once (a new name format), no title on Avito changes and the owner is told."""
    import xml.etree.ElementTree as ET
    from tires_sync.sync import title_basis
    conn = store.connect(tmp_path / "t.db")
    first = "Ikon 205/55/16 T 94 Test Model"
    _kept_title_cycle(tmp_path, conn, "OLD", first)
    base = tmp_path / "token"
    tight = make_settings(guard_min=0, guard_fraction=0.0)                                  # one rename is already "many"
    report = run_cycle(xml(commodity_xml(NNOMMODIF="100", SLOAD="94", SMODIFNAME_ORIG="Ikon 205/55/16 T 94 Test Model 2")), tight, REF, conn,
                       TODAY, out_dir=base / "next", account_id=1)
    assert report.titles_held == 1
    assert ET.parse(str(base / "next" / "avito_feed.xml")).getroot().find("Ad").findtext("Title") == "OLD"
    assert title_basis(conn, 1) == {"100": ["100", first]}                                            # the rename is held, not forgotten
    report = run_cycle(xml(commodity_xml(NNOMMODIF="100", SLOAD="94", SMODIFNAME_ORIG=first)), tight, REF, conn, TODAY,
                       out_dir=base / "next", account_id=1)
    assert report.titles_held == 0                                                           # same name: nothing to hold


def test_renames_up_to_the_limit_pass_and_a_held_rename_comes_once_the_guard_allows(tmp_path):
    import xml.etree.ElementTree as ET
    from tires_sync.sync import title_basis
    conn = store.connect(tmp_path / "t.db")
    first, second = "Ikon 205/55/16 T 94 Test Model", "Ikon 205/55/16 T 94 Test Model 2"
    _kept_title_cycle(tmp_path, conn, "OLD", first)
    base = tmp_path / "token"

    def cycle(settings):
        report = run_cycle(xml(commodity_xml(NNOMMODIF="100", SLOAD="94", SMODIFNAME_ORIG=second)), settings, REF, conn, TODAY,
                           out_dir=base / "next", account_id=1)
        return report.titles_held, ET.parse(str(base / "next" / "avito_feed.xml")).getroot().find("Ad").findtext("Title")

    assert cycle(make_settings(guard_min=0, guard_fraction=0.0)) == (1, "OLD")               # one over a limit of 0: held
    held, fresh = cycle(make_settings(guard_min=1, guard_fraction=0.0))                      # one rename, limit 1: passes
    assert held == 0 and "Test Model 2" in fresh and title_basis(conn, 1) == {"100": ["100", first]}
    (base / "avito_feed.xml").write_text("<Ads><Ad><Id>100</Id><Title>%s</Title></Ad></Ads>" % fresh, encoding="utf-8")   # published
    assert cycle(make_settings(guard_min=0, guard_fraction=0.0)) == (0, fresh)               # nothing left to rename
    assert title_basis(conn, 1) == {"100": ["100", second]}


def test_an_id_that_passes_to_another_lot_of_the_tire_is_not_a_rename():
    """An Id taken over by another code (same_tire, prebind): the other lot's name starts a new basis, the live title stays."""
    import xml.etree.ElementTree as ET
    from tires_sync.sync import next_title_basis
    ad = ET.fromstring("<Ad><Id>100</Id><Title>FRESH</Title></Ad>")
    old = {"100": ["100", "Ikon 205/55/16 T 94 Ice Contact 3"]}
    assert next_title_basis(old, {"100": "LIVE"}, [ad], {id(ad): ("200", "Ikon 205/55/16 T 94 IceContact 3 XL")}) == \
        {"100": ["200", "Ikon 205/55/16 T 94 IceContact 3 XL"]}


def test_without_an_account_or_a_live_title_nothing_is_remembered(tmp_path):
    from tires_sync.sync import title_basis
    conn = store.connect(tmp_path / "t.db")
    (tmp_path / "next").mkdir()
    run_cycle(xml(commodity_xml(NNOMMODIF="100")), make_settings(), REF, conn, TODAY, out_dir=tmp_path / "next", account_id=1)
    assert title_basis(conn, 1) == {}                                                        # a new ad: its title is fresh anyway


def test_a_bound_title_from_avito_also_follows_a_rename(tmp_path):
    import xml.etree.ElementTree as ET
    from tires_sync import mirror_feed
    conn = store.connect(tmp_path / "t.db")
    mirror_feed.set_bound_titles(conn, 1, {"100": "FROM AVITO"})                             # the Id a position took over before publication
    (tmp_path / "next").mkdir()

    def title(name):
        run_cycle(xml(commodity_xml(NNOMMODIF="100", SLOAD="94", SMODIFNAME_ORIG=name)), make_settings(), REF, conn, TODAY,
                  out_dir=tmp_path / "next", account_id=1)
        return ET.parse(str(tmp_path / "next" / "avito_feed.xml")).getroot().find("Ad").findtext("Title")

    assert title("Ikon 205/55/16 T 94 Test Model") == "FROM AVITO"
    assert "Test Model 2" in title("Ikon 205/55/16 T 94 Test Model 2")


def test_renamed_ignores_format_spaces_studs_and_origin():
    from tires_sync.sync import renamed
    assert not renamed("Ikon 205/55/16 T 94 Test Model", "Ikon  205/55/16 T 94  Test Model Ш. KOREA")
    assert renamed("Ikon 205/55/16 T 94 Test Model", "Ikon 205/55/16 T 96 Test Model")
    assert renamed("Ikon 205/55/16 T 94 Test Model", "Ikon 205/60/16 T 94 Test Model")


def test_live_titles_are_read_only_beside_next_or_new_and_a_broken_file_gives_none(tmp_path):
    from tires_sync.sync import live_titles_of
    (tmp_path / "next").mkdir()
    assert live_titles_of(None) == {} and live_titles_of(tmp_path / "next") == {}              # no published file yet
    (tmp_path / "avito_feed.xml").write_text("<Ads><Ad><Id>1_a2</Id><Title>T</Title></Ad><Ad><Id>1-x4_a2</Id><Title>T4</Title></Ad></Ads>", encoding="utf-8")
    assert live_titles_of(tmp_path / "next") == {"1_a2": "T", "1-x4_a2": "T4"}               # the Id is the final one: suffix and pack included
    assert live_titles_of(tmp_path) == {}                                                       # a folder that is not a draft folder
    (tmp_path / "avito_feed.xml").write_text("<Ads><Ad>", encoding="utf-8")
    assert live_titles_of(tmp_path / "next") == {}


def test_a_kept_title_does_not_hide_a_broken_fresh_ad(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    folder = tmp_path / "token"
    (folder / "next").mkdir(parents=True)
    (folder / "avito_feed.xml").write_text("<Ads><Ad><Id>100</Id><Title>OLD</Title></Ad></Ads>", encoding="utf-8")
    report = run_cycle(xml(commodity_xml(NNOMMODIF="100", SMODIFNAME="", SMODIFNAME_ORIG=""), commodity_xml(NNOMMODIF="101")), make_settings(), REF, conn, TODAY, out_dir=folder / "next")
    assert report.invalid_ads.get("100")                                                         # no name from the supplier: invalid, whatever the live title is
