"""«Лист ожидания — автодействия при появлении предложения» (the owner 2026-11-27): the switch «Автоотправка предложений из листа ожидания»
(off by default: the manager's task with the words and the button), «появился аналог», the task to call when there is no Avito chat, the task
«поставьте цену», the bare answers «Да» / «Нет», the lines of an order taken from its positions. A fake Avito only: nothing goes anywhere."""
from __future__ import annotations

import pytest

from panel_helpers import login, make_app, post
from test_order_wait_auto import NOW, NEXT, WANT, WORDS, env, price_of, run, said, stage, tasks, tire, waiting  # noqa: F401
from test_stuck_sending import Sender
from tires_sync import chats, clients, message_texts as mt, order_engine as engine, order_wait, orders, store, users

ANALOG = ("Добрый день. Ранее вас добавляли в лист ожидания: у нас на складе появился аналог — «%s». Цена: %s за штуку. Актуально ещё? "
          "Это авто-сообщение. Можете ответить: Да — тогда позову менеджера. Нет — тогда закрою это ожидание и не будем вас беспокоить.")


@pytest.fixture
def off(env):
    """The switch as it is by default: off."""
    conn, _row = env
    conn.execute("DELETE FROM app_state WHERE key = ?", (order_wait.AUTO_KEY,))
    conn.commit()
    return env


def kumho(**changes):
    return tire(**{"brand_key": "KUMHO", "code": "K1", "goods_id": "300", "name": "Kumho 205/55 R16", "name_orig": "Kumho WinterCraft 205/55 R16",
                   "model_raw": "WinterCraft", **changes})


def price_k(offer, chat_row):
    return {"TS1": 8500, "K1": 7000}.get(offer.code)


def titles(conn, card):
    return [t["title"] for t in tasks(conn, card)]


def events(conn, card):
    return [e["text"] for e in orders.list_events(conn, card)]


# --- the switch ----------------------------------------------------------------------------------------------------------------------------

def test_the_switch_is_off_by_default_the_manager_gets_the_words_and_nothing_goes(off):
    conn, chat_row = off
    assert not order_wait.auto_on(conn)
    card, sender, told = waiting(off), Sender(), []
    run(conn, sender, [tire()], notify=lambda *a: told.append(a))
    assert sender.sent == [] and titles(conn, card) == [order_wait.TITLE_EXACT] and len(told) == 1
    assert order_wait.AUTO_OFF in tasks(conn, card)[0]["body"]
    assert order_wait.form_text(conn, orders.get_card(conn, card), [tire()], price_of, chat_row) == WORDS % ("Ikon Autograph 205/55 R16", "8 500 ₽")
    run(conn, sender, [tire()], NEXT, notify=lambda *a: told.append(a))                     # once per stay: no second task, no second alert
    assert sender.sent == [] and len(tasks(conn, card)) == 1 and len(told) == 1


def test_the_switch_on_sends_by_itself_once(env):
    conn, _row = env
    card, sender = waiting(env), Sender()
    run(conn, sender, [tire()])
    run(conn, sender, [tire()], NEXT)
    assert sender.sent == [WORDS % ("Ikon Autograph 205/55 R16", "8 500 ₽")] and tasks(conn, card) == []
    assert len([e for e in events(conn, card) if e.startswith(order_wait.TOLD)]) == 1


def test_the_owners_switch_is_audited(off):
    conn, _row = off
    assert order_wait.set_auto(conn, True, NOW, "owner") and order_wait.auto_on(conn)
    assert not order_wait.set_auto(conn, True, NOW, "owner")
    assert order_wait.set_auto(conn, False, NOW, "owner") and not order_wait.auto_on(conn)
    assert conn.execute("SELECT COUNT(*) FROM audit WHERE action = 'wait_autosend'").fetchone()[0] == 2


# --- analogs ---------------------------------------------------------------------------------------------------------------------------------

