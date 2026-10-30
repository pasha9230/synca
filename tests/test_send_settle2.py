"""The follow-ups of the review of «send-retry» (tails3, 2026-10-25): the chat is looked at from the first start of a send (orig_at, M1);
another send counts by its first start (L1); a send of the sync stops when its task was skipped or its order closed (L4); a task whose message
the sync delivered is done; the callers say a 402 in its own words (L3, L5); the panel warns that a text stops the retry of its photo (L2)."""
from __future__ import annotations

import pytest

from test_chat_send_refusal import Refusing
from test_send_settle import NOW_TS, at, inbox, out, refused402, row, settle
from test_stuck_sending import NOW, Sender, env, journal  # noqa: F401  (env is a fixture)
from tires_sync import chats, order_engine as engine, order_manager, order_templates as tpl, orders


def lost_after_send_again(conn, chat_row):
    """«Добрый день» unknown at NOW, not found, sent again by the sync at 301 s — unknown too."""
    sender = Refusing(TimeoutError(), TimeoutError())
    with pytest.raises(chats.ChatSendUnknown):
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")
    assert settle(conn, sender, at(61)) == {"wait": 1}
    assert settle(conn, sender, at(301)) == {"unknown": 1}
    assert conn.execute("SELECT at, orig_at FROM chat_send_log WHERE token = 'tok-1'").fetchone() == (at(301), NOW)   # `at` moved, orig_at not
    return sender


# --- M1: from the first start --------------------------------------------------------------------------------------------------------

def test_the_first_message_shown_late_after_the_send_again_is_found_and_nothing_more_goes(env):
    conn, chat_row, card = env
    sender = lost_after_send_again(conn, chat_row)
    sender.history = [out("m-first", "Добрый день", created=NOW_TS + 30)]                     # Avito shows the first one now, with its own time
    assert settle(conn, sender, at(362)) == {"sent": 1}
    assert row(conn)["status"] == "sent" and row(conn)["message_id"] == "m-first" and sender.sent == []
    assert chats.pending_sends(conn, [chat_row], at(363)) == [] and settle(conn, sender, at(2000)) == {}


def test_the_button_finds_the_first_message_shown_late_and_sends_no_third_copy(env):
    conn, chat_row, card = env
    sender = lost_after_send_again(conn, chat_row)
    settle(conn, sender, at(362))
    assert settle(conn, sender, at(601)) == {"lost": 1}
    sender.history = [out("m-first", "Добрый день", created=NOW_TS + 30)]
    assert chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", at(800), "owner", again=True) == "found"
    assert sender.sent == [] and row(conn)["status"] == "sent" and row(conn)["message_id"] == "m-first"


def test_another_message_of_ours_stamped_before_the_send_again_leaves_it_unknown_without_a_button(env):
    conn, chat_row, card = env
    sender = lost_after_send_again(conn, chat_row)
    sender.history = [out("m-other", "Другое", created=NOW_TS + 100)]                           # written in Avito's app, shown late
    assert settle(conn, sender, at(362)) == {"unknown": 1} and row(conn)["verdict"] == chats.OTHERS
    [shown] = chats.pending_sends(conn, [chat_row], at(363))
    assert not shown["again"] and sender.sent == []


def test_a_send_of_the_panel_keeps_its_first_start(env):
    conn, chat_row, card = env
    sender = Refusing(refused402())
    with pytest.raises(chats.ChatSendRetry):
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")
    assert settle(conn, sender, at(61)) == {"sent": 1}
    assert conn.execute("SELECT orig_at FROM chat_send_log WHERE token = 'tok-1'").fetchone()[0] == NOW


# --- L1: another send by its first start -----------------------------------------------------------------------------------------------

def test_a_retelling_sent_again_by_the_sync_still_stops_the_older_send(env):
    """L1: «hand-1» was started after «tok-1»; that the sync sent it again later (again_at, `at` moved) does not hide it."""
    conn, chat_row, card = env
    sender = Refusing(TimeoutError(), TimeoutError())
    with pytest.raises(chats.ChatSendUnknown):
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")
    with pytest.raises(chats.ChatSendUnknown):
        chats.send_reply(conn, sender, chat_row, "Татьяна, заказ готов", "hand-1", at(30), "owner")
    conn.execute("UPDATE chat_send_log SET at = ?, again_at = ?, next_at = ? WHERE token = 'hand-1'", (at(40), at(40), at(5000)))
    conn.commit()
    assert settle(conn, sender, at(61)) == {"unknown": 1} and row(conn)["verdict"] == chats.OTHERS and sender.sent == []


