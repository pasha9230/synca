"""«Заказы WebMIM» заказа (решения владельца 07.11, lane/order-webmim-list): фиксировать каждый оформленный заказ WebMIM, по которому шины
доставили на точку.
(1) блок в карточке: все заказы и перемещения заказа из нескольких источников — с ОТХ (номер заказа-покупки), свой заказ поставщика, свой
    остаток (без номеров); часть с ОТХ и часть у поставщика видны обе, «Оформить в WebMIM» для части поставщика остаётся;
(2) строка с ОТХ показывает номер источника, пока человек не вписал номер прямо для неё; вписанный на заказ номер её больше не подменяет;
(3) шины, добавленные в заказ с номером: вторая часть со своими № MIM и перемещением, старые подсвечены, своя строка таблицы;
(4) примечание цепочки: «с ОТХ, заказ №A, через №B»;
(5) объединённый заказ идёт за номером источника.
Таблица: одна строка на один заказ WebMIM; строки старых заказов не меняются. Реальных заказов WebMIM нет."""
from __future__ import annotations

import sqlite3

from tests.test_order_reserve import card_at, db, stage
from tests.test_oth_numbers import NUMBER, TRANSFER, source
from tests.test_oth_sale import LATER, T, line, new_order, take
from tires_sync import order_merge, order_sheet, order_webmim, orders, oth_manual, oth_sale, store, webmim_create

OWN, OWN_TRANSFER = "6100200", "26R-5550001"


def parts(conn, cid):
    return order_webmim.parts(conn, orders.get_card(conn, cid))


def rows(conn, cid):
    return order_sheet.card_rows(conn, orders.get_card(conn, cid), LATER)


def oth_item(conn, cid):
    return next(iter(oth_sale.taken_ids(conn, cid)))


# ---- (1) the block -------------------------------------------------------------------------------------------------------------------------

def test_part_from_oth_and_part_from_the_supplier_are_both_listed_and_the_button_stays(tmp_path):
    conn = db(tmp_path)
    old = source(conn)
    cid = new_order(conn, qty=6)
    assert take(conn, cid) == 4
    found = parts(conn, cid)
    oth = [p for p in found if p["kind"] == "oth"]
    mine = [p for p in found if p["kind"] == "supplier"]
    assert len(oth) == 1 and oth[0]["number"] == NUMBER and oth[0]["transfer"] == TRANSFER and oth[0]["source"] == old
    assert [x["qty"] for x in oth[0]["items"]] == [4]
    assert len(mine) == 1 and mine[0]["number"] == "" and [x["qty"] for x in mine[0]["items"]] == [2]
    button = webmim_create.button(conn, orders.get_card(conn, cid), "prod")
    assert button is not None and button["label"].startswith("Оформить")


def test_own_stock_of_the_point_is_a_part_without_numbers(tmp_path):
    conn = db(tmp_path)
    cid = new_order(conn, qty=2)
    oth_manual.set_qty(conn, "1", orders.get_card(conn, cid)["point_id"], 2, T, "o")      # «Свой остаток (ОТХ)» of the point
    [offer] = oth_sale.offers(conn, orders.get_card(conn, cid))[line(conn, cid)["id"]]
    oth_sale.take(conn, cid, line(conn, cid)["id"], offer["from_item"], LATER, "o")
    [stock] = [p for p in parts(conn, cid) if p["kind"] == "stock"]
    assert stock["number"] == "" and stock["transfer"] == "" and stock["day"] == "" and stock["source"] is None


def test_an_order_of_its_own_is_one_part_with_its_numbers(tmp_path):
    conn = db(tmp_path)
    cid = new_order(conn, qty=4)
    orders.update_card(conn, cid, {"webmim_number": OWN, "transfer_number": OWN_TRANSFER}, LATER, "o")
    [part] = parts(conn, cid)
    assert (part["kind"], part["number"], part["transfer"]) == ("supplier", OWN, OWN_TRANSFER) and part["items"][0]["qty"] == 4


# ---- (2) a number typed for the row ------------------------------------------------------------------------------------------------------------

