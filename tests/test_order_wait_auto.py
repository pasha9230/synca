"""«Ожидает наличие» writes to the client by itself (the owner 2026-11-04): all wanted tires here in full → «появилось в наличии» in the order's
chat, once per stay in the stage; analogs and short stock never; no chat or a failed send → the task of the responsible. A fake Avito only."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from helpers import make_offer
from test_chats_sync import FakeClient, chat, db, msg
from test_stuck_sending import Sender
from tires_sync import chats, message_texts as mt, order_engine as engine, order_wait, orders, store
from tires_sync.models import Season

NOW = "2026-11-04T12:00:00"
NEXT = "2026-11-04T13:40:00"
WANT = dict(brand="Ikon Tyres", model="Test Model", width=205, profile=55, radius=16, season="winter", studded="no", qty=4)
PRICE = {"TS1": 8500, "TS2": 6000.5}


def tire(**changes):
    return make_offer(**{"brand_key": "IKON TYRES", "width": 205, "height": 55, "diameter": 16, "season": Season.WINTER, "studded": False,
                         "stock": 4, "code": "TS1", "name": "Ikon 205/55 R16", "name_orig": "Ikon Autograph 205/55 R16", **changes})


def price_of(offer, chat_row):
    return PRICE.get(offer.code)


@pytest.fixture
def env(tmp_path):
    conn, account = db(tmp_path)
    orders.ensure_pipeline(conn, NOW)
    conn.execute("UPDATE account SET chats_enabled = 1, chats_send = 1 WHERE id = ?", (account,))
    conn.commit()
    chats.sync_account(conn, FakeClient([[chat("c1", text="Здравствуйте")]], {"c1": [msg("m1", "Здравствуйте", read=1)]}), account, NOW)
    return conn, chats.list_chats(conn)[0]["id"]


def stage(conn, code):
    return next(s["id"] for s in orders.list_stages(conn) if s["code"] == code)


def waiting(env, *wants, with_chat=True):
    conn, chat_row = env
    card = orders.create_card(conn, {"client_name": "Иван"}, NOW, "worker2")
    for want in wants or (WANT,):
        order_wait.add_want(conn, card, want, NOW, "worker2")
    orders.move_card(conn, card, stage(conn, "wait"), NOW, "worker2")
    if with_chat:
        conn.execute("UPDATE avito_chat SET order_id = ? WHERE id = ?", (card, chat_row))
        conn.commit()
    return card


def run(conn, sender, offers, now=NOW, notify=None, price=price_of):
    return order_wait.check(conn, offers, now, notify=notify, price_of=price,
                            deliver=lambda row, text, token: chats.send_reply(conn, sender, row, text, token, now, "система"))


def tasks(conn, card):
    return [t for t in engine.list_tasks(conn, card) if t["title"] in (order_wait.TITLE_EXACT, order_wait.TITLE_CHECK, order_wait.TITLE_ANALOG)]


def events(conn, card, level=None):
    return [e["text"] for e in orders.list_events(conn, card) if level is None or e["level"] == level]


def test_all_here_in_full_the_client_hears_the_owners_words_and_no_task_is_made(env):
    conn, chat_row = env
    card, sender, told = waiting(env), Sender(), []
    assert run(conn, sender, [tire()], notify=lambda *a: told.append(a))["exact"] == [card]
    assert sender.sent == ["Здравствуйте. «Ikon Autograph 205/55 R16» появилось в наличии у нас на складе, стоимость: 8 500 ₽ за штуку. Желаете оформить заказ?"]
    assert tasks(conn, card) == [] and told == []
    assert order_wait.TAG_EXACT in orders.get_card(conn, card)["tags"]
    assert [t for t in events(conn, card, "green") if sender.sent[0] in t]                      # the order's history has it, word for word
    assert not [t for t in events(conn, card) if t.startswith("Отправлен ответ клиенту")]       # and once


def test_short_stock_writes_nothing_and_makes_no_task(env):
    conn, _row = env
    card, sender = waiting(env), Sender()
    assert run(conn, sender, [tire(stock=2)]) == {"exact": [], "analogs": []}
    assert sender.sent == [] and tasks(conn, card) == []


def test_analogs_stay_a_task_and_the_client_is_not_written_to(env):
    conn, _row = env
    card, sender = waiting(env, dict(WANT, any_brand="1")), Sender()
    assert run(conn, sender, [tire(brand_key="KUMHO", code="K1")])["analogs"] == [card]
    assert sender.sent == [] and [t["title"] for t in tasks(conn, card)] == [order_wait.TITLE_ANALOG]


def test_a_repeat_cycle_sends_nothing_and_adds_nothing_to_the_journal(env):
    conn, _row = env
    card, sender = waiting(env), Sender()
    run(conn, sender, [tire()])
    before = events(conn, card)
    run(conn, sender, [tire()], NEXT)
    assert len(sender.sent) == 1 and events(conn, card) == before and tasks(conn, card) == []


def test_a_new_stay_in_the_stage_may_send_again(env):
    conn, _row = env
    card, sender = waiting(env), Sender()
    run(conn, sender, [tire()])
    orders.move_card(conn, card, orders.list_stages(conn)[0]["id"], "2026-11-04T12:10:00", "worker2")
    run(conn, sender, [tire()], "2026-11-04T12:20:00")                                          # another stage: nobody is written to
    assert len(sender.sent) == 1
    orders.move_card(conn, card, stage(conn, "wait"), "2026-11-04T12:30:00", "worker2")
    run(conn, sender, [tire()], NEXT)
    assert len(sender.sent) == 2 and tasks(conn, card) == []


def test_a_crash_after_the_send_writes_the_history_and_never_sends_a_second_one(env):
    conn, _row = env
    card, sender = waiting(env), Sender()
    real = orders.add_event
    calls = []

    def lost(conn_, card_id, now, author, kind, text, **more):
        if text.startswith(order_wait.TOLD) and not calls:
            calls.append(1)
            raise orders.OrderError("killed")
        return real(conn_, card_id, now, author, kind, text, **more)
    orders.add_event = lost
    try:
        run(conn, sender, [tire()])
    finally:
        orders.add_event = real
    conn.rollback()
    run(conn, sender, [tire()], NEXT)
    assert len(sender.sent) == 1 and [t for t in events(conn, card) if t.startswith(order_wait.TOLD)]


def test_no_chat_gives_the_task_with_the_reason_and_the_alert(env):
    conn, _row = env
    card, sender, told = waiting(env, with_chat=False), Sender(), []
    run(conn, sender, [tire()], notify=lambda card_id, title, body: told.append(body))
    assert sender.sent == []
    [task] = tasks(conn, card)
    assert task["title"] == order_wait.TITLE_EXACT and "у заказа нет чата Авито" in task["body"] and task["body"] == told[0]
    run(conn, sender, [tire()], NEXT, notify=lambda card_id, title, body: told.append(body))
    assert len(tasks(conn, card)) == 1 and len(told) == 1


def test_account_with_sending_off_gives_the_task(env):
    conn, _row = env
    card, sender = waiting(env), Sender()
    conn.execute("UPDATE account SET chats_send = 0")
    conn.commit()
    run(conn, sender, [tire()])
    assert sender.sent == [] and "выключена отправка" in tasks(conn, card)[0]["body"]


def test_a_failed_send_gives_the_task_and_a_red_record_and_is_not_repeated(env):
    conn, chat_row = env
    card, told = waiting(env), []
    calls = []

    def refuse(row, text, token):
        calls.append(token)
        raise chats.ChatError("Авито отказал")
    order_wait.check(conn, [tire()], NOW, notify=lambda *a: told.append(a), deliver=refuse, price_of=price_of)
    [task] = tasks(conn, card)
    assert task["title"] == order_wait.TITLE_EXACT and "Авито отказал" in task["body"] and len(told) == 1
    assert [t for t in events(conn, card, "red") if "Авито отказал" in t]
    assert not [t for t in events(conn, card) if t.startswith(order_wait.TOLD)]
    order_wait.check(conn, [tire()], NEXT, notify=lambda *a: told.append(a), deliver=refuse, price_of=price_of)
    assert len(calls) == 1 and len(told) == 1


def test_a_send_that_may_have_gone_gives_the_task_that_forbids_a_second_one(env):
    conn, _row = env
    card = waiting(env)
    calls = []

    def unknown(row, text, token):
        calls.append(token)
        raise chats.ChatSendUnknown("нет ответа")
    order_wait.check(conn, [tire()], NOW, deliver=unknown, price_of=price_of)
    order_wait.check(conn, [tire()], NEXT, deliver=unknown, price_of=price_of)
    assert len(calls) == 1 and "повторно не отправляйте" in tasks(conn, card)[0]["body"]


def test_without_the_keys_the_task_is_made_as_before(env):
    conn, _row = env
    card = waiting(env)
    order_wait.check(conn, [tire()], NOW)
    assert "ключи Авито недоступны" in tasks(conn, card)[0]["body"]


def test_a_task_of_this_stay_means_the_manager_has_it_nothing_goes(env):
    """Waiting cards marked before this release: no message to all of them at the first cycle after the deploy."""
    conn, _row = env
    card, sender = waiting(env), Sender()
    order_wait.check(conn, [tire()], NOW)
    engine_task = tasks(conn, card)[0]
    conn.execute("UPDATE order_task SET status = 'done' WHERE id = ?", (engine_task["id"],))
    conn.commit()
    run(conn, sender, [tire()], NEXT)
    assert sender.sent == []


@pytest.mark.parametrize("night", ("2026-11-04T03:40:00", "2026-11-04T06:45:00", "2026-11-04T20:40:00"))
def test_the_client_is_not_woken_the_next_cycle_in_the_hours_sends(env, night):
    conn, _row = env
    card, sender = waiting(env), Sender()
    run(conn, sender, [tire()], night)
    assert sender.sent == [] and tasks(conn, card) == []
    run(conn, sender, [tire()], "2026-11-05T10:40:00")
    assert len(sender.sent) == 1


def test_two_positions_all_here_are_named_with_both_prices_in_one_message(env):
    conn, _row = env
    other = dict(WANT, width=215, qty=2)
    card, sender = waiting(env, WANT, other), Sender()
    run(conn, sender, [tire(), tire(width=215, code="TS2", goods_id="200", name_orig="Ikon 215/55 R16", stock=2)])
    assert sender.sent == ["Здравствуйте. «Ikon Autograph 205/55 R16, Ikon 215/55 R16» появилось в наличии у нас на складе, стоимость: 8 500 ₽ и 6 000.50 ₽ за штуку. "
                           "Желаете оформить заказ?"] and tasks(conn, card) == []


def test_a_part_of_the_positions_writes_nothing(env):
    conn, _row = env
    other = dict(WANT, width=215, qty=2)
    card, sender = waiting(env, WANT, other), Sender()
    assert run(conn, sender, [tire()]) == {"exact": [], "analogs": []}                          # as before: a part of the set is no sale
    assert run(conn, sender, [tire(), tire(width=215, code="TS2", stock=1)]) == {"exact": [], "analogs": []}     # short on the second
    assert sender.sent == [] and tasks(conn, card) == []


def test_one_exact_and_one_analog_stay_the_analog_task(env):
    conn, _row = env
    card, sender = waiting(env, WANT, dict(WANT, width=215, qty=2, any_brand="1")), Sender()
    assert run(conn, sender, [tire(), tire(width=215, code="K1", brand_key="KUMHO")])["analogs"] == [card]
    assert sender.sent == [] and [t["title"] for t in tasks(conn, card)] == [order_wait.TITLE_ANALOG]


def test_two_lines_of_one_position_need_the_stock_of_both(env):
    conn, _row = env
    card, sender = waiting(env, WANT, dict(WANT, qty=2)), Sender()
    run(conn, sender, [tire(stock=5)])                                                          # short stock: nothing, and it goes when there is enough
    assert sender.sent == [] and tasks(conn, card) == []
    run(conn, sender, [tire(stock=6)], NEXT)
    assert len(sender.sent) == 1
    sender.sent.clear()
    orders.move_card(conn, card, orders.list_stages(conn)[0]["id"], "2026-11-04T13:50:00", "worker2")
    conn2_card = waiting(env, dict(WANT, width=225), dict(WANT, width=225, qty=2))
    run(conn, sender, [tire(width=225, stock=6)])
    assert len(sender.sent) == 1 and sender.sent[0].count("Ikon Autograph") == 1 and tasks(conn, conn2_card) == []


def owners_row(conn, body):
    conn.execute("INSERT INTO order_template (name, audience, body) VALUES (?, 'client', ?)", (mt.entry(mt.WAIT_FOUND)["name"], body))
    conn.commit()


def test_the_owners_text_from_the_settings_goes(env):
    conn, _row = env
    waiting(env)
    sender = Sender()
    mt.save(conn, mt.WAIT_FOUND, "Добрый день! Есть {name} за {price}.", "owner", NOW)
    run(conn, sender, [tire()])
    assert sender.sent == ["Добрый день! Есть Ikon Autograph 205/55 R16 за 8 500 ₽."]


def test_the_settings_refuse_a_variable_the_text_cannot_have():
    assert mt.BY_KEY[mt.WAIT_FOUND]["to"] == "client" and set(mt.BY_KEY[mt.WAIT_FOUND]["variables"]) == {"name", "price"}
    with pytest.raises(orders.OrderError):
        mt.check(mt.entry(mt.WAIT_FOUND), "Есть {name} за {price}, заказ {order_no}")


@pytest.mark.parametrize("body", ("Есть {name}, спросите цену.", "Цена {price}.", "Есть {name} за {price}, {client_name}.", "Просто текст."))
def test_a_template_without_both_values_or_with_a_stranger_never_goes(env, body):
    conn, _row = env
    card, sender = waiting(env), Sender()
    owners_row(conn, body)
    run(conn, sender, [tire()])
    assert sender.sent == [] and "{name} и {price}" in tasks(conn, card)[0]["body"]


@pytest.mark.parametrize("changes, price, words", (
    (dict(name_orig="", name=""), price_of, "наименования"),
    (dict(name_orig="Шина {код}"), price_of, "незаполненная подстановка"),
    (dict(name_orig="Шина [Номер заказа WebMIM]"), price_of, "незаполненная подстановка"),
    (dict(code="NOPRICE"), price_of, "цены продажи"),
    ({}, lambda offer, row: 0, "цены продажи"),
    ({}, lambda offer, row: "не число", "цены продажи"),
    ({}, lambda offer, row: 1 / 0, "цены продажи"),
))
def test_an_empty_name_or_price_or_a_mark_never_goes(env, changes, price, words):
    conn, _row = env
    card, sender = waiting(env), Sender()
    run(conn, sender, [tire(**changes)], price=price)
    assert sender.sent == [] and words in tasks(conn, card)[0]["body"]


def test_one_card_without_a_chat_does_not_stop_the_others(env):
    conn, chat_row = env
    first = waiting(env, with_chat=False)
    second = waiting(env)
    sender = Sender()
    run(conn, sender, [tire(stock=8)])
    assert len(tasks(conn, first)) == 1 and len(sender.sent) == 1 and tasks(conn, second) == []


def test_the_sync_does_not_repeat_it_once_the_order_left_the_stay(env):
    conn, chat_row = env
    card = waiting(env)
    entered = orders.get_card(conn, card)["entered_at"]
    token = "wait-%d-%s" % (card, "".join(ch for ch in entered if ch.isdigit()))
    row = {"order_id": card}
    assert chats._stopped(conn, row, token) == ""
    orders.move_card(conn, card, orders.list_stages(conn)[0]["id"], NEXT, "worker2")
    assert chats._stopped(conn, row, token)
    orders.move_card(conn, card, stage(conn, "wait"), "2026-11-04T14:00:00", "worker2")
    assert chats._stopped(conn, row, token)                                                     # another stay: not that message


def test_the_cycle_hook_prices_by_the_ad_of_the_chats_account(tmp_path, monkeypatch):
    from test_review4_fixes import REF, TODAY, goods, published_ten  # noqa: F401
    from tires_sync import cli, order_offers
    active_rn = lambda c: cli.active_settings(c).territory_rn
    from tires_sync.webmim import parse_unload
    conn, _key, box = published_ten(tmp_path)
    parsed = parse_unload(goods(10), active_rn(conn))
    offer = next(iter(parsed.offers.values()))
    conn.execute("UPDATE published_state SET price = 12345 WHERE account_id = 1 AND goods_id = ?", (offer.goods_id,))
    conn.execute("UPDATE account SET chats_enabled = 1, chats_send = 1 WHERE id = 1")
    orders.ensure_pipeline(conn, NOW)
    card = orders.create_card(conn, {"client_name": "Иван"}, NOW, "worker2")
    order_wait.add_want(conn, card, {"code": offer.code, "qty": 1}, NOW, "worker2")
    orders.move_card(conn, card, stage(conn, "wait"), NOW, "worker2")
    conn.execute("INSERT INTO avito_chat (account_id, chat_id, order_id) VALUES (1, 'c1', ?)", (card,))
    conn.commit()
    sent = []
    monkeypatch.setattr(cli, "_chat_deliver", lambda conn_, box_, now: lambda row, text, token: sent.append((text, token)))
    monkeypatch.setattr(cli, "moscow_now", lambda: __import__("datetime").datetime(2026, 11, 4, 12, 0))
    cli._check_waiting(conn, SimpleNamespace(offers=parsed.offers, issues=parsed.issues), box, REF)
    assert len(sent) == 1 and "стоимость: 12 345 ₽ за штуку." in sent[0][0] and sent[0][1].startswith("wait-%d-" % card)
    assert order_offers.account_for(conn, card_id=card) == 1
    sent.clear()
    card2 = orders.create_card(conn, {"client_name": "Пётр"}, NOW, "worker2")
    order_wait.add_want(conn, card2, {"code": offer.code, "qty": 1}, NOW, "worker2")
    orders.move_card(conn, card2, stage(conn, "wait"), NOW, "worker2")
    conn.execute("INSERT INTO avito_chat (account_id, chat_id, order_id) VALUES (1, 'c2', ?)", (card2,))
    conn.commit()
    cli._check_waiting(conn, SimpleNamespace(offers=parsed.offers, issues=parsed.issues), box)                 # no reference: only the task
    assert sent == [] and tasks(conn, card2)[0]["title"] == order_wait.TITLE_EXACT


# --- the review of 2026-11-04 ----------------------------------------------------------------------------------------------------------------

def refused_then(conn, card):
    """The first try refused with a 402: the sync sends it again."""
    from test_chat_send_refusal import Refusing
    from test_send_settle import refused402
    return Refusing(refused402())


def test_a_402_gives_a_task_without_the_send_form_and_the_sync_that_delivers_closes_it(env):
    conn, _row = env
    card = waiting(env)
    sender = refused_then(conn, card)
    order_wait.check(conn, [tire()], NOW, deliver=lambda row, text, token: chats.send_reply(conn, sender, row, text, token, NOW, "система"), price_of=price_of)
    [task] = tasks(conn, card)
    assert task["title"] == order_wait.TITLE_CHECK != order_wait.TITLE_EXACT and task["status"] == "pending"     # no form: the card shows it for TITLE_EXACT
    assert [t for t in events(conn, card, "yellow") if "402" in t and "появилось" in t]         # our record; the send's own red one is as for every 402
    assert chats.settle_sends(conn, sender, lambda: sender, 1, "2026-11-04T12:02:00") == {"sent": 1}
    assert engine.get_task(conn, task["id"])["status"] == "done"
    assert [t for t in events(conn, card, "green") if t.startswith(order_wait.TOLD)]
    order_wait.check(conn, [tire()], NEXT, deliver=lambda *a: pytest.fail("a second one"), price_of=price_of)


def test_a_failed_send_keeps_the_task_with_the_form_a_person_writes(env):
    conn, _row = env
    card = waiting(env)

    def refuse(row, text, token):
        raise chats.ChatError("Авито отказал")
    order_wait.check(conn, [tire()], NOW, deliver=refuse, price_of=price_of)
    assert tasks(conn, card)[0]["title"] == order_wait.TITLE_EXACT


def test_the_closing_of_the_sync_touches_only_the_task_of_that_stay(env):
    conn, _row = env
    card = waiting(env, with_chat=False)
    order_wait.check(conn, [tire()], NOW)
    [task] = tasks(conn, card)
    order_wait.delivered(conn, "wait-%d-20200101000000" % card, "текст", NEXT, "повтор")        # another stay
    assert engine.get_task(conn, task["id"])["status"] == "pending"


@pytest.mark.parametrize("want", (
    dict(width=205, profile=55, radius=16, qty=4),                                              # only a size
    dict(WANT, season=""),                                                                      # a title that does not name the season
    dict(WANT, brand=""),
))
def test_a_line_that_does_not_name_the_tire_is_a_task_not_a_message(env, want):
    conn, _row = env
    card, sender = waiting(env, want), Sender()
    run(conn, sender, [tire()])
    assert sender.sent == [] and "не называет" in tasks(conn, card)[0]["body"]


def test_two_models_fit_the_line_the_manager_chooses(env):
    conn, _row = env
    card, sender = waiting(env, dict(WANT, model="")), Sender()
    run(conn, sender, [tire(), tire(code="TS2", goods_id="200", model_raw="Other Model")])
    assert sender.sent == [] and "несколько моделей" in tasks(conn, card)[0]["body"]


def test_a_code_names_the_tire_by_itself(env):
    conn, _row = env
    waiting(env, dict(code="TS1", qty=4))
    sender = Sender()
    run(conn, sender, [tire()])
    assert len(sender.sent) == 1


def test_the_same_tire_at_two_prices_is_a_task_at_one_price_a_message(env):
    conn, _row = env
    card, sender = waiting(env), Sender()
    two = [tire(), tire(code="TS2", goods_id="200")]
    run(conn, sender, two)                                                                      # 8 500 and 6 000.50
    assert sender.sent == [] and "разные цены" in tasks(conn, card)[0]["body"]
    other = waiting(env, dict(WANT, width=225))
    conn.execute("UPDATE avito_chat SET order_id = ?", (other,))
    conn.commit()
    run(conn, sender, [tire(width=225), tire(width=225, code="TS2", goods_id="200")], price=lambda offer, row: 7000)
    assert len(sender.sent) == 1 and "7 000 ₽" in sender.sent[0]


def test_a_question_of_the_client_nobody_answered_is_not_wiped_by_the_message(env):
    conn, chat_row = env
    card, sender = waiting(env), Sender()
    conn.execute("UPDATE avito_message SET is_read = 0, kind = 'text', direction = 'in' WHERE chat_row = ?", (chat_row,))
    conn.commit()
    run(conn, sender, [tire()])
    assert sender.sent == [] and "ждёт ответа" in tasks(conn, card)[0]["body"]


def test_three_positions_at_two_prices_keep_the_order_of_the_prices(env):
    conn, _row = env
    sender = Sender()
    waiting(env, WANT, dict(WANT, width=215), dict(WANT, width=225))
    prices = {205: 5000, 215: 6000, 225: 5000}
    run(conn, sender, [tire(), tire(width=215, code="TS2", goods_id="200", name_orig="B"), tire(width=225, code="TS3", goods_id="300", name_orig="C")], price=lambda o, row: prices[o.width])
    assert "«Ikon Autograph 205/55 R16, B, C»" in sender.sent[0] and "стоимость: 5 000 ₽, 6 000 ₽ и 5 000 ₽ за штуку." in sender.sent[0]


def test_at_night_a_missing_price_waits_for_the_morning_it_wakes_nobody(env):
    conn, _row = env
    card, sender, told = waiting(env), Sender(), []
    run(conn, sender, [tire()], "2026-11-04T03:40:00", notify=lambda *a: told.append(a), price=lambda o, row: None)
    assert tasks(conn, card) == [] and told == []


def test_an_unexpected_failure_of_one_card_does_not_stop_the_others(env, monkeypatch):
    conn, _row = env
    first, second = waiting(env), waiting(env, with_chat=False)
    real = order_wait._tell_client

    def broken(conn_, card_id, *a):
        if card_id == first:
            raise RuntimeError("boom")
        return real(conn_, card_id, *a)
    monkeypatch.setattr(order_wait, "_tell_client", broken)
    run(conn, Sender(), [tire(stock=8)])
    assert len(tasks(conn, second)) == 1


def test_an_unknown_send_found_in_the_chat_closes_the_task(env):
    from datetime import datetime

    from test_chat_send_refusal import Refusing
    from test_send_settle import out
    conn, _row = env
    card = waiting(env)
    sender = Refusing(TimeoutError())
    order_wait.check(conn, [tire()], NOW, deliver=lambda row, text, token: chats.send_reply(conn, sender, row, text, token, NOW, "система"), price_of=price_of)
    [task] = tasks(conn, card)
    assert task["title"] == order_wait.TITLE_CHECK and "повторно не отправляйте" in task["body"]
    words = conn.execute("SELECT text FROM chat_send_log WHERE token LIKE 'wait-%'").fetchone()[0]
    sender.history = [out("m9", words, int(datetime.fromisoformat(NOW).timestamp()) + 5)]
    chats.settle_sends(conn, sender, lambda: sender, 1, "2026-11-04T12:02:00")
    assert engine.get_task(conn, task["id"])["status"] == "done" and [t for t in events(conn, card, "green") if t.startswith(order_wait.TOLD)]


def lost_one(env):
    from test_chat_send_refusal import Refusing
    conn, chat_row = env
    card = waiting(env)
    sender = Refusing(TimeoutError())
    order_wait.check(conn, [tire()], NOW, deliver=lambda row, text, token: chats.send_reply(conn, sender, row, text, token, NOW, "система"), price_of=price_of)
    token, words = conn.execute("SELECT token, text FROM chat_send_log WHERE token LIKE 'wait-%'").fetchone()
    conn.execute("UPDATE chat_send_log SET status = 'lost' WHERE token = ?", (token,))
    conn.commit()
    return conn, chat_row, card, token, words


def test_sent_again_by_the_button_after_it_was_lost_closes_the_task_and_the_history_has_it(env):
    conn, chat_row, card, token, words = lost_one(env)
    task = tasks(conn, card)[0]
    sender = Sender()
    chats.send_reply(conn, sender, chat_row, words, token, "2026-11-04T12:10:00", "worker2", again=True)
    assert sender.sent == [words] and engine.get_task(conn, task["id"])["status"] == "done"
    assert [t for t in events(conn, card, "green") if t.startswith(order_wait.TOLD)]


def test_the_button_that_finds_it_in_the_chat_closes_the_task_too(env):
    from datetime import datetime

    from test_send_settle import out
    conn, chat_row, card, token, words = lost_one(env)
    task = tasks(conn, card)[0]
    sender = Sender(history=[out("m9", words, int(datetime.fromisoformat(NOW).timestamp()) + 5)])
    assert chats.send_reply(conn, sender, chat_row, words, token, "2026-11-04T12:10:00", "worker2", again=True) == "found"
    assert sender.sent == [] and engine.get_task(conn, task["id"])["status"] == "done"


def test_the_stage_hint_says_the_client_is_written_to():
    from tires_sync import onboarding_texts
    words = " ".join(onboarding_texts.STAGES["wait"]["auto"])
    assert "сама пишет клиенту" in words and "Клиенту само ничего не уходит" not in words


# --- the owner's answers of 2026-11-05 -------------------------------------------------------------------------------------------------------

def s2(**changes):
    return tire(**{"goods_id": "s2-200", "brand_key": "S2:IKON TYRES", "code": "TS2", "name_orig": "S2 Ikon 205/55 R16", **changes})


def test_webmim_is_quoted_and_named_when_the_other_supplier_has_another_price(env):
    conn, _row = env
    card, sender = waiting(env), Sender()
    run(conn, sender, [s2(), tire()])                                                           # the order of the offers does not decide
    assert "«Ikon Autograph 205/55 R16»" in sender.sent[0] and "8 500 ₽" in sender.sent[0] and tasks(conn, card) == []


def test_without_a_webmim_price_the_other_supplier_is_quoted(env):
    conn, _row = env
    waiting(env)
    sender = Sender()
    run(conn, sender, [tire(code="NOPRICE"), s2()])
    assert "«S2 Ikon 205/55 R16»" in sender.sent[0] and "6 000.50 ₽" in sender.sent[0]


def test_the_other_suppliers_alone_at_two_prices_are_a_task(env):
    conn, _row = env
    card, sender = waiting(env), Sender()
    run(conn, sender, [s2(), s2(goods_id="s3-300", code="TS1")])
    assert sender.sent == [] and "разные цены у поставщиков" in tasks(conn, card)[0]["body"]


def test_two_prices_inside_webmim_are_a_task(env):
    conn, _row = env
    card, sender = waiting(env), Sender()
    run(conn, sender, [tire(), tire(goods_id="200", code="TS2"), s2(code="TS1")])
    assert sender.sent == [] and "разные цены в WebMIM" in tasks(conn, card)[0]["body"]


def test_no_price_anywhere_is_a_task(env):
    conn, _row = env
    card, sender = waiting(env), Sender()
    run(conn, sender, [tire(code="NOPRICE"), s2(code="NOPRICE2")])
    assert sender.sent == [] and "нет цены продажи" in tasks(conn, card)[0]["body"]


def test_the_default_has_per_piece_and_the_owners_row_is_not_overwritten(env):
    conn, _row = env
    assert "{price} за штуку." in mt.entry(mt.WAIT_FOUND)["default"]
    owners_row(conn, "Есть {name} за {price}.")
    assert mt.body(conn, mt.WAIT_FOUND) == "Есть {name} за {price}."


NO_MODEL = dict(WANT, model="")


def titled(conn, chat_row, title):
    conn.execute("UPDATE avito_chat SET item_title = ? WHERE id = ?", (title, chat_row))
    conn.commit()


def test_brand_and_season_go_when_the_ad_of_the_order_names_the_one_model_in_stock(env):
    conn, chat_row = env
    waiting(env, NO_MODEL)
    titled(conn, chat_row, "Шины Ikon test  model 205/55 R16 зимние")
    sender = Sender()
    run(conn, sender, [tire()])
    assert len(sender.sent) == 1


def test_brand_and_season_go_when_a_position_of_the_order_names_it(env):
    conn, chat_row = env
    card = waiting(env, NO_MODEL)
    titled(conn, chat_row, "")
    orders.add_item(conn, card, now=NOW, author="worker2", qty=4, goods_id="100", code="TS1", name="Ikon Test Model 205/55 R16", brand_key="IKON TYRES", season="winter",
                    diameter=16, territory="", sale_unit=8500, supplier_price=7000, terms={}, fee_pct=0, promo_pct=0)
    sender = Sender()
    run(conn, sender, [tire()])
    assert len(sender.sent) == 1


@pytest.mark.parametrize("title", ("", "Шины Ikon Other Model 205/55 R16", "Шины Ikon Test Models 205/55 R16"))
def test_a_name_in_the_order_that_is_not_the_model_gives_the_task(env, title):
    conn, chat_row = env
    card, sender = waiting(env, NO_MODEL), Sender()
    titled(conn, chat_row, title)
    run(conn, sender, [tire()])
    assert sender.sent == [] and "не совпадает с названием в заказе" in tasks(conn, card)[0]["body"]


def two_waiting(env, older_second=False):
    conn, chat_row = env
    a = waiting(env)
    b = orders.create_card(conn, {"client_name": "Пётр"}, NOW, "worker2")
    order_wait.add_want(conn, b, WANT, NOW, "worker2")
    orders.move_card(conn, b, stage(conn, "wait"), NOW, "worker2")
    conn.execute("INSERT INTO avito_chat (account_id, chat_id, order_id, last_at) SELECT account_id, 'c2', ?, last_at FROM avito_chat WHERE id = ?", (b, chat_row))
    conn.execute("UPDATE order_card SET entered_at = ? WHERE id = ?", ("2026-11-01T10:00:00", b if older_second else a))
    conn.execute("UPDATE order_card SET entered_at = ? WHERE id = ?", ("2026-11-02T10:00:00", a if older_second else b))
    conn.commit()
    return (b, a) if older_second else (a, b)


def chat_of(conn, card):
    return conn.execute("SELECT chat_id FROM avito_chat WHERE order_id = ?", (card,)).fetchone()[0]


@pytest.mark.parametrize("older_second", (False, True))
def test_stock_for_one_goes_to_the_order_that_waits_longest_and_both_show_a_mark(env, older_second):
    conn, _row = env
    old, young = two_waiting(env, older_second)
    sender, sends = Sender(), []
    order_wait.check(conn, [tire()], NOW, price_of=price_of, deliver=lambda row, text, token: (sends.append(token), chats.send_reply(conn, sender, row, text, token, NOW, "система")))
    assert len(sends) == 1 and sends[0].startswith("wait-%d-" % old)
    assert [t for t in events(conn, old, "green") if t.startswith("Сообщение о наличии отправлено")]
    assert "Сообщение о наличии не отправлено: первым написали заказу №%d" % old in events(conn, young)
    assert tasks(conn, young) == [] and tasks(conn, old) == []


def test_the_mark_is_once_and_the_next_cycles_send_nothing_while_the_first_stays(env):
    conn, _row = env
    old, young = two_waiting(env)
    sender = Sender()
    run(conn, sender, [tire()])
    before = events(conn, young)
    run(conn, sender, [tire()], NEXT)
    run(conn, sender, [tire()], "2026-11-04T14:40:00")
    assert len(sender.sent) == 1 and events(conn, young) == before


def test_the_next_order_goes_once_the_first_left_the_stage(env):
    conn, _row = env
    old, young = two_waiting(env)
    sender = Sender()
    run(conn, sender, [tire()])
    orders.move_card(conn, old, orders.list_stages(conn)[0]["id"], "2026-11-04T12:30:00", "worker2")
    run(conn, sender, [tire()], NEXT)
    assert len(sender.sent) == 2 and [t for t in events(conn, young, "green") if t.startswith(order_wait.TOLD)]


def test_the_next_order_goes_when_the_stock_no_longer_holds_for_the_first(env):
    conn, _row = env
    old = waiting(env)
    young = two_small(env)
    sender = Sender()
    run(conn, sender, [tire(stock=4)], "2026-11-04T12:05:00")
    assert len(sender.sent) == 1 and tasks(conn, young) == []
    run(conn, sender, [tire(stock=2)], NEXT)                                                    # less than the first wants: it holds nothing
    assert len(sender.sent) == 2 and [t for t in events(conn, young, "green") if t.startswith(order_wait.TOLD)]
    assert old != young


def two_small(env, chat_id="c2"):
    conn, chat_row = env
    card = orders.create_card(conn, {"client_name": "Пётр"}, NOW, "worker2")
    order_wait.add_want(conn, card, dict(WANT, qty=2), NOW, "worker2")
    orders.move_card(conn, card, stage(conn, "wait"), "2026-11-04T12:01:00", "worker2")
    conn.execute("INSERT INTO avito_chat (account_id, chat_id, order_id, last_at) SELECT account_id, ?, ?, last_at FROM avito_chat WHERE id = ?", (chat_id, card, chat_row))
    conn.commit()
    return card


def test_stock_for_all_goes_to_all(env):
    conn, _row = env
    old, young = two_waiting(env)
    sender = Sender()
    run(conn, sender, [tire(stock=8)])
    assert len(sender.sent) == 2 and not [t for t in events(conn, young) if "первым" in t]


def test_stock_is_counted_across_the_orders_of_one_cycle(env):
    conn, chat_row = env
    old, young = two_waiting(env)
    third = two_small(env, "c3")
    sender = Sender()
    run(conn, sender, [tire(stock=6)], "2026-11-04T12:05:00")                                                          # 4 to the first; 2 left: not for 4, for 2 yes
    assert len(sender.sent) == 2 and tasks(conn, young) == []
    assert "Сообщение о наличии не отправлено: первым написали заказу №%d" % old in events(conn, young)
    assert [t for t in events(conn, third, "green") if t.startswith(order_wait.TOLD)]


def test_a_first_order_without_a_chat_holds_the_tires_while_its_task_is_open(env):
    conn, chat_row = env
    old, young = two_waiting(env)
    conn.execute("UPDATE avito_chat SET order_id = NULL WHERE order_id = ?", (old,))
    conn.commit()
    sender = Sender()
    run(conn, sender, [tire()])
    assert sender.sent == [] and len(tasks(conn, old)) == 1 and tasks(conn, young) == []
    assert "Сообщение о наличии не отправлено: шины первым предлагает менеджер заказу №%d" % old in events(conn, young)
    conn.execute("UPDATE order_task SET status = 'done' WHERE id = ?", (tasks(conn, old)[0]["id"],))
    conn.commit()
    run(conn, sender, [tire()], NEXT)                                                           # done: the manager offered them, still on the stage
    assert sender.sent == []
    conn.execute("UPDATE order_task SET status = 'skipped' WHERE id = ?", (tasks(conn, old)[0]["id"],))
    conn.commit()
    run(conn, sender, [tire()], "2026-11-04T14:40:00")                                          # skipped: it holds nothing
    assert len(sender.sent) == 1 and [t for t in events(conn, young, "green") if t.startswith(order_wait.TOLD)]


def test_at_night_nobody_is_told_and_nobody_has_a_mark(env):
    conn, _row = env
    old, young = two_waiting(env)
    sender = Sender()
    run(conn, sender, [tire()], "2026-11-04T03:40:00")
    assert sender.sent == [] and not [t for t in events(conn, young) if "первым" in t]
    run(conn, sender, [tire()], "2026-11-05T10:40:00")
    assert len(sender.sent) == 1 and [t for t in events(conn, old, "green") if t.startswith(order_wait.TOLD)]


def test_the_younger_order_takes_the_other_suppliers_stock_at_its_price(env):
    conn, _row = env
    old, young = two_waiting(env)
    sender = Sender()
    run(conn, sender, [tire(), s2()])
    assert len(sender.sent) == 2 and "8 500 ₽" in sender.sent[0] and "6 000.50 ₽" in sender.sent[1]


# --- the review of 8c11c0d --------------------------------------------------------------------------------------------------------------------

def test_a_code_not_in_stock_is_a_task_even_when_the_size_fits(env):
    conn, _row = env
    card, sender = waiting(env, dict(WANT, code="ZZ9")), Sender()
    run(conn, sender, [tire()])
    assert sender.sent == [] and "артикула ZZ9 нет в наличии" in tasks(conn, card)[0]["body"]


def test_a_code_in_stock_offers_that_position_not_another_of_the_size(env):
    conn, _row = env
    PRICE["ZZ9"] = 9900
    waiting(env, dict(WANT, code="ZZ9"))
    sender = Sender()
    try:
        run(conn, sender, [tire(), tire(goods_id="200", code="ZZ9", name_orig="Та самая"), s2(code="OTHER")])
    finally:
        del PRICE["ZZ9"]
    assert "«Та самая»" in sender.sent[0] and "9 900 ₽" in sender.sent[0]


@pytest.mark.parametrize("other", (dict(speed="H"), dict(load="91"), dict(runflat=True), dict(xl=True)))
def test_another_index_runflat_or_xl_is_another_tire_the_manager_chooses(env, other):
    conn, _row = env
    card, sender = waiting(env), Sender()
    run(conn, sender, [tire(speed="T", load="94", runflat=False, xl=False), tire(**{"goods_id": "200", "code": "TS2", "speed": "T", "load": "94", "runflat": False, "xl": False, **other})])
    assert sender.sent == [] and "несколько моделей или индексов" in tasks(conn, card)[0]["body"]


def test_an_empty_model_without_a_code_is_a_task(env):
    conn, _row = env
    card, sender = waiting(env, NO_MODEL), Sender()
    run(conn, sender, [tire(model_raw="")])
    assert sender.sent == [] and "не указана модель" in tasks(conn, card)[0]["body"]


def test_a_line_of_the_catalog_keeps_the_code_of_the_position():
    line = order_wait.catalog_line(tire(code="TS7"), {"qty": 4})
    assert line["code"] == "TS7"
    assert order_wait._clean(line)["code"] == "TS7"


def test_a_lone_other_brand_model_is_not_the_one_of_the_title(env):
    conn, chat_row = env
    card, sender = waiting(env, dict(NO_MODEL, brand="Nordman")), Sender()
    titled(conn, chat_row, "Nokian Hakkapeliitta R5 205/55 R16")
    run(conn, sender, [tire(brand_key="NORDMAN", model_raw="Nordman 8")])
    assert sender.sent == [] and "не совпадает с названием в заказе" in tasks(conn, card)[0]["body"]


def test_studs_named_in_the_order_must_be_the_studs_in_stock(env):
    conn, chat_row = env
    card, sender = waiting(env, dict(NO_MODEL, studded="")), Sender()
    titled(conn, chat_row, "Ikon Test Model 205/55 R16 шипованная")
    run(conn, sender, [tire(studded=False)])
    assert sender.sent == [] and "шипы" in tasks(conn, card)[0]["body"]
    titled(conn, chat_row, "Ikon Test Model 205/55 R16 шипованная")
    other = waiting(env, dict(NO_MODEL, width=225, studded=""))
    conn.execute("UPDATE avito_chat SET order_id = ?", (other,))
    conn.commit()
    run(conn, sender, [tire(width=225, studded=True)])
    assert len(sender.sent) == 1


def many_waiting(env, count):
    conn, chat_row = env
    cards = []
    for n in range(count):
        card = orders.create_card(conn, {"client_name": "К%d" % n}, NOW, "worker2")
        order_wait.add_want(conn, card, WANT, NOW, "worker2")
        orders.move_card(conn, card, stage(conn, "wait"), NOW, "worker2")
        conn.execute("INSERT INTO avito_chat (account_id, chat_id, order_id, last_at) SELECT account_id, ?, ?, last_at FROM avito_chat WHERE id = ?", ("m%d" % n, card, chat_row))
        cards.append(card)
    conn.commit()
    return cards


def test_at_most_twenty_messages_a_cycle_the_rest_go_at_the_next(env):
    conn, _row = env
    cards = many_waiting(env, 23)
    sender = Sender()
    run(conn, sender, [tire(stock=200)])
    assert len(sender.sent) == order_wait.MAX_SENDS == 20
    assert not [c for c in cards if tasks(conn, c)] and not [t for t in events(conn, cards[-1]) if "первым" in t]
    assert [t for t in events(conn, cards[19], "green") if t.startswith(order_wait.TOLD)] and not [t for t in events(conn, cards[20], "green") if t.startswith(order_wait.TOLD)]
    run(conn, sender, [tire(stock=200)], NEXT)
    assert len(sender.sent) == 23


def test_the_count_command_counts_on_a_copy_and_changes_nothing(tmp_path, capsys, monkeypatch):
    import hashlib

    from test_review4_fixes import goods, published_ten
    from tires_sync import cli
    from tires_sync.webmim import parse_unload
    conn, _key, _box = published_ten(tmp_path)
    offer = next(iter(parse_unload(goods(10), cli.active_settings(conn).territory_rn).offers.values()))
    conn.execute("UPDATE account SET chats_enabled = 1, chats_send = 1 WHERE id = 1")
    orders.ensure_pipeline(conn, NOW)
    card = orders.create_card(conn, {"client_name": "Иван"}, NOW, "worker2")
    order_wait.add_want(conn, card, {"code": offer.code, "qty": 1}, NOW, "worker2")
    orders.move_card(conn, card, stage(conn, "wait"), NOW, "worker2")
    conn.execute("INSERT INTO avito_chat (account_id, chat_id, order_id) VALUES (1, 'c1', ?)", (card,))
    conn.commit()
    conn.close()
    db = tmp_path / "t.db"
    (tmp_path / "unload.xml").write_bytes(goods(10))
    (tmp_path / "reference.txt").write_text('<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>', encoding="utf-8")
    monkeypatch.setattr(cli, "_chat_deliver", lambda *a: pytest.fail("the real channel"))
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    args = SimpleNamespace(db=str(db), unload=str(tmp_path / "unload.xml"), reference=str(tmp_path / "reference.txt"), at="2026-11-04T12:00:00")
    assert cli._cmd_wait_count(args) == 0
    out = capsys.readouterr().out
    assert "сообщений в первом цикле 1" in out and "заказы, которым ушло бы: %d" % card in out
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before


def test_the_history_keeps_the_words_that_went_not_the_price_of_the_next_cycle(env):
    conn, _row = env
    card, sender = waiting(env), Sender()
    real = orders.add_event

    def lost(conn_, card_id, now, author, kind, text, **more):
        if text.startswith(order_wait.TOLD):
            raise orders.OrderError("killed")
        return real(conn_, card_id, now, author, kind, text, **more)
    orders.add_event = lost
    try:
        run(conn, sender, [tire()])
    finally:
        orders.add_event = real
    conn.rollback()
    run(conn, sender, [tire()], NEXT, price=lambda o, row: 1)                                   # the price changed meanwhile
    told = [t for t in events(conn, card) if t.startswith(order_wait.TOLD)]
    assert len(sender.sent) == 1 and len(told) == 1 and "8 500 ₽" in told[0]


def test_delivered_writes_the_history_once_also_without_a_task_and_not_for_another_stay(env):
    conn, _row = env
    card = waiting(env)
    token = order_wait._token(card, orders.get_card(conn, card)["entered_at"])
    order_wait.delivered(conn, token, "слова", NEXT, "повтор")
    order_wait.delivered(conn, token, "слова", NEXT, "повтор")
    assert [t for t in events(conn, card) if t.startswith(order_wait.TOLD)] == [order_wait.TOLD + "слова"]
    other = waiting(env)
    order_wait.delivered(conn, "wait-%d-20200101000000" % other, "слова", NEXT, "повтор")
    assert not [t for t in events(conn, other) if t.startswith(order_wait.TOLD)]


def test_the_settings_warn_when_the_template_lacks_the_name_or_the_price():
    assert "{price}" in " ".join(mt.check(mt.entry(mt.WAIT_FOUND), "Есть {name}.")[1])
    assert "{name}" in " ".join(mt.check(mt.entry(mt.WAIT_FOUND), "Цена {price}.")[1])
    assert mt.check(mt.entry(mt.WAIT_FOUND), "Есть {name} за {price}.")[1] == []


# --- the client's reply (the owner 2026-11-05) -------------------------------------------------------------------------------------------------

def said(conn, chat_row, mid, at):
    from datetime import datetime
    chats._store_message(conn, chat_row, {"id": mid, "created": int(datetime.fromisoformat(at + "+03:00").timestamp()), "direction": "in", "type": "text",
                                          "content": {"text": "да"}, "isRead": False})
    conn.commit()


def reply_tasks(conn, card):
    return [t for t in engine.list_tasks(conn, card) if t["title"] == order_wait.TITLE_REPLY]


def test_the_first_reply_makes_one_task_with_the_link_a_second_reply_none(env):
    conn, chat_row = env
    card, sender, told = waiting(env), Sender(), []
    store.set_state(conn, "public_base", "https://panel.example")
    run(conn, sender, [tire()])
    assert order_wait.replies(conn, "2026-11-04T12:01:00") == 0
    said(conn, chat_row, "r1", "2026-11-04T12:30:00")
    assert order_wait.replies(conn, "2026-11-04T12:31:00", lambda *a: told.append(a)) == 1
    [task] = reply_tasks(conn, card)
    assert "https://panel.example/orders/%d" % card in task["body"] and len(told) == 1
    said(conn, chat_row, "r2", "2026-11-04T12:40:00")
    assert order_wait.replies(conn, "2026-11-04T12:41:00", lambda *a: told.append(a)) == 0
    assert len(reply_tasks(conn, card)) == 1 and len(told) == 1
    assert orders.get_card(conn, card)["stage"]["code"] == "wait"                               # not read as consent


def test_a_reply_after_the_stage_changed_makes_no_task(env):
    conn, chat_row = env
    card, sender = waiting(env), Sender()
    run(conn, sender, [tire()])
    orders.move_card(conn, card, orders.list_stages(conn)[0]["id"], "2026-11-04T12:10:00", "worker2")
    said(conn, chat_row, "r1", "2026-11-04T12:30:00")
    assert order_wait.replies(conn, "2026-11-04T12:31:00") == 0 and reply_tasks(conn, card) == []


def test_a_message_of_the_client_before_ours_makes_no_task(env):
    conn, chat_row = env
    card, sender = waiting(env), Sender()
    said(conn, chat_row, "r0", "2026-11-04T11:00:00")
    conn.execute("UPDATE avito_message SET is_read = 1")
    conn.commit()
    run(conn, sender, [tire()])
    assert len(sender.sent) == 1 and order_wait.replies(conn, "2026-11-04T12:31:00") == 0


def test_no_message_went_no_reply_task(env):
    conn, chat_row = env
    card = waiting(env)
    said(conn, chat_row, "r1", "2026-11-04T12:30:00")
    assert order_wait.replies(conn, "2026-11-04T12:31:00") == 0


def test_a_new_stay_has_its_own_reply_task(env):
    conn, chat_row = env
    card, sender = waiting(env), Sender()
    run(conn, sender, [tire()])
    said(conn, chat_row, "r1", "2026-11-04T12:30:00")
    order_wait.replies(conn, "2026-11-04T12:31:00")
    orders.move_card(conn, card, orders.list_stages(conn)[0]["id"], "2026-11-04T12:35:00", "worker2")
    orders.move_card(conn, card, stage(conn, "wait"), "2026-11-04T12:40:00", "worker2")
    conn.execute("UPDATE avito_message SET is_read = 1")
    conn.commit()
    run(conn, sender, [tire()], "2026-11-04T12:50:00")
    assert len(sender.sent) == 2
    said(conn, chat_row, "r2", "2026-11-04T13:30:00")
    assert order_wait.replies(conn, "2026-11-04T13:31:00") == 1 and len(reply_tasks(conn, card)) == 2


def test_a_failing_alert_does_not_stop_the_look_at_the_replies_nor_repeat_the_task(env):
    conn, chat_row = env
    card, sender = waiting(env), Sender()
    run(conn, sender, [tire()])
    said(conn, chat_row, "r1", "2026-11-04T12:30:00")

    def boom(*a):
        raise RuntimeError("boom")
    assert order_wait.replies(conn, "2026-11-04T12:31:00", boom) == 1
    assert order_wait.replies(conn, "2026-11-04T12:32:00", boom) == 0 and len(reply_tasks(conn, card)) == 1


# --- the review of cb0e3c0 --------------------------------------------------------------------------------------------------------------------

def test_a_vague_line_is_the_managers_and_holds_no_tire_of_the_order_that_named_it(env):
    conn, chat_row = env
    vague, exact = two_waiting(env)
    conn.execute("UPDATE order_want SET model = '' WHERE order_id = ?", (vague,))
    conn.commit()
    sender = Sender()
    run(conn, sender, [tire(goods_id="100"), tire(goods_id="200", code="TS2", model_raw="Other Model")])
    assert "несколько моделей" in tasks(conn, vague)[0]["body"]
    assert len(sender.sent) == 1 and [t for t in events(conn, exact, "green") if t.startswith(order_wait.TOLD)]
    assert not [t for t in events(conn, exact) if "первым" in t]
    run(conn, sender, [tire(goods_id="100"), tire(goods_id="200", code="TS2", model_raw="Other Model")], NEXT)
    assert len(sender.sent) == 1


@pytest.mark.parametrize("title", ("Ikon Test Model 2 205/55 R16", "Ikon Test Model FR 205/55 R16", "Ikon Super Test Model 205/55 R16", "Ikon Test Model-2 205/55 R16"))
def test_a_longer_name_is_another_model(env, title):
    conn, chat_row = env
    card, sender = waiting(env, NO_MODEL), Sender()
    titled(conn, chat_row, title)
    run(conn, sender, [tire()])
    assert sender.sent == [] and "не совпадает с названием в заказе" in tasks(conn, card)[0]["body"]


@pytest.mark.parametrize("title", ("Зимние шины Ikon Test Model 205/55 R16", "Ikon Test Model 205/55 R16 94T XL", "IKON 205/55/16 T 94 Test Model", "Ikon Test Model, 205/55 R16, 4 шт"))
def test_the_usual_forms_of_a_name_name_the_model(env, title):
    conn, chat_row = env
    waiting(env, NO_MODEL)
    sender = Sender()
    titled(conn, chat_row, title)
    run(conn, sender, [tire()])
    assert len(sender.sent) == 1


def test_a_failure_after_the_tires_were_taken_keeps_them_the_next_order_is_not_told_a_wrong_reason(env, monkeypatch):
    conn, _row = env
    old, young = two_waiting(env)
    real = order_wait._tell_client

    def broken(conn_, card_id, *a, **k):
        if card_id == old:
            raise RuntimeError("database is locked")
        return real(conn_, card_id, *a, **k)
    monkeypatch.setattr(order_wait, "_tell_client", broken)
    sender = Sender()
    run(conn, sender, [tire()])
    assert sender.sent == [] and tasks(conn, young) == [] and not [t for t in events(conn, young) if "первым" in t]
    monkeypatch.setattr(order_wait, "_tell_client", real)
    run(conn, sender, [tire()], NEXT)
    assert len(sender.sent) == 1 and [t for t in events(conn, old, "green") if t.startswith(order_wait.TOLD)]


def test_a_vague_line_with_a_task_from_an_earlier_cycle_holds_nothing_either(env):
    conn, chat_row = env
    vague = waiting(env, NO_MODEL, with_chat=False)
    two = [tire(goods_id="100"), tire(goods_id="200", code="TS2", model_raw="Other Model")]
    sender = Sender()
    run(conn, sender, two)
    assert len(tasks(conn, vague)) == 1
    named = waiting(env)
    run(conn, sender, two, NEXT)
    assert len(sender.sent) == 1 and [t for t in events(conn, named, "green") if t.startswith(order_wait.TOLD)]


# --- the review of 157ea93 --------------------------------------------------------------------------------------------------------------------

def told_of_x_then_y_appears(env, first_sender):
    """A (brand and season, the model from the ad's title) was told of X; now Y of that size is in stock too, and B waits for X by its model."""
    conn, chat_row = env
    a = waiting(env, NO_MODEL)
    titled(conn, chat_row, "Ikon Test Model 205/55 R16")
    run(conn, first_sender, [tire()])
    b = two_small(env)
    conn.execute("UPDATE order_want SET qty = 4 WHERE order_id = ?", (b,))
    conn.commit()
    return a, b, [tire(), tire(goods_id="200", code="TS2", model_raw="Other Model")]


def test_the_tire_a_client_was_told_of_stays_held_when_another_model_appears(env):
    conn, _row = env
    sender = Sender()
    a, b, both = told_of_x_then_y_appears(env, sender)
    run(conn, sender, both, NEXT)
    assert len(sender.sent) == 1 and tasks(conn, a) == [] and tasks(conn, b) == []
    assert "Сообщение о наличии не отправлено: первым написали заказу №%d" % a in events(conn, b)


def test_the_held_position_is_the_one_named_when_webmim_had_no_price(env):
    conn, chat_row = env
    a, b = two_waiting(env)
    sender = Sender()
    offers = [tire(code="NOPRICE"), s2()]
    run(conn, sender, offers)                                                                   # A is told of the second supplier's: the only one with a price
    assert len(sender.sent) == 1 and "«S2 Ikon 205/55 R16»" in sender.sent[0]
    assert "нет цены продажи" in tasks(conn, b)[0]["body"]                                      # never the same four tires to B
    run(conn, sender, offers, NEXT)
    assert len(sender.sent) == 1


def test_a_send_that_may_have_gone_holds_the_tire_too(env):
    from test_chat_send_refusal import Refusing
    conn, chat_row = env
    a, b = two_waiting(env)
    sender = Refusing(TimeoutError())
    run(conn, sender, [tire()])
    assert tasks(conn, a)[0]["title"] == order_wait.TITLE_CHECK
    run(conn, Sender(), [tire(), tire(goods_id="200", code="TS2", model_raw="Other Model")], NEXT)
    assert tasks(conn, b) == [] and [t for t in events(conn, b) if "первым" in t]


def test_the_held_tire_is_free_when_its_stock_no_longer_covers_the_first(env):
    conn, _row = env
    sender = Sender()
    a, b, _both = told_of_x_then_y_appears(env, sender)
    conn.execute("UPDATE order_want SET qty = 2 WHERE order_id = ?", (b,))
    conn.commit()
    run(conn, sender, [tire(stock=2)], NEXT)
    assert len(sender.sent) == 2 and [t for t in events(conn, b, "green") if t.startswith(order_wait.TOLD)]


def test_gone_out_with_the_history_cut_off_is_written_when_the_stock_is_gone(env):
    conn, _row = env
    card, sender = waiting(env), Sender()
    real = orders.add_event

    def lost(conn_, card_id, now, author, kind, text, **more):
        if text.startswith(order_wait.TOLD):
            raise orders.OrderError("killed")
        return real(conn_, card_id, now, author, kind, text, **more)
    orders.add_event = lost
    try:
        run(conn, sender, [tire()])
    finally:
        orders.add_event = real
    conn.rollback()
    run(conn, sender, [], NEXT)
    assert [t for t in events(conn, card) if t.startswith(order_wait.TOLD) and "8 500 ₽" in t] and tasks(conn, card) == []


@pytest.mark.parametrize("offers", ([tire()], []))
@pytest.mark.parametrize("status", ("sending", "unknown"))
def test_a_cycle_cut_off_in_the_send_gives_the_task_without_the_form_whatever_else_is_true(env, status, offers):
    conn, chat_row = env
    card = waiting(env)
    token = order_wait._token(card, orders.get_card(conn, card)["entered_at"])
    conn.execute("INSERT INTO chat_send_log (token, chat_row, at, author, status, text) VALUES (?,?,?,?,?,?)", (token, chat_row, NOW, "система", status, "слова"))
    conn.execute("UPDATE avito_message SET is_read = 0, direction = 'in', kind = 'text' WHERE chat_row = ?", (chat_row,))
    conn.commit()
    sender = Sender()
    run(conn, sender, offers, NEXT)
    assert sender.sent == [] and tasks(conn, card)[0]["title"] == order_wait.TITLE_CHECK


def test_the_sync_stops_repeating_when_the_task_was_closed_or_skipped(env):
    conn, _row = env
    card = waiting(env)
    order_wait.check(conn, [tire()], NOW)
    token = order_wait._token(card, orders.get_card(conn, card)["entered_at"])
    row = {"order_id": card}
    assert chats._stopped(conn, row, token) == ""
    conn.execute("UPDATE order_task SET status = 'skipped' WHERE order_id = ?", (card,))
    assert "пропущена" in chats._stopped(conn, row, token)
    conn.execute("UPDATE order_task SET status = 'done' WHERE order_id = ?", (card,))
    assert "закрыта" in chats._stopped(conn, row, token)


# --- the review of c7f3252 --------------------------------------------------------------------------------------------------------------------

def test_two_webmim_positions_of_one_tire_at_one_price_hold_each_its_own(env):
    conn, chat_row = env
    a, b = two_waiting(env)
    third = two_small(env, "c3")
    conn.execute("UPDATE order_want SET qty = 4 WHERE order_id = ?", (third,))
    conn.commit()
    offers = [tire(goods_id="100", code="M1"), tire(goods_id="200", code="M2")]
    same = lambda o, row: 7000
    sender = Sender()
    run(conn, sender, offers, "2026-11-04T12:05:00", price=same)
    assert len(sender.sent) == 2
    assert [t for t in events(conn, third) if "первым написали" in t] and tasks(conn, third) == []
    run(conn, sender, offers, NEXT, price=same)
    assert len(sender.sent) == 2
    tok = order_wait._token(b, orders.get_card(conn, b)["entered_at"])
    assert store.get_state(conn, order_wait.HELD % tok) == '[["200", 4]]'


def test_two_lines_named_from_the_other_supplier_need_its_stock_for_both(env):
    conn, _row = env
    card, sender = waiting(env, dict(WANT, qty=2), dict(WANT, qty=2)), Sender()
    run(conn, sender, [tire(code="NOPRICE", stock=8), s2(stock=3)])
    assert sender.sent == [] and "не хватает на все строки" in tasks(conn, card)[0]["body"]