def test_a_send_again_of_an_older_send_does_not_stop_a_newer_one(env):
    conn, chat_row, card = env
    conn.execute("INSERT INTO chat_send_log (token, chat_row, at, author, status, orig_at, again_at) VALUES ('old-1', ?, ?, 'o', 'sent', ?, ?)",
                 (chat_row, at(30), at(-600), at(30)))                                         # started before, sent again after: not «another»
    conn.execute("INSERT INTO chat_send_log (token, chat_row, at, author, status, again_at) VALUES ('old-2', ?, ?, 'o', 'sent', ?)",
                 (chat_row, at(30), at(30)))                                                   # a row of before orig_at: as it was
    conn.commit()
    sender = Refusing(refused402())
    with pytest.raises(chats.ChatSendRetry):
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")
    assert settle(conn, sender, at(61)) == {"sent": 1} and sender.sent == ["Добрый день"]


# --- L4: the task skipped, the order closed --------------------------------------------------------------------------------------------

def make_task(conn, card, body="Заказ готов"):
    template = tpl.add_template(conn, "Клиенту: готов", "client", body)
    cur = conn.execute("INSERT INTO order_task (order_id, rule_id, entered_at, kind, arg, title, body, status, created_at) "
                       "VALUES (?, 0, ?, 'message', ?, 'Клиенту: готов', ?, 'pending', ?)", (card, NOW, str(template), body, NOW))
    conn.commit()
    return cur.lastrowid


def task_token(task_id):
    return "task-%d-%s" % (task_id, "".join(ch for ch in NOW if ch.isdigit()))


def press_task(conn, task_id, sender, error):
    with pytest.raises(error):
        engine.send_task(conn, task_id, "Заказ готов", lambda c, t, k: chats.send_reply(conn, sender, c, t, k, NOW, "owner"), NOW, "owner")
    assert engine.get_task(conn, task_id)["status"] == "pending"


def to_stage(conn, card, code):
    conn.execute("UPDATE order_card SET stage_id = (SELECT id FROM order_stage WHERE code = ?) WHERE id = ?", (code, card))
    conn.commit()


def test_a_402_of_a_skipped_task_is_not_sent_again(env):
    conn, chat_row, card = env
    task_id = make_task(conn, card)
    sender = Refusing(refused402())
    press_task(conn, task_id, sender, chats.ChatSendRetry)
    engine.skip_task(conn, task_id, at(20), "owner")
    assert settle(conn, sender, at(61)) == {"stopped": 1} and sender.sent == []
    mark = row(conn, task_token(task_id))
    assert mark["status"] == "failed" and mark["text"] == "" and mark["next_at"] == "" and mark["error"] == "остановлено: задача пропущена"
    assert "Сообщение клиенту не отправлено повторно: задача пропущена — система больше не повторяет." in journal(conn, card)
    assert inbox(conn) == [] and chats.pending_sends(conn, [chat_row], at(62)) == [] and settle(conn, sender, at(500)) == {}


def test_a_402_of_a_task_ticked_off_by_hand_is_not_sent_again(env):
    conn, chat_row, card = env
    task_id = make_task(conn, card)
    sender = Refusing(refused402())
    press_task(conn, task_id, sender, chats.ChatSendRetry)
    assert engine.run_task(conn, task_id, at(20), "owner") == "done"                          # «отправлено вручную»
    assert settle(conn, sender, at(61)) == {"stopped": 1} and sender.sent == []
    assert row(conn, task_token(task_id))["error"] == "остановлено: задача уже закрыта"


