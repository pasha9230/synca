"""An order taking tyres from «На ОТХ» has the three values of the order they were bought by (the owner 2026-11-05): «№ заказа MIM», «Перемещение
MIM», «Дата счета» in its row of the table; the day is never stored, the card shows it read-only."""
from __future__ import annotations

import json

from tests.test_order_reserve import db, stage
from tests.test_oth_numbers import NUMBER, TRANSFER, numbers, source
from tests.test_oth_sale import LATER, line, new_order, panel, take
from tires_sync import cli, oth_manual, order_sheet as sheet, orders, oth_sale, store

DAY = "2026-10-21"
COLUMNS = ("№ заказа MIM", "Перемещение MIM", "Дата счета")


def by_button(conn, order_id, at="2026-10-22T18:00:00"):
    """The order was made by «Оформить в WebMIM» and the snapshot has not read it yet: its day is the moment of the button, its number finds nothing."""
    conn.execute("INSERT INTO webmim_create (order_id, server, state, first_at, started_at, author, webmim_id, updated_at) "
                 "VALUES (?, 'prod', 'done', ?, ?, 'o', '1001', ?)", (order_id, LATER, LATER, at))
    conn.commit()


def invoice(conn, order_id, day=DAY, number=None):
    card = orders.get_card(conn, order_id)
    conn.execute("INSERT INTO webmim_order (server, id, number, create_date, seen_at) VALUES ('prod', ?, ?, ?, ?)",
                 ("w%d" % order_id, number or card["webmim_number"], day + "T11:00:00", LATER))
    conn.commit()


def row_of(conn, cid, goods="1"):
    card = orders.get_card(conn, cid)
    item = next(i for i in card["items"] if i["goods_id"] == goods and i["id"] in oth_sale.taken_ids(conn, cid))
    found = sheet.rows_of(conn, cid, LATER)[sheet.key(cid, item["id"])]
    return tuple(found[c] for c in COLUMNS)


def enrolled(conn, cid):
    """The order is in the table, its rows written and nothing new since."""
    conn.execute("INSERT INTO sheet_order (order_id, placed_at, sent_at, rows, seen_event) VALUES (?,?,?,?,?)",
                 (cid, LATER, LATER, "{}", conn.execute("SELECT COALESCE(MAX(id), 0) FROM order_event").fetchone()[0]))
    conn.commit()


def own_stock(conn, cid, qty):
    oth_manual.set_qty(conn, "1", orders.get_card(conn, cid)["point_id"], qty, LATER, "o", None)


def own_stock_order(conn, typed=None):
    cid = new_order(conn)
    own_stock(conn, cid, 4)
    if typed:
        orders.update_card(conn, cid, typed, LATER, "o")
    o = oth_sale.offers(conn, orders.get_card(conn, cid))[line(conn, cid)["id"]][0]
    oth_sale.take(conn, cid, line(conn, cid)["id"], o["from_item"], LATER, "o")
    return cid


# ---- the two ways of taking, one source -------------------------------------------------------------------------------------------------

def test_take_gives_the_row_all_three_values_of_the_source(tmp_path):
    conn = db(tmp_path)
    old = source(conn)
    invoice(conn, old)
    cid = new_order(conn)
    take(conn, cid)
    assert numbers(conn, cid) == (NUMBER, TRANSFER)
    assert row_of(conn, cid) == (NUMBER, TRANSFER, DAY)


def test_take_gives_the_day_of_the_source_even_when_its_number_finds_no_day(tmp_path):
    conn = db(tmp_path)
    by_button(conn, source(conn))
    cid = new_order(conn)
    take(conn, cid)
    assert row_of(conn, cid) == (NUMBER, TRANSFER, "2026-10-22")


def test_a_new_client_from_the_oth_stage_gets_all_three_too(tmp_path):
    conn = db(tmp_path)
    old = source(conn)
    invoice(conn, old)
    cid = oth_sale.to_new_client(conn, old, "Пётр", "+7 900 555 66 77", LATER, "o")
    assert cid == old and numbers(conn, cid) == (NUMBER, TRANSFER)                   # all the tyres: the order itself (oth_swap, 2026-11-06)
    [found] = sheet.rows_of(conn, cid, LATER).values()
    assert tuple(found[c] for c in COLUMNS) == (NUMBER, TRANSFER, DAY)


