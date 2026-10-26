"""A stage rule prepares a message; it goes only by a press. The card: the window «Сообщения по заказу» (the owner 2026-10-23, the messages
to the point too: tests/test_order_messages.py); the board and «Чаты»: «Отправить клиенту?» with a link to that window for the others."""
from __future__ import annotations

import json
import re

from test_chats_pages import NOW, _sendable_page, setup
from panel_helpers import post
from tires_sync import chats, order_engine as engine, order_rules as rules, order_templates as tpl, orders

TOKEN = (200, {"access_token": "t", "expires_in": 3600})                  # the next press makes a new client: it asks for a token first
SENT = (200, {"id": "m-out", "created": 1_791_900_600, "direction": "out", "type": "text", "content": {"text": "x"}})


def _stage(conn, code):
    return next(s["id"] for s in orders.list_stages(conn) if s["code"] == code)


def _rule(conn, audience="client", body="Здравствуйте, {client_name}! Заказ №{order_no} принят.", code="talk"):
    t = tpl.add_template(conn, "Клиенту: принят" if audience == "client" else "Менеджеру: принят", audience, body)
    rules.add_rule(conn, _stage(conn, code), "enter", 0, "message", t, "", "ask", None)
    return t


def _ready(tmp_path, *replies, audience="client", body=None):
    """A sendable chat of Татьяна with its deal on «new» and a rule of «talk» that prepares a message."""
    app, client, tok, conn, tanya = _sendable_page(tmp_path, *replies)
    tanya = chats.get_chat(conn, tanya["id"])
    card_id = tanya["order_id"]
    assert card_id and orders.get_card(conn, card_id)["stage"]["code"] != "talk"
    _rule(conn, audience, **({"body": body} if body else {}))
    return app, client, tok, conn, tanya, card_id


def _texts(app):
    return [c for c in app.config["AVITO_HTTP"].calls if c[0] == "POST" and c[1].endswith("/messages")]


def _move_in_card(client, tok, card_id, conn):
    return client.post("/orders/%d/move" % card_id, data={"csrf": tok, "stage_id": _stage(conn, "talk")})


def _task(conn, card_id):
    return [t for t in engine.list_tasks(conn, card_id) if t["kind"] == "message"][0]


def _send(client, tok, card_id, task_id, text, fetch=True, **extra):
    headers = {"X-Requested-With": "fetch"} if fetch else {}
    return client.post("/orders/%d/task/%d/send" % (card_id, task_id), data={"csrf": tok, "text": text, **extra}, headers=headers)


def _window(page):
    """(classes, markup) of the window of the page: «Сообщения по заказу» of the card, «Отправить клиенту?» of «Чаты»; None without one."""
    m = (re.search(r'<div class="modal-backdrop send-window messages-window([^"]*)" id="order-messages".*?</form></div></div>', page, re.S)
         or re.search(r'<div class="modal-backdrop send-window([^"]*)" id="send-task-\d+".*?</form></div></div>', page, re.S))
    return m and (m.group(1), m.group(0))


def test_a_move_in_the_card_opens_the_window_once_with_the_text_and_sends_nothing(tmp_path):
    app, client, tok, conn, tanya, card_id = _ready(tmp_path)
    _move_in_card(client, tok, card_id, conn)
    page = client.get("/orders/%d" % card_id).get_data(as_text=True)
    state, win = _window(page)
    assert "is-open" in state and "Сообщения по заказу №%d" % card_id in win and "Клиенту · чат Ч-%d" % tanya["id"] in win
    assert "Заказ №%d принят." % card_id in win and "Здравствуйте, Татьяна" in win and 'name="csrf"' in win and "Позже" in win
    assert "Отправить клиенту в Авито" in win and "Отправить оба" not in win                   # one message: its own button
    assert _texts(app) == []                                                                   # nothing goes without the press
    again = client.get("/orders/%d" % card_id).get_data(as_text=True)
    state, win = _window(again)
    assert "is-open" not in state                                                              # «Не сейчас» / a reload: the task waits in the card
    assert 'data-open-modal="order-messages"' in again and "Открыть сообщения" in again and "Что ждёт действия" not in again
    assert _task(conn, card_id)["status"] == "pending"


