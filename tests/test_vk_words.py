"""The points write the way people write (the owner 2026-10-25: «Привет, приехали» in reply to an order, «5259052 приехали»): a greeting before
the command and a number before the word are understood; a question, a negation, two commands or a number the bot did not take never are."""
from __future__ import annotations

import pytest

from test_order_manager import NOW, card_at, setup, stage
from test_vk_commands import GROUP, SENT, hook, ready, to_client, to_points
from tires_sync import order_manager as mgr, order_rules as rules, orders

PLACED = "Заказ 5000100 оформлен на перемещение.\nТерритория СПб, ул. Примерова, 35А\n\nNokian 205/55 R16 × 4\n\nПокупатель Татьяна +7 900 111 22 33"


def _webmim(conn, pid, number, code="moving"):
    card = card_at(conn, pid, code)
    orders.update_card(conn, card, {"webmim_number": str(number)}, NOW, "o")
    return card


def _outcome(conn, message_id):
    return conn.execute("SELECT outcome FROM order_command_log WHERE message_id = ?", (message_id,)).fetchone()[0]


@pytest.mark.parametrize("text, word, number", [
    ("Привет, приехали", "приехали", None),
    ("Здравствуйте! Приехали шины", "приехали", None),
    ("Добрый день, выдали 5258323", "выдали", 5258323),
    ("Доброе утро! Коллеги, приехал 5258323", "приехал", 5258323),
    ("Здорово, приехали 5258323", "приехали", 5258323),
    ("здраствуйте, приехали", "приехали", None),
    ("5259052 приехали", "приехали", 5259052),
    ("5253058, приехали!", "приехали", 5253058),
    ("№5259052 выдали", "выдали", 5259052),
    ("Заказ 17 приехал", "приехал", 17),
    ("Привет, 5259052 приехали", "приехали", 5259052),
    ("5259052 клиент не пришёл", mgr.NO_SHOW, 5259052),
    ("Добрый вечер, клиент не пришёл 5259052", mgr.NO_SHOW, 5259052),
    ("приехал 123456", "приехал", 123456),                          # as before
    ("клиент не пришёл 123", mgr.NO_SHOW, 123),
    ("Приехали шины", "приехали", None),
    ("5259052 — приехали", "приехали", 5259052),
    ("5259052 – приехали", "приехали", 5259052),
    ("👋 Привет, приехали", "приехали", None),
    ("Ребят, приехали", "приехали", None),
])
def test_a_greeting_and_a_number_in_front_are_read_around_the_command(text, word, number):
    assert mgr._word(text) == word and mgr.order_number(text) == number, text


@pytest.mark.parametrize("text", ["Привет", "Привет, коллеги!", "Иван, приехали", "2 приехали", "1234 приехали", "приветливо приехали"])
def test_no_greeting_no_name_and_a_quantity_in_front_change_nothing(text):
    assert mgr._word(text) not in mgr.BUILTIN and mgr.order_number(text) is None, text


def test_the_commands_with_a_greeting_or_a_number_in_front_move_the_order(tmp_path):
    conn, pid, _ = setup(tmp_path)
    for n, text in enumerate(("Добрый день, приехали %d", "%d приехали", "№%d приехал", "Привет! %d приехали")):
        card = _webmim(conn, pid, 5259050 + n)
        assert mgr.is_command(conn, text % (5259050 + n)), text
        mgr.dispatch(conn, "a%d" % n, "111", text % (5259050 + n), None, NOW)
        assert orders.get_card(conn, card)["stage"]["code"] == "at_point", text
    card = _webmim(conn, pid, 5258323, "at_point")
    mgr.dispatch(conn, "i", "111", "Добрый день, выдали 5258323", None, NOW)
    assert orders.get_card(conn, card)["stage"]["code"] == "done"
    card = _webmim(conn, pid, 5259060, "at_point")
    mgr.dispatch(conn, "n", "111", "5259060 клиент не пришёл", None, NOW)                          # the stage stays, a red record
    assert _outcome(conn, "n").startswith("клиент не пришёл: красная запись")


def test_a_greeting_alone_or_without_a_number_is_not_guessed(tmp_path):
    conn, pid, _ = setup(tmp_path)
    card = _webmim(conn, pid, 5259052)
    assert mgr.dispatch(conn, "h", "111", "Привет", None, NOW) == mgr.HINT                        # the hint, as before
    answer = mgr.dispatch(conn, "g", "111", "Привет, приехали", None, NOW)                        # like a bare «приехали»: the point is asked
    assert "О каком заказе речь" in answer and orders.get_card(conn, card)["stage"]["code"] == "moving"
    mgr.dispatch(conn, "q", "111", "2 приехали", 5259052, NOW)                                      # a quantity: a question, as before
    assert orders.get_card(conn, card)["stage"]["code"] == "moving"


