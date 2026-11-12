"""The owner's answers of 2026-11-10 on the record of storage (oth_storage): a record is never a source of a merge (2); a record cancelled
by hand by mistake goes back on «На ОТХ» (3); any move of a record to «Выполнено» is the point's «Продать» (order_oth.sell) (4); and the low
points of the review: the panel's routes of the record, a second split after a give-back, one sale per «Продать»."""
from __future__ import annotations

from datetime import date

import pytest

from panel_helpers import post
from test_oth_sale import new_order, panel, take
from test_oth_seventh_day import asked_by_hand, started
from test_oth_storage import LATER, MOVED, at_point, split
from test_order_reserve import db, stage
from tires_sync import (order_bulk, order_engine as engine, order_merge, order_oth, order_rules as rules, order_templates as tpl, orders, oth_sale,
                        oth_stock, oth_storage, point_log)

OTH_SPLIT = True
LATE = "2026-10-22T10:00:00"


def code(conn, cid):
    return orders.get_card(conn, cid)["stage"]["code"]


# ---- 2: the merge ---------------------------------------------------------------------------------------------------------------------

def test_a_record_is_never_a_source_of_a_merge(tmp_path):
    conn = db(tmp_path)
    cid, x = split(conn)
    other = new_order(conn, qty=4)
    with pytest.raises(order_merge.MergeError, match="Шины с ОТХ берутся кнопкой «Взять с ОТХ»"):
        order_merge.merge(conn, [x, other], stage(conn, "at_point"), LATER, "o", client_from=other)
    assert code(conn, x) == "oth" and order_merge.into(conn, x) is None and oth_stock.pieces(conn) == {"1": 4}


# ---- 3: a record cancelled by hand by mistake goes back on «На ОТХ» ---------------------------------------------------------------------

def test_a_record_cancelled_by_hand_by_mistake_goes_back_on_oth_with_the_same_tyres(tmp_path):
    conn = db(tmp_path)
    cid, x = split(conn)
    assert orders.move_card(conn, x, stage(conn, "cancel"), LATER, "o", changes={"cancel_reason": "другое: ошиблись"})
    assert oth_stock.pieces(conn) == {}
    assert orders.move_card(conn, x, stage(conn, "oth"), LATE, "o")
    assert code(conn, x) == "oth" and oth_storage.record(conn, x)["state"] == oth_storage.HELD
    assert oth_stock.pieces(conn) == {"1": 4} and oth_storage.record_ids(conn) == {x}               # no second record, no doubles
    assert code(conn, cid) == "cancel" and oth_storage.why_not_back(conn, cid) is None             # the client may still be called back
    assert orders.get_card(conn, x)["cancel_reason"] == ""
    assert any("отмена была ошибкой" in e["text"] for e in orders.list_events(conn, x))
    with pytest.raises(orders.OrderError, match="записи хранения №%d" % x):
        orders.move_card(conn, cid, stage(conn, "oth"), LATE, "o")                                  # Б1 still: the cancelled order never
    b = new_order(conn, qty=4)
    assert take(conn, b, now=LATE) == 4 and code(conn, x) == "cancel" and oth_stock.pieces(conn) == {}


def test_a_record_cancelled_by_mistake_does_not_go_to_return_given(tmp_path):
    conn = db(tmp_path)
    cid, x = split(conn)
    orders.move_card(conn, x, stage(conn, "cancel"), LATER, "o", changes={"cancel_reason": "другое: ошиблись"})
    with pytest.raises(orders.OrderError, match="Запись хранения №%d закрыта" % x):
        orders.move_card(conn, x, stage(conn, orders.RETURN_GIVEN), LATE, "o")


def _given_back(conn):
    cid, x = split(conn)
    assert orders.move_card(conn, cid, stage(conn, "at_point"), LATER, "o")
    return x


def _taken(conn):
    cid, x = split(conn)
    take(conn, new_order(conn, qty=4), now=LATER)
    return x


@pytest.mark.parametrize("closed", [_given_back, _taken])
def test_a_record_closed_by_the_take_or_the_give_back_never_goes_back(tmp_path, closed):
    conn = db(tmp_path)
    x = closed(conn)
    assert code(conn, x) == "cancel" and not oth_storage.cancelled_by_mistake(conn, orders.get_card(conn, x))
    with pytest.raises(orders.OrderError, match="Запись хранения №%d закрыта" % x):
        orders.move_card(conn, x, stage(conn, "oth"), LATE, "o")
    assert code(conn, x) == "cancel" and oth_stock.pieces(conn) == {}


# ---- 4: any move of a record to «Выполнено» is the point's «Продать» -------------------------------------------------------------------

def sale_rules(conn):
    """The sale of «Выполнено» and a message to the client, as on production."""
    done = stage(conn, "done")
    rules.add_rule(conn, done, "enter", 0, "record_sale", None, "", "auto", None)
    thanks = tpl.add_template(conn, "Клиенту: спасибо", "client", "{client_name}, спасибо за покупку!")
    rules.add_rule(conn, done, "enter", 0, "message", thanks, "", "auto", None)
    conn.commit()
    return done


