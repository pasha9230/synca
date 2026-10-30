"""The 4tochki trial (plan tochki-feed §4–§5, owner 2026-10-27): account 1 only, one tire one ad, the same Id and photos when the supplier
changes, the strict entry of a new 4tochki position, at most 40 in the text, the rules of the check."""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from datetime import date, datetime

import pytest

from helpers import catalog_xml, commodity_xml, make_offer, settings_dict
from test_tire_match_feed_unchanged import fixed_clock, tochki_item  # noqa: F401  (fixed_clock: the golden day here too)
from tires_sync import connections, cycle, feed_check, store, tire_match, tochki_feed
from tires_sync.candidates import Candidate, Evaluation
from tires_sync.catalog import parse_reference
from tires_sync.cli import main
from tires_sync.settings_store import active_settings, init_settings
from tires_sync.suppliers import load_all_parsed
from tires_sync.webmim import ParseResult

NOW = "2026-10-14T10:00:00"
REFERENCE = ('<Shiny>\n<make name="Ikon Tyres">\n<model name="Character Ice 7"/>\n<model name="Nordman 7"/>\n<model name="Test Model"/>\n'
             '<model name="Lone Model"/>\n</make>\n</Shiny>')
TRIAL_MERGE = {"accounts": [1], "mode": "merge"}


def unload(rest_100="12", without_100=False, extra=()):
    return catalog_xml(*([] if without_100 else [
        commodity_xml(NNOMMODIF="100", TERRITORY_RN="1001", NPRICE_DELAYED="4000", NREST=rest_100, SWIDTH="155", SHEIGHT="80", SDIAMETR="13",
                      SLOAD="79", SSPEED="T", SMNFCODE="CI7", SMODEL="Ikon Character Ice 7", SBRANDCOUNTRY="КОРЕЯ",
                      SPICTURE="https://example.test/ci7.jpg",
                      SMODIFNAME="Ikon Tyres 155/80/13 T 79 Ikon Character Ice 7 Ш.", SMODIFNAME_ORIG="Ikon Tyres 155/80/13 T 79 Ikon Character Ice 7 Ш.")]),
        *extra,
        commodity_xml(NNOMMODIF="101", TERRITORY_RN="1001", NPRICE_DELAYED="6000", NREST="5", SWIDTH="215", SMNFCODE="TS2",
                      SMODIFNAME="IKON 215/55/16 T 95 Test Model", SMODIFNAME_ORIG="Ikon 215/55/16 T 95 Test Model"))


def snapshot():
    return {"version": 1, "fetched_at": datetime.utcnow().isoformat(timespec="seconds"), "warehouse_id": 7, "items": [
        tochki_item("TCH1", "IKON 155/80R13 79T Character Ice 7 (Nordman 7) TL (шип.)", "Character Ice 7 (Nordman 7)", 155, 80, 13, 79, 41,
                    3500.0, thorn=True),                     # the same tire as WebMIM 100
        tochki_item("TCH9", "IKON 225/55R16 98T Test Model", "Test Model", 225, 55, 16, 98, 10, 7000.0),      # only 4tochki; WebMIM has the model
        tochki_item("TCH7", "IKON 235/55R17 99T Lone Model", "Lone Model", 235, 55, 17, 99, 10, 7000.0),      # only 4tochki, a model WebMIM has not
    ]}


def prepare(tmp_path, trial=TRIAL_MERGE):
    conn = store.connect(tmp_path / "t.db")
    priced = {"enabled": [["IKON TYRES", "winter"], ["S2:IKON", "winter"]],
              "pricing": {"IKON TYRES|winter": {"pct_small": -3.0, "pct_large": -3.0}, "S2:IKON|winter": {"pct_small": 5.0, "pct_large": 5.0}}}
    init_settings(conn, settings_dict(territory_rn="1001", min_diameter=13, **priced), NOW)
    connections.create_account(conn, "Основной", NOW, "owner")
    connections.create_account(conn, "Партнёр-2", NOW, "owner")
    two = connections.create_supplier(conn, "4Точки", NOW, "owner")
    connections.set_config(conn, "tochki", 0, dict({"login": "api-user", "supplier_id": two, "feed": True}, **trial), NOW, "owner")
    store.upsert_brand_map(conn, "IKON", "Ikon Tyres")
    store.upsert_mapping(conn, "Ikon Tyres", "Ikon Character Ice 7", "Character Ice 7")
    store.upsert_mapping(conn, "Ikon Tyres", "Character Ice 7 (Nordman 7)", "Nordman 7")       # the other side: another Avito model
    conn.commit()
    conn.close()
    (tmp_path / "unload.xml").write_bytes(unload())
    (tmp_path / "tochki-snapshot.json").write_text(json.dumps(snapshot(), ensure_ascii=False), encoding="utf-8")
    (tmp_path / "reference.txt").write_text(REFERENCE, encoding="utf-8")
    return ["--db", str(tmp_path / "t.db"), "--unload", str(tmp_path / "unload.xml"),
            "--reference", str(tmp_path / "reference.txt"), "--feed-root", str(tmp_path / "feed")]


