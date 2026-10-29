"""«Взять с ОТХ» (oth_sale, the owner 2026-10-29): the tyres of an order on «На ОТХ» are sold to a new order at the same point — the old
order gives its pieces (emptied, it goes to «Отмена»), WebMIM is asked only for what lacks, the sale is counted at the old order's purchase
and once, the Google table lowers the old row, the stock of OTH (and so the ad) goes down."""
from __future__ import annotations

import re

import pytest

from order_fixtures import free_phone
from panel_helpers import login, make_app, post
from tests.test_order_reserve import card_at, db, stage
from tests import test_webmim_create as wc
from tires_sync import order_engine as engine, order_oth, sales as sales_store, order_report, order_sheet, orders, oth_sale, oth_stock, store, trash, users, webmim_match

T = "2026-10-29T10:00:00"
LATER = "2026-10-29T11:00:00"
BUY_OLD, BUY_NOW = 4000.0, 4500.0             # card_at: the purchase of the order on OTH; the new order: today's supplier price


def point_id(conn, name="Бета"):
    return conn.execute("SELECT id FROM point WHERE name = ?", (name,)).fetchone()[0]


def new_order(conn, qty=4, goods="1", at="Бета", extra=()):
    pid = None if at is None else (point_id(conn, at) if conn.execute("SELECT 1 FROM point WHERE name = ?", (at,)).fetchone()
                                   else wc.point(conn, name=at))
    fields = dict(client_name="Пётр", client_phone=free_phone(conn, "+7 900 555 66 77"), delivery_date="2026-10-30")
    if pid is not None:
        fields["point_id"] = pid
    cid = orders.create_card(conn, fields, T, "o")
    for g, q in ((goods, qty),) + tuple(extra):
        orders.add_item(conn, cid, goods_id=g, name="Nokian 205/55 R16" if g == "1" else "Шина " + g, brand_key="nokian", season="winter",
                        diameter=16, territory="1", qty=q, sale_unit=6000, supplier_price=BUY_NOW, terms=None, fee_pct=0, promo_pct=0, now=T, author="o")
    return cid


def line(conn, cid, goods="1"):
    return next(i for i in orders.get_card(conn, cid)["items"] if i["goods_id"] == goods and i["id"] not in oth_sale.taken_ids(conn, cid))


def offer(conn, cid, goods="1"):
    return oth_sale.offers(conn, orders.get_card(conn, cid))[line(conn, cid, goods)["id"]][0]


def take(conn, cid, goods="1", now=LATER):
    return oth_sale.take(conn, cid, line(conn, cid, goods)["id"], offer(conn, cid, goods)["from_item"], now, "o")


def items(conn, cid):
    return [(i["goods_id"], i["qty"], i["supplier_price"], i["id"] in oth_sale.taken_ids(conn, cid)) for i in orders.get_card(conn, cid)["items"]]


def code(conn, cid):
    return orders.get_card(conn, cid)["stage"]["code"]


# ---- the take -------------------------------------------------------------------------------------------------------------------------

def test_all_taken_the_old_order_is_cancelled_and_the_stock_of_oth_is_gone(tmp_path):
    conn = db(tmp_path)
    old = card_at(conn, "oth", T)
    assert oth_stock.by_point(conn) == {("1", point_id(conn)): 4}
    cid = new_order(conn, qty=4)
    o = offer(conn, cid)
    assert (o["from_order"], o["point"], o["qty"], o["take"], o["why"]) == (old, "Бета", 4, 4, None)
    assert take(conn, cid) == 4
    assert items(conn, cid) == [("1", 4, BUY_OLD, True)]                          # the supplier's position is gone, the purchase is the old one
    assert orders.get_card(conn, cid)["items"][0]["purchase_manual"] == 1
    card = orders.get_card(conn, old)
    assert card["stage"]["code"] == "cancel" and card["cancel_reason"] == "Шины переданы в заказ №%d" % cid
    assert [i["qty"] for i in card["items"]] == [0]
    assert oth_stock.by_point(conn) == {} and oth_stock.points(conn) == {}       # the ad loses «Шины в наличии» in the next cycle
    assert not engine.list_tasks(conn, old, ("pending",))                         # no rule of «Отмена»: the old client is not written to
    assert any("Взято с ОТХ (заказ №%d, Бета)" % old in e["text"] for e in orders.list_events(conn, cid))
    assert any("Передано с ОТХ в заказ №%d" % cid in e["text"] for e in orders.list_events(conn, old))
    assert conn.execute("SELECT COUNT(*) FROM audit WHERE action = 'oth_take'").fetchone()[0] == 1
    assert oth_sale.offers(conn, orders.get_card(conn, cid)) == {}


def test_more_wanted_than_on_oth_the_rest_stays_the_suppliers(tmp_path):
    conn = db(tmp_path)
    card_at(conn, "oth", T)
    cid = new_order(conn, qty=6)
    assert take(conn, cid) == 4
    assert sorted(items(conn, cid)) == [("1", 2, BUY_NOW, False), ("1", 4, BUY_OLD, True)]
    assert [i["qty"] for i in oth_sale.supplier_items(conn, orders.get_card(conn, cid))] == [2]


