"""«Аналитика рекламы» как в Roistat (the owner 2026-11-12): the tree of groups (account, brand, model, width, profile, diameter, ad) over the
ads of the period, the totals of a group are the sums of its rows, «Без данных» for an ad without our Id or without a position of the
catalog, the sort inside every group with the empty values below, the formulas of the funnel (a dash for a division by zero), the
comparison with the previous period of the same length, one query of the catalog for the page. Made-up data only."""
from __future__ import annotations

import json
import pytest

from test_ads_spend import TODAY, db, mark, spend
from test_ads_spend_report import won_order
from tires_sync import ads_tree, mirror_feed


def item(conn, code, brand="Nokian", model="Hakka", width=205, height=55, diameter=16):
    data = {"goods_id": code, "name": "%s %s" % (brand, model), "brand_raw": brand, "model_raw": model, "width": width, "height": height,
            "diameter": diameter, "season": "winter"}
    conn.execute("INSERT INTO catalog_item (goods_id, data, first_seen, last_seen) VALUES (?, ?, '2026-10-01', '2026-11-11')",
                 (code, json.dumps(data, ensure_ascii=False)))


def ad(conn, account, avito_id, ad_id, title=""):
    conn.execute("INSERT OR REPLACE INTO avito_ad (account_id, avito_id, ad_id, title, seen_at) VALUES (?, ?, ?, ?, '2026-11-11')",
                 (account, avito_id, ad_id, title or "Объявление %d" % avito_id))


def shop(tmp_path):
    """Two accounts: the first with plain Ids (= the code), the second with `<code>_a2`; two brands, one ad without a position."""
    conn = db(tmp_path, accounts=2)
    mark(conn, 1, 14)
    mark(conn, 2, 14)
    item(conn, "100", "Nokian", "Hakka", 205, 55, 16)
    item(conn, "200", "Nokian", "Nordman", 195, 65, 15)
    item(conn, "300", "Cordiant", "Snow", 205, 55, 16)
    for account, avito_id, ad_id, rub, views, contacts in ((1, 11, "100", 300, 100, 10), (1, 12, "200", 100, 50, 5), (1, 13, "300", 50, 20, 0),
                                                          (1, 14, "999", 40, 10, 1),                     # no such position: «Без данных»
                                                          (2, 21, "100_a2", 200, 80, 4), (2, 22, "300_a2", 0, 30, 3)):
        ad(conn, account, avito_id, ad_id)
        spend(conn, account, avito_id, "2026-11-09", rub, views=views, contacts=contacts, listed=False)
    spend(conn, 1, 15, "2026-11-09", 25, views=5, listed=False)                                          # no our Id at all
    won_order(conn, items=((4, 5000.0, 4000.0),), chats=[(1, "11")])                                     # revenue 20000, margin 4000
    won_order(conn, items=((2, 3000.0, 2500.0),), chats=[(2, "22")])                                     # revenue 6000, margin 1000
    conn.commit()
    return conn


def groups(data, depth=0):
    return {r["label"]: r for r in data["rows"] if r["depth"] == depth and r["kind"] == "group"}


def children(data, row):
    return [r for r in data["rows"] if r["parent"] == row["id"]]


# ---- the levels ----------------------------------------------------------------------------------------------------------------------

def test_the_levels_are_read_from_the_address_and_bad_ones_left_out():
    assert ads_tree.parse_levels("brand,ad") == ("brand", "ad")
    assert ads_tree.parse_levels("width,width,bad,diameter") == ("width", "diameter")
    assert ads_tree.parse_levels("") is None and ads_tree.parse_levels("bad") is None
    assert ads_tree.DEFAULT_LEVELS == ("account", "brand", "ad")


def test_the_default_tree_is_account_brand_ad_and_the_totals_are_the_sums(tmp_path):
    conn = shop(tmp_path)
    data = ads_tree.build(conn, TODAY, 7, ads_tree.DEFAULT_LEVELS)
    accounts = groups(data)
    assert list(accounts) == ["Аккаунт 1", "Аккаунт 2"]                                                  # by the spending, the most first
    one = accounts["Аккаунт 1"]
    assert (one["m"]["spend"], one["m"]["views"], one["m"]["contacts"], one["m"]["pieces"], one["m"]["revenue"]) == (515.0, 185, 16, 4, 20000.0)
    brands = {r["label"]: r for r in children(data, one)}
    assert set(brands) == {"Nokian", "Cordiant", "Без данных"}
    assert brands["Nokian"]["m"]["spend"] == 400.0 and brands["Без данных"]["m"]["spend"] == 65.0       # 999 and the ad without our Id
    assert [r["ad"]["avito_id"] for r in children(data, brands["Nokian"])] == [11, 12]
    assert [r["ad"]["avito_id"] for r in children(data, brands["Без данных"])] == [14, 15]
    total = data["total"]["m"]
    assert total["spend"] == 715.0 and total["pieces"] == 6 and total["revenue"] == 26000.0
    for row in data["rows"]:                                                                              # every group is the sum of its rows
        kids = children(data, row)
        if row["kind"] == "group" and kids and all(k["kind"] != "more" for k in kids):
            for key in ("spend", "views", "contacts", "pieces", "orders", "revenue", "margin"):
                assert round(sum(k["m"][key] for k in kids), 2) == row["m"][key], (row["label"], key)