def cycle_and_publish(tmp_path, args) -> dict:
    """One cycle; its drafts become what Avito reads (the published file and the state). Returns {account name: [ads]}."""
    assert main(["run-cycle"] + args) == 0                                # 3 if the check found a critical finding
    conn = store.connect(tmp_path / "t.db")
    out = {}
    for account in connections.list_accounts(conn):
        folder = tmp_path / "feed" / account["feed_token"]
        data = (folder / "next" / "avito_feed.xml").read_bytes()
        store.commit_pending(conn, account["id"], data)
        (folder / "avito_feed.xml").write_bytes(data)
        out[account["name"]] = ET.fromstring(data).findall("Ad")
    conn.close()
    return out


def by_id(ads) -> dict:
    return {a.findtext("Id"): a for a in ads}


def images(ad):
    return [i.get("url") for i in ad.findall("Images/Image")]


def test_a_webmim_ad_goes_on_under_its_id_with_4tochki_stock_and_price_and_keeps_title_photos_and_model(tmp_path):
    args = prepare(tmp_path)
    first = cycle_and_publish(tmp_path, args)
    ads = by_id(first["Основной"])
    assert "100" in ads and "s2-TCH1" not in ads and "s2-TCH9" in ads and "s2-TCH7" in ads
    assert not [a for a in first["Партнёр-2"] if "s2-" in (a.findtext("Id") or "")]        # account 2: never 4tochki
    live = ads["100"]
    (tmp_path / "unload.xml").write_bytes(unload(rest_100="2"))                              # WebMIM below the threshold
    second = by_id(cycle_and_publish(tmp_path, args)["Основной"])
    ad = second["100"]
    assert not [i for i in second if "TCH1" in i]                                            # never a second Id of the tire
    assert ad.findtext("Title") == live.findtext("Title") and images(ad) == images(live) == ["https://example.test/ci7.jpg"]
    assert ad.findtext("Model") == live.findtext("Model") == "Character Ice 7"
    # price_guard (the owner 2026-10-30, answer 4): the tire that takes the ad keeps its anchor — 4000 of WebMIM × the rule of S2:IKON (+5 %),
    # not 3500 + 5 %, until the owner accepts the fall
    assert ad.findtext("Price") == "4200"
    from tires_sync import price_guard
    conn = store.connect(tmp_path / "t.db")
    assert price_guard.load(conn)["anchors"]["s2-TCH1"]["pending"] == 3500.0
    price_guard.accept(conn, ["s2-TCH1"], NOW, "owner")
    conn.close()
    second = by_id(cycle_and_publish(tmp_path, args)["Основной"])
    ad = second["100"]
    assert ad.findtext("Price") == "3680"                                                    # accepted: 3500 + 5 %, the rule of S2:IKON
    text = ad.findtext("Description")
    assert "В наличии: 40 шт." in text and "Артикул: CI7" in text and "Страна происхождения бренда: Корея" in text
    conn = store.connect(tmp_path / "t.db")
    assert store.last_match_switches(conn, 1)["100"][:2] == ("100", "s2-TCH1")
    assert store.last_match_switches(conn, 2) == {}
    conn.close()
    (tmp_path / "unload.xml").write_bytes(unload(rest_100="12"))                             # WebMIM back: the live ad stays as it is
    third = by_id(cycle_and_publish(tmp_path, args)["Основной"])
    assert not [i for i in third if "TCH1" in i] and images(third["100"]) == images(live)
    assert "В наличии: 40 шт." in third["100"].findtext("Description") and "Артикул: CI7" in third["100"].findtext("Description")
    (tmp_path / "unload.xml").write_bytes(unload(without_100=True))                         # WebMIM gone for good: the live ad is the same
    for _ in range(2):
        later = by_id(cycle_and_publish(tmp_path, args)["Основной"])
        assert not [i for i in later if "TCH1" in i]
        assert later["100"].findtext("Model") == "Character Ice 7" and later["100"].findtext("Title") == live.findtext("Title")
        assert images(later["100"]) == images(live)