def test_fewer_wanted_than_on_oth_the_old_order_keeps_the_rest_on_oth(tmp_path):
    conn = db(tmp_path)
    old = card_at(conn, "oth", T)
    cid = new_order(conn, qty=2)
    assert take(conn, cid) == 2
    assert items(conn, cid) == [("1", 2, BUY_OLD, True)]
    assert code(conn, old) == "oth" and [i["qty"] for i in orders.get_card(conn, old)["items"]] == [2]
    assert oth_stock.by_point(conn) == {("1", point_id(conn)): 2}
    assert order_oth.stock(conn)["stock"][0]["qty"] == 2


def test_another_point_is_never_offered_and_an_order_without_a_point_gets_the_tyres_one(tmp_path):
    conn = db(tmp_path)
    card_at(conn, "oth", T)
    cid = new_order(conn, at="Гамма")
    o = offer(conn, cid)
    assert "Шины лежат на «Бета», а заказ на «Гамма»" in o["why"]
    with pytest.raises(orders.OrderError, match="на другой точке"):
        oth_sale.take(conn, cid, line(conn, cid)["id"], o["from_item"], LATER, "o")
    assert oth_stock.by_point(conn) == {("1", point_id(conn)): 4} and items(conn, cid) == [("1", 4, BUY_NOW, False)]
    bare = new_order(conn, at=None, qty=1)
    assert take(conn, bare) == 1
    assert orders.get_card(conn, bare)["point_id"] == point_id(conn)


def test_two_tabs_never_take_the_same_tyres_twice(tmp_path):
    conn = db(tmp_path)
    card_at(conn, "oth", T)
    first, second = new_order(conn, qty=4), new_order(conn, qty=4)
    from_item = offer(conn, second)["from_item"]
    take(conn, first)
    with pytest.raises(orders.OrderError, match="уже нет"):
        oth_sale.take(conn, second, line(conn, second)["id"], from_item, LATER, "o")
    assert items(conn, second) == [("1", 4, BUY_NOW, False)]


def test_the_limit_is_in_the_statement_itself(tmp_path):
    """The check before saw 4 on OTH; meanwhile 3 were marked returned: the statement takes nothing below them."""
    conn = db(tmp_path)
    old = card_at(conn, "oth", T)
    cid = new_order(conn, qty=4)
    real = oth_stock._rows
    rows = real(conn)
    conn.execute("INSERT INTO oth_return (item_id, order_id, qty, at, author) VALUES (?,?,?,?,?)", (rows[0]["item_id"], old, 3, T, "o"))
    conn.commit()
    oth_stock._rows = lambda _c: rows
    try:
        with pytest.raises(orders.OrderError, match="уже нет"):
            oth_sale.take(conn, cid, line(conn, cid)["id"], rows[0]["item_id"], LATER, "o")
    finally:
        oth_stock._rows = real
    assert [i["qty"] for i in orders.get_card(conn, old)["items"]] == [4] and items(conn, cid) == [("1", 4, BUY_NOW, False)]


@pytest.mark.parametrize("case", ["placed", "webmim", "pressed", "agreed", "test_point", "returned"])
def test_what_keeps_the_tyres_from_being_taken(tmp_path, case):
    conn = db(tmp_path)
    old = card_at(conn, "oth", T)
    cid = new_order(conn, qty=4)
    if case == "placed":
        orders.move_card(conn, cid, stage(conn, "placed"), T, "o")
    elif case == "webmim":
        orders.update_card(conn, cid, {"webmim_number": "777"}, T, "o")
    elif case == "pressed":
        conn.execute("INSERT INTO webmim_create (order_id, server, state, first_at, started_at, author, updated_at) VALUES (?,?,?,?,?,?,?)",
                     (cid, "test", "unknown", T, T, "o", T))
    elif case == "agreed":
        conn.execute("INSERT INTO oth_return_plan (order_id, agreed_at, agreed_by, return_date) VALUES (?,?,?,?)", (old, T, "o", "2026-10-29"))
    elif case == "test_point":
        conn.execute("UPDATE point SET name = 'Тест-Бета' WHERE name = 'Бета'")
    elif case == "returned":
        order_oth.mark_returned(conn, order_oth.stock(conn)["stock"][0]["item_id"], T, "o")
    conn.commit()
    found = oth_sale.offers(conn, orders.get_card(conn, cid))
    if case in ("agreed", "test_point", "returned"):
        assert found == {}
    else:
        assert found[line(conn, cid)["id"]][0]["why"]
    with pytest.raises(orders.OrderError):
        oth_sale.take(conn, cid, line(conn, cid)["id"], conn.execute("SELECT id FROM order_item WHERE order_id = ?", (old,)).fetchone()[0], LATER, "o")
    assert [i["qty"] for i in orders.get_card(conn, old)["items"]] == [4]