@pytest.mark.parametrize("text", [
    "не приехали", "ещё не приехал", "пока не приехали", "шины не приехали?", "приехали?", "Привет, не приехали", "Добрый день, ещё не приехал",
    "5259052 не приехали", "приехали не все", "приехали, но не все", "приехали 2, одной нет", "приехали, одной нету", "Нет, не приехали", "Приехали? 5259052",
    "приехали и выдали 5259052", "приехал 5259052, клиент забрал", "клиент не пришёл, выдали",
    "приехали 5259052 и 5253058", "приехал заказ 5259052, 4 шт", "приехал 5259052, заказ 5259052",   # «приехали 5259052 шины»: a command now (the owner 2026-10-25)
    "клиент не пришёл, не отвечает",
    # review 1: a marked number in front that the parser did not take is never read as «the order of the reply»
    "Заказали 4, приехали 3", "Заказ 4 приехали 2", "№4 приехали 2", "№4, приехали 2 из 4", "заказ 17, приехали 2 шт", "Заказчик 2 приехал",
    "приехали заказ 17, 4 шт", "приехали ни одной", "приехали неа", "приехали не-а", "приехали 5259052？",
    "приехали 5 259 053", "приехал 5 259 052",                                   # review 2: a number with spaces is not taken, so never guessed
    # the owner 2026-10-25: «приехал клиент» is about the client, a partial delivery is not «можно забирать»
    "Приехал клиент", "Привет, приехал клиент", "приехала клиентка за заказом", "Приехали шины, клиента ждём", "Добрый день, приехал клиент 5259052",
    "приехали 3 из 4", "приехали 2 шт из 4", "Приехали только 2", "приехали пока 2", "пока 2 приехали", "приехала только одна",
    "приехали только две шины", "приехали частично", "выдали 2 из 4",
    # the lead's review 2026-10-25: a part in other words, a defect, a doubt
    "приехали 3 шины из 4", "приехали 2 штуки из 4", "приехали 2шт из 4", "приехали 3шт. из 4", "приехали 3из4", "приехали 3 из 4х", "приехали 2 из",
    "приехали три из четырёх", "две из четырёх приехали", "приехали две из четырёх", "приехали 3/4", "Приехали, кроме одной", "приехали без одной",
    "приехали без двух", "приехали, нехватка одной", "приехали, недостача", "Приехали, но брак", "приехали но одна битая",
    "приехали, одна повреждена", "приехали вроде", "приехали наверное", "приехали ли",
    # the lead's last review: a defect or a mix-up anywhere in a word, «другие»
    "приехали, две побиты", "приехали разбитые", "приехали, царапина на одной", "приехали, порез на боковине", "приехали, грыжа на одной",
    "приехали, диск сломан", "приехали, одна порвана", "приехали с дефектом", "приехали, вмятина", "приехали, перепутали размер",
    "приехали по ошибке", "приехали другие", "приехали, но другая модель", "приехали другого размера",
])
def test_a_question_a_negation_two_commands_or_a_number_not_taken_are_never_a_command(tmp_path, text):
    conn, pid, _ = setup(tmp_path)
    card = _webmim(conn, pid, 5259052, "at_point" if "клиент" in text else "moving")
    before = orders.get_card(conn, card)["stage"]["code"]
    assert not mgr.is_command(conn, text), text
    mgr.dispatch(conn, "x", "111", text, 5259052, NOW)                                             # even in reply to the order
    assert orders.get_card(conn, card)["stage"]["code"] == before and mgr.new_count(conn) == 1, text


@pytest.mark.parametrize("text", ["приехали, заказчику 2 раза звонил", "выдал, заказчик 3 шины забрал", "приехали, как заказывали 4 шт"])
def test_a_word_that_only_starts_like_order_never_names_an_order(tmp_path, text):
    """Review 2: «заказчик», «заказывали» are not «заказ»: the quantity after them is not the panel's order №2…4."""
    conn, pid, _ = setup(tmp_path)
    cards = [card_at(conn, pid) for _ in range(4)]
    assert mgr.order_number(text) is None, text
    mgr.dispatch(conn, "z", "111", text, None, NOW)
    assert all(orders.get_card(conn, c)["stage"]["code"] == "moving" for c in cards), text


@pytest.mark.parametrize("text", ["приехали 4 шт", "приехали все", "Приехали все 4", "приехали, спасибо, пока", "выдали клиенту", "Приехали шины",
                                  "приехали все 4 шины", "Приехали, всё на месте", "приехали, ждём"])
