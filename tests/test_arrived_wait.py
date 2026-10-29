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
