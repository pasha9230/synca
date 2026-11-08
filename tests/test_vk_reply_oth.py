"""08.11, the order 509: a point answered «Забрали» to the bot's «(бот) Шины с ОТХ (заказ №…) отложены…» and the bot did not know the order.
The number is read from the bot's own lines only (order_manager.line_order: «Заказ №…», BOT_LINES and OTH_LINE), never from digits of any text.
The number in «Шины с ОТХ (заказ №A)» is A's, the order the tyres were bought by: the line means the one order holding them now."""
from __future__ import annotations

import pytest

from tests.test_oth_numbers import NUMBER, source
from tests.test_oth_sale import LATER, new_order, take
from tests.test_order_reserve import POINT_VK, card_at, db, stage
from tires_sync import message_texts, order_manager as mgr, order_oth, order_reserve, order_templates as templates, orders, oth_sale, store, vk_callback

GROUP = "220945892"
WHEN = "2026-11-08T12:00:00"


def code(conn, cid):
    return orders.get_card(conn, cid)["stage"]["code"]


def at_point(conn, cid):
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (stage(conn, "at_point"), cid))
    conn.commit()


def reply_no(text, from_id="-" + GROUP, conn=None):
    """The number vk_callback takes from a reply of the point to `text`."""
    data = {"object": {"message": {"id": 7, "from_id": int(POINT_VK), "peer_id": int(POINT_VK), "text": "Забрали",
                                   "reply_message": {"from_id": int(from_id), "text": text}}}}
    return vk_callback._message(data, GROUP, conn)["reply_no"]


def oth_order(conn, number=NUMBER, qty=4):
    """An order on «На точке» whose tyres all come from the order on OTH of `number` (all its 4 or `qty` of them): the point got «Шины с ОТХ
    (заказ №…) отложены…»."""
    old = source(conn, number=number)
    cid = new_order(conn, qty=qty)
    take(conn, cid)
    at_point(conn, cid)
    return old, cid, mgr.signed(oth_sale.point_text(conn, orders.get_card(conn, cid), LATER))


def test_the_point_s_word_to_the_oth_line_closes_the_new_order_as_a_reply_to_the_placed_line_does(tmp_path):
    conn = db(tmp_path)
    old, cid, line = oth_order(conn)
    assert line.startswith("(бот) Шины с ОТХ (заказ №%s) отложены под нового клиента" % NUMBER)
    assert reply_no(line, conn=conn) == cid and reply_no(line) is None                  # the order holding the tyres; no base: none
    mgr.handle_command(conn, "vk-1", POINT_VK, "Забрали", reply_no(line, conn=conn), WHEN)
    assert code(conn, cid) == "done" and code(conn, old) == "cancel"                  # the order that gave the tyres away stays as it was

    plain = card_at(conn, "at_point", LATER)                                           # the same as a reply to «Заказ №… оформлен…»
    placed = mgr.signed(templates.render(conn, orders.get_card(conn, plain), templates.SAMPLES[1][2])[0])
    assert placed.startswith("(бот) Заказ ") and reply_no(placed) == int(orders.get_card(conn, plain)["webmim_number"])
    mgr.handle_command(conn, "vk-2", POINT_VK, "Забрали", reply_no(placed), WHEN)
    assert code(conn, plain) == "done"


def test_a_number_shared_with_the_cancelled_order_names_the_live_one(tmp_path):
    """The order 509 shared its WebMIM number with the cancelled 466 that gave it the tyres: the live one is meant (oth_sale.given_away)."""
    conn = db(tmp_path)
    old, cid, line = oth_order(conn)
    assert orders.get_card(conn, cid)["webmim_number"] == orders.get_card(conn, old)["webmim_number"] == NUMBER
    assert oth_sale.given_away(conn, [old, cid]) == {old}
    assert mgr.find_orders(conn, int(NUMBER)) == [cid] and mgr.find_named(conn, int(NUMBER)) == [cid]
    answer = mgr.handle_command(conn, "vk-1", POINT_VK, "Забрали", reply_no(line, conn=conn), WHEN)
    assert code(conn, cid) == "done" and code(conn, old) == "cancel" and NUMBER in answer
    assert orders.get_card(conn, old)["cancel_reason"] == oth_sale.TAKEN_REASON % cid