def test_a_new_client_gets_the_day_of_the_source_even_when_its_number_finds_no_day(tmp_path):
    conn = db(tmp_path)
    old = source(conn)
    by_button(conn, old)
    cid = oth_sale.to_new_client(conn, old, "Пётр", "+7 900 555 66 77", LATER, "o")
    [found] = sheet.rows_of(conn, cid, LATER).values()                             # the order itself (oth_swap, 2026-11-06)
    assert cid == old and tuple(found[c] for c in COLUMNS) == (NUMBER, TRANSFER, "2026-10-22")


def test_the_first_of_a_chain_gives_them(tmp_path):
    conn = db(tmp_path)
    first = source(conn)
    invoice(conn, first)
    middle = new_order(conn)
    take(conn, middle)
    orders.move_card(conn, middle, next(s["id"] for s in orders.list_stages(conn) if s["code"] == "oth"), LATER, "o")
    last = new_order(conn, qty=2)
    take(conn, last)
    assert row_of(conn, last) == (NUMBER, TRANSFER, DAY)


# ---- several sources, own stock, the supplier's positions -------------------------------------------------------------------------------

def test_two_sources_each_position_has_its_own_and_the_order_stays_empty(tmp_path):
    conn = db(tmp_path)
    one, two = source(conn), source(conn, number="5254044", transfer="26R-2")
    invoice(conn, one)
    invoice(conn, two, day="2026-10-25")
    cid = new_order(conn, qty=4, extra=(("1", 4),))
    lines = orders.get_card(conn, cid)["items"]
    oth_sale.take(conn, cid, lines[0]["id"], orders.get_card(conn, one)["items"][0]["id"], LATER, "o")
    oth_sale.take(conn, cid, lines[1]["id"], orders.get_card(conn, two)["items"][0]["id"], LATER, "o")
    assert numbers(conn, cid) == ("", "")                                           # the rule of 2026-11-04 stays
    card = orders.get_card(conn, cid)
    rows = sheet.rows_of(conn, cid, LATER)
    got = {tuple(rows[sheet.key(cid, i)][c] for c in COLUMNS) for i in oth_sale.taken_ids(conn, cid)}
    assert got == {(NUMBER, TRANSFER, DAY), ("5254044", "26R-2", "2026-10-25")} and len(card["items"]) == 2
    assert sheet.oth_invoice(conn, card) == [{"order": one, "day": DAY}, {"order": two, "day": "2026-10-25"}]


def test_a_supplier_position_keeps_the_order_s_own_values_and_the_taken_one_the_source_s(tmp_path):
    conn = db(tmp_path)
    old = source(conn)
    invoice(conn, old)
    cid = new_order(conn, qty=6)                                                    # 4 from OTH, 2 stay the supplier's
    take(conn, cid)
    assert numbers(conn, cid) == ("", "")
    orders.update_card(conn, cid, {"webmim_number": "7000001", "transfer_number": "26R-OWN", "webmim_id": "own1"}, LATER, "o")
    conn.execute("INSERT INTO webmim_order (server, id, number, create_date, seen_at) VALUES ('prod','own1','7000001','2026-11-01T10:00:00',?)", (LATER,))
    conn.commit()
    card = orders.get_card(conn, cid)
    rows = sheet.rows_of(conn, cid, LATER)
    taken_id = next(iter(oth_sale.taken_ids(conn, cid)))
    supplier_id = next(i["id"] for i in card["items"] if i["id"] != taken_id)
    assert tuple(rows[sheet.key(cid, taken_id)][c] for c in COLUMNS) == (NUMBER, TRANSFER, DAY)
    assert tuple(rows[sheet.key(cid, supplier_id)][c] for c in COLUMNS) == ("7000001", "26R-OWN", "2026-11-01")


