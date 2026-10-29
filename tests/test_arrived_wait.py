"""«Можно забирать» that did not go by itself (the owner 2026-10-29): the order keeps it as a message (the text, «Скопировать», «Отправлено
вручную»); a chat bound later sends it once by itself; sent by hand or by itself already — never again. A fake Avito only."""
from __future__ import annotations

import pytest

from test_chats_sync import FakeClient, chat, db, msg
from test_stuck_sending import NOW, Sender
from tires_sync import chat_deals, chats, order_engine as engine, order_manager as mgr, orders

LATER = "2026-10-13T12:30:00"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(mgr.templates, "render", lambda *a: ("Заказ 5000777 приехал, можно забирать", []))     # the sample's values aside
    conn, account = db(tmp_path)
    orders.ensure_pipeline(conn, NOW)
    conn.execute("UPDATE account SET chats_enabled = 1, chats_send = 1 WHERE id = ?", (account,))
    conn.commit()
    chats.sync_account(conn, FakeClient([[chat("c1", text="Здравствуйте")]], {"c1": [msg("m1", "Здравствуйте", read=1)]}), account, NOW)
    [row] = chats.list_chats(conn)
    card = orders.create_card(conn, dict(client_name="Татьяна", webmim_number="5000777"), NOW, "o")
    at_point = conn.execute("SELECT id FROM order_stage WHERE code = 'at_point'").fetchone()[0]
    conn.execute("UPDATE order_card SET stage_id = ?, entered_at = ? WHERE id = ?", (at_point, NOW, card))
    conn.commit()
    return conn, row["id"], card


def deliver_by(conn, sender):
    return lambda row, text, token: chats.send_reply(conn, sender, row, text, token, LATER, "система")


def waiting(conn, card):
    return [t for t in engine.list_tasks(conn, card) if t["rule_id"] == engine.ARRIVED_RULE]


def test_without_a_chat_it_waits_in_the_card_with_its_text(env):
    conn, chat_row, card = env
    sent, _note = mgr.tell_client_arrived(conn, card, deliver_by(conn, Sender()), NOW, "точка")
    assert not sent
    [task] = waiting(conn, card)
    assert task["status"] == "pending" and task["kind"] == "message" and "5000777" in task["body"]
    assert any(e["level"] == "red" and "не отправлено" in e["text"] for e in orders.list_events(conn, card))


def test_a_chat_bound_later_sends_it_once(env):
    conn, chat_row, card = env
    mgr.tell_client_arrived(conn, card, None, NOW, "точка")
    chat_deals.link_chat(conn, chat_row, card, LATER, "owner")
    sender = Sender()
    line = mgr.arrived_after_link(conn, card, deliver_by(conn, sender), LATER, "owner")
    assert line.startswith("Клиенту отправлено") and len(sender.sent) == 1 and "5000777" in sender.sent[0]
    assert waiting(conn, card)[0]["status"] == "done"
    assert mgr.arrived_after_link(conn, card, deliver_by(conn, sender), LATER, "owner") == ""        # bound again: nothing more
    sent, _note = mgr.tell_client_arrived(conn, card, deliver_by(conn, sender), LATER, "точка")      # the point says «приехал» again
    assert sent and len(sender.sent) == 1


def test_sent_by_hand_is_never_sent_by_itself(env):
    conn, chat_row, card = env
    mgr.tell_client_arrived(conn, card, None, NOW, "точка")
    engine.run_task(conn, waiting(conn, card)[0]["id"], LATER, "owner")                              # «Отправлено вручную»
    chat_deals.link_chat(conn, chat_row, card, LATER, "owner")
    sender = Sender()
    assert mgr.arrived_after_link(conn, card, deliver_by(conn, sender), LATER, "owner") == ""
    mgr.tell_client_arrived(conn, card, deliver_by(conn, sender), LATER, "точка")
    assert sender.sent == []


def test_skipped_is_never_sent_by_itself(env):
    conn, chat_row, card = env
    mgr.tell_client_arrived(conn, card, None, NOW, "точка")
    engine.skip_task(conn, waiting(conn, card)[0]["id"], LATER, "owner")
    chat_deals.link_chat(conn, chat_row, card, LATER, "owner")
    sender = Sender()
    assert mgr.arrived_after_link(conn, card, deliver_by(conn, sender), LATER, "owner") == ""
    mgr.tell_client_arrived(conn, card, deliver_by(conn, sender), LATER, "точка")
    assert sender.sent == []


def test_the_order_gone_from_the_point_sends_nothing(env):
    conn, chat_row, card = env
    mgr.tell_client_arrived(conn, card, None, NOW, "точка")
    done = conn.execute("SELECT id FROM order_stage WHERE code = 'done'").fetchone()[0]
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (done, card))
    conn.commit()
    chat_deals.link_chat(conn, chat_row, card, LATER, "owner")
    sender = Sender()
    assert mgr.arrived_after_link(conn, card, deliver_by(conn, sender), LATER, "owner") == "" and sender.sent == []


