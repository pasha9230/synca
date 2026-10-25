"""No copy of a message the client has already (tails6, lane no-copy, 2026-10-25): a new press of a send that did not go («failed»: a second
«приехал» after Avito refused the first one, the task pressed again) looks at the chat from the first start of that send first — the same
text of ours there (a manager wrote it by hand from «Входящие») is «sent», nothing goes; another message of ours there stops it («проверьте
чат»); a chat that cannot be read does not hold it back. A closed line of «Входящие» shows no text to copy. An unknown send of a skipped task
or a closed order that cannot be checked closes the caller's line of «Входящие» too."""
from __future__ import annotations

import re

import pytest

from test_chat_send_refusal import Refusing, picture, refusal
from test_send_settle import NOW_TS, at, out, row, settle
from test_send_settle2 import make_task, task_token, to_stage
from test_stuck_sending import NOW, env, journal  # noqa: F401  (env is a fixture)
from tires_sync import chats, order_engine as engine, order_manager, orders

ARRIVED = "Можно забирать"


def refused403():
    return refusal(403, {"error": {"code": 403, "message": "Forbidden"}})


def lines(conn):
    return [dict(zip(("message_id", "status", "answer", "text"), r))
            for r in conn.execute("SELECT message_id, status, answer, text FROM manager_message ORDER BY id")]


def events(conn, card):
    return [(e["text"], e["level"]) for e in orders.list_events(conn, card)]


def refused_once(conn, chat_row, sender, text="Добрый день", token="tok-1"):
    """«Добрый день» refused by Avito (403) at NOW: the mark is «failed», its first start NOW."""
    with pytest.raises(chats.ChatError):
        chats.send_reply(conn, sender, chat_row, text, token, NOW, "owner")
    assert row(conn, token)["status"] == "failed" and sender.sent == []


def arrived(conn, card, sender, monkeypatch, when):
    monkeypatch.setattr(order_manager, "_arrived_body", lambda conn: ARRIVED)
    return order_manager._tell_arrived(conn, card, lambda c, t, k: chats.send_reply(conn, sender, c, t, k, when, "система"), when, "система")


def arrived_token(conn):
    return conn.execute("SELECT token FROM chat_send_log WHERE token LIKE 'arrived-%'").fetchone()[0]


# --- 1: a new press of a failed text looks at the chat first ---------------------------------------------------------------------------

def test_a_new_press_of_a_refused_text_written_by_hand_meanwhile_sends_nothing(env):
    conn, chat_row, card = env
    sender = Refusing(refused403())
    refused_once(conn, chat_row, sender)
    sender.history = [out("m-hand", "Добрый  день", created=NOW_TS + 300)]                    # the manager wrote it by hand
    assert chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", at(1000), "owner") == "found"
    assert sender.sent == [] and row(conn)["status"] == "sent" and row(conn)["message_id"] == "m-hand" and row(conn)["error"] == ""
    assert ("Сообщение клиенту уже есть в чате Авито: повторно не отправлено", "green") in events(conn, card)
    assert chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", at(1100), "owner") is None and sender.sent == []


def test_a_second_arrived_after_a_refusal_finds_the_text_written_from_the_inbox_and_closes_its_line(env, monkeypatch):
    conn, chat_row, card = env
    sender = Refusing(refused403())
    sent, _note = arrived(conn, card, sender, monkeypatch, NOW)
    [line] = lines(conn)
    assert not sent and line["status"] == "new" and order_manager.COPY_MARK + ARRIVED in line["text"]
    sender.history = [out("m-hand", ARRIVED, created=NOW_TS + 300)]
    sent, _note = arrived(conn, card, sender, monkeypatch, at(1000))
    assert sent and sender.sent == [] and row(conn, arrived_token(conn))["status"] == "sent"
    [line] = lines(conn)
    assert line["status"] == "answered" and line["answer"] == FOUND


FOUND = "Итог: сообщение уже есть в чате Авито (найдено при повторной отправке) — отправлять не нужно."


def alert_line(conn, token, text="Заказ: клиенту не отправлено — Отправьте вручную."):
    """The sync's own alert with the text to copy (_alert_not_sent: the 402s ended, a send again refused)."""
    conn.execute("INSERT INTO manager_message (message_id, at, point_id, sender, text, status) VALUES (?, ?, NULL, 'система', ?, 'new')",
                 ("send-%s-20261013120500" % token, NOW, text + order_manager.COPY_MARK + "Добрый день"))
    conn.commit()


