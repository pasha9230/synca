"""One tire, one ad: several supplier codes of the same tire are published once, under the Id of its oldest ad."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date

from helpers import catalog_xml, commodity_xml, settings_dict
from tires_sync import connections, cycle, feed_check, mirror_feed, publishing, store
from tires_sync.catalog import parse_reference
from tires_sync.settings_store import init_settings

NOW = "2026-10-14T12:00:00"
REF = parse_reference('<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>')


def two_lots(stock_a, stock_b):
    same = dict(SLOAD="94", SWIDTH="205")                                                   # the same tire in two lots (two supplier codes)
    return catalog_xml(commodity_xml(NNOMMODIF="106", NREST=str(stock_a), SMNFCODE="LOT-A", **same),
                       commodity_xml(NNOMMODIF="125", NREST=str(stock_b), SMNFCODE="LOT-B", **same))


def setup(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(), NOW)
    account = connections.create_account(conn, "Основной", NOW, "o")
    connections.set_live(conn, account, True, NOW, "o")
    conn.commit()
    return conn, account


def ids(tmp_path, conn, account):
    token = connections.get_account(conn, account)["feed_token"]
    return sorted(a.findtext("Id") for a in ET.parse(tmp_path / "feed" / token / "next" / "avito_feed.xml").getroot())


def run(conn, tmp_path, unload, day=14):
    results = cycle.run_for_accounts(conn, unload, REF, date(2026, 10, day), tmp_path / "feed")
    assert results[0].ok, results[0].error
    publishing.publish_live_accounts(conn, tmp_path / "feed")


def test_the_same_tire_in_two_lots_is_published_once_the_one_with_more_stock(tmp_path):
    conn, account = setup(tmp_path)
    run(conn, tmp_path, two_lots(8, 50))
    assert ids(tmp_path, conn, account) == ["125"]


def test_the_published_lot_stays_when_the_stock_of_the_other_grows(tmp_path):
    conn, account = setup(tmp_path)
    run(conn, tmp_path, two_lots(8, 4))                                                        # the first has more stock: published
    assert ids(tmp_path, conn, account) == ["106"]
    run(conn, tmp_path, two_lots(8, 50), day=15)                                              # the second grows: the ad does not flip
    assert ids(tmp_path, conn, account) == ["106"]


def test_a_new_lot_takes_the_id_of_the_oldest_ad_of_the_tire(tmp_path):
    conn, account = setup(tmp_path)
    run(conn, tmp_path, catalog_xml(commodity_xml(NNOMMODIF="106", NREST="8", SMNFCODE="LOT-A", SLOAD="94", SWIDTH="205")))
    conn.execute("INSERT INTO avito_ad (account_id, avito_id, ad_id, seen_at, first_seen) VALUES (?,?,?,?,?)",
                 (account, 1, "106", "2026-10-14T00:00:00", "2026-07-16T00:00:00"))
    conn.commit()
    run(conn, tmp_path, two_lots(0, 50), day=15)                                              # the first lot is gone, the second one is the tire now
    assert ids(tmp_path, conn, account) == ["106"]                                            # under the Id the cabinet already knows
    assert mirror_feed.adopted_maps(conn, account)[0]["125"] == "106"
    run(conn, tmp_path, two_lots(8, 50), day=16)                                              # both lots are back: still one ad, the same Id
    assert ids(tmp_path, conn, account) == ["106"]


def test_the_check_before_publication_stops_one_tire_twice(tmp_path):
    from tires_sync.feed import render_ad
    from tires_sync.candidates import Candidate
    from helpers import make_offer, make_settings
    a = render_ad(Candidate("106", make_offer(goods_id="106", stock=12), 4810, "Ikon Tyres", "Test Model"), 4810, "P1", make_settings(), date(2026, 10, 14))
    b = render_ad(Candidate("125", make_offer(goods_id="125", stock=12), 4810, "Ikon Tyres", "Test Model"), 4810, "P1", make_settings(), date(2026, 10, 14))
    template = feed_check.template_tags_of(cycle.TEMPLATE_PATH)
    findings = feed_check.check_feed([a, b], feed_check.Context(mode="generated", template_tags=template, reference=REF, stocks={"106": 12, "125": 12}))
    assert "same_tire" in {f.rule for f in findings if f.level == "critical"}


def lot(code, stock, **kw):
    return commodity_xml(NNOMMODIF=code, NREST=str(stock), SMNFCODE="LOT-" + code, SLOAD="94", SWIDTH="205", **kw)


def cabinet(conn, account, ad_id, avito_id=1, first="2026-07-16T00:00:00"):
    conn.execute("INSERT INTO avito_ad (account_id, avito_id, ad_id, seen_at, first_seen) VALUES (?,?,?,?,?)",
                 (account, avito_id, ad_id, "2026-10-14T00:00:00", first))
    conn.commit()


def test_the_old_lot_has_left_the_unload_and_the_new_one_takes_its_id(tmp_path):
    conn, account = setup(tmp_path)
    run(conn, tmp_path, catalog_xml(lot("106", 8)))
    cabinet(conn, account, "106")
    run(conn, tmp_path, catalog_xml(lot("125", 50)), day=15)                       # 106 is gone from the unload, 125 is the same tire
    assert ids(tmp_path, conn, account) == ["106"]


def test_the_old_lot_comes_back_and_the_feed_is_not_blocked(tmp_path):
    conn, account = setup(tmp_path)
    run(conn, tmp_path, catalog_xml(lot("106", 8)))
    cabinet(conn, account, "106")
    run(conn, tmp_path, two_lots(0, 50), day=15)
    assert ids(tmp_path, conn, account) == ["106"]
    results = cycle.run_for_accounts(conn, catalog_xml(lot("106", 8)), REF, date(2026, 10, 16), tmp_path / "feed")
    assert results[0].ok, results[0].error                                        # the Id 106 is the position's own again: no confusion of codes
    assert ids(tmp_path, conn, account) == ["106"]


def test_a_held_lot_and_a_new_lot_of_the_same_tire_do_not_block_the_feed(tmp_path):
    conn, account = setup(tmp_path)
    run(conn, tmp_path, catalog_xml(lot("106", 8)))
    results = cycle.run_for_accounts(conn, catalog_xml(lot("106", 8, NPRICE_DELAYED="abc"), lot("125", 50)), REF, date(2026, 10, 15), tmp_path / "feed")
    assert results[0].ok, results[0].error
    assert ids(tmp_path, conn, account) == ["106"] or ids(tmp_path, conn, account) == ["125"]       # one ad of the tire, never both


def test_a_run_flat_tire_is_not_the_same_tire_as_the_ordinary_one(tmp_path):
    conn, account = setup(tmp_path)
    run(conn, tmp_path, catalog_xml(lot("106", 8), lot("125", 50, SRUNFLAT="Да")))
    assert len(ids(tmp_path, conn, account)) == 2


def test_the_chat_of_an_inherited_ad_points_to_the_lot_that_sells_it(tmp_path):
    from tires_sync import chat_deals
    conn, account = setup(tmp_path)
    run(conn, tmp_path, catalog_xml(lot("106", 8)))
    cabinet(conn, account, "106", avito_id=777)
    run(conn, tmp_path, two_lots(0, 50), day=15)
    assert chat_deals.ad_goods(conn, account, "777")[0] == "125"


def test_a_removal_for_the_same_tire_is_not_a_mass_removal(tmp_path):
    from tires_sync.decide import SETTINGS_DRIVEN
    assert "same_tire" in SETTINGS_DRIVEN


def test_moving_an_account_keeps_the_bindings_made_before(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(), NOW)
    account = connections.create_account(conn, "Партнёр-2", NOW, "o")
    mirror_feed.import_feed(conn, account, '<Ads><Ad><Id>100_1</Id><Title>T</Title><Price>1</Price></Ad></Ads>'.encode(), NOW)
    mirror_feed.bind_ids(conn, account, {"100": "100_early"}, NOW, "o")
    mirror_feed.adopt(conn, account, NOW, "o")
    assert mirror_feed.adopted_maps(conn, account)[0] == {"100": "100_early"}


def test_a_new_code_of_a_tire_whose_ad_left_the_feed_lately_takes_the_old_id(tmp_path):
    """Основной, 2026-10-13/14: the old code vanished from the supplier, a new code of the same tire came back. Avito saw a repeat of the first ad
    (its term had not ended), blocked the new one and published the first again. The new code must go under the Id of the first ad."""
    conn, account = setup(tmp_path)
    other = commodity_xml(NNOMMODIF="200", NREST="9", SMNFCODE="OTHER", SLOAD="91", SWIDTH="195")
    run(conn, tmp_path, catalog_xml(commodity_xml(NNOMMODIF="106", NREST="8", SMNFCODE="LOT-A", SLOAD="94", SWIDTH="205"), other))
    assert "106" in ids(tmp_path, conn, account)
    run(conn, tmp_path, catalog_xml(other), day=15)                                           # the old code is gone: its ad is taken off
    assert ids(tmp_path, conn, account) == ["200"]
    run(conn, tmp_path, catalog_xml(commodity_xml(NNOMMODIF="125", NREST="8", SMNFCODE="LOT-B", SLOAD="94", SWIDTH="205"), other), day=16)
    assert ids(tmp_path, conn, account) == ["106", "200"]                                     # the new code under the Id of the first ad
    assert mirror_feed.adopted_maps(conn, account)[0]["125"] == "106"


def test_the_old_id_is_remembered_for_good_not_only_for_a_term(tmp_path):                # owner, 2026-10-15: «навсегда»
    conn, account = setup(tmp_path)
    other = commodity_xml(NNOMMODIF="200", NREST="9", SMNFCODE="OTHER", SLOAD="91", SWIDTH="195")
    run(conn, tmp_path, catalog_xml(commodity_xml(NNOMMODIF="106", NREST="8", SMNFCODE="LOT-A", SLOAD="94", SWIDTH="205"), other))
    run(conn, tmp_path, catalog_xml(other), day=15)
    conn.execute("UPDATE retired_ad SET removed_at = '2026-08-01T00:00:00'")                  # long ago
    conn.commit()
    run(conn, tmp_path, catalog_xml(commodity_xml(NNOMMODIF="125", NREST="8", SMNFCODE="LOT-B", SLOAD="94", SWIDTH="205"), other), day=16)
    assert ids(tmp_path, conn, account) == ["106", "200"]


def lots_and_another(stock_a, stock_b):
    """The two lots of test_same_tire plus another tire, so that the feed is never empty."""
    same = dict(SLOAD="94", SWIDTH="205")
    return catalog_xml(commodity_xml(NNOMMODIF="106", NREST=str(stock_a), SMNFCODE="LOT-A", **same),
                       commodity_xml(NNOMMODIF="125", NREST=str(stock_b), SMNFCODE="LOT-B", **same),
                       commodity_xml(NNOMMODIF="101"))


def test_a_lot_taken_off_for_low_stock_hands_its_id_to_the_next_lot_that_comes_later(tmp_path):
    """feed-reviewer, 2026-10-24: lot 106 fell to 0 and was taken off while no other lot of the tire could be published; later lot 125 can.
    106 is still in the unload (with 0): its Id is handed over from the memory of retired ads, not a new Id (Avito blocks it as a repeat)."""
    conn, account = setup(tmp_path)
    run(conn, tmp_path, lots_and_another(8, 4))
    assert ids(tmp_path, conn, account) == ["101", "106"]
    run(conn, tmp_path, lots_and_another(0, 0), day=15)
    assert ids(tmp_path, conn, account) == ["101"]
    run(conn, tmp_path, lots_and_another(0, 50), day=16)
    assert ids(tmp_path, conn, account) == ["101", "106"]                                     # 125 under the Id of the ad of 106
    assert set(store.load_published(conn, account)) == {"101", "125"}


def test_a_retired_lot_back_at_the_threshold_with_the_same_tire_still_hands_its_id_over_as_before(tmp_path):
    """Unchanged by the fix: 106 back at the threshold is the same tire as 125, which is kept (more stock); 106 is hidden as the same tire,
    so the one ad of the tire goes on under the Id 106."""
    conn, account = setup(tmp_path)
    run(conn, tmp_path, lots_and_another(8, 0))
    run(conn, tmp_path, lots_and_another(0, 0), day=15)
    run(conn, tmp_path, lots_and_another(6, 50), day=16)
    assert ids(tmp_path, conn, account) == ["101", "106"]
    assert set(store.load_published(conn, account)) == {"101", "125"}


def test_a_retired_lot_that_comes_back_as_another_tire_keeps_its_own_id(tmp_path):
    """106 comes back at the threshold, but its description changed (another load index: another tire now). It is published under its own
    Id; 125 must not take that Id from the memory of retired ads (two ads under one Id)."""
    conn, account = setup(tmp_path)
    run(conn, tmp_path, lots_and_another(8, 0))
    run(conn, tmp_path, lots_and_another(0, 0), day=15)
    same = dict(SLOAD="94", SWIDTH="205")
    run(conn, tmp_path, catalog_xml(commodity_xml(NNOMMODIF="106", NREST="8", SMNFCODE="LOT-A", SLOAD="99", SWIDTH="205"),
                                    commodity_xml(NNOMMODIF="125", NREST="50", SMNFCODE="LOT-B", **same),
                                    commodity_xml(NNOMMODIF="101")), day=16)
    assert ids(tmp_path, conn, account) == ["101", "106", "125"]


def test_a_new_lot_takes_the_renewed_id_of_the_lot_with_its_mark(tmp_path):
    """tails2 Ф3: the owner renewed the Id of lot 106 (`106_r20261020`: the old ad was deleted in the cabinet, Avito does not take it back).
    106 falls to 0 while lot 125 of the same tire has stock: 125 takes the Id the cabinet has, with its mark. Without the mark it is the
    deleted ad: for Avito a new one, a repeat of the live `106_r…`, and blocked."""
    conn, account = setup(tmp_path)
    run(conn, tmp_path, lots_and_another(8, 4))
    mirror_feed.reissue_own(conn, account, ["106"], "20261020", NOW, "o")
    run(conn, tmp_path, lots_and_another(8, 4), day=15)
    assert ids(tmp_path, conn, account) == ["101", "106_r20261020"]
    run(conn, tmp_path, lots_and_another(0, 50), day=16)
    assert ids(tmp_path, conn, account) == ["101", "106_r20261020"]                           # 125 under the renewed Id of the ad of 106
    assert set(store.load_published(conn, account)) == {"101", "125"}
    assert mirror_feed.adopted_maps(conn, account)[0]["125"] == "106_r20261020"
    run(conn, tmp_path, lots_and_another(0, 50), day=17)
    assert ids(tmp_path, conn, account) == ["101", "106_r20261020"]                           # kept, the mark not doubled


def test_a_new_lot_takes_the_renewed_id_from_the_memory_of_retired_ads_too(tmp_path):
    """Ф3, the other path: 106 (renewed) was taken off with no other lot to take its Id; 125 comes later: the Id of the memory, with its mark."""
    conn, account = setup(tmp_path)
    run(conn, tmp_path, lots_and_another(8, 0))
    mirror_feed.reissue_own(conn, account, ["106"], "20261020", NOW, "o")
    run(conn, tmp_path, lots_and_another(8, 0), day=15)
    run(conn, tmp_path, lots_and_another(0, 0), day=16)
    assert ids(tmp_path, conn, account) == ["101"]
    run(conn, tmp_path, lots_and_another(0, 50), day=17)
    assert ids(tmp_path, conn, account) == ["101", "106_r20261020"]


def test_a_lot_back_as_another_tire_waits_while_its_id_is_on_the_ad_of_the_next_lot(tmp_path):
    """tails2 Ф2: 106 was taken off; it is still in the unload with 0, but its description changed (another load index). 125, the tire of the
    old ad of 106, takes the Id 106. 106 back at the threshold is another tire with its own Id 106, which the ad of 125 has now: two ads under
    one Id would stop the account's feed (duplicate_id). It waits, hidden like a duplicate, and comes out under its Id once 125 has left."""
    conn, account = setup(tmp_path)
    same, other = dict(SLOAD="94", SWIDTH="205"), dict(SLOAD="99", SWIDTH="205")

    def unload(stock_a, stock_b):
        return catalog_xml(commodity_xml(NNOMMODIF="106", NREST=str(stock_a), SMNFCODE="LOT-A", **other),
                           commodity_xml(NNOMMODIF="125", NREST=str(stock_b), SMNFCODE="LOT-B", **same),
                           commodity_xml(NNOMMODIF="101"))
    run(conn, tmp_path, lots_and_another(8, 0))
    run(conn, tmp_path, lots_and_another(0, 0), day=15)
    run(conn, tmp_path, unload(0, 50), day=16)
    assert ids(tmp_path, conn, account) == ["101", "106"]
    assert set(store.load_published(conn, account)) == {"101", "125"}                          # 125 under the Id 106
    run(conn, tmp_path, unload(8, 50), day=17)
    assert ids(tmp_path, conn, account) == ["101", "106"]                                     # one ad under 106: 125's
    assert set(store.load_published(conn, account)) == {"101", "125"}
    run(conn, tmp_path, unload(8, 0), day=18)                                                 # 125 leaves the feed this cycle: 106 still waits
    assert ids(tmp_path, conn, account) == ["101"]
    run(conn, tmp_path, unload(8, 0), day=19)
    assert ids(tmp_path, conn, account) == ["101", "106"]                                     # under its own Id, no new one
    assert set(store.load_published(conn, account)) == {"101", "106"}


