"""«На ОТХ» in the feed (the owner 2026-10-29): the ad of a position that lies on «На ОТХ» starts with «Шины в наличии на <точка>» in every
account of ours, shows the pieces on OTH plus the supplier's stock, is not taken off while the order stays there and is published again if it
was not on Avito (its former Id, never a new one); an excluded brand or a season switched off do not stop it, the manual «выкл» does. Orders
at test points give nothing. When the order leaves the stage the usual rules apply."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date

from helpers import make_offer, make_settings
from tires_sync import feed_check, oth_stock, order_points, orders, store
from tires_sync.candidates import Candidate, Evaluation, evaluate
from tires_sync.decide import decide
from tires_sync.overrides import override_key
from tires_sync.models import Action, PublishedItem
from tests.test_order_reserve import card_at, stage
from tests.test_stock_pass import AFTER, FIVE, ads_of, events, prepared, public, published, rows, run
from tests.test_feed_check import ad as check_ad, ctx as check_ctx

TODAY = date(2026, 10, 29)
S = make_settings()
LINE = oth_stock.FIRST_LINE
AT_BETA = LINE + " на Бета"                       # card_at puts its orders at the point «Бета»


def cand(gid="100", stock=10, price=4810):
    return Candidate(gid, make_offer(goods_id=gid, stock=stock, base_price=5000.0), price, "Ikon Tyres", "Test Model")


def pub(gid="100", price=4700, first_hold=None):
    return PublishedItem(gid, price, 5000.0, "P1", "supplier", first_hold)


def one(ev, published, oth, settings=S, **kw):
    ds = decide(ev, published, set(), {}, settings, TODAY, oth=oth, **kw)
    return [d for d in ds.decisions if d.goods_id == "100"][0]


def test_a_published_position_on_oth_in_the_unload_is_built_afresh_whatever_its_stock():
    d = one(Evaluation(ok={"100": cand(stock=2)}), {"100": pub()}, {"100": 4})
    assert (d.action, d.reason, d.price) == (Action.UPDATE, "oth:low_stock", 4810)            # the price by the rules, as any ad
    d = one(Evaluation(ok={"100": cand(stock=2, price=4700)}), {"100": pub()}, {"100": 4})
    assert (d.action, d.reason, d.price) == (Action.KEEP, "oth:low_stock", 4700)
    assert one(Evaluation(ok={"100": cand(stock=2)}), {"100": pub()}, {}).action is Action.REMOVE   # not on OTH: taken off as before


def test_a_published_position_on_oth_out_of_the_unload_is_held_on_its_last_price():
    for ev, why in ((Evaluation(), "absent_from_supplier"), (Evaluation(excluded={"100": "old_production"}), "old_production"),
                    (Evaluation(problems={"100": ["bad_price:0"]}, stocks={"100": 1}), "low_stock")):
        d = one(ev, {"100": pub()}, {"100": 4})
        assert (d.action, d.reason, d.price, d.point, d.first_hold) == (Action.HOLD, "oth:" + why, 4700, "P1", TODAY)
        assert one(ev, {"100": pub()}, {}).action is Action.REMOVE
    stale = one(Evaluation(problems={"100": ["bad_price:0"]}), {"100": pub(first_hold=date(2026, 10, 1))}, {"100": 4})
    assert (stale.action, stale.reason, stale.first_hold) == (Action.HOLD, "oth:stale:bad_price:0", date(2026, 10, 1))


def test_the_manual_off_size_radius_still_take_a_position_on_oth_off():
    for why in ("manual_off", "size_excluded", "diameter", "same_tire"):
        d = one(Evaluation(excluded={"100": why}), {"100": pub()}, {"100": 4})
        assert (d.action, d.reason) == (Action.REMOVE, why)


def test_an_excluded_brand_or_a_season_switched_off_does_not_stop_a_position_on_oth_the_manual_off_does():
    offer = make_offer(goods_id="100", stock=2)
    ref = {"Ikon Tyres": frozenset({"Test Model"})}
    for settings in (make_settings(excluded_brands=["IKON TYRES"]), make_settings(enabled=[["PIRELLI", "winter"]])):
        assert "100" in evaluate({"100": offer}, {}, settings, ref, {}, oth=frozenset({"100"})).ok
        assert "100" not in evaluate({"100": offer}, {}, settings, ref, {}).ok                        # not on OTH: as before
        off = make_settings(excluded_brands=["IKON TYRES"], overrides={override_key("100"): "off"})
        assert evaluate({"100": offer}, {}, off, ref, {}, oth=frozenset({"100"})).excluded == {"100": "manual_off"}
    old = make_offer(goods_id="100", stock=2, name="IKON 205/55/16 T 94 Test Model старше 3-х лет")
    excluded = evaluate({"100": old}, {}, make_settings(excluded_brands=["IKON TYRES"]), ref, {}, oth=frozenset({"100"})).excluded
    assert excluded == {"100": "old_production"}                                                        # only the brand and the season give way


def test_a_position_on_oth_not_on_avito_is_published_with_no_threshold_and_no_queue():
    assert (one(Evaluation(ok={"100": cand(stock=1)}), {}, {"100": 4}).action, ) == (Action.PUBLISH, )
    d = one(Evaluation(ok={"100": cand(stock=1)}), {}, {"100": 4}, settings=make_settings(portion_limit=0))
    assert (d.action, d.reason) == (Action.PUBLISH, "oth:new")
    d = one(Evaluation(), {}, {"100": 4}, oth_retired={"100": 4650})                                      # gone from the unload: its last ad
    assert (d.action, d.reason, d.price) == (Action.PUBLISH, "oth:retired", 4650)
    ds = decide(Evaluation(excluded={"100": "manual_off"}), {}, set(), {}, S, TODAY, oth={"100": 4}, oth_retired={"100": 4650})
    assert ds.decisions == []                                                                            # switched off by hand: nothing


def test_at_most_twenty_ads_come_back_for_oth_in_a_cycle():
    ev = Evaluation(ok={str(n): cand(str(n), stock=1) for n in range(100, 125)})
    ds = decide(ev, {}, set(), {}, S, TODAY, oth={str(n): 4 for n in range(100, 125)})
    kinds = [(d.action, d.reason) for d in ds.decisions]
    assert kinds.count((Action.PUBLISH, "oth:new")) == 20 and kinds.count((Action.DEFER, "oth:limit")) == 5


def test_holds_for_oth_are_not_removals_for_the_guard():
    published = {g: pub(g) for g in ("100", "101", "102", "103", "104", "105", "106", "107", "108", "109", "110")}
    ds = decide(Evaluation(), published, set(), {}, S, TODAY, oth={g: 4 for g in published})
    assert not ds.guard_tripped and {d.reason for d in ds.decisions} == {"oth:absent_from_supplier"}


def test_the_first_line_names_the_points_and_the_count_is_put_in_the_text():
    text = "🔥 Ikon\n▸ В наличии: 6 шт.\nНОВЫЕ ШИНЫ"
    assert oth_stock.add_line(text, ("Янино", "Девяткино", "Янино")) == "Шины в наличии на Девяткино, Янино\n" + text
    assert oth_stock.add_line(text, ()) == LINE + "\n" + text
    marked = oth_stock.add_line(text, ("Янино",))
    assert oth_stock.add_line(marked, ("Юнона",)) == "Шины в наличии на Юнона\n" + text and oth_stock.drop_line(marked) == text
    assert oth_stock.drop_line(text) == text and oth_stock.add_line("x" * 7495, ("Янино",)) == "x" * 7495
    assert oth_stock.with_count(text, 9) == text.replace("6 шт", "9 шт")
    assert oth_stock.with_count(text + "\n▸ В наличии: 2 шт.", 9) == text + "\n▸ В наличии: 2 шт."          # two such lines: left as it is
    assert oth_stock.with_count("без числа", 9) == "без числа"


def test_the_check_before_publication_does_not_judge_a_position_on_oth_by_stock():
    for kw in (dict(stocks={"100": 2}), dict(stocks={}), dict(stocks={}, known_stocks={"100": 1})):
        assert feed_check.check_feed([check_ad()], check_ctx(oth=frozenset({"100"}), **kw)) == []
        assert feed_check.check_feed([check_ad()], check_ctx(oth=frozenset({"100"}), held=frozenset({"100"}), **kw)) == []
        assert [f.rule for f in feed_check.check_feed([check_ad()], check_ctx(**kw))] == ["stock"]     # not on OTH: as before


def connect(tmp_path):
    return store.connect(tmp_path / "t.db")


def on_oth(tmp_path, *goods, qty=4, point=None):
    """An order with one position per code (4 pieces of the first), at the point «Бета» or `point`, moved to «На ОТХ»; returns its card."""
    conn = connect(tmp_path)
    try:
        orders.ensure_pipeline(conn, "2026-10-29T09:00:00")
        cid = card_at(conn, "at_point", "2026-10-29T09:00:00")
        conn.execute("UPDATE order_item SET goods_id = ? WHERE order_id = ?", (goods[0], cid))
        if point:
            pid = next((p["id"] for p in order_points.list_points(conn) if p["name"] == point), None) or order_points.save_point(
                conn, dict(name=point, manager_name="М", channel="vk", vk_peer_id=str(100600 + len(order_points.list_points(conn)))))
            conn.execute("UPDATE order_card SET point_id = ? WHERE id = ?", (pid, cid))
        for code in goods[1:]:
            orders.add_item(conn, cid, goods_id=code, name="Ikon " + code, brand_key="ikon", season="winter", diameter=16, territory="1",
                            qty=qty, sale_unit=5000, supplier_price=4000, terms=None, fee_pct=0, promo_pct=0, now="2026-10-29T09:00:00", author="o")
        conn.commit()
        orders.move_card(conn, cid, stage(conn, "oth"), "2026-10-29T10:00:00", "o")
        return cid
    finally:
        conn.close()


def move(tmp_path, cid, code):
    conn = connect(tmp_path)
    try:
        orders.move_card(conn, cid, stage(conn, code), "2026-10-29T12:00:00", "o")
    finally:
        conn.close()


def ad_of(data: bytes, ad_id: str):
    return next(a for a in ET.fromstring(data).findall("Ad") if a.findtext("Id") == ad_id)


def description(data: bytes, ad_id: str) -> str:
    return ad_of(data, ad_id).findtext("Description")


def shown(text: str) -> int:
    return int(re.search(r"В наличии:\s*(\d+)", text).group(1))


def snapshot(tmp_path, code, acct=1) -> str:
    conn = connect(tmp_path)
    try:
        return store.get_snapshot(conn, code, acct)
    finally:
        conn.close()


def passed(capsys) -> str:
    out = capsys.readouterr()
    text = out.out + out.err
    assert "не пройдена" not in text, text
    return text


def test_the_cycle_keeps_and_marks_the_positions_on_oth_in_every_account_and_lets_them_go_after(tmp_path, capsys):
    prepared(tmp_path)
    assert run(tmp_path, FIVE) == 0
    before = {1: ads_of(public(tmp_path, 1)), 2: ads_of(public(tmp_path, 2))}
    cid = on_oth(tmp_path, "101", "102")
    capsys.readouterr()
    assert run(tmp_path, AFTER) == 0                                     # 101 fell to 2, 102 left the unload
    out = passed(capsys)
    assert "held_low_stock" not in out and "stock:" not in out           # the check knows them: not even a warning
    for acct, suffix in ((1, ""), (2, "_a2")):
        data = public(tmp_path, acct)
        assert {"101" + suffix, "102" + suffix} <= set(ads_of(data))
        for code in ("101", "102"):
            text = description(data, code + suffix)
            assert text.startswith(AT_BETA + "\n") and text.count(LINE) == 1
            assert ad_of(data, code + suffix).findtext("Price") == ET.fromstring(before[acct][code + suffix]).findtext("Price")
        assert shown(description(data, "101" + suffix)) == 4 + 2           # ours on OTH and the supplier's
        assert shown(description(data, "102" + suffix)) == 4               # gone from the unload: ours only
        assert not description(data, "103" + suffix).startswith(LINE)
        assert {"101", "102"} <= set(published(tmp_path, acct)) and events(tmp_path, acct) == []
        assert LINE not in snapshot(tmp_path, "101", acct)                # the saved ad stays clean: no line left after OTH
    assert run(tmp_path, AFTER, "--stock-only") == 0                     # the light pass takes nothing off either
    assert {"101", "102"} <= set(ads_of(public(tmp_path, 1)))
    assert run(tmp_path, AFTER) == 0                                     # a second hour: still there, still one line
    assert description(public(tmp_path, 1), "101").count(LINE) == 1
    move(tmp_path, cid, "done")                                          # sold from OTH: the usual rules from the next cycle
    capsys.readouterr()
    assert run(tmp_path, AFTER) == 0
    for acct, suffix in ((1, ""), (2, "_a2")):
        after = ads_of(public(tmp_path, acct))
        assert "101" + suffix not in after and "102" + suffix not in after
        assert sorted(events(tmp_path, acct)) == [("101", "removed", "low_stock"), ("102", "removed", "absent_from_supplier")]


def test_two_points_are_named_in_order_and_a_test_point_gives_nothing(tmp_path, capsys):
    prepared(tmp_path, accounts=("A",))
    assert run(tmp_path, FIVE) == 0
    on_oth(tmp_path, "103", point="Янино")
    on_oth(tmp_path, "103", point="Девяткино")
    on_oth(tmp_path, "103", point="Тест 1")                               # a test point: not on Avito, not counted
    on_oth(tmp_path, "104", point="тестовая")
    capsys.readouterr()
    assert run(tmp_path, FIVE) == 0
    passed(capsys)
    text = description(public(tmp_path), "103")
    assert text.startswith("Шины в наличии на Девяткино, Янино\n") and shown(text) == 4 + 4 + 10 and "Тест" not in text
    assert not description(public(tmp_path), "104").startswith(LINE)
    conn = connect(tmp_path)
    try:
        assert oth_stock.pieces(conn) == {"103": 8}
        by_point = oth_stock.by_point(conn)
        assert sorted(by_point.values()) == [4, 4] and {code for code, _point in by_point} == {"103"}
    finally:
        conn.close()


def test_a_test_point_can_also_be_named_by_its_id(tmp_path):
    prepared(tmp_path, accounts=("A",))
    on_oth(tmp_path, "101", point="Склад")
    conn = connect(tmp_path)
    try:
        pid = next(p["id"] for p in order_points.list_points(conn) if p["name"] == "Склад")
        assert oth_stock.pieces(conn) == {"101": 4}
        store.set_state(conn, oth_stock.TEST_POINTS_KEY, "[%d]" % pid)
        assert oth_stock.pieces(conn) == {} and oth_stock.by_point(conn) == {}
    finally:
        conn.close()


def test_a_stocked_position_on_oth_gets_the_line_and_loses_it_when_the_order_leaves(tmp_path, capsys):
    prepared(tmp_path, accounts=("A",))
    assert run(tmp_path, FIVE) == 0
    cid = on_oth(tmp_path, "103")
    before = snapshot(tmp_path, "103")
    assert run(tmp_path, AFTER) == 0                                     # 103 has a new price: its ad is built again and saved
    assert description(public(tmp_path), "103").startswith(AT_BETA + "\n")
    assert ad_of(public(tmp_path), "103").findtext("Price") != ET.fromstring(before).findtext("Price")   # the price follows the rules
    assert snapshot(tmp_path, "103") != before and LINE not in snapshot(tmp_path, "103")
    move(tmp_path, cid, "at_point")                                      # the client came back
    assert run(tmp_path, AFTER) == 0
    assert LINE not in description(public(tmp_path), "103") and shown(description(public(tmp_path), "103")) == 10


def test_a_position_returned_to_the_supplier_is_no_longer_on_oth(tmp_path):
    prepared(tmp_path, accounts=("A",))
    on_oth(tmp_path, "101")
    conn = connect(tmp_path)
    try:
        assert oth_stock.pieces(conn) == {"101": 4}
        from tires_sync import order_oth
        order_oth.mark_returned(conn, order_oth.stock(conn)["stock"][0]["item_id"], "2026-10-29T11:00:00", "o")
        assert oth_stock.pieces(conn) == {}
    finally:
        conn.close()


def test_a_base_without_orders_has_nothing_on_oth(tmp_path):
    import sqlite3
    assert oth_stock.pieces(sqlite3.connect(":memory:")) == {} and oth_stock.by_point(sqlite3.connect(":memory:")) == {}


def test_a_set_of_four_on_the_pieces_of_oth_and_the_supplier_and_an_alias_account_is_marked_too(tmp_path, capsys):
    from tires_sync import mirror_feed
    prepared(tmp_path, pack4=True)
    conn = connect(tmp_path)
    mirror_feed.set_alias_scheme(conn, 2, True, "2026-10-29T09:00:00", "test")
    conn.close()
    assert run(tmp_path, FIVE) == 0
    on_oth(tmp_path, "101", "102", qty=2)                                # 4 pieces of 101 (fell to 2), 2 of 102 (gone)
    assert run(tmp_path, AFTER) == 0
    passed(capsys)
    first = public(tmp_path, 1)
    assert {"101", "101-x4", "102"} <= set(ads_of(first)) and "102-x4" not in ads_of(first)
    assert all(description(first, i).startswith(AT_BETA + "\n") for i in ("101", "101-x4", "102"))
    alias = mirror_feed.alias_id(2, "101")
    second = public(tmp_path, 2)
    assert {alias, alias + "-x4"} <= set(ads_of(second)) and description(second, alias).startswith(AT_BETA + "\n")


def test_another_lot_of_the_same_tire_takes_the_id_and_the_ad_goes_on_with_the_line(tmp_path, capsys):
    """101 is on OTH and falls below the threshold; 107 is the same tire with stock: it takes the Id of 101 (same_tire). One ad under that Id,
    never two (a repeated Id stops the account): 101 is not kept, 107 goes on under its Id with the line."""
    prepared(tmp_path)
    assert run(tmp_path, FIVE) == 0
    on_oth(tmp_path, "101")
    lots = rows(dict(NNOMMODIF="101", NREST="2"), *(dict(NNOMMODIF=str(n)) for n in range(102, 106)), dict(NNOMMODIF="107", SLOAD="95"))
    capsys.readouterr()
    assert run(tmp_path, lots) == 0
    passed(capsys)
    for acct, suffix in ((1, ""), (2, "_a2")):
        ids = [a.findtext("Id") for a in ET.fromstring(public(tmp_path, acct)).findall("Ad")]
        assert ids.count("101" + suffix) == 1 and not any(i.startswith("107") for i in ids)
        assert description(public(tmp_path, acct), "101" + suffix).startswith(AT_BETA + "\n")
        assert "107" in published(tmp_path, acct) and "101" not in published(tmp_path, acct)


def test_a_removed_position_that_the_same_tire_now_carries_is_not_published_twice(tmp_path, capsys):
    prepared(tmp_path, accounts=("A",))
    assert run(tmp_path, FIVE) == 0
    lots = rows(dict(NNOMMODIF="101", NREST="2"), *(dict(NNOMMODIF=str(n)) for n in range(102, 106)), dict(NNOMMODIF="107", SLOAD="95"))
    assert run(tmp_path, lots) == 0                                      # 101 off, 107 goes on under the Id «101»
    on_oth(tmp_path, "101")
    capsys.readouterr()
    assert run(tmp_path, lots) == 0
    passed(capsys)
    ids = [a.findtext("Id") for a in ET.fromstring(public(tmp_path)).findall("Ad")]
    assert ids.count("101") == 1 and description(public(tmp_path), "101").startswith(AT_BETA + "\n")
    assert "101" not in published(tmp_path)


def test_a_long_stay_on_oth_keeps_the_ad_alive_on_avito(tmp_path, capsys):
    """A held ad is the saved one: its DateEnd is renewed, or after some weeks on OTH Avito would close it."""
    prepared(tmp_path, accounts=("A",))
    assert run(tmp_path, FIVE) == 0
    on_oth(tmp_path, "102")
    conn = connect(tmp_path)
    old = store.get_snapshot(conn, "102", 1)
    conn.execute("UPDATE published_state SET snapshot = ? WHERE goods_id = '102'",
                 (old.replace(ET.fromstring(old).findtext("DateEnd"), "2026-09-01"),))
    conn.commit()
    conn.close()
    assert run(tmp_path, AFTER) == 0                                     # 102 is gone from the unload: held on the saved ad
    passed(capsys)
    held = ad_of(public(tmp_path), "102")
    assert held.findtext("DateEnd") > date.today().isoformat() and held.findtext("DateEnd") == ad_of(public(tmp_path), "103").findtext("DateEnd")


def test_a_tire_type_the_owner_left_out_of_the_account_takes_a_position_on_oth_off_there(tmp_path, capsys):
    from tires_sync import connections
    studs = rows(dict(NNOMMODIF="101", SMODIFNAME="IKON 205/55/16 T 95 Test Model Ш."), *(dict(NNOMMODIF=str(n)) for n in range(102, 106)))
    prepared(tmp_path)
    assert run(tmp_path, studs) == 0
    on_oth(tmp_path, "101")
    conn = connect(tmp_path)
    connections.set_excluded_types(conn, 1, ["Зимние шипованные"], "2026-10-29T11:00:00", "o")
    conn.close()
    capsys.readouterr()
    assert run(tmp_path, studs) == 0
    passed(capsys)
    assert "101" not in ads_of(public(tmp_path, 1)) and "101" not in published(tmp_path, 1)
    assert description(public(tmp_path, 2), "101_a2").startswith(AT_BETA + "\n")      # the other account has the type: there it stays


def test_a_removed_position_comes_back_under_its_former_id_when_it_goes_to_oth(tmp_path, capsys):
    prepared(tmp_path)
    assert run(tmp_path, FIVE) == 0
    assert run(tmp_path, AFTER) == 0                                     # 101 off (stock 2), 102 off (gone)
    for acct in (1, 2):
        assert not {"101", "102"} & set(published(tmp_path, acct))
    on_oth(tmp_path, "101", "102")
    capsys.readouterr()
    assert run(tmp_path, AFTER) == 0
    passed(capsys)
    for acct, suffix in ((1, ""), (2, "_a2")):
        data = public(tmp_path, acct)
        assert {"101" + suffix, "102" + suffix} <= set(ads_of(data))      # the former Ids: no new ad for Avito to call a repeat
        assert not any(i.startswith(("101_", "102_")) and i not in ("101_a2", "102_a2") for i in ads_of(data))
        assert shown(description(data, "101" + suffix)) == 6 and shown(description(data, "102" + suffix)) == 4
        assert description(data, "102" + suffix).startswith(AT_BETA + "\n")
        assert ad_of(data, "102" + suffix).findtext("DateEnd") == ad_of(data, "103" + suffix).findtext("DateEnd")
        assert {"101", "102"} <= set(published(tmp_path, acct))
        assert LINE not in snapshot(tmp_path, "102", acct)


def test_a_position_removed_before_its_ad_was_kept_does_not_come_back_from_nothing(tmp_path, capsys):
    prepared(tmp_path, accounts=("A",))
    assert run(tmp_path, FIVE) == 0
    assert run(tmp_path, AFTER) == 0
    conn = connect(tmp_path)
    conn.execute("UPDATE retired_ad SET snapshot = NULL")                 # retired before 2026-10-29: no saved ad
    conn.commit()
    conn.close()
    on_oth(tmp_path, "102")
    capsys.readouterr()
    assert run(tmp_path, AFTER) == 0
    passed(capsys)
    assert "102" not in ads_of(public(tmp_path))


def test_a_season_switched_off_does_not_keep_a_position_on_oth_off_avito(tmp_path, capsys):
    pricing = {"IKON TYRES|winter": {"pct_small": -3.82, "pct_large": -4.28}, "PIRELLI|winter": {"pct_small": -5.85, "pct_large": -6.2},
               "IKON TYRES|summer": {"pct_small": 0, "pct_large": 0}}
    unload = rows(*(dict(NNOMMODIF=str(n)) for n in range(101, 106)), dict(NNOMMODIF="106", SSEASON="Летняя", NREST="1"))
    prepared(tmp_path, accounts=("A",), pricing=pricing)
    assert run(tmp_path, unload) == 0
    assert "106" not in ads_of(public(tmp_path))                          # summer is not switched on
    on_oth(tmp_path, "106")
    capsys.readouterr()
    assert run(tmp_path, unload) == 0
    passed(capsys)
    assert description(public(tmp_path), "106").startswith(AT_BETA + "\n") and shown(description(public(tmp_path), "106")) == 5