def test_a_number_typed_for_the_supplier_s_positions_is_theirs_not_the_taken_tyres(tmp_path):
    conn = db(tmp_path)
    old = source(conn)
    invoice(conn, old)
    cid = new_order(conn, qty=6)                                                    # 4 from OTH, 2 stay the supplier's
    take(conn, cid)
    orders.update_card(conn, cid, {"webmim_number": "7000001", "transfer_number": "26R-OWN"}, LATER, "o")     # typed, no id of WebMIM
    assert numbers(conn, cid) == ("7000001", "26R-OWN")
    assert row_of(conn, cid) == (NUMBER, TRANSFER, DAY)


def test_own_stock_has_no_source_so_the_three_are_empty(tmp_path):
    conn = db(tmp_path)
    cid = own_stock_order(conn)
    assert row_of(conn, cid) == ("", "", "")
    assert sheet.oth_invoice(conn, orders.get_card(conn, cid)) == []


def test_own_stock_next_to_an_order_of_webmim_shows_none_of_that_order(tmp_path):
    conn = db(tmp_path)
    cid = new_order(conn, qty=4)                                                    # 2 from the own stock, 2 the supplier's
    own_stock(conn, cid, 2)
    o = oth_sale.offers(conn, orders.get_card(conn, cid))[line(conn, cid)["id"]][0]
    oth_sale.take(conn, cid, line(conn, cid)["id"], o["from_item"], LATER, "o")
    orders.update_card(conn, cid, {"webmim_number": "7000001", "transfer_number": "26R-OWN", "webmim_id": "own1"}, LATER, "o")
    assert row_of(conn, cid) == ("", "", "")


# ---- a person's value ---------------------------------------------------------------------------------------------------------------

def test_a_typed_transfer_stays_in_the_card_and_the_row(tmp_path):
    conn = db(tmp_path)
    old = source(conn)
    invoice(conn, old)
    cid = new_order(conn)
    orders.update_card(conn, cid, {"transfer_number": "26R-МОЙ"}, LATER, "o")
    take(conn, cid)
    assert numbers(conn, cid) == (NUMBER, "26R-МОЙ")
    assert row_of(conn, cid) == (NUMBER, "26R-МОЙ", DAY)


def test_a_typed_number_stays_and_its_own_day_goes_with_it(tmp_path):
    conn = db(tmp_path)
    old = source(conn)
    invoice(conn, old)
    conn.execute("INSERT INTO webmim_order (server, id, number, create_date, seen_at) VALUES ('prod','x1','5999999','2026-09-09T10:00:00',?)", (LATER,))
    cid = new_order(conn)
    take(conn, cid)
    orders.update_card(conn, cid, {"webmim_number": "5999999"}, LATER, "o")         # over the copy: a person's from now on
    assert numbers(conn, cid)[0] == "5999999"
    assert row_of(conn, cid) == ("5999999", TRANSFER, "2026-09-09")
    assert sheet.oth_invoice(conn, orders.get_card(conn, cid)) == [{"order": 0, "day": "2026-09-09"}]


def test_a_typed_transfer_on_own_stock_stays_in_the_row(tmp_path):
    conn = db(tmp_path)
    cid = own_stock_order(conn, typed={"transfer_number": "26R-МОЙ"})
    assert row_of(conn, cid) == ("", "26R-МОЙ", "")


def test_a_typed_transfer_survives_a_change_of_the_source(tmp_path):
    conn = db(tmp_path)
    old = source(conn)
    cid = new_order(conn)
    orders.update_card(conn, cid, {"transfer_number": "26R-МОЙ"}, LATER, "o")
    take(conn, cid)
    orders.update_card(conn, old, {"transfer_number": "26R-НОВЫЙ"}, LATER, "o")
    assert numbers(conn, cid)[1] == "26R-МОЙ" and row_of(conn, cid)[1] == "26R-МОЙ"


# ---- the source changes ---------------------------------------------------------------------------------------------------------------