def ten_six_and_twenty(stock_x, stock_y, stock_v=0):
    """106 (now another tire: load 99), 125 (the tire of the old ad of 106), 200 (another lot of 106's new tire), 101."""
    same, other = dict(SLOAD="94", SWIDTH="205"), dict(SLOAD="99", SWIDTH="205")
    return catalog_xml(commodity_xml(NNOMMODIF="106", NREST=str(stock_x), SMNFCODE="LOT-A", **other),
                       commodity_xml(NNOMMODIF="125", NREST=str(stock_y), SMNFCODE="LOT-B", **same),
                       commodity_xml(NNOMMODIF="200", NREST=str(stock_v), SMNFCODE="LOT-C", **other),
                       commodity_xml(NNOMMODIF="101"))


def handed_over(conn, tmp_path, account):
    """106 taken off, then 125 out under the Id 106 (binding 125 -> 106), as in the test above."""
    run(conn, tmp_path, lots_and_another(8, 0))
    run(conn, tmp_path, lots_and_another(0, 0), day=15)
    run(conn, tmp_path, ten_six_and_twenty(0, 50), day=16)
    assert ids(tmp_path, conn, account) == ["101", "106"] and set(store.load_published(conn, account)) == {"101", "125"}


def test_both_lots_back_in_one_cycle_give_one_ad_under_the_id_the_last_one_had(tmp_path):
    """Review of Ф2, 1: 125 (under 106) fell and left; then 106 (another tire now) and 125 come back in one cycle, neither published. The Id
    106 goes to 125, the last one under it (the memory of retired ads); 106 waits. Never two ads under 106."""
    conn, account = setup(tmp_path)
    handed_over(conn, tmp_path, account)
    run(conn, tmp_path, ten_six_and_twenty(0, 0), day=17)
    assert ids(tmp_path, conn, account) == ["101"]
    run(conn, tmp_path, ten_six_and_twenty(8, 50), day=18)
    assert ids(tmp_path, conn, account) == ["101", "106"]
    assert set(store.load_published(conn, account)) == {"101", "125"}


