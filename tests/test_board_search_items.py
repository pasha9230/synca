"""The search of the board «Заказы» finds an order by any of its positions and by a size written in any way (the owner 08.11, the prototype
of the audit: «Клиент, телефон, № заказа, позиция, размер»); a query of digits only stays a number (orders.board_matches)."""
from __future__ import annotations

from datetime import datetime

import pytest

from tires_sync import orders, store

NOW = "2026-11-08T12:00:00"


@pytest.fixture
def conn(tmp_path):
    c = store.connect(str(tmp_path / "state.db"))
    orders.ensure_pipeline(c, NOW)
    return c


def order(conn, client, items, webmim=""):
    cid = orders.create_card(conn, {"client_name": client}, NOW, "o")
    for name, qty in items:
        orders.add_item(conn, cid, goods_id="1", name=name, brand_key="nokian", season="winter", diameter=15, territory="1", qty=qty,
                        sale_unit=6000, supplier_price=5000, terms=None, fee_pct=0, promo_pct=0, now=NOW, author="o")
    if webmim:
        conn.execute("UPDATE order_card SET webmim_number = ? WHERE id = ?", (webmim, cid))
    conn.commit()
    return cid


def found(conn, query):
    data = orders.board(conn, datetime(2026, 11, 8, 12), query=query)
    return {c["id"] for col in data["columns"] for c in col["cards"]}


@pytest.fixture
def three(conn):
    return dict(two=order(conn, "Иван", [("Nokian Hakkapeliitta 10 205/55 R16", 4), ("Ikon Nordman 7 185/65 R15", 4)]),
                one=order(conn, "Пётр", [("Pirelli Ice Zero 225/45 R17", 4)], webmim="1856515"),
                none=order(conn, "Ольга", []))


@pytest.mark.parametrize("query", ["Nordman", "nordman 7", "185/65 R15", "185/65R15", "185 65 15", "185/65 r15", "Nordman 7 185/65"])
def test_the_second_position_and_its_size_find_the_order(conn, three, query):
    assert found(conn, query) == {three["two"]}


def test_the_first_position_still_finds_it(conn, three):
    assert found(conn, "Hakkapeliitta") == {three["two"]}
    assert found(conn, "205/55 R16") == {three["two"]}


def test_digits_only_stay_a_number_never_a_size(conn, three):
    assert found(conn, "1856515") == {three["one"]}                 # the WebMIM number, not the size 185/65 R15
    assert found(conn, "№%d" % three["none"]) == {three["none"]}


def test_a_size_written_otherwise_finds_no_other_size(conn, three):
    assert found(conn, "185/55 R15") == set()
    assert found(conn, "225 45 17") == {three["one"]}
    assert found(conn, "Ольга") == {three["none"]}
