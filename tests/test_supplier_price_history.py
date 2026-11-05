"""История цены поставщика и своя цена позиции, которой нет у поставщика (слово владельца 2026-11-05): «Если ранее была у поставщика, мы цену свою
все равно можем выставить, когда она пропала из наличия, так же можно вести историю как менялась цена у поставщика. Если у поставщика в наличии
не было, то мы менять не можем цену». История: запись только при изменении закупки, первый цикл только запоминает, видит её тот, кому можно
видеть закупку (право «money»). Своя цена сама объявление не создаёт."""
from __future__ import annotations

import sqlite3

from helpers import make_offer
from panel_helpers import login, post
from test_manual_prices import GONE, _flash, _gone_app, _owner
from test_oth_feed import ads_of, public, run
from test_panel_stale_ux import app_of, stale_setup
from test_roles import person
from test_stock_pass import AFTER, FIVE, prepared
from tires_sync import catalog_archive, store
from tires_sync.webmim import ParseResult


def _cycle(conn, at, **prices):
    catalog_archive.record(conn, ParseResult(offers={g: make_offer(goods_id=g, base_price=p) for g, p in prices.items()}), at)


def _history(conn, goods_id):
    return [(r["at"], r["was"], r["price"]) for r in catalog_archive.price_history(conn, goods_id)[0]]


# --- позиция, которой никогда не было у поставщика ------------------------------------------------------------------------------------

def test_a_position_never_at_the_supplier_has_the_own_price_closed_with_a_clear_reason(tmp_path):
    stale_setup(tmp_path)                                                    # s2-5: старый файл второго поставщика, только для просмотра
    conn = store.connect(tmp_path / "t.db")
    assert catalog_archive.item_info(conn, "s2-5") is None
    first = conn.execute("SELECT id FROM account ORDER BY id").fetchone()[0]
    conn.close()
    client = app_of(tmp_path).test_client()
    tok = login(client)
    page = client.get("/catalog/item/s2-5").get_data(as_text=True)
    assert "data-own-closed" in page and "никогда не было у поставщика" in page
    assert 'action="/catalog/item/s2-5/own-price"' not in page
    r = post(client, "/catalog/item/s2-5/own-price", tok, kind="price", value="4500", a=[str(first)], action="save")
    assert "никогда не было у поставщика" in _flash(client, r)
    conn = store.connect(tmp_path / "t.db")
    from tires_sync import staging
    assert not (staging.summary(conn)["changes"] or {}).get("price_overrides")


def test_the_closed_note_is_not_shown_to_a_role_without_the_purchase(tmp_path):
    stale_setup(tmp_path)
    client = person(app_of(tmp_path), "chats")[0]
    page = client.get("/catalog/item/s2-5").get_data(as_text=True)
    assert "data-own-closed" not in page