def test_a_lot_that_waits_for_its_id_lets_another_lot_of_its_tire_out(tmp_path):
    """Review of Ф2, 2: 106 waits (its Id is on the ad of 125); 200 is the same tire as 106 now, under its own Id, which nobody has: the tire
    goes out with 200 instead of leaving the feed with 106."""
    conn, account = setup(tmp_path)
    handed_over(conn, tmp_path, account)
    run(conn, tmp_path, ten_six_and_twenty(50, 50, 10), day=17)
    assert ids(tmp_path, conn, account) == ["101", "106", "200"]
    assert set(store.load_published(conn, account)) == {"101", "125", "200"}


def test_a_waiting_lot_stays_hidden_when_its_id_goes_to_a_lot_of_its_old_tire_in_the_same_step(monkeypatch):
    """Review 2 of Ф2 (B): 106 is another tire now (T2); 125 and 130 of T1 are bound to 106 (130 was the last out under it, now at 0). 125 and
    106 both want 106: 106 has it by code, 125 waits. T1 goes with 300, which takes 106 from the memory of retired ads. 125, waiting, is the
    same tire as 300 now: it must still stay hidden, or two ads go under 106."""
    from types import SimpleNamespace as NS
    from tires_sync import same_tire
    monkeypatch.setattr(same_tire, "identity", lambda c: c.key)
    T1, T2, T3 = ("T1",), ("T2",), ("T3",)
    lots = {"106": (T2, 50), "125": (T1, 50), "130": (T1, 0), "300": (T1, 60), "101": (T3, 8)}
    ev = NS(ok={g: NS(key=k, offer=NS(stock=s)) for g, (k, s) in lots.items()}, problems={}, excluded={})
    binding = {"125": "106", "130": "106"}
    inherit = same_tire.dedupe(ev, {"101"}, 4, lambda g: binding.get(g, g), lambda i: None, lambda g: None,
                               [("125", "106", T1, "2026-10-17T00:00:00"), ("130", "106", T1, "2026-10-20T00:00:00")])
    going = {g: inherit.get(g, binding.get(g, g)) for g in ev.ok if ev.ok[g].offer.stock >= 4}
    assert going == {"300": "106", "101": "101"}
    assert ev.excluded == {"106": "same_tire", "125": "same_tire"}


