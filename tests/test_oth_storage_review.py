"""The review of the record of storage (oth_storage, the owner 2026-11-10) and the owner's answers of 2026-11-10:
Б1 a cancelled order whose tyres lie in a record never goes to «На ОТХ» again (a second record, the tyres twice in the feed);
Б2 a closed record (given back, taken) never stands on «На ОТХ» again but by «Вернуть на ОТХ» of the order that took it (cancelled by hand by
mistake: it may, the owner 2026-11-10, tests/test_oth_storage_owner.py);
В1 «Вернуть на ОТХ» gives back the whole record in one transaction; В2/В3 a record has no «Выдали» (the tyres are given out from «На точке»),
«Продать» closes it in «Выполнено» without a client and «Отменить» puts it back without a new split; В7 the point is told nothing in VK
when an order goes to «На ОТХ»; М2 «Доставка в другой город» and the numbers by position go with the tyres; М3 any error of the split rolls
everything back; М4 the take never commits the caller's transaction; М5 the order given back keeps «one open order per phone»."""
from __future__ import annotations

import pytest

from test_mypoint import manager
from test_oth_sale import new_order, panel, take
from test_oth_seventh_day import asked_by_hand, started
from test_oth_storage import ARRIVED, LATER, MOVED, TRANSFER, at_point, number, record, split
from test_order_reserve import POINT_VK, db, stage
from tires_sync import (order_manager as mgr, order_oth, order_reserve as reserve, orders, oth_sale, oth_stock, oth_storage, point_log)

OTH_SPLIT = True
LATE = "2026-10-22T10:00:00"


def code(conn, cid):
    return orders.get_card(conn, cid)["stage"]["code"]


def records(conn):
    return oth_storage.record_ids(conn)


# ---- Б1: the cancelled order never goes to «На ОТХ» again --------------------------------------------------------------------------------

@pytest.mark.parametrize("bulk", [False, True])
def test_the_cancelled_order_is_not_put_on_oth_again_while_its_tyres_lie_in_the_record(tmp_path, bulk):
    conn = db(tmp_path)
    cid, x = split(conn)
    how = dict(fire_rules=False, journal=False) if bulk else {}
    with pytest.raises(orders.OrderError, match="записи хранения №%d" % x):
        orders.move_card(conn, cid, stage(conn, "oth"), LATER, "o", **how)
    assert code(conn, cid) == "cancel" and records(conn) == {x} and oth_stock.pieces(conn) == {"1": 4}
    with pytest.raises(orders.OrderError, match="записи хранения №%d" % x):
        orders.move_card(conn, cid, stage(conn, "oth"), LATER, "o", oth_split=False)          # nor the old way: the tyres would lie twice
    with pytest.raises(orders.OrderError, match="записи хранения №%d" % x):
        orders.move_card(conn, cid, stage(conn, orders.RETURN_GIVEN), LATER, "o")
    assert code(conn, cid) == "cancel" and oth_stock.pieces(conn) == {"1": 4}


def test_the_cancelled_order_whose_record_was_taken_is_not_put_on_oth(tmp_path):
    conn = db(tmp_path)
    cid, x = split(conn)
    b = new_order(conn, qty=4)
    take(conn, b, now=LATER)
    with pytest.raises(orders.OrderError, match="записи хранения №%d" % x):
        orders.move_card(conn, cid, stage(conn, "oth"), LATE, "o")
    assert code(conn, cid) == "cancel" and records(conn) == {x} and oth_stock.pieces(conn) == {}


# ---- Б2: a closed record never stands on «На ОТХ» again by itself --------------------------------------------------------------------------

def given_back(conn):
    cid, x = split(conn)
    assert orders.move_card(conn, cid, stage(conn, "at_point"), LATER, "o")
    return cid, x


def taken_by_b(conn):
    cid, x = split(conn)
    take(conn, new_order(conn, qty=4), now=LATER)
    return cid, x


@pytest.mark.parametrize("closed", [given_back, taken_by_b])          # cancelled by hand: back on «На ОТХ» (test_oth_storage_owner.py)
def test_a_closed_record_never_stands_on_oth_again(tmp_path, closed):
    conn = db(tmp_path)
    cid, x = closed(conn)
    assert code(conn, x) == "cancel"
    for target, how in (("oth", {}), ("oth", dict(oth_split=False)), ("oth", dict(fire_rules=False, journal=False)), (orders.RETURN_GIVEN, {})):
        with pytest.raises(orders.OrderError, match="Запись хранения №%d закрыта" % x):
            orders.move_card(conn, x, stage(conn, target), LATE, "o", **how)
        assert code(conn, x) == "cancel"
    assert oth_stock.pieces(conn) == {} and order_oth.goods_on_oth(conn) == {}