def test_a_402_of_a_task_that_is_gone_is_not_sent_again(env):
    conn, chat_row, card = env
    task_id = make_task(conn, card)
    sender = Refusing(refused402())
    press_task(conn, task_id, sender, chats.ChatSendRetry)
    conn.execute("DELETE FROM order_task WHERE id = ?", (task_id,))
    conn.commit()
    assert settle(conn, sender, at(61)) == {"stopped": 1} and sender.sent == []


@pytest.mark.parametrize("code, name", [("cancel", "Отмена / невыкуп"), ("return", "Возврат")])
def test_a_402_of_a_task_of_a_closed_order_is_not_sent_again(env, code, name):
    conn, chat_row, card = env
    task_id = make_task(conn, card)
    sender = Refusing(refused402())
    press_task(conn, task_id, sender, chats.ChatSendRetry)
    to_stage(conn, card, code)
    assert settle(conn, sender, at(61)) == {"stopped": 1} and sender.sent == []
    assert row(conn, task_token(task_id))["error"] == "остановлено: заказ в «%s»" % name


def test_a_402_of_a_task_of_a_done_order_still_goes_and_closes_the_task(env):
    conn, chat_row, card = env
    task_id = make_task(conn, card)
    sender = Refusing(refused402())
    press_task(conn, task_id, sender, chats.ChatSendRetry)
    to_stage(conn, card, "done")
    assert settle(conn, sender, at(61)) == {"sent": 1} and sender.sent == ["Заказ готов"]
    task = engine.get_task(conn, task_id)
    assert task["status"] == "done" and task["result"] == "отправлено клиенту системой (повтор после отказа Авито, код 402)"
    with pytest.raises(orders.OrderError):
        engine.send_task(conn, task_id, "Заказ готов", lambda *a: pytest.fail("sent twice"), at(62), "owner")


def test_the_reserve_message_of_a_cancelled_order_is_not_sent_again(env):
    conn, chat_row, card = env
    sender = Refusing(refused402())
    token = "reserve-%d-20261013100000" % card
    with pytest.raises(chats.ChatSendRetry):
        chats.send_reply(conn, sender, chat_row, "Срок резерва истекает", token, NOW, "система")
    to_stage(conn, card, "cancel")
    assert settle(conn, sender, at(61)) == {"stopped": 1} and sender.sent == []


def test_a_reply_written_in_the_chat_of_a_cancelled_order_still_goes(env):
    """The stage never stops what a person wrote in the chat: it may be about the cancellation itself."""
    conn, chat_row, card = env
    sender = Refusing(refused402())
    with pytest.raises(chats.ChatSendRetry):
        chats.send_reply(conn, sender, chat_row, "Заказ отменён, извините", "tok-1", NOW, "owner")
    to_stage(conn, card, "cancel")
    assert settle(conn, sender, at(61)) == {"sent": 1} and sender.sent == ["Заказ отменён, извините"]


def test_an_unknown_send_of_a_skipped_task_is_not_sent_again_by_itself(env):
    conn, chat_row, card = env
    task_id = make_task(conn, card)
    sender = Refusing(TimeoutError())
    press_task(conn, task_id, sender, chats.ChatSendUnknown)
    engine.skip_task(conn, task_id, at(20), "owner")
    assert settle(conn, sender, at(61)) == {"wait": 1}
    assert settle(conn, sender, at(301)) == {"stopped": 1} and sender.sent == []
    assert row(conn, task_token(task_id))["status"] == "failed"
    assert ("Сообщение клиенту не дошло (в чате Авито его нет) и не отправлено повторно: задача пропущена — система больше не повторяет."
            in journal(conn, card))
    assert chats.pending_sends(conn, [chat_row], at(302)) == [] and inbox(conn) == []


# --- the task is done once the sync delivered its message ----------------------------------------------------------------------------------

def test_the_task_is_done_after_the_send_again_of_the_sync(env):
    conn, chat_row, card = env
    task_id = make_task(conn, card)
    sender = Refusing(TimeoutError())
    press_task(conn, task_id, sender, chats.ChatSendUnknown)
    settle(conn, sender, at(61))
    assert settle(conn, sender, at(301)) == {"resent": 1} and sender.sent == ["Заказ готов"]
    task = engine.get_task(conn, task_id)
    assert task["status"] == "done" and task["result"] == "отправлено клиенту системой (повторно автоматически)"
    assert any(line.startswith("Клиенту: готов: выполнено (отправлено клиенту системой") for line in journal(conn, card))


