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
    """As the card shows them: looking at the card takes the snapshot of an order with its own number."""
    return order_webmim.parts(conn, orders.get_card(conn, cid), take_snapshot=True, now=LATER)


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
    parts(conn, cid)                                                           # the card was looked at with its number
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


def test_without_a_look_at_the_card_nothing_becomes_added(tmp_path):
    """No snapshot yet (the number came with the pieces in one save): nothing is a second part — never a false one."""
    conn = db(tmp_path)
    cid = new_order(conn, qty=4)
    orders.update_card(conn, cid, {"webmim_number": OWN}, LATER, "o")
    orders.update_item(conn, orders.get_card(conn, cid)["items"][0]["id"], 6, 6000, LATER, "o")
    assert [(p["kind"], p["items"][0]["qty"]) for p in parts(conn, cid)] == [("supplier", 6)]


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


# ---- the panel -------------------------------------------------------------------------------------------------------------------------------

def test_the_card_shows_the_block_its_forms_and_a_point_sees_none_of_it(tmp_path):
    from panel_helpers import post
    from test_mypoint import manager
    from test_vk_commands import _card, ready
    app, client, tok, conn, pid, _tanya = ready(tmp_path)
    old = _card(conn, pid, webmim=NUMBER, code="at_point")
    conn.execute("UPDATE order_card SET transfer_number = ?, stage_id = ? WHERE id = ?", (TRANSFER, stage(conn, "oth"), old))
    conn.commit()
    cid = new_order(conn, qty=6, at="Бета") if conn.execute("SELECT 1 FROM point WHERE name = 'Бета'").fetchone() else None
    conn.execute("UPDATE order_card SET point_id = ? WHERE id = ?", (pid, cid))
    conn.commit()
    assert take(conn, cid) == 4
    page = client.get("/orders/%d" % cid).get_data(as_text=True)
    block = page[page.index('id="webmim-parts"'):].split("</section>")[0]
    assert NUMBER in block and TRANSFER in block and 'data-mimpart="oth"' in block and 'data-mimpart="supplier"' in block
    assert "/items/%d/mim" % oth_item(conn, cid) in block
    assert "4500" not in block and "4 500" not in block and "4000" not in block and "₽" not in block      # no money
    item = oth_item(conn, cid)
    assert post(client, "/orders/%d/items/%d/mim" % (cid, item), tok, webmim_number="5559999", transfer_number="26R-9").status_code == 302
    assert order_webmim.item_numbers(conn, cid)[item]["webmim_number"] == "5559999"
    point = manager(app, conn, pid)
    seen = point.get("/orders/%d" % cid)
    assert seen.status_code == 200
    page = seen.get_data(as_text=True)
    assert 'id="webmim-parts"' not in page and "/mim" not in page and "Заказы WebMIM" not in page   # the block is not a point's (2026-10-30)
    assert post(point, "/orders/%d/items/%d/mim" % (cid, item), "tok", webmim_number="1").status_code == 403
    assert order_webmim.item_numbers(conn, cid)[item]["webmim_number"] == "5559999"


# ---- the review of 2026-11-07 ------------------------------------------------------------------------------------------------------------------

def test_tyres_coming_back_from_oth_or_a_merge_are_never_added(tmp_path):
    conn = db(tmp_path)
    old = source(conn, number=OWN, transfer=OWN_TRANSFER)                      # A: its own order, its tyres on «На ОТХ»
    cid = new_order(conn)
    take(conn, cid)                                                           # B takes them all: A's position at 0
    parts(conn, old)                                                          # A looked at: its snapshot at 0
    before = rows(conn, old)
    oth_sale.untake(conn, cid, oth_item(conn, cid), LATER, "o")              # back to A
    assert [p for p in parts(conn, old) if p["kind"] == "extra"] == []
    assert set(rows(conn, old)) == set(before)                                # its row is there, no second one


def second_order(conn, cid):
    """An order of 4 with its own number, looked at, then 2 more of the same tyre: its second part, its numbers typed."""
    orders.update_card(conn, cid, {"webmim_number": OWN, "transfer_number": OWN_TRANSFER}, LATER, "o")
    parts(conn, cid)
    item = orders.get_card(conn, cid)["items"][0]["id"]
    orders.update_item(conn, item, 6, 6000, LATER, "o")
    parts(conn, cid)
    order_webmim.set_extra_numbers(conn, cid, item, "6100300", "26R-2", LATER, "o")
    return item


