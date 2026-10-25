"""The tails of «send-retry» (tails5, lane chats-tails, 2026-10-25): a press on an interrupted send looks at the chat from its first start
(orig_at); the line of «Входящие» a caller wrote on a 402 or an unknown send is closed once the sync settled it as sent; a skipped task or
a closed order whose send cannot be checked is not «напишите клиенту вручную»; after «Отправить снова» the texts say a person sent it again."""
from __future__ import annotations

import pytest

from test_chat_send_refusal import Refusing
from test_send_settle import NOW_TS, at, out, refused402, row, settle
from test_send_settle2 import make_task, press_task, task_token, to_stage
from test_stuck_sending import NOW, env, journal  # noqa: F401  (env is a fixture)
from tires_sync import chats, order_engine as engine, order_manager, orders


def events(conn, card):
    return [(e["text"], e["level"]) for e in orders.list_events(conn, card)]


def lines(conn):
    return [dict(zip(("message_id", "status", "answer", "text"), r))
            for r in conn.execute("SELECT message_id, status, answer, text FROM manager_message ORDER BY id")]


# --- 1: a press on an interrupted send of the sync looks from the first start ----------------------------------------------------------

def unknown_then_taken_by_the_sync(conn, chat_row, sender):
    """«Добрый день» unknown at NOW (orig_at NOW); the sync took it at 301 s to send it again and was killed (`at` moved, orig_at not)."""
    with pytest.raises(chats.ChatSendUnknown):
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")
    conn.execute("UPDATE chat_send_log SET status = 'sending', at = ?, next_at = ?, again_at = ? WHERE token = 'tok-1'", (at(301), at(301), at(301)))
    conn.commit()
    assert conn.execute("SELECT at, orig_at FROM chat_send_log WHERE token = 'tok-1'").fetchone() == (at(301), NOW)


def test_a_press_on_a_cut_send_again_finds_the_first_message_shown_late_and_sends_nothing(env):
    conn, chat_row, card = env
    sender = Refusing(TimeoutError())
    unknown_then_taken_by_the_sync(conn, chat_row, sender)
    sender.history = [out("m-first", "Добрый день", created=NOW_TS + 30)]                     # stamped before the send again
    assert chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", at(1000), "owner") is None
    assert sender.sent == [] and row(conn)["status"] == "sent" and row(conn)["message_id"] == "m-first"


def test_a_press_on_a_cut_send_again_with_another_message_since_the_first_start_is_unknown(env):
    conn, chat_row, card = env
    sender = Refusing(TimeoutError())
    unknown_then_taken_by_the_sync(conn, chat_row, sender)
    sender.history = [out("m-other", "Другое", created=NOW_TS + 100)]                         # between the first start and the send again
    with pytest.raises(chats.ChatSendUnknown):
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", at(1000), "owner")
    assert sender.sent == [] and row(conn)["status"] == "unknown"


# --- 2: the line of «Входящие» of a caller is closed once the sync sent it -------------------------------------------------------------

def arrived(conn, card, sender, monkeypatch, when=NOW):
    monkeypatch.setattr(order_manager, "_arrived_body", lambda conn: "Можно забирать")
    sent, note = order_manager._tell_arrived(conn, card, lambda c, t, k: chats.send_reply(conn, sender, c, t, k, when, "система"), when, "система")
    assert not sent
    return conn.execute("SELECT token FROM chat_send_log WHERE token LIKE 'arrived-%'").fetchone()[0]


def test_the_arrived_line_of_a_402_is_closed_when_the_sync_sent_it(env, monkeypatch):
    conn, chat_row, card = env
    sender = Refusing(refused402())
    token = arrived(conn, card, sender, monkeypatch)
    [line] = lines(conn)
    assert line["status"] == "new" and line["message_id"].startswith(token + "-") and "повторю сам" in line["text"]
    assert order_manager.new_count(conn) == 1
    assert settle(conn, sender, at(61)) == {"sent": 1} and sender.sent == ["Можно забирать"]
    [line] = lines(conn)
    assert line["status"] == "answered" and line["answer"] == ("Итог: сообщение клиенту дошло (повтор после отказа Авито, код 402). "
                                                               "Делать ничего не нужно.")
    assert order_manager.new_count(conn) == 0