def test_the_task_is_done_when_the_sync_finds_its_message_in_the_chat(env):
    conn, chat_row, card = env
    task_id = make_task(conn, card)
    sender = Refusing(TimeoutError())
    press_task(conn, task_id, sender, chats.ChatSendUnknown)
    sender.history = [out("m-there", "Заказ готов")]
    assert settle(conn, sender, at(61)) == {"sent": 1} and sender.sent == []
    assert engine.get_task(conn, task_id)["result"] == "отправлено клиенту системой (подтверждено: дошло)"


def test_closing_the_task_never_breaks_the_send(env, monkeypatch):
    conn, chat_row, card = env
    task_id = make_task(conn, card)
    sender = Refusing(refused402())
    press_task(conn, task_id, sender, chats.ChatSendRetry)

    def broken(*a, **k):
        raise RuntimeError("the base is busy")
    monkeypatch.setattr(engine, "close_after_send", broken)
    assert settle(conn, sender, at(61)) == {"sent": 1} and row(conn, task_token(task_id))["status"] == "sent"
    assert engine.get_task(conn, task_id)["status"] == "pending"                              # its «Отправить» says «отправлено раньше»


def test_a_token_of_another_task_or_of_a_changed_task_is_not_its_task(env):
    conn, chat_row, card = env
    task_id = make_task(conn, card)
    assert engine.task_of_token(conn, task_token(task_id))["id"] == task_id
    assert engine.task_of_token(conn, "task-%d-20991231000000" % task_id) is None
    assert engine.task_of_token(conn, "tok-1") is None and engine.task_of_token(conn, "task-x-1") is None


# --- L3, L5: the callers say a 402 in its own words -------------------------------------------------------------------------------------

BAD = ("Проверьте чат", "повторно не отправляйте", "вручную")


def test_the_arrived_message_with_a_402_says_only_that_the_system_repeats_it(env, monkeypatch):
    conn, chat_row, card = env
    monkeypatch.setattr(order_manager, "_arrived_body", lambda conn: "Можно забирать")      # nothing lacking in the text
    sender = Refusing(refused402())
    sent, note = order_manager._tell_arrived(conn, card, lambda c, t, k: chats.send_reply(conn, sender, c, t, k, NOW, "система"), NOW, "система")
    assert not sent
    [line] = inbox(conn)
    assert "повторю сам через минуту" in line and not any(bad in line for bad in BAD), line


def test_the_arrived_message_of_unknown_outcome_still_asks_to_look(env, monkeypatch):
    conn, chat_row, card = env
    monkeypatch.setattr(order_manager, "_arrived_body", lambda conn: "Можно забирать")      # nothing lacking in the text
    sender = Refusing(TimeoutError())
    order_manager._tell_arrived(conn, card, lambda c, t, k: chats.send_reply(conn, sender, c, t, k, NOW, "система"), NOW, "система")
    [line] = inbox(conn)
    assert "Проверьте чат Авито заказа" in line


@pytest.mark.parametrize("mark, said", [("retry", "Авито отказал (402), система повторит сама."),
                                        ("lost", "не дошло: отправьте его снова кнопкой «Отправить снова» в чате.")])
def test_a_crash_after_a_402_or_a_lost_send_is_never_send_it_by_hand(env, mark, said):
    conn, chat_row, card = env
    conn.execute("INSERT INTO chat_send_log (token, chat_row, at, author, status, text) VALUES ('arrived-y', ?, ?, 'система', ?, 'Можно забирать')",
                 (chat_row, NOW, mark))
    conn.commit()

    def crash(chat, text, token):
        raise RuntimeError("boom")
    assert order_manager.send_to_client(conn, orders.get_card(conn, card), "Можно забирать", "arrived-y", crash, NOW) == ("unknown", said, "")


