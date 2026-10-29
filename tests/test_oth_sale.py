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