def test_the_record_given_back_and_taken_back_by_mistake_by_its_order(tmp_path):
    """The only way back: «Вернуть на ОТХ» of the order that took the record (the review's guard of В1 is below)."""
    conn = db(tmp_path)
    cid, x = split(conn)
    b = new_order(conn, qty=4)
    take(conn, b, now=LATER)
    [item] = oth_sale.taken_ids(conn, b)
    assert oth_sale.untake(conn, b, item, LATE, "o") == 4
    assert code(conn, x) == "oth" and oth_storage.record(conn, x)["state"] == oth_storage.HELD and oth_stock.pieces(conn) == {"1": 4}


# ---- В1: «Вернуть на ОТХ» gives back the whole record ----------------------------------------------------------------------------------

def two_positions(conn):
    cid = at_point(conn)
    orders.add_item(conn, cid, goods_id="2", name="Шина 2", brand_key="b", season="winter", diameter=16, territory="1", qty=2, sale_unit=7000,
                    supplier_price=4500, terms=None, fee_pct=0, promo_pct=0, now=ARRIVED, author="o")
    orders.move_card(conn, cid, stage(conn, "oth"), MOVED, "o", changes={"cancel_reason": "отказ"})
    x = record(conn, cid)
    b = new_order(conn, qty=4, extra=(("2", 2),))
    assert take(conn, b, now=LATER) == 6
    return cid, x, b


def test_giving_back_one_position_of_a_record_gives_back_the_whole_record(tmp_path):
    conn = db(tmp_path)
    cid, x, b = two_positions(conn)
    first = sorted(oth_sale.taken_ids(conn, b))[0]
    assert oth_sale.untake(conn, b, first, LATE, "o") == 6                                 # every tyre of the record, in one transaction
    assert oth_sale.taken_ids(conn, b) == set() and code(conn, x) == "oth"
    assert sorted((i["goods_id"], i["qty"]) for i in orders.get_card(conn, x)["items"]) == [("1", 4), ("2", 2)]
    assert oth_stock.pieces(conn) == {"1": 4, "2": 2} and oth_storage.why_not_back(conn, cid) is None
    assert (orders.get_card(conn, x)["webmim_number"], orders.get_card(conn, x)["transfer_number"]) == (number(cid), TRANSFER)