def test_send_goes_to_the_chat_of_the_order_marks_the_task_and_keeps_the_edited_text(tmp_path):
    app, client, tok, conn, tanya, card_id = _ready(tmp_path, SENT)
    _move_in_card(client, tok, card_id, conn)
    task = _task(conn, card_id)
    res = _send(client, tok, card_id, task["id"], "Здравствуйте! Поправленный текст.", chat=str(tanya["id"])).get_json()
    assert res["ok"] and "Ч-%d" % tanya["id"] in res["message"]
    sent = _texts(app)
    assert len(sent) == 1 and tanya["chat_id"] in sent[0][1]
    assert "Поправленный текст" in json.loads(sent[0][3])["message"]["text"]
    done = engine.get_task(conn, task["id"])
    assert done["status"] == "done" and done["result"] == "отправлено клиенту, текст изменён" and done["body"] == "Здравствуйте! Поправленный текст."
    events = [e["text"] for e in orders.list_events(conn, card_id)]
    assert any(e.startswith("Отправлен ответ клиенту: Здравствуйте! Поправленный") for e in events)
    assert any("выполнено (отправлено клиенту, текст изменён)" in e for e in events)


def test_the_unchanged_text_says_sent_to_the_client(tmp_path):
    app, client, tok, conn, tanya, card_id = _ready(tmp_path, SENT)
    _move_in_card(client, tok, card_id, conn)
    task = _task(conn, card_id)
    assert _send(client, tok, card_id, task["id"], task["body"]).get_json()["ok"]
    assert engine.get_task(conn, task["id"])["result"] == "отправлено клиенту"


def test_a_double_press_sends_once(tmp_path):
    app, client, tok, conn, tanya, card_id = _ready(tmp_path, SENT, SENT)
    _move_in_card(client, tok, card_id, conn)
    task = _task(conn, card_id)
    assert _send(client, tok, card_id, task["id"], "Текст").get_json()["ok"]
    second = _send(client, tok, card_id, task["id"], "Текст").get_json()
    assert not second["ok"] and "уже" in second["message"]
    assert len(_texts(app)) == 1


def test_a_task_taken_back_after_a_lost_record_is_not_sent_again(tmp_path):
    """The message left but the task stayed «running» (a killed worker): it comes back as waiting; the next press sends nothing twice."""
    app, client, tok, conn, tanya, card_id = _ready(tmp_path, SENT, SENT)
    _move_in_card(client, tok, card_id, conn)
    task = _task(conn, card_id)
    assert _send(client, tok, card_id, task["id"], "Текст").get_json()["ok"]
    conn.execute("UPDATE order_task SET status = 'pending', result = '' WHERE id = ?", (task["id"],))
    conn.commit()
    assert _send(client, tok, card_id, task["id"], "Другой текст").get_json()["ok"]            # the send mark says «sent»: no second message
    assert len(_texts(app)) == 1
    done = engine.get_task(conn, task["id"])
    assert done["body"] != "Другой текст" and done["result"] == "отправлено клиенту раньше (текст — в чате)"      # not the text that never went


def test_a_refused_send_keeps_the_task_waiting_and_says_why_with_a_red_record(tmp_path):
    app, client, tok, conn, tanya, card_id = _ready(tmp_path, (403, {"error": {"message": "forbidden"}}), TOKEN, (200, {"messages": []}), SENT)
    # tails6: the second press reads the chat first (a copy written by hand is not sent again); the chat is empty here
    _move_in_card(client, tok, card_id, conn)
    task = _task(conn, card_id)
    res = _send(client, tok, card_id, task["id"], "Текст после сбоя").get_json()
    assert not res["ok"] and "Авито не принял сообщение" in res["message"]
    assert engine.get_task(conn, task["id"])["status"] == "pending"
    assert any("клиенту не отправлено" in e["text"] for e in orders.list_events(conn, card_id))
    assert _send(client, tok, card_id, task["id"], "Текст после сбоя").get_json()["ok"]      # a definite refusal may be sent again
    assert len(_texts(app)) == 2


def test_an_unknown_outcome_is_not_sent_again(tmp_path):
    app, client, tok, conn, tanya, card_id = _ready(tmp_path, (502, {"error": {"message": "bad gateway"}}), SENT)
    _move_in_card(client, tok, card_id, conn)
    task = _task(conn, card_id)
    assert "Неизвестно, ушло ли" in _send(client, tok, card_id, task["id"], "Текст").get_json()["message"]
    again = _send(client, tok, card_id, task["id"], "Текст").get_json()
    assert not again["ok"] and "не подтверждена" in again["message"]
    assert len(_texts(app)) == 1 and engine.get_task(conn, task["id"])["status"] == "pending"


def test_a_chat_the_window_did_not_name_gets_nothing(tmp_path):
    app, client, tok, conn, tanya, card_id = _ready(tmp_path, SENT)
    _move_in_card(client, tok, card_id, conn)
    task = _task(conn, card_id)
    ivan = next(c for c in chats.list_chats(conn) if c["peer_name"] == "Иван")
    res = _send(client, tok, card_id, task["id"], "x", chat=str(ivan["id"])).get_json()      # Иван's chat is not of this order
    assert not res["ok"] and "обновите страницу" in res["message"]
    assert _texts(app) == [] and engine.get_task(conn, task["id"])["status"] == "pending"