def profit_of_rows(conn, cid):
    return round(sum(float(r["Прибыль ₽"] or 0) for r in rows(conn, cid).values()), 2)


def test_a_corrected_first_number_keeps_the_second_order(tmp_path):
    conn = db(tmp_path)
    cid = new_order(conn, qty=4)
    item = second_order(conn, cid)
    orders.update_card(conn, cid, {"webmim_number": "6100299"}, LATER, "o")   # a typo of the first number fixed
    parts(conn, cid)
    extra = rows(conn, cid)[order_sheet.extra_key(cid, item)]
    assert (extra["№ заказа MIM"], extra["Кол-во"]) == ("6100300", 2) and rows(conn, cid)[order_sheet.key(cid, item)]["№ заказа MIM"] == "6100299"


def test_a_new_first_number_takes_an_empty_second_order_back(tmp_path, monkeypatch):
    """The owner 13.11 (order 576): the quantity changed right after «Оформить», the order of WebMIM made again for every tyre and its
    number typed: the empty second order keeps 0 pieces, the new number covers all of them. The table already written: the first row
    takes every piece, the second goes to 0 pieces and no profit, the profit is counted once (review 13.11)."""
    conn = db(tmp_path)
    cid = new_order(conn, qty=4)
    orders.update_card(conn, cid, {"webmim_number": OWN, "transfer_number": OWN_TRANSFER}, LATER, "o")
    parts(conn, cid)
    item = orders.get_card(conn, cid)["items"][0]["id"]
    monkeypatch.setattr(order_sheet, "_profits", lambda conn, card: {item: (1200.0, 20.0)})
    orders.update_item(conn, item, 6, 6000, LATER, "o")
    assert [p for p in parts(conn, cid) if p["kind"] == "extra"][0]["needs_numbers"]
    written = rows(conn, cid)
    assert written[order_sheet.extra_key(cid, item)]["Кол-во"] == 2
    orders.update_card(conn, cid, {"webmim_number": "6100301", "transfer_number": "26R-5550009"}, LATER, "o")
    found = parts(conn, cid)
    assert [p["kind"] for p in found] == ["supplier"] and not found[0]["highlight"] and [x["qty"] for x in found[0]["items"]] == [6]
    sent = {op["key"]: op["fields"] for op in order_sheet.changes(rows(conn, cid), written, order_sheet.followed(conn, cid))}
    first, second = order_sheet.key(cid, item), order_sheet.extra_key(cid, item)
    assert sent[first]["Кол-во"] == 6 and sent[second]["Кол-во"] == 0
    now = rows(conn, cid)
    assert (now[first]["Прибыль ₽"], now[second]["Прибыль ₽"]) == (1200.0, 0.0) and now[second]["Сумма продажи"] == 0
    assert conn.execute("SELECT COUNT(*) FROM order_event WHERE order_id = ? AND text LIKE '%второй заказ WebMIM обнулён%'", (cid,)).fetchone()[0] == 1
    orders.update_item(conn, item, 7, 7000, LATER, "o")                        # added after the new number: the second order again
    assert [x["qty"] for p in parts(conn, cid) if p["kind"] == "extra" for x in p["items"]] == [1]


def test_a_new_first_number_without_a_second_order_keeps_the_pieces_added_meanwhile(tmp_path):
    """No second order kept yet (the card not looked at after the pieces were added): the number only moves, the added pieces stay a
    second order (review 13.11)."""
    conn = db(tmp_path)
    cid = new_order(conn, qty=4)
    orders.update_card(conn, cid, {"webmim_number": OWN, "transfer_number": OWN_TRANSFER}, LATER, "o")
    parts(conn, cid)
    item = orders.get_card(conn, cid)["items"][0]["id"]
    orders.update_item(conn, item, 6, 6000, LATER, "o")
    orders.update_card(conn, cid, {"webmim_number": "6100301"}, LATER, "o")
    assert [x["qty"] for p in parts(conn, cid) if p["kind"] == "extra" for x in p["items"]] == [2]