def test_a_quantity_without_a_part_and_a_client_of_the_issue_stay_a_command(tmp_path, text):
    """The partial rule is narrow (the owner 2026-10-25): «N из M», «только/пока N», «частично»; «приехали 4 шт» and «приехали все» are commands,
    «клиент» counts against an arrival only."""
    conn, pid, _ = setup(tmp_path)
    card = _webmim(conn, pid, 5259052, "at_point" if text.startswith("выдали") else "moving")
    assert mgr.is_command(conn, text), text
    mgr.dispatch(conn, "k", "111", text, 5259052, NOW)
    assert orders.get_card(conn, card)["stage"]["code"] == ("done" if text.startswith("выдали") else "at_point"), text


def test_the_words_that_worked_still_work(tmp_path):
    conn, pid, _ = setup(tmp_path)
    rules.add_command(conn, stage(conn, "moving"), "принял", stage(conn, "at_point"))
    for n, (text, reply, code, target) in enumerate((("Приехали шины", True, "moving", "at_point"), ("приехал {}", False, "moving", "at_point"),
                                                    ("принял {}", False, "moving", "at_point"), ("Принял 4 шины, заказ {}", False, "moving", "at_point"),
                                                    ("приехали!", True, "moving", "at_point"), ("выдали", True, "at_point", "done"))):
        number = 5259000 + n
        card = _webmim(conn, pid, number, code)
        mgr.dispatch(conn, "w%d" % n, "111", text.format(number), number if reply else None, NOW)
        assert orders.get_card(conn, card)["stage"]["code"] == target, text
    card = _webmim(conn, pid, 123, "at_point")
    mgr.dispatch(conn, "ns", "111", "клиент не пришёл 123", None, NOW)
    assert _outcome(conn, "ns").startswith("клиент не пришёл: красная запись")


def test_a_greeting_in_reply_to_the_order_line_moves_it_and_tells_the_client(tmp_path):
    """The case of 2026-10-25 (vk-528): «Привет, приехали» in reply to «Заказ … оформлен на перемещение» — the order is at the point and the
    client hears «можно забирать» once."""
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    hook(app, conn, "Привет, приехали", reply={"from_id": -int(GROUP), "text": PLACED})
    assert orders.get_card(conn, card)["stage"]["code"] == "at_point" and len(to_client(app)) == 1
    assert conn.execute("SELECT word FROM order_command_log").fetchone()[0] == "приехали"