def test_the_trial_switched_off_gives_the_ad_back_to_webmim_under_the_same_id(tmp_path):
    args = prepare(tmp_path)
    live = by_id(cycle_and_publish(tmp_path, args)["Основной"])["100"]
    (tmp_path / "unload.xml").write_bytes(unload(rest_100="2"))
    assert "В наличии: 40 шт." in by_id(cycle_and_publish(tmp_path, args)["Основной"])["100"].findtext("Description")
    conn = store.connect(tmp_path / "t.db")
    connections.set_config(conn, "tochki", 0, dict(connections.get_config(conn, "tochki", 0), accounts=[]), NOW, "owner")   # «Откат»
    conn.commit()
    conn.close()
    (tmp_path / "unload.xml").write_bytes(unload(rest_100="12"))
    ads = by_id(cycle_and_publish(tmp_path, args)["Основной"])
    assert not [i for i in ads if "s2-" in i] and "В наличии: 12 шт." in ads["100"].findtext("Description")
    assert images(ads["100"]) == images(live) and ads["100"].findtext("Title") == live.findtext("Title")


def test_without_the_merge_a_new_webmim_lot_does_not_come_next_to_a_published_4tochki_ad_of_the_tire(tmp_path):
    """no_twin: a live 4tochki ad stays when WebMIM gets the tire, and the WebMIM position waits (one tire, one ad), whatever Avito models
    the two are mapped to."""
    args = prepare(tmp_path, {"accounts": [1], "mode": "no_twin"})
    (tmp_path / "unload.xml").write_bytes(unload(without_100=True))
    ads = by_id(cycle_and_publish(tmp_path, args)["Основной"])
    assert "s2-TCH1" in ads                                                                  # WebMIM had no such tire: it went in
    (tmp_path / "unload.xml").write_bytes(unload())                                          # WebMIM 100 comes: the same tire
    ads = by_id(cycle_and_publish(tmp_path, args)["Основной"])
    assert "s2-TCH1" in ads and "100" not in ads


def test_the_light_pass_leaves_the_ad_whose_id_4tochki_takes_to_the_hourly_cycle(tmp_path):
    args = prepare(tmp_path)
    cycle_and_publish(tmp_path, args)
    (tmp_path / "unload.xml").write_bytes(unload(rest_100="2"))
    conn = store.connect(tmp_path / "t.db")
    connections.set_live(conn, 1, True, NOW, "owner")
    settings = active_settings(conn)
    parsed, _sha, _notes = load_all_parsed(conn, settings, tmp_path / "unload.xml")
    ref = parse_reference(REFERENCE)
    results = cycle.stock_pass(conn, parsed, ref, date(2026, 10, 25), tmp_path / "feed", now=datetime(2026, 10, 25, 3, 0, 0))
    conn.close()
    first = next(r for r in results if r.account_id == 1)
    assert "100" not in first.removed and "100" in first.left


def test_new_4tochki_ads_show_the_webmim_picture_of_their_model_else_their_own_and_keep_it(tmp_path):
    args = prepare(tmp_path)
    ads = by_id(cycle_and_publish(tmp_path, args)["Основной"])
    assert images(ads["s2-TCH9"]) == ["https://example.test/1.jpg"]                         # WebMIM 101, the same model (Test Model)
    assert images(ads["s2-TCH7"]) == ["https://img.4tochki.test/TCH7.jpg"]                  # no WebMIM of Lone Model: the supplier's
    assert "Артикул" not in ads["s2-TCH9"].findtext("Description")                           # nothing to take it from (owner, question 4)
    other = commodity_xml(NNOMMODIF="099", TERRITORY_RN="1001", NREST="5", SWIDTH="245", SLOAD="100", SMNFCODE="TS9",
                          SPICTURE="https://example.test/other.jpg", SMODIFNAME="IKON 245/55/16 T 100 Test Model",
                          SMODIFNAME_ORIG="Ikon 245/55/16 T 100 Test Model")
    (tmp_path / "unload.xml").write_bytes(unload(extra=[other]))                             # a smaller WebMIM code of the model, its own picture
    ads = by_id(cycle_and_publish(tmp_path, args)["Основной"])
    assert images(ads["s2-TCH9"]) == ["https://example.test/1.jpg"]                         # the live ad keeps its photo