def test_an_analog_goes_by_itself_with_its_own_words_once(env):
    conn, _row = env
    card, sender = waiting(env, dict(WANT, any_brand="1")), Sender()
    assert run(conn, sender, [kumho()], price=price_k)["analogs"] == [card]
    assert sender.sent == [ANALOG % ("Kumho WinterCraft 205/55 R16", "7 000 ₽")] and tasks(conn, card) == []
    assert [e for e in events(conn, card) if e.startswith(order_wait.TOLD_ANALOG)] and order_wait.TAG_ANALOG in orders.get_card(conn, card)["tags"]
    run(conn, sender, [kumho()], NEXT, price=price_k)
    assert len(sender.sent) == 1 and tasks(conn, card) == []


def test_an_analog_with_the_switch_off_is_the_task_with_the_analog_words(off):
    conn, chat_row = off
    card, sender = waiting(off, dict(WANT, any_brand="1")), Sender()
    run(conn, sender, [kumho()], price=price_k)
    assert sender.sent == [] and titles(conn, card) == [order_wait.TITLE_ANALOG]
    words = order_wait.form_text(conn, orders.get_card(conn, card), [kumho()], price_k, chat_row, "analog")
    assert words == ANALOG % ("Kumho WinterCraft 205/55 R16", "7 000 ₽")


def test_two_analog_models_are_the_managers_choice(env):
    conn, _row = env
    card, sender = waiting(env, dict(WANT, any_brand="1")), Sender()
    run(conn, sender, [kumho(), kumho(code="K2", goods_id="301", model_raw="Other")], price=price_k)
    assert sender.sent == [] and titles(conn, card) == [order_wait.TITLE_ANALOG]
    assert "несколько аналогов" in tasks(conn, card)[0]["body"]


def test_the_exact_tire_after_the_analog_went_is_the_managers_task_no_second_message(env):
    conn, _row = env
    card, sender = waiting(env, dict(WANT, any_brand="1")), Sender()
    run(conn, sender, [kumho()], price=price_k)
    run(conn, sender, [kumho(), tire()], NEXT, price=price_k)
    assert len(sender.sent) == 1 and titles(conn, card) == [order_wait.TITLE_EXACT]
    assert "уже ушло предложение аналога" in tasks(conn, card)[0]["body"]


# --- no chat, no price -------------------------------------------------------------------------------------------------------------------------

def test_no_chat_is_the_task_to_call_with_the_client_the_order_and_the_price(env):
    conn, _row = env
    card = waiting(env, with_chat=False)
    orders.update_card(conn, card, {"client_phone": "+7 900 000-00-00"}, NOW, "worker2")
    sender = Sender()
    run(conn, sender, [tire()])
    [task] = tasks(conn, card)
    assert sender.sent == [] and task["title"] == order_wait.TITLE_CALL
    for words in ("позвоните клиенту", "Иван", "900", "Состав заказа", "Ikon Autograph 205/55 R16", "8 500 ₽ за штуку"):
        assert words in task["body"]
    run(conn, sender, [tire()], NEXT)
    assert len(tasks(conn, card)) == 1


def test_no_chat_and_an_analog_is_the_task_to_call_about_the_analog(env):
    conn, _row = env
    card = waiting(env, dict(WANT, any_brand="1"), with_chat=False)
    run(conn, Sender(), [kumho()], price=price_k)
    [task] = tasks(conn, card)
    assert task["title"] == order_wait.TITLE_CALL_ANALOG and "появился аналог" in task["body"] and "аналог Kumho WinterCraft" in task["body"]


def test_no_price_is_the_task_to_set_the_price_its_form_has_the_gap_and_such_words_never_go(env):
    conn, chat_row = env
    card, sender = waiting(env), Sender()
    run(conn, sender, [tire()], price=lambda o, row: None)
    assert sender.sent == [] and titles(conn, card) == [order_wait.TITLE_PRICE]
    assert order_wait.PRICE_GAP in tasks(conn, card)[0]["body"]
    words = order_wait.form_text(conn, orders.get_card(conn, card), [tire()], lambda o, row: None, chat_row)
    assert words == WORDS % ("Ikon Autograph 205/55 R16", order_wait.PRICE_GAP)