@pytest.mark.parametrize("level, labels", [
    ("account", {"Аккаунт 1", "Аккаунт 2"}),
    ("brand", {"Nokian", "Cordiant", "Без данных"}),
    ("model", {"Nokian Hakka", "Nokian Nordman", "Cordiant Snow", "Без данных"}),
    ("width", {"205", "195", "Без данных"}),
    ("profile", {"55", "65", "Без данных"}),
    ("diameter", {"R16", "R15", "Без данных"}),
])
def test_every_level_groups_and_the_second_account_finds_its_positions(tmp_path, level, labels):
    conn = shop(tmp_path)
    data = ads_tree.build(conn, TODAY, 7, (level,))
    top = groups(data)
    assert set(top) == labels
    assert round(sum(r["m"]["spend"] for r in top.values()), 2) == data["total"]["m"]["spend"] == 715.0
    assert not [r for r in data["rows"] if r["depth"] > 0]                                                  # no level «ad»: no rows below


def test_a_combination_of_levels_in_another_order(tmp_path):
    conn = shop(tmp_path)
    data = ads_tree.build(conn, TODAY, 7, ("diameter", "account", "ad"))
    r16 = groups(data)["R16"]
    accounts = {r["label"]: r for r in children(data, r16)}
    assert set(accounts) == {"Аккаунт 1", "Аккаунт 2"}
    assert [r["ad"]["avito_id"] for r in children(data, accounts["Аккаунт 1"])] == [11, 13]
    assert [r["ad"]["avito_id"] for r in children(data, accounts["Аккаунт 2"])] == [21, 22]
    assert r16["m"]["spend"] == 550.0 and r16["count"] == 4
    only_ads = ads_tree.build(conn, TODAY, 7, ("ad",))
    assert len([r for r in only_ads["rows"] if r["kind"] == "ad"]) == 7 and all(r["depth"] == 0 for r in only_ads["rows"])


def test_an_adopted_id_and_a_renewed_one_find_their_position(tmp_path):
    conn = shop(tmp_path)
    mirror_feed.bind_ids(conn, 2, {"200": "легаси-777"}, "2026-11-01T10:00:00", "o")
    ad(conn, 2, 23, "легаси-777")
    spend(conn, 2, 23, "2026-11-09", 10, views=1, listed=False)
    ad(conn, 1, 16, "300_r20261020")
    spend(conn, 1, 16, "2026-11-09", 10, views=1, listed=False)
    conn.commit()
    data = ads_tree.build(conn, TODAY, 7, ("model", "ad"))
    nordman = groups(data)["Nokian Nordman"]
    assert sorted(r["ad"]["avito_id"] for r in children(data, nordman)) == [12, 23]
    assert 16 in [r["ad"]["avito_id"] for r in children(data, groups(data)["Cordiant Snow"])]


def test_the_attributes_are_read_in_one_query_for_the_page(tmp_path):
    conn = shop(tmp_path)

    def count(levels):
        seen = []
        conn.set_trace_callback(seen.append)
        ads_tree.build(conn, TODAY, 7, levels)
        conn.set_trace_callback(None)
        return len([q for q in seen if "catalog_item" in q]), len(seen)

    few = count(("brand", "ad"))
    for n in range(40):                                                                                       # many more ads of known positions
        ad(conn, 1, 1000 + n, "100" if n % 2 else "200")
        spend(conn, 1, 1000 + n, "2026-11-08", 5, views=1, listed=False)
    conn.commit()
    many = count(("brand", "ad"))
    assert few[0] == many[0] == 1 and many[1] == few[1]                                                    # no query per ad


# ---- the sort ------------------------------------------------------------------------------------------------------------------------

def test_the_sort_both_ways_inside_every_group_and_the_empty_values_below(tmp_path):
    conn = shop(tmp_path)
    down = ads_tree.build(conn, TODAY, 7, ("account", "ad"), sort="contact_cost", desc=True)
    one = groups(down)["Аккаунт 1"]
    # cost of a contact: 11 → 30, 12 → 20, 14 → 40, 13 and 15 have no contacts (a dash)
    assert [r["ad"]["avito_id"] for r in children(down, one)] == [14, 11, 12, 13, 15]
    up = ads_tree.build(conn, TODAY, 7, ("account", "ad"), sort="contact_cost", desc=False)
    assert [r["ad"]["avito_id"] for r in children(up, groups(up)["Аккаунт 1"])] == [12, 11, 14, 13, 15]
    assert [r["label"] for r in up["rows"] if r["depth"] == 0] == ["Аккаунт 2", "Аккаунт 1"]             # 200/7 against 515/16
    assert ads_tree.build(conn, TODAY, 7, ("ad",), sort="bad")["sort"] == "spend"