# ---- the position «с ОТХ» in the order --------------------------------------------------------------------------------------------------

def test_its_pieces_do_not_change_and_it_is_not_deleted_the_price_does(tmp_path):
    conn = db(tmp_path)
    card_at(conn, "oth", T)
    cid = new_order(conn, qty=2)
    take(conn, cid)
    item = orders.get_card(conn, cid)["items"][0]
    with pytest.raises(orders.OrderError, match="количество не меняется"):
        orders.update_item(conn, item["id"], 3, 6000, LATER, "o")
    with pytest.raises(orders.OrderError, match="Вернуть на ОТХ"):
        orders.remove_item(conn, item["id"], LATER, "o")
    orders.update_item(conn, item["id"], 2, 6500, LATER, "o")
    assert orders.get_card(conn, cid)["items"][0]["sale_unit"] == 6500


def test_the_order_holding_them_is_neither_cancelled_nor_deleted(tmp_path):
    conn = db(tmp_path)
    old = card_at(conn, "oth", T)
    cid = new_order(conn, qty=2)
    take(conn, cid)
    for code_ in ("cancel", "refused"):
        with pytest.raises(orders.OrderError, match="шины с ОТХ \\(заказ №%d\\)" % old):
            orders.move_card(conn, cid, stage(conn, code_), LATER, "o", changes={"cancel_reason": "передумал"})
    assert code(conn, cid) == "new"
    with pytest.raises(orders.OrderError, match="шины с ОТХ"):
        trash.delete_order(conn, cid, LATER, "o", only_if=lambda c, _card: oth_sale.holds_text(oth_sale.holds(c, cid)) if oth_sale.holds(c, cid) else None)


def test_given_back_the_pieces_return_and_the_cancelled_old_order_stands_on_oth_again(tmp_path):
    conn = db(tmp_path)
    old = card_at(conn, "oth", T)
    cid = new_order(conn, qty=4)
    take(conn, cid)
    assert code(conn, old) == "cancel"
    item = orders.get_card(conn, cid)["items"][0]["id"]
    assert oth_sale.untake(conn, cid, item, "2026-10-29T12:00:00", "o") == 4
    assert orders.get_card(conn, cid)["items"] == [] and code(conn, old) == "oth"
    assert [i["qty"] for i in orders.get_card(conn, old)["items"]] == [4]
    assert oth_stock.by_point(conn) == {("1", point_id(conn)): 4}
    assert oth_sale.taken(conn, cid) == {} and conn.execute("SELECT undone_at FROM order_item_oth").fetchone()[0]
    orders.move_card(conn, cid, stage(conn, "cancel"), LATER, "o", changes={"cancel_reason": "передумал"})       # nothing held any more


def test_given_back_to_an_old_order_still_on_oth(tmp_path):
    conn = db(tmp_path)
    old = card_at(conn, "oth", T)
    cid = new_order(conn, qty=1)
    take(conn, cid)
    oth_sale.untake(conn, cid, orders.get_card(conn, cid)["items"][0]["id"], LATER, "o")
    assert code(conn, old) == "oth" and [i["qty"] for i in orders.get_card(conn, old)["items"]] == [4]


def test_not_given_back_to_an_old_order_that_left_oth(tmp_path):
    conn = db(tmp_path)
    old = card_at(conn, "oth", T)
    cid = new_order(conn, qty=1)
    take(conn, cid)
    orders.move_card(conn, old, stage(conn, "at_point"), LATER, "o")                 # its client came back for the rest
    with pytest.raises(orders.OrderError, match="ушёл с ОТХ"):
        oth_sale.untake(conn, cid, orders.get_card(conn, cid)["items"][0]["id"], LATER, "o")


# ---- WebMIM ----------------------------------------------------------------------------------------------------------------------------

def test_webmim_is_asked_only_for_what_lacks(tmp_path):
    conn = wc.db(tmp_path)
    old = wc.card(conn, goods=(("111", 2),), tags="")
    for step in ("placed", "moving", "oth"):
        orders.move_card(conn, old, wc.stage(conn, step)["id"], wc.NOW, "o")
    cid = wc.card(conn, goods=(("111", 4), ("222", 1)), client_phone=free_phone(conn, "+7 (911) 999-22-33"))
    card = orders.get_card(conn, cid)
    first = next(i for i in card["items"] if i["goods_id"] == "111")
    oth_sale.take(conn, cid, first["id"], oth_sale.offers(conn, card)[first["id"]][0]["from_item"], wc.NOW, "o")
    fake = wc.FakeWebmim()
    assert wc.place(conn, cid, fake)["state"] == "done"
    assert [(c[2]["goodsId"], c[2]["quantity"]) for c in fake.posts("/items")] == [("111", 2), ("222", 1)]
    block = webmim_match.card_block(conn, orders.get_card(conn, cid))
    assert all(d["goods_id"] != "111" or d["panel_qty"] == 2 for d in block["differences"])
    assert webmim_match.differences([{"goods_id": "111", "name": "x", "qty": i["qty"]} for i in oth_sale.supplier_items(
        conn, orders.get_card(conn, cid)) if i["goods_id"] == "111"], {"items": [{"goods_id": "111", "name": "x", "qty": 2}]}) == []