def test_a_found_text_closes_the_alert_of_the_sync_with_its_copy_too(env):
    """The review of tails6, 1: the 402s ended in «failed» and a line «Отправьте вручную» with the text; the manager wrote it; a new press
    finds it — no line is left asking to send it by hand."""
    conn, chat_row, card = env
    sender = Refusing(refused403())
    refused_once(conn, chat_row, sender)
    alert_line(conn, "tok-1")
    alert_line(conn, "tok-10")                                                                  # another send: its alert stays
    sender.history = [out("m-hand", "Добрый день", created=NOW_TS + 300)]
    assert chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", at(1000), "owner") == "found"
    shown = {line["message_id"]: (line["status"], line["answer"]) for line in lines(conn)}
    assert shown["send-tok-1-20261013120500"] == ("answered", FOUND) and shown["send-tok-10-20261013120500"] == ("new", "")


def test_a_press_that_another_press_beat_to_the_found_text_sends_nothing(env, monkeypatch):
    conn, chat_row, card = env
    sender = Refusing(refused403())
    refused_once(conn, chat_row, sender)
    sender.history = [out("m-hand", "Добрый день", created=NOW_TS + 300)]
    real = chats._same_text

    def beaten(ours, text, state="sent"):
        conn.execute("UPDATE chat_send_log SET status = ? WHERE token = 'tok-1'", (state,))     # another press settled it meanwhile
        conn.commit()
        return real(ours, text)
    monkeypatch.setattr(chats, "_same_text", beaten)
    assert chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", at(1000), "owner") == "found" and sender.sent == []
    conn.execute("UPDATE chat_send_log SET status = 'failed' WHERE token = 'tok-1'")
    conn.commit()
    monkeypatch.setattr(chats, "_same_text", lambda ours, text: beaten(ours, text, "sending"))
    with pytest.raises(chats.ChatError) as raised:
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", at(1100), "owner")
    assert "уже отправляется" in str(raised.value) and sender.sent == []


def test_a_busy_base_after_the_read_sends_nothing(env, monkeypatch):
    import sqlite3
    conn, chat_row, card = env
    sender = Refusing(refused403())
    refused_once(conn, chat_row, sender)

    def busy(*args):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(chats, "_others", busy)
    with pytest.raises(chats.ChatSendUnknown) as raised:
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", at(1000), "owner")
    assert "попробуйте через минуту" in str(raised.value) and sender.sent == [] and row(conn)["status"] == "failed"


def test_a_new_press_of_a_refused_text_with_another_message_of_ours_since_sends_nothing(env):
    conn, chat_row, card = env
    sender = Refusing(refused403())
    refused_once(conn, chat_row, sender)
    sender.history = [out("m-hand", "Здравствуйте, ваш заказ ждёт", created=NOW_TS + 300)]     # written by hand in other words
    with pytest.raises(chats.ChatSendUnknown) as raised:
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", at(1000), "owner")
    assert "проверьте чат" in str(raised.value).lower() and "возможно, его уже написали вручную" in str(raised.value)
    assert sender.sent == [] and row(conn)["status"] == "failed"
    [said] = [e for e in events(conn, card) if "не отправлено повторно" in e[0]]
    assert said[1] == "red" and chats.OTHERS in said[0] and "Проверьте чат в кабинете Авито" in said[0]


def test_another_send_of_the_panel_since_the_refusal_stops_the_new_press(env):
    conn, chat_row, card = env
    sender = Refusing(refused403())
    refused_once(conn, chat_row, sender)
    chats.send_reply(conn, sender, chat_row, "Здравствуйте, заказ ждёт вас", "tok-2", at(300), "owner")    # by hand, in the panel
    assert sender.sent == ["Здравствуйте, заказ ждёт вас"]
    with pytest.raises(chats.ChatSendUnknown):
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", at(1000), "owner")
    assert sender.sent == ["Здравствуйте, заказ ждёт вас"] and row(conn)["status"] == "failed"


def test_a_second_arrived_stopped_by_another_message_gives_no_text_to_copy(env, monkeypatch):
    conn, chat_row, card = env
    sender = Refusing(refused403())
    arrived(conn, card, sender, monkeypatch, NOW)
    sender.history = [out("m-hand", "Здравствуйте! Шины приехали, ждём вас", created=NOW_TS + 300)]
    sent, _note = arrived(conn, card, sender, monkeypatch, at(1000))
    assert not sent and sender.sent == [] and row(conn, arrived_token(conn))["status"] == "failed"
    first, second = order_manager.list_inbox(conn)[::-1]
    assert second["status"] == "new" and second["client_text"] == "" and "повторно не отправляйте" in second["text"]
    assert first["status"] == "answered" and first["answer"].startswith("Итог: сообщение не отправлено повторно: " + chats.OTHERS)  # review 2