def sales(conn, cid):
    return conn.execute("SELECT pieces FROM sale WHERE note = ? AND reversed_at IS NULL", ("Заказ №%d" % cid,)).fetchall()


def sold_like_the_point(conn, x, who):
    rec = orders.get_card(conn, x)
    assert rec["stage"]["code"] == "done" and rec["client_name"] == "" and oth_stock.pieces(conn) == {}
    assert sales(conn, x) == [(4,)] and len(rec["sale_ids"]) == 1                                   # one sale, the rule's
    [event] = [e for e in orders.list_events(conn, x) if e["text"] == order_oth.OFFICE_SOLD]
    assert event["author"] == who
    done = stage(conn, "done")
    messages = [t for t in engine.list_tasks(conn, x) if t["kind"] == "message" and t["stage_id"] == done]
    assert messages and all(t["status"] == "skipped" and t["result"] == order_oth.SOLD_SKIP for t in messages)
    [row] = [r for r in point_log.history(conn, rec["point_id"]) if r["kind"] == "oth_sold"]
    assert row["author"] == who and row["order_id"] == x                                            # «История точки»
    assert point_log._kept(conn, x)[1]["action"] == "oth_sold"                                      # the point's «Отменить» (after the storno)


def test_the_office_moving_a_record_to_done_sells_it_as_the_point_does(tmp_path):
    conn = db(tmp_path)
    sale_rules(conn)
    started(conn)
    cid, x = split(conn)
    asked_by_hand(conn, x, "2026-10-27T12:30:00")                                                  # E1: the question of a move by hand
    assert order_oth.question_open(conn, orders.get_card(conn, x))
    assert orders.move_card(conn, x, stage(conn, "done"), "2026-10-27T13:00:00", "офис")
    sold_like_the_point(conn, x, "офис")
    assert not order_oth.question_open(conn, orders.get_card(conn, x))                              # the question of the seventh day: answered
    assert not any(e["text"] == "Точка продала шины с ОТХ" for e in orders.list_events(conn, x))


def test_an_agreed_record_refuses_the_move_as_the_point_refuses_the_sale(tmp_path):
    """The owner 2026-11-10: agreed, the tyres go back to the supplier, «Продать» is refused; the office's move to «Выполнено» is the same sale
    (_sell_record): refused, the agreement and the record stay. (Before 2026-11-10 it dropped the agreement, «Отменить» brought it back.)"""
    conn = db(tmp_path)
    cid, x = split(conn)
    order_oth.agree(conn, orders.get_card(conn, x), date(2026, 10, 21), LATER, "офис")
    assert orders.get_card(conn, x)["stage"]["code"] == orders.RETURN_GIVEN and order_oth.return_plan(conn, x)
    with pytest.raises(orders.OrderError, match="возврат уже согласован"):
        orders.move_card(conn, x, stage(conn, "done"), LATE, "офис")
    assert code(conn, x) == orders.RETURN_GIVEN and order_oth.return_plan(conn, x) is not None and oth_storage.record_ids(conn) == {x}
    refused = [e["text"] for e in orders.list_events(conn, x) if e["kind"] == "refused"]
    assert len(refused) == 1 and "как «Продать» точки" in refused[0]


def test_tyres_given_back_to_the_supplier_refuse_the_move_as_they_refuse_the_sale(tmp_path):
    conn = db(tmp_path)
    cid, x = split(conn)
    order_oth.mark_returned(conn, orders.get_card(conn, x)["items"][0]["id"], LATER, "o", qty=2)
    conn.commit()
    with pytest.raises(orders.OrderError, match="часть шин уже возвращена поставщику"):
        orders.move_card(conn, x, stage(conn, "done"), LATE, "офис")
    assert code(conn, x) == "oth"
    refused = [e["text"] for e in orders.list_events(conn, x) if e["kind"] == "refused"]
    assert len(refused) == 1 and "как «Продать» точки" in refused[0]                               # one red record, not two


def test_the_bulk_move_sells_a_record_and_moves_a_plain_order_as_before(tmp_path):
    conn = db(tmp_path)
    sale_rules(conn)
    cid, x = split(conn)
    plain = new_order(conn, qty=2)
    done = stage(conn, "done")
    plan = order_bulk.check(conn, [x, plain], done, "")
    assert {c["id"] for c in plan["ok"]} == {x, plain}
    out = order_bulk.run(conn, tmp_path / "x.db", [x, plain], done, "", plan["sign"], LATE, "офис")
    assert out["moved"] == 2
    sold_like_the_point(conn, x, "офис")
    assert code(conn, plain) == "done" and sales(conn, plain) == []                                 # the bulk move fires no rule of a plain order


def test_the_card_and_the_board_of_the_panel_sell_a_record(tmp_path):
    app, owner, tok, conn = panel(tmp_path)
    sale_rules(conn)
    cid, x = split(conn)
    r = post(owner, "/orders/%d/move" % x, tok, stage_id=str(stage(conn, "done")))
    assert r.status_code in (200, 302)
    sold_like_the_point(conn, x, "owner")