def test_all_from_oth_webmim_is_not_needed(tmp_path):
    conn = wc.db(tmp_path)
    old = wc.card(conn, goods=(("111", 4),), tags="")
    for step in ("placed", "moving", "oth"):
        orders.move_card(conn, old, wc.stage(conn, step)["id"], wc.NOW, "o")
    cid = wc.card(conn, goods=(("111", 2),), client_phone=free_phone(conn, "+7 (911) 999-22-33"))
    card = orders.get_card(conn, cid)
    oth_sale.take(conn, cid, card["items"][0]["id"], oth_sale.offers(conn, card)[card["items"][0]["id"]][0]["from_item"], wc.NOW, "o")
    from datetime import date
    from tires_sync import webmim_create
    assert webmim_create.problems(conn, orders.get_card(conn, cid), date(2026, 10, 23)) == ["Все позиции взяты с ОТХ: заказ в WebMIM не нужен."]


def test_the_purchase_from_webmim_leaves_the_position_from_oth(tmp_path):
    conn = db(tmp_path)
    card_at(conn, "oth", T)
    cid = new_order(conn, qty=6)
    take(conn, cid)
    orders.refresh_purchase(conn, cid, {"1": 4800.0}, "unload", LATER, "o")
    assert sorted(items(conn, cid)) == [("1", 2, 4800.0, False), ("1", 4, BUY_OLD, True)]


# ---- money ------------------------------------------------------------------------------------------------------------------------------

def done_with_sale(conn, cid):
    for step in ("placed", "moving", "at_point", "done"):
        orders.move_card(conn, cid, stage(conn, step), LATER, "o")
    orders.record_sales(conn, cid, "2026-10-29", LATER, "o", "2026-10-29")


def test_the_sale_is_counted_once_at_the_old_purchase_never_at_webmims_price(tmp_path, monkeypatch):
    conn = db(tmp_path)
    old = card_at(conn, "oth", T)
    cid = new_order(conn, qty=6)
    take(conn, cid)
    done_with_sale(conn, cid)
    sales = conn.execute("SELECT id, note, pieces, sale_total, price_total / pieces FROM sale WHERE reversed_at IS NULL ORDER BY id").fetchall()
    assert [s[1] for s in sales] == ["Заказ №%d" % cid] * 2                       # the old order never has a sale
    assert sorted((s[2], s[3], s[4]) for s in sales) == [(2, 12000.0, BUY_NOW), (4, 24000.0, BUY_OLD)]
    from_oth = conn.execute("SELECT sale_id FROM order_item_oth").fetchone()[0]
    assert from_oth == next(s[0] for s in sales if s[2] == 4)
    monkeypatch.setattr(order_report, "webmim_prices", lambda _c, ids: {cid: {"1": 5000.0}} if cid in ids else {})
    monkeypatch.setattr(order_report, "WEBMIM_FROM", "2000-01-01")
    names = [c.strip() for c in sales_store.COLUMNS.split(",")]
    rows = [dict(zip(names, r)) for r in conn.execute("SELECT %s FROM sale ORDER BY id" % sales_store.COLUMNS)]
    counted = {r["id"]: r for r in order_report._counted(conn, rows)}
    assert not counted[from_oth].get("webmim") and counted[from_oth]["price_total"] == rows[[r["id"] for r in rows].index(from_oth)]["price_total"]
    assert all(r.get("webmim") for i, r in counted.items() if i != from_oth)
    assert code(conn, old) == "cancel" and orders.get_card(conn, old)["sale_ids"] == []


# ---- the Google table ------------------------------------------------------------------------------------------------------------------

def test_the_table_lowers_the_old_row_and_marks_the_new_one(tmp_path):
    conn = db(tmp_path)
    old = card_at(conn, "oth", T)
    item = orders.get_card(conn, old)["items"][0]["id"]
    mark = order_sheet.key(old, item)
    written = order_sheet.rows_of(conn, old, T)
    assert written[mark]["Кол-во"] == 4 and written[mark]["Реализация"] == "О"
    cid = new_order(conn, qty=3)
    take(conn, cid)
    follow = {order_sheet.key(old, i) for i in oth_sale.given(conn, old)}
    ops = order_sheet.changes(order_sheet.rows_of(conn, old, T), written, follow)
    assert ops == [{"key": mark, "create": False, "fields": {"Кол-во": 1, "Сумма MIM": 4000.0, "Сумма продажи": 5000.0},
                    "check": {"Артикул": written[mark]["Артикул"]}}]
    other = dict(written, **{mark: dict(written[mark], **{"Кол-во": 9})})
    assert order_sheet.changes(order_sheet.rows_of(conn, old, T), other) == [op for op in order_sheet.changes(
        order_sheet.rows_of(conn, old, T), other) if "Кол-во" not in op["fields"]]                   # without `follow`: written once, as before
    done_with_sale(conn, cid)
    rows = order_sheet.rows_of(conn, cid, LATER)
    row = rows[order_sheet.key(cid, orders.get_card(conn, cid)["items"][0]["id"])]
    assert row["Реализация"] == "Б" and row["№ заказа MIM"] == orders.get_card(conn, old)["webmim_number"] and row["Цена MIM"] == BUY_OLD
    assert row["Примечание"].startswith("с ОТХ, заказ №%d" % old) and row["Прибыль ₽"] != ""


