"""Правки возврата с ОТХ и сбора за переданного клиента (слова владельца, ветка lane/oth-return-fixes), по пунктам задачи.

1 задача возврата закрыта «не возвращаем»: шины, снятые с продажи меткой «На возврат» (позиция) или «Согласуем» (заказ на «На возврат», возврат не
согласован), сами возвращаются в продажу, с записью в журнале; 2 «пока» и «ждём/ждем» в результате — не отказ: заказ ждёт согласования, 7-й день
переводит как обычно, согласование даёт «Согласовано на возврат»; 3 сбор точке за переданного клиента B идёт за клиентом и в объединённый заказ,
а вторая смена клиента у A не забывает первую B (сбор не платится дважды и не платится за ушедшего клиента)."""
from __future__ import annotations

from datetime import date

import pytest

from test_oth_client_swap import LATER, LATEST, T, _b, _sell, new_order, point_id, texts
from test_oth_flow2 import _two_positions
from test_oth_seventh_day import ENTERED, on_oth, started
from test_order_reserve import card_at, db, stage
from tires_sync import order_merge, order_oth, order_rules as rules, order_report, orders, oth_sale, oth_stock, oth_swap, tasks

TODAY = date(2026, 10, 21)
NOW = "2026-10-21T11:00:00"


def events(conn, cid):
    return [(e["level"], e["text"]) for e in orders.list_events(conn, cid)]


def task_of(conn, cid):
    [row] = conn.execute("SELECT id FROM callback_task WHERE order_id = ? AND done_at IS NULL AND cancelled_at IS NULL", (cid,)).fetchall()
    return row[0]


def marked_position(tmp_path, qty="2"):
    """An order on «На ОТХ» (its seventh day 2026-10-26) with `qty` pieces of its first position marked «На возврат» (off sale, the task open)."""
    conn, cid, card = _two_positions(tmp_path)
    started(conn)
    conn.execute("UPDATE order_card SET entered_at = ? WHERE id = ?", (ENTERED, cid))
    conn.commit()
    item = card["items"][0]["id"]
    order_oth.item_to_return(conn, item, "2026-10-21T10:00:00", "o", qty)
    return conn, cid, item


def whole_returning(tmp_path):
    """An order moved to «На возврат» by hand (the label «Ожидает согласование возврата»: «Согласуем»), its task open."""
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    before = on_oth(conn, cid)
    orders.move_card(conn, cid, stage(conn, orders.RETURN_GIVEN), "2026-10-21T10:00:00", "o")
    order_oth.moved_by_hand(conn, before, "2026-10-21T10:00:00", "o")
    return conn, cid


def close(conn, cid, result, at="2026-10-22T10:00:00"):
    tasks.close(conn, task_of(conn, cid), "done", at, "o", result)


# ---- 1. «не возвращаем»: шины снова в продаже --------------------------------------------------------------------------------------------

def test_1_a_position_marked_for_the_return_is_on_sale_again(tmp_path):
    conn, cid, item = marked_position(tmp_path)
    assert oth_stock.pieces(conn, manual=False) == {"1": 2, "2": 2}                 # off sale while the return is agreed
    close(conn, cid, "Не возвращаем, продаём")
    assert oth_stock.pieces(conn, manual=False) == {"1": 4, "2": 2}                 # back on sale by itself
    assert order_oth.goods_on_oth(conn) == {"1": 4, "2": 2}
    assert not conn.execute("SELECT 1 FROM oth_return WHERE order_id = ?", (cid,)).fetchone()
    card = orders.get_card(conn, cid)
    assert card["stage"]["code"] == "oth" and order_oth.item_mark(order_oth.item_marks(conn), item, card["entered_at"]) is None
    assert any(t.startswith("Шины снова в продаже") and "2 шт." in t for _l, t in events(conn, cid))
    assert order_oth.ask_seventh_day(conn, "2026-10-26T12:30:00") == 0            # «не возвращаем»: no move on the 7th day, as before