def test_the_points_sell_of_a_record_records_the_sale_once(tmp_path):
    conn = db(tmp_path)
    sale_rules(conn)
    cid, x = split(conn)
    outcome, answer, done = order_oth.sell(conn, orders.get_card(conn, x), "менеджер Бета", LATE)
    assert done, answer
    assert sales(conn, x) == [(4,)] and len(orders.get_card(conn, x)["sale_ids"]) == 1
    assert [e["kind"] for e in orders.list_events(conn, x) if e["text"] == "Точка продала шины с ОТХ"] == ["point"]
    again = order_oth.sell(conn, orders.get_card(conn, x), "менеджер Бета", LATE)
    assert not again[2] and sales(conn, x) == [(4,)]


def test_a_record_cancelled_by_mistake_goes_to_done_only_from_oth(tmp_path):
    conn = db(tmp_path)
    cid, x = split(conn)
    orders.move_card(conn, x, stage(conn, "cancel"), LATER, "o", changes={"cancel_reason": "другое: ошиблись"})
    with pytest.raises(orders.OrderError, match="только продажей с «На ОТХ»"):
        orders.move_card(conn, x, stage(conn, "done"), LATE, "офис")
    assert code(conn, x) == "cancel"


# ---- the review (low): the panel's routes of a record, a second split, one sale ------------------------------------------------------------

def _taken_by_route(owner, tok, conn, x):
    b = new_order(conn, qty=4)
    line = next(i for i in orders.get_card(conn, b)["items"])
    source = orders.get_card(conn, x)["items"][0]["id"]
    r = post(owner, "/orders/%d/items/%d/oth-take" % (b, line["id"]), tok, from_item=str(source))
    assert r.status_code == 302
    return b


def test_the_routes_take_and_untake_a_record_whole(tmp_path):
    app, owner, tok, conn = panel(tmp_path)
    cid, x = split(conn)
    b = _taken_by_route(owner, tok, conn, x)
    assert len(oth_sale.taken_ids(conn, b)) == 1 and code(conn, x) == "cancel" and oth_stock.pieces(conn) == {}
    assert oth_storage.record(conn, x)["state"] == oth_storage.TAKEN and oth_storage.record(conn, x)["to"] == b
    [item] = oth_sale.taken_ids(conn, b)
    r = post(owner, "/orders/%d/items/%d/oth-untake" % (b, item), tok)
    assert r.status_code == 302 and oth_sale.taken_ids(conn, b) == set()
    assert code(conn, x) == "oth" and oth_storage.record(conn, x)["state"] == oth_storage.HELD and oth_stock.pieces(conn) == {"1": 4}


def test_the_route_back_of_a_record_to_a_new_client_takes_it_whole(tmp_path):
    app, owner, tok, conn = panel(tmp_path)
    cid, x = split(conn)
    post(owner, "/orders/%d/move" % x, tok, stage_id=str(stage(conn, "at_point")))                 # asks «новый клиент или тот же?»
    assert code(conn, x) == "oth"
    r = post(owner, "/orders/%d/oth-back" % x, tok, answer="new", client_name="Пётр", client_phone="+7 900 555 66 77")
    assert r.status_code == 302
    new = int(r.headers["Location"].rsplit("/", 1)[1])
    assert new != x and code(conn, new) == "at_point" and orders.get_card(conn, new)["client_name"] == "Пётр"
    assert code(conn, x) == "cancel" and oth_storage.record(conn, x)["to"] == new and oth_stock.pieces(conn) == {}
    assert code(conn, cid) == "cancel" and oth_storage.why_not_back(conn, cid)                    # the old client is too late


def test_the_route_back_of_a_record_to_the_same_client_gives_the_order_back(tmp_path):
    app, owner, tok, conn = panel(tmp_path)
    cid, x = split(conn)
    post(owner, "/orders/%d/move" % x, tok, stage_id=str(stage(conn, "at_point")))
    r = post(owner, "/orders/%d/oth-back" % x, tok, answer="same")
    assert r.status_code == 302
    assert code(conn, cid) == "at_point" and code(conn, x) == "cancel" and oth_storage.record(conn, x)["state"] == oth_storage.BACK
    assert oth_stock.pieces(conn) == {} and conn.execute("SELECT COUNT(*) FROM order_card").fetchone()[0] == 2


def test_given_back_to_the_client_and_on_oth_again_makes_one_new_record_of_four(tmp_path):
    conn = db(tmp_path)
    cid, x = split(conn)
    assert orders.move_card(conn, cid, stage(conn, "at_point"), LATER, "o")
    assert oth_stock.pieces(conn) == {}
    assert orders.move_card(conn, cid, stage(conn, "oth"), LATE, "o", changes={"cancel_reason": "отказ"})
    second = oth_storage.record_of(conn, cid)
    assert second not in (None, x) and oth_storage.record_ids(conn) == {x, second}
    assert code(conn, x) == "cancel" and code(conn, second) == "oth" and oth_stock.pieces(conn) == {"1": 4}     # 4, not 8