def test_the_second_row_never_goes_away_and_the_profit_is_counted_once(tmp_path, monkeypatch):
    conn = db(tmp_path)
    cid = new_order(conn, qty=4)
    item = second_order(conn, cid)
    monkeypatch.setattr(order_sheet, "_profits", lambda conn_, card: {item: (1200.0, 20.0)})   # the position's profit, as the sale gives it
    whole = 1200.0
    assert profit_of_rows(conn, cid) == whole
    assert order_sheet.extra_key(cid, item) in order_sheet.followed(conn, cid)         # its pieces and sums follow the order
    orders.update_card(conn, cid, {"webmim_number": ""}, LATER, "o")                    # the number cleared
    assert order_sheet.extra_key(cid, item) in rows(conn, cid) and abs(profit_of_rows(conn, cid) - whole) <= 0.02
    orders.update_card(conn, cid, {"webmim_number": OWN}, LATER, "o")
    orders.update_item(conn, item, 3, 6000, LATER, "o")                                 # fewer than were ordered first
    now = rows(conn, cid)
    assert now[order_sheet.extra_key(cid, item)]["Кол-во"] == 0 and now[order_sheet.key(cid, item)]["Кол-во"] == 3
    assert now[order_sheet.extra_key(cid, item)]["Прибыль ₽"] in (0, 0.0) and profit_of_rows(conn, cid) == whole


def test_a_take_after_the_second_order_moves_nothing_between_the_parts(tmp_path):
    conn = db(tmp_path)
    old = source(conn, number=OWN, transfer=OWN_TRANSFER)                               # A: its own order of 4 on «На ОТХ»
    parts(conn, old)
    item = orders.get_card(conn, old)["items"][0]["id"]
    before = rows(conn, old)
    taker = new_order(conn, qty=2)
    take(conn, taker)                                                                   # 2 of A's 4 leave
    assert [p for p in parts(conn, old) if p["kind"] == "extra"] == [] and set(rows(conn, old)) == set(before)
    oth_sale.untake(conn, taker, oth_item(conn, taker), LATER, "o")
    assert [p for p in parts(conn, old) if p["kind"] == "extra"] == [] and order_sheet.extra_key(old, item) not in rows(conn, old)


def test_the_button_of_webmim_takes_a_fresh_snapshot(tmp_path):
    conn = db(tmp_path)
    cid = new_order(conn, qty=4)
    conn.execute("INSERT INTO order_mim_base (order_id, item_id, qty, at, number) VALUES (?, 0, 0, ?, 'W1')", (cid, LATER))
    conn.execute("INSERT INTO order_mim_base (order_id, item_id, qty, at, number) VALUES (?, ?, 2, ?, 'W1')", (cid, orders.get_card(conn, cid)["items"][0]["id"], LATER))
    conn.commit()
    webmim_create._write_order(conn, cid, {"id": "W1", "number": "W1", "status_text": "Новый"}, LATER, "o", "Заказ WebMIM оформлен")
    assert [(p["kind"], p["items"][0]["qty"]) for p in parts(conn, cid)] == [("supplier", 4)]


def test_a_chain_from_the_points_own_stock_is_own_stock_not_order_zero(tmp_path):
    conn = db(tmp_path)
    middle = new_order(conn, qty=2)
    oth_manual.set_qty(conn, "1", orders.get_card(conn, middle)["point_id"], 2, T, "o")
    [offer] = oth_sale.offers(conn, orders.get_card(conn, middle))[line(conn, middle)["id"]]
    oth_sale.take(conn, middle, line(conn, middle)["id"], offer["from_item"], LATER, "o")
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (stage(conn, "oth"), middle))
    conn.commit()
    last = new_order(conn, qty=2)
    oth_sale.take(conn, last, line(conn, last)["id"], oth_item(conn, middle), LATER, "o")
    note = rows(conn, last)[order_sheet.key(last, oth_item(conn, last))]["Примечание"]
    assert note.startswith("с ОТХ, свой остаток, через №%d" % middle) and "№0" not in note
    assert [p["kind"] for p in parts(conn, last)] == ["stock"]


