"""The minute's sync settles the sends to clients (the owner 2026-10-25, «проверка чата и автоповтор при коде 402»): a 402 is sent again by
itself after a look into the chat, up to three times, then «не отправлено» and a line of «Входящие»; an unknown send is looked up in the chat
(1 and 5 minutes after it): there — «отправлено», nothing — «не дошло» and «Отправить снова» (a press only), unreadable — it stays unknown
with the reason. Other refusals are as before; a success clears Avito's old answer."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from test_chat_send_refusal import Refusing, picture, refusal
from test_stuck_sending import CLAIMED_TS, NOW, Sender, env, journal, status  # noqa: F401  (env is a fixture)
from tires_sync import chats, order_manager, orders

NOW_TS = int(datetime.fromisoformat(NOW).replace(tzinfo=chats.MSK).timestamp())


def at(seconds: int) -> str:
    return (datetime.fromisoformat(NOW) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def row(conn, token="tok-1"):
    names = ("status", "text", "photo", "tries", "next_at", "verdict", "error", "message_id")
    return dict(zip(names, conn.execute("SELECT %s FROM chat_send_log WHERE token = ?" % ", ".join(names), (token,)).fetchone()))


def settle(conn, sender, when, account=1):
    return chats.settle_sends(conn, sender, lambda: sender, account, when)


def out(mid, text, created=NOW_TS + 30):
    return {"id": mid, "created": created, "direction": "out", "type": "text", "content": {"text": text}}


def refused402(text="Перейдите на подписку с API мессенджера"):
    return refusal(402, {"error": {"code": 402, "message": text}})


def inbox(conn):
    return [r[0] for r in conn.execute("SELECT text FROM manager_message ORDER BY id")]


# --- 402: sent again by the sync --------------------------------------------------------------------------------------------------------

def test_a_402_is_sent_again_a_minute_later_by_the_sync_and_its_old_answer_is_cleared(env):
    conn, chat_row, card = env
    sender = Refusing(refused402())
    with pytest.raises(chats.ChatSendRetry) as raised:
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")
    assert "повторю сам через минуту (попытка 1 из 3)" in str(raised.value)
    assert row(conn)["status"] == "retry" and row(conn)["text"] == "Добрый день" and row(conn)["next_at"] == at(60)
    assert any(line.startswith("Сообщение клиенту не отправлено: Авито отказал") for line in journal(conn, card))
    assert settle(conn, sender, at(30)) == {} and sender.sent == []                              # not before its minute
    assert settle(conn, sender, at(61)) == {"sent": 1}
    assert sender.sent == ["Добрый день"]
    done = row(conn)
    assert done["status"] == "sent" and done["error"] == "" and done["text"] == "" and done["message_id"] == "m-out-1"
    assert "Отправлен ответ клиенту (с повтора: Авито до этого отказал, код 402): Добрый день" in journal(conn, card)
    assert settle(conn, sender, at(200)) == {} and sender.sent == ["Добрый день"]                # once


def test_three_more_402s_end_in_not_sent_with_a_line_of_inbox_and_the_text_to_copy(env):
    conn, chat_row, card = env
    sender = Refusing(*[refused402() for _ in range(4)])
    with pytest.raises(chats.ChatSendRetry):
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")
    assert settle(conn, sender, at(61)) == {"retry": 1} and row(conn)["tries"] == 2
    assert settle(conn, sender, at(122)) == {"retry": 1} and row(conn)["tries"] == 3
    assert settle(conn, sender, at(183)) == {"failed": 1}
    assert row(conn)["status"] == "failed" and row(conn)["text"] == "" and sender.errors == [] and sender.sent == []
    assert settle(conn, sender, at(400)) == {}                                                   # the end: nothing more by itself
    refusals = [line for line in journal(conn, card) if "402" in line]
    assert len(refusals) == 2 and any("отказал 4 раза подряд" in line for line in refusals)                     # the first refusal and the end, not every try
    [line] = inbox(conn)
    assert line.startswith("Заказ №%d: клиенту не отправлено" % card) and "Отправьте вручную" in line
    assert line.endswith(order_manager.COPY_MARK + "Добрый день")


def test_the_message_already_in_the_chat_before_a_retry_is_sent_and_not_sent_twice(env):
    conn, chat_row, card = env
    sender = Refusing(refused402())
    with pytest.raises(chats.ChatSendRetry):
        chats.send_reply(conn, sender, chat_row, "Добрый   день", "tok-1", NOW, "owner")
    sender.history = [out("m-there", "Добрый день")]
    assert settle(conn, sender, at(61)) == {"sent": 1}
    assert sender.sent == [] and row(conn)["status"] == "sent" and row(conn)["message_id"] == "m-there"
    assert any(line.startswith("Сообщение клиенту подтверждено: дошло") for line in journal(conn, card))


def test_an_unreadable_chat_does_not_stop_the_retry_of_a_402(env):
    conn, chat_row, card = env
    sender = Refusing(refused402())
    with pytest.raises(chats.ChatSendRetry):
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")
    sender.broken = refused402()                                                                 # the read is refused too: a 402 is «not taken»
    assert settle(conn, sender, at(61)) == {"sent": 1} and sender.sent == ["Добрый день"]


@pytest.mark.parametrize("code", [400, 403, 404])
def test_other_refusals_are_not_sent_again_by_themselves(env, code):
    conn, chat_row, card = env
    sender = Refusing(refusal(code, {"error": {"message": "нет"}}))
    with pytest.raises(chats.ChatError) as raised:
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")
    assert not isinstance(raised.value, chats.ChatSendUnknown) and row(conn)["status"] == "failed"
    assert settle(conn, sender, at(400)) == {} and sender.sent == []


def test_a_timeout_on_the_retry_is_unknown_and_goes_to_the_check(env):
    conn, chat_row, card = env
    sender = Refusing(refused402(), TimeoutError())
    with pytest.raises(chats.ChatSendRetry):
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")
    assert settle(conn, sender, at(61)) == {"unknown": 1}
    assert row(conn)["status"] == "unknown" and row(conn)["next_at"] == at(121) and row(conn)["text"] == "Добрый день"


def test_a_press_on_a_send_waiting_for_its_retry_sends_nothing(env):
    conn, chat_row, card = env
    sender = Refusing(refused402())
    with pytest.raises(chats.ChatSendRetry):
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")
    with pytest.raises(chats.ChatSendRetry, match="попытка 1 из 3"):
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", at(10), "owner")
    assert sender.sent == [] and row(conn)["status"] == "retry"


def test_a_402_of_a_photo_sends_the_uploaded_picture_again(env):
    conn, chat_row, card = env
    sender = Refusing(refused402())
    with pytest.raises(chats.ChatSendRetry):
        chats.send_image(conn, sender, chat_row, picture(), "img-1", NOW, "owner")
    assert row(conn, "img-1")["status"] == "retry" and row(conn, "img-1")["text"] == "img-1" and row(conn, "img-1")["photo"] == 1
    assert settle(conn, sender, at(61)) == {"sent": 1}
    assert sender.sent == ["img-1"] and row(conn, "img-1")["status"] == "sent"
    assert "Отправлено фото клиенту (с повтора: Авито до этого отказал, код 402)" in journal(conn, card)


def test_a_retry_with_the_sends_switched_off_meanwhile_ends_with_an_alert(env):
    conn, chat_row, card = env
    sender = Refusing(refused402())
    with pytest.raises(chats.ChatSendRetry):
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")
    conn.execute("UPDATE account SET chats_send = 0")
    conn.commit()
    assert settle(conn, sender, at(61)) == {"failed": 1} and sender.sent == []
    assert "отправка ответов выключена" in row(conn)["error"] and len(inbox(conn)) == 1


def test_the_first_402_of_a_message_that_goes_by_itself_says_the_system_repeats_it(env):
    conn, chat_row, card = env
    sender = Refusing(refused402())
    order = orders.get_card(conn, card)
    status_, why, copy = order_manager.send_to_client(conn, order, "Можно забирать", "arrived-x", lambda c, t, k: chats.send_reply(
        conn, sender, c, t, k, NOW, "система"), NOW)
    assert status_ == "unknown" and "повторю сам через минуту" in why and copy == ""             # never «send it by hand» meanwhile


# --- unknown: the chat is looked up --------------------------------------------------------------------------------------------------------

def unknown(conn, chat_row, text="Добрый день"):
    sender = Refusing(TimeoutError())
    with pytest.raises(chats.ChatSendUnknown) as raised:
        chats.send_reply(conn, sender, chat_row, text, "tok-1", NOW, "owner")
    assert chats.CHECKING in str(raised.value) and "Повторно не отправляйте" not in str(raised.value)
    return sender


def test_an_unknown_send_found_in_the_chat_is_sent(env):
    conn, chat_row, card = env
    sender = unknown(conn, chat_row)
    assert row(conn)["next_at"] == at(60) and settle(conn, sender, at(30)) == {}
    sender.history = [out("m-there", "Добрый день")]
    assert settle(conn, sender, at(61)) == {"sent": 1}
    assert row(conn)["status"] == "sent" and row(conn)["message_id"] == "m-there" and row(conn)["error"] == ""
    assert "Сообщение клиенту подтверждено: дошло (найдено в чате Авито)" in journal(conn, card) and sender.sent == []


def test_an_unknown_send_not_in_the_chat_after_two_looks_is_sent_again_by_itself_once(env):
    """The owner 25.10: «Отправить снова сама, один раз, и записать это в журнал заказа» — under the same mark."""
    conn, chat_row, card = env
    sender = unknown(conn, chat_row)
    assert settle(conn, sender, at(61)) == {"wait": 1} and row(conn)["next_at"] == at(300)
    assert settle(conn, sender, at(120)) == {}
    assert settle(conn, sender, at(301)) == {"resent": 1} and sender.reads == 2
    assert sender.sent == ["Добрый день"] and row(conn)["status"] == "sent" and row(conn)["error"] == ""
    assert conn.execute("SELECT again_at FROM chat_send_log WHERE token = 'tok-1'").fetchone()[0] == at(301)
    lines = journal(conn, card)
    assert "Сообщение клиенту не дошло (в чате Авито его нет) — отправляем повторно автоматически, один раз" in lines
    assert "Отправлен ответ клиенту (повторно автоматически: первая отправка не дошла): Добрый день" in lines
    assert settle(conn, sender, at(1000)) == {} and sender.sent == ["Добрый день"]


def test_a_send_again_that_is_unknown_too_is_checked_again_and_never_sent_a_third_time(env):
    # tails3 (M1): the chat is looked at from the first start of the send, not from the send again; a message of ours stamped between the
    # two is its business now (tests/test_send_settle2.py), so this test no longer puts one there
    conn, chat_row, card = env
    sender = Refusing(TimeoutError(), TimeoutError())
    with pytest.raises(chats.ChatSendUnknown):
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")
    settle(conn, sender, at(61))
    assert settle(conn, sender, at(301)) == {"unknown": 1} and row(conn)["status"] == "unknown" and row(conn)["next_at"] == at(361)
    assert settle(conn, sender, at(362)) == {"wait": 1} and row(conn)["next_at"] == at(601)
    assert settle(conn, sender, at(601)) == {"lost": 1} and sender.errors == [] and sender.sent == []
    assert row(conn)["status"] == "lost" and row(conn)["text"] == "Добрый день"
    assert any(line.startswith("Сообщение клиенту не дошло и после автоматического повтора") and "Отправить снова" in line for line in journal(conn, card))
    [shown] = chats.pending_sends(conn, [chat_row], at(602))
    assert shown["again"] and "после автоматического повтора" in shown["say"] and shown["text"] == "Добрый день"
    assert settle(conn, sender, at(2000)) == {} and sender.sent == []                           # never by itself again
    with pytest.raises(chats.ChatSendLost, match="Отправить снова"):                            # nor by another press of its token
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", at(700), "система")
    assert sender.sent == []
    chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", at(800), "owner", again=True)
    chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", at(801), "owner", again=True)   # a double press
    assert sender.sent == ["Добрый день"] and row(conn)["status"] == "sent" and chats.pending_sends(conn, [chat_row], at(802)) == []


def test_found_at_the_second_look_it_is_not_sent_again(env):
    conn, chat_row, card = env
    sender = unknown(conn, chat_row)
    settle(conn, sender, at(61))
    sender.history = [out("m-late", "Добрый день", created=NOW_TS + 200)]
    assert settle(conn, sender, at(301)) == {"sent": 1} and sender.sent == [] and row(conn)["message_id"] == "m-late"


def test_the_send_again_is_taken_once_whatever_two_runs_do(env):
    conn, chat_row, card = env
    sender = unknown(conn, chat_row)
    settle(conn, sender, at(61))
    names = ("token", "chat_row", "at", "author", "status", "text", "photo", "tries", "again_at", "first_at")
    stale = dict(zip(names, conn.execute("SELECT %s FROM chat_send_log WHERE token = 'tok-1'" % ", ".join(names)).fetchone()))
    assert settle(conn, sender, at(301)) == {"resent": 1}
    chat = chats.get_chat(conn, chat_row)
    assert chats._check_unknown(conn, sender, lambda: sender, chat, stale, at(301), lambda: at(302)) == "busy"   # the other run's old view
    assert sender.sent == ["Добрый день"]
    conn.execute("DELETE FROM avito_message WHERE direction = 'out'")                           # the chat as if that send were not there
    conn.execute("INSERT INTO chat_send_log (token, chat_row, at, author, status, text, tries, again_at) VALUES ('tok-2', ?, ?, 'o', 'unknown', 'Ещё', 1, ?)",
                 (chat_row, at(400), at(10)))
    conn.commit()
    fresh = dict(stale, token="tok-2", at=at(400), text="Ещё", tries=1, again_at="")        # a run that read the row before again_at was set
    assert chats._check_unknown(conn, sender, lambda: sender, chat, fresh, at(701), lambda: at(701)) == "busy"   # the claim checks again_at itself
    assert sender.sent == ["Добрый день"] and row(conn, "tok-2")["status"] == "unknown"
    assert settle(conn, sender, at(701)) == {"lost": 1} and sender.sent == ["Добрый день"]     # sent again once already: lost


def test_the_button_looks_at_the_chat_first(env):
    conn, chat_row, card = env
    sender = Refusing(TimeoutError(), TimeoutError())
    with pytest.raises(chats.ChatSendUnknown):
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")
    settle(conn, sender, at(61)), settle(conn, sender, at(301)), settle(conn, sender, at(362))
    assert settle(conn, sender, at(601)) == {"lost": 1}
    sender.history = [out("m-came", "Добрый день", created=NOW_TS + 700)]                         # it came late, or was written by hand
    chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", at(800), "owner", again=True)
    assert sender.sent == [] and row(conn)["status"] == "sent" and row(conn)["message_id"] == "m-came"


def test_a_lost_send_is_lost_to_the_callers_too_never_send_it_by_hand(env):
    conn, chat_row, card = env
    conn.execute("INSERT INTO chat_send_log (token, chat_row, at, author, status, text) VALUES ('arrived-x', ?, ?, 'система', 'lost', 'Можно забирать')", (chat_row, NOW))
    conn.commit()
    sender = Sender()
    status_, why, copy = order_manager.send_to_client(conn, orders.get_card(conn, card), "Можно забирать", "arrived-x", lambda c, t, k: chats.send_reply(
        conn, sender, c, t, k, NOW, "система"), NOW)
    assert status_ == "unknown" and "Отправить снова" in why and copy == "" and sender.sent == []


def test_an_unknown_send_in_a_chat_avito_does_not_show_stays_unknown_with_the_reason(env):
    conn, chat_row, card = env
    sender = unknown(conn, chat_row)
    sender.broken = refused402()
    assert settle(conn, sender, at(61)) == {"unknown": 1}
    assert row(conn)["status"] == "unknown" and row(conn)["verdict"] == chats.UNREADABLE
    assert settle(conn, sender, at(400)) == {} and sender.reads == 1                            # not read again
    with pytest.raises(chats.ChatSendUnknown, match="проверить нельзя"):
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", at(500), "owner")
    assert sender.sent == [] and "проверить нельзя" in chats.pending_sends(conn, [chat_row], at(500))[0]["say"]


def test_another_message_of_ours_since_leaves_it_unknown(env):
    conn, chat_row, card = env
    sender = unknown(conn, chat_row)
    sender.history = [out("m-other", "Другой текст")]
    assert settle(conn, sender, at(61)) == {"unknown": 1} and row(conn)["verdict"] == chats.OTHERS and sender.sent == []


def test_a_passing_trouble_at_the_look_is_tried_next_minute_and_not_counted(env):
    conn, chat_row, card = env
    sender = unknown(conn, chat_row)
    sender.broken = chats.AvitoError(429, None, "HTTP 429")
    assert settle(conn, sender, at(61)) == {"recheck": 1}
    assert row(conn)["tries"] == 0 and row(conn)["next_at"] == at(121) and row(conn)["status"] == "unknown"


def test_an_unknown_photo_is_lost_without_a_button(env):
    conn, chat_row, card = env
    sender = Refusing(TimeoutError())
    with pytest.raises(chats.ChatSendUnknown):
        chats.send_image(conn, sender, chat_row, picture(), "img-1", NOW, "owner")
    settle(conn, sender, at(61))
    assert settle(conn, sender, at(301)) == {"lost": 1} and row(conn, "img-1")["text"] == ""
    assert not chats.pending_sends(conn, [chat_row], at(302))[0]["again"]
    assert any(line.startswith("Фото клиенту не дошло") and "вручную" in line for line in journal(conn, card))


def test_an_old_unknown_send_without_its_text_is_looked_up_too_within_48_hours(env):
    """25.10 12:13, order 434: a send of before this release, no text kept: not there — «не дошло, отправьте вручную»; older than 48 hours —
    not touched."""
    conn, chat_row, card = env
    conn.execute("INSERT INTO chat_send_log (token, chat_row, at, author, status, error) VALUES ('old', ?, ?, 'система', 'unknown', 'нет ответа (TimeoutError)')",
                 (chat_row, at(-3600)))
    conn.execute("INSERT INTO chat_send_log (token, chat_row, at, author, status) VALUES ('older', ?, ?, 'система', 'unknown')", (chat_row, at(-49 * 3600)))
    conn.commit()
    sender = Sender()
    assert settle(conn, sender, NOW) == {"wait": 1}
    assert settle(conn, sender, at(61)) == {"lost": 1}
    assert row(conn, "old")["status"] == "lost" and row(conn, "older")["status"] == "unknown" and sender.sent == []
    assert any(line.startswith("Сообщение (или фото) клиенту не дошло") and "Отправьте вручную из чата" in line for line in journal(conn, card))
    shown = {s["token"]: s for s in chats.pending_sends(conn, [chat_row], at(62))}
    assert not shown["old"]["again"] and "Отправьте вручную" in shown["old"]["say"] and "не проверяет" in shown["older"]["say"]


def test_an_old_unknown_send_without_its_text_and_a_message_of_ours_since_stays_unknown(env):
    conn, chat_row, card = env
    conn.execute("INSERT INTO chat_send_log (token, chat_row, at, author, status) VALUES ('old', ?, ?, 'система', 'unknown')", (chat_row, at(-3600)))
    conn.commit()
    sender = Sender(history=[out("m-x", "Можно забирать", created=NOW_TS - 3000)])
    assert settle(conn, sender, NOW) == {"unknown": 1} and row(conn, "old")["verdict"] == chats.OTHERS


def test_the_sync_settles_only_the_sends_of_its_account(env):
    conn, chat_row, card = env
    sender = unknown(conn, chat_row)
    assert settle(conn, sender, at(61), account=2) == {} and sender.reads == 0


def test_a_success_after_a_failed_try_clears_avitos_old_answer(env):
    """25.10 12:32:59: «sent», while `error` kept the 402 of the try before it."""
    conn, chat_row, card = env
    sender = Refusing(refusal(403, {"error": {"message": "Forbidden"}}))
    with pytest.raises(chats.ChatError):
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")
    assert row(conn)["error"] == "HTTP 403: Forbidden"
    chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", at(5), "owner")
    assert row(conn)["status"] == "sent" and row(conn)["error"] == ""


# --- chats-sync -------------------------------------------------------------------------------------------------------------------------

def test_chats_sync_settles_the_sends_with_a_writer_and_its_failure_is_a_warning(tmp_path, monkeypatch, capsys):
    from http_fakes import ScriptedHttp
    from test_cli_chats import replies
    from tires_sync import cli
    tmp, base = _cli_env(tmp_path)
    calls = []

    def fake(conn, client, writer, account_id, now, clock=None):
        calls.append((account_id, writer().allow_write, client.allow_write))
        return {"sent": 1}
    monkeypatch.setattr(chats, "settle_sends", fake)
    monkeypatch.setattr(cli, "_http", lambda: ScriptedHttp(*replies()))
    monkeypatch.setattr(cli, "moscow_now", lambda: datetime(2026, 10, 13, 12, 0, 0))
    assert cli.main(["chats-sync"] + base) == 0
    assert calls == [(1, True, False)] and "отправки клиентам разобраны: sent 1" in capsys.readouterr().out

    def broken(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(chats, "settle_sends", broken)
    monkeypatch.setattr(cli, "_http", lambda: ScriptedHttp(*replies()))
    assert cli.main(["chats-sync"] + base) == 0
    assert "отправки клиентам не разобраны (RuntimeError)" in capsys.readouterr().err


def _cli_env(tmp_path):
    from tires_sync import connections, store
    from tires_sync.secretbox import create_key_file, load_box
    conn = store.connect(tmp_path / "t.db")
    orders.ensure_pipeline(conn, NOW)
    account = connections.create_account(conn, "Только сообщения", NOW, "o")
    key_file = tmp_path / "secret.key"
    create_key_file(key_file)
    box = load_box(key_file)
    connections.set_secret(conn, box, "avito", account, "client_id", "CLIENTID-12345678", NOW, "o")
    connections.set_secret(conn, box, "avito", account, "client_secret", "SECRETVALUE-XYZ", NOW, "o")
    connections.set_modes(conn, account, True, False, NOW, "o")
    conn.close()
    return tmp_path, ["--db", str(tmp_path / "t.db"), "--key-file", str(key_file)]


# --- the panel --------------------------------------------------------------------------------------------------------------------------

def test_a_lost_send_is_shown_in_the_chat_and_the_card_and_goes_again_once_by_the_button(tmp_path):
    from test_chats_pages import _sendable_page, post
    app, client, tok, conn, tanya = _sendable_page(tmp_path, (200, {"messages": []}), (200, {"id": "m-again", "created": 1_791_900_800}))   # a look, the send
    card = tanya["order_id"]
    assert card
    conn.execute("INSERT INTO chat_send_log (token, chat_row, at, author, status, text) VALUES ('lost-1', ?, ?, 'система', 'lost', 'Можно забирать')",
                 (tanya["id"], "2026-10-13T08:00:00"))
    conn.execute("INSERT INTO chat_send_log (token, chat_row, at, author, status, tries, first_at) VALUES ('retry-1', ?, ?, 'система', 'retry', 2, ?)",
                 (tanya["id"], "2026-10-13T08:50:00", "2026-10-13T08:49:00"))
    conn.commit()
    page = client.get("/chats/%d" % tanya["id"]).get_data(as_text=True)
    assert "Не дошло: в чате Авито его нет. Можно отправить снова." in page and 'name="again" value="lost-1"' in page and "«Можно забирать»" in page
    assert "Авито отказал (402)" in page and 'value="retry-1"' not in page                      # «повторю сам» or «не повторила»: by the clock
    card_page = client.get("/orders/%d" % card).get_data(as_text=True)
    assert 'name="again" value="lost-1"' in card_page and 'name="back" value="/orders/%d"' % card in card_page
    res = post(client, "/chats/%d/send" % tanya["id"], tok, again="lost-1", back="/orders/%d" % card)
    assert res.status_code == 302 and res.headers["Location"].endswith("/orders/%d" % card)
    assert conn.execute("SELECT status, message_id, text FROM chat_send_log WHERE token = 'lost-1'").fetchone() == ("sent", "m-again", "")
    calls = len(app.config["AVITO_HTTP"].calls)
    post(client, "/chats/%d/send" % tanya["id"], tok, again="lost-1")                           # a second press: nothing goes
    assert len(app.config["AVITO_HTTP"].calls) == calls
    assert 'value="lost-1"' not in client.get("/chats/%d" % tanya["id"]).get_data(as_text=True)


def test_the_button_does_not_send_a_send_that_is_not_lost(tmp_path):
    from test_chats_pages import _sendable_page, post
    app, client, tok, conn, tanya = _sendable_page(tmp_path)
    conn.execute("INSERT INTO chat_send_log (token, chat_row, at, author, status, text) VALUES ('u-1', ?, ?, 'o', 'unknown', 'Текст')", (tanya["id"], NOW))
    conn.commit()
    calls = len(app.config["AVITO_HTTP"].calls)
    post(client, "/chats/%d/send" % tanya["id"], tok, again="u-1")
    assert len(app.config["AVITO_HTTP"].calls) == calls and conn.execute("SELECT status FROM chat_send_log WHERE token = 'u-1'").fetchone()[0] == "unknown"


def test_a_send_of_a_deleted_chat_or_one_that_breaks_does_not_hold_the_others(env, monkeypatch):
    conn, chat_row, card = env
    sender = unknown(conn, chat_row)
    conn.execute("INSERT INTO chat_send_log (token, chat_row, at, author, status, text, next_at) VALUES ('gone', 9999, ?, 'o', 'retry', 'x', ?)", (NOW, NOW))
    conn.commit()
    assert chats.settle_sends(conn, sender, lambda: sender, 1, at(61)) == {"wait": 1}          # 9999 is not a chat of this account: not its business
    conn.execute("UPDATE chat_send_log SET chat_row = ? WHERE token = 'gone'", (chat_row,))
    conn.commit()
    monkeypatch.setattr(chats, "get_chat", lambda c, r: None)
    assert settle(conn, sender, at(400)) == {"gone": 2}
    assert row(conn, "gone")["status"] == "failed" and row(conn)["verdict"] == "чат удалён" and settle(conn, sender, at(500)) == {}
    monkeypatch.undo()
    conn.execute("UPDATE chat_send_log SET status = 'unknown', verdict = '', next_at = '' WHERE token = 'tok-1'")
    conn.commit()
    monkeypatch.setattr(chats, "_check_unknown", lambda *a: 1 / 0)
    assert settle(conn, sender, at(600)) == {"error": 1} and row(conn)["next_at"] == at(660)     # next minute, not every run before the others


def test_the_writer_is_made_once_per_run(env):
    conn, chat_row, card = env
    sender = Refusing(refused402(), refused402())
    for token in ("tok-1", "tok-2"):
        with pytest.raises(chats.ChatSendRetry):
            chats.send_reply(conn, sender, chat_row, "Текст " + token, token, NOW, "owner")
    made = []
    assert chats.settle_sends(conn, sender, lambda: made.append(1) or sender, 1, at(61)) == {"sent": 2} and made == [1]


# --- the review, round 1 ----------------------------------------------------------------------------------------------------------------

def test_another_message_of_ours_after_the_refusal_stops_the_retry(env):
    """A person who wrote by hand after «Авито отказал» must not get a second message from the sync."""
    conn, chat_row, card = env
    sender = Refusing(refused402())
    with pytest.raises(chats.ChatSendRetry):
        chats.send_reply(conn, sender, chat_row, "Срок резерва истекает", "tok-1", NOW, "система")
    sender.history = [out("m-hand", "Татьяна, резерв до завтра", created=NOW_TS + 20)]
    assert settle(conn, sender, at(61)) == {"failed": 1} and sender.sent == []
    assert "повтор остановлен" in row(conn)["error"] and row(conn)["text"] == ""
    [line] = inbox(conn)
    assert "уже есть другое наше сообщение" in line and "ничего не делайте" in line and order_manager.COPY_MARK not in line


def test_the_syncs_own_send_of_another_message_does_not_stop_a_retry(env):
    conn, chat_row, card = env
    sender = Refusing(refused402(), refused402())
    for token in ("tok-1", "tok-2"):
        with pytest.raises(chats.ChatSendRetry):
            chats.send_reply(conn, sender, chat_row, "Текст " + token, token, NOW, "owner")
    assert settle(conn, sender, at(61)) == {"sent": 2} and sender.sent == ["Текст tok-1", "Текст tok-2"]


def test_a_passing_trouble_of_the_look_puts_the_retry_off_without_counting(env):
    conn, chat_row, card = env
    sender = Refusing(refused402())
    with pytest.raises(chats.ChatSendRetry):
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")
    sender.broken = chats.AvitoError(503, None, "HTTP 503")
    assert settle(conn, sender, at(61)) == {"recheck": 1} and sender.sent == []
    assert row(conn)["status"] == "retry" and row(conn)["tries"] == 1 and row(conn)["next_at"] == at(121)
    sender.broken = False
    assert settle(conn, sender, at(121)) == {"sent": 1} and sender.sent == ["Добрый день"]


def test_a_retry_not_gone_in_30_minutes_ends_with_an_alert_not_a_late_message(env):
    conn, chat_row, card = env
    sender = Refusing(refused402())
    with pytest.raises(chats.ChatSendRetry):
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")
    assert conn.execute("SELECT first_at FROM chat_send_log WHERE token = 'tok-1'").fetchone()[0] == NOW
    shown = chats.pending_sends(conn, [chat_row], at(31 * 60))
    assert "система не повторила" in shown[0]["say"]
    assert settle(conn, sender, at(31 * 60)) == {"failed": 1} and sender.sent == []
    [line] = inbox(conn)
    assert "повторить за 30 минут не удалось" in line and line.endswith(order_manager.COPY_MARK + "Добрый день")


def test_the_retries_count_from_the_first_refusal_not_from_the_last_try(env):
    conn, chat_row, card = env
    sender = Refusing(refused402(), refused402())
    with pytest.raises(chats.ChatSendRetry):
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")
    settle(conn, sender, at(61))
    assert conn.execute("SELECT first_at, at FROM chat_send_log WHERE token = 'tok-1'").fetchone() == (NOW, at(61))


def test_a_send_the_sync_took_and_left_sending_becomes_unknown_and_is_checked(env):
    conn, chat_row, card = env
    sender = Refusing(refused402())
    with pytest.raises(chats.ChatSendRetry):
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")
    conn.execute("UPDATE chat_send_log SET status = 'sending', at = ?, next_at = ? WHERE token = 'tok-1'", (at(61), at(61)))   # the run was killed
    conn.commit()
    assert settle(conn, sender, at(200)) == {} and row(conn)["status"] == "sending"                 # fresh: still its own
    assert settle(conn, sender, at(400)) == {"wait": 1} and row(conn)["status"] == "unknown" and sender.sent == []


def test_a_press_sending_is_not_taken_by_the_sync(env):
    conn, chat_row, card = env
    conn.execute("INSERT INTO chat_send_log (token, chat_row, at, author, status, text) VALUES ('tok-1', ?, ?, 'owner', 'sending', 'Текст')", (chat_row, NOW))
    conn.commit()
    assert settle(conn, Sender(), at(1000)) == {} and row(conn)["status"] == "sending"


def test_the_402s_are_settled_before_the_unknown_ones(env, monkeypatch):
    conn, chat_row, card = env
    monkeypatch.setattr(chats, "SETTLE_BUDGET", 1)
    for n in range(2):
        conn.execute("INSERT INTO chat_send_log (token, chat_row, at, author, status, text) VALUES (?, ?, ?, 'o', 'unknown', 'x')", ("u%d" % n, chat_row, at(-600 + n)))
    sender = Refusing(refused402())
    with pytest.raises(chats.ChatSendRetry):
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")
    assert settle(conn, sender, at(61)) == {"sent": 1}


def test_the_kept_text_goes_after_a_week(env):
    conn, chat_row, card = env
    conn.execute("INSERT INTO chat_send_log (token, chat_row, at, author, status, text) VALUES ('old-lost', ?, ?, 'o', 'lost', 'Текст')", (chat_row, at(-8 * 86400)))
    conn.commit()
    settle(conn, Sender(), NOW)
    assert row(conn, "old-lost")["text"] == ""


# --- the review, round 2 ----------------------------------------------------------------------------------------------------------------

class PhotoThenText(Refusing):
    """One press of the chat: the photo goes, the text after it is refused or not answered."""

    def upload_image(self, user_id, name, data):
        return "img-1"

    def send_image(self, user_id, chat_id, image_id):
        self.sent.append(image_id)
        return {"id": "p-out", "created": NOW_TS + 5}


def test_a_photo_sent_with_the_text_does_not_stop_the_retry_of_the_text(env):
    conn, chat_row, card = env
    sender = PhotoThenText(refused402())
    chats.send_image(conn, sender, chat_row, picture(), "img-tok", NOW, "owner")
    with pytest.raises(chats.ChatSendRetry):
        chats.send_reply(conn, sender, chat_row, "Вот фото шины", "tok-1", NOW, "owner")
    sender.history = [{"id": "p-out", "created": NOW_TS + 5, "direction": "out", "type": "image", "content": {}}]
    assert settle(conn, sender, at(61)) == {"sent": 1} and sender.sent == ["img-1", "Вот фото шины"]


def test_a_photo_sent_with_the_text_does_not_stop_the_check_of_an_unknown_text(env):
    conn, chat_row, card = env
    sender = PhotoThenText(TimeoutError())
    chats.send_image(conn, sender, chat_row, picture(), "img-tok", NOW, "owner")
    with pytest.raises(chats.ChatSendUnknown):
        chats.send_reply(conn, sender, chat_row, "Вот фото шины", "tok-1", NOW, "owner")
    assert settle(conn, sender, at(61)) == {"wait": 1}
    assert settle(conn, sender, at(301)) == {"resent": 1} and sender.sent == ["img-1", "Вот фото шины"]


def test_a_message_written_in_the_panel_after_the_refusal_still_stops_the_retry(env):
    conn, chat_row, card = env
    sender = Refusing(refused402())
    with pytest.raises(chats.ChatSendRetry):
        chats.send_reply(conn, sender, chat_row, "Срок резерва истекает", "tok-1", NOW, "система")
    chats.send_reply(conn, sender, chat_row, "Татьяна, резерв до завтра", "hand-1", at(30), "owner")
    assert settle(conn, sender, at(61)) == {"failed": 1} and sender.sent == ["Татьяна, резерв до завтра"]


def test_a_press_on_a_retry_over_30_minutes_old_says_the_same_as_the_page(env):
    conn, chat_row, card = env
    sender = Refusing(refused402())
    with pytest.raises(chats.ChatSendRetry):
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")
    with pytest.raises(chats.ChatSendRetry, match="не повторила"):
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", at(31 * 60), "owner")
    assert sender.sent == []


def test_the_text_written_by_hand_within_30_minutes_is_found_before_the_end(env):
    conn, chat_row, card = env
    sender = Refusing(refused402())
    with pytest.raises(chats.ChatSendRetry):
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")
    sender.history = [out("m-hand", "Добрый день", created=NOW_TS + 600)]
    assert settle(conn, sender, at(31 * 60)) == {"sent": 1} and inbox(conn) == []


def test_a_send_again_that_could_not_go_is_not_called_sent_again(env):
    conn, chat_row, card = env
    sender = unknown(conn, chat_row)
    settle(conn, sender, at(61))

    def broken():
        raise RuntimeError("no keys")
    assert chats.settle_sends(conn, sender, broken, 1, at(301)) == {"lost": 1} and sender.sent == []
    lines = journal(conn, card)
    assert not any("отправлено повторно автоматически" in line for line in lines)
    assert any(line.startswith("Сообщение клиенту не дошло") and "отправляем повторно" in line for line in lines)
    assert "после автоматического повтора" not in chats.pending_sends(conn, [chat_row], at(302))[0]["say"]


def test_the_button_says_so_when_the_message_was_found_in_the_chat(env):
    conn, chat_row, card = env
    conn.execute("INSERT INTO chat_send_log (token, chat_row, at, author, status, text) VALUES ('tok-1', ?, ?, 'o', 'lost', 'Добрый день')", (chat_row, NOW))
    conn.commit()
    sender = Sender(history=[out("m-came", "Добрый день")])
    assert chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", at(60), "owner", again=True) == "found" and sender.sent == []


# --- the review, round 3 ----------------------------------------------------------------------------------------------------------------

def test_a_retelling_by_hand_refused_too_stops_the_automatic_retry_and_goes_itself(env):
    """Н1: «Срок резерва» gets a 402, the inbox says «напишите вручную», the person writes it again — refused as well. When Avito takes
    messages again only the person's goes, never both."""
    conn, chat_row, card = env
    sender = Refusing(refused402(), refused402())
    with pytest.raises(chats.ChatSendRetry):
        chats.send_reply(conn, sender, chat_row, "Срок резерва истекает", "reserve-1", NOW, "система")
    with pytest.raises(chats.ChatSendRetry):
        chats.send_reply(conn, sender, chat_row, "Татьяна, резерв до завтра", "hand-1", at(20), "owner")
    assert settle(conn, sender, at(81)) == {"failed": 1, "sent": 1}
    assert sender.sent == ["Татьяна, резерв до завтра"] and row(conn, "reserve-1")["status"] == "failed"


def test_a_retelling_by_hand_after_an_unknown_send_stops_the_send_again(env):
    conn, chat_row, card = env
    sender = unknown(conn, chat_row)
    chats.send_reply(conn, sender, chat_row, "Татьяна, напишу ещё раз: заказ готов", "hand-1", at(30), "owner")
    conn.execute("DELETE FROM avito_message WHERE direction = 'out'")                          # not yet read back from Avito
    conn.commit()
    assert settle(conn, sender, at(61)) == {"unknown": 1} and row(conn)["verdict"] == chats.OTHERS
    assert sender.sent == ["Татьяна, напишу ещё раз: заказ готов"]


def test_a_late_retry_with_another_message_since_asks_to_look_not_to_send_by_hand(env):
    conn, chat_row, card = env
    sender = Refusing(refused402())
    with pytest.raises(chats.ChatSendRetry):
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")
    sender.history = [out("m-hand", "Здравствуйте, Татьяна", created=NOW_TS + 600)]
    assert settle(conn, sender, at(31 * 60)) == {"failed": 1}
    [line] = inbox(conn)
    assert "ничего не делайте" in line and order_manager.COPY_MARK not in line
