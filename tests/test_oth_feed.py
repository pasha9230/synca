"""«На ОТХ» in the feed (the owner 2026-10-29): the ad of a position that lies on «На ОТХ» starts with «Шины в наличии» in every account of
ours and is not taken off while the order stays there, whatever the supplier's stock; when the order leaves the stage the usual rules apply."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date

from helpers import make_offer, make_settings
from tires_sync import feed_check, oth_stock, orders, store
from tires_sync.candidates import Candidate, Evaluation
from tires_sync.decide import decide
from tires_sync.models import Action, PublishedItem
from tests.test_order_reserve import card_at, stage
from tests.test_stock_pass import AFTER, FIVE, ads_of, events, prepared, public, published, run
from tests.test_feed_check import ad as check_ad, ctx as check_ctx

TODAY = date(2026, 10, 29)
S = make_settings()
LINE = oth_stock.FIRST_LINE


def cand(gid="100", stock=10):
    return Candidate(gid, make_offer(goods_id=gid, stock=stock, base_price=5000.0), 4810, "Ikon Tyres", "Test Model")


def pub(gid="100", price=4700, first_hold=None):
    return PublishedItem(gid, price, 5000.0, "P1", "supplier", first_hold)


def one(ev, published, oth, settings=S, **kw):
    ds = decide(ev, published, set(), {}, settings, TODAY, oth=oth, **kw)
    return [d for d in ds.decisions if d.goods_id == "100"][0]


def test_a_published_position_on_oth_is_held_on_its_last_price_whatever_the_supplier_has():
    for ev, why in ((Evaluation(ok={"100": cand(stock=2)}), "low_stock"), (Evaluation(), "absent_from_supplier"),
                    (Evaluation(excluded={"100": "old_production"}), "old_production"),
                    (Evaluation(problems={"100": ["bad_price:0"]}, stocks={"100": 1}), "low_stock")):
        d = one(ev, {"100": pub()}, {"100": 4})
        assert (d.action, d.reason, d.price, d.point, d.first_hold) == (Action.HOLD, "oth:" + why, 4700, "P1", TODAY)
        assert one(ev, {"100": pub()}, {}).action is Action.REMOVE                       # not on OTH: taken off as before
    stale = one(Evaluation(problems={"100": ["bad_price:0"]}), {"100": pub(first_hold=date(2026, 10, 1))}, {"100": 4})
    assert (stale.action, stale.reason, stale.first_hold) == (Action.HOLD, "oth:stale:bad_price:0", date(2026, 10, 1))


def test_the_owner_s_own_switches_take_a_position_on_oth_off_as_before():
    for why in ("manual_off", "brand_excluded", "not_enabled", "size_excluded", "diameter"):
        d = one(Evaluation(excluded={"100": why}), {"100": pub()}, {"100": 4})
        assert (d.action, d.reason) == (Action.REMOVE, why)


def test_oth_publishes_nothing_new_and_leaves_a_stocked_position_to_the_usual_rules():
    assert one(Evaluation(ok={"100": cand(stock=2)}), {}, {"100": 4}).action is Action.SKIP
    assert one(Evaluation(ok={"100": cand(stock=10)}), {}, {"100": 4}).action is Action.PUBLISH
    assert one(Evaluation(ok={"100": cand(stock=10)}), {"100": pub(price=4810)}, {"100": 4}).action is Action.KEEP


def test_holds_for_oth_are_not_removals_for_the_guard():
    published = {g: pub(g) for g in ("100", "101", "102", "103", "104", "105", "106", "107", "108", "109", "110")}
    ds = decide(Evaluation(), published, set(), {}, S, TODAY, oth={g: 4 for g in published})
    assert not ds.guard_tripped and {d.reason for d in ds.decisions} == {"oth:absent_from_supplier"}


def test_the_first_line_is_added_once_and_dropped_cleanly():
    text = "🔥 Ikon\n\nНОВЫЕ ШИНЫ"
    assert oth_stock.add_line(text) == LINE + "\n" + text
    assert oth_stock.add_line(oth_stock.add_line(text)) == LINE + "\n" + text
    assert oth_stock.drop_line(oth_stock.add_line(text)) == text and oth_stock.drop_line(text) == text
    assert oth_stock.add_line("x" * 7495) == "x" * 7495                                  # Avito's limit: the text stays whole without the line


def test_the_check_before_publication_does_not_judge_a_position_on_oth_by_stock():
    for kw in (dict(stocks={"100": 2}), dict(stocks={}), dict(stocks={}, known_stocks={"100": 1})):
        assert feed_check.check_feed([check_ad()], check_ctx(oth=frozenset({"100"}), **kw)) == []
        assert feed_check.check_feed([check_ad()], check_ctx(oth=frozenset({"100"}), held=frozenset({"100"}), **kw)) == []
        assert [f.rule for f in feed_check.check_feed([check_ad()], check_ctx(**kw))] == ["stock"]     # not on OTH: as before


def on_oth(tmp_path, *goods, qty=4):
    """An order at a point with one position per code, moved to «На ОТХ»; returns its card."""
    conn = store.connect(tmp_path / "t.db")
    try:
        orders.ensure_pipeline(conn, "2026-10-29T09:00:00")
        cid = card_at(conn, "at_point", "2026-10-29T09:00:00")
        conn.execute("UPDATE order_item SET goods_id = ? WHERE order_id = ?", (goods[0], cid))
        for code in goods[1:]:
            orders.add_item(conn, cid, goods_id=code, name="Ikon " + code, brand_key="ikon", season="winter", diameter=16, territory="1",
                            qty=qty, sale_unit=5000, supplier_price=4000, terms=None, fee_pct=0, promo_pct=0, now="2026-10-29T09:00:00", author="o")
        conn.commit()
        orders.move_card(conn, cid, stage(conn, "oth"), "2026-10-29T10:00:00", "o")
        return cid
    finally:
        conn.close()


def move(tmp_path, cid, code):
    conn = store.connect(tmp_path / "t.db")
    try:
        orders.move_card(conn, cid, stage(conn, code), "2026-10-29T12:00:00", "o")
    finally:
        conn.close()


def description(data: bytes, ad_id: str) -> str:
    return next(a.findtext("Description") for a in ET.fromstring(data).findall("Ad") if a.findtext("Id") == ad_id)


def snapshot(tmp_path, code, acct=1) -> str:
    conn = store.connect(tmp_path / "t.db")
    try:
        return store.get_snapshot(conn, code, acct)
    finally:
        conn.close()


def test_the_cycle_keeps_and_marks_the_positions_on_oth_in_every_account_and_lets_them_go_after(tmp_path, capsys):
    prepared(tmp_path)
    assert run(tmp_path, FIVE) == 0
    before = {1: ads_of(public(tmp_path, 1)), 2: ads_of(public(tmp_path, 2))}
    cid = on_oth(tmp_path, "101", "102")
    assert run(tmp_path, AFTER) == 0                                     # 101 fell to 2, 102 left the unload
    capsys.readouterr()
    for acct, suffix in ((1, ""), (2, "_a2")):
        data = public(tmp_path, acct)
        assert {"101" + suffix, "102" + suffix} <= set(ads_of(data))
        for code in ("101", "102"):
            text = description(data, code + suffix)
            assert text.startswith(LINE + "\n") and text.count(LINE) == 1
            held = ET.fromstring(ads_of(data)[code + suffix])
            assert held.findtext("Price") == ET.fromstring(before[acct][code + suffix]).findtext("Price")   # the last published price
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


def test_a_stocked_position_on_oth_gets_the_line_and_loses_it_when_the_order_leaves(tmp_path, capsys):
    prepared(tmp_path, accounts=("A",))
    assert run(tmp_path, FIVE) == 0
    cid = on_oth(tmp_path, "103")
    assert run(tmp_path, FIVE) == 0
    assert description(public(tmp_path), "103").startswith(LINE + "\n")
    assert LINE not in (snapshot(tmp_path, "103") or "")
    move(tmp_path, cid, "at_point")                                      # the client came back
    assert run(tmp_path, FIVE) == 0
    assert LINE not in description(public(tmp_path), "103")


def test_a_position_returned_to_the_supplier_is_no_longer_on_oth(tmp_path):
    prepared(tmp_path, accounts=("A",))
    on_oth(tmp_path, "101")
    conn = store.connect(tmp_path / "t.db")
    try:
        assert oth_stock.pieces(conn) == {"101": 4}
        from tires_sync import order_oth
        order_oth.mark_returned(conn, order_oth.stock(conn)["stock"][0]["item_id"], "2026-10-29T11:00:00", "o")
        assert oth_stock.pieces(conn) == {}
    finally:
        conn.close()


def test_a_base_without_orders_has_nothing_on_oth(tmp_path):
    import sqlite3
    assert oth_stock.pieces(sqlite3.connect(":memory:")) == {}


def test_a_set_of_four_stays_on_four_pieces_on_oth_and_an_alias_account_is_marked_too(tmp_path, capsys):
    from tires_sync import mirror_feed
    prepared(tmp_path, pack4=True)
    conn = store.connect(tmp_path / "t.db")
    mirror_feed.set_alias_scheme(conn, 2, True, "2026-10-29T09:00:00", "test")
    conn.close()
    assert run(tmp_path, FIVE) == 0
    on_oth(tmp_path, "101")                                              # 4 pieces of 101
    on_oth(tmp_path, "103", "102", qty=2)                                # 2 pieces of 102 (103 keeps its 4 from card_at)
    assert run(tmp_path, AFTER) == 0
    capsys.readouterr()
    first = ads_of(public(tmp_path, 1))
    assert {"101", "101-x4", "102"} <= set(first) and "102-x4" not in first
    assert all(description(public(tmp_path, 1), i).startswith(LINE + "\n") for i in ("101", "101-x4", "102"))
    alias = mirror_feed.alias_id(2, "101")
    second = ads_of(public(tmp_path, 2))
    assert {alias, alias + "-x4"} <= set(second) and description(public(tmp_path, 2), alias).startswith(LINE + "\n")