def test_a_number_typed_on_the_order_no_longer_replaces_the_source_of_the_oth_row(tmp_path):
    conn = db(tmp_path)
    source(conn)
    cid = new_order(conn)
    take(conn, cid)
    orders.update_card(conn, cid, {"webmim_number": "РУКОЙ-1", "transfer_number": "РУКОЙ-П"}, LATER, "o")
    item = oth_item(conn, cid)
    row = rows(conn, cid)[order_sheet.key(cid, item)]
    assert (row["№ заказа MIM"], row["Перемещение MIM"]) == (NUMBER, TRANSFER)
    order_webmim.set_item_numbers(conn, cid, item, "5559999", "26R-777", LATER, "o")
    row = rows(conn, cid)[order_sheet.key(cid, item)]
    assert (row["№ заказа MIM"], row["Перемещение MIM"]) == ("5559999", "26R-777")
    [oth] = [p for p in parts(conn, cid) if p["kind"] == "oth"]
    assert oth["number"] == "5559999" and oth["typed"]


def test_only_a_position_from_oth_takes_a_number_of_its_own(tmp_path):
    import pytest
    conn = db(tmp_path)
    cid = new_order(conn)
    with pytest.raises(orders.OrderError):
        order_webmim.set_item_numbers(conn, cid, orders.get_card(conn, cid)["items"][0]["id"], "1", "", LATER, "o")


def test_an_old_base_keeps_the_numbers_typed_on_the_order_for_its_rows(tmp_path):
    """The migration: an order whose OTH row showed the number typed on the order (the old rule) keeps showing it."""
    conn = db(tmp_path)
    source(conn)
    cid = new_order(conn)
    take(conn, cid)
    orders.update_card(conn, cid, {"webmim_number": "РУКОЙ-1"}, LATER, "o")
    item = oth_item(conn, cid)
    conn.execute("DELETE FROM order_item_mim")
    conn.execute("DELETE FROM order_mim_base")
    conn.execute("DELETE FROM app_state WHERE key = ?", (order_webmim.MIGRATED,))
    conn.commit()
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    conn.close()
    again = store.connect(path)                                                # the migration runs from the schema, once
    assert rows(again, cid)[order_sheet.key(cid, item)]["№ заказа MIM"] == "РУКОЙ-1"
    assert again.execute("SELECT value FROM app_state WHERE key = ?", (order_webmim.MIGRATED,)).fetchone()
    store.connect(path).close()                                                # a second start changes nothing
    assert again.execute("SELECT COUNT(*) FROM order_item_mim").fetchone()[0] == 1


def test_a_base_without_the_new_tables_opens_and_reads_the_old_orders(tmp_path):
    conn = db(tmp_path)
    cid = new_order(conn)
    orders.update_card(conn, cid, {"webmim_number": OWN}, LATER, "o")
    before = rows(conn, cid)
    for table in ("order_item_mim", "order_mim_extra", "order_mim_base"):
        conn.execute("DROP TABLE %s" % table)
    conn.execute("DELETE FROM app_state WHERE key = ?", (order_webmim.MIGRATED,))
    conn.commit()
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    conn.close()
    again = store.connect(path)
    assert rows(again, cid) == before


# ---- (3) tyres added to an order with a number ---------------------------------------------------------------------------------------------------

def test_tyres_added_to_an_order_with_a_number_are_a_second_part_with_its_own_row(tmp_path):
    conn = db(tmp_path)
    cid = new_order(conn, qty=4)
    orders.update_card(conn, cid, {"webmim_number": OWN, "transfer_number": OWN_TRANSFER}, LATER, "o")
    item = orders.get_card(conn, cid)["items"][0]["id"]
    first = rows(conn, cid)
    assert list(first) == [order_sheet.key(cid, item)] and first[order_sheet.key(cid, item)]["Кол-во"] == 4
    orders.update_item(conn, item, 6, 6000, LATER, "o")                        # two more of the same tyre
    found = parts(conn, cid)
    extra = [p for p in found if p["kind"] == "extra"]
    assert len(extra) == 1 and extra[0]["number"] == "" and [x["qty"] for x in extra[0]["items"]] == [2] and extra[0]["needs_numbers"]
    assert [p for p in found if p["kind"] == "supplier"][0]["highlight"]          # the old numbers lit
    now = rows(conn, cid)
    assert now[order_sheet.key(cid, item)] == first[order_sheet.key(cid, item)]  # the written row: as it was
    assert now[order_sheet.extra_key(cid, item)]["Кол-во"] == 2 and now[order_sheet.extra_key(cid, item)]["№ заказа MIM"] == ""
    order_webmim.set_extra_numbers(conn, cid, item, "6100300", "26R-5550002", LATER, "o")
    now = rows(conn, cid)
    assert (now[order_sheet.extra_key(cid, item)]["№ заказа MIM"], now[order_sheet.extra_key(cid, item)]["Перемещение MIM"]) == ("6100300", "26R-5550002")
    assert now[order_sheet.key(cid, item)]["№ заказа MIM"] == OWN


