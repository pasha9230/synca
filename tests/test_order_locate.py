"""The position an order takes is found by the supplier's data (the owner 2026-11-24, a Kumho WS-31 whose code moved): its code in today's unload,
else the exact supplier's name in it, else the permanent base of positions; never a guess, several of one name are never taken."""
from __future__ import annotations

import pytest

from helpers import make_offer
from tires_sync import catalog_archive as archive, order_search, store
from tires_sync.panel.routes_orders import point_no_webmim
from tires_sync.webmim import ParseResult

T1 = "2026-11-20T10:00:00"
NAME = "Kumho 275/65/17 T 115 WS-31 SUV Ш"


def parsed(*offers):
    return ParseResult(offers={o.goods_id: o for o in offers})


def kumho(goods_id, **kw):
    return make_offer(goods_id=goods_id, name=kw.pop("name", NAME.upper()), name_orig=kw.pop("name_orig", NAME), **kw)


def test_the_code_of_today_is_taken_first(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    found = order_search.locate(conn, parsed(kumho("x-new"), make_offer(goods_id="1")), "1", [NAME])
    assert found.how == "id" and found.offer.goods_id == "1" and order_search.notes(found, "chat") == []


def test_a_code_the_unload_lost_is_found_by_the_exact_supplier_name_case_and_spaces_aside(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    today = parsed(kumho("x-new", stock=4), kumho("x-other", name="KUMHO 275/65/17 T 115 WS-31 SUV Ш XL", name_orig="Kumho 275/65/17 T 115 WS-31 SUV Ш XL"))
    found = order_search.locate(conn, today, "x-old", ["  kumho  275/65/17 t 115 ws-31 suv ш "])
    assert found.how == "name" and found.offer.goods_id == "x-new"
    [(kind, text, level)] = order_search.notes(found, "chat")
    assert kind == "chat" and level == "grey" and text.startswith("Позиция найдена по названию поставщика: «%s» (код x-new)" % NAME)
    assert "Искали: код x-old" in text


def test_the_same_name_twice_in_the_unload_is_not_taken_and_the_variants_are_named(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    with pytest.raises(order_search.NotLocated) as err:
        order_search.locate(conn, parsed(kumho("x-a"), kumho("x-b")), "x-old", [NAME])
    text = str(err.value)
    assert text.startswith("по названию нашлось позиций: 2, ни одна не взята") and "(код x-a)" in text and "(код x-b)" in text


def test_a_position_only_in_the_base_is_taken_from_it_by_its_code(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    archive.record(conn, parsed(kumho("x-old", stock=6, base_price=8500.0)), T1)
    found = order_search.locate(conn, parsed(make_offer(goods_id="1")), "x-old", [""])
    assert found.how == "base" and found.offer.goods_id == "x-old" and found.info["last_seen"] == T1 and found.info["last_stock"] == 6
    [(kind, text, level)] = order_search.notes(found, "item")
    lines = text.split("\n")
    assert lines[0] == "Позиция взята из базы позиций: в выгрузке поставщика сегодня её нет."
    assert "последний раз 2026-11-20, остаток тогда 6 шт." in lines[1] and "8500" not in "\n".join(lines[:4])     # the point of it first
    assert "Цена закупки из базы позиций: 8500 ₽" in text and level == "yellow"
    assert kind == "webmim" and point_no_webmim({"kind": kind, "text": text})                         # the office's only: never the point's


def test_a_base_position_without_a_sale_price_says_so_clearly(tmp_path, monkeypatch):
    from tires_sync import order_offers, orders

    def no_price(*a, **k):
        raise orders.OrderError("Для этой позиции не рассчитана цена продажи: задайте процент бренда.")
    monkeypatch.setattr(order_offers, "offer_args", no_price)
    conn = store.connect(str(tmp_path / "x.db"))
    archive.record(conn, parsed(kumho("x-old")), T1)
    found = order_search.locate(conn, parsed(make_offer(goods_id="1")), "x-old", [])
    with pytest.raises(order_search.NotLocated) as err:
        order_search.located_args(conn, None, None, None, found, "2026-11-24")
    assert order_search.first_line(err.value) == ("Позиция «%s» (код x-old) есть только в базе позиций, цены продажи для неё нет "
                                                  "(для этой позиции не рассчитана цена продажи: задайте процент бренда)." % NAME)


def test_a_position_only_in_the_base_is_found_by_its_exact_name(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    archive.record(conn, parsed(kumho("x-old", stock=2)), T1)
    found = order_search.locate(conn, parsed(make_offer(goods_id="1")), "x-moved", [NAME.lower()])
    assert found.how == "base" and found.offer.goods_id == "x-old"


def test_several_of_one_name_in_the_base_are_not_taken(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    archive.record(conn, parsed(kumho("x-a"), kumho("x-b")), T1)
    with pytest.raises(order_search.NotLocated) as err:
        order_search.locate(conn, parsed(make_offer(goods_id="1")), "", [NAME])
    assert "в базе позиций с таким названием их 2" in str(err.value) and "(код x-a)" in str(err.value)


def test_nothing_found_says_what_was_searched_and_where(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    with pytest.raises(order_search.NotLocated) as err:
        order_search.locate(conn, parsed(make_offer(goods_id="1")), "x-old", [NAME])
    lines = str(err.value).split("\n")
    assert lines[0] == "этой позиции нет в выгрузке поставщика и нет в базе позиций."
    assert lines[1] == "Искали: код x-old; название «%s»." % NAME and lines[2] == "В выгрузке сегодня: нет. В базе позиций: нет."


def test_a_typed_exact_supplier_name_is_found_though_a_longer_name_holds_every_word(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    today = parsed(kumho("x-new", stock=0), kumho("x-xl", name="KUMHO 275/65/17 T 115 WS-31 SUV Ш XL", name_orig="Kumho 275/65/17 T 115 WS-31 SUV Ш XL"))
    offer, count = order_search.typed(conn, today, None, NAME)
    assert offer.goods_id == "x-new" and count == 1