def test_a_number_in_front_against_the_reply_is_doubtful_and_a_negation_in_reply_tells_nobody(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    hook(app, conn, "Привет, не приехали", reply={"from_id": -int(GROUP), "text": PLACED})
    hook(app, conn, "приехали?", event="e2", reply={"from_id": -int(GROUP), "text": PLACED})
    assert orders.get_card(conn, card)["stage"]["code"] == "moving" and to_client(app) == []
    other = orders.create_card(conn, dict(client_name="Пётр"), NOW, "o")
    orders.update_card(conn, other, {"webmim_number": "654321", "point_id": pid}, NOW, "o")
    hook(app, conn, "654321 приехали", event="e3", reply={"from_id": -int(GROUP), "text": PLACED})
    assert orders.get_card(conn, card)["stage"]["code"] == "moving" and to_client(app) == []
    assert mgr.list_inbox(conn)[0]["dispute"].startswith("в ответе заказ №5000100")


def _cancelled(conn, pid, number):
    card = _webmim(conn, pid, number)
    orders.update_card(conn, card, {"cancel_reason": "клиент передумал"}, NOW, "o")
    orders.move_card(conn, card, stage(conn, "cancel"), NOW, "o")
    return card


def test_a_command_about_a_cancelled_order_tells_the_point_it_is_cancelled(tmp_path):
    """The owner 2026-10-25: «Если заказ отменен, надо писать в ответ точке что заказ отменен». The line in «Входящие» stays as before;
    the client hears nothing."""
    conn, pid, _ = setup(tmp_path)
    card = _cancelled(conn, pid, 5259052)
    assert mgr.dispatch(conn, "c1", "111", "приехал 5259052", None, NOW) == "Заказ №5259052 отменён"
    assert mgr.dispatch(conn, "c2", "111", "клиент не пришёл 5259052", None, NOW) == "Заказ №5259052 отменён"
    assert mgr.dispatch(conn, "c3", "111", "выдали", 5259052, NOW) == "Заказ №5259052 отменён"
    assert orders.get_card(conn, card)["stage"]["code"] == "cancel"
    reasons = [m["dispute"] for m in reversed(mgr.list_inbox(conn))]
    assert reasons == ["слово «приехал» не подходит к этапу «Отмена»", "«клиент не пришёл», а заказ на этапе «Отмена»",
                       "слово «выдали» не подходит к этапу «Отмена»"]
    back = _cancelled(conn, pid, 5259054)                                                           # the owner's own word of «Отмена» works as before
    rules.add_command(conn, stage(conn, "cancel"), "вернули", stage(conn, "placed"))
    assert not mgr.dispatch(conn, "c5", "111", "вернули 5259054", None, NOW).startswith("Заказ №5259054 отменён")
    assert orders.get_card(conn, back)["stage"]["code"] == "placed"
    _webmim(conn, pid, 5259053, "placed")                                                           # another stage: the answer of before
    assert mgr.dispatch(conn, "c4", "111", "клиент не пришёл 5259053", None, NOW) == \
        "Заказ №5259053 сейчас на этапе «Заказ оформлен»; сообщение передано владельцу."


def test_a_number_not_in_the_crm_tells_the_point_to_add_the_order(tmp_path):
    """The owner 2026-10-25: «Их нет в CRM, необходимо добавить заказ и отправить сообщение клиенту»; the partner's alert stays."""
    app, client, tok, conn, pid, card = ready(tmp_path)
    hook(app, conn, "Добрый день, 5259999 приехали")
    assert [a for _, a in to_points(app)] == ["(бот) Заказа №5259999 нет в CRM, необходимо добавить заказ и отправить сообщение клиенту"]
    assert mgr.list_inbox(conn)[0]["dispute"].startswith("заказа №5259999 нет в CRM") and to_client(app) == []
    assert [r["text"] for r in mgr.missing_orders(conn)] == ["Точка «Бета» сообщила о заказе №5259999 («приехали»), его нет в CRM — добавьте"]


def test_a_bare_one_or_two_digit_number_after_the_word_is_a_quantity_not_an_order(tmp_path):
    """The lead 2026-10-25: «приехали 2», «приехали 4» without «заказ/№» and not in reply name no order — no search (not even by the panel's
    number), no «нет в CRM»; the point is asked which order. «№5», «заказ 17», «приехал 429», «приехал 5259052» are orders as before."""
    conn, pid, _ = setup(tmp_path)
    small = [card_at(conn, pid) for _ in range(17)]                                                # the panel's orders №1…17, no WebMIM number
    for n, text in enumerate(("приехали 2", "приехали 4", "Привет, приехал 17", "выдали 3")):
        answer = mgr.dispatch(conn, "q%d" % n, "111", text, None, NOW)
        assert "О каком заказе речь" in answer and "нет в CRM" not in answer, text
    assert all(orders.get_card(conn, c)["stage"]["code"] == "moving" for c in small)
    assert mgr.missing_orders(conn) == [] and {m["dispute"] for m in mgr.list_inbox(conn)} == {"нет номера заказа"}
    assert mgr.order_number("№5 приехали") == 5 and mgr.order_number("заказ 17 приехали") == 17
    mgr.dispatch(conn, "m1", "111", "№5 приехали", None, NOW)
    mgr.dispatch(conn, "m2", "111", "заказ 17 приехали", None, NOW)
    assert orders.get_card(conn, small[4])["stage"]["code"] == "at_point" and orders.get_card(conn, small[16])["stage"]["code"] == "at_point"
    big = _webmim(conn, pid, 5259052)
    mgr.dispatch(conn, "w", "111", "приехал 5259052", None, NOW)
    assert orders.get_card(conn, big)["stage"]["code"] == "at_point"


def test_a_three_digit_panel_number_still_names_its_order(tmp_path):
    conn, pid, _ = setup(tmp_path)
    from test_order_manager import panel_numbers_from
    panel_numbers_from(conn, 429)
    card = card_at(conn, pid)
    assert card == 429
    mgr.dispatch(conn, "p", "111", "приехал 429", None, NOW)
    assert orders.get_card(conn, card)["stage"]["code"] == "at_point"


def test_a_long_message_is_parsed_quickly(tmp_path):
    """The lead's last review: VK gives up to 4096 signs; no form of them makes the parser slow (a cubic pattern took seconds)."""
    import time
    conn, pid, _ = setup(tmp_path)
    n = 4096
    for text in ("приехали одна" + "а" * n, "приехали " + "две " * (n // 4), "приехали 3 " + "шины " * (n // 5), "приехали №" + " " * n,
                 "приехали заказ" + " ." * (n // 2), "привет, " * (n // 8) + "приехали", "приехали " + "1 " * (n // 2), "1" * n + " приехали",
                 "приехали " + "а-" * (n // 2), "приехали " + "только " * (n // 7), "приехали" + ",.:;!-" * (n // 6) + "5", "№" * n + " приехали"):
        text = text[:n]
        started = time.perf_counter()
        mgr.is_command(conn, text), mgr._word(text), mgr.order_number(text)
        assert time.perf_counter() - started < 0.1, text[:30]