def test_another_message_closes_the_alert_of_the_sync_with_its_copy(env):
    conn, chat_row, card = env
    sender = Refusing(refused403())
    refused_once(conn, chat_row, sender)
    alert_line(conn, "tok-1")
    sender.history = [out("m-hand", "Здравствуйте, ваш заказ ждёт", created=NOW_TS + 300)]
    with pytest.raises(chats.ChatSendUnknown):
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", at(1000), "owner")
    [line] = lines(conn)
    assert line["status"] == "answered" and "возможно, его уже написали вручную" in line["answer"] and "Проверьте чат" in line["answer"]


def test_a_new_press_of_a_refused_text_with_nothing_in_the_chat_goes_as_before(env):
    conn, chat_row, card = env
    sender = Refusing(refused403())
    refused_once(conn, chat_row, sender)
    sender.history = [out("m-before", "Раньше", created=NOW_TS - 600)]                        # before the first start: not this send's time
    assert chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", at(1000), "owner") is None
    assert sender.sent == ["Добрый день"] and row(conn)["status"] == "sent"


@pytest.mark.parametrize("broken", [True, chats.AvitoError(403, None, "HTTP 403"), ValueError("a strange answer")])
def test_a_new_press_of_a_refused_text_whose_chat_cannot_be_read_goes_as_before(env, broken):
    """A refusal means Avito did not take the first one: a chat that cannot be read now does not hold the press back."""
    conn, chat_row, card = env
    sender = Refusing(refused403())
    refused_once(conn, chat_row, sender)
    sender.broken = broken
    assert chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", at(1000), "owner") is None
    assert sender.sent == ["Добрый день"] and row(conn)["status"] == "sent"


def test_a_task_pressed_again_after_a_refusal_finds_the_text_written_by_hand(env):
    conn, chat_row, card = env
    task_id = make_task(conn, card)
    sender = Refusing(refused403())
    with pytest.raises(chats.ChatError):
        engine.send_task(conn, task_id, "Заказ готов", lambda c, t, k: chats.send_reply(conn, sender, c, t, k, NOW, "owner"), NOW, "owner")
    sender.history = [out("m-hand", "Заказ готов", created=NOW_TS + 300)]
    engine.send_task(conn, task_id, "Заказ готов", lambda c, t, k: chats.send_reply(conn, sender, c, t, k, at(1000), "owner"), at(1000), "owner")
    assert sender.sent == [] and row(conn, task_token(task_id))["status"] == "sent" and engine.get_task(conn, task_id)["status"] == "done"


# --- 1: a photo ----------------------------------------------------------------------------------------------------------------------

def refused_photo(conn, chat_row, sender):
    sender.at_upload = True
    with pytest.raises(chats.ChatError):
        chats.send_image(conn, sender, chat_row, picture(), "img-tok", NOW, "owner")
    assert row(conn, "img-tok")["status"] == "failed"


def test_a_new_press_of_a_refused_photo_with_a_message_of_ours_since_sends_nothing(env):
    conn, chat_row, card = env
    sender = Refusing(refused403())
    refused_photo(conn, chat_row, sender)
    sender.history = [{"id": "p-hand", "created": NOW_TS + 300, "direction": "out", "type": "image", "content": {"image": {}}}]
    with pytest.raises(chats.ChatSendUnknown):
        chats.send_image(conn, sender, chat_row, picture(), "img-tok", at(1000), "owner")
    assert sender.sent == [] and row(conn, "img-tok")["status"] == "failed"
    assert any("Фото клиенту не отправлено повторно" in text and "отправили вручную" in text for text, level in events(conn, card))


def test_a_new_press_of_a_refused_photo_with_nothing_in_the_chat_goes_as_before(env):
    conn, chat_row, card = env
    sender = Refusing(refused403())
    refused_photo(conn, chat_row, sender)
    chats.send_image(conn, sender, chat_row, picture(), "img-tok", at(1000), "owner")
    assert sender.sent == ["img-1"] and row(conn, "img-tok")["status"] == "sent"


# --- 2: a closed line of «Входящие» gives no text to copy ----------------------------------------------------------------------------

