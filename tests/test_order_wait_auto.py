"""«Ожидает наличие» writes to the client by itself (the owner 2026-11-04): all wanted tires here in full → «появилось в наличии» in the order's
chat, once per stay in the stage; analogs and short stock never; no chat or a failed send → the task of the responsible. A fake Avito only."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from helpers import make_offer
from test_chats_sync import FakeClient, chat, db, msg
from test_stuck_sending import Sender
from tires_sync import chats, message_texts as mt, order_engine as engine, order_wait, orders
from tires_sync.models import Season

NOW = "2026-11-04T12:00:00"
NEXT = "2026-11-04T13:40:00"
WANT = dict(brand="Ikon Tyres", width=205, profile=55, radius=16, season="winter", studded="no", qty=4)
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
    return [t for t in engine.list_tasks(conn, card) if t["title"] in (order_wait.TITLE_EXACT, order_wait.TITLE_ANALOG)]


def events(conn, card, level=None):
    return [e["text"] for e in orders.list_events(conn, card) if level is None or e["level"] == level]


def test_all_here_in_full_the_client_hears_the_owners_words_and_no_task_is_made(env):
    conn, chat_row = env
    card, sender, told = waiting(env), Sender(), []
    assert run(conn, sender, [tire()], notify=lambda *a: told.append(a))["exact"] == [card]
    assert sender.sent == ["Здравствуйте. «Ikon Autograph 205/55 R16» появилось в наличии у нас на складе, стоимость: 8 500 ₽. Желаете оформить заказ?"]
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
    run(conn, sender, [tire(), tire(width=215, code="TS2", name_orig="Ikon 215/55 R16", stock=2)])
    assert sender.sent == ["Здравствуйте. «Ikon Autograph 205/55 R16, Ikon 215/55 R16» появилось в наличии у нас на складе, стоимость: 8 500 ₽ и 6 000.50 ₽. "
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
    run(conn, sender, [tire(stock=5)])
    assert sender.sent == [] and "не хватает на все строки" in tasks(conn, card)[0]["body"]
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
    run(conn, sender, [tire()])
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
    assert len(sent) == 1 and "стоимость: 12 345 ₽." in sent[0][0] and sent[0][1].startswith("wait-%d-" % card)
    assert order_offers.account_for(conn, card_id=card) == 1
    sent.clear()
    card2 = orders.create_card(conn, {"client_name": "Пётр"}, NOW, "worker2")
    order_wait.add_want(conn, card2, {"code": offer.code, "qty": 1}, NOW, "worker2")
    orders.move_card(conn, card2, stage(conn, "wait"), NOW, "worker2")
    conn.execute("INSERT INTO avito_chat (account_id, chat_id, order_id) VALUES (1, 'c2', ?)", (card2,))
    conn.commit()
    cli._check_waiting(conn, SimpleNamespace(offers=parsed.offers, issues=parsed.issues), box)                 # no reference: only the task
    assert sent == [] and tasks(conn, card2)[0]["title"] == order_wait.TITLE_EXACT