def test_a_take_from_an_order_with_a_second_part_counts_every_piece_once(tmp_path):
    """Review 3: A of 4 with its own number, +2 added (its second part), then on «На ОТХ» B takes 5: A's rows hold the 1 it has."""
    conn = db(tmp_path)
    old = source(conn, number=OWN, transfer=OWN_TRANSFER)
    item = second_order(conn, old)
    taker = new_order(conn, qty=5)
    take(conn, taker)
    held = int(orders.get_card(conn, old)["items"][0]["qty"])
    mine = {k: r for k, r in rows(conn, old).items() if k in (order_sheet.key(old, item), order_sheet.extra_key(old, item))}
    assert set(mine) == {order_sheet.key(old, item), order_sheet.extra_key(old, item)}           # both rows still follow
    assert sum(r["Кол-во"] for r in mine.values()) == held
    assert {order_sheet.key(old, item), order_sheet.extra_key(old, item)} <= order_sheet.followed(conn, old)


# ---- review 4: what the table would hold ----------------------------------------------------------------------------------------------

def table_after(conn, cid, written):
    """The rows as the table holds them after one flush: the written rows with the changes applied (follow = order_sheet.followed)."""
    now = order_sheet.rows_of(conn, cid, LATER)
    held = {k: dict(v) for k, v in written.items()}
    for op in order_sheet.changes(now, written, order_sheet.followed(conn, cid)):
        held[op["key"]] = dict(held.get(op["key"], {}), **op["fields"])
    return held


def test_lowered_pieces_follow_in_the_table_too(tmp_path):
    conn = db(tmp_path)
    cid = new_order(conn, qty=4)
    item = second_order(conn, cid)
    written = order_sheet.rows_of(conn, cid, LATER)
    orders.update_item(conn, item, 3, 6000, LATER, "o")
    held = table_after(conn, cid, written)
    assert sum(r["Кол-во"] for r in held.values()) == 3


def test_the_button_after_a_cleared_number_puts_everything_in_one_order_in_the_table(tmp_path):
    conn = db(tmp_path)
    cid = new_order(conn, qty=4)
    item = second_order(conn, cid)
    written = order_sheet.rows_of(conn, cid, LATER)
    orders.update_card(conn, cid, {"webmim_number": "", "transfer_number": ""}, LATER, "o")
    webmim_create._write_order(conn, cid, {"id": "W9", "number": "W9", "status_text": "Новый"}, LATER, "o", "Заказ WebMIM оформлен")
    held = table_after(conn, cid, written)
    assert sum(r["Кол-во"] for r in held.values()) == 6 and held[order_sheet.key(cid, item)]["Кол-во"] == 6


def test_dosozdat_done_takes_the_pieces_added_meanwhile_into_the_first_order(tmp_path):
    conn = db(tmp_path)
    cid = new_order(conn, qty=4)
    orders.update_card(conn, cid, {"webmim_id": "W5", "webmim_number": "W5"}, LATER, "o")
    parts(conn, cid)
    orders.update_item(conn, orders.get_card(conn, cid)["items"][0]["id"], 6, 6000, LATER, "o")
    conn.execute("INSERT OR REPLACE INTO webmim_create (order_id, server, state, first_at, started_at, author, webmim_id, updated_at) "
                 "VALUES (?, 'prod', 'partial', ?, ?, 'o', 'W5', ?)", (cid, LATER, LATER, LATER))
    conn.commit()
    webmim_create._finish(conn, cid, "done", "Оформлен в WebMIM: заказ №W5", LATER, "o", "green")   # «Досоздать» added the 2
    assert [(p["kind"], p["items"][0]["qty"]) for p in parts(conn, cid)] == [("supplier", 6)]


def test_the_profit_of_the_two_rows_is_exact(tmp_path, monkeypatch):
    conn = db(tmp_path)
    cid = new_order(conn, qty=1)
    orders.update_card(conn, cid, {"webmim_number": OWN}, LATER, "o")
    parts(conn, cid)
    item = orders.get_card(conn, cid)["items"][0]["id"]
    orders.update_item(conn, item, 2, 6000, LATER, "o")                     # 1 + 1: each half of 1.15 rounds to 0.57
    monkeypatch.setattr(order_sheet, "_profits", lambda conn_, card: {item: (1.15, 10.0)})
    assert round(sum(float(r["Прибыль ₽"] or 0) for r in rows(conn, cid).values()), 2) == 1.15
