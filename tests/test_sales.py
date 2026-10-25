from __future__ import annotations

import pytest

from tires_sync import sales, store

NOW = "2026-10-09T10:00:00"
GISLAVED = {"markdown_pct": -8.5, "discount_pct": 0.0, "bonus_pct": 13.0, "monthly_pct": 10.0}


@pytest.fixture
def conn(tmp_path):
    return store.connect(tmp_path / "t.db")


def add(conn, **kw):
    base = dict(day="2026-10-08", goods_id="100", brand_key="GISLAVED", season="summer", diameter=16, name="Gislaved 205/55 R16",
                ad_kind="single", pieces=4, sale_total=36600.0, supplier_price=10000.0, terms=GISLAVED, fee_pct=0.0, promo_pct=0.0,
                account_id=None, point="", note="", today="2026-10-09")
    base.update(kw)
    return sales.add_sale(conn, now=NOW, author="owner", **base)


def test_a_sale_freezes_the_money_of_the_moment(conn):
    sid = add(conn)                                             # 4 tires, 9150 each: the owner's example times four
    row = sales.list_sales(conn)[0]
    assert row["id"] == sid and row["pieces"] == 4
    assert (row["price_total"], row["sale_total"], row["cost_total"]) == (40000.0, 36600.0, 40000.0)
    assert (row["bonus_total"], row["monthly_total"], row["margin_total"]) == (5200.0, 4000.0, 5800.0)
    assert [e["action"] for e in store.list_audit(conn, 5)] == ["sale_added"]


def test_the_actual_price_may_differ_from_the_list_price_and_costs_of_avito_are_taken_from_what_was_paid(conn):
    add(conn, sale_total=40000.0, fee_pct=5, promo_pct=2)         # sold 4 for 40000: no markdown this time
    row = sales.list_sales(conn)[0]
    assert row["fee_total"] == 2000.0 and row["promo_total"] == 800.0 and row["margin_total"] == 9200.0 - 2800.0


@pytest.mark.parametrize("bad", [
    {"pieces": 0}, {"pieces": 101}, {"sale_total": 0}, {"sale_total": -5}, {"sale_total": float("nan")}, {"sale_total": 99999999},
    {"day": "08.10.2026"}, {"day": "2026-10-10"}, {"ad_kind": "bundle"}, {"season": "spring"}, {"goods_id": ""}, {"supplier_price": 0},
    {"pieces": 3, "ad_kind": "set4"}, {"point": "x" * 80},
])
def test_bad_sales_are_refused(conn, bad):
    with pytest.raises(ValueError):
        add(conn, **bad)
    assert sales.list_sales(conn) == []


def test_a_set_of_four_is_sold_in_multiples_of_four(conn):
    add(conn, ad_kind="set4", pieces=8, sale_total=73200.0)
    assert sales.list_sales(conn)[0]["ad_kind"] == "set4"


def test_a_reversed_sale_leaves_the_facts_and_stays_in_the_journal(conn):
    first, second = add(conn), add(conn, day="2026-10-07", sale_total=36000.0)
    sales.reverse_sale(conn, first, NOW, "owner")
    assert [r["id"] for r in sales.list_sales(conn)] == [second]
    assert [r["id"] for r in sales.list_sales(conn, include_reversed=True)] == [first, second]
    with pytest.raises(ValueError):
        sales.reverse_sale(conn, first, NOW, "owner")
    with pytest.raises(ValueError):
        sales.reverse_sale(conn, 999, NOW, "owner")
    assert store.list_audit(conn, 10)[0]["action"] == "sale_reversed"


def test_period_brand_account_and_point_filters(conn):
    add(conn, day="2026-09-01")
    add(conn, day="2026-10-05", brand_key="NEXEN", account_id=1, point="Бета")
    add(conn, day="2026-10-08", account_id=2, point="Гамма")
    assert len(sales.list_sales(conn, start="2026-10-01", end="2026-10-31")) == 2
    assert [r["brand_key"] for r in sales.list_sales(conn, start="2026-10-01", end="2026-10-31", brand="NEXEN")] == ["NEXEN"]
    assert len(sales.list_sales(conn, account_id=2)) == 1 and len(sales.list_sales(conn, point="Бета")) == 1


def test_totals_breakdowns_and_series(conn):
    add(conn, day="2026-10-05")                                              # margin 5800
    add(conn, day="2026-10-06", season="winter", diameter=19, sale_total=20000.0, pieces=2)
    add(conn, day="2026-09-20", brand_key="NEXEN")
    rows = sales.list_sales(conn)
    total = sales.summarize(rows)
    assert total["sales"] == 3 and total["pieces"] == 10 and total["margin_pct_price"] == round(100 * total["margin"] / total["price"], 2)
    by_season = {r["key"]: r for r in sales.breakdown(rows, "season")}
    assert by_season["summer"]["pieces"] == 8 and by_season["winter"]["pieces"] == 2
    assert [r["key"] for r in sales.breakdown(rows, "radius")] == ["R16", "R19"]
    assert [p["key"] for p in sales.series(rows, "day")] == ["2026-09-20", "2026-10-05", "2026-10-06"]
    assert [p["key"] for p in sales.series(rows, "month")] == ["2026-09", "2026-10"]
    assert [p["key"] for p in sales.series(rows, "week")] == ["2026-09-14", "2026-10-05"]       # weeks start on Monday
    assert sales.series(rows, "month")[1]["pieces"] == 6


def test_csv_export_neutralises_formulas(conn):
    add(conn, name="=HYPERLINK(\"x\")", note="+cmd")
    text = sales.export_csv(sales.list_sales(conn))
    assert text.startswith("﻿") and "'=HYPERLINK" in text and "'+cmd" in text
