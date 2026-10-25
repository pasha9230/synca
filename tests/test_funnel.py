"""The card funnel: views → contacts → sales → margin per position, and the first stage where it drops."""
from __future__ import annotations

from datetime import date

import pytest

from panel_helpers import login, make_app
from tires_sync import funnel, store

TODAY = date(2026, 10, 12)


@pytest.mark.parametrize("views,contacts,sold,days,code,reliable", [
    (500, 50, 0, 3, "watching", False),          # too early whatever the numbers
    (500, 50, 2, 3, "watching", False),
    (500, 50, 1, 10, "selling", True),
    (5, 0, 0, 10, "few_views", False),
    (19, 0, 0, 10, "few_views", False),
    (100, 1, 0, 10, "few_contacts", True),       # 1 % < 2 %
    (100, 2, 0, 10, "ok", True),                 # exactly 2 %
    (100, 3, 0, 10, "no_sales", True),
    (8, 3, 0, 10, "no_sales", True),             # few views but they write: the card works, the problem is after the contact
    (0, 0, 0, 10, "few_views", False),           # no division by zero
])
def test_the_verdict_names_the_first_stage_that_drops(views, contacts, sold, days, code, reliable):
    assert funnel.diagnose(views, contacts, sold, days) == (code, reliable)


def test_a_set_of_four_belongs_to_the_same_position():
    assert funnel.base_goods_id("123-x4") == "123" and funnel.base_goods_id("123") == "123" and funnel.base_goods_id("") == ""


def seed(conn):
    conn.executemany("INSERT INTO avito_ad (account_id, avito_id, ad_id, title, price, status, seen_at, first_seen) VALUES (?,?,?,?,?,?,?,?)", [
        (1, 11, "G1", "Шина G1", 5000, "active", "2026-10-12T04:20:00", "2026-10-01T04:20:00"),
        (1, 12, "G1-x4", "Шина G1 (4 шт.)", 20000, "active", "2026-10-12T04:20:00", "2026-10-01T04:20:00"),
        (1, 21, "G2", "Шина G2", 6000, "active", "2026-10-12T04:20:00", "2026-10-11T04:20:00"),
        (1, 31, "G3", "Шина G3", 7000, "closed", "2026-10-12T04:20:00", "2026-10-01T04:20:00"),
        (1, 41, None, "Без сопоставления", 1000, "active", "2026-10-12T04:20:00", "2026-10-01T04:20:00"),
        (1, 51, "G5", "Шина G5", 8000, "active", "2026-10-12T04:20:00", "2026-10-01T04:20:00")])
    conn.executemany("INSERT INTO ad_stat VALUES (?,?,?,?,?,?)", [
        (1, 11, "2026-10-10", 60, 2, 3), (1, 12, "2026-10-10", 40, 3, 0), (1, 12, "2026-09-01", 999, 99, 99),      # the old day is outside
        (1, 21, "2026-10-12", 50, 5, 1), (1, 31, "2026-10-10", 70, 7, 7), (1, 51, "2026-10-10", 100, 1, 0)])
    conn.executemany(
        "INSERT INTO sale (day, goods_id, brand_key, season, diameter, name, ad_kind, pieces, price_total, sale_total, cost_total, bonus_total,"
        " monthly_total, fee_total, promo_total, margin_total, created, author, reversed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
            ("2026-10-11", "G1", "b", "winter", 16, "n", "single", 4, 0, 20000, 0, 0, 0, 0, 0, 3000.0, "t", "o", None),
            ("2026-10-11", "G1", "b", "winter", 16, "n", "single", 4, 0, 99999, 0, 0, 0, 0, 0, 9999.0, "t", "o", "2026-10-11T12:00:00")])    # reversed
    conn.commit()


def test_rows_group_a_position_with_its_set_and_count_only_live_sales_and_the_chosen_period(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    seed(conn)
    items = {r["goods_id"]: r for r in funnel.rows(conn, "2026-10-06", "2026-10-12", TODAY)}
    assert set(items) == {"G1", "G2", "G5"}                       # closed and unmapped ads are not here
    g1 = items["G1"]
    assert (g1["views"], g1["contacts"], g1["favorites"]) == (100, 5, 3) and g1["title"] == "Шина G1" and g1["price"] == 5000
    assert (g1["pieces"], g1["margin"], g1["code"]) == (4, 3000.0, "selling")
    assert items["G2"]["code"] == "watching" and items["G2"]["observed_days"] == 2
    assert items["G5"]["code"] == "few_contacts" and items["G5"]["reliable"] is True
    totals = funnel.summarize(list(items.values()))
    assert (totals["views"], totals["contacts"], totals["pieces"], totals["margin"]) == (250, 11, 4, 3000.0)
    assert totals["counts"]["selling"] == 1 and totals["counts"]["watching"] == 1


def test_the_table_lists_the_most_urgent_problems_first(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    seed(conn)
    codes = [r["code"] for r in funnel.rows(conn, "2026-10-06", "2026-10-12", TODAY)]
    assert codes == sorted(codes, key=funnel.CODES.index)


def test_existing_ads_get_a_first_seen_date_when_the_column_appears(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.db")
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE avito_ad (account_id INTEGER NOT NULL, avito_id INTEGER NOT NULL, ad_id TEXT, title TEXT NOT NULL DEFAULT '', "
                "price INTEGER, status TEXT NOT NULL DEFAULT '', seen_at TEXT NOT NULL, PRIMARY KEY (account_id, avito_id))")
    raw.execute("INSERT INTO avito_ad VALUES (1, 5, 'G', 't', 1, 'active', '2026-10-12T04:20:00')")
    raw.commit()
    raw.close()
    conn = store.connect(path)
    assert conn.execute("SELECT first_seen FROM avito_ad").fetchone()[0] == "2026-10-12T04:20:00"


def page(tmp_path, with_data):
    app = make_app(tmp_path, NOW=lambda: __import__("datetime").datetime(2026, 10, 12, 12, 0, 0))
    client = app.test_client()
    login(client)
    if with_data:
        seed(store.connect(app.config["DB_PATH"]))
    return client


def test_the_funnel_page_explains_an_empty_state_and_shows_the_numbers(tmp_path):
    empty = page(tmp_path, False).get("/economics/funnel").get_data(as_text=True)
    assert "Статистики ещё нет" in empty and 'href="/economics/funnel"' in empty
    (tmp_path / "d").mkdir()
    client = page(tmp_path / "d", True)
    html = client.get("/economics/funnel").get_data(as_text=True)
    assert "Шина G1" in html and "Шина G5" in html and "Смотрят, но не пишут" in html and "Набирается статистика" in html
    assert "Шина G3" not in html and "Без сопоставления" not in html
    assert 'class="is-active" aria-current="page">Воронка<' in html
    only = client.get("/economics/funnel?c=few_contacts").get_data(as_text=True)
    assert "Шина G5" in only and "Шина G1" not in only
    assert client.get("/economics/funnel?p=abc&c=zzz&pg=x").status_code == 200
    assert "за 7 дней" in client.get("/economics/funnel?p=7").get_data(as_text=True)
    assert app_redirects_when_anonymous(client)


def app_redirects_when_anonymous(client):
    return client.application.test_client().get("/economics/funnel").status_code == 302
