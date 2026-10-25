"""A send mark left «sending» by a killed panel (a deploy in the middle of a send): fresh, it still refuses a second press; older than
five minutes it is resolved — marked sent when the message is in the chat, «unknown» when nobody can tell, sent again only when nothing
of it is there — and never sent twice."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from test_chats_sync import TS, FakeClient, chat, db, msg
from tires_sync import chat_deals, chats, order_engine as engine, order_manager, order_templates as tpl, orders

NOW = "2026-10-13T12:00:00"
CLAIMED = "2026-10-13T11:50:00"                     # ten minutes ago: the panel was killed while it sent
CLAIMED_TS = int(datetime.fromisoformat(CLAIMED).replace(tzinfo=chats.MSK).timestamp())


class Sender:
    def __init__(self, history=(), broken=False):
        self.sent, self.history, self.broken, self.reads = [], list(history), broken, 0           # broken: True (a 503) or an error to raise

    def get_self(self, retry=True):
        return {"id": 1}

    def chat_messages(self, user_id, chat_id, limit=100, offset=0, retry=True):
        """What Avito has in the chat now (read before an interrupted send is taken again)."""
        self.reads += 1
        if self.broken:
            raise self.broken if isinstance(self.broken, Exception) else chats.AvitoError(503, None, "HTTP 503")
        return list(self.history)

    def send_message(self, user_id, chat_id, text):
        self.sent.append(text)
        return {"id": "m-out-%d" % len(self.sent), "created": TS + 1000 + len(self.sent), "direction": "out", "type": "text", "content": {"text": text}}


@pytest.fixture
def env(tmp_path):
    conn, account = db(tmp_path)
    orders.ensure_pipeline(conn, NOW)
    conn.execute("UPDATE account SET chats_enabled = 1, chats_send = 1 WHERE id = ?", (account,))
    conn.commit()
    chats.sync_account(conn, FakeClient([[chat("c1", text="Здравствуйте")]], {"c1": [msg("m1", "Здравствуйте", read=1)]}), account, NOW)
    [row] = chats.list_chats(conn)
    card = orders.create_card(conn, dict(client_name="Татьяна"), NOW, "o")
    chat_deals.link_chat(conn, row["id"], card, NOW, "owner")
    return conn, row["id"], card


def claim(conn, chat_row, token="tok-1", at=CLAIMED, message_id=""):
    conn.execute("INSERT INTO chat_send_log (token, chat_row, at, author, status, message_id) VALUES (?,?,?,?, 'sending', ?)", (token, chat_row, at, "owner", message_id))
    conn.commit()


def out_message(conn, chat_row, mid, text, created):
    chats._store_message(conn, chat_row, {"id": mid, "created": created, "direction": "out", "type": "text", "content": {"text": text}, "isRead": True})
    conn.commit()


def status(conn, token="tok-1"):
    return conn.execute("SELECT status, message_id FROM chat_send_log WHERE token = ?", (token,)).fetchone()


def journal(conn, card):
    return [e["text"] for e in orders.list_events(conn, card)]


def test_a_fresh_sending_still_refuses_the_second_press(env):
    conn, chat_row, card = env
    claim(conn, chat_row, at="2026-10-13T11:58:00")
    sender = Sender()
    with pytest.raises(chats.ChatError, match="уже отправляется"):
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")
    assert sender.sent == [] and status(conn)[0] == "sending"


def test_an_old_sending_with_nothing_of_it_in_the_chat_is_sent_again_once(env):
    conn, chat_row, card = env
    claim(conn, chat_row)
    sender = Sender()
    chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")
    assert sender.sent == ["Добрый день"] and status(conn) == ("sent", "m-out-1")
    assert any("прервалась" in t for t in journal(conn, card))
    chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")          # the same press again: it is out
    assert sender.sent == ["Добрый день"]


def test_an_old_sending_whose_message_is_in_the_chat_is_marked_sent_without_a_second_one(env):
    conn, chat_row, card = env
    claim(conn, chat_row)
    out_message(conn, chat_row, "m-left", "Добрый  день", CLAIMED_TS + 5)                  # it left before the panel died; the reading brought it
    sender = Sender()
    chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")
    assert sender.sent == [] and status(conn) == ("sent", "m-left")


def test_an_old_sending_with_the_answer_of_avito_is_never_sent_again(env):
    conn, chat_row, card = env
    claim(conn, chat_row, message_id="m-known")
    sender = Sender()
    chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")
    assert sender.sent == [] and status(conn) == ("sent", "m-known")


def test_another_message_of_ours_after_the_start_or_a_chat_without_history_is_unknown_not_a_second_send(env):
    conn, chat_row, card = env
    claim(conn, chat_row)
    out_message(conn, chat_row, "m-other", "Совсем другое", CLAIMED_TS + 30)
    sender = Sender()
    with pytest.raises(chats.ChatSendUnknown):
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")
    assert sender.sent == [] and status(conn)[0] == "unknown"
    claim(conn, chat_row, token="tok-2")
    conn.execute("UPDATE avito_chat SET history_ok = 0 WHERE id = ?", (chat_row,))
    conn.commit()
    with pytest.raises(chats.ChatSendUnknown):
        chats.send_reply(conn, sender, chat_row, "Ещё", "tok-2", NOW, "owner")
    assert sender.sent == [] and status(conn, "tok-2")[0] == "unknown"


def test_the_same_text_sent_before_the_interrupted_send_does_not_count(env):
    conn, chat_row, card = env
    out_message(conn, chat_row, "m-yesterday", "Добрый день", CLAIMED_TS - 3600)
    claim(conn, chat_row)
    sender = Sender()
    chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")
    assert sender.sent == ["Добрый день"]


def test_an_old_photo_sending_with_any_photo_of_ours_since_is_unknown_never_sent_again(env):
    conn, chat_row, card = env
    claim(conn, chat_row, token="img-1")
    chats._store_message(conn, chat_row, {"id": "p-left", "created": CLAIMED_TS + 5, "direction": "out", "type": "image", "content": {}, "isRead": True})
    conn.commit()

    class Pictures(Sender):
        def upload_image(self, *a, **k):
            raise AssertionError("must not be sent again")
    from PIL import Image
    import io
    data = io.BytesIO()
    Image.new("RGB", (10, 10)).save(data, "JPEG")
    with pytest.raises(chats.ChatSendUnknown):
        chats.send_image(conn, Pictures(), chat_row, data.getvalue(), "img-1", NOW, "owner")    # it may be another photo: never «sent» by a guess
    assert status(conn, "img-1")[0] == "unknown"


def test_the_chat_is_read_at_avito_before_a_second_send_and_a_message_there_is_never_sent_again(env):
    conn, chat_row, card = env
    claim(conn, chat_row)                                                                        # the reading has not brought it (the poll stood still)
    sender = Sender(history=[{"id": "m-left", "created": CLAIMED_TS + 5, "direction": "out", "type": "text", "content": {"text": "Добрый день"}, "isRead": True}])
    chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")
    assert sender.reads == 1 and sender.sent == [] and status(conn) == ("sent", "m-left")


@pytest.mark.parametrize("error", [chats.AvitoError(503, None, "HTTP 503"), chats.AvitoError(429, None, "HTTP 429"), OSError("timed out")],
                         ids=["503", "429", "network"])
def test_a_passing_trouble_of_the_read_asks_to_try_again_and_keeps_the_mark(env, error):
    conn, chat_row, card = env
    claim(conn, chat_row)
    sender = Sender(broken=error)
    with pytest.raises(chats.ChatError, match="попробуйте через минуту") as err:
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")
    assert not isinstance(err.value, chats.ChatSendUnknown)
    assert sender.sent == [] and status(conn) == ("sending", "")
    sender.broken = False                                                                     # a minute later Avito answers: it goes once
    chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")
    assert sender.sent == ["Добрый день"]


def test_a_refused_read_or_avitos_stub_is_unknown_not_a_second_send(env):
    conn, chat_row, card = env
    claim(conn, chat_row)
    sender = Sender(broken=chats.AvitoError(402, None, "Перейдите на подписку с API мессенджера"))
    with pytest.raises(chats.ChatSendUnknown):
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")
    assert sender.sent == [] and status(conn)[0] == "unknown"
    claim(conn, chat_row, token="tok-2")
    stub = Sender(history=[{"id": "x", "created": TS, "direction": "in", "type": "system", "content": {"text": "Перейдите на подписку с API мессенджера"}}])
    with pytest.raises(chats.ChatSendUnknown):
        chats.send_reply(conn, stub, chat_row, "Ещё", "tok-2", NOW, "owner")
    assert stub.sent == []


def test_a_full_page_that_does_not_reach_back_to_the_start_is_unknown(env):
    conn, chat_row, card = env
    claim(conn, chat_row)
    page = [{"id": "c%d" % i, "created": CLAIMED_TS + 10 + i, "direction": "in", "type": "text", "content": {"text": "?"}} for i in range(chats.RECHECK)]
    sender = Sender(history=page)                                          # all newer than the start: ours may be further back
    with pytest.raises(chats.ChatSendUnknown):
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")
    assert sender.sent == []


def test_the_check_keeps_nothing_of_the_chat_so_the_reading_still_alerts_about_the_client(env):
    conn, chat_row, card = env
    claim(conn, chat_row)
    new_in = {"id": "m-client", "created": TS + 5000, "direction": "in", "type": "text", "content": {"text": "Вы где?"}, "isRead": False, "read": 0}
    sender = Sender(history=[new_in])
    chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")
    assert conn.execute("SELECT COUNT(*) FROM avito_message WHERE message_id = 'm-client'").fetchone() == (0,)
    reader = FakeClient([[chat("c1", updated=TS + 5000, text="Вы где?")]], {"c1": [msg("m1", "Здравствуйте", read=1), new_in]})
    chats.sync_account(conn, reader, chats.get_chat(conn, chat_row)["account_id"], "2026-10-13T12:05:00")
    assert any(t.startswith("Клиент написал: Вы где?") for t in journal(conn, card))


def test_a_busy_base_during_the_check_asks_to_try_again(env, monkeypatch):
    import sqlite3
    conn, chat_row, card = env
    claim(conn, chat_row)
    monkeypatch.setattr(chats, "_ours_since", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")))
    sender = Sender()
    with pytest.raises(chats.ChatError, match="попробуйте через минуту"):
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")
    assert sender.sent == [] and status(conn)[0] == "sending"


def test_a_second_press_while_the_resumed_send_goes_is_refused(env):
    conn, chat_row, card = env
    claim(conn, chat_row)
    seen = []

    class Slow(Sender):
        def send_message(self, user_id, chat_id, text):
            with pytest.raises(chats.ChatError, match="уже отправляется") as err:
                chats.send_reply(conn, Sender(), chat_row, text, "tok-1", NOW, "other")
            seen.append(str(err.value))
            return super().send_message(user_id, chat_id, text)
    sender = Slow()
    chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")
    assert sender.sent == ["Добрый день"] and len(seen) == 1 and status(conn)[0] == "sent"


def test_a_task_that_is_not_a_message_keeps_its_ten_minutes(env):
    conn, chat_row, card = env
    conn.execute("INSERT INTO order_task (order_id, rule_id, entered_at, kind, arg, title, body, status, created_at, done_at) VALUES (?, 0, '2026-10-13T11:40:00', 'stage', '', 'Этап', '', 'running', ?, ?)",
                 (card, "2026-10-13T11:40:00", "2026-10-13T11:54:00"))
    task_id = conn.execute("SELECT MAX(id) FROM order_task").fetchone()[0]
    conn.commit()
    engine.refresh_tasks(conn, card, NOW)
    assert engine.get_task(conn, task_id)["status"] == "running"


def test_a_message_task_stuck_by_a_killed_panel_can_be_sent_after_five_minutes(env):
    conn, chat_row, card = env
    template = tpl.add_template(conn, "Клиенту: готов", "client", "Заказ готов")
    conn.execute("INSERT INTO order_task (order_id, rule_id, entered_at, kind, arg, title, body, status, created_at, done_at) VALUES (?, 0, '2026-10-13T11:40:00', 'message', ?, 'Клиенту', 'Заказ готов', 'running', ?, ?)",
                 (card, str(template), "2026-10-13T11:40:00", CLAIMED))
    task_id = conn.execute("SELECT MAX(id) FROM order_task").fetchone()[0]
    claim(conn, chat_row, token="task-%d-%s" % (task_id, "20261013114000"))
    conn.commit()
    sender = Sender()
    sent_to = engine.send_task(conn, task_id, "Заказ готов", lambda row, text, token: chats.send_reply(conn, sender, row, text, token, NOW, "owner"), NOW, "owner")
    assert sent_to == chat_row and sender.sent == ["Заказ готов"]
    assert engine.get_task(conn, task_id)["status"] == "done"


def test_a_message_task_running_for_a_minute_is_not_taken_again(env):
    conn, chat_row, card = env
    conn.execute("INSERT INTO order_task (order_id, rule_id, entered_at, kind, arg, title, body, status, created_at, done_at) VALUES (?, 0, '2026-10-13T11:40:00', 'message', '', 'Клиенту', 'Заказ готов', 'running', ?, ?)",
                 (card, "2026-10-13T11:40:00", "2026-10-13T11:59:00"))
    task_id = conn.execute("SELECT MAX(id) FROM order_task").fetchone()[0]
    conn.commit()
    with pytest.raises(orders.OrderError, match="уже выполняется"):
        engine.send_task(conn, task_id, "Заказ готов", lambda *a: None, NOW, "owner")


def test_can_be_collected_after_an_old_interrupted_send_goes_once(env, monkeypatch):
    conn, chat_row, card = env
    entered = orders.get_card(conn, card)["entered_at"]
    claim(conn, chat_row, token="arrived-%d-%s" % (card, "".join(ch for ch in entered if ch.isdigit())))
    delivered = []
    monkeypatch.setattr(order_manager, "_client_chat", lambda c, card_id: (chats.get_chat(conn, chat_row), ""))
    monkeypatch.setattr(order_manager.templates, "render", lambda *a: ("Можно забирать", []))
    ok, _text = order_manager.tell_client_arrived(conn, card, lambda chat_, text, token: delivered.append(token), NOW, "owner")
    assert ok and len(delivered) == 1


def test_can_be_collected_with_an_old_mark_that_cannot_go_now_is_unknown_without_a_text_to_copy(env, monkeypatch):
    conn, chat_row, card = env
    entered = orders.get_card(conn, card)["entered_at"]
    claim(conn, chat_row, token="arrived-%d-%s" % (card, "".join(ch for ch in entered if ch.isdigit())))
    monkeypatch.setattr(order_manager, "_client_chat", lambda c, card_id: (chats.get_chat(conn, chat_row), ""))
    monkeypatch.setattr(order_manager.templates, "render", lambda *a: ("Можно забирать", []))
    ok, _text = order_manager.tell_client_arrived(conn, card, None, NOW, "owner")             # no keys of Avito now
    assert not ok
    [line] = [r[0] for r in conn.execute("SELECT text FROM manager_message")]
    assert "неизвестно, ушло ли" in line and order_manager.COPY_MARK not in line


def test_can_be_collected_with_a_fresh_mark_is_still_unknown(env, monkeypatch):
    conn, chat_row, card = env
    entered = orders.get_card(conn, card)["entered_at"]
    claim(conn, chat_row, token="arrived-%d-%s" % (card, "".join(ch for ch in entered if ch.isdigit())), at="2026-10-13T11:59:00")
    delivered = []
    monkeypatch.setattr(order_manager, "_client_chat", lambda c, card_id: (chats.get_chat(conn, chat_row), ""))
    monkeypatch.setattr(order_manager.templates, "render", lambda *a: ("Можно забирать", []))
    ok, _text = order_manager.tell_client_arrived(conn, card, lambda *a: delivered.append(a), NOW, "owner")
    assert not ok and delivered == []


# ---- хвост Q3 (match3, lane misc-tails): the cabinet's id is not there at the check ---------------------------------------------------

class NoCabinet(Sender):
    """get_self as Avito answers it now and then: 200 without the id; `answer`: an error to raise instead, or the real id once fixed."""

    def __init__(self, answer=None, **kw):
        super().__init__(**kw)
        self.answer = answer

    def get_self(self, retry=True):
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer or {}


def _forget_cabinet(conn, chat_row):
    from tires_sync import connections
    connections.forget_identity(conn, chats.get_chat(conn, chat_row)["account_id"])


@pytest.mark.parametrize("answer", [None, chats.AvitoError(503, None, "HTTP 503"), chats.AvitoError(429, None, "HTTP 429"), OSError("timed out")],
                         ids=["no-id", "503", "429", "network"])
def test_q3_no_cabinet_id_at_the_check_asks_to_try_again_and_keeps_the_mark(env, answer):
    conn, chat_row, card = env
    claim(conn, chat_row)
    _forget_cabinet(conn, chat_row)
    sender = NoCabinet(answer)
    with pytest.raises(chats.ChatError, match="попробуйте через минуту") as err:
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")
    assert not isinstance(err.value, chats.ChatSendUnknown)
    assert sender.sent == [] and sender.reads == 0 and status(conn) == ("sending", "")       # the chat was not read: nothing is decided
    sender.answer = {"id": 1}                                                               # a minute later Avito gives the id: checked, sent once
    chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")
    assert sender.reads == 1 and sender.sent == ["Добрый день"] and status(conn)[0] == "sent"


def test_q3_a_refused_cabinet_id_at_the_check_is_still_unknown(env):
    conn, chat_row, card = env
    claim(conn, chat_row)
    _forget_cabinet(conn, chat_row)
    sender = NoCabinet(chats.AvitoError(403, None, "forbidden"))
    with pytest.raises(chats.ChatSendUnknown):
        chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")
    assert sender.sent == [] and status(conn)[0] == "unknown"


def test_q3_the_cabinet_id_once_known_is_not_asked_at_the_check(env):
    conn, chat_row, card = env
    claim(conn, chat_row)
    sender = NoCabinet()                                                                    # the id was remembered by the reading: not asked
    chats.send_reply(conn, sender, chat_row, "Добрый день", "tok-1", NOW, "owner")
    assert sender.sent == ["Добрый день"] and status(conn)[0] == "sent"