def test_a_new_position_in_an_order_with_a_number_is_a_second_part(tmp_path):
    conn = db(tmp_path)
    cid = new_order(conn, qty=4)
    orders.update_card(conn, cid, {"webmim_number": OWN}, LATER, "o")
    parts(conn, cid)                                                           # the card was looked at with its number
    new = orders.add_item(conn, cid, goods_id="2", name="Шина 2", brand_key="b", season="winter", diameter=16, territory="1", qty=2,
                          sale_unit=6000, supplier_price=4500, terms=None, fee_pct=0, promo_pct=0, now=LATER, author="o")
    [extra] = [p for p in parts(conn, cid) if p["kind"] == "extra"]
    assert [x["id"] for x in extra["items"]] == [new]
    assert order_sheet.key(cid, new) not in rows(conn, cid) and order_sheet.extra_key(cid, new) in rows(conn, cid)


def test_an_order_without_a_number_has_no_second_part(tmp_path):
    conn = db(tmp_path)
    cid = new_order(conn, qty=4)
    orders.update_item(conn, orders.get_card(conn, cid)["items"][0]["id"], 6, 6000, LATER, "o")
    assert [p["kind"] for p in parts(conn, cid)] == ["supplier"]


# ---- (4) the note of a chain --------------------------------------------------------------------------------------------------------------------

def test_the_note_of_a_chain_names_the_order_of_purchase_and_the_one_between(tmp_path):
    conn = db(tmp_path)
    first = source(conn)
    middle = new_order(conn)
    take(conn, middle)
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (stage(conn, "oth"), middle))
    conn.commit()
    last = new_order(conn)
    oth_sale.take(conn, last, line(conn, last)["id"], oth_item(conn, middle), LATER, "o")
    row = rows(conn, last)[order_sheet.key(last, oth_item(conn, last))]
    assert row["Примечание"].startswith("с ОТХ, заказ №%d, через №%d" % (first, middle))
    assert row["№ заказа MIM"] == NUMBER                                         # the numbers: of A
    direct = new_order(conn)
    source(conn, number="5111111", transfer="26R-1")
    take(conn, direct)
    assert rows(conn, direct)[order_sheet.key(direct, oth_item(conn, direct))]["Примечание"].startswith("с ОТХ, заказ №")
    assert "через" not in rows(conn, direct)[order_sheet.key(direct, oth_item(conn, direct))]["Примечание"]


# ---- (5) a merged order ------------------------------------------------------------------------------------------------------------------------

def test_a_merged_order_lists_each_placed_source_under_its_number(tmp_path):
    conn = db(tmp_path)
    one = card_at(conn, "placed", T)
    two = card_at(conn, "placed", T)
    conn.execute("UPDATE order_card SET webmim_number = '7000001', transfer_number = '26R-A' WHERE id = ?", (one,))
    conn.execute("UPDATE order_card SET webmim_number = '7000002', transfer_number = '26R-B' WHERE id = ?", (two,))
    conn.commit()
    new = order_merge.merge(conn, [one, two], stage(conn, "placed"), LATER, "o", client_from=one)
    found = sorted((p["number"], p["transfer"]) for p in parts(conn, new) if p["kind"] == "merged")
    assert found == [("7000001", "26R-A"), ("7000002", "26R-B")]
    assert {r["№ заказа MIM"] for r in rows(conn, new).values()} == {"7000001", "7000002"}


# ---- the point -----------------------------------------------------------------------------------------------------------------------------

def test_the_block_has_no_money(tmp_path):
    conn = db(tmp_path)
    source(conn)
    cid = new_order(conn, qty=6)
    take(conn, cid)
    for part in parts(conn, cid):
        assert not {"price", "purchase", "profit", "sum"} & set(part)
        for x in part["items"]:
            assert set(x) <= {"id", "name", "qty"}