def test_the_old_row_follows_a_give_back_too(tmp_path):
    conn = db(tmp_path)
    old = card_at(conn, "oth", T)
    cid = new_order(conn, qty=4)
    take(conn, cid)
    lowered = order_sheet.rows_of(conn, old, T)
    oth_sale.untake(conn, cid, orders.get_card(conn, cid)["items"][0]["id"], LATER, "o")
    follow = {order_sheet.key(old, i) for i in oth_sale.given(conn, old)}
    ops = order_sheet.changes(order_sheet.rows_of(conn, old, T), lowered, follow)
    assert [op["fields"]["Кол-во"] for op in ops] == [4]


# ---- the panel ----------------------------------------------------------------------------------------------------------------------

def panel(tmp_path):
    app = make_app(tmp_path)
    owner = app.test_client()
    tok = login(owner)
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, T)
    return app, owner, tok, conn


def test_the_card_shows_the_tyres_on_oth_and_takes_them_by_the_button(tmp_path):
    app, owner, tok, conn = panel(tmp_path)
    old = card_at(conn, "oth", T)
    cid = new_order(conn, qty=2)
    html = owner.get("/orders/%d" % cid).get_data(as_text=True)
    assert "На Бета есть 4 шт (ОТХ, заказ" in html and 'href="/orders/%d"' % old in html and "Взять с ОТХ 2 шт" in html
    form = re.search(r'action="/orders/%d/items/(\d+)/oth-take".*?name="from_item" value="(\d+)"' % cid, html)
    r = post(owner, "/orders/%d/items/%s/oth-take" % (cid, form.group(1)), tok, from_item=form.group(2))
    assert r.status_code == 302
    html = owner.get("/orders/%d" % cid).get_data(as_text=True)
    assert "с ОТХ · заказ №%d" % old in html and "Вернуть на ОТХ" in html and "Взять с ОТХ" not in html
    item = orders.get_card(conn, cid)["items"][0]["id"]
    post(owner, "/orders/%d/items/%d/oth-untake" % (cid, item), tok)
    assert orders.get_card(conn, cid)["items"] == [] and oth_stock.by_point(conn) == {("1", point_id(conn)): 4}


def test_the_list_where_a_position_is_chosen_marks_the_tyres_on_oth(tmp_path):
    conn = db(tmp_path)
    card_at(conn, "oth", T)
    assert oth_sale.find_hint(conn, ["1", "2"]) == {"1": "на ОТХ: Бета 4 шт"}


def test_a_manager_of_a_point_cannot_take_from_oth(tmp_path):
    app, owner, tok, conn = panel(tmp_path)
    card_at(conn, "oth", T)
    cid = new_order(conn, qty=2)
    users.add_user(conn, "point1", "длинный-пароль-1", T, role="point", point_id=point_id(conn))
    c = app.test_client()
    with c.session_transaction() as session:
        session.update(user="point1", epoch=store.get_session_epoch(conn, "point1"), csrf="tok")
    o = offer(conn, cid)
    r = c.post("/orders/%d/items/%d/oth-take" % (cid, line(conn, cid)["id"]), data={"csrf": "tok", "from_item": o["from_item"]})
    assert r.status_code in (302, 403)
    assert items(conn, cid) == [("1", 2, BUY_NOW, False)] and oth_stock.by_point(conn) == {("1", point_id(conn)): 4}
    from tires_sync.panel import common
    assert "order_oth_take" not in common.POINT_PAGES and "order_oth_untake" not in common.POINT_PAGES


# ---- the review of 2026-10-29 -----------------------------------------------------------------------------------------------------------

def test_the_old_position_and_the_old_order_are_not_deleted_while_their_tyres_are_held(tmp_path):
    conn = db(tmp_path)
    old = card_at(conn, "oth", T)
    item = orders.get_card(conn, old)["items"][0]["id"]
    cid = new_order(conn, qty=2)
    take(conn, cid)
    with pytest.raises(orders.OrderError, match="взяты с ОТХ в заказ №%d" % cid):
        orders.remove_item(conn, item, LATER, "o")
    assert "взяты с ОТХ в заказ №%d" % cid in oth_sale.why_not_delete(conn, old)
    with pytest.raises(orders.OrderError, match="удалить его нельзя"):
        trash.delete_order(conn, old, LATER, "o", only_if=lambda c, _card: oth_sale.why_not_delete(c, old))