def test_no_twin_mode_keeps_the_pair_out_and_the_trial_switched_off_takes_4tochki_off(tmp_path):
    args = prepare(tmp_path, {"accounts": [1], "mode": "no_twin"})
    ads = by_id(cycle_and_publish(tmp_path, args)["Основной"])
    assert "100" in ads and "s2-TCH9" in ads and not [i for i in ads if "TCH1" in i]
    conn = store.connect(tmp_path / "t.db")
    config = connections.get_config(conn, "tochki", 0)
    connections.set_config(conn, "tochki", 0, dict(config, accounts=[]), NOW, "owner")       # «Откат»: the trial off
    conn.commit()
    conn.close()
    assert main(["run-cycle"] + args) == 0
    conn = store.connect(tmp_path / "t.db")
    token = connections.get_account(conn, 1)["feed_token"]
    conn.close()
    text = (tmp_path / "feed" / token / "next" / "avito_feed.xml").read_text(encoding="utf-8")
    assert "s2-" not in text and "<Id>100</Id>" in text


# --- the strict entry (tire_match.tochki_entry), one code of two, a tire with a tube ---

def offer(gid, **kw):
    base = dict(goods_id=gid, brand_raw="IKON", brand_key="S2:IKON" if gid.startswith("s2-") else "IKON TYRES", model_raw="Test Model",
                name="IKON 205/55R16 94T Test Model", name_orig="IKON 205/55R16 94T Test Model", code="", stock=10)
    base.update(kw)
    return make_offer(**base)


def entry(offers, published=(), mode="no_twin", synonyms=None, keys=(), retired=(), allow=(), not_pairs=()):
    ev = Evaluation(ok={g: Candidate(g, o, 100, "Ikon Tyres", o.model_raw) for g, o in offers.items()})
    match = tire_match.pairs(offers, synonyms or {}, 2, not_pairs=not_pairs)
    return tire_match.tochki_entry(ev, match, offers, published, 2, mode, keys, retired, allow)


def test_a_4tochki_tire_of_its_own_goes_in():
    assert entry({"s2-A": offer("s2-A"), "1": offer("1", width=225, load="98", name="IKON 225/55R16 98T Test Model")}) == {}


def test_webmim_with_the_same_tire_under_another_model_keeps_it_out_in_any_state():
    offers = {"s2-A": offer("s2-A"), "1": offer("1", model_raw="Other Model", stock=0)}       # below the threshold, another model
    assert entry(offers) == {"s2-A": "tochki_twin"}
    offers["1"] = offer("1", model_raw="Other Model", xl=True)                               # XL is another tire
    assert entry(offers) == {}


def test_a_pair_is_kept_out_without_the_merge_and_goes_in_with_it():
    offers = {"s2-A": offer("s2-A"), "1": offer("1")}
    assert entry(offers) == {"s2-A": "tochki_pair"}
    assert entry(offers, mode="merge") == {}


def test_ambiguous_refused_and_doubtful_positions_stay_out():
    doubtful = {"s2-A": offer("s2-A", name="IKON 175/70R14 94T Test Model")}               # the name says another size
    assert entry(doubtful) == {"s2-A": "tochki_unclear"}
    ambiguous = {"s2-A": offer("s2-A"), "s2-B": offer("s2-B", model_raw="Test Model (Other)"), "1": offer("1", model_raw="Other")}
    assert entry(ambiguous, mode="merge")["s2-A"] in ("tochki_unclear", "tochki_twin")
    refused = {"s2-A": offer("s2-A"), "1": offer("1")}                                       # the owner: not one tire, yet one bucket
    assert entry(refused, mode="merge", not_pairs={("1", "s2-A")}) == {"s2-A": "tochki_twin"}