# --- the answers -------------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, answer", (("Да", "yes"), (" да. ", "yes"), ("ДА", "yes"), ("Нет", "no"), ("нет.", "no"), ("  НЕТ  ", "no"),
                                          ("Да, беру", ""), ("нет, спасибо", ""), ("да!", ""), ("", ""), ("Не надо", "")))
def test_only_a_bare_yes_or_no_is_read(text, answer):
    assert order_wait.answer_of(text) == answer


def told(env):
    conn, chat_row = env
    card, sender = waiting(env), Sender()
    run(conn, sender, [tire()])
    assert len(sender.sent) == 1
    return conn, chat_row, card


def test_yes_calls_the_manager_with_the_tag_the_order_stays_waiting(env):
    conn, chat_row, card = told(env)
    alerts = []
    said(conn, chat_row, "r1", "2026-11-04T12:30:00", "Да")
    assert order_wait.replies(conn, "2026-11-04T12:31:00", lambda *a: alerts.append(a)) == 1
    [task] = [t for t in engine.list_tasks(conn, card) if t["rule_id"] == order_wait.TASK_REPLY]
    got = orders.get_card(conn, card)
    assert task["title"] == order_wait.TITLE_YES and task["status"] == "pending" and alerts[0][1] == order_wait.TITLE_YES
    assert order_wait.TAG_YES in got["tags"] and got["stage"]["code"] == "wait"
    said(conn, chat_row, "r2", "2026-11-04T12:40:00", "Нет")                                  # read once: the first answer decides
    assert order_wait.replies(conn, "2026-11-04T12:41:00") == 0 and orders.get_card(conn, card)["stage"]["code"] == "wait"


def test_no_closes_the_waiting_by_the_cancel_stage_with_its_reason_and_keeps_the_lines(env):
    conn, chat_row, card = told(env)
    alerts = []
    said(conn, chat_row, "r1", "2026-11-04T12:30:00", "нет.")
    assert order_wait.replies(conn, "2026-11-04T12:31:00", lambda *a: alerts.append(a)) == 1
    got = orders.get_card(conn, card)
    assert got["stage"]["code"] == "cancel" and got["cancel_reason"] == order_wait.NO_REASON
    assert len(got["wants"]) == 1 and got["wants"][0]["brand"] == "Ikon Tyres"                 # the waiting cancelled loses nothing
    assert [t for t in engine.list_tasks(conn, card) if t["title"] == order_wait.TITLE_NO and t["status"] == "done"]
    assert any("ответил «Нет»" in e for e in events(conn, card)) and alerts[0][1] == order_wait.TITLE_NO
    client = clients.client_of_order(conn, card)
    assert client is None or clients.REFUSED_ORDER_TAG not in (client.get("tags") or [])        # not «Неблагонадёжный»
    assert order_wait.replies(conn, "2026-11-04T12:35:00") == 0


def test_any_other_answer_is_the_managers_task(env):
    conn, chat_row, card = told(env)
    said(conn, chat_row, "r1", "2026-11-04T12:30:00", "Нет, спасибо, а летние есть?")
    assert order_wait.replies(conn, "2026-11-04T12:31:00") == 1
    [task] = [t for t in engine.list_tasks(conn, card) if t["rule_id"] == order_wait.TASK_REPLY]
    assert task["title"] == order_wait.TITLE_REPLY and orders.get_card(conn, card)["stage"]["code"] == "wait"


def test_the_answer_to_the_words_a_manager_sent_from_the_form_is_read_too(off):
    conn, chat_row = off
    card = waiting(off)
    run(conn, Sender(), [tire()])
    [task] = tasks(conn, card)
    chats.send_reply(conn, Sender(), chat_row, WORDS % ("Ikon Autograph 205/55 R16", "8 500 ₽"), "o%dt%d" % (card, task["id"]), "2026-11-04T12:10:00", "worker2")
    said(conn, chat_row, "r1", "2026-11-04T12:30:00", "Да")
    assert order_wait.replies(conn, "2026-11-04T12:31:00") == 1
    assert [t["title"] for t in engine.list_tasks(conn, card) if t["rule_id"] == order_wait.TASK_REPLY] == [order_wait.TITLE_YES]