def test_a_failed_give_back_of_a_record_changes_nothing(tmp_path, monkeypatch):
    conn = db(tmp_path)
    cid, x, b = two_positions(conn)
    first = sorted(oth_sale.taken_ids(conn, b))[0]
    real = oth_sale._untake_order
    calls = []

    def broken(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise oth_sale.OthSaleError("сломалось")
        return real(*args, **kwargs)
    monkeypatch.setattr(oth_sale, "_untake_order", broken)
    with pytest.raises(oth_sale.OthSaleError):
        oth_sale.untake(conn, b, first, LATE, "o")
    assert len(oth_sale.taken_ids(conn, b)) == 2 and code(conn, x) == "cancel" and oth_stock.pieces(conn) == {}


# ---- В2/В3: no «Выдали» for a record; «Продать» closes it ---------------------------------------------------------------------------

def on_the_return(conn):
    started(conn)
    cid, x = split(conn)
    asked_by_hand(conn, x)                                                                    # the question of a move by hand (E1: not the 7th day)
    return cid, x


def test_the_seventh_day_of_a_record_asks_if_the_tyres_are_there_never_if_they_were_given_out(tmp_path):
    conn = db(tmp_path)
    cid, x = on_the_return(conn)
    card = orders.get_card(conn, x)
    assert order_oth.question_open(conn, card) and "выдали" not in order_oth.question(conn, card, LATE).lower()
    outcome, answer, done = order_oth.answer_issued(conn, card, "менеджер Бета", LATE)
    assert not done and "«Продать»" in answer and code(conn, x) == orders.RETURN_GIVEN
    assert order_oth.question_open(conn, orders.get_card(conn, x))                              # the question still waits for «На точке»


def test_the_vk_question_of_the_seventh_day_of_a_record_never_asks_issued(tmp_path):
    conn = db(tmp_path)
    cid, x = on_the_return(conn)
    told = []
    order_oth.finish_moved(conn, "2026-10-26T13:00:00", lambda card, text: told.append(text) or "ok")
    [vk] = [t for t in told if "на точке %s" % number(cid) in t]
    assert "выдал" not in vk.lower() and "«Продать»" in vk


def test_the_vk_word_issued_for_a_record_is_refused_with_the_hint(tmp_path):
    conn = db(tmp_path)
    cid, x = on_the_return(conn)
    answer = mgr.handle_command(conn, "vk-1", POINT_VK, "выдал %s" % number(cid), None, LATE)
    assert "«Продать»" in answer and code(conn, x) == orders.RETURN_GIVEN


def test_the_vk_word_issued_for_a_record_on_oth_is_refused_with_the_hint(tmp_path):
    conn = db(tmp_path)
    cid, x = split(conn)                                                                         # on «На ОТХ», no question asked
    answer = mgr.handle_command(conn, "vk-2", POINT_VK, "выдал %s" % number(cid), None, LATE)
    assert "«Продать»" in answer and code(conn, x) == "oth"


def test_the_cabinet_of_the_point_has_no_issued_for_a_record(tmp_path):
    app, owner, tok, conn = panel(tmp_path)
    started(conn)
    cid, x = split(conn)
    asked_by_hand(conn, x)
    mine = manager(app, conn, orders.get_card(conn, x)["point_id"])
    page = mine.get("/my-point?tab=agree").get_data(as_text=True)                         # not agreed yet: «Согласование возврата» (2026-11-12)
    assert 'action="/my-point/%d/oth_here"' % x in page and "oth_sold" not in page        # «Продать»: on «На ОТХ» only (the owner 2026-11-12)
    assert 'action="/my-point/%d/oth_issued"' % x not in page


def sell(conn, x):
    before = orders.get_card(conn, x)
    outcome, answer, done = order_oth.sell(conn, before, "менеджер Бета", LATE)
    point_log.remember(conn, before, "oth_sold", before["point_id"], "менеджер Бета", LATE)
    return outcome, answer, done


def test_sold_closes_the_record_in_done_without_a_client_and_the_undo_puts_it_back_without_a_split(tmp_path):
    conn = db(tmp_path)
    cid, x = split(conn)
    outcome, answer, done = sell(conn, x)
    assert done, answer
    rec = orders.get_card(conn, x)
    assert rec["stage"]["code"] == "done" and rec["client_name"] == "" and oth_stock.pieces(conn) == {}
    assert "уже на этапе" in oth_storage.why_not_back(conn, cid)                                  # the same client is too late
    point = orders.get_card(conn, x)["point"]
    point_log.undo(conn, orders.get_card(conn, x), point, "менеджер Бета", "2026-10-22T11:00:00")
    rec = orders.get_card(conn, x)
    assert rec["stage"]["code"] == "oth" and records(conn) == {x} and oth_stock.pieces(conn) == {"1": 4}     # the same record, no new one
    assert oth_storage.why_not_back(conn, cid) is None


def test_given_back_of_a_record_refuses_the_sale(tmp_path):
    conn = db(tmp_path)
    cid, x = split(conn)
    item = orders.get_card(conn, x)["items"][0]["id"]
    order_oth.mark_returned(conn, item, LATER, "o", qty=2)
    conn.commit()
    assert order_oth.given_back(conn, x)
    outcome, answer, done = order_oth.sell(conn, orders.get_card(conn, x), "менеджер Бета", LATE)
    assert not done and code(conn, x) == "oth"


def test_done_still_needs_the_client_for_any_order_but_a_record(tmp_path):
    conn = db(tmp_path)
    cid, x = split(conn)
    done = next(s for s in orders.list_stages(conn) if s["code"] == "done")
    plain = dict(orders.get_card(conn, x), oth_record=False)
    assert {"телефон клиента", "имя клиента"} <= set(orders.missing_for(plain, done))
    assert orders.missing_for(orders.get_card(conn, x), done) == []


# ---- В7: the point is told nothing in VK of a move to «На ОТХ» --------------------------------------------------------------------------

def test_the_timer_of_the_reservation_tells_the_point_nothing_and_the_responsible_to_call(tmp_path):
    from test_reserve_noon import NEXT, told
    conn, cid, _chat, _deliver, _vk = told(tmp_path)
    got = []
    assert reserve._no_answer_step(conn, orders.get_card(conn, cid), NEXT + "T10:30:00", lambda card, text: got.append((card.get("responsible"), text)))
    reserve._release_held(conn, NEXT + "T11:05:00", lambda card, text: got.append((card.get("responsible"), text)))
    assert [who for who, _t in got if not who] == []                                              # "": the point
    assert any(t.startswith("Позвонить клиенту") for who, t in got if who)


# ---- М2, М3, М4, М5 ----------------------------------------------------------------------------------------------------------------

def test_the_flag_of_another_city_and_the_numbers_by_position_go_to_the_record(tmp_path):
    conn = db(tmp_path)
    cid = at_point(conn)
    item = orders.get_card(conn, cid)["items"][0]["id"]
    conn.execute("INSERT INTO order_intercity (order_id, enabled, city, carrier, updated_at) VALUES (?, 1, 'Город', 'ТК', ?)", (cid, ARRIVED))
    conn.execute("INSERT INTO order_item_mim (item_id, order_id, webmim_number, transfer_number, at, author) VALUES (?, ?, '777', '26R-7', ?, 'o')",
                 (item, cid, ARRIVED))
    conn.commit()
    orders.move_card(conn, cid, stage(conn, "oth"), MOVED, "o", changes={"cancel_reason": "отказ"})
    x = record(conn, cid)
    new_item = orders.get_card(conn, x)["items"][0]["id"]
    assert conn.execute("SELECT enabled, city, carrier FROM order_intercity WHERE order_id = ?", (x,)).fetchone() == (1, "Город", "ТК")
    assert conn.execute("SELECT order_id, webmim_number, transfer_number FROM order_item_mim WHERE item_id = ?", (new_item,)).fetchone() == (x, "777", "26R-7")
    assert conn.execute("SELECT 1 FROM order_item_mim WHERE item_id = ?", (item,)).fetchone() is None
    assert orders.move_card(conn, cid, stage(conn, "at_point"), LATER, "o")                     # back to the client: the numbers too
    assert conn.execute("SELECT order_id, webmim_number FROM order_item_mim WHERE item_id = ?", (item,)).fetchone() == (cid, "777")


def test_any_error_of_the_split_rolls_everything_back(tmp_path, monkeypatch):
    from tires_sync import order_sheet
    conn = db(tmp_path)
    cid = at_point(conn)
    monkeypatch.setattr(order_sheet, "enroll", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("таблица")))
    with pytest.raises(RuntimeError):
        orders.move_card(conn, cid, stage(conn, "oth"), MOVED, "o", changes={"cancel_reason": "отказ"})
    conn.commit()                                                                                 # a caller's later commit saves nothing of it
    a = orders.get_card(conn, cid)
    assert a["stage"]["code"] == "at_point" and a["webmim_number"] == number(cid) and records(conn) == set()
    assert conn.execute("SELECT COUNT(*) FROM order_card").fetchone()[0] == 1