def test_the_arrived_line_of_a_402_is_closed_when_the_sync_finds_it_in_the_chat(env, monkeypatch):
    conn, chat_row, card = env
    sender = Refusing(refused402())
    arrived(conn, card, sender, monkeypatch)
    sender.history = [out("m-there", "Можно забирать")]
    assert settle(conn, sender, at(61)) == {"sent": 1} and sender.sent == []
    [line] = lines(conn)
    assert line["status"] == "answered" and line["answer"] == "Итог: сообщение клиенту дошло (найдено в чате Авито). Делать ничего не нужно."


def test_the_arrived_line_of_an_unknown_send_is_closed_when_the_sync_finds_it(env, monkeypatch):
    conn, chat_row, card = env
    sender = Refusing(TimeoutError())
    arrived(conn, card, sender, monkeypatch)
    sender.history = [out("m-there", "Можно забирать")]
    assert settle(conn, sender, at(61)) == {"sent": 1} and sender.sent == []
    [line] = lines(conn)
    assert line["status"] == "answered" and "найдено в чате Авито" in line["answer"]


def test_the_arrived_line_of_an_unknown_send_is_closed_after_the_send_again_of_the_sync(env, monkeypatch):
    conn, chat_row, card = env
    sender = Refusing(TimeoutError())
    arrived(conn, card, sender, monkeypatch)
    settle(conn, sender, at(61))
    assert settle(conn, sender, at(301)) == {"resent": 1} and sender.sent == ["Можно забирать"]
    [line] = lines(conn)
    assert line["status"] == "answered" and "повторно автоматически" in line["answer"]


def test_the_arrived_line_stays_when_the_402s_end_in_not_sent(env, monkeypatch):
    conn, chat_row, card = env
    sender = Refusing(*[refused402() for _ in range(4)])
    arrived(conn, card, sender, monkeypatch)
    for seconds, outcome in ((61, "retry"), (122, "retry"), (183, "failed")):
        assert settle(conn, sender, at(seconds)) == {outcome: 1}
    first, alert = lines(conn)
    assert first["status"] == "new" and first["answer"] == "" and alert["status"] == "new" and alert["message_id"].startswith("send-")


def test_a_line_a_person_answered_and_the_line_of_another_order_are_not_touched(env, monkeypatch):
    conn, chat_row, card = env
    sender = Refusing(refused402())
    token = arrived(conn, card, sender, monkeypatch)
    conn.execute("INSERT INTO manager_message (message_id, at, point_id, sender, text, status, answer, answered_at) "
                 "VALUES (?, ?, NULL, 'система', 'раньше', 'answered', 'Написал сам', ?)", (token + "-20261013110000", NOW, NOW))
    conn.execute("INSERT INTO manager_message (message_id, at, point_id, sender, text, status) VALUES (?, ?, NULL, 'система', 'чужой', 'new')",
                 ("arrived-%d-20261013100000-20261013120000" % (card + 1), NOW))
    conn.execute("INSERT INTO manager_message (message_id, at, point_id, sender, text, status) VALUES (?, ?, NULL, 'система', 'своя тревога', 'new')",
                 ("send-%s-20261013120000" % token, NOW))
    conn.commit()
    assert settle(conn, sender, at(61)) == {"sent": 1}
    shown = {line["text"]: (line["status"], line["answer"]) for line in lines(conn)}
    assert shown["раньше"] == ("answered", "Написал сам") and shown["чужой"] == ("new", "") and shown["своя тревога"] == ("new", "")


def test_the_reserve_line_of_a_402_is_closed_when_the_sync_sent_it(tmp_path):
    from test_order_reserve import card_at, chat_of, db, ok
    from test_stuck_sending import Sender  # noqa: F401
    from tires_sync import order_reserve as reserve
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    chat_row = chat_of(conn, cid)
    account = conn.execute("SELECT account_id FROM avito_chat WHERE id = ?", (chat_row,)).fetchone()[0]
    sender = Refusing(refused402())
    when = "2026-10-18T12:00:00"
    reserve.check(conn, when, ok, lambda c, t, k: chats.send_reply(conn, sender, c, t, k, when, "система"))
    [token] = [r[0] for r in conn.execute("SELECT token FROM chat_send_log WHERE token LIKE 'reserve-%'")]
    [line] = [r for r in lines(conn) if r["message_id"] == token]
    assert line["status"] == "new"
    assert chats.settle_sends(conn, sender, lambda: sender, account, "2026-10-18T12:01:01") == {"sent": 1}
    [line] = [r for r in lines(conn) if r["message_id"] == token]
    assert line["status"] == "answered" and "повтор после отказа Авито" in line["answer"]