def test_the_reserve_message_with_a_402_says_only_that_the_system_repeats_it(tmp_path):
    from test_order_reserve import card_at, chat_of, db, told
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    chat_of(conn, cid)

    def deliver(chat_row, text, token):
        conn.execute("INSERT INTO chat_send_log (token, chat_row, at, author, status, text) VALUES (?, ?, ?, 'система', 'retry', ?)",
                     (token, chat_row, "2026-10-18T12:00:00", text))
        conn.commit()
        raise chats.ChatSendRetry("Авито отказал (402), повторю сам через минуту (попытка 1 из 3).")
    told(conn, "2026-10-18T12:00:00", deliver)
    [event] = [e for e in orders.list_events(conn, cid) if e["kind"] == "reserve" and "Клиенту: срок резерва" in e["text"]]
    assert "повторю сам через минуту" in event["text"] and event["level"] == "yellow" and not any(bad in event["text"] for bad in BAD)
    [line] = [r[0] for r in conn.execute("SELECT text FROM manager_message")]
    assert "повторю сам через минуту" in line and not any(bad in line for bad in BAD) and ".." not in line


def test_the_placed_message_with_a_402_says_only_that_the_system_repeats_it(tmp_path):
    from test_order_placed import Deliver, placed, ready
    from test_webmim_create import FakeWebmim, db, place
    conn = db(tmp_path)
    mine = ready(conn)
    place(conn, mine, FakeWebmim())
    [line] = placed(conn, mine, Deliver(fail=chats.ChatSendRetry("Авито отказал (402), повторю сам через минуту (попытка 1 из 3).")))
    assert line.endswith("повторю сам через минуту (попытка 1 из 3).") and not any(bad in line for bad in BAD), line


# --- L2: the panel ----------------------------------------------------------------------------------------------------------------------

def test_a_402_of_a_photo_sent_with_a_text_warns_that_the_text_stops_its_retry(tmp_path, monkeypatch):
    from test_chats_pages import _photo, _sendable_page
    from test_chats_send import _press
    app, client, tok, conn, tanya = _sendable_page(tmp_path)

    def refused(*a, **k):
        raise chats.ChatSendRetry("Авито отказал (402), повторю сам через минуту (попытка 1 из 3).")
    monkeypatch.setattr(chats, "send_image", refused)
    _press(client, tanya, tok, text="Вот фото", photo=_photo((80, 60)))
    page = client.get("/chats/%d" % tanya["id"]).get_data(as_text=True)
    assert "Текст не отправлен, он остался в поле." in page and "повтор фото отменится" in page


# --- the review of this lane ------------------------------------------------------------------------------------------------------------

def test_a_task_of_the_closing_stage_itself_still_goes_after_its_402(env):
    """Finding 1: «Клиенту: заказ отменён» made by a rule of «Отмена» is what the person sent: the stage does not stop it."""
    conn, chat_row, card = env
    to_stage(conn, card, "cancel")
    task_id = make_task(conn, card)
    stage_id, entered = conn.execute("SELECT stage_id, entered_at FROM order_card WHERE id = ?", (card,)).fetchone()
    conn.execute("UPDATE order_task SET stage_id = ?, entered_at = ? WHERE id = ?", (stage_id, entered, task_id))
    conn.commit()
    sender = Refusing(refused402())
    press_task(conn, task_id, sender, chats.ChatSendRetry)
    assert settle(conn, sender, at(61)) == {"sent": 1} and sender.sent == ["Заказ готов"]
    assert engine.get_task(conn, task_id)["status"] == "done"


def test_a_press_again_of_a_failed_send_starts_anew(env):
    """Finding 2: a press on a failed send an hour later is a new start: an old message of ours from before it is not «another message»."""
    from test_chat_send_refusal import refusal
    conn, chat_row, card = env
    sender = Refusing(refusal(400, {"error": {"message": "нет"}}), refused402())
    with pytest.raises(chats.ChatError):
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")
    assert row(conn)["status"] == "failed"
    with pytest.raises(chats.ChatSendRetry):
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", at(3600), "owner")
    assert conn.execute("SELECT orig_at FROM chat_send_log WHERE token = 'tok-1'").fetchone()[0] == at(3600)
    sender.history = [out("m-old", "Здравствуйте, Татьяна", created=NOW_TS + 30)]               # written in Avito's app before the press
    assert settle(conn, sender, at(3661)) == {"sent": 1} and sender.sent == ["Добрый день"]


