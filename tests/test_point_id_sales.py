"""Sales and the point's fixed sum are tied to the point by its id (the owner 2026-10-26): renaming a point never splits its money.
`sale.point` keeps the name at the time of the sale (history); a sale whose point is gone shows that name."""
from __future__ import annotations

import sqlite3

import pytest

import order_report_fixture as fixture
from test_order_report_rates import GOLDEN
from tires_sync import order_points as points, order_report, orders, sales, store

NOW = fixture.NOW
OLD_SALE = """CREATE TABLE sale (
    id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL, goods_id TEXT NOT NULL, brand_key TEXT NOT NULL, season TEXT NOT NULL,
    diameter INTEGER NOT NULL, name TEXT NOT NULL, ad_kind TEXT NOT NULL, pieces INTEGER NOT NULL, price_total REAL NOT NULL,
    sale_total REAL NOT NULL, cost_total REAL NOT NULL, bonus_total REAL NOT NULL, monthly_total REAL NOT NULL, fee_total REAL NOT NULL,
    promo_total REAL NOT NULL, margin_total REAL NOT NULL, account_id INTEGER, point TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'manual', created TEXT NOT NULL, author TEXT NOT NULL, reversed_at TEXT, reversed_by TEXT)"""


def _old_row(conn, point, note="Заказ №1", source="order"):
    conn.execute("INSERT INTO sale (day, goods_id, brand_key, season, diameter, name, ad_kind, pieces, price_total, sale_total, cost_total,"
                 " bonus_total, monthly_total, fee_total, promo_total, margin_total, point, note, source, created, author)"
                 " VALUES ('2026-10-10', 'g', 'nokian', 'winter', 16, 'N', 'single', 4, 16000, 20000, 16000, 0, 0, 0, 0, 4000, ?, ?, ?, ?, 'o')",
                 (point, note, source, NOW))


def test_the_migration_adds_the_column_fills_it_by_the_exact_name_and_a_second_run_changes_nothing(tmp_path):
    path = str(tmp_path / "old.db")
    raw = sqlite3.connect(path)
    raw.execute(OLD_SALE)
    raw.execute("CREATE TABLE point (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE)")
    raw.executemany("INSERT INTO point (id, name) VALUES (?, ?)", [(3, "Альфа"), (7, "Бета")])
    for point in ("Альфа", "Бета", "бета", "Бета ", "Гамма", ""):
        _old_row(raw, point)
    _old_row(raw, "Альфа", note="вручную", source="manual")
    raw.commit()
    raw.close()
    conn = store.connect(path)
    assert "point_id" in {r[1] for r in conn.execute("PRAGMA table_info(sale)")}
    got = conn.execute("SELECT point, point_id FROM sale ORDER BY id").fetchall()
    assert got == [("Альфа", 3), ("Бета", 7), ("бета", None), ("Бета ", None), ("Гамма", None), ("", None), ("Альфа", 3)]
    assert conn.execute("SELECT COUNT(*) FROM sale").fetchone()[0] == 7                            # no row lost
    conn.execute("INSERT INTO point (name) VALUES ('Гамма')")                                       # a point named later is not tied backwards
    conn.commit()
    conn.close()
    again = store.connect(path)
    assert again.execute("SELECT point, point_id FROM sale ORDER BY id").fetchall() == got