def test_1_a_real_return_to_the_supplier_stays_returned(tmp_path):
    conn, cid, item = marked_position(tmp_path, qty="1")
    other = orders.get_card(conn, cid)["items"][1]["id"]
    order_oth.mark_returned(conn, other, "2026-10-21T12:00:00", "o", "1")          # really given back: not a mark of «На возврат»
    close(conn, cid, "не возвращаем")
    assert oth_stock.pieces(conn, manual=False) == {"1": 4, "2": 1}
    assert conn.execute("SELECT item_id, qty FROM oth_return WHERE order_id = ?", (cid,)).fetchall() == [(other, 1)]


def test_1_a_mark_and_a_real_return_of_one_position_only_the_mark_is_on_sale(tmp_path):
    conn, cid, item = marked_position(tmp_path, qty="1")
    order_oth.mark_returned(conn, item, "2026-10-21T12:00:00", "o", "1")            # the same position, one row of oth_return: 2
    assert conn.execute("SELECT qty FROM oth_return WHERE item_id = ?", (item,)).fetchone()[0] == 2
    close(conn, cid, "не возвращаем")
    assert conn.execute("SELECT qty FROM oth_return WHERE item_id = ?", (item,)).fetchone()[0] == 1     # the real return stays
    assert oth_stock.pieces(conn, manual=False) == {"1": 3, "2": 2}


def test_1_the_whole_order_on_return_is_on_sale_again_on_oth(tmp_path):
    conn, cid = whole_returning(tmp_path)
    assert "1" not in order_oth.goods_on_oth(conn)                                  # «Согласуем»: not on sale
    close(conn, cid, "Решили: не возвращаем")
    card = orders.get_card(conn, cid)
    assert card["stage"]["code"] == "oth" and card["entered_at"] == "2026-10-22T10:00:00"
    assert order_oth.goods_on_oth(conn) == {"1": 4}
    assert any(t.startswith("Шины снова в продаже") for _l, t in events(conn, cid))
    assert not conn.execute("SELECT 1 FROM callback_task WHERE order_id = ? AND done_at IS NULL AND cancelled_at IS NULL", (cid,)).fetchone()
    assert order_oth.ask_seventh_day(conn, "2026-10-28T12:30:00") == 0             # the new stay: the 7th day never sends it back again
    assert orders.get_card(conn, cid)["stage"]["code"] == "oth"


def test_1_back_on_oth_fires_no_rule_of_the_stage(tmp_path):
    conn, cid = whole_returning(tmp_path)
    rules.add_rule(conn, stage(conn, "oth"), "enter", "0", "move", None, str(stage(conn, "cancel")), "auto", None)
    close(conn, cid, "не возвращаем")
    assert orders.get_card(conn, cid)["stage"]["code"] == "oth"                     # a rule (a message to the client too) would fire here


def test_1_an_order_with_tyres_given_to_the_supplier_stays_and_says_so(tmp_path):
    conn, cid = whole_returning(tmp_path)
    item = orders.get_card(conn, cid)["items"][0]["id"]
    order_oth.mark_returned(conn, item, "2026-10-21T12:00:00", "o", "1")
    close(conn, cid, "не возвращаем")
    assert orders.get_card(conn, cid)["stage"]["code"] == orders.RETURN_GIVEN       # moving it would forget the piece given back
    assert conn.execute("SELECT qty FROM oth_return WHERE order_id = ?", (cid,)).fetchone()[0] == 1
    assert any(lvl == "yellow" and "вручную" in t for lvl, t in events(conn, cid))


def test_1_an_agreed_return_is_never_undone(tmp_path):
    conn, cid = whole_returning(tmp_path)
    order_oth.agree(conn, orders.get_card(conn, cid), TODAY, NOW, "o")              # closes the task itself
    task = conn.execute("SELECT id FROM callback_task WHERE order_id = ? ORDER BY id DESC LIMIT 1", (cid,)).fetchone()[0]
    order_oth.task_closed(conn, tasks.get(conn, task), "done", "не возвращаем", "o", "2026-10-22T10:00:00")
    assert orders.get_card(conn, cid)["stage"]["code"] == orders.RETURN_GIVEN and order_oth.return_plan(conn, cid)


