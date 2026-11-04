"""The draft of a message that lacks a value (the owner 2026-11-04: a client got «[Номер заказа WebMIM]» copied by hand): read-only, with a warning,
nothing to copy; and no way of sending lets a mark out."""
from __future__ import annotations

import pytest

from test_chats_pages import _sendable_page
from test_send_popup import _move_in_card, _ready, _task, _texts, _window
from panel_helpers import post
from tires_sync import chats, order_engine as engine, order_templates as templates, orders

LATER = "2026-10-29T11:00:00"


# ---- the draft -----------------------------------------------------------------------------------------------------------------------------

def _lacking_window(tmp_path):
    app, client, tok, conn, tanya, card_id = _ready(tmp_path, body="Заказ {webmim_number}, перемещение {transfer_number}")
    _move_in_card(client, tok, card_id, conn)
    task = _task(conn, card_id)
    assert task["status"] == "error"
    page = client.get("/orders/%d" % card_id).get_data(as_text=True)
    return app, client, tok, conn, card_id, task, _window(page)[1]


def test_the_draft_is_read_only_with_the_warning_and_without_copy(tmp_path):
    app, client, tok, conn, card_id, task, win = _lacking_window(tmp_path)
    assert "Не отправляйте этот текст: сначала заполните Номер заказа WebMIM, Номер перемещения." in win
    assert "[Номер заказа WebMIM]" in win and "data-draft" in win and "readonly data-draft" in win
    assert "Скопировать" not in win and "data-copy" not in win
    assert 'data-focus-field="webmim_number"' in win and 'data-focus-field="transfer_number"' in win
    assert "Отправлено вручную" in win
    assert 'name="text_%d"' % task["id"] not in win.split("data-draft")[0].rsplit("<textarea", 1)[-1]        # the draft goes with no form


def test_copy_comes_back_when_nothing_lacks(tmp_path):
    app, client, tok, conn, card_id, task, win = _lacking_window(tmp_path)
    orders.update_card(conn, card_id, {"webmim_number": "111", "transfer_number": "T-1"}, LATER, "o")
    state, ready = _window(client.get("/orders/%d" % card_id).get_data(as_text=True))
    assert "Скопировать" in ready and "Не отправляйте" not in ready and "Заказ 111, перемещение T-1" in ready


def test_marked_by_hand_still_works_while_it_lacks(tmp_path):
    app, client, tok, conn, card_id, task, win = _lacking_window(tmp_path)
    post(client, "/orders/%d/task/%d/run" % (card_id, task["id"]), tok)
    assert engine.get_task(conn, task["id"])["status"] == "done" and _texts(app) == []


# ---- no way of sending lets a mark out --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, found", [
    ("Заказ [Номер заказа WebMIM] оформлен", ["[Номер заказа WebMIM]"]),
    ("[Номер перемещения]", ["[Номер перемещения]"]),
    ("[Номер чего-то нового]", ["[Номер чего-то нового]"]),
    ("Срок [Резерв до] и [Клиент]", ["[Резерв до]", "[Клиент]"]),
    ("Заказ 5254033 оформлен [см. чат]", []),
    ("Номер заказа WebMIM 5254033", []),
    ("", []),
])
def test_unfilled_finds_the_marks_of_a_draft_only(text, found):
    assert templates.unfilled(text) == found


def test_every_label_of_a_draft_is_a_mark():
    assert all(templates.unfilled("[%s]" % label) for label in templates.VARIABLES.values())


def test_the_window_refuses_a_text_with_a_mark(tmp_path):
    app, client, tok, conn, tanya, card_id = _ready(tmp_path)
    _move_in_card(client, tok, card_id, conn)
    task = _task(conn, card_id)
    assert task["status"] == "pending"
    answer = client.post("/orders/%d/messages/send" % card_id, data={
        "csrf": tok, "task": str(task["id"]), "whom_%d" % task["id"]: "client", "chat_%d" % task["id"]: str(tanya["id"]),
        "text_%d" % task["id"]: "Заказ [Номер заказа WebMIM] оформлен"}, headers={"X-Requested-With": "fetch"}).get_json()
    assert answer["ok"] is False and "незаполненные поля [Номер заказа WebMIM]" in answer["results"][0]["message"]
    assert _texts(app) == [] and engine.get_task(conn, task["id"])["status"] == "pending"


def test_a_task_send_and_a_point_send_refuse_before_taking_the_task(tmp_path):
    app, client, tok, conn, tanya, card_id = _ready(tmp_path)
    _move_in_card(client, tok, card_id, conn)
    task = _task(conn, card_id)
    called = []
    with pytest.raises(orders.OrderError, match="незаполненные поля"):
        engine.send_task(conn, task["id"], "№[Номер перемещения]", lambda *a: called.append(a), LATER, "o")
    with pytest.raises(orders.OrderError, match="незаполненные поля"):
        engine.send_point_task(conn, task["id"], "№[Номер перемещения]", lambda *a, **k: called.append(a), LATER, "o")
    assert called == [] and engine.get_task(conn, task["id"])["status"] == "pending"


def test_a_reply_in_the_chat_refuses_a_pasted_draft_and_marks_nothing(tmp_path):
    app, client, tok, conn, tanya = _sendable_page(tmp_path)
    class NoClient:
        def send_message(self, *_a):
            raise AssertionError("never sent")
    with pytest.raises(chats.ChatError, match="незаполненные поля"):
        chats.send_reply(conn, NoClient(), tanya["id"], "Заказ [Номер заказа WebMIM] оформлен", "tok-1", LATER, "o")
    assert conn.execute("SELECT COUNT(*) FROM chat_send_log WHERE token = 'tok-1'").fetchone()[0] == 0


def test_an_answer_to_a_point_in_the_inbox_refuses_a_pasted_draft(tmp_path):
    from tires_sync import order_manager as mgr, order_points as points
    app, client, tok, conn, _tanya = _sendable_page(tmp_path)
    pid = points.save_point(conn, dict(name="Бета", channel="vk"))
    conn.execute("UPDATE point SET vk_peer_id = '111' WHERE id = ?", (pid,))
    conn.commit()
    mgr.handle_message(conn, "q1", "111", "Когда завоз?", "2026-10-12T12:00:00")
    [m] = mgr.list_inbox(conn)
    post(client, "/orders/inbox/%d/answer" % m["id"], tok, text="Заказ [Номер заказа WebMIM]")
    assert not mgr.list_inbox(conn)[0]["answer"]
    post(client, "/orders/inbox/%d/answer" % m["id"], tok, text="Завтра")
    assert mgr.list_inbox(conn)[0]["answer"] == "Завтра"