def test_the_button_on_a_lost_send_looks_from_the_first_start_then_starts_anew(env):
    conn, chat_row, card = env
    conn.execute("INSERT INTO chat_send_log (token, chat_row, at, author, status, text, orig_at) VALUES ('tok-1', ?, ?, 'o', 'lost', 'Добрый день', ?)",
                 (chat_row, at(301), NOW))
    conn.commit()
    sender = Refusing(refused402())
    sender.history = [out("m-first", "Добрый день", created=NOW_TS + 30)]
    assert chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", at(86400), "owner", again=True) == "found" and sender.sent == []
    conn.execute("UPDATE chat_send_log SET status = 'lost', text = 'Добрый день' WHERE token = 'tok-1'")
    conn.commit()
    sender.history = []
    with pytest.raises(chats.ChatSendRetry):
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", at(86400), "owner", again=True)
    assert conn.execute("SELECT orig_at FROM chat_send_log WHERE token = 'tok-1'").fetchone()[0] == at(86400)


def test_after_a_press_of_the_button_the_sync_never_sends_it_by_itself(env):
    """Re-review: the sync's send again could not go (no cabinet id: again_at stays ''), the person presses «Отправить снова», unknown again:
    the checks look from the press, so the sync must not send a third copy by itself — «lost» and the button again."""
    conn, chat_row, card = env
    sender = Refusing(TimeoutError(), TimeoutError())
    with pytest.raises(chats.ChatSendUnknown):
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")
    settle(conn, sender, at(61))

    def broken():
        raise RuntimeError("no keys")
    assert chats.settle_sends(conn, sender, broken, 1, at(301)) == {"lost": 1}
    assert conn.execute("SELECT again_at FROM chat_send_log WHERE token = 'tok-1'").fetchone()[0] == ""
    with pytest.raises(chats.ChatSendUnknown):
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", at(400), "owner", again=True)
    assert settle(conn, sender, at(461)) == {"wait": 1}
    assert settle(conn, sender, at(701)) == {"lost": 1} and sender.sent == [] and row(conn)["status"] == "lost"


class SkippedWhileLooking(Refusing):
    """The person presses «Пропустить» while the sync reads the chat."""

    def __init__(self, conn, *errors):
        super().__init__(*errors)
        self.conn, self.task_id = conn, None

    def chat_messages(self, *a, **k):
        if self.task_id is not None:
            engine.skip_task(self.conn, self.task_id, at(60), "owner")
            self.task_id = None
        return super().chat_messages(*a, **k)


def test_a_skip_during_the_look_into_the_chat_still_stops_the_retry(env):
    conn, chat_row, card = env
    task_id = make_task(conn, card)
    sender = SkippedWhileLooking(conn, refused402())
    press_task(conn, task_id, sender, chats.ChatSendRetry)
    sender.task_id = task_id
    assert settle(conn, sender, at(61)) == {"stopped": 1} and sender.sent == []


def test_a_skipped_task_with_another_message_since_is_stopped_without_a_line_of_inbox(env):
    conn, chat_row, card = env
    task_id = make_task(conn, card)
    sender = Refusing(refused402())
    press_task(conn, task_id, sender, chats.ChatSendRetry)
    engine.skip_task(conn, task_id, at(20), "owner")
    sender.history = [out("m-hand", "Татьяна, заказ готов", created=NOW_TS + 20)]
    assert settle(conn, sender, at(61)) == {"stopped": 1} and inbox(conn) == []


def test_a_lost_send_of_a_skipped_task_has_no_button(env):
    conn, chat_row, card = env
    task_id = make_task(conn, card)
    sender = Refusing(TimeoutError(), TimeoutError())
    press_task(conn, task_id, sender, chats.ChatSendUnknown)
    settle(conn, sender, at(61))
    assert settle(conn, sender, at(301)) == {"unknown": 1}                                     # sent again by the sync, unknown too
    engine.skip_task(conn, task_id, at(320), "owner")
    settle(conn, sender, at(362))
    assert settle(conn, sender, at(601)) == {"stopped": 1} and chats.pending_sends(conn, [chat_row], at(602)) == []