def test_without_csrf_nothing_goes(tmp_path):
    app, client, tok, conn, tanya, card_id = _ready(tmp_path, SENT)
    _move_in_card(client, tok, card_id, conn)
    task = _task(conn, card_id)
    assert client.post("/orders/%d/task/%d/send" % (card_id, task["id"]), data={"text": "x"}).status_code == 400
    assert _send(client, "wrong", card_id, task["id"], "x").status_code == 400
    assert _texts(app) == [] and engine.get_task(conn, task["id"])["status"] == "pending"


def test_a_task_of_another_order_is_404(tmp_path):
    app, client, tok, conn, tanya, card_id = _ready(tmp_path, SENT)
    _move_in_card(client, tok, card_id, conn)
    task = _task(conn, card_id)
    other = orders.create_card(conn, dict(client_name="Пётр", client_phone="+7 900 000 00 01"), NOW, "o")
    assert _send(client, tok, other, task["id"], "x").status_code == 404 and _texts(app) == []


def test_an_empty_text_sends_nothing_and_keeps_the_task(tmp_path):
    app, client, tok, conn, tanya, card_id = _ready(tmp_path, SENT)
    _move_in_card(client, tok, card_id, conn)
    task = _task(conn, card_id)
    assert not _send(client, tok, card_id, task["id"], "  \r\n ").get_json()["ok"]
    assert _texts(app) == [] and engine.get_task(conn, task["id"])["status"] == "pending"


def test_a_message_to_the_manager_is_in_the_window_with_the_vk_button_and_never_goes_to_the_chat(tmp_path):
    """Was: no window, only «Отправлено вручную» (the owner 2026-10-23: «Отправить точке в ВК»). The order has no point here: the button is off."""
    app, client, tok, conn, tanya, card_id = _ready(tmp_path, SENT, audience="manager")
    _move_in_card(client, tok, card_id, conn)
    page = client.get("/orders/%d" % card_id).get_data(as_text=True)
    task = _task(conn, card_id)
    state, win = _window(page)
    assert "is-open" in state and "Точке · Менеджеру: принят" in win and 'value="%d" data-send-one disabled' % task["id"] in win
    assert "У заказа нет точки" in win and "Отправлено вручную" in win                         # the manual mark stays the way out
    res = _send(client, tok, card_id, task["id"], "x").get_json()
    assert not res["ok"] and "не клиенту" in res["message"] and _texts(app) == []


def test_sending_switched_off_for_the_account_opens_the_window_to_copy_and_explains(tmp_path):
    """The owner 2026-10-21: a message that cannot go from the panel is offered to copy and mark «Отправлено вручную» (was: no window)."""
    app, client, tok, conn, tanya, card_id = _ready(tmp_path, SENT)
    post(client, "/connections/accounts/%d/modes" % tanya["account_id"], tok, chats_enabled="1", feed_enabled="1")     # reading on, sending off
    _move_in_card(client, tok, card_id, conn)
    page = client.get("/orders/%d" % card_id).get_data(as_text=True)
    task = _task(conn, card_id)
    state, win = _window(page)
    assert "is-open" in state and "выключена отправка" in win and "Отправлено вручную" in win and "Скопировать" in win
    assert 'value="%d" data-send-one disabled' % task["id"] in win and 'name="ready"' not in win
    assert not _send(client, tok, card_id, task["id"], "x").get_json()["ok"] and _texts(app) == []


def test_an_order_without_a_chat_opens_the_window_to_copy_and_explains(tmp_path):
    app, client, tok, conn, account = setup(tmp_path)
    _rule(conn)
    card_id = orders.create_card(conn, dict(client_name="Пётр", client_phone="+7 900 000 00 01"), NOW, "o")
    _move_in_card(client, tok, card_id, conn)
    page = client.get("/orders/%d" % card_id).get_data(as_text=True)
    state, win = _window(page)
    assert "is-open" in state and "нет чата Авито" in win and "Отправлено вручную" in win and 'data-send-one disabled' in win