def test_tyres_the_old_order_cannot_take_back_have_no_give_back_and_a_refusal_puts_them_on_oth(tmp_path):
    """The owner 2026-10-30: no «Убрать без возврата»; the old order left «На ОТХ» — no «Вернуть на ОТХ», and the refused new order goes to
    «На ОТХ» with its tyres: stock of OTH there (at the first order's purchase), held by the ad, taken by a third order."""
    conn = db(tmp_path)
    old = card_at(conn, "oth", T)
    cid = new_order(conn, qty=2)
    take(conn, cid)
    item = orders.get_card(conn, cid)["items"][0]["id"]
    assert oth_sale.can_untake(conn, cid, item) and not hasattr(oth_sale, "drop")
    orders.move_card(conn, old, stage(conn, "at_point"), LATER, "o")                 # its client came back for the rest
    assert not oth_sale.can_untake(conn, cid, item)
    with pytest.raises(orders.OrderError, match="ушёл с ОТХ.*переведите заказ в «На ОТХ»"):
        oth_sale.untake(conn, cid, item, LATER, "o")
    with pytest.raises(orders.OrderError, match="переведите заказ на «На ОТХ» или верните их"):
        orders.move_card(conn, cid, stage(conn, "cancel"), LATER, "o", changes={"cancel_reason": "передумал"})
    for step in ("placed", "moving", "at_point", "oth"):                              # the client of the new order refused at the point
        orders.move_card(conn, cid, stage(conn, step), LATER, "o")
    assert code(conn, cid) == "oth"
    assert oth_stock.by_point(conn) == {("1", point_id(conn)): 2}                    # the new order's; the old one is a client's again
    assert [(r["order_id"], r["qty"]) for r in oth_stock._rows(conn) if r["order_id"] == cid] == [(cid, 2)]
    assert orders.get_card(conn, cid)["items"][0]["supplier_price"] == BUY_OLD
    third = new_order(conn, qty=2)
    o = next(x for x in oth_sale.offers(conn, orders.get_card(conn, third))[line(conn, third)["id"]] if x["from_order"] == cid)
    oth_sale.take(conn, third, line(conn, third)["id"], o["from_item"], LATER, "o")
    assert code(conn, cid) == "cancel" and items(conn, third) == [("1", 2, BUY_OLD, True)]
    assert oth_sale.first_order(conn, orders.get_card(conn, third)["items"][0]["id"]) == old


def test_nothing_goes_back_into_an_agreed_return(tmp_path):
    conn = db(tmp_path)
    old = card_at(conn, "oth", T)
    cid = new_order(conn, qty=1)
    take(conn, cid)
    conn.execute("INSERT INTO oth_return_plan (order_id, agreed_at, agreed_by, return_date) VALUES (?,?,?,?)", (old, T, "o", "2026-10-29"))
    conn.commit()
    item = orders.get_card(conn, cid)["items"][0]["id"]
    assert not oth_sale.can_untake(conn, cid, item)
    with pytest.raises(orders.OrderError, match="согласован возврат"):
        oth_sale.untake(conn, cid, item, LATER, "o")
    assert [i["qty"] for i in orders.get_card(conn, old)["items"]] == [3]


def test_a_second_press_that_read_before_the_first_was_saved_takes_nothing(tmp_path, monkeypatch):
    conn = db(tmp_path)
    old = card_at(conn, "oth", T)
    cid = new_order(conn, qty=2)
    stale_card, stale_rows, line_id = orders.get_card(conn, cid), oth_stock._rows(conn), line(conn, cid)["id"]
    from_item = offer(conn, cid)["from_item"]
    take(conn, cid)
    real = orders.get_card
    monkeypatch.setattr(orders, "get_card", lambda c, i: stale_card if i == cid else real(c, i))
    monkeypatch.setattr(oth_stock, "_rows", lambda _c: stale_rows)
    with pytest.raises(orders.OrderError, match="только что изменили"):
        oth_sale.take(conn, cid, line_id, from_item, LATER, "o")
    monkeypatch.undo()
    assert items(conn, cid) == [("1", 2, BUY_OLD, True)] and [i["qty"] for i in orders.get_card(conn, old)["items"]] == [2]


