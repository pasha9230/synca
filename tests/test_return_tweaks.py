"""Правки возврата с ОТХ (решения владельца, ветка lane/return-tweaks): по одному тесту на пункт задачи.

1 точка отдала шины водителю: сразу «Вернули поставщику», офису ничего; 2 «На возврат» нельзя выбрать в правилах этапа; 3 «Вернуть поставщику»
и отметка точки закрывают задачу согласования; 4 вопрос точке и при ручном «На возврат»; 5 «Ожидает согласование возврата» везде; 6 партнёру
«На возврат» на странице ОТХ; 7 «Согласовано» по позиции выделяет её в свой заказ; 8 поставщик принимает не все шины: остаток и пометка;
9 задача закрыта «не возвращаем»: 7-й день не переводит; 10 задачу на возврат можно продлить на 2 рабочих дня; 11 сумма возврата только по
закупке у поставщика; 12 разбор «отказ» точки."""
from __future__ import annotations

from datetime import date

import pytest

from panel_helpers import agree_return, post
from test_mypoint import manager, press, workplace
from test_oth_flow2 import _on_oth_page, _two_positions, _vk_order, _outcomes, open_tasks
from test_oth_seventh_day import ENTERED, on_oth, started
from test_order_reserve import card_at, db, stage
from test_point_refuse import at_point, linked
from test_vk_commands import PEER, _stage, hook
from tires_sync import message_texts, order_manager as mgr, order_oth, order_rules as rules, orders, oth_stock, store, tasks, users

TODAY = date(2026, 10, 21)                            # a Wednesday: a day of the supplier
NOW = "2026-10-21T11:00:00"


def returning(conn, cid, at="2026-10-21T10:00:00"):
    orders.move_card(conn, cid, stage(conn, orders.RETURN_GIVEN), at, "o")
    return orders.get_card(conn, cid)


def events(conn, cid):
    return [(e["level"], e["text"]) for e in orders.list_events(conn, cid)]


def point_tyre(conn, cid, qty=1):
    """A tyre of the point itself (no purchase from the supplier): «с точки · нет в выгрузке»."""
    conn.execute("INSERT INTO order_item (order_id, goods_id, name, qty, supplier_price, sale_unit) VALUES (?, ?, 'Шина точки', ?, 0, 3000)",
                 (cid, orders.POINT_GOODS, qty))
    conn.commit()


def as_role(app, conn, login_, role):
    users.add_user(conn, login_, "password-123", ENTERED, role=role)
    conn.commit()
    client = app.test_client()
    with client.session_transaction() as session:
        session["user"] = login_
        session["epoch"] = store.get_session_epoch(conn, login_)
        session["csrf"] = "tok"
    return client


# ---- 1, 11. точка отдала шины водителю; сумма возврата от поставщика ------------------------------------------------------------------