def test_a_new_lot_takes_the_renewed_id_with_its_mark_in_a_second_account_too(tmp_path):
    """feed-reviewer of Ф3: the same in an account whose Ids carry `_a2`: 125 takes `106_a2_r20261020`, never `106_a2`."""
    conn, first = setup(tmp_path)
    second = connections.create_account(conn, "Второй", NOW, "o")
    connections.set_live(conn, second, True, NOW, "o")
    conn.commit()
    run(conn, tmp_path, lots_and_another(8, 4))
    mirror_feed.reissue_own(conn, second, ["106"], "20261020", NOW, "o")
    run(conn, tmp_path, lots_and_another(8, 4), day=15)
    assert ids(tmp_path, conn, second) == ["101_a2", "106_a2_r20261020"]
    run(conn, tmp_path, lots_and_another(0, 50), day=16)
    assert ids(tmp_path, conn, second) == ["101_a2", "106_a2_r20261020"]
    assert mirror_feed.adopted_maps(conn, second)[0]["125"] == "106_a2_r20261020"
    assert ids(tmp_path, conn, first) == ["101", "106"]                                      # the first account: its own Id, no mark


def test_a_renewed_lot_that_takes_the_renewed_id_of_another_lot_gets_no_second_mark(tmp_path):
    """tails2 report: 125 has its own mark (`125_r20261021`) and takes the Id of 106 (`106_r20261020`): that Id as it stands in the cabinet,
    never `106_r20261020_r20261021` (an Id Avito has never seen: a new ad, a repeat of the live one, blocked)."""
    conn, account = setup(tmp_path)
    run(conn, tmp_path, lots_and_another(8, 4))
    mirror_feed.reissue_own(conn, account, ["106"], "20261020", NOW, "o")
    mirror_feed.reissue_own(conn, account, ["125"], "20261021", NOW, "o")
    run(conn, tmp_path, lots_and_another(8, 4), day=15)
    assert ids(tmp_path, conn, account) == ["101", "106_r20261020"]
    for day in (16, 17):
        run(conn, tmp_path, lots_and_another(0, 50), day=day)
        assert ids(tmp_path, conn, account) == ["101", "106_r20261020"]                       # 125 under the Id of 106, one mark
    assert set(store.load_published(conn, account)) == {"101", "125"}