def test_an_emptied_position_does_not_stop_the_sale_of_the_rest(tmp_path):
    conn = db(tmp_path)
    old = card_at(conn, "oth", T)
    orders.add_item(conn, old, goods_id="2", name="Шина 2", brand_key="nokian", season="winter", diameter=16, territory="1", qty=2, sale_unit=5000,
                    supplier_price=4000, terms=None, fee_pct=0, promo_pct=0, now=T, author="o")
    cid = new_order(conn, qty=4)
    take(conn, cid)
    assert code(conn, old) == "oth" and sorted(i["qty"] for i in orders.get_card(conn, old)["items"]) == [0, 2]
    orders.move_card(conn, old, stage(conn, "at_point"), LATER, "o")
    orders.move_card(conn, old, stage(conn, "done"), LATER, "o")
    ids = orders.record_sales(conn, old, "2026-10-29", LATER, "o", "2026-10-29")
    assert len(ids) == 1 and conn.execute("SELECT goods_id, pieces FROM sale WHERE id = ?", (ids[0],)).fetchone() == ("2", 2)


def test_the_terms_of_the_supplier_are_the_old_orders_with_its_purchase(tmp_path):
    conn = db(tmp_path)
    old = card_at(conn, "oth", T)
    conn.execute("UPDATE order_item SET terms = ? WHERE order_id = ?", ('{"discount_pct": 7}', old))
    conn.commit()
    cid = new_order(conn, qty=2)
    conn.execute("UPDATE order_item SET terms = ?, fee_pct = 9 WHERE order_id = ?", ('{"discount_pct": 2}', cid))
    conn.commit()
    take(conn, cid)
    got = orders.get_card(conn, cid)["items"][0]
    assert got["terms"] == {"discount_pct": 7} and got["fee_pct"] == 9                # the purchase side is the old order's, the sale side the new one's


def test_along_a_chain_the_table_names_the_webmim_order_of_the_first(tmp_path):
    conn = db(tmp_path)
    first = card_at(conn, "oth", T)
    second = new_order(conn, qty=4)
    take(conn, second)
    for step in ("placed", "moving", "oth"):
        orders.move_card(conn, second, stage(conn, step), LATER, "o")
    third = new_order(conn, qty=4)
    assert offer(conn, third)["from_order"] == second
    take(conn, third)
    assert code(conn, second) == "cancel"
    item = orders.get_card(conn, third)["items"][0]["id"]
    assert oth_sale.first_order(conn, item) == first
    assert oth_sale.from_number(conn, third) == {item: orders.get_card(conn, first)["webmim_number"]}


def test_the_card_offers_no_give_back_once_the_order_is_placed(tmp_path):
    app, owner, tok, conn = panel(tmp_path)
    card_at(conn, "oth", T)
    cid = new_order(conn, qty=2)
    take(conn, cid)
    assert "Вернуть на ОТХ" in owner.get("/orders/%d" % cid).get_data(as_text=True)
    orders.move_card(conn, cid, stage(conn, "placed"), LATER, "o")
    html = owner.get("/orders/%d" % cid).get_data(as_text=True)
    assert "с ОТХ · заказ" in html and "Вернуть на ОТХ" not in html and "Убрать без возврата" not in html


# ---- «Шины уже на точке» (the owner 2026-10-30) ----------------------------------------------------------------------------------------

def test_the_ad_holds_the_tyres_of_a_refused_order_that_took_them_from_oth(tmp_path):
    conn = db(tmp_path)
    card_at(conn, "oth", T)
    cid = new_order(conn, qty=4)
    take(conn, cid)
    for step in ("placed", "moving", "at_point", "oth"):
        orders.move_card(conn, cid, stage(conn, step), LATER, "o")
    assert oth_stock.pieces(conn) == {"1": 4} and oth_stock.points(conn) == {"1": ("Бета",)}


def test_only_an_order_whose_tyres_all_come_from_oth_is_offered_the_move_to_the_point(tmp_path):
    conn = db(tmp_path)
    card_at(conn, "oth", T)
    whole, mixed, other = new_order(conn, qty=2), new_order(conn, qty=6), new_order(conn, qty=1, goods="2")
    assert not oth_sale.all_from_oth(conn, orders.get_card(conn, whole))
    take(conn, whole)
    take(conn, mixed)
    assert oth_sale.all_from_oth(conn, orders.get_card(conn, whole))
    assert not oth_sale.all_from_oth(conn, orders.get_card(conn, mixed)) and not oth_sale.all_from_oth(conn, orders.get_card(conn, other))
    orders.move_card(conn, whole, stage(conn, "placed"), LATER, "o")
    assert not oth_sale.all_from_oth(conn, orders.get_card(conn, whole))


def test_the_point_text_names_the_old_order_the_client_and_the_reservation(tmp_path):
    conn = db(tmp_path)
    old = card_at(conn, "oth", T)
    cid = new_order(conn, qty=2)
    take(conn, cid)
    orders.move_card(conn, cid, stage(conn, "at_point"), LATER, "o")
    text = oth_sale.point_text(conn, orders.get_card(conn, cid), LATER)
    card = orders.get_card(conn, cid)
    assert text == ("Шины с ОТХ (заказ №%s) отложены под нового клиента: заказ №%d, Пётр, %s, Nokian 205/55 R16 × 2. Не продавать другим, "
                    "резерв до 01.11.2026." % (orders.get_card(conn, old)["webmim_number"], cid, card["client_phone"]))     # the number the point knows
    conn.execute("INSERT INTO order_template (name, audience, body) VALUES (?, 'manager', ?)", (oth_sale_template(), "Свой текст №{order_no}"))
    conn.commit()
    assert oth_sale.point_text(conn, card, LATER) == "Свой текст №%d" % cid                  # the owner's template wins