def test_the_answer_to_an_analog_is_read_the_same_way(env):
    conn, chat_row = env
    card = waiting(env, dict(WANT, any_brand="1"))
    run(conn, Sender(), [kumho()], price=price_k)
    said(conn, chat_row, "r1", "2026-11-04T12:30:00", "Нет")
    assert order_wait.replies(conn, "2026-11-04T12:31:00") == 1 and orders.get_card(conn, card)["stage"]["code"] == "cancel"


def wrote(conn, chat_row, mid, at, text="Доставка нужна?"):
    from datetime import datetime                                  # a message of ours in the chat (a manager, the Avito app)
    chats._store_message(conn, chat_row, {"id": mid, "created": int(datetime.fromisoformat(at + "+03:00").timestamp()), "direction": "out", "type": "text",
                                          "content": {"text": text}, "isRead": True})
    conn.commit()


def reply_titles(conn, card):
    return [t["title"] for t in engine.list_tasks(conn, card) if t["rule_id"] == order_wait.TASK_REPLY]


def test_a_no_after_a_question_of_the_manager_is_not_read_as_the_answer_to_the_offer(env):
    conn, chat_row, card = told(env)
    wrote(conn, chat_row, "o1", "2026-11-04T12:20:00")
    said(conn, chat_row, "r1", "2026-11-04T12:30:00", "Нет")
    assert order_wait.replies(conn, "2026-11-04T12:31:00") == 1
    assert reply_titles(conn, card) == [order_wait.TITLE_REPLY] and orders.get_card(conn, card)["stage"]["code"] == "wait"


def test_a_no_after_another_send_of_the_panel_is_the_managers_too(env):
    conn, chat_row, card = told(env)
    chats.send_reply(conn, Sender(), chat_row, "Уточните, пожалуйста, радиус", "b" * 24, "2026-11-04T12:20:00", "worker2")
    conn.execute("DELETE FROM avito_message WHERE direction = 'out' AND text LIKE 'Уточните%'")   # not synced yet: only the send log has it
    conn.commit()
    said(conn, chat_row, "r1", "2026-11-04T12:30:00", "Нет")
    order_wait.replies(conn, "2026-11-04T12:31:00")
    assert reply_titles(conn, card) == [order_wait.TITLE_REPLY] and orders.get_card(conn, card)["stage"]["code"] == "wait"


def test_a_no_in_a_chat_no_longer_the_orders_is_the_managers(env):
    conn, chat_row, card = told(env)
    said(conn, chat_row, "r1", "2026-11-04T12:30:00", "Нет")
    conn.execute("UPDATE avito_chat SET order_id = NULL WHERE id = ?", (chat_row,))
    conn.commit()
    order_wait.replies(conn, "2026-11-04T12:31:00")
    assert reply_titles(conn, card) == [order_wait.TITLE_REPLY] and orders.get_card(conn, card)["stage"]["code"] == "wait"


def two_for_one_analog(env):
    conn, chat_row = env
    a = waiting(env, dict(WANT, any_brand="1"))
    b = orders.create_card(conn, {"client_name": "Пётр"}, NOW, "worker2")
    order_wait.add_want(conn, b, dict(WANT, any_brand="1"), NOW, "worker2")
    orders.move_card(conn, b, stage(conn, "wait"), "2026-11-04T11:00:00", "worker2")
    conn.execute("INSERT INTO avito_chat (account_id, chat_id, order_id, last_at) SELECT account_id, 'c2', ?, last_at FROM avito_chat WHERE id = ?", (b, chat_row))
    conn.execute("UPDATE order_card SET entered_at = '2026-11-01T10:00:00' WHERE id = ?", (a,))
    conn.commit()
    return a, b


def test_one_analog_set_is_offered_to_one_client_and_held_for_him(env):
    conn, _row = env
    a, b = two_for_one_analog(env)
    sender = Sender()
    run(conn, sender, [kumho()], price=price_k)
    assert len(sender.sent) == 1 and [e for e in events(conn, a) if e.startswith(order_wait.TOLD_ANALOG)]
    assert titles(conn, b) == [order_wait.TITLE_ANALOG] and "не хватает" in tasks(conn, b)[0]["body"]
    run(conn, sender, [kumho()], NEXT, price=price_k)
    assert len(sender.sent) == 1 and not [e for e in events(conn, b) if e.startswith(order_wait.TOLD_ANALOG)]