def test_a_new_number_of_the_source_reaches_the_row_and_the_order_is_looked_at_again(tmp_path):
    conn = db(tmp_path)
    old = source(conn)
    invoice(conn, old)
    cid = new_order(conn)
    take(conn, cid)
    enrolled(conn, cid)
    assert cid not in sheet._due(conn, LATER, False)
    orders.update_card(conn, old, {"webmim_number": "5300000", "transfer_number": "26R-9"}, LATER, "o")
    assert numbers(conn, cid) == ("5300000", "26R-9")
    assert row_of(conn, cid)[:2] == ("5300000", "26R-9")
    assert cid in sheet._due(conn, LATER, False)


def test_two_sources_copy_nothing_yet_the_row_is_looked_at_again_when_one_changes(tmp_path):
    conn = db(tmp_path)
    one, two = source(conn), source(conn, number="5254044", transfer="26R-2")
    cid = new_order(conn, qty=4, extra=(("1", 4),))
    lines = orders.get_card(conn, cid)["items"]
    oth_sale.take(conn, cid, lines[0]["id"], orders.get_card(conn, one)["items"][0]["id"], LATER, "o")
    oth_sale.take(conn, cid, lines[1]["id"], orders.get_card(conn, two)["items"][0]["id"], LATER, "o")
    enrolled(conn, cid)
    orders.update_card(conn, one, {"transfer_number": "26R-9"}, LATER, "o")
    assert numbers(conn, cid) == ("", "")                                           # no copy, no record in its journal
    assert cid in sheet._due(conn, LATER, False)
    assert "26R-9" in {sheet.rows_of(conn, cid, LATER)[sheet.key(cid, i)]["Перемещение MIM"] for i in oth_sale.taken_ids(conn, cid)}


def test_a_new_id_of_the_source_brings_a_new_day(tmp_path):
    conn = db(tmp_path)
    old = source(conn, number="", transfer="")
    conn.execute("UPDATE order_card SET webmim_number = '' WHERE id = ?", (old,))
    conn.execute("INSERT INTO webmim_order (server, id, number, create_date, seen_at) VALUES ('prod','9001','','2026-10-05T10:00:00',?)", (LATER,))
    conn.commit()
    cid = new_order(conn)
    take(conn, cid)
    enrolled(conn, cid)
    orders.update_card(conn, old, {"webmim_id": "9001"}, LATER, "o")
    assert row_of(conn, cid) == ("9001", "", "2026-10-05")
    assert cid in sheet._due(conn, LATER, False)


def test_an_order_with_a_press_going_on_is_looked_at_again_too(tmp_path):
    conn = db(tmp_path)
    old = source(conn)
    cid = new_order(conn)
    take(conn, cid)
    enrolled(conn, cid)
    conn.execute("INSERT INTO webmim_create (order_id, server, state, first_at, started_at, author, updated_at) VALUES (?, 'prod', 'running', ?, ?, 'o', ?)", (cid, LATER, LATER, LATER))
    conn.commit()
    orders.update_card(conn, old, {"transfer_number": "26R-9"}, LATER, "o")
    assert numbers(conn, cid)[1] == TRANSFER                                        # its own number comes by the press
    assert cid in sheet._due(conn, LATER, False)


# ---- the order taken before the rule ----------------------------------------------------------------------------------------------------

def test_an_old_order_without_numbers_shows_them_in_the_row_and_its_card_fields_stay_empty(tmp_path):
    conn = db(tmp_path)
    old = source(conn)
    invoice(conn, old)
    cid = new_order(conn)
    take(conn, cid)
    conn.execute("UPDATE order_card SET webmim_number = '', transfer_number = '' WHERE id = ?", (cid,))    # as the orders 509 and 515 stand
    conn.execute("DELETE FROM audit WHERE action = 'oth_numbers'")
    conn.commit()
    assert row_of(conn, cid) == (NUMBER, TRANSFER, DAY)
    assert numbers(conn, cid) == ("", "")


# ---- the card ---------------------------------------------------------------------------------------------------------------------------