def oth_sale_template():
    from tires_sync import order_templates
    return order_templates.OTH_POINT_SAMPLE[0]


def test_the_button_moves_to_the_point_past_placed_and_tells_the_client_once_and_the_point_its_text(tmp_path, monkeypatch):
    app, owner, tok, conn = panel(tmp_path)
    card_at(conn, "oth", T)
    cid = new_order(conn, qty=2)
    take(conn, cid)
    mixed = new_order(conn, qty=6)
    take(conn, mixed)
    assert "Шины уже на точке" not in owner.get("/orders/%d" % mixed).get_data(as_text=True)
    assert "Шины уже на точке — перевести на «Товар на точке»" in owner.get("/orders/%d" % cid).get_data(as_text=True)
    from tires_sync import order_manager, order_notify
    told_client, told_point = [], []
    monkeypatch.setattr(order_manager, "tell_client_arrived", lambda c, card_id, deliver, now, who, body=None: (told_client.append((card_id, body)), (True, "Клиенту отправлено."))[1])
    monkeypatch.setattr(order_notify, "tell_point", lambda c, box, card, text, now, **kw: (told_point.append(text), "показано точке")[1])
    assert post(owner, "/orders/%d/oth-at-point" % cid, tok).status_code == 302
    post(owner, "/orders/%d/oth-at-point" % cid, tok)                                   # a second press: nothing more
    card = orders.get_card(conn, cid)
    assert card["stage"]["code"] == "at_point" and card["reserve_until"]                # the reservation of the stage, three days
    assert [c for c, _b in told_client] == [cid] and "{webmim_number}" not in told_client[0][1] and "приехал в пункт выдачи" in told_client[0][1]
    assert len(told_point) == 1 and told_point[0].startswith("Шины с ОТХ (заказ №") and "заказ №%d" % cid in told_point[0]
    stages = [e["text"] for e in orders.list_events(conn, cid) if e["kind"] == "stage"]
    assert len(stages) == 1 and "Заказ оформлен" not in stages[0]                         # straight from «Новый»: no «Заказ оформлен» and its message
    assert not webmim_create_state(conn, cid)


def webmim_create_state(conn, cid):
    return conn.execute("SELECT 1 FROM webmim_create WHERE order_id = ?", (cid,)).fetchone()


def test_a_point_cannot_press_it(tmp_path):
    app, owner, tok, conn = panel(tmp_path)
    card_at(conn, "oth", T)
    cid = new_order(conn, qty=2)
    take(conn, cid)
    users.add_user(conn, "point2", "длинный-пароль-1", T, role="point", point_id=point_id(conn))
    c = app.test_client()
    with c.session_transaction() as session:
        session.update(user="point2", epoch=store.get_session_epoch(conn, "point2"), csrf="tok")
    assert c.post("/orders/%d/oth-at-point" % cid, data={"csrf": "tok"}).status_code in (302, 403)
    assert code(conn, cid) == "new"


def test_an_order_all_from_oth_is_never_asked_for_a_webmim_number(tmp_path):
    from tires_sync import order_reserve
    conn = db(tmp_path)
    card_at(conn, "oth", T)
    cid = new_order(conn, qty=2)
    take(conn, cid)
    orders.move_card(conn, cid, stage(conn, "at_point"), LATER, "o")
    card = orders.get_card(conn, cid)
    told = []
    order_reserve._ask_number(conn, card, lambda c, t: (told.append(t), "ok")[1], LATER)
    assert told == [] and not any(order_reserve.NO_NUMBER_MARK in e["text"] for e in orders.list_events(conn, cid))
    mixed = new_order(conn, qty=6)
    take(conn, mixed)
    order_reserve._ask_number(conn, orders.get_card(conn, mixed), lambda c, t: (told.append(t), "ok")[1], LATER)
    assert len(told) == 1                                                            # its supplier's part has a WebMIM order to name


def test_the_point_text_goes_to_the_points_own_page_for_an_order_of_its_account(tmp_path, monkeypatch):
    from tires_sync import order_notify, order_points
    conn = db(tmp_path)
    card_at(conn, "oth", T)
    cid = new_order(conn, qty=2)
    seen = {}
    monkeypatch.setattr(order_notify, "deliver", lambda c, box, card, text, now, **kw: (seen.update(kw["way"]), "ok")[1])
    monkeypatch.setattr(order_points, "of_account", lambda c, order_id: {"id": point_id(conn)})
    order_notify.tell_point(conn, None, orders.get_card(conn, cid), "x", LATER)
    assert seen["own_point"] == point_id(conn)