def test_the_held_analog_is_not_offered_to_a_younger_order_that_wants_it_exactly(env):
    conn, chat_row = env
    a = waiting(env, dict(WANT, any_brand="1"))
    conn.execute("UPDATE order_card SET entered_at = '2026-11-01T10:00:00' WHERE id = ?", (a,))
    conn.commit()
    sender = Sender()
    run(conn, sender, [kumho()], price=price_k)
    b = orders.create_card(conn, {"client_name": "Пётр"}, NOW, "worker2")
    order_wait.add_want(conn, b, dict(brand="Kumho", model="WinterCraft", width=205, profile=55, radius=16, season="winter", studded="no", qty=4), NOW, "worker2")
    orders.move_card(conn, b, stage(conn, "wait"), "2026-11-04T12:30:00", "worker2")
    conn.execute("INSERT INTO avito_chat (account_id, chat_id, order_id, last_at) SELECT account_id, 'c2', ?, last_at FROM avito_chat WHERE id = ?", (b, chat_row))
    conn.commit()
    run(conn, sender, [kumho()], NEXT, price=price_k)
    assert len(sender.sent) == 1 and any("первым написали заказу №%d" % a in e for e in events(conn, b))


def test_the_analog_a_manager_sent_from_the_form_is_the_offer_of_the_stay(off):
    conn, chat_row = off
    card = waiting(off, dict(WANT, any_brand="1"))
    run(conn, Sender(), [kumho()], price=price_k)
    [task] = tasks(conn, card)
    chats.send_reply(conn, Sender(), chat_row, ANALOG % ("Kumho WinterCraft 205/55 R16", "7 000 ₽"), "o%dt%d" % (card, task["id"]), "2026-11-04T12:10:00", "worker2")
    store.set_state(conn, order_wait.AUTO_KEY, "1")
    sender = Sender()
    run(conn, sender, [kumho(), tire()], NEXT, price=price_k)
    assert sender.sent == [] and [e for e in events(conn, card) if e.startswith(order_wait.TOLD_ANALOG)]
    assert order_wait.TITLE_EXACT in titles(conn, card)
    assert "уже ушло предложение аналога" in next(t for t in tasks(conn, card) if t["title"] == order_wait.TITLE_EXACT)["body"]


def test_an_account_that_does_not_send_is_not_called_no_chat(env):
    conn, _row = env
    card = waiting(env)
    conn.execute("UPDATE account SET chats_send = 0")
    conn.commit()
    run(conn, Sender(), [tire()])
    [task] = tasks(conn, card)
    assert task["title"] == order_wait.TITLE_CALL and "Написать в чат Авито из панели нельзя" in task["body"] and "Чата Авито нет" not in task["body"]


# --- the positions of an order with no lines (the owner's orders 620, 513, 427, 475) -------------------------------------------------------------

def waiting_by_items(env, code="TS1"):
    conn, chat_row = env
    card = orders.create_card(conn, {"client_name": "Иван"}, NOW, "worker2")
    orders.add_item(conn, card, goods_id="100", name="Ikon Autograph 205/55 R16", brand_key="IKON TYRES", season="winter", diameter=16, territory="",
                    qty=4, sale_unit=8500, supplier_price=6000, terms={}, fee_pct=0, promo_pct=0, now=NOW, author="worker2", code=code)
    orders.update_card(conn, card, {"wait_wish": "эти шины, как появятся"}, NOW, "worker2")
    orders.move_card(conn, card, stage(conn, "wait"), NOW, "worker2")
    conn.execute("UPDATE avito_chat SET order_id = ? WHERE id = ?", (card, chat_row))
    conn.commit()
    return card