def test_the_card_shows_the_day_read_only_only_when_the_order_took_from_oth(tmp_path):
    app, owner, tok, conn = panel(tmp_path)
    old = source(conn)
    invoice(conn, old)
    plain, cid = new_order(conn), new_order(conn)
    take(conn, cid)
    page = owner.get("/orders/%d" % cid).get_data(as_text=True)
    shown = page[page.index("data-oth-invoice"):page.index("</div>", page.index("data-oth-invoice"))]
    assert "Дата счета" in shown and "21.10.2026" in shown and "заказ №%d" % old in shown and "<input" not in shown
    assert "data-oth-invoice" not in owner.get("/orders/%d" % plain).get_data(as_text=True)


def test_the_card_of_own_stock_shows_no_day(tmp_path):
    app, owner, tok, conn = panel(tmp_path)
    assert "data-oth-invoice" not in owner.get("/orders/%d" % own_stock_order(conn)).get_data(as_text=True)


# ---- a merge ----------------------------------------------------------------------------------------------------------------------------

def test_a_merged_order_gives_the_tyres_of_a_source_on_oth_that_source_s_values(tmp_path):
    from tests.test_order_reserve import card_at
    from tires_sync import order_merge
    conn = db(tmp_path)
    on_oth = source(conn)
    invoice(conn, on_oth)
    placed = card_at(conn, "moving", LATER)
    conn.execute("UPDATE order_card SET webmim_number = '7000001', transfer_number = '26R-OWN' WHERE id = ?", (placed,))
    conn.commit()
    new = order_merge.merge(conn, [placed, on_oth], stage(conn, "moving"), LATER, "o", client_from=placed)
    assert row_of(conn, new) == (NUMBER, TRANSFER, DAY)                             # not the number the merged order was given by its base


# ---- oth-numbers-fill (the owner 2026-11-05: the orders 509 and 515) -----------------------------------------------------------------------

def before_the_rule(conn, cid):
    """The order took its tyres before the numbers were copied."""
    conn.execute("UPDATE order_card SET webmim_number = '', transfer_number = '' WHERE id = ?", (cid,))
    conn.execute("DELETE FROM audit WHERE action = 'oth_numbers'")
    conn.execute("DELETE FROM order_event WHERE order_id = ? AND text LIKE 'Номер%'", (cid,))
    conn.commit()


def old_order(conn):
    old = source(conn)
    cid = new_order(conn)
    take(conn, cid)
    before_the_rule(conn, cid)
    return old, cid


def fill(tmp_path, capsys, *orders_named, dry=False):
    argv = ["oth-numbers-fill", "--db", str(tmp_path / "x.db")] + [x for o in orders_named for x in ("--order", str(o))] + (["--dry-run"] if dry else [])
    assert cli.main(argv) == 0
    return capsys.readouterr().out


def traces(conn, cid):
    return (numbers(conn, cid), conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0], conn.execute("SELECT COUNT(*) FROM order_event").fetchone()[0])


def sends(conn):
    return (conn.execute("SELECT COUNT(*) FROM order_task").fetchone()[0],)


def test_the_command_fills_the_named_order_with_journal_and_audit_and_sends_nothing(tmp_path, capsys):
    conn = db(tmp_path)
    old, cid = old_order(conn)
    other = new_order(conn, qty=4, goods="9")
    tasks = sends(conn)
    out = fill(tmp_path, capsys, cid)
    conn = store.connect(str(tmp_path / "x.db"))
    assert numbers(conn, cid) == (NUMBER, TRANSFER) and numbers(conn, other) == ("", "")
    assert "№%d" % cid in out and "«» → «%s»" % NUMBER in out and "«» → «%s»" % TRANSFER in out and "заказа №%d" % old in out
    events = [(e["author"], e["text"]) for e in orders.list_events(conn, cid)]
    assert ("система", "Номер WebMIM и перемещение взяты из заказа №%d" % old) in events
    author, detail = conn.execute("SELECT author, detail FROM audit WHERE action = 'oth_numbers' ORDER BY id DESC").fetchone()
    assert author == "система" and json.loads(detail) == {"order": cid, "from_order": old, "changes": {"webmim_number": NUMBER, "transfer_number": TRANSFER}}
    assert sends(conn) == tasks and oth_sale.borrowed_ids(conn, cid) == {cid}