def test_a_message_lacking_values_does_not_open_the_window_and_says_what_it_lacks(tmp_path):
    app, client, tok, conn, tanya, card_id = _ready(tmp_path, SENT, body="Доставка {delivery_date}")
    orders.update_card(conn, card_id, {"delivery_date": ""}, NOW, "o")        # a chat's order comes with tomorrow (the owner 2026-10-26)
    _move_in_card(client, tok, card_id, conn)
    task = _task(conn, card_id)
    assert task["status"] == "error"
    page = client.get("/orders/%d" % card_id).get_data(as_text=True)
    state, win = _window(page)
    assert "is-open" not in state and "Не хватает данных. Заполните в окне заказа: Дата доставки." in win
    assert 'value="%d" data-send-one disabled' % task["id"] in win and "Не хватает данных: см. в окне." in page
    assert not _send(client, tok, card_id, task["id"], "x").get_json()["ok"] and _texts(app) == []


def test_a_move_in_the_chat_opens_the_window_for_this_chat(tmp_path):
    app, client, tok, conn, tanya, card_id = _ready(tmp_path, SENT)
    client.post("/chats/%d/stage" % tanya["id"], data={"csrf": tok, "stage_id": _stage(conn, "talk")})
    page = client.get("/chats/%d" % tanya["id"]).get_data(as_text=True)
    state, win = _window(page)
    assert "is-open" in state and "Ч-%d" % tanya["id"] in win and 'name="chat" value="%d"' % tanya["id"] in win
    assert 'action="/orders/%d/task/%d/send"' % (card_id, _task(conn, card_id)["id"]) in win and _texts(app) == []
    assert _window(client.get("/chats/%d" % tanya["id"]).get_data(as_text=True)) is None        # once


def _board_move(client, tok, card_id, stage_id):
    return client.post("/orders/%d/move" % card_id, data={"csrf": tok, "stage_id": stage_id}, headers={"X-Requested-With": "fetch"}).get_json()


def test_a_board_move_answers_with_the_window_and_sends_nothing(tmp_path):
    app, client, tok, conn, tanya, card_id = _ready(tmp_path, SENT)
    res = _board_move(client, tok, card_id, _stage(conn, "talk"))
    task = _task(conn, card_id)
    assert res["ok"] and res["send"] == {"url": "/orders/%d/task/%d/send" % (card_id, task["id"]), "task": task["id"], "card": card_id,
                                         "title": task["title"], "client": orders.get_card(conn, card_id)["client_name"], "ref": "Ч-%d" % tanya["id"],
                                         "chat": tanya["id"], "body": task["body"]}
    assert _texts(app) == []
    assert "is-open" not in _window(client.get("/orders/%d" % card_id).get_data(as_text=True))[0]       # the board showed it: the card does not again
    assert "more" not in res["send"]                                                           # nothing else waits
    assert _send(client, tok, card_id, task["id"], task["body"], chat=str(res["send"]["chat"])).get_json()["ok"] and len(_texts(app)) == 1


def test_a_board_move_to_a_message_that_cannot_go_says_why_on_the_board(tmp_path):
    app, client, tok, conn, tanya, card_id = _ready(tmp_path, SENT)
    post(client, "/connections/accounts/%d/modes" % tanya["account_id"], tok, chats_enabled="1", feed_enabled="1")     # sending off
    res = _board_move(client, tok, card_id, _stage(conn, "talk"))
    assert res["ok"] and res["send"] == {"more": "/orders/%d?messages=1#messages" % card_id, "others": ["Клиенту: принят"], "card": card_id}
    assert "отправить его из панели нельзя" in client.get("/orders").get_data(as_text=True)          # and the link to the window (the lead 2026-10-23)


def test_a_board_move_to_a_message_for_the_manager_offers_the_link_to_the_window(tmp_path):
    """Was: nothing (the message to the point waited unseen). The lead 2026-10-23: a link «Открыть сообщения заказа»."""
    app, client, tok, conn, tanya, card_id = _ready(tmp_path, SENT, audience="manager")
    res = _board_move(client, tok, card_id, _stage(conn, "talk"))
    assert res["ok"] and res["send"] == {"more": "/orders/%d?messages=1#messages" % card_id, "others": ["Менеджеру: принят"], "card": card_id}
    assert "нельзя" not in client.get("/orders").get_data(as_text=True)
    state, win = _window(client.get(res["send"]["more"]).get_data(as_text=True))                # the link opens the window of the card
    assert "is-open" in state and "Менеджеру: принят" in win