def test_the_text_to_copy_shows_only_on_a_line_that_waits(tmp_path):
    from test_manual_order_messages import ready
    app, client, tok, conn, tanya, ivan = ready(tmp_path)
    for mid, status in (("waits", "new"), ("closed", "answered")):
        conn.execute("INSERT INTO manager_message (message_id, at, point_id, sender, text, status) VALUES (?, ?, NULL, 'система', ?, ?)",
                     (mid, NOW, "Заказ №%s: клиенту не отправлено%sТекст для %s" % (mid, order_manager.COPY_MARK, mid), status))
    conn.commit()
    body = client.get("/orders/inbox").get_data(as_text=True)
    shown = re.findall(r'<textarea[^>]*id="inbox-copy-\d+"[^>]*>([^<]*)</textarea>', body)
    assert shown == ["Текст для waits"] and body.count("data-copy=") == 1 and body.count("Текст клиенту (отправьте сами)") == 1


# --- 3: cannot be checked, the task skipped or the order closed: the caller's line is closed too ----------------------------------------

def unknown_arrived_of_a_cancelled_order(conn, card, chat_row, monkeypatch):
    sender = Refusing(TimeoutError())
    sent, _note = arrived(conn, card, sender, monkeypatch, NOW)
    assert not sent
    token = arrived_token(conn)
    to_stage(conn, card, "cancel")
    conn.execute("UPDATE avito_chat SET history_ok = 0 WHERE id = ?", (chat_row,))
    conn.commit()
    return sender, token


def test_an_unknown_arrived_of_a_cancelled_order_that_cannot_be_checked_closes_its_line(env, monkeypatch):
    conn, chat_row, card = env
    sender, token = unknown_arrived_of_a_cancelled_order(conn, card, chat_row, monkeypatch)
    [line] = lines(conn)
    assert line["status"] == "new" and line["message_id"].startswith(token + "-")
    assert settle(conn, sender, at(61)) == {"unknown": 1} and sender.sent == []
    [line] = lines(conn)
    assert line["status"] == "answered" and line["answer"] == "Итог: проверить нельзя; заказ в «Отмена» — писать клиенту не нужно."
    assert order_manager.new_count(conn) == 0


def test_closing_after_cannot_leaves_a_line_a_person_answered_another_order_and_the_alerts(env, monkeypatch):
    conn, chat_row, card = env
    sender, token = unknown_arrived_of_a_cancelled_order(conn, card, chat_row, monkeypatch)
    conn.execute("INSERT INTO manager_message (message_id, at, point_id, sender, text, status, answer, answered_at) "
                 "VALUES (?, ?, NULL, 'система', 'раньше', 'answered', 'Написал сам', ?)", (token + "-20261013110000", NOW, NOW))
    conn.execute("INSERT INTO manager_message (message_id, at, point_id, sender, text, status) VALUES (?, ?, NULL, 'система', 'чужой', 'new')",
                 ("arrived-%d-20261013100000-20261013120000" % (card + 1), NOW))
    conn.execute("INSERT INTO manager_message (message_id, at, point_id, sender, text, status) VALUES (?, ?, NULL, 'система', 'своя тревога', 'new')",
                 ("send-%s-20261013120000" % token, NOW))
    conn.execute("INSERT INTO manager_message (message_id, at, point_id, sender, text, status) VALUES (?, ?, NULL, 'Гамма', 'точка', 'new')",
                 (token + "-1", NOW))
    conn.commit()
    settle(conn, sender, at(61))
    shown = {line["text"]: (line["status"], line["answer"]) for line in lines(conn)}
    assert shown["раньше"] == ("answered", "Написал сам") and shown["чужой"] == ("new", "") and shown["своя тревога"] == ("new", "")
    assert shown["точка"] == ("new", "")


def test_an_unknown_arrived_of_an_open_order_that_cannot_be_checked_keeps_its_line(env, monkeypatch):
    conn, chat_row, card = env
    sender = Refusing(TimeoutError())
    arrived(conn, card, sender, monkeypatch, NOW)
    conn.execute("UPDATE avito_chat SET history_ok = 0 WHERE id = ?", (chat_row,))
    conn.commit()
    assert settle(conn, sender, at(61)) == {"unknown": 1}
    [line] = lines(conn)
    assert line["status"] == "new" and line["answer"] == ""


def test_closing_the_line_after_cannot_never_breaks_the_verdict(env, monkeypatch):
    conn, chat_row, card = env
    sender, token = unknown_arrived_of_a_cancelled_order(conn, card, chat_row, monkeypatch)
    conn.execute("ALTER TABLE manager_message RENAME TO manager_message_gone")
    conn.commit()
    assert settle(conn, sender, at(61)) == {"unknown": 1}
    assert row(conn, token)["verdict"] == chats.UNREADABLE
    assert any("писать клиенту не нужно" in text for text, level in events(conn, card))