def test_the_dry_run_says_the_same_and_writes_nothing(tmp_path, capsys):
    conn = db(tmp_path)
    old, cid = old_order(conn)
    was = traces(conn, cid)
    out = fill(tmp_path, capsys, cid, dry=True)
    conn = store.connect(str(tmp_path / "x.db"))
    assert traces(conn, cid) == was and numbers(conn, cid) == ("", "")
    assert "«» → «%s»" % NUMBER in out and "заказа №%d" % old in out and "ничего не записано" in out


def test_the_command_keeps_a_typed_value_and_a_second_run_changes_nothing(tmp_path, capsys):
    conn = db(tmp_path)
    old, cid = old_order(conn)
    conn.execute("UPDATE order_card SET transfer_number = '26R-МОЙ' WHERE id = ?", (cid,))
    conn.commit()
    out = fill(tmp_path, capsys, cid)
    conn = store.connect(str(tmp_path / "x.db"))
    assert numbers(conn, cid) == (NUMBER, "26R-МОЙ") and "26R-МОЙ" not in out
    was = traces(conn, cid)
    assert "не заполнится: менять нечего" in fill(tmp_path, capsys, cid)
    assert traces(conn, cid) == was


def test_the_command_refuses_what_the_rules_refuse_and_says_why(tmp_path, capsys):
    conn = db(tmp_path)
    two = (source(conn), source(conn, number="5254044", transfer="26R-2"))
    several = new_order(conn, qty=4, extra=(("1", 4),))
    for at, line_of in enumerate(orders.get_card(conn, several)["items"]):
        oth_sale.take(conn, several, line_of["id"], orders.get_card(conn, two[at])["items"][0]["id"], LATER, "o")
    plain = new_order(conn, goods="9")
    source(conn)
    with_supplier = new_order(conn, qty=6)
    take(conn, with_supplier)
    source(conn)
    closed = new_order(conn)
    take(conn, closed)
    before_the_rule(conn, closed)
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (stage(conn, "done"), closed))
    conn.commit()
    was = conn.execute("SELECT COUNT(*) FROM audit WHERE action = 'oth_numbers'").fetchone()[0]
    out = fill(tmp_path, capsys, several, plain, with_supplier, closed, 99999)
    lines = dict(zip((several, plain, with_supplier, closed, 99999), out.strip().split("\n")))
    assert "разных источников" in lines[several] and "не держит шин" in lines[plain] and "позиции поставщика" in lines[with_supplier]
    assert "заказ закрыт" in lines[closed] and "заказа нет" in lines[99999]
    conn = store.connect(str(tmp_path / "x.db"))
    assert conn.execute("SELECT COUNT(*) FROM audit WHERE action = 'oth_numbers'").fetchone()[0] == was
    assert numbers(conn, closed) == ("", "") and numbers(conn, several) == ("", "")


def test_the_command_refuses_with_an_order_of_webmim_a_press_going_on_and_own_stock(tmp_path, capsys):
    conn = db(tmp_path)
    source(conn)
    own = new_order(conn)
    take(conn, own)
    before_the_rule(conn, own)
    orders.update_card(conn, own, {"webmim_id": "1"}, LATER, "o")
    source(conn)
    pressed = new_order(conn)
    take(conn, pressed)
    before_the_rule(conn, pressed)
    conn.execute("INSERT INTO webmim_create (order_id, server, state, first_at, started_at, author, updated_at) VALUES (?, 'prod', 'running', ?, ?, 'o', ?)", (pressed, LATER, LATER, LATER))
    conn.commit()
    stock = own_stock_order(conn)
    out = fill(tmp_path, capsys, own, pressed, stock).strip().split("\n")
    assert "свой заказ WebMIM" in out[0] and "оформляется" in out[1] and "номера не нужны" in out[2]