def test_two_quick_moves_offer_only_the_message_of_the_last_one(tmp_path):
    from datetime import datetime
    app, client, tok, conn, tanya, card_id = _ready(tmp_path, SENT)
    t2 = tpl.add_template(conn, "Клиенту: ждём", "client", "Ждём товар, {client_name}.")
    rules.add_rule(conn, _stage(conn, "new"), "enter", 0, "message", t2, "", "ask", None)
    app.config["NOW"] = lambda: datetime(2026, 10, 13, 9, 0, 0)
    first = _board_move(client, tok, card_id, _stage(conn, "talk"))["send"]
    app.config["NOW"] = lambda: datetime(2026, 10, 13, 9, 0, 1)
    second = _board_move(client, tok, card_id, _stage(conn, "new"))["send"]
    assert first["title"] == "Клиенту: принят" and second["title"] == "Клиенту: ждём" and second["task"] != first["task"]
    app.config["NOW"] = lambda: datetime(2026, 10, 13, 9, 0, 2)
    _move_in_card(client, tok, card_id, conn)                                                    # the card: «talk», then «wait» a second later
    app.config["NOW"] = lambda: datetime(2026, 10, 13, 9, 0, 3)
    client.post("/orders/%d/move" % card_id, data={"csrf": tok, "stage_id": _stage(conn, "new")})
    page = client.get("/orders/%d" % card_id).get_data(as_text=True)
    state, win = _window(page)
    assert "is-open" in state and "Ждём товар" in win and win.count('data-message="') == 4      # the window shows every waiting message
    assert len([t for t in engine.list_tasks(conn, card_id, ("pending",)) if t["kind"] == "message"]) == 4      # the earlier ones wait in the card


def test_the_plain_form_without_script_redirects_back(tmp_path):
    app, client, tok, conn, tanya, card_id = _ready(tmp_path, SENT)
    _move_in_card(client, tok, card_id, conn)
    task = _task(conn, card_id)
    res = _send(client, tok, card_id, task["id"], "Текст", fetch=False, back="/chats/%d" % tanya["id"])
    assert res.status_code == 302 and res.headers["Location"].endswith("/chats/%d" % tanya["id"])
    res = _send(client, tok, card_id, task["id"], "Текст", fetch=False, back="https://evil.example/")
    assert res.headers["Location"].endswith("/orders/%d" % card_id)


def test_a_second_press_while_the_first_is_sending_sends_nothing(tmp_path):
    """Two tabs at once: the second press comes while the first one is still talking to Avito."""
    import pytest
    app, client, tok, conn, tanya, card_id = _ready(tmp_path)
    _move_in_card(client, tok, card_id, conn)
    task = _task(conn, card_id)
    sent, inner = [], []

    def deliver(chat_row, text, token):
        try:
            engine.send_task(conn, task["id"], text, lambda *a: sent.append(a), NOW, "o")
        except orders.OrderError as err:
            inner.append(str(err))
        sent.append((chat_row, text, token))
    engine.send_task(conn, task["id"], "Текст", deliver, NOW, "o")
    assert len(sent) == 1 and inner and "уже" in inner[0]
    assert engine.get_task(conn, task["id"])["status"] == "done"
    with pytest.raises(orders.OrderError):
        engine.send_task(conn, task["id"], "Текст", deliver, NOW, "o")


def test_the_live_refresh_of_the_chats_page_does_not_take_the_window_away(tmp_path):
    app, client, tok, conn, tanya, card_id = _ready(tmp_path, SENT)
    client.post("/chats/%d/stage" % tanya["id"], data={"csrf": tok, "stage_id": _stage(conn, "talk")})
    live = client.get("/chats/%d" % tanya["id"], headers={"X-Requested-With": "fetch"}).get_data(as_text=True)
    assert _window(live) is None
    assert "is-open" in _window(client.get("/chats/%d" % tanya["id"]).get_data(as_text=True))[0]


def test_the_chat_says_why_a_prepared_message_cannot_go_from_the_panel(tmp_path):
    app, client, tok, conn, tanya, card_id = _ready(tmp_path, SENT)
    post(client, "/connections/accounts/%d/modes" % tanya["account_id"], tok, chats_enabled="1", feed_enabled="1")     # sending off
    client.post("/chats/%d/stage" % tanya["id"], data={"csrf": tok, "stage_id": _stage(conn, "talk")})
    page = client.get("/chats/%d" % tanya["id"]).get_data(as_text=True)
    assert _window(page) is None and "отправить его из панели нельзя" in page and "выключена отправка" in page
    assert 'href="/orders/%d">заказе №%d' % (card_id, card_id) in page and _texts(app) == []


def test_a_board_move_that_cannot_prepare_the_window_still_says_moved(tmp_path, monkeypatch):
    app, client, tok, conn, tanya, card_id = _ready(tmp_path, SENT)

    def broken(*a, **k):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(engine, "new_messages", broken)
    res = _board_move(client, tok, card_id, _stage(conn, "talk"))
    assert res["ok"] and res["send"] is None and res["message"] == "Заказ перенесён." and _texts(app) == []