def test_closing_the_line_never_breaks_the_send(env, monkeypatch):
    conn, chat_row, card = env
    sender = Refusing(refused402())
    arrived(conn, card, sender, monkeypatch)
    conn.execute("ALTER TABLE manager_message RENAME TO manager_message_gone")
    conn.commit()
    assert settle(conn, sender, at(61)) == {"sent": 1} and row(conn, conn.execute(
        "SELECT token FROM chat_send_log WHERE token LIKE 'arrived-%'").fetchone()[0])["status"] == "sent"


# --- 3: a skipped task or a closed order whose send cannot be checked: no «напишите вручную» -------------------------------------------

def test_an_unknown_send_of_a_skipped_task_that_cannot_be_checked_does_not_ask_to_write_by_hand(env):
    conn, chat_row, card = env
    task_id = make_task(conn, card)
    sender = Refusing(TimeoutError())
    press_task(conn, task_id, sender, chats.ChatSendUnknown)
    engine.skip_task(conn, task_id, at(20), "owner")
    conn.execute("UPDATE avito_chat SET history_ok = 0 WHERE id = ?", (chat_row,))
    conn.commit()
    assert settle(conn, sender, at(61)) == {"unknown": 1} and sender.sent == []
    assert row(conn, task_token(task_id))["verdict"] == chats.UNREADABLE
    [said] = [e for e in events(conn, card) if "проверить нельзя" in e[0]]
    assert said == ("Сообщение клиенту не подтверждено, проверить нельзя: %s. Задача пропущена — писать клиенту не нужно." % chats.UNREADABLE,
                    "yellow")
    assert not any("вручную" in text for text, level in events(conn, card) if "проверить нельзя" in text)


def test_an_unknown_send_of_a_task_of_a_cancelled_order_with_other_messages_does_not_ask_to_write_by_hand(env):
    conn, chat_row, card = env
    task_id = make_task(conn, card)
    sender = Refusing(TimeoutError())
    press_task(conn, task_id, sender, chats.ChatSendUnknown)
    to_stage(conn, card, "cancel")
    sender.history = [out("m-other", "Другое", created=NOW_TS + 30)]
    assert settle(conn, sender, at(61)) == {"unknown": 1} and sender.sent == []
    [said] = [e for e in events(conn, card) if "проверить нельзя" in e[0]]
    assert said[1] == "yellow" and "Заказ в «Отмена» — писать клиенту не нужно." in said[0] and "вручную" not in said[0]


def test_an_unknown_reply_written_in_the_chat_that_cannot_be_checked_still_asks_to_look_and_write(env):
    conn, chat_row, card = env
    sender = Refusing(TimeoutError())
    with pytest.raises(chats.ChatSendUnknown):
        chats.send_reply(conn, sender, chat_row, "Заказ отменён, извините", "tok-1", NOW, "owner")
    to_stage(conn, card, "cancel")
    conn.execute("UPDATE avito_chat SET history_ok = 0 WHERE id = ?", (chat_row,))
    conn.commit()
    assert settle(conn, sender, at(61)) == {"unknown": 1}
    [said] = [e for e in events(conn, card) if "проверить нельзя" in e[0]]
    assert said == ("Сообщение клиенту не подтверждено, проверить нельзя: %s. Проверьте чат в кабинете Авито; если его там нет — напишите "
                    "клиенту вручную." % chats.UNREADABLE, "red")


# --- 4: after «Отправить снова» the texts say a person sent it again -----------------------------------------------------------------

def lost_without_the_send_again_of_the_sync(conn, chat_row, sender, token="tok-1", text="Добрый день", press=True):
    """Unknown at NOW, not found; sending is off for the account at the end, so the sync does not send it again: «lost» with its text."""
    if press:
        with pytest.raises(chats.ChatSendUnknown):
            chats.send_reply(conn, sender, chat_row, text, token, NOW, "owner")
    assert settle(conn, sender, at(61)) == {"wait": 1}
    conn.execute("UPDATE account SET chats_send = 0")
    conn.commit()
    assert settle(conn, sender, at(301)) == {"lost": 1} and sender.sent == []
    conn.execute("UPDATE account SET chats_send = 1")
    conn.commit()


def again_by_hand(conn, chat_row, sender, when, token="tok-1", text="Добрый день"):
    with pytest.raises(chats.ChatSendUnknown):
        chats.send_reply(conn, sender, chat_row, text, token, at(when), "owner", again=True)