# ---- the formulas --------------------------------------------------------------------------------------------------------------------

def test_the_formulas_of_the_funnel():
    m = ads_tree.metrics({"spend": 500.0, "views": 200, "contacts": 8, "pieces": 4, "orders": 2, "revenue": 20000.0, "margin": 4000.0})
    assert (m["view_cost"], m["contact_conv"], m["contact_cost"], m["sale_conv"], m["sale_cost"]) == (2.5, 4.0, 62.5, 25.0, 125.0)
    assert (m["profit"], m["drr"], m["roi"]) == (3500.0, 2.5, 700.0)
    loss = ads_tree.metrics({"spend": 500.0, "views": 0, "contacts": 0, "pieces": 0, "orders": 0, "revenue": 0.0, "margin": 0.0})
    assert [loss[k] for k in ("view_cost", "contact_conv", "contact_cost", "sale_conv", "sale_cost", "drr")] == [None] * 6
    assert (loss["profit"], loss["roi"]) == (-500.0, -100.0)
    free = ads_tree.metrics({"spend": 0.0, "views": 10, "contacts": 1, "pieces": 1, "orders": 1, "revenue": 100.0, "margin": 30.0})
    assert free["roi"] is None and free["view_cost"] == 0.0 and free["profit"] == 30.0 and free["drr"] == 0.0
    assert [c[0] for c in ads_tree.COLUMNS] == ["spend", "views", "view_cost", "contacts", "contact_conv", "contact_cost", "pieces", "sale_conv",
                                                "sale_cost", "revenue", "margin", "profit", "drr", "roi", "bid"]
    assert all(c[2] for c in ads_tree.COLUMNS)                                                              # every column explains itself


# ---- the previous period -------------------------------------------------------------------------------------------------------------

def test_the_change_against_the_previous_period_of_the_same_length(tmp_path):
    conn = shop(tmp_path)
    spend(conn, 1, 11, "2026-11-02", 100, views=10, listed=False)                    # the previous 7 days: 27.10 – 02.11
    spend(conn, 2, 21, "2026-10-28", 400, views=10, listed=False)
    won_order(conn, day="2026-11-01", items=((2, 5000.0, 4000.0),), chats=[(1, "11")])
    conn.commit()
    data = ads_tree.build(conn, TODAY, 7, ("account", "ad"))
    assert (data["prev_since"], data["prev_until"]) == ("2026-10-28", "2026-11-03")
    total = data["total"]["delta"]
    assert total["spend"] == round((715 - 500) / 500 * 100, 1) and total["pieces"] == 200.0
    assert total["drr"] == round((2.8 - 5.0) / 5.0 * 100, 1)                        # ДРР 2,8 % (715 / 26000) against 5 %
    top = groups(data)
    assert top["Аккаунт 1"]["delta"]["spend"] == 415.0 and top["Аккаунт 2"]["delta"]["spend"] == -50.0
    assert top["Аккаунт 2"]["delta"]["pieces"] is None                                  # nothing sold before: no change to tell
    assert all(r["delta"] is None for r in data["rows"] if r["depth"] > 0)
    assert ads_tree.change(5, 0) is None and ads_tree.change(None, 3) is None and ads_tree.change(3, 4) == -25.0


# ---- the filters and the columns -----------------------------------------------------------------------------------------------------

def test_the_filter_keeps_the_ads_with_sales_and_the_columns_empty_everywhere(tmp_path):
    conn = shop(tmp_path)
    data = ads_tree.build(conn, TODAY, 7, ("ad",), only="sales")
    assert sorted(r["ad"]["avito_id"] for r in data["rows"]) == [11, 22]
    assert data["total"]["m"]["spend"] == 300.0
    assert "bid" not in data["filled"] and {"spend", "pieces", "roi"} <= data["filled"]
    assert ads_tree.build(conn, TODAY, 7, ("ad",), only="bad")["only"] == ""


def test_the_columns_and_the_levels_are_kept_for_the_person(tmp_path):
    conn = db(tmp_path)
    assert ads_tree.hidden_columns(conn, "owner") == list(ads_tree.HIDDEN_DEFAULT)
    ads_tree.save_columns(conn, "owner", ["spend", "roi", "nonsense"], "2026-11-12T10:00:00")
    assert set(ads_tree.hidden_columns(conn, "owner")) == {c[0] for c in ads_tree.COLUMNS} - {"spend", "roi"}
    assert ads_tree.saved_levels(conn, "owner") == ads_tree.DEFAULT_LEVELS
    ads_tree.save_levels(conn, "owner", ("brand", "width"), "2026-11-12T10:00:00")
    assert ads_tree.saved_levels(conn, "owner") == ("brand", "width") and ads_tree.saved_levels(conn, "partner") == ads_tree.DEFAULT_LEVELS