def test_sent_by_itself_with_the_chat_there_leaves_no_task(env):
    conn, chat_row, card = env
    chat_deals.link_chat(conn, chat_row, card, NOW, "owner")
    sender = Sender()
    sent, _note = mgr.tell_client_arrived(conn, card, deliver_by(conn, sender), NOW, "точка")
    assert sent and len(sender.sent) == 1 and waiting(conn, card) == []


def test_a_message_lacking_values_waits_as_an_error_and_is_not_sent_by_itself(env, monkeypatch):
    conn, chat_row, card = env
    monkeypatch.setattr(mgr.templates, "render", lambda *a: ("", ["Адрес точки"]))
    mgr.tell_client_arrived(conn, card, None, NOW, "точка")
    [task] = waiting(conn, card)
    assert task["status"] == "error" and "Адрес точки" in task["result"]
    chat_deals.link_chat(conn, chat_row, card, LATER, "owner")
    sender = Sender()
    line = mgr.arrived_after_link(conn, card, deliver_by(conn, sender), LATER, "owner")
    assert "не отправлено" in line and sender.sent == []


# ---- review round 1 (2026-10-30, chats-orders) -----------------------------------------------------------------------------------------

def test_answered_in_the_inbox_by_hand_is_never_sent_by_itself(env):
    """D1: the owner copied the text from «Входящие», wrote to the client and answered the line: a chat bound later sends nothing."""
    conn, chat_row, card = env
    mgr.tell_client_arrived(conn, card, None, NOW, "точка")
    [line] = [r[0] for r in conn.execute("SELECT id FROM manager_message")]
    mgr.answer(conn, line, "написал клиенту сам", LATER)
    chat_deals.link_chat(conn, chat_row, card, LATER, "owner")
    sender = Sender()
    assert mgr.arrived_after_link(conn, card, deliver_by(conn, sender), LATER, "owner") == "" and sender.sent == []
    assert waiting(conn, card)[0]["status"] == "done"
    mgr.tell_client_arrived(conn, card, deliver_by(conn, sender), LATER, "точка")
    assert sender.sent == []


def test_one_mark_for_the_send_by_itself_by_a_press_and_after_binding(env):
    """D2: the waiting task sends with the arrival's own mark: a send after binding that Avito refused (402) is not sent again by the next
    «приехал», nor by a press in the card."""
    conn, chat_row, card = env
    mgr.tell_client_arrived(conn, card, None, NOW, "точка")
    [task] = waiting(conn, card)
    assert engine.task_token(task) == mgr.arrived_token(orders.get_card(conn, card), NOW)
    chat_deals.link_chat(conn, chat_row, card, LATER, "owner")
    sender = Sender()

    def refused(row, text, token):
        conn.execute("INSERT OR REPLACE INTO chat_send_log (token, chat_row, at, author, status, message_id) VALUES (?,?,?,?, 'retry', '')",
                     (token, row, LATER, "система"))
        conn.commit()
        raise chats.ChatSendRetry("Авито отказал (402), система повторит сама.")
    line = mgr.arrived_after_link(conn, card, refused, LATER, "owner")
    assert "неизвестно" in line
    sent, _note = mgr.tell_client_arrived(conn, card, deliver_by(conn, sender), LATER, "точка")      # the point repeats «приехал»
    assert not sent and sender.sent == []
    with pytest.raises((orders.OrderError, chats.ChatError)):
        engine.send_task(conn, task["id"], task["body"], deliver_by(conn, sender), LATER, "owner")   # a press in the card
    assert sender.sent == []



# ---- review round 2 (2026-10-30, chats-orders) -----------------------------------------------------------------------------------------

def bound_after_a_402(env):
    conn, chat_row, card = env
    mgr.tell_client_arrived(conn, card, None, NOW, "точка")
    chat_deals.link_chat(conn, chat_row, card, LATER, "owner")

    def refused(row, text, token):
        conn.execute("INSERT OR REPLACE INTO chat_send_log (token, chat_row, at, author, status, message_id) VALUES (?,?,?,?, 'retry', '')",
                     (token, row, LATER, "система"))
        conn.commit()
        raise chats.ChatSendRetry("Авито отказал (402), система повторит сама.")
    mgr.arrived_after_link(conn, card, refused, LATER, "owner")
    [task] = waiting(conn, card)
    return conn, chat_row, card, task


def test_the_sync_delivering_after_a_402_closes_the_waiting_task(env):
    """R1: the minute's sync sent it again: the task of the card is done, nobody writes it by hand."""
    conn, chat_row, card, task = bound_after_a_402(env)
    assert task["status"] == "pending"
    chats._close_task(conn, engine.task_token(task), task["body"], LATER, "повторно после 402")
    assert engine.get_task(conn, task["id"])["status"] == "done"


def test_a_skipped_task_stops_the_sync_sending_it_again(env):
    """R2: «Пропустить» after a 402: the sync does not send «можно забирать» again."""
    conn, chat_row, card, task = bound_after_a_402(env)
    engine.skip_task(conn, task["id"], LATER, "owner")
    assert chats._stopped(conn, chats.get_chat(conn, chat_row), engine.task_token(task)) == "задача пропущена"
