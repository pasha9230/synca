"""The point's «не выкупил» (a word that led to «Отказ/невыкуп» before the merge) of an order without tyres of OTH sends it to «На ОТХ», not to
«Отмена / невыкуп» (the owner 2026-10-30): the tyres stay at the point, as after «Клиент не пришёл» → «Да, на ОТХ». The reason is the same."""
from __future__ import annotations

from test_oth_sale import new_order, take
from test_order_reserve import card_at, db, stage
from test_vk_commands import hook, ready, to_points
from tires_sync import order_manager as mgr, order_rules as rules, orders

T = "2026-10-29T10:00:00"
LATER = "2026-10-30T09:00:00"
REASON = orders.NOT_BOUGHT + " (команда точки)"


def merged(conn, from_code, word="невыкуп"):
    """A word of the points that led to «Отказ/невыкуп» before the merge (orders._merge_refused_stage marks it so)."""
    command = rules.add_command(conn, stage(conn, from_code), word, stage(conn, "cancel"))
    conn.execute("INSERT OR REPLACE INTO app_state VALUES (?, '1')", (orders.MERGED_COMMAND_KEY % command,))
    conn.commit()
    return {"id": command, "to_stage_id": stage(conn, "cancel")}


def code(conn, cid):
    return orders.get_card(conn, cid)["stage"]["code"]


def test_not_bought_at_the_point_goes_to_oth_with_the_reason(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", T)
    command = merged(conn, "at_point")
    outcome, answer = mgr.apply(conn, orders.get_card(conn, cid), command["to_stage_id"], "менеджер Бета", LATER, None, mgr.merged_reason(conn, command))
    card = orders.get_card(conn, cid)
    assert card["stage"]["code"] == "oth" and card["cancel_reason"] == REASON, outcome
    assert card["reserve_until"] == ""                                                    # as «Да, на ОТХ»: no reservation on OTH
    assert outcome == "заказ перенесён на «На ОТХ»"
    assert answer == "Заказ №%s на «На ОТХ»: шины остаются на точке нашим товаром." % mgr.label(card)
    assert mgr._return_from(card)                                                         # «возврат отдал» still fits


def test_not_bought_keeps_a_reason_of_its_own(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", T)
    orders.update_card(conn, cid, {"cancel_reason": "клиент уехал"}, T, "o")
    command = merged(conn, "at_point")
    mgr.apply(conn, orders.get_card(conn, cid), command["to_stage_id"], "менеджер Бета", LATER, None, mgr.merged_reason(conn, command))
    card = orders.get_card(conn, cid)
    assert card["stage"]["code"] == "oth" and card["cancel_reason"] == "клиент уехал"


def test_not_bought_before_the_point_marked_the_arrival_goes_to_oth_too(tmp_path):
    """«Заказ оформлен» or «Перемещение на точку»: the panel often lags behind the tyres (the stages «выдал» is taken from)."""
    conn = db(tmp_path)
    for from_code in ("placed", "moving"):
        cid = card_at(conn, from_code, T)
        command = merged(conn, from_code, word={"placed": "отказался", "moving": "отказалась"}[from_code])
        mgr.apply(conn, orders.get_card(conn, cid), command["to_stage_id"], "менеджер Бета", LATER, None, mgr.merged_reason(conn, command))
        assert code(conn, cid) == "oth" and orders.get_card(conn, cid)["cancel_reason"] == REASON


def test_not_bought_of_an_order_not_placed_goes_to_cancel_as_before(tmp_path):
    conn = db(tmp_path)
    cid = orders.create_card(conn, {"client_name": "Иван"}, T, "o")
    command = merged(conn, "new")
    mgr.apply(conn, orders.get_card(conn, cid), command["to_stage_id"], "менеджер Бета", LATER, None, mgr.merged_reason(conn, command))
    card = orders.get_card(conn, cid)
    assert card["stage"]["code"] == "cancel" and card["cancel_reason"] == REASON


def test_another_word_into_cancel_still_cancels(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", T)
    command = {"id": rules.add_command(conn, stage(conn, "at_point"), "отменили", stage(conn, "cancel")), "to_stage_id": stage(conn, "cancel")}
    mgr.apply(conn, orders.get_card(conn, cid), command["to_stage_id"], "менеджер Бета", LATER, None, mgr.merged_reason(conn, command))
    card = orders.get_card(conn, cid)
    assert card["stage"]["code"] == "cancel" and card["cancel_reason"] == "команда точки"


def test_not_bought_of_an_order_with_tyres_of_oth_is_refused_as_before(tmp_path):
    conn = db(tmp_path)
    card_at(conn, "oth", T)
    cid = new_order(conn, qty=2)
    take(conn, cid)
    for step in ("placed", "moving", "at_point"):
        orders.move_card(conn, cid, stage(conn, step), LATER, "o")
    command = merged(conn, "at_point")
    outcome, _answer = mgr.apply(conn, orders.get_card(conn, cid), command["to_stage_id"], "менеджер Бета", LATER, None,
                                 mgr.merged_reason(conn, command))
    assert outcome.startswith("не выполнено") and "шины с ОТХ" in outcome and code(conn, cid) == "at_point"


def test_the_vk_word_sends_it_to_oth(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    orders.move_card(conn, card, stage(conn, "at_point"), "2026-10-13T10:00:00", "o")
    merged(conn, "at_point")
    hook(app, conn, "невыкуп 5000100")
    got = orders.get_card(conn, card)
    assert got["stage"]["code"] == "oth" and got["cancel_reason"] == REASON
    assert "шины остаются на точке нашим товаром" in to_points(app)[-1][1]


def test_the_owners_confirmation_of_the_word_sends_it_to_oth_too(tmp_path):
    """«Подтвердить» in «Входящие» of a disputed «не выкупил» (no number) does what the point's command does (review, round 1)."""
    app, client, tok, conn, pid, card = ready(tmp_path)
    orders.move_card(conn, card, stage(conn, "at_point"), "2026-10-13T10:00:00", "o")
    merged(conn, "at_point")
    hook(app, conn, "невыкуп")
    [item] = mgr.list_inbox(conn)
    mgr.confirm(conn, item["id"], "5000100", LATER, "owner")
    got = orders.get_card(conn, card)
    assert got["stage"]["code"] == "oth" and got["cancel_reason"] == REASON