def test_an_order_waiting_by_its_positions_gets_its_lines_with_the_code_but_only_a_task_until_a_manager_confirms(env):
    conn, _row = env
    card, sender = waiting_by_items(env), Sender()
    assert order_wait.list_wants(conn, card) == []
    run(conn, sender, [tire(stock=0)])                                                         # not in stock yet: the lines are made, nothing goes
    [line] = order_wait.list_wants(conn, card)
    assert line["code"] == "TS1" and line["qty"] == 4 and (line["width"], line["profile"], line["radius"]) == (205, 55, 16)
    assert sender.sent == [] and events(conn, card).count(order_wait.FROM_ITEMS) == 1
    run(conn, sender, [tire()], NEXT)                                                          # the switch on, the lines nobody saw: the task only
    assert sender.sent == [] and titles(conn, card) == [order_wait.TITLE_EXACT] and order_wait.UNCONFIRMED_NOTE in tasks(conn, card)[0]["body"]
    run(conn, sender, [tire()], "2026-11-04T14:40:00")
    assert sender.sent == [] and len(tasks(conn, card)) == 1 and events(conn, card).count(order_wait.FROM_ITEMS) == 1 and len(order_wait.list_wants(conn, card)) == 1


def test_the_lines_from_the_positions_a_manager_saved_are_offered_by_themselves(env):
    conn, _row = env
    card, sender = waiting_by_items(env), Sender()
    run(conn, sender, [tire(stock=0)])
    [line] = order_wait.list_wants(conn, card)
    order_wait.update_want(conn, line["id"], {}, NOW, "worker2")                              # «Сохранить строку» of a person
    run(conn, sender, [tire()], NEXT)
    assert sender.sent == [WORDS % ("Ikon Autograph 205/55 R16", "8 500 ₽")] and tasks(conn, card) == []


def test_a_position_without_a_code_is_left_to_the_manager(env):
    conn, _row = env
    card = waiting_by_items(env, code="")
    conn.execute("UPDATE order_item SET goods_id = 'gone' WHERE order_id = ?", (card,))
    conn.commit()
    run(conn, Sender(), [tire()])
    assert order_wait.list_wants(conn, card) == [] and tasks(conn, card) == []


# --- the panel ----------------------------------------------------------------------------------------------------------------------------------

def test_only_the_owner_switches_and_the_page_shows_it(tmp_path):
    from datetime import datetime
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 11, 4, 12, 0, 0))
    owner = app.test_client()
    tok = login(owner)
    conn = store.connect(app.config["DB_PATH"])
    users.add_user(conn, "partner1", "Secret-pass-123", NOW, role="partner")
    conn.commit()
    partner = app.test_client()
    with partner.session_transaction() as session:
        session["user"], session["epoch"], session["csrf"] = "partner1", store.get_session_epoch(conn, "partner1"), "tok"
    page = owner.get("/orders/templates").get_data(as_text=True)
    assert "Автоотправка предложений из листа ожидания" in page and mt.entry(mt.WAIT_ANALOG)["name"].split(": ", 1)[1][1:] in page
    assert post(partner, "/orders/templates/wait-auto", "tok", on="1").status_code == 403 and not order_wait.auto_on(conn)
    assert post(owner, "/orders/templates/wait-auto", tok, on="1").status_code == 302 and order_wait.auto_on(conn)
    assert post(owner, "/orders/templates/wait-auto", tok).status_code == 302 and not order_wait.auto_on(conn)


def test_the_chat_refuses_words_with_the_gap_for_the_price(tmp_path):
    from datetime import datetime
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 11, 4, 12, 0, 0))
    owner = app.test_client()
    tok = login(owner)
    conn = store.connect(app.config["DB_PATH"])
    conn.execute("INSERT INTO avito_chat (account_id, chat_id) VALUES (1, 'c1')")
    conn.commit()
    row = conn.execute("SELECT id FROM avito_chat").fetchone()[0]
    answer = post(owner, "/chats/%d/send" % row, tok, text="Цена: %s за штуку." % order_wait.PRICE_GAP, reply_token="a" * 24)
    assert answer.status_code == 302 and conn.execute("SELECT COUNT(*) FROM chat_send_log").fetchone()[0] == 0