def test_a_published_or_retired_webmim_ad_of_the_tire_keeps_it_out_but_not_its_own_retired_ad():
    offers = {"s2-A": offer("s2-A")}
    key = ("IKON TYRES", "OTHERMODEL", "205", "55", "16", "Зимние нешипованные", "94", "T", False)
    assert entry(offers, keys=[key]) == {"s2-A": "tochki_twin"}
    assert entry(offers, retired=[("100", "100", key, "2026-01-01T00:00:00")]) == {"s2-A": "tochki_twin"}
    assert entry(offers, retired=[("s2-A", "s2-A", key, "2026-01-01T00:00:00")]) == {}


def test_a_4tochki_ad_that_went_on_under_a_webmim_id_tells_that_webmim_had_the_tire():
    key = ("IKON TYRES", "OTHERMODEL", "205", "55", "16", "Зимние нешипованные", "94", "T", False)
    assert entry({"s2-A": offer("s2-A")}, retired=[("s2-B", "100", key, "2026-01-01T00:00:00")]) == {"s2-A": "tochki_twin"}
    assert entry({"s2-A": offer("s2-A")}, retired=[("s2-B", "s2-B_r20261020", key, "2026-01-01T00:00:00")]) == {}


def test_the_merge_of_the_trial_counts_as_the_merge_on_for_the_pairs_page(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    two = connections.create_supplier(conn, "4Точки", NOW, "owner")
    assert not tire_match.merge_in_feed(conn)
    for cfg, on in (({"accounts": [1], "mode": "merge"}, True), ({"accounts": [1], "mode": "no_twin"}, False),
                    ({"accounts": [], "mode": "merge"}, False), ({"accounts": [1], "mode": "merge", "feed": False}, False)):
        connections.set_config(conn, "tochki", 0, dict({"login": "x", "supplier_id": two, "feed": True}, **cfg), NOW, "owner")
        assert tire_match.merge_in_feed(conn) is on, cfg
    conn.close()


def test_a_published_4tochki_ad_stays_when_webmim_gets_the_tire():
    offers = {"s2-A": offer("s2-A"), "1": offer("1", model_raw="Other Model")}
    assert entry(offers, published={"s2-A"}) == {}


def test_the_allow_list_lets_in_only_its_codes():
    offers = {"s2-A": offer("s2-A"), "s2-B": offer("s2-B", width=225, load="98", name="IKON 225/55R16 98T Test Model", name_orig="IKON 225/55R16 98T Test Model")}
    assert entry(offers, allow={"s2-B"}) == {"s2-A": "tochki_not_allowed"}


def test_two_4tochki_codes_of_one_tire_give_one_the_main_name_else_the_published_one():
    synonyms = {("IKON", "NORDMAN7"): "CHARACTERICE7"}
    offers = {"s2-N": offer("s2-N", model_raw="Nordman 7", stock=30), "s2-C": offer("s2-C", model_raw="Character Ice 7", stock=5)}
    assert entry(offers, synonyms=synonyms) == {"s2-N": "same_tire"}                          # Character, though less stock
    assert entry(offers, synonyms=synonyms, published={"s2-N"}) == {"s2-C": "same_tire"}     # the live ad stays


@pytest.mark.parametrize("name", ["165-13 78P АИ-168У TT + Камера 6,45-13 (УК-13М)", "Шина И-391 с камерой"])
def test_a_tire_with_a_tube_never_goes_in(name):
    assert entry({"s2-A": offer("s2-A", name=name, name_orig=name)}, published={"s2-A"}) == {"s2-A": "with_tube"}


# --- the account's positions (tochki_feed.for_account) ---

def test_out_of_the_trial_4tochki_is_not_there_in_it_the_stock_shown_is_at_most_40():
    parsed = ParseResult(offers={"1": offer("1"), "s2-A": offer("s2-A", stock=41)}, stocks={"s2-B": 2}, issues={"s2-C": ["bad"]})
    out = tochki_feed.for_account(parsed, 2, None)
    assert set(out.offers) == {"1"} and out.stocks == {} and out.issues == {} and set(parsed.offers) == {"1", "s2-A"}
    inside = tochki_feed.for_account(parsed, 2, {"mode": "no_twin"})
    assert inside.offers["s2-A"].stock == 40 and parsed.offers["s2-A"].stock == 41
    plain = ParseResult(offers={"1": offer("1")})
    assert tochki_feed.for_account(plain, 2, None) is plain and tochki_feed.for_account(parsed, None, None) is parsed


# --- the rules of the check ---

def ad(ad_id, model="Test Model", stock=10):
    return ET.fromstring("<Ad><Id>%s</Id><Model>%s</Model><Price>100</Price><Description>▸ В наличии: %d шт.</Description></Ad>" % (
        ad_id, model, stock))


def rules(ads, **kw):
    ctx = feed_check.Context(mode="generated", template_tags=frozenset({"Id", "Model", "Price", "Description"}), stocks={}, **kw)
    return {(f.rule, f.ad_id) for f in feed_check.check_feed(ads, ctx) if f.rule.startswith(("tire_twin", "tochki"))}


def test_tire_twin_stops_two_ads_of_one_tire_one_of_them_4tochki():
    same = {"100": "T", "s2-A": "T", "101": "U", "102": "U"}
    found = rules([ad("100"), ad("s2-A", model="Nordman 7"), ad("101"), ad("102")], tochki_prefix="s2-", tochki_trial=True, tire_ids=same)
    assert found == {("tire_twin", "s2-A")}                                                  # two WebMIM lots: the old rules' business
    inherited = rules([ad("100"), ad("100_r20261020")], tochki_prefix="s2-", tochki_trial=True, tire_ids={"100": "T", "s2-A": "T"},
                      id_code={"100_r20261020": "s2-A"})
    assert inherited == {("tire_twin", "100_r20261020")}


def test_a_webmim_position_under_a_4tochki_id_is_not_judged_as_4tochki():
    """A WebMIM lot of a tire whose 4tochki ad was taken off takes that ad's Id (same_tire): it is WebMIM, its stock text is its own."""
    ctx = dict(tochki_prefix="s2-", id_code={"s2-A": "100"})
    assert rules([ad("s2-A", stock=55)], tochki_trial=False, **ctx) == set()
    assert rules([ad("s2-A", stock=55)], tochki_trial=True, **ctx) == set()


def test_4tochki_outside_the_trial_and_more_than_40_in_the_text_stop_the_feed():
    assert rules([ad("s2-A")], tochki_prefix="s2-", tochki_trial=False) == {("tochki_outside_trial", "s2-A")}
    assert rules([ad("100")], tochki_prefix="s2-", tochki_trial=False, id_code={"100": "s2-A"}) == {("tochki_outside_trial", "100")}
    assert rules([ad("s2-A_r20261020")], tochki_prefix="s2-", tochki_trial=False) == {("tochki_outside_trial", "s2-A_r20261020")}
    assert rules([ad("s2-A")], tochki_prefix="s2-", tochki_trial=False, held=frozenset({"s2-A"})) == set()    # the guard holds it (rollback)
    assert rules([ad("s2-A", stock=41)], tochki_prefix="s2-", tochki_trial=True) == {("tochki_stock", "s2-A")}
    assert rules([ad("s2-A", stock=40), ad("100", stock=55)], tochki_prefix="s2-", tochki_trial=True) == set()
    assert rules([ad("s2-A", stock=41)]) == set()                                            # 4tochki not set up: nothing of it


def test_a_changed_model_of_a_live_4tochki_ad_is_a_warning():
    ctx = dict(tochki_prefix="s2-", tochki_trial=True, live_models={"100": "Character Ice 7"}, id_code={"100": "s2-A"})
    assert rules([ad("100", model="Nordman 7")], **ctx) == {("tochki_model_change", "100")}
    assert rules([ad("100", model="Character Ice 7")], **ctx) == set()
    levels = feed_check.load_rules()["rules"]
    assert levels["tochki_model_change"]["generated"] == "warning" and levels["tire_twin"]["generated"] == "critical"
    assert levels["tochki_outside_trial"]["mirror"] == "critical" and levels["tochki_stock"]["generated"] == "critical"


# --- review round 2 ---

def test_live_photos_are_kept_only_while_our_frames_among_them_are_still_the_account_s():
    """A withdrawn frame, a set out of the feed or own photos off: the ad is built afresh (else `own_photo_foreign` every cycle)."""
    from tires_sync import sync
    from tires_sync.photo_library import OwnPhotos, photo_url
    frame = photo_url("https://panel.test", "a" * 64, "jpg")
    live = [frame, "https://panel.test/img/T/100.jpg"]
    assert sync._keepable(live, OwnPhotos({}, {}, frozenset({frame})))
    assert not sync._keepable(live, OwnPhotos({}, {}, frozenset()))
    assert not sync._keepable(live, None)
    assert sync._keepable(["https://panel.test/img/T/100.jpg"], None) and not sync._keepable([], None)
    prefix = "https://panel.test/img/T/"                                                     # the photo address of the account now
    assert sync._keepable(["https://panel.test/img/T/100.jpg"], None, prefix)
    assert not sync._keepable(["https://old.test/img/T/100.jpg"], None, prefix)               # the address changed: the check «photo»
    assert sync._keepable(["https://panel.test/img/T/100.jpg?s=v2"], None, prefix, "v2")
    assert not sync._keepable(["https://panel.test/img/T/100.jpg?s=v1"], None, prefix, "v2")  # the look changed: new links, as every ad
    assert not sync._keepable(["https://panel.test/img/T/100.jpg"], None, prefix, "v2")


def test_a_withdrawn_frame_of_a_live_4tochki_ad_is_not_kept(tmp_path):
    args = prepare(tmp_path)
    cycle_and_publish(tmp_path, args)
    conn = store.connect(tmp_path / "t.db")
    token = connections.get_account(conn, 1)["feed_token"]
    conn.close()
    live = tmp_path / "feed" / token / "avito_feed.xml"
    frame = "https://panel.test/photos/%s.jpg" % ("b" * 64)
    live.write_text(live.read_text(encoding="utf-8").replace(
        '<Image url="https://example.test/1.jpg" />', '<Image url="%s" /><Image url="https://example.test/1.jpg" />' % frame), encoding="utf-8")
    assert frame in live.read_text(encoding="utf-8")                                         # the live ad led with our frame, now withdrawn
    assert main(["run-cycle"] + args) == 0                                                   # 3: own_photo_foreign would stop the account
    draft = by_id(ET.fromstring((tmp_path / "feed" / token / "next" / "avito_feed.xml").read_bytes()).findall("Ad"))
    assert images(draft["s2-TCH9"]) == ["https://example.test/1.jpg"]


def test_in_merge_a_webmim_row_the_trial_does_not_merge_waits_next_to_a_live_4tochki_ad():
    offers = {"s2-A": offer("s2-A"), "s3-W": offer("s3-W", brand_key="S3:IKON TYRES")}
    ev = Evaluation(ok={g: Candidate(g, o, 100, "Ikon Tyres", o.model_raw) for g, o in offers.items()})
    match = tire_match.only_webmim_and(tire_match.pairs(offers, {}, 2), 2)
    assert match.groups == [] and match.left_out == [("s2-A", "s3-W")]
    assert tire_match.tochki_entry(ev, match, offers, {"s2-A"}, 2, "merge") == {"s3-W": "same_tire"}


def test_an_account_of_the_alias_scheme_is_never_in_the_trial(tmp_path, monkeypatch):
    from tires_sync import mirror_feed
    conn = store.connect(tmp_path / "t.db")
    two = connections.create_supplier(conn, "4Точки", NOW, "owner")
    connections.set_config(conn, "tochki", 0, {"login": "x", "supplier_id": two, "feed": True, "accounts": [1], "mode": "no_twin"}, NOW, "o")
    assert tochki_feed.trial(conn, 1) is not None
    monkeypatch.setattr(mirror_feed, "alias_scheme", lambda conn, account_id: True)
    assert tochki_feed.trial(conn, 1) is None
    conn.close()


def test_the_pairs_page_builds_an_account_out_of_the_trial_without_the_merge(tmp_path, monkeypatch):
    from tires_sync import sync
    from tires_sync.panel import routes_pairs
    prepare(tmp_path)
    seen = []
    real = sync.plan_decisions
    monkeypatch.setattr(sync, "plan_decisions", lambda *a, **k: (seen.append((a[4], k.get("cross_supplier"))), real(*a, **k))[1])
    conn = store.connect(tmp_path / "t.db")
    settings = active_settings(conn)
    parsed, _sha, _notes = load_all_parsed(conn, settings, tmp_path / "unload.xml")
    out = routes_pairs.feed_ads(conn, parsed, parse_reference(REFERENCE), settings, datetime(2026, 10, 25, 3, 0, 0))
    conn.close()
    assert seen == [(1, None), (2, None)]                                                   # as the cycle reads it, account by account
    assert not [i for i in out[2] if "s2-" in i] and "100" in out[1]
