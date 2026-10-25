"""What managers write to the bot: commands that move an order and free questions."""
from __future__ import annotations

from tires_sync import order_engine as engine, order_manager as mgr, order_points as points, order_rules as rules, orders, store

NOW = "2026-10-12T12:00:00"


def setup(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    orders.ensure_pipeline(conn, NOW)
    pid = points.save_point(conn, dict(name="Бета", channel="vk"))
    conn.execute("UPDATE point SET vk_peer_id = '111' WHERE id = ?", (pid,))
    other = points.save_point(conn, dict(name="Гамма", channel="vk"))
    conn.execute("UPDATE point SET vk_peer_id = '222' WHERE id = ?", (other,))
    conn.commit()
    return conn, pid, other


def stage(conn, code):
    return next(s["id"] for s in orders.list_stages(conn) if s["code"] == code)


def panel_numbers_from(conn, first):
    """The panel's own numbers start at `first`: a bare 1–2 digit number after the word is a quantity (the lead 2026-10-25), a real panel
    number has three digits (the owner's «приехал 429»)."""
    conn.execute("DELETE FROM sqlite_sequence WHERE name = 'order_card'")
    conn.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('order_card', ?)", (first - 1,))
    conn.commit()


def card_at(conn, pid, code="moving"):
    card = orders.create_card(conn, dict(client_name="Иван", client_phone="+7 900 111 22 33", point_id=pid, delivery_date="2026-10-15"), NOW, "o")
    orders.add_item(conn, card, goods_id="1", name="Nokian", brand_key="nokian", season="winter", diameter=16, territory="1", qty=4,
                    sale_unit=5000, supplier_price=4000, terms=None, fee_pct=0, promo_pct=0, now=NOW, author="o")
    orders.move_card(conn, card, stage(conn, "placed"), NOW, "o")
    if code != "placed":
        orders.move_card(conn, card, stage(conn, code), NOW, "o")
    return card


def test_a_command_from_the_points_manager_moves_the_order_once(tmp_path):
    conn, pid, _ = setup(tmp_path)
    rules.add_command(conn, stage(conn, "moving"), "принял", stage(conn, "at_point"))
    card = card_at(conn, pid)
    answer = mgr.handle_command(conn, "m1", "111", "Принял №%d" % card, None, "2026-10-12T15:00:00")
    assert orders.get_card(conn, card)["stage_id"] == stage(conn, "at_point") and "этапе" in answer
    assert any("Бета" in e["author"] for e in orders.list_events(conn, card))
    again = mgr.handle_command(conn, "m1", "111", "Принял №%d" % card, None, "2026-10-12T15:00:05")      # the same message delivered twice
    assert "уже" in again.lower() and orders.get_card(conn, card)["stage_id"] == stage(conn, "at_point")


def test_a_command_with_the_order_taken_from_the_reply_and_with_the_stage_rules_run(tmp_path):
    conn, pid, _ = setup(tmp_path)
    at_point = stage(conn, "at_point")
    rules.add_command(conn, stage(conn, "moving"), "принял", at_point)
    rules.add_rule(conn, at_point, "enter", 0, "tag_add", None, "на точке", "auto", None)
    card = card_at(conn, pid)
    mgr.handle_command(conn, "m2", "111", "принял", card, "2026-10-12T15:00:00")                         # no number in the text: the reply gives it
    assert orders.get_card(conn, card)["tags"] == ["на точке"]


def test_commands_that_must_not_work_change_nothing(tmp_path):
    conn, pid, other = setup(tmp_path)
    rules.add_command(conn, stage(conn, "moving"), "принял", stage(conn, "at_point"))
    card = card_at(conn, pid)
    moving = stage(conn, "moving")
    for message_id, sender, text, reply in (("a", "999", "принял №%d" % card, None),                  # a stranger
                                             ("b", "222", "принял №%d" % card, None),                  # another point's manager
                                             ("c", "111", "принял", None),                            # no number
                                             ("d", "111", "принял 9999", None),                       # no such order
                                             ("e", "111", "клиент не пришёл №%d" % card, None)):       # no such word at this stage («выдал» fits it: owner 2026-10-21)
        mgr.handle_command(conn, message_id, sender, text, reply, "2026-10-12T15:00:00")
        assert orders.get_card(conn, card)["stage_id"] == moving
    log = {r[0]: r[1] for r in conn.execute("SELECT message_id, outcome FROM order_command_log")}
    assert set(log) == {"a", "b", "c", "d", "e"} and "не привязан" in log["a"]


def test_is_a_command_tells_a_command_word_from_a_question(tmp_path):
    conn, pid, _ = setup(tmp_path)
    rules.add_command(conn, stage(conn, "moving"), "принял", stage(conn, "at_point"))
    assert mgr.is_command(conn, "Принял 17") and mgr.is_command(conn, "принял") and not mgr.is_command(conn, "Где лежат шины для заказа 17?")
    assert not mgr.is_command(conn, "")


def test_a_free_question_goes_to_the_inbox_signed_with_the_point_and_a_stranger_does_not(tmp_path):
    conn, pid, _ = setup(tmp_path)
    mgr.handle_message(conn, "q1", "111", "Подскажите, когда завоз?", NOW)
    mgr.handle_message(conn, "q1", "111", "Подскажите, когда завоз?", NOW)                             # repeated
    mgr.handle_message(conn, "q2", "999", "Здравствуйте", NOW)                                          # nobody's manager
    [m] = mgr.list_inbox(conn)
    assert m["point"] == "Бета" and m["status"] == "new" and mgr.new_count(conn) == 1
    mgr.answer(conn, m["id"], "Завтра утром", "2026-10-12T16:00:00")
    assert mgr.list_inbox(conn)[0]["status"] == "answered" and mgr.new_count(conn) == 0
    try:
        mgr.answer(conn, m["id"], "", NOW)
    except orders.OrderError:
        pass
    else:
        raise AssertionError("an empty answer must be refused")


def test_an_auto_answer_replies_only_on_a_keyword_and_only_when_enabled(tmp_path):
    conn, pid, _ = setup(tmp_path)
    aid = mgr.add_bot_answer(conn, "график, часы работы", "С 10 до 20")
    assert mgr.handle_message(conn, "q1", "111", "Какой у вас график?", NOW) == ""                     # disabled: waits for the owner
    mgr.set_bot_answer_enabled(conn, aid, True)
    assert mgr.handle_message(conn, "q2", "111", "Какой у вас График?", NOW) == "С 10 до 20"
    assert mgr.handle_message(conn, "q3", "111", "Где мои шины?", NOW) == ""
    statuses = {m["text"]: m["status"] for m in mgr.list_inbox(conn)}
    assert statuses["Какой у вас График?"] == "answered" and statuses["Где мои шины?"] == "new"
    mgr.delete_bot_answer(conn, aid)
    assert mgr.list_bot_answers(conn) == []


def test_dispatch_sends_commands_and_questions_the_right_way(tmp_path):
    conn, pid, _ = setup(tmp_path)
    rules.add_command(conn, stage(conn, "moving"), "принял", stage(conn, "at_point"))
    card = card_at(conn, pid)
    mgr.dispatch(conn, "d1", "111", "Где склад?", None, NOW)
    mgr.dispatch(conn, "d2", "111", "принял №%d" % card, None, NOW)
    assert orders.get_card(conn, card)["stage_id"] == stage(conn, "at_point")
    # the question, and (the goods arrived, the order has no Avito chat) «можно забирать» that did not go: the owner sends it by hand
    [failed, question] = mgr.list_inbox(conn)
    assert question["text"] == "Где склад?" and failed["text"].startswith("Заказ №%d: клиенту не отправлено" % card) and mgr.new_count(conn) == 2


def test_punctuation_numbers_and_errors_do_not_break_a_command(tmp_path):
    conn, pid, _ = setup(tmp_path)
    rules.add_command(conn, stage(conn, "moving"), "принял", stage(conn, "at_point"))
    panel_numbers_from(conn, 429)
    for n, text in enumerate(("Принял, %d", "принял! %d", "Принял №%d.", "принял заказ %d")):
        card = card_at(conn, pid)
        assert mgr.is_command(conn, text % card)
        mgr.handle_command(conn, "p%d" % n, "111", text % card, None, NOW)
        assert orders.get_card(conn, card)["stage_id"] == stage(conn, "at_point"), text
    card = card_at(conn, pid)
    mgr.handle_command(conn, "big", "111", "принял 99999999999999999999999", None, NOW)               # no crash, nothing moves
    assert orders.get_card(conn, card)["stage_id"] == stage(conn, "moving")
    mgr.handle_command(conn, "tires", "111", "принял 4 шины, заказ %d" % card, None, NOW)             # the number after «заказ» is the order, not the quantity
    assert orders.get_card(conn, card)["stage_id"] == stage(conn, "at_point")
    card = card_at(conn, pid)
    outcome = conn.execute("SELECT outcome FROM order_command_log WHERE message_id = 'big'").fetchone()[0]
    assert outcome != "обрабатывается"


def test_an_unexpected_failure_is_logged_not_left_hanging(tmp_path, monkeypatch):
    conn, pid, _ = setup(tmp_path)
    rules.add_command(conn, stage(conn, "moving"), "принял", stage(conn, "at_point"))
    card = card_at(conn, pid)
    monkeypatch.setattr(orders, "move_card", lambda *a, **k: 1 / 0)
    assert "Не получилось" in mgr.handle_command(conn, "z", "111", "принял №%d" % card, None, NOW)
    assert conn.execute("SELECT outcome FROM order_command_log WHERE message_id = 'z'").fetchone()[0] != "обрабатывается"


def test_auto_answers_match_whole_words_and_strangers_leave_no_text(tmp_path):
    conn, pid, _ = setup(tmp_path)
    aid = mgr.add_bot_answer(conn, "да, часы работы", "ответ")
    mgr.set_bot_answer_enabled(conn, aid, True)
    assert mgr.handle_message(conn, "a", "111", "Когда привезут?", NOW) == ""                         # «да» inside «Когда» is not a match
    assert mgr.handle_message(conn, "b", "111", "Да, спасибо", NOW) == "ответ"
    assert mgr.handle_message(conn, "c", "111", "Подскажите часы   работы!", NOW) == "ответ"
    mgr.handle_message(conn, "s", "999", "личные данные клиента", NOW)
    assert conn.execute("SELECT text FROM manager_message WHERE message_id = 's'").fetchone()[0] == ""


def test_the_order_number_is_never_guessed_from_a_quantity(tmp_path):
    conn, pid, _ = setup(tmp_path)
    rules.add_command(conn, stage(conn, "moving"), "принял", stage(conn, "at_point"))
    first = card_at(conn, pid)
    low = card_at(conn, pid)
    assert low == first + 1
    mgr.handle_command(conn, "n1", "111", "Принял %d шины заказ %d" % (low, first), None, NOW)          # a quantity equal to a live order's number
    assert orders.get_card(conn, first)["stage_id"] == stage(conn, "at_point") and orders.get_card(conn, low)["stage_id"] == stage(conn, "moving")
    for n, text in enumerate(("принял %d шины", "принял %d спасибо", "принял %d и %d")):
        text = text % ((low,) * text.count("%d"))
        mgr.handle_command(conn, "x%d" % n, "111", text, None, NOW)
        assert orders.get_card(conn, low)["stage_id"] == stage(conn, "moving"), text
    mgr.handle_command(conn, "n2", "111", "принял заказ номер %d" % low, None, NOW)
    assert orders.get_card(conn, low)["stage_id"] == stage(conn, "at_point")


def test_a_long_question_that_starts_with_a_command_word_is_a_question(tmp_path):
    conn, pid, _ = setup(tmp_path)
    rules.add_command(conn, stage(conn, "moving"), "принял", stage(conn, "at_point"))
    mgr.dispatch(conn, "q", "111", "Принял, а когда остальные шины привезут?", None, NOW)
    assert mgr.new_count(conn) == 1
    assert "номер" in mgr.dispatch(conn, "s", "111", "принял", None, NOW).lower()                         # a bare word still gets a hint


def test_the_panels_number_of_an_order_with_a_webmim_number_gets_a_hint_only_at_its_own_point(tmp_path):
    """The owner's check 2026-10-23: the point wrote «приехал 429», the order №429 has the WebMIM number 5000200 — «Не нашёл» helped nobody.
    Its own point hears the WebMIM number; another point (or an order of no point) hears «Не нашёл», the WebMIM number is not shown."""
    conn, pid, other = setup(tmp_path)
    panel_numbers_from(conn, 429)
    card = card_at(conn, pid)
    orders.update_card(conn, card, {"webmim_number": "5000200"}, NOW, "o")
    moving = stage(conn, "moving")
    answer = mgr.handle_command(conn, "h1", "111", "приехал %d" % card, None, "2026-10-12T15:00:00")
    assert answer == "У этого заказа номер WebMIM 5000200 — напишите его: «приехал 5000200»."
    assert orders.get_card(conn, card)["stage_id"] == moving and mgr.list_inbox(conn) == []
    assert conn.execute("SELECT outcome, order_id FROM order_command_log WHERE message_id = 'h1'").fetchone() == (
        "внутренний номер заказа: подсказан номер WebMIM", card)
    stranger = mgr.handle_command(conn, "h2", "222", "приехал %d" % card, None, "2026-10-12T15:00:00")
    assert stranger.startswith("Заказа №%d нет в CRM" % card) and "5000200" not in stranger
    loose = orders.create_card(conn, dict(client_name="Пётр", client_phone="+7 900 111 22 44", webmim_number="5000201"), NOW, "o")
    nobody = mgr.handle_command(conn, "h3", "111", "приехал %d" % loose, None, "2026-10-12T15:00:00")
    assert nobody.startswith("Заказа №%d нет в CRM" % loose) and "5000201" not in nobody
    assert mgr.handle_command(conn, "h4", "111", "приехал 5000200", None, "2026-10-12T15:00:00")      # the WebMIM number works as before
    assert orders.get_card(conn, card)["stage"]["code"] == "at_point"