def test_a_new_base_has_the_column(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    assert "point_id" in {r[1] for r in conn.execute("PRAGMA table_info(sale)")}


def test_a_sale_from_an_order_keeps_the_id_of_the_orders_point(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    orders.ensure_pipeline(conn, NOW)
    pid = points.save_point(conn, {"name": "Бета", "channel": "panel"})
    card = orders.create_card(conn, {"client_name": "К", "point_id": pid}, NOW, "o")
    won = next(s for s in orders.list_stages(conn) if s["kind"] == "won")
    conn.execute("INSERT INTO order_item (order_id, goods_id, name, qty, supplier_price, sale_unit) VALUES (?, 'g1', 'N', 4, 4000, 5000)", (card,))
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (won["id"], card))
    conn.commit()
    ids = orders.record_sales(conn, card, "2026-10-10", NOW, "o", "2026-10-20")
    assert conn.execute("SELECT point, point_id FROM sale WHERE id = ?", (ids[0],)).fetchone() == ("Бета", pid)


def test_a_manual_sale_gets_the_id_of_the_point_with_exactly_that_name(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    pid = points.save_point(conn, {"name": "Бета", "channel": "panel"})
    a = fixture._sale(conn, "2026-10-10", "Бета", "вручную", "manual", 20000, 4000, 4, 1)
    b = fixture._sale(conn, "2026-10-10", "Склад", "вручную", "manual", 20000, 4000, 4, 2)
    assert [conn.execute("SELECT point_id FROM sale WHERE id = ?", (i,)).fetchone()[0] for i in (a, b)] == [pid, None]


def _with_points(tmp_path, names=("Альфа", "Бета", "Гамма")):
    conn = store.connect(str(tmp_path / "x.db"))
    ids = {n: points.save_point(conn, {"name": n, "channel": "panel"}) for n in names}
    fixture.fill(conn)
    return conn, ids


@pytest.mark.parametrize("period", fixture.PERIODS)
def test_with_the_points_in_the_base_the_report_is_the_same_to_the_kopeck(tmp_path, period):
    conn, _ = _with_points(tmp_path)
    rep = order_report.build(conn, *period)
    assert {"lines": rep["lines"], "total": rep["total"]} == GOLDEN[period]


def test_a_renamed_point_is_one_line_with_its_new_name_and_the_totals_do_not_change(tmp_path):
    conn, ids = _with_points(tmp_path)
    before = order_report.build(conn, "2026-08-01", "2026-10-31")
    points.save_point(conn, {"name": "Бета-2", "channel": "panel"}, ids["Бета"])
    fixture._sale(conn, "2026-10-20", "Бета-2", "Заказ №900", "order", 20000, 4000, 4, 900)       # a sale after the rename
    conn.execute("UPDATE sale SET point_id = ? WHERE note = 'Заказ №900'", (ids["Бета"],))
    conn.commit()
    after = order_report.build(conn, "2026-08-01", "2026-10-31")
    names = [r["point"] for r in after["lines"]]
    assert "Бета" not in names and names.count("Бета-2") == 1
    old = next(r for r in before["lines"] if r["point"] == "Бета")
    new = next(r for r in after["lines"] if r["point"] == "Бета-2")
    assert (new["orders"], new["sale"], new["margin"], new["fee"]) == (old["orders"] + 1, round(old["sale"] + 20000, 2),
                                                                         round(old["margin"] + 4000, 2), old["fee"] + 75)
    assert after["total"]["sale"] == round(before["total"]["sale"] + 20000, 2)
    assert {"Бета-2"} == {r["point"] for r in sales.list_sales(conn, point="Бета-2")} and not sales.list_sales(conn, point="Бета")
    assert order_report.build(conn, "2026-08-01", "2026-10-31", point="Бета-2")["lines"][0]["sale"] == new["sale"]


def test_the_fixed_sum_of_a_handed_over_client_stays_in_the_renamed_points_line(tmp_path):
    conn, ids = _with_points(tmp_path)
    order_report.set_rates(conn, "2026-10-01", "2026-10-01", NOW, "owner", handoff_fee=50)
    conn.execute("INSERT INTO order_handoff (order_id, message_id, point_id, created_at) VALUES (301, 'vk-1', ?, ?)", (ids["Гамма"], NOW))
    conn.commit()
    before = order_report.build(conn, "2026-10-01", "2026-10-31")
    points.save_point(conn, {"name": "Гамма-2", "channel": "panel"}, ids["Гамма"])
    after = order_report.build(conn, "2026-10-01", "2026-10-31")
    assert [r["point"] for r in after["lines"]].count("Гамма-2") == 1 and "Гамма" not in [r["point"] for r in after["lines"]]
    old = next(r for r in before["lines"] if r["point"] == "Гамма")
    new = next(r for r in after["lines"] if r["point"] == "Гамма-2")
    assert {k: v for k, v in new.items() if k != "point"} == {k: v for k, v in old.items() if k != "point"}
    assert after["total"] == before["total"]
    assert after["handoff"]["lines"] == [{"point": "Гамма-2", "clients": 1, "fee": 50.0}]


def test_a_sale_of_a_point_that_is_gone_shows_its_old_name(tmp_path):
    conn, ids = _with_points(tmp_path)
    before = order_report.build(conn, "2026-08-01", "2026-10-31")
    conn.execute("DELETE FROM point WHERE id = ?", (ids["Гамма"],))
    conn.commit()
    after = order_report.build(conn, "2026-08-01", "2026-10-31")
    assert {"lines": after["lines"], "total": after["total"]} == {"lines": before["lines"], "total": before["total"]}
    assert [r["point"] for r in after["lines"]].count("Гамма") == 1