def test_the_take_of_a_record_never_commits_the_callers_transaction(tmp_path):
    conn = db(tmp_path)
    cid, x = split(conn)
    b = new_order(conn, qty=4)
    conn.execute("UPDATE order_card SET comment = 'не сохранять' WHERE id = ?", (b,))
    with pytest.raises(RuntimeError, match="транзакц"):
        take(conn, b, now=LATER)
    conn.rollback()
    assert orders.get_card(conn, b)["comment"] != "не сохранять" and code(conn, x) == "oth"


def test_the_order_is_not_given_back_while_the_phone_has_another_open_order(tmp_path):
    conn = db(tmp_path)
    cid, x = split(conn)
    a = orders.get_card(conn, cid)
    other = orders.create_card(conn, dict(client_name="Иван", client_phone=a["client_phone"], point_id=a["point_id"]), LATER, "o")
    with pytest.raises(orders.OrderError, match="открытый заказ №%d" % other):
        orders.move_card(conn, cid, stage(conn, "at_point"), LATE, "o")
    assert code(conn, cid) == "cancel" and code(conn, x) == "oth"


# ---- the guard: an order already on «На ОТХ» the old way goes as before ------------------------------------------------------------------

def test_an_old_order_on_oth_is_sold_undone_issued_and_put_back_as_before(tmp_path, monkeypatch):
    conn = db(tmp_path)
    monkeypatch.setattr(oth_storage, "SPLIT", False)
    old = at_point(conn)
    orders.move_card(conn, old, stage(conn, "oth"), MOVED, "o", changes={"cancel_reason": "Клиент не пришёл"})
    monkeypatch.setattr(oth_storage, "SPLIT", True)
    assert code(conn, old) == "oth" and not oth_storage.is_record(conn, old)
    outcome, answer, done = sell(conn, old)
    assert done and code(conn, old) == "done" and records(conn) == set()
    point = orders.get_card(conn, old)["point"]
    point_log.undo(conn, orders.get_card(conn, old), point, "менеджер Бета", "2026-10-22T11:00:00")
    assert code(conn, old) == "oth" and records(conn) == set() and oth_stock.pieces(conn) == {"1": 4}
    done_stage = next(s for s in orders.list_stages(conn) if s["code"] == "done")
    assert "телефон клиента" in orders.missing_for(dict(orders.get_card(conn, old), client_phone=""), done_stage)
    b = new_order(conn, qty=2)
    assert take(conn, b, now=LATE) == 2
    [item] = oth_sale.taken_ids(conn, b)
    assert oth_sale.untake(conn, b, item, LATE, "o") == 2 and oth_stock.pieces(conn) == {"1": 4} and code(conn, old) == "oth"