def test_1_another_result_changes_nothing(tmp_path):
    conn, cid, item = marked_position(tmp_path)
    close(conn, cid, "договорились")
    assert oth_stock.pieces(conn, manual=False) == {"1": 2, "2": 2}
    (tmp_path / "w").mkdir()
    conn2, cid2 = whole_returning(tmp_path / "w")
    close(conn2, cid2, "договорились")
    assert orders.get_card(conn2, cid2)["stage"]["code"] == orders.RETURN_GIVEN


# ---- 2. «пока», «ждём» — не отказ ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("result", ["Пока не возвращаем, ждём ответа", "пока не возвращаем", "не возвращаем, ждем поставщика", "Ждём: не будем возвращать"])
def test_2_waiting_is_not_a_refusal_the_position_still_waits(tmp_path, result):
    conn, cid, item = marked_position(tmp_path)
    close(conn, cid, result)
    card = orders.get_card(conn, cid)
    assert oth_stock.pieces(conn, manual=False) == {"1": 2, "2": 2}                 # still off sale
    assert not order_oth._kept(conn, card)
    mark = order_oth.item_mark(order_oth.item_marks(conn), item, card["entered_at"])
    assert mark and mark["qty"] == 2                                                # «Ожидает согласование возврата · 2 шт.» stays
    assert not any(t.startswith("Шины снова в продаже") for _l, t in events(conn, cid))


def test_2_the_seventh_day_still_moves_and_the_agreement_is_as_usual(tmp_path):
    conn, cid, item = marked_position(tmp_path)
    close(conn, cid, "Пока не возвращаем, ждём ответа")
    assert order_oth.ask_seventh_day(conn, "2026-10-26T12:30:00") == 1
    card = orders.get_card(conn, cid)
    assert card["stage"]["code"] == orders.RETURN_GIVEN and order_oth.label_of(order_oth.return_plan(conn, cid)) == "Ожидает согласование возврата"
    order_oth.agree(conn, card, TODAY, "2026-10-26T13:00:00", "o")
    assert order_oth.point_label_of(order_oth.return_plan(conn, cid)).startswith("Согласовано на возврат")


def test_2_waiting_agreed_on_oth_sends_the_marked_pieces(tmp_path):
    conn, cid, item = marked_position(tmp_path)
    close(conn, cid, "ждём ответа поставщика")
    order_oth.agree(conn, orders.get_card(conn, cid), TODAY, NOW, "o")              # «Согласовано» по позиции: the marked pieces go
    [new] = order_oth.split_of(conn, cid)
    assert [int(i["qty"]) for i in orders.get_card(conn, new)["items"]] == [2]
    assert order_oth.point_label_of(order_oth.return_plan(conn, new)).startswith("Согласовано на возврат")
    assert oth_stock.pieces(conn, manual=False) == {"1": 2, "2": 2}                 # what stays on «На ОТХ» is on sale


def test_2_the_whole_order_waits_on_return(tmp_path):
    conn, cid = whole_returning(tmp_path)
    close(conn, cid, "пока не возвращаем, ждём")
    card = orders.get_card(conn, cid)
    assert card["stage"]["code"] == orders.RETURN_GIVEN and order_oth.label_of(order_oth.return_plan(conn, cid)) == "Ожидает согласование возврата"
    order_oth.agree(conn, card, TODAY, NOW, "o")
    assert order_oth.point_label_of(order_oth.return_plan(conn, cid)).startswith("Согласовано на возврат")


# ---- 3. сбор за переданного клиента ------------------------------------------------------------------------------------------------------

def hand(conn, order_id, mid):
    conn.execute("INSERT INTO order_handoff (order_id, message_id, point_id, created_at) VALUES (?, ?, ?, ?)", (order_id, mid, point_id(conn), T))
    conn.commit()