def test_a_number_of_its_own_still_names_the_order_holding_the_tyres(tmp_path):
    """The number not copied (a person wrote another one): the line's number is the cancelled order's only, the order meant is the new one."""
    conn = db(tmp_path)
    old, cid, line = oth_order(conn)
    orders.update_card(conn, cid, {"webmim_number": "5299999"}, LATER, "o")
    assert mgr.find_named(conn, int(NUMBER)) == [old]
    answer = mgr.handle_command(conn, "vk-1", POINT_VK, "Забрали", reply_no(line, conn=conn), WHEN)
    assert code(conn, cid) == "done" and code(conn, old) == "cancel" and "5299999" in answer


@pytest.mark.parametrize("typed", [False, True])
def test_a_part_taken_names_the_new_order_never_the_old_one_still_on_oth(tmp_path, typed):
    """The review 08.11: A gave 2 of its 4 tyres and stays on «На ОТХ» with its question of the 7th day open; «Выдал» to the line about the
    new order closes the new order, never A (with A's number copied or a number of its own)."""
    conn = db(tmp_path)
    old, cid, line = oth_order(conn, qty=2)
    if typed:
        orders.update_card(conn, cid, {"webmim_number": "5299999"}, LATER, "o")
    store.set_state(conn, order_oth._ask_key(orders.get_card(conn, old)), "asked|x")
    assert code(conn, old) == "oth" and order_oth.question_open(conn, orders.get_card(conn, old))
    assert line.startswith("(бот) Шины с ОТХ (заказ №%s)" % NUMBER) and reply_no(line, conn=conn) == cid
    mgr.handle_command(conn, "vk-1", POINT_VK, "Выдал", reply_no(line, conn=conn), WHEN)
    assert code(conn, cid) == "done" and code(conn, old) == "oth"


def test_tyres_of_one_order_in_two_new_orders_name_neither(tmp_path):
    conn = db(tmp_path)
    old, b, line = oth_order(conn, qty=2)
    c = new_order(conn, qty=2)
    take(conn, c)
    at_point(conn, c)
    assert reply_no(line, conn=conn) is None                                            # the bot asks which
    mgr.handle_command(conn, "vk-1", POINT_VK, "Выдал", reply_no(line, conn=conn), WHEN)
    assert code(conn, b) == code(conn, c) == "at_point"


def test_an_order_of_the_tyres_closed_before_is_not_meant(tmp_path):
    """The review 08.11: B took 2 of A's tyres and was issued; C took the other 2 and is at the point. «Выдал» to the line about C closes C."""
    conn = db(tmp_path)
    old = source(conn)
    b = new_order(conn, qty=2)
    take(conn, b)
    at_point(conn, b)
    orders.move_card(conn, b, stage(conn, "done"), WHEN, "o")
    c = new_order(conn, qty=2)
    take(conn, c)
    at_point(conn, c)
    line = mgr.signed(oth_sale.point_text(conn, orders.get_card(conn, c), LATER))
    assert reply_no(line, conn=conn) == c
    mgr.handle_command(conn, "vk-1", POINT_VK, "Выдал", reply_no(line, conn=conn), WHEN)
    assert code(conn, c) == "done" and code(conn, b) == "done" and code(conn, old) == "cancel"


def test_the_one_order_of_the_tyres_issued_already_is_still_named(tmp_path):
    conn = db(tmp_path)
    old, cid, line = oth_order(conn)
    orders.move_card(conn, cid, stage(conn, "done"), WHEN, "o")
    assert reply_no(line, conn=conn) == cid                                             # «уже отмечено», not «какой заказ?»
    assert "уже отмечен" in mgr.handle_command(conn, "vk-1", POINT_VK, "Выдал", reply_no(line, conn=conn), WHEN)


