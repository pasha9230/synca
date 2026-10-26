"""The owner 2026-10-26 (a client got «срок резерва истекает» the day after picking the order up): on the last day of the reservation the
point is asked first; only its «на точке» tells the client; silence tells nobody but the responsible. «выдал» without a number asks the
point which order, and nothing moves without its «да» or a number. The text to the client has no prepayment."""
from __future__ import annotations

from test_order_reserve import OLD_TEXT, POINT_VK, PointVk, _deliver, card_at, chat_of, db, ok, owner_text, stage
from tires_sync import order_manager as mgr, order_reserve as reserve, order_templates as tpl, orders, vk_send

DAY = "2026-10-18"                                     # the last day of a card that came to the point on 2026-10-15


def at(hour, minute=5, day=DAY):
    return "%sT%02d:%02d:00" % (day, hour, minute)


def ready(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    chat = chat_of(conn, cid)
    return conn, cid, chat, _deliver(conn), PointVk()


def say(conn, text, deliver, when, message_id="m1", reply=None):
    """The point writes to the bot in VK."""
    return mgr.dispatch(conn, message_id, POINT_VK, text, reply, when, deliver)


def yellow(conn, cid):
    """The yellow records besides the alerts of the end of the reservation (those stay as they were)."""
    return [e["text"] for e in orders.list_events(conn, cid) if e["level"] == "yellow" and not e["text"].startswith("Оповещение о конце")]


# ---- 1. the point is asked before the client -------------------------------------------------------------------------------------

def test_the_tick_of_the_last_day_asks_the_point_once_and_writes_nothing_to_the_client(tmp_path):
    conn, cid, _chat, deliver, vk = ready(tmp_path)
    reserve.check(conn, at(9, 30), ok, deliver, vk)                                     # before the working hours: nobody
    assert vk.sent == []
    for hour in (10, 11, 15, 19):
        reserve.check(conn, at(hour), ok, deliver, vk)
    assert deliver.sent == []                                                           # the client hears nothing from the tick
    assert vk.sent == [(POINT_VK, "(бот) Заказ №5200001 (Иван, 205/55 R16) ещё на точке? Ответьте «выдал 5200001» или «на точке 5200001».")]
    assert mgr.line_number(vk.sent[0][1]) == 5200001                                     # a reply to it in VK names the order by itself
    assert any(e["text"].startswith("Точке «Бета» отправлен вопрос: «Заказ №5200001") for e in orders.list_events(conn, cid))


def test_the_point_says_here_and_the_client_hears_it_once(tmp_path):
    conn, cid, chat, deliver, vk = ready(tmp_path)
    reserve.check(conn, at(10), ok, deliver, vk)
    answer = say(conn, "на точке 5200001", deliver, at(11))
    assert answer == "Заказ №5200001 — отмечено: на точке. Клиенту отправлено напоминание о сроке хранения."
    assert [(c, t) for c, t, _ in deliver.sent] == [(chat, owner_text())]
    assert say(conn, "на точке 5200001", deliver, at(11, 30), "m2") == "Заказ №5200001 уже отмечен: на точке."
    assert say(conn, "на точке 5200001", deliver, at(11, 30), "m1") == "Это сообщение уже обработано."    # a repeat of the VK event
    reserve.check(conn, at(20), ok, deliver, vk)
    reserve.check(conn, at(12, day="2026-10-19"), ok, deliver, vk)
    assert len(deliver.sent) == 1 and len(vk.sent) == 1 and yellow(conn, cid) == []
    assert orders.get_card(conn, cid)["stage"]["code"] == "at_point"


def test_a_reply_to_the_question_with_here_is_enough(tmp_path):
    from tires_sync import vk_callback
    conn, _cid, chat, deliver, vk = ready(tmp_path)
    reserve.check(conn, at(10), ok, deliver, vk)
    assert mgr.is_command(conn, "На точке") and mgr.is_command(conn, "на точке, заказ 5200001")
    event = {"object": {"message": {"id": 7, "from_id": int(POINT_VK), "peer_id": int(POINT_VK), "text": "На точке",
                                    "reply_message": {"from_id": -42, "text": vk.sent[0][1]}}}}     # VK gives the signed question as it went
    reply = vk_callback._message(event, "42")["reply_no"]
    assert reply == 5200001
    say(conn, "На точке", deliver, at(10, 30), reply=reply)
    assert [(c, t) for c, t, _ in deliver.sent] == [(chat, owner_text())]


def test_issued_closes_the_order_and_the_client_gets_nothing(tmp_path):
    conn, cid, _chat, deliver, vk = ready(tmp_path)
    reserve.check(conn, at(10), ok, deliver, vk)
    assert say(conn, "выдал 5200001", deliver, at(10, 40)).startswith("Заказ №5200001 — отмечено: выдан")
    assert orders.get_card(conn, cid)["stage"]["code"] == "done"
    reserve.check(conn, at(20), ok, deliver, vk)
    assert say(conn, "на точке 5200001", deliver, at(19), "m2") == \
        "Заказ №5200001 сейчас на этапе «Выполнено», ничего не изменено; сообщение передано владельцу."
    assert deliver.sent == [] and yellow(conn, cid) == []


def test_no_answer_by_the_end_of_the_day_is_a_yellow_record_and_nothing_to_the_client(tmp_path):
    conn, cid, _chat, deliver, vk = ready(tmp_path)
    reserve.check(conn, at(10), ok, deliver, vk)
    reserve.check(conn, at(19, 55), ok, deliver, vk)
    assert yellow(conn, cid) == []                                                      # the day is not over yet
    reserve.check(conn, at(20), ok, deliver, vk)
    reserve.check(conn, at(21), ok, deliver, vk)
    [record] = yellow(conn, cid)
    assert record.startswith("Точка «Бета» не ответила до конца рабочего дня, ещё ли заказ на точке: клиенту «Клиенту: срок резерва истекает» "
                             "не отправлено")
    assert say(conn, "на точке 5200001", deliver, at(20, 30)).startswith("Заказ №5200001: ответ пришёл после конца рабочего дня")
    assert deliver.sent == [] and yellow(conn, cid)[0] == "Точка ответила после конца рабочего дня: заказ ещё на точке (клиенту не отправлено)" and len(yellow(conn, cid)) == 2   # the late answer is seen too


def test_silence_is_found_the_next_morning_when_no_evening_tick_ran_and_a_late_here_sends_nothing(tmp_path):
    conn, cid, _chat, deliver, vk = ready(tmp_path)
    reserve.check(conn, at(10), ok, deliver, vk)
    assert say(conn, "на точке 5200001", deliver, at(9, 0, "2026-10-19")).startswith("Заказ №5200001: ответ пришёл после конца рабочего дня")
    reserve.check(conn, at(9, 30, "2026-10-19"), ok, deliver, vk)
    assert deliver.sent == [] and yellow(conn, cid)[0] == "Точка ответила после конца рабочего дня: заказ ещё на точке (клиенту не отправлено)" and len(yellow(conn, cid)) == 2


def test_here_without_a_question_or_after_an_extension_writes_nothing_to_the_client(tmp_path):
    conn, cid, _chat, deliver, vk = ready(tmp_path)
    assert say(conn, "на точке 5200001", deliver, at(11), "m0") == ("Про заказ №5200001 бот не спрашивал, ничего не изменено; "
                                                                   "сообщение передано владельцу. Если шины приехали, напишите «приехал 5200001».")
    assert [m["dispute"] for m in mgr.list_inbox(conn)] == ["«на точке» без вопроса бота"]          # maybe «приехал»: the owner sees it
    reserve.check(conn, at(10), ok, deliver, vk)
    orders.update_card(conn, cid, {"reserve_until": "2026-10-21"}, at(10, 30), "o")      # the manager moved the date meanwhile
    assert say(conn, "на точке 5200001", deliver, at(11)) == "Заказ №5200001 — отмечено: на точке. Резерв уже продлён, клиенту не пишем."
    assert deliver.sent == []


def test_a_point_that_cannot_be_asked_is_a_yellow_record_and_the_client_gets_nothing(tmp_path):
    conn = db(tmp_path)
    deliver = _deliver(conn)
    refused = card_at(conn, "at_point", "2026-10-15T10:00:00")
    chat_of(conn, refused)
    reserve.check(conn, at(10), ok, deliver, PointVk(vk_send.VkError("ВК отказал (код 901): нельзя писать")))
    reserve.check(conn, at(11), ok, deliver, PointVk())                                 # never asked a second time
    no_key = card_at(conn, "at_point", "2026-10-16T10:00:00")
    chat_of(conn, no_key)
    reserve.check(conn, at(10, day="2026-10-19"), ok, deliver, None)
    conn.execute("UPDATE point SET vk_peer_id = ''")
    conn.commit()
    no_vk = card_at(conn, "at_point", "2026-10-17T10:00:00")
    chat_of(conn, no_vk)
    vk = PointVk()
    reserve.check(conn, at(10, day="2026-10-20"), ok, deliver, vk)
    assert deliver.sent == [] and vk.sent == []
    assert [len(yellow(conn, c)) for c in (refused, no_key, no_vk)] == [1, 1, 1]
    assert "ВК не принял вопрос: ВК отказал (код 901)" in yellow(conn, refused)[0]
    assert "ВК не настроен: нет ключа сообщества" in yellow(conn, no_key)[0]
    assert "точке «Бета» бот в ВК не пишет" in yellow(conn, no_vk)[0]
    reserve.check(conn, at(21, day="2026-10-20"), ok, deliver, vk)                      # nothing was asked: no «не ответила» on top
    assert [len(yellow(conn, c)) for c in (refused, no_key, no_vk)] == [1, 1, 1]
    assert reserve.here(conn, orders.get_card(conn, refused), "менеджер Бета", at(12), deliver)[0] == "«на точке» без вопроса бота: ничего не изменено"
    assert deliver.sent == []


def test_the_answer_and_the_evening_tick_never_both_win(tmp_path):
    conn, cid, _chat, deliver, vk = ready(tmp_path)
    reserve.check(conn, at(10), ok, deliver, vk)
    key = reserve._ask_key(orders.get_card(conn, cid))
    asked = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()[0]
    assert reserve._move(conn, key, asked, "answered|" + at(19, 59))
    assert not reserve._silent(conn, orders.get_card(conn, cid), key, asked, at(20))   # the tick read «asked» before: it loses
    assert yellow(conn, cid) == []


def test_here_is_a_command_only_with_the_order(tmp_path):
    conn = db(tmp_path)
    for text in ("на точке 5200001", "На точке №5200001", "5200001 на точке", "на точке заказ 5200001"):
        assert mgr.is_command(conn, text) and mgr._word(text) == mgr.HERE and mgr.order_number(text) == 5200001, text
    for text in ("на точке нет шин 5200001", "на точке клиент ждёт", "на точке? 5200001", "на точке не все 5200001", "на точкее 5200001",
                 "не на точке 5200001", "на точке 5200001 и 5200002"):
        assert not mgr.is_command(conn, text), text


def test_the_owner_cannot_confirm_here_from_the_inbox(tmp_path):
    """«на точке» without a number waits in «Входящие»; the owner's confirmation never sends the client the reminder."""
    import pytest
    conn, _cid, _chat, deliver, vk = ready(tmp_path)
    reserve.check(conn, at(10), ok, deliver, vk)
    say(conn, "на точке", deliver, at(10, 30))
    [line] = [m for m in mgr.list_inbox(conn) if m["dispute"]]
    with pytest.raises(orders.OrderError):
        mgr.confirm(conn, line["id"], "5200001", at(10, 40), "owner", deliver)
    assert deliver.sent == []


# ---- 2. «выдал» without a number ------------------------------------------------------------------------------------------------

def two(tmp_path):
    conn = db(tmp_path)
    return conn, card_at(conn, "at_point", "2026-10-15T10:00:00"), _deliver(conn)


def test_one_order_at_the_point_is_asked_about_and_yes_issues_it(tmp_path):
    conn, cid, deliver = two(tmp_path)
    assert say(conn, "забрали", deliver, at(12)) == "Это заказ №5200001 (205/55 R16)? Ответьте «да» или «нет»."
    assert orders.get_card(conn, cid)["stage"]["code"] == "at_point"
    assert [m["dispute"] for m in mgr.list_inbox(conn, only_new=True)] == ["нет номера заказа"]
    assert say(conn, "Да!", deliver, at(12, 20), "m2").startswith("Заказ №5200001 — отмечено: выдан")
    assert orders.get_card(conn, cid)["stage"]["code"] == "done" and deliver.sent == []
    assert mgr.list_inbox(conn, only_new=True) == []                                    # the line of «Входящие» is closed by the «да»
    assert say(conn, "Да!", deliver, at(12, 21), "m2") == ""                            # a repeat of the VK event does nothing
    say(conn, "да", deliver, at(12, 22), "m3")                                          # no question any more: a plain message
    assert [m["text"] for m in mgr.list_inbox(conn, only_new=True)] == ["да"]


def test_no_late_yes_or_another_message_between_issues_nothing(tmp_path):
    conn, cid, deliver = two(tmp_path)
    say(conn, "выдал", deliver, at(12))
    assert say(conn, "нет", deliver, at(12, 1), "m2").startswith("Понятно, ничего не изменено")
    say(conn, "да", deliver, at(12, 2), "m3")
    assert orders.get_card(conn, cid)["stage"]["code"] == "at_point"
    say(conn, "выдал", deliver, at(13), "m4")
    assert say(conn, "да", deliver, at(13, 36), "m5").startswith("Вопрос про заказ №5200001 устарел")
    say(conn, "выдал", deliver, at(14), "m6")
    say(conn, "привет, как дела", deliver, at(14, 1), "m7")                              # the question is over
    say(conn, "да", deliver, at(14, 2), "m8")
    assert orders.get_card(conn, cid)["stage"]["code"] == "at_point"


def test_yes_after_the_order_moved_issues_nothing(tmp_path):
    conn, cid, deliver = two(tmp_path)
    say(conn, "выдала", deliver, at(12))
    orders.move_card(conn, cid, stage(conn, "refused"), at(12, 5), "o")
    assert say(conn, "да", deliver, at(12, 6), "m2").startswith("Заказ №5200001 уже изменился, ничего не сделано")
    assert orders.get_card(conn, cid)["stage"]["code"] == "refused"


def test_several_orders_are_listed_and_yes_does_nothing(tmp_path):
    conn, first, deliver = two(tmp_path)
    second = card_at(conn, "moving", "2026-10-15T10:00:00")
    answer = say(conn, "Выдали", deliver, at(12))
    assert answer == "Какой заказ выдан? №5200001 — 205/55 R16, №5200002 — 205/55 R16; напишите «выдал <номер>»."
    say(conn, "да", deliver, at(12, 1), "m2")
    assert [orders.get_card(conn, c)["stage"]["code"] for c in (first, second)] == ["at_point", "moving"]


def test_no_order_to_issue_is_as_before_and_a_bare_yes_is_a_plain_message(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    orders.move_card(conn, cid, stage(conn, "done"), at(11), "o")
    assert say(conn, "выдал", _deliver(conn), at(12)).startswith("О каком заказе речь?")
    say(conn, "да", _deliver(conn), at(12, 1), "m2")
    assert [m["text"] for m in mgr.list_inbox(conn)][0] == "да"                          # no question was asked: «Входящие», as any message


# ---- 3. the text to the client ------------------------------------------------------------------------------------------------

def test_the_owners_old_text_kept_word_for_word_is_replaced_and_a_changed_one_is_kept(tmp_path):
    conn = db(tmp_path)
    old = tpl.add_template(conn, tpl.RESERVE_SAMPLE[0], "client", OLD_TEXT)
    assert mgr.template_body(conn, tpl.RESERVE_SAMPLE) == tpl.RESERVE_SAMPLE[2]         # the old sample is never sent again
    tpl.add_samples(conn)
    assert tpl.get_template(conn, old)["body"] == tpl.RESERVE_SAMPLE[2]
    tpl.update_template(conn, old, tpl.RESERVE_SAMPLE[0], "client", "Свой текст {hold_until}")
    tpl.add_samples(conn)
    assert tpl.get_template(conn, old)["body"] == "Свой текст {hold_until}" == mgr.template_body(conn, tpl.RESERVE_SAMPLE)


# ---- the review of 2026-10-26 ----------------------------------------------------------------------------------------------------

def test_yes_after_the_owner_dealt_with_the_message_issues_nothing(tmp_path):
    conn, cid, deliver = two(tmp_path)
    other = card_at(conn, "at_point", "2026-10-15T10:00:00")
    orders.move_card(conn, other, stage(conn, "refused"), at(11), "o")                 # not the point's to issue: one order is asked about
    say(conn, "выдал", deliver, at(12))
    [line] = [m for m in mgr.list_inbox(conn) if m["dispute"]]
    orders.move_card(conn, other, stage(conn, "at_point"), at(12, 1), "o")
    mgr.confirm(conn, line["id"], str(5200000 + other), at(12, 2), "owner", deliver)     # the owner says it was the other order
    assert say(conn, "да", deliver, at(12, 3), "m2").startswith("Сообщение уже разобрал владелец, ничего не изменено")
    assert [orders.get_card(conn, c)["stage"]["code"] for c in (cid, other)] == ["at_point", "done"]


def test_a_question_cut_before_it_went_never_lets_here_tell_the_client(tmp_path):
    conn, cid, _chat, deliver, vk = ready(tmp_path)
    key = reserve._ask_key(orders.get_card(conn, cid))
    conn.execute("INSERT INTO app_state (key, value) VALUES (?, ?)", (key, "asking|" + at(10)))     # the tick was cut while sending
    conn.commit()
    say(conn, "на точке 5200001", deliver, at(10, 30))
    reserve.check(conn, at(20), ok, deliver, vk)
    assert deliver.sent == [] and vk.sent == []
    assert [t.split(":")[0] for t in yellow(conn, cid)] == ["Клиенту «Клиенту"] and "неизвестно, ушёл ли вопрос" in yellow(conn, cid)[0]


def test_the_reserve_question_ends_an_open_question_about_an_issue(tmp_path):
    conn, cid, chat, deliver, vk = ready(tmp_path)
    say(conn, "выдал", deliver, at(9, 50))                                              # «Это заказ №5200001?»
    reserve.check(conn, at(10), ok, deliver, vk)                                        # «Заказ №5200001 ещё на точке?»
    say(conn, "да", deliver, at(10, 1), "m2")                                           # not an answer to either
    assert orders.get_card(conn, cid)["stage"]["code"] == "at_point" and deliver.sent == []


def test_after_an_extension_the_silence_is_no_yellow_record(tmp_path):
    conn, cid, _chat, deliver, vk = ready(tmp_path)
    reserve.check(conn, at(10), ok, deliver, vk)
    orders.update_card(conn, cid, {"reserve_until": "2026-10-21"}, at(11), "o")
    reserve.check(conn, at(20), ok, deliver, vk)
    assert yellow(conn, cid) == [] and deliver.sent == []


def test_an_order_without_the_webmim_number_tells_the_responsible_not_the_client(tmp_path):
    """The owner 2026-10-27: the reminder carries the WebMIM number; without it the client gets nothing and the responsible is told."""
    conn, cid, _chat, deliver, vk = ready(tmp_path)
    conn.execute("UPDATE order_card SET webmim_number = '' WHERE id = ?", (cid,))
    conn.commit()
    reserve.check(conn, at(10), ok, deliver, vk)
    assert vk.sent[0][1].startswith("(бот) Заказ №%d (" % cid)                          # the point knows it by the panel's number
    alerts = []
    answer = mgr.dispatch(conn, "m1", POINT_VK, "на точке №%d" % cid, None, at(11), deliver, lambda card, text: (alerts.append(text), "ok")[1])
    assert answer == "Заказ №%d — отмечено: на точке. Клиенту напоминание не отправлено: менеджер напишет ему сам." % cid
    assert deliver.sent == [] and len(alerts) == 1 and "не хватает — Номер заказа WebMIM" in alerts[0] and "Напишите клиенту вручную" in alerts[0]
    [record] = yellow(conn, cid)
    assert record.startswith("Заказ №%d: точка «Бета» подтвердила" % cid) and "Оповещение: ok" in record
    reserve.check(conn, at(20), ok, deliver, vk)
    assert say(conn, "на точке №%d" % cid, deliver, at(19), "m2") == "Заказ №%d уже отмечен: на точке." % cid
    assert deliver.sent == [] and len(alerts) == 1 and len(yellow(conn, cid)) == 1