def test_a_renewed_lot_marks_a_plain_inherited_id_as_before(tmp_path):
    """Only 125 renewed and its inherited Id `106` has no mark: marked as before (`ids-heal --renew` of an inherited Id deleted in the cabinet
    gives it a new Id); `_final_id` says the same."""
    from tires_sync.sync import _final_id
    conn, account = setup(tmp_path)
    run(conn, tmp_path, lots_and_another(8, 4))
    mirror_feed.reissue_own(conn, account, ["125"], "20261021", NOW, "o")
    for day in (16, 17):
        run(conn, tmp_path, lots_and_another(0, 50), day=day)
        assert ids(tmp_path, conn, account) == ["101", "106_r20261021"]
    assert _final_id("125", mirror_feed.adopted_maps(conn, account)[0], "", mirror_feed.reissued_own(conn, account), account) == "106_r20261021"


def test_no_second_mark_on_an_inherited_id_in_a_second_account_either(tmp_path):
    conn, first = setup(tmp_path)
    second = connections.create_account(conn, "Второй", NOW, "o")
    connections.set_live(conn, second, True, NOW, "o")
    conn.commit()
    run(conn, tmp_path, lots_and_another(8, 4))
    mirror_feed.reissue_own(conn, second, ["106"], "20261020", NOW, "o")
    mirror_feed.reissue_own(conn, second, ["125"], "20261021", NOW, "o")
    run(conn, tmp_path, lots_and_another(8, 4), day=15)
    for day in (16, 17):
        run(conn, tmp_path, lots_and_another(0, 50), day=day)
        assert ids(tmp_path, conn, second) == ["101_a2", "106_a2_r20261020"]
    assert ids(tmp_path, conn, first) == ["101", "106"]