def test_a_take_undone_names_nothing(tmp_path):
    conn = db(tmp_path)
    source(conn)
    cid = new_order(conn)
    take(conn, cid)
    line = mgr.signed(oth_sale.point_text(conn, orders.get_card(conn, cid), LATER))
    assert reply_no(line, conn=conn) == cid
    for item_id in list(oth_sale.taken(conn, cid)):
        oth_sale.untake(conn, cid, item_id, LATER, "o")
    assert reply_no(line, conn=conn) is None


def test_every_line_of_the_bot_to_a_point_about_one_order_names_it(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", LATER)
    card = orders.get_card(conn, cid)
    label = mgr.label(card)
    lines = [message_texts.text(conn, key, order_label=label, date="09.11", why="нет чата", order_summary="Иван")
             for key in (message_texts.RESERVE_ASK, message_texts.MOVED_NOTE, message_texts.HANDOVER_REMIND, message_texts.RETURN_STATUS,
                         message_texts.RETURN_AGREED, message_texts.UNAGREED, message_texts.UNTOLD_POINT)]
    lines += ["Готово: заказ №%s на этапе «Товар на точке»." % label, order_reserve.WROTE_TASK % label,
              order_reserve.NOT_SENT_TASK % (label, "нет чата"), order_reserve.NO_DATE_TASK % (label, "заберу 12 числа, 5 штук")]
    for line in lines:
        assert reply_no(mgr.signed(line), conn=conn) == int(label), line
    for kind, until in (("before", "2026-11-09"), ("after", "2026-11-07"), ("after", "2026-11-08")):       # the panel's number: find_named knows it
        assert reply_no(mgr.signed(order_reserve._text(card, kind, until, "2026-11-08")), conn=conn) == cid
    assert mgr.find_named(conn, cid) == [cid]


@pytest.mark.parametrize("text", [
    "(бот) Шины с ОТХ (заказ №5254033) отложены под нового клиента",                     # from the community: named
])
def test_only_the_community_s_quote_names_the_order(tmp_path, text):
    conn = db(tmp_path)
    old, cid, line = oth_order(conn)
    assert reply_no(text, conn=conn) == cid
    assert reply_no(text, from_id=POINT_VK, conn=conn) is None                          # the point's own message quoted
    assert reply_no(text, from_id="-1", conn=conn) is None                              # another community


@pytest.mark.parametrize("text", [
    "(бот) Шины со своего остатка отложены под нового клиента:\n\nNokian\nКол-во 4",     # no order at all
    "(бот) Шины с ОТХ (заказ №5254033, 5254034) отложены под нового клиента",            # several orders: none of them
    "(бот) Заказа №4242 нет в CRM, необходимо добавить заказ и отправить сообщение клиенту",
    "(бот) Под №5254033 несколько заказов: владелец уточнит.",
    "(бот) Вы ответили на сообщение о заказе №5254033, а написали №5254034: какой из них?",
    "(бот) Шины заказа №5254033 уже на точке, они в объединённом заказе №5254034.",
    "(бот) У этого заказа номер 5254033 — напишите его: «выдал 5254033».",
    "(бот) Клиент 5254033 звонил, шины 4 шт, 2026",                                     # digits of any text
    "Шины с ОТХ (заказ №5254033) отложены под нового клиента",                           # not the bot's: unsigned
    "Привет, это про шины с ОТХ (заказ №5254033)",
    "(бот) Возврат по заказу №5254033",                                                  # not the whole start of the bot's line
    "",
])
def test_a_quote_without_a_line_of_the_bot_names_no_order(tmp_path, text):
    conn = db(tmp_path)
    oth_order(conn)                                                                     # an order holding tyres of 5254033 is there
    assert reply_no(text, conn=conn) is None and mgr.line_order(conn, text) is None


def test_no_quote_and_no_number_still_asks_which(tmp_path):
    conn = db(tmp_path)
    old, cid, line = oth_order(conn)
    answer = mgr.handle_command(conn, "vk-1", POINT_VK, "Забрали", reply_no("(бот) Шины со своего остатка отложены", conn=conn), WHEN)
    assert code(conn, cid) == "at_point" and answer