def take(conn, b, a, at):
    return oth_swap.take_client(conn, b, orders.get_card(conn, b)["items"][0]["id"], orders.get_card(conn, a)["items"][0]["id"], at, "o")


def b_at_a_price(conn):
    b = new_order(conn)
    conn.execute("UPDATE order_item SET sale_unit = 5000 WHERE order_id = ?", (b,))
    conn.commit()
    return b


def clients_paid(conn):
    return order_report.build(conn, "2026-10-01", "2026-10-31")["handoff"]["clients"]


def test_3_the_fee_of_b_goes_on_into_the_merged_order(tmp_path):
    conn = db(tmp_path)
    a = card_at(conn, "oth", T)
    b, _ = _b(conn)
    hand(conn, b, "hb")
    take(conn, b, a, LATER)
    c = card_at(conn, "at_point", T)
    n = order_merge.merge(conn, [a, c], stage(conn, "at_point"), LATEST, "o", client_from=a)
    assert oth_swap.handed_fee(conn, n) and not oth_swap.handed_fee(conn, b)
    assert order_merge.handed(conn) == {n: b}
    _sell(conn, n)
    assert clients_paid(conn) == 1


def test_3_the_merged_order_pays_the_fee_once(tmp_path):
    conn = db(tmp_path)
    a = card_at(conn, "oth", T)
    b, _ = _b(conn)
    hand(conn, b, "hb")
    take(conn, b, a, LATER)
    c = card_at(conn, "at_point", T)
    hand(conn, c, "hc")                                                             # two handed sources: once, as any merge (2026-10-30)
    n = order_merge.merge(conn, [a, c], stage(conn, "at_point"), LATEST, "o", client_from=a)
    _sell(conn, n)
    assert clients_paid(conn) == 1


@pytest.mark.parametrize("second", ["b2", "typed"])
def test_3_a_second_swap_never_forgets_the_first_b(tmp_path, second):
    conn = db(tmp_path)
    a = card_at(conn, "oth", T)
    b1, _ = _b(conn)
    hand(conn, b1, "h1")
    take(conn, b1, a, LATER)
    orders.move_card(conn, a, stage(conn, "oth"), LATER, "o", changes={"cancel_reason": "Отказ"})     # the client of B1 refused A
    if second == "typed":
        oth_sale.to_new_client(conn, a, "Олег", "+7 900 444 55 66", LATEST, "o")
    else:
        take(conn, b_at_a_price(conn), a, LATEST)
    assert not oth_swap.handed_fee(conn, a)                                         # the client of B1 left: no fee for him
    assert not oth_swap.handed_fee(conn, b1)                                        # B1 is still the one whose client went on
    off, moved = oth_swap.fee_handed(conn)
    assert b1 in off and a not in moved


def test_3_a_second_swap_with_a_handed_b2_pays_b2_once(tmp_path):
    conn = db(tmp_path)
    a = card_at(conn, "oth", T)
    b1, _ = _b(conn)
    hand(conn, b1, "h1")
    take(conn, b1, a, LATER)
    orders.move_card(conn, a, stage(conn, "oth"), LATER, "o", changes={"cancel_reason": "Отказ"})
    b2 = b_at_a_price(conn)
    hand(conn, b2, "h2")
    take(conn, b2, a, LATEST)
    assert oth_swap.fee_handed(conn)[1] == {a: b2}
    assert oth_swap.handed_fee(conn, a) and not oth_swap.handed_fee(conn, b1) and not oth_swap.handed_fee(conn, b2)
    _sell(conn, a)
    assert clients_paid(conn) == 1


def test_3_without_a_merge_or_a_second_swap_as_before(tmp_path):
    conn = db(tmp_path)
    a = card_at(conn, "oth", T)
    b, _ = _b(conn)
    hand(conn, b, "hb")
    take(conn, b, a, LATER)
    assert oth_swap.fee_handed(conn) == ({b}, {a: b})
    _sell(conn, a)
    assert clients_paid(conn) == 1
    assert any(t == oth_swap.FEE_FROM % b for t in texts(conn, a))