def test_1_the_act_signed_closes_the_return_at_once_and_tells_the_office_nothing(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    on_oth(conn, cid)
    point_tyre(conn, cid)                                                    # before: no refund could be counted, «Отдано, нужна сумма»
    card = returning(conn, cid)
    order_oth.agree(conn, card, TODAY, NOW, "o")
    assert order_oth.sign_act(conn, orders.get_card(conn, cid), "Бета", "2026-10-23T10:00:00", "точка") == "return"
    card = orders.get_card(conn, cid)
    assert card["stage"]["code"] == "return" and float(card["return_amount"]) == 16000.0     # 4 × 4000, the supplier's tyres only
    assert not [t for lvl, t in events(conn, cid) if lvl in ("yellow", "red")]


def test_1_the_vk_word_gave_of_an_agreed_return_is_the_hand_over_not_a_message_to_the_owner(tmp_path):
    app, conn, cid = _vk_order(tmp_path, orders.RETURN_GIVEN)
    order_oth.agree(conn, orders.get_card(conn, cid), TODAY, NOW, "owner")
    hook(app, conn, "отдал 700300", event="g1")
    assert _outcomes(conn, cid) == ["ждёт подтверждения: шины не отмечены отданными"]
    assert not mgr.list_inbox(conn)
    hook(app, conn, "№700300 возврат отдал подтверждаю", event="g2")
    assert orders.get_card(conn, cid)["stage"]["code"] == "return"


def test_11_the_refund_is_the_supplier_s_purchase_never_the_sale(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    point_tyre(conn, cid, 2)
    card = orders.get_card(conn, cid)
    assert order_oth.purchase_sum(card) == 16000.0                           # 4 × 4000 (sale 5000): the point's own tyre is no purchase
    conn.execute("UPDATE order_item SET supplier_price = 0 WHERE order_id = ? AND goods_id = '1'", (cid,))
    conn.commit()
    assert order_oth.purchase_sum(orders.get_card(conn, cid)) is None         # a supplier's tyre without a purchase: never made up


# ---- 2. правила этапа ------------------------------------------------------------------------------------------------------------------

def test_2_na_vozvrat_cannot_be_chosen_in_the_rules_of_a_stage(tmp_path):
    from test_vk_commands import ready
    app, client, tok, conn, pid, card = ready(tmp_path)
    returning_id = _stage(conn, orders.RETURN_GIVEN)
    with pytest.raises(orders.OrderError):
        rules.add_rule(conn, _stage(conn, "at_point"), "enter", "0", "move", None, str(returning_id), "auto", None)
    page = client.get("/orders/settings").get_data(as_text=True)
    select = page.split('name="arg_stage"', 1)[1].split("</select>", 1)[0]
    assert 'value="%d"' % returning_id not in select and 'value="%d"' % _stage(conn, "done") in select


# ---- 3. задача согласования закрывается сама ------------------------------------------------------------------------------------------

def test_3_return_to_the_supplier_closes_the_task_of_the_agreement(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    before = on_oth(conn, cid)
    card = returning(conn, cid)
    order_oth.moved_by_hand(conn, before, "2026-10-21T10:00:00", "o")
    assert [t["title"] for t in open_tasks(conn, cid)] == [order_oth.RETURN_TASK]
    order_oth.mark_returned(conn, card["items"][0]["id"], NOW, "o")
    assert open_tasks(conn, cid) == []
    [row] = conn.execute("SELECT done_at FROM callback_task WHERE order_id = ?", (cid,)).fetchall()
    assert row[0] == NOW


def test_3_a_part_returned_keeps_the_task(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    before = on_oth(conn, cid)
    card = returning(conn, cid)
    order_oth.moved_by_hand(conn, before, "2026-10-21T10:00:00", "o")
    order_oth.mark_returned(conn, card["items"][0]["id"], NOW, "o", "2")
    assert [t["title"] for t in open_tasks(conn, cid)] == [order_oth.RETURN_TASK]


def test_3_the_point_s_mark_closes_the_task_too(tmp_path):
    """Already so before this branch: the act signed moves the order to «Вернули поставщику», the tasks of the return close (left_return)."""
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    on_oth(conn, cid)
    card = returning(conn, cid)
    order_oth.agree(conn, card, TODAY, NOW, "o")
    order_oth._return_task(conn, orders.get_card(conn, cid), NOW, "o")       # a task left open (made again by hand)
    conn.commit()
    order_oth.sign_act(conn, orders.get_card(conn, cid), "Бета", "2026-10-23T10:00:00", "точка")
    assert open_tasks(conn, cid) == []


# ---- 4. вопрос точке при ручном «На возврат» -------------------------------------------------------------------------------------------

def test_4_a_move_by_hand_asks_the_point_too(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    before = on_oth(conn, cid)
    returning(conn, cid, "2026-10-21T12:40:00")
    order_oth.moved_by_hand(conn, before, "2026-10-21T12:40:00", "o")
    card = orders.get_card(conn, cid)
    assert order_oth.question_open(conn, card)
    assert order_oth.answer_here(conn, card, "точка", "2026-10-21T13:00:00")[2] is True
    told = []
    order_oth.finish_moved(conn, "2026-10-21T13:00:00", lambda c, text: told.append(text) or "ok")


def test_4_the_vk_point_hears_the_question_after_a_move_by_hand(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    before = on_oth(conn, cid)
    returning(conn, cid, "2026-10-21T12:40:00")
    order_oth.moved_by_hand(conn, before, "2026-10-21T12:40:00", "o")
    told = []
    order_oth.finish_moved(conn, "2026-10-21T13:00:00", lambda c, text: told.append(text) or "ok")
    ask = message_texts.text(conn, message_texts.RETURN_ASK, order_label=mgr.label(orders.get_card(conn, cid)))
    assert told and told[0].endswith(ask)


# ---- 5. одна подпись «Ожидает согласование возврата» ----------------------------------------------------------------------------------

def test_5_one_label_everywhere(tmp_path):
    assert order_oth.LABELS[order_oth.AGREE] == order_oth.POINT_LABELS[order_oth.AGREE] == "Ожидает согласование возврата"
    assert order_oth.FILTERS[order_oth.AGREE] == "Ожидает согласование возврата"
    app, client, tok, conn, cid = _on_oth_page(tmp_path)
    post(client, "/orders/%d/move" % cid, tok, stage_id=str(_stage(conn, orders.RETURN_GIVEN)))
    assert "data-return-label>Ожидает согласование возврата<" in client.get("/orders").get_data(as_text=True)
    card = client.get("/orders/%d" % cid).get_data(as_text=True)
    assert "Ожидает согласование возврата" in card and "Согласуем" not in card


# ---- 6. партнёр: «На возврат» на странице ОТХ ------------------------------------------------------------------------------------------

def test_6_the_partner_has_na_vozvrat_on_the_oth_page(tmp_path):
    app, client, tok, conn, cid = _on_oth_page(tmp_path)
    partner = as_role(app, conn, "zaman", "partner")
    assert 'formaction="/oth/to-return"' in partner.get("/oth").get_data(as_text=True)
    item = orders.get_card(conn, cid)["items"][0]["id"]
    post(partner, "/oth/to-return", "tok", item=str(item), qty="1")
    assert [r["qty"] for r in order_oth.stock(conn)["returned"] if r["item_id"] == item] == [1]
    viewer = as_role(app, conn, "viewer1", "viewer")
    assert 'formaction="/oth/to-return"' not in viewer.get("/oth").get_data(as_text=True)


# ---- 7. «Согласовано» по позиции -------------------------------------------------------------------------------------------------------

def test_7_agreeing_a_marked_position_splits_it_off_and_the_rest_stays_on_sale(tmp_path):
    conn, cid, card = _two_positions(tmp_path)
    first, second = card["items"][0]["id"], card["items"][1]["id"]
    order_oth.item_to_return(conn, first, "2026-10-21T10:00:00", "o")         # the whole first position: 4 pcs
    order_oth.agree(conn, orders.get_card(conn, cid), TODAY, NOW, "o")
    old = orders.get_card(conn, cid)
    assert old["stage"]["code"] == "oth" and order_oth.return_plan(conn, cid) is None
    assert {i["id"]: int(i["qty"]) for i in old["items"]} == {first: 0, second: 2}
    assert oth_stock.pieces(conn, manual=False) == {"2": 2}                    # the second is on sale as before
    new = order_oth.split_of(conn, cid)
    [moved] = [n for n in new]
    got = orders.get_card(conn, moved)
    assert got["stage"]["code"] == orders.RETURN_GIVEN and got["point_id"] == old["point_id"]
    assert [(i["goods_id"], int(i["qty"]), float(i["supplier_price"])) for i in got["items"]] == [("1", 4, 4000.0)]
    assert order_oth.return_plan(conn, moved)["return_date"] == "2026-10-21"
    assert got["webmim_number"] == old["webmim_number"]                        # the numbers of the purchase
    assert open_tasks(conn, cid) == []                                         # agreed: the task of the position is done
    assert any("Выделен" in t for _l, t in events(conn, moved)) and any("выделен" in t for _l, t in events(conn, cid))


def test_7_one_position_or_nothing_marked_moves_the_whole_order_as_before(tmp_path):
    conn, cid, card = _two_positions(tmp_path)
    order_oth.agree(conn, orders.get_card(conn, cid), TODAY, NOW, "o")
    assert orders.get_card(conn, cid)["stage"]["code"] == orders.RETURN_GIVEN and order_oth.split_of(conn, cid) == []


# ---- 8. поставщик принимает не все шины -----------------------------------------------------------------------------------------------

def test_8_fewer_tyres_go_back_the_rest_is_our_stock_with_a_note(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    on_oth(conn, cid)
    card = returning(conn, cid)
    item = card["items"][0]["id"]
    order_oth.agree(conn, card, TODAY, NOW, "o", qty={item: 3})
    got = orders.get_card(conn, cid)
    assert got["stage"]["code"] == orders.RETURN_GIVEN and [int(i["qty"]) for i in got["items"]] == [3]
    assert any("едет меньше шин, чем в закупке" in t and "3 из 4" in t for _l, t in events(conn, cid))
    [rest] = order_oth.split_of(conn, cid)
    left = orders.get_card(conn, rest)
    assert left["stage"]["code"] == "oth" and [int(i["qty"]) for i in left["items"]] == [1]
    assert oth_stock.pieces(conn, manual=False) == {"1": 1}                    # the remainder is on sale again
    assert order_oth.purchase_sum(got) == 12000.0                              # the refund of the 3 that go back


def test_8_the_card_offers_the_pieces_and_refuses_more_than_there_are(tmp_path):
    app, client, tok, conn, cid = _on_oth_page(tmp_path)
    item = orders.get_card(conn, cid)["items"][0]["id"]
    page = client.get("/orders/%d" % cid).get_data(as_text=True)
    assert 'name="return_qty_%d"' % item in page
    with pytest.raises(order_oth.OthError):
        order_oth.agree(conn, orders.get_card(conn, cid), TODAY, NOW, "o", qty={item: 99})
    found = __import__("re").search(r'name="return_date" value="([0-9-]+)"', page)
    post(client, "/orders/%d/oth-agreed" % cid, tok, return_date=found.group(1), **{"return_qty_%d" % item: "3"})
    old = orders.get_card(conn, cid)                                           # on «На ОТХ»: the 3 going back leave, the rest stays on sale
    assert old["stage"]["code"] == "oth" and [int(i["qty"]) for i in old["items"]] == [1]
    [new] = order_oth.split_of(conn, cid)
    got = orders.get_card(conn, new)
    assert got["stage"]["code"] == orders.RETURN_GIVEN and [int(i["qty"]) for i in got["items"]] == [3]
    assert any("едет меньше шин, чем в закупке: 3 из 4" in t for _l, t in events(conn, new))


# ---- 9. «не возвращаем» ----------------------------------------------------------------------------------------------------------------

def test_9_a_task_closed_with_no_return_keeps_the_order_on_oth_on_the_seventh_day(tmp_path):
    conn, cid, card = _two_positions(tmp_path)
    started(conn)
    conn.execute("UPDATE order_card SET entered_at = ? WHERE id = ?", (ENTERED, cid))
    conn.commit()
    order_oth.item_to_return(conn, card["items"][0]["id"], "2026-10-21T10:00:00", "o", "1")
    [task] = conn.execute("SELECT id FROM callback_task WHERE order_id = ?", (cid,)).fetchall()
    tasks.close(conn, task[0], "done", "2026-10-22T10:00:00", "o", "Не возвращаем, продаём")
    assert order_oth.ask_seventh_day(conn, "2026-10-26T12:30:00") == 0
    assert orders.get_card(conn, cid)["stage"]["code"] == "oth"
    assert any("не возвращаем" in t for _l, t in events(conn, cid))


def test_9_another_result_still_moves_on_the_seventh_day(tmp_path):
    conn, cid, card = _two_positions(tmp_path)
    started(conn)
    conn.execute("UPDATE order_card SET entered_at = ? WHERE id = ?", (ENTERED, cid))
    conn.commit()
    order_oth.item_to_return(conn, card["items"][0]["id"], "2026-10-21T10:00:00", "o", "1")
    [task] = conn.execute("SELECT id FROM callback_task WHERE order_id = ?", (cid,)).fetchall()
    tasks.close(conn, task[0], "done", "2026-10-22T10:00:00", "o", "договорились")
    assert order_oth.ask_seventh_day(conn, "2026-10-26T12:30:00") == 1


# ---- 10. продлить задачу на возврат ----------------------------------------------------------------------------------------------------

def test_10_the_return_task_is_extended_by_two_working_days_while_the_tyres_are_not_given(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    before = on_oth(conn, cid)
    returning(conn, cid, "2026-10-23T10:00:00")                               # a Friday
    order_oth.moved_by_hand(conn, before, "2026-10-23T10:00:00", "o")
    [task] = conn.execute("SELECT id, due_at FROM callback_task WHERE order_id = ?", (cid,)).fetchall()
    assert task[1] == "2026-10-27T10:00"                                      # Tuesday
    assert order_oth.extend_task(conn, task[0], "2026-10-27T09:00:00", "o") == "2026-10-29T10:00"     # +2 working days
    assert any("продлена" in t for _l, t in events(conn, cid))


def test_10_no_extension_once_the_tyres_are_given(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    on_oth(conn, cid)
    card = returning(conn, cid)
    order_oth.agree(conn, card, TODAY, NOW, "o")
    task = order_oth._return_task(conn, orders.get_card(conn, cid), NOW, "o")
    conn.commit()
    order_oth.sign_act(conn, orders.get_card(conn, cid), "Бета", "2026-10-23T10:00:00", "точка")
    with pytest.raises((order_oth.OthError, tasks.TaskError)):
        order_oth.extend_task(conn, task, "2026-10-23T11:00:00", "o")


def test_10_the_tasks_page_has_the_button(tmp_path):
    app, client, tok, conn, cid = _on_oth_page(tmp_path)
    post(client, "/orders/%d/move" % cid, tok, stage_id=str(_stage(conn, orders.RETURN_GIVEN)))
    [task] = conn.execute("SELECT id, due_at FROM callback_task WHERE order_id = ?", (cid,)).fetchall()
    assert 'action="/tasks/%d/extend"' % task[0] in client.get("/tasks?tab=all").get_data(as_text=True)
    post(client, "/tasks/%d/extend" % task[0], tok)
    assert conn.execute("SELECT due_at FROM callback_task WHERE id = ?", (task[0],)).fetchone()[0] > task[1]


# ---- 12. «отказ» точки -----------------------------------------------------------------------------------------------------------------

def _say(tmp_path, text):
    app, _client, conn, _pid, card = at_point(tmp_path)
    answer = mgr.dispatch(conn, "r1", PEER, text, None, "2026-10-13T13:00:00", None, None)
    return conn, card, answer


def test_12_took_two_goes_to_the_inbox(tmp_path):
    conn, card, answer = _say(tmp_path, "отказ 5000100 взял 2")
    assert orders.get_card(conn, card)["stage"]["code"] == "at_point" and linked(conn, card)
    assert mgr.list_inbox(conn) and mgr.list_inbox(conn)[0]["dispute"]


def test_12_not_the_size_is_an_ordinary_reason(tmp_path):
    conn, card, answer = _say(tmp_path, "отказ 5000100 не тот размер")
    got = orders.get_card(conn, card)
    assert got["stage"]["code"] == "oth" and got["cancel_reason"] == mgr.REFUSE_REASON % "не тот размер"
    assert mgr.refuse_doubt("отказ 5000100 не нужен", "не нужен") == "отрицание"     # a real negation still waits


def test_12_the_old_client_did_not_come_hints_the_refusal(tmp_path):
    conn, card, answer = _say(tmp_path, "клиент не пришёл 5000100")
    assert "отказ 5000100" in answer


def test_12_a_number_before_the_word_goes_to_the_inbox(tmp_path):
    """Already so before this branch: the message is not a command, the owner reads it in «Входящие»."""
    conn, card, answer = _say(tmp_path, "5000100 отказ старый год производства")
    assert orders.get_card(conn, card)["stage"]["code"] == "at_point" and linked(conn, card)
    assert [m["text"] for m in mgr.list_inbox(conn)] == ["5000100 отказ старый год производства"]


# ---- review of 2026-11-07: a real «Вернуть поставщику» is not a mark; a second press splits nothing more ----------------------------------

def test_7_a_real_return_stays_and_only_the_pieces_at_the_point_can_go(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    card = on_oth(conn, cid)
    item = card["items"][0]["id"]
    order_oth.mark_returned(conn, item, NOW, "o", "2")                         # 2 of 4 really given back
    card = orders.get_card(conn, cid)
    assert order_oth.pieces_here(conn, card) == {item: 2}
    with pytest.raises(order_oth.OthError):
        order_oth.agree(conn, card, TODAY, NOW, "o", qty={item: 3})
    order_oth.agree(conn, card, TODAY, NOW, "o", qty={item: 1})
    assert conn.execute("SELECT qty FROM oth_return WHERE item_id = ?", (item,)).fetchone() == (2,)     # the real return is kept
    assert oth_stock.pieces(conn, manual=False) == {"1": 1}                    # 4 − 2 returned − 1 going back


def test_7_fewer_than_marked_puts_the_rest_of_the_mark_on_sale_again(tmp_path):
    conn, cid, card = _two_positions(tmp_path)
    first = card["items"][0]["id"]
    order_oth.item_to_return(conn, first, "2026-10-21T10:00:00", "o", "3")
    order_oth.agree(conn, orders.get_card(conn, cid), TODAY, NOW, "o", qty={first: 2})
    assert conn.execute("SELECT qty FROM oth_return WHERE item_id = ?", (first,)).fetchone() is None
    assert oth_stock.pieces(conn, manual=False) == {"1": 2, "2": 2}             # 4 − 2 going back, nothing falsely «returned»


def test_7_a_second_press_with_the_old_form_splits_nothing(tmp_path):
    conn, cid, card = _two_positions(tmp_path)
    first = card["items"][0]["id"]
    seen = order_oth.pieces_here(conn, orders.get_card(conn, cid))
    order_oth.agree(conn, orders.get_card(conn, cid), TODAY, NOW, "o", qty={first: 2}, seen=seen)
    with pytest.raises(order_oth.OthError, match="только что изменили"):
        order_oth.agree(conn, orders.get_card(conn, cid), TODAY, NOW, "o", qty={first: 2}, seen=seen)
    assert len(order_oth.split_of(conn, cid)) == 1


def test_9_only_a_decision_not_to_return_counts():
    assert order_oth.NO_RETURN.search("Не возвращаем, продаём") and order_oth.NO_RETURN.search("решили не возвращать")
    assert not order_oth.NO_RETURN.search("ещё не возвращали, ждём")


def test_7_a_mark_of_an_earlier_stay_never_hides_a_real_return(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    card = on_oth(conn, cid)
    item = card["items"][0]["id"]
    order_oth.item_to_return(conn, item, "2026-10-16T10:00:00", "o", "2")       # stay 1: a mark of 2
    orders.move_card(conn, cid, stage(conn, "at_point"), "2026-10-17T10:00:00", "o")
    orders.move_card(conn, cid, stage(conn, "oth"), "2026-10-18T10:00:00", "o")   # stay 2: the old oth_return is dropped
    order_oth.mark_returned(conn, item, "2026-10-19T10:00:00", "o", "1")        # a real return of 1
    assert order_oth.pieces_here(conn, orders.get_card(conn, cid)) == {item: 3}


def test_8_a_position_given_back_whole_does_not_block_a_part(tmp_path):
    conn, cid, card = _two_positions(tmp_path)
    first, second = card["items"][0]["id"], card["items"][1]["id"]
    order_oth.mark_returned(conn, second, NOW, "o")                             # the second all given back
    order_oth.agree(conn, orders.get_card(conn, cid), TODAY, NOW, "o", qty={str(first): "3", str(second): "0"})
    assert [int(i["qty"]) for i in orders.get_card(conn, cid)["items"] if i["id"] == first] == [1]


def test_8_the_remainder_the_supplier_refused_is_not_sent_back_on_its_seventh_day(tmp_path):
    conn = db(tmp_path)
    started(conn)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    on_oth(conn, cid)
    card = returning(conn, cid)
    order_oth.agree(conn, card, TODAY, NOW, "o", qty={card["items"][0]["id"]: 3})
    [rest] = order_oth.split_of(conn, cid)
    assert order_oth.ask_seventh_day(conn, "2026-11-05T12:30:00") == 0
    assert orders.get_card(conn, rest)["stage"]["code"] == "oth"


def test_3_a_position_still_marked_keeps_the_task_when_another_is_given_back(tmp_path):
    conn, cid, card = _two_positions(tmp_path)
    first, second = card["items"][0]["id"], card["items"][1]["id"]
    order_oth.item_to_return(conn, first, "2026-10-21T10:00:00", "o")           # A × 4 waits for the agreement
    order_oth.mark_returned(conn, second, NOW, "o")                             # B × 2 given back
    assert [t["title"] for t in open_tasks(conn, cid)] == [order_oth.RETURN_TASK]
    assert order_oth.item_mark(order_oth.item_marks(conn), first, orders.get_card(conn, cid)["entered_at"])
