"""The models of a mirrored feed against the Avito reference: what is outside it is shown to the owner, a confirmed mapping replaces the Model."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date

from helpers import settings_dict
from tires_sync import connections, mirror_feed, store
from tires_sync.settings_store import init_settings

NOW = "2026-10-14T12:00:00"
REF = {
    "Yokohama": frozenset({"W.Drive V905", "BluEarth Winter V905", "BluEarth Winter V906"}),
    "Ikon Tyres": frozenset({"Autograph Ice C3", "Character Ice 7"}),
}


def car(ad_id, brand, model):
    return ('<Ad><Id>%s</Id><Brand>%s</Brand><Model>%s</Model><Title>Шина</Title><Price>5000</Price>'
            '<Description>Цена: 5000 руб.</Description></Ad>' % (ad_id, brand, model))


def feed(*ads):
    return ('<?xml version="1.0" encoding="utf-8"?><Ads formatVersion="3" target="Avito.ru">%s</Ads>' % "".join(ads)).encode("utf-8")


def setup(tmp_path, *ads):
    conn = store.connect(str(tmp_path / "x.db"))
    init_settings(conn, settings_dict(), NOW)
    account = connections.create_account(conn, "Партнёр", NOW, "o")
    mirror_feed.import_feed(conn, account, feed(*ads), NOW)
    return conn, account


def test_pairs_outside_the_reference_are_listed_with_their_ads(tmp_path):
    conn, account = setup(
        tmp_path, car("1_1", "YOKOHAMA", "V905"), car("2_1", "Yokohama", "v905"), car("3_1", "KUMHO", "Wi51"),
        car("4_1", "YOKOHAMA", "BLUEARTH WINTER V906"),                                    # the reference has it (case does not matter)
        car("5_1", "IKON TYRES", "Character Ice C2"))
    got = mirror_feed.unmatched_models(conn, account, REF)
    assert [(p["avito_brand"], p["model"], p["ad_ids"]) for p in got] == [
        ("Ikon Tyres", "Character Ice C2", ["5_1"]), ("Yokohama", "V905", ["1_1", "2_1"])]


def test_a_confirmed_mapping_replaces_the_model_and_clears_the_pair(tmp_path):
    conn, account = setup(tmp_path, car("1_1", "YOKOHAMA", "V905"), car("2_1", "YOKOHAMA", "BluEarth Winter V906"))
    assert mirror_feed.models_for(conn, account, REF) == {}
    store.upsert_mapping(conn, "Yokohama", "V905", "W.Drive V905")
    assert mirror_feed.models_for(conn, account, REF) == {"1_1": "W.Drive V905"}
    assert mirror_feed.unmatched_models(conn, account, REF) == []


def test_render_changes_only_the_model_of_the_mapped_ads(tmp_path):
    conn, account = setup(tmp_path, car("1_1", "YOKOHAMA", "V905"), car("2_1", "YOKOHAMA", "BluEarth Winter V906"))
    store.upsert_mapping(conn, "Yokohama", "V905", "W.Drive V905")
    models = mirror_feed.models_for(conn, account, REF)
    root = ET.fromstring(mirror_feed.render(conn, account, date(2026, 10, 14), settings_for_render(), models=models))
    got = {a.findtext("Id"): (a.findtext("Brand"), a.findtext("Model")) for a in root}
    assert got == {"1_1": ("YOKOHAMA", "W.Drive V905"), "2_1": ("YOKOHAMA", "BluEarth Winter V906")}
    plain = ET.fromstring(mirror_feed.render(conn, account, date(2026, 10, 14), settings_for_render()))
    assert {a.findtext("Id"): a.findtext("Model") for a in plain}["1_1"] == "V905"            # without `models` nothing changes


def test_a_mapping_to_a_model_that_left_the_reference_is_not_applied(tmp_path):
    conn, account = setup(tmp_path, car("1_1", "YOKOHAMA", "V905"))
    store.upsert_mapping(conn, "Yokohama", "V905", "Gone Model")
    assert mirror_feed.models_for(conn, account, REF) == {}
    assert [p["model"] for p in mirror_feed.unmatched_models(conn, account, REF)] == ["V905"]


def test_a_brand_the_reference_does_not_know_is_not_a_model_problem(tmp_path):
    conn, account = setup(tmp_path, car("1_1", "НЕИЗВЕСТНЫЙ", "X1"))
    assert mirror_feed.unmatched_models(conn, account, REF) == [] and mirror_feed.models_for(conn, account, REF) == {}


def settings_for_render():
    from tires_sync.config import settings_from_dict
    return settings_from_dict(settings_dict(), enforce_pct_limit=False)


def test_the_cycle_sends_the_confirmed_model_to_the_mirror_feed(tmp_path):
    from helpers import catalog_xml, commodity_xml
    from tires_sync import cycle
    ref = {"Ikon Tyres": frozenset({"Test Model", "Autograph Ice C3"})}
    conn, account = setup(tmp_path, car("100_20260801", "IKON TYRES", "Character Ice C2"))
    unload = catalog_xml(commodity_xml(NNOMMODIF="100"))
    path = tmp_path / "feed" / connections.get_account(conn, account)["feed_token"] / "next" / "avito_feed.xml"
    cycle.run_for_accounts(conn, unload, ref, date(2026, 10, 14), tmp_path / "feed")
    assert ET.parse(path).getroot().find("Ad").findtext("Model") == "Character Ice C2"                 # nobody has confirmed anything yet
    store.upsert_mapping(conn, "Ikon Tyres", "Character Ice C2", "Autograph Ice C3")
    cycle.run_for_accounts(conn, unload, ref, date(2026, 10, 14), tmp_path / "feed")
    ad = ET.parse(path).getroot().find("Ad")
    assert ad.findtext("Model") == "Autograph Ice C3" and ad.findtext("Id") == "100_20260801"          # the Id is never touched


def test_a_mapping_never_touches_an_ad_whose_model_the_reference_has(tmp_path):
    conn, account = setup(tmp_path, car("1_1", "YOKOHAMA", "BluEarth Winter V906"), car("2_1", "YOKOHAMA", "bluearth winter v906"))
    store.upsert_mapping(conn, "Yokohama", "BluEarth Winter V906", "W.Drive V905")              # a stale or careless mapping on a good model
    assert mirror_feed.models_for(conn, account, REF) == {}
    assert mirror_feed.unmatched_models(conn, account, REF) == []


def test_a_brand_written_differently_from_the_reference_is_a_brand_problem_not_a_model_one(tmp_path):
    ref = {"КАМА (Нижнекамский шинный завод)": frozenset({"Кама 505"})}
    conn, account = setup(tmp_path, car("1_1", "КАМА", "Irbis"))
    store.upsert_brand_map(conn, "КАМА", "КАМА (Нижнекамский шинный завод)")
    store.upsert_mapping(conn, "КАМА (Нижнекамский шинный завод)", "Irbis", "Кама 505")
    assert mirror_feed.models_for(conn, account, ref) == {}                                         # the Brand text would stay as written: not ours to judge
    assert mirror_feed.unmatched_models(conn, account, ref) == []


def test_the_mapping_keeps_the_count_the_order_and_the_ids_of_the_feed(tmp_path):
    ads = [car("%d_1" % n, "YOKOHAMA", "V905" if n % 2 else "BluEarth Winter V906") for n in range(1, 9)]
    conn, account = setup(tmp_path, *ads)
    before = ET.fromstring(mirror_feed.render(conn, account, date(2026, 10, 14), settings_for_render(), skip={"3_1", "4_1"}))
    store.upsert_mapping(conn, "Yokohama", "V905", "W.Drive V905")
    after = ET.fromstring(mirror_feed.render(conn, account, date(2026, 10, 14), settings_for_render(), skip={"3_1", "4_1"},
                                             models=mirror_feed.models_for(conn, account, REF)))
    assert [a.findtext("Id") for a in after] == [a.findtext("Id") for a in before] == ["1_1", "2_1", "5_1", "6_1", "7_1", "8_1"]


def test_a_failure_of_the_model_check_does_not_stop_the_mirror_feed(tmp_path, monkeypatch):
    from helpers import catalog_xml, commodity_xml
    from tires_sync import cycle
    conn, account = setup(tmp_path, car("100_20260801", "IKON TYRES", "Test Model"))
    monkeypatch.setattr(mirror_feed, "models_for", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    results = cycle.run_for_accounts(conn, catalog_xml(commodity_xml(NNOMMODIF="100")), {"Ikon Tyres": frozenset({"Test Model"})},
                                     date(2026, 10, 14), tmp_path / "feed")
    path = tmp_path / "feed" / connections.get_account(conn, account)["feed_token"] / "next" / "avito_feed.xml"
    assert [r.ok for r in results] == [True] and path.exists() and "boom" in results[0].error