# ---- the review of 6bce7a7 --------------------------------------------------------------------------------------------------------------

def merged_on_oth(conn):
    """Two placed orders merged, the new order refused: its tyres lie on «На ОТХ», their numbers are its sources' (order_merge)."""
    from tests.test_order_reserve import card_at
    from tires_sync import order_merge
    one = card_at(conn, "moving", LATER)
    two = card_at(conn, "moving", LATER)
    conn.execute("UPDATE order_item SET goods_id = '2', name = 'Шина 2' WHERE order_id = ?", (two,))
    conn.execute("UPDATE order_card SET webmim_number = '6000001', transfer_number = '26R-A' WHERE id = ?", (one,))
    conn.execute("UPDATE order_card SET webmim_number = '6000002', transfer_number = '26R-B' WHERE id = ?", (two,))
    conn.commit()
    invoice(conn, two, day="2026-10-02")
    target = order_merge.merge(conn, [one, two], stage(conn, "moving"), LATER, "o", client_from=one)
    orders.move_card(conn, target, stage(conn, "oth"), LATER, "o", fire_rules=False)
    return one, two, target


def test_tyres_of_a_merged_order_on_oth_have_the_values_of_the_order_that_bought_them(tmp_path):
    conn = db(tmp_path)
    one, two, target = merged_on_oth(conn)
    cid = new_order(conn, goods="2")
    take(conn, cid, goods="2")
    assert row_of(conn, cid, goods="2") == ("6000002", "26R-B", "2026-10-02")       # not the transfer of the merged order's base
    assert sheet.oth_invoice(conn, orders.get_card(conn, cid)) == [{"order": two, "day": "2026-10-02"}]


def test_a_new_number_of_the_order_behind_a_merge_wakes_the_row(tmp_path):
    conn = db(tmp_path)
    one, two, target = merged_on_oth(conn)
    cid = new_order(conn, goods="2")
    take(conn, cid, goods="2")
    enrolled(conn, cid)
    orders.update_card(conn, two, {"transfer_number": "26R-C"}, LATER, "o")
    assert cid in sheet._due(conn, LATER, False) and row_of(conn, cid, goods="2")[1] == "26R-C"


def test_a_source_gone_from_the_base_leaves_the_row_what_the_order_has(tmp_path):
    conn = db(tmp_path)
    old = source(conn)
    cid = new_order(conn)
    take(conn, cid)
    conn.execute("DELETE FROM order_card WHERE id = ?", (old,))                     # «Удалённые»: a chain lets the first order go
    conn.commit()
    assert row_of(conn, cid)[:2] == (NUMBER, TRANSFER)                              # the copy, never an empty cell
    assert sheet.oth_invoice(conn, orders.get_card(conn, cid)) == [{"order": 0, "day": ""}]


def test_a_typed_number_on_own_stock_shows_its_day_in_the_card_too(tmp_path):
    conn = db(tmp_path)
    conn.execute("INSERT INTO webmim_order (server, id, number, create_date, seen_at) VALUES ('prod','x1','5999999','2026-09-09T10:00:00',?)", (LATER,))
    conn.commit()
    cid = own_stock_order(conn)
    orders.update_card(conn, cid, {"webmim_number": "5999999"}, LATER, "o")
    assert row_of(conn, cid) == ("5999999", "", "2026-09-09")
    assert sheet.oth_invoice(conn, orders.get_card(conn, cid)) == [{"order": 0, "day": "2026-09-09"}]


def test_a_number_typed_equal_to_the_source_s_keeps_the_source_s_day(tmp_path):
    conn = db(tmp_path)
    old = source(conn)
    by_button(conn, old)
    cid = new_order(conn)
    take(conn, cid)
    conn.execute("DELETE FROM audit WHERE action = 'oth_numbers'")                  # no journal of the copy: a person's as far as anybody knows
    conn.commit()
    assert row_of(conn, cid) == (NUMBER, TRANSFER, "2026-10-22")
    assert sheet.oth_invoice(conn, orders.get_card(conn, cid)) == [{"order": old, "day": "2026-10-22"}]