def test_the_id_of_a_position_is_the_one_the_feed_writes():
    """`_final_id` (what the hourly pass hands over and removes) and `build_ads` agree: the mark only on the position's own Id."""
    from tires_sync.sync import _final_id
    renewed = {"125": "r20261021", "106": "r20261020"}
    assert _final_id("125", {}, "", renewed) == "125_r20261021"
    assert _final_id("125", {}, "_a2", renewed) == "125_a2_r20261021"
    assert _final_id("125", {"125": "106_r20261020"}, "", renewed) == "106_r20261020"            # inherited: as it is
    assert _final_id("125", {"125": "106_a2"}, "_a2", renewed) == "106_a2_r20261021"             # not marked yet: as before
    assert _final_id("125", {"125": "106_a2_r20261020"}, "_a2", renewed) == "106_a2_r20261020"
    assert _final_id("125", {"125": "125_r20261014"}, "", renewed) == "125_r20261014_r20261021"   # its own Id renewed twice: as before
    assert _final_id("125", {"125": "125_a2"}, "_a2", renewed) == "125_a2_r20261021"            # bound to its own code: its own Id
    assert _final_id("106", {"106": "106_r20261020"}, "", renewed) == "106_r20261020"


def test_an_inherited_alias_with_a_mark_gets_no_second_one():
    """An account with the alias scheme: 125 took the alias of 106 (`k…_r20261020`); its own renewal does not mark it again. Its own alias does."""
    from tires_sync.sync import _final_id
    own, other = mirror_feed.alias_id(3, "125"), mirror_feed.alias_id(3, "106")
    renewed = {"125": "r20261021"}
    assert _final_id("125", {"125": other + "_r20261020"}, "_a3", renewed, 3) == other + "_r20261020"
    assert _final_id("125", {"125": own}, "_a3", renewed, 3) == own + "_r20261021"
    assert _final_id("125", {"125": own + "_r20261014"}, "_a3", renewed, 3) == own + "_r20261014_r20261021"     # its own, renewed twice


def test_the_mirror_new_positions_never_mark_a_renewed_id_twice(tmp_path):
    """`apply_reissued_own` (the new positions of a mirror account, after build_ads): an Id that already has a mark, its own one written by
    build_ads or a renewed Id taken over, stays; a plain own Id is marked as before."""
    conn, account = setup(tmp_path)
    mirror_feed.reissue_own(conn, account, ["106", "300"], "20261027", NOW, "o")

    def ad(ad_id):
        node = ET.Element("Ad")
        ET.SubElement(node, "Id").text = ad_id
        return node
    ads = [ad("106_r20261020"), ad("106_a2_r20261027"), ad("300"), ad("300-x4")]
    mirror_feed.apply_reissued_own(conn, account, ads)
    assert [a.findtext("Id") for a in ads] == ["106_r20261020", "106_a2_r20261027", "300_r20261027", "300-x4_r20261027"]