def test_a_send_again_by_a_person_that_did_not_arrive_says_so_not_the_sync(env):
    conn, chat_row, card = env
    sender = Refusing(TimeoutError(), TimeoutError())
    lost_without_the_send_again_of_the_sync(conn, chat_row, sender)
    [shown] = chats.pending_sends(conn, [chat_row], at(302))
    assert shown["say"] == "Не дошло: в чате Авито его нет. Можно отправить снова."
    again_by_hand(conn, chat_row, sender, 400)
    assert settle(conn, sender, at(461)) == {"wait": 1}
    assert settle(conn, sender, at(701)) == {"lost": 1} and sender.sent == []                  # never sent again by the sync after a person
    [shown] = chats.pending_sends(conn, [chat_row], at(702))
    assert shown["say"] == "Не дошло и после повторной отправки из панели: в чате Авито его нет. Можно отправить снова." and shown["again"]
    assert ("Сообщение клиенту не дошло и после повторной отправки из панели: в чате Авито его нет. Отправьте его снова кнопкой «Отправить снова» "
            "в чате или в карточке заказа.") in journal(conn, card)
    assert not any("автоматического" in text for text in journal(conn, card))


def test_a_send_again_by_a_person_after_the_send_again_of_the_sync_says_the_person(env):
    conn, chat_row, card = env
    sender = Refusing(TimeoutError(), TimeoutError(), TimeoutError())
    with pytest.raises(chats.ChatSendUnknown):
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")
    settle(conn, sender, at(61))
    assert settle(conn, sender, at(301)) == {"unknown": 1}                                         # the sync sent it again: unknown too
    settle(conn, sender, at(362))
    assert settle(conn, sender, at(602)) == {"lost": 1}
    [shown] = chats.pending_sends(conn, [chat_row], at(603))
    assert shown["say"].startswith("Не дошло и после автоматического повтора")
    again_by_hand(conn, chat_row, sender, 800)
    settle(conn, sender, at(861))
    assert settle(conn, sender, at(1101)) == {"lost": 1}
    [shown] = chats.pending_sends(conn, [chat_row], at(1102))
    assert shown["say"].startswith("Не дошло и после повторной отправки из панели")


def test_the_sync_send_again_still_says_the_sync(env):
    conn, chat_row, card = env
    sender = Refusing(TimeoutError(), TimeoutError())
    with pytest.raises(chats.ChatSendUnknown):
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")
    settle(conn, sender, at(61))
    settle(conn, sender, at(301))
    settle(conn, sender, at(362))
    assert settle(conn, sender, at(602)) == {"lost": 1}
    assert any(text.startswith("Сообщение клиенту не дошло и после автоматического повтора") for text in journal(conn, card))


def test_a_skipped_task_sent_again_by_a_person_says_the_person(env):
    conn, chat_row, card = env
    task_id = make_task(conn, card)
    token = task_token(task_id)
    sender = Refusing(TimeoutError(), TimeoutError())
    press_task(conn, task_id, sender, chats.ChatSendUnknown)
    lost_without_the_send_again_of_the_sync(conn, chat_row, sender, token=token, text="Заказ готов", press=False)
    again_by_hand(conn, chat_row, sender, 400, token=token, text="Заказ готов")
    engine.skip_task(conn, task_id, at(420), "owner")
    settle(conn, sender, at(461))
    assert settle(conn, sender, at(701)) == {"stopped": 1} and sender.sent == []
    assert ("Сообщение клиенту не дошло (в чате Авито его нет) и после повторной отправки из панели: задача пропущена — система больше не "
            "повторяет.") in journal(conn, card)


@pytest.mark.parametrize("orig_at, again_at, said", [
    ("", "", ""), ("2026-10-13T12:00:00", "", ""),
    ("2026-10-13T12:00:00", "2026-10-13T12:05:01", " и после автоматического повтора"),              # the sync: after the first start
    ("", "2026-10-13T12:05:01", " и после автоматического повтора"),                                   # a row of before orig_at: only the sync
    ("2026-10-13T12:06:40", "2026-10-13T12:06:40", " и после повторной отправки из панели"),           # «Отправить снова»: the same moment
    ("2026-10-13T12:13:20", "2026-10-13T12:05:01", " и после повторной отправки из панели")])          # a press after the sync's send again
def test_who_sent_it_again(orig_at, again_at, said):
    assert chats._again_said(orig_at, again_at) == said
