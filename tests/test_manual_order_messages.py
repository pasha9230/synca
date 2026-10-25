"""Messages of a hand-made order (the owner 2026-10-21): «Отправить клиенту?» right after «Новый заказ», the field «Чат Авито» binds the chat
(our number, an Avito link, or the chat found by the client's phone on a press), a chat of another order is never moved silently,
without a chat the window gives the text to copy and «Отправлено вручную»."""
from __future__ import annotations

import json
import re

from test_chats_pages import _sendable_page
from test_roles import person
from test_send_popup import SENT, _texts, _window
from panel_helpers import post
from tires_sync import chats, clients, order_engine as engine, order_points as points, order_rules as rules, order_templates as tpl, orders


def ready(tmp_path, *replies):
    """A sendable account with the chats of Татьяна (her deal made from the chat) and Иван (free), and a rule of the first stage that
    prepares a message to the client."""
    app, client, tok, conn, tanya = _sendable_page(tmp_path, *replies)
    ivan = next(c for c in chats.list_chats(conn) if c["peer_name"] == "Иван")
    conn.execute("UPDATE avito_chat SET order_id = NULL WHERE id = ?", (ivan["id"],))
    conn.commit()
    new = {s["code"]: s["id"] for s in orders.list_stages(conn)}["new"]
    t = tpl.add_template(conn, "Клиенту: принят", "client", "Здравствуйте, {client_name}! Заказ №{order_no} принят.")
    rules.add_rule(conn, new, "enter", 0, "message", t, "", "ask", None)
    return app, client, tok, conn, chats.get_chat(conn, tanya["id"]), chats.get_chat(conn, ivan["id"])


def make(client, tok, **fields) -> int:
    r = post(client, "/orders/new", tok, client_name="Иван", **fields)
    assert r.status_code == 302, r.get_data(as_text=True)[:300]
    return int(re.search(r"/orders/(\d+)$", r.headers["Location"]).group(1))


def message(conn, card_id):
    return [t for t in engine.list_tasks(conn, card_id) if t["kind"] == "message"][0]


def page(client, card_id) -> str:
    return client.get("/orders/%d" % card_id).get_data(as_text=True)


def audit(conn, action):
    return [json.loads(r[0]) for r in conn.execute("SELECT detail FROM audit WHERE action = ? ORDER BY id", (action,))]


def test_a_new_order_opens_the_send_window_with_the_message_of_the_first_stage_and_sends_nothing(tmp_path):
    app, client, tok, conn, tanya, ivan = ready(tmp_path)
    cid = make(client, tok, avito_chat="Ч-%d" % ivan["id"])
    state, win = _window(page(client, cid))
    assert "is-open" in state and "Сообщения по заказу" in win and "чат Ч-%d" % ivan["id"] in win and "Заказ №%d принят." % cid in win
    assert _texts(app) == [] and message(conn, cid)["status"] == "pending"                    # nothing goes without the press
    state, _ = _window(page(client, cid))
    assert "is-open" not in state                                                              # once


def test_the_chat_number_binds_the_chat_and_the_send_goes_to_it(tmp_path):
    app, client, tok, conn, tanya, ivan = ready(tmp_path, SENT)
    cid = make(client, tok, avito_chat="Ч-%d" % ivan["id"])
    assert chats.get_chat(conn, ivan["id"])["order_id"] == cid
    assert any("Привязан чат Ч-%d" % ivan["id"] in e["text"] for e in orders.list_events(conn, cid))
    assert audit(conn, "order_chat_link")[-1] == {"order": cid, "chat": ivan["id"], "how": "field"}
    task = message(conn, cid)
    r = client.post("/orders/%d/task/%d/send" % (cid, task["id"]), data={"csrf": tok, "text": task["body"], "chat": str(ivan["id"])},
                    headers={"X-Requested-With": "fetch"})
    assert r.get_json()["ok"]
    sent = _texts(app)
    assert len(sent) == 1 and ivan["chat_id"] in sent[0][1]


def test_an_avito_link_of_the_chat_binds_it_from_the_card(tmp_path):
    app, client, tok, conn, tanya, ivan = ready(tmp_path)
    cid = make(client, tok)
    post(client, "/orders/%d/save" % cid, tok, avito_chat="https://www.avito.ru/profile/messenger/channel/%s" % ivan["chat_id"])
    assert chats.get_chat(conn, ivan["id"])["order_id"] == cid
    state, win = _window(page(client, cid))                                                    # the waiting message is offered for the chat bound
    assert "is-open" in state and "чат Ч-%d" % ivan["id"] in win
    assert _texts(app) == []


def test_a_chat_of_another_order_is_not_moved_without_a_press(tmp_path):
    app, client, tok, conn, tanya, ivan = ready(tmp_path)
    other = tanya["order_id"]
    cid = make(client, tok, avito_chat="Ч-%d" % tanya["id"])
    assert chats.get_chat(conn, tanya["id"])["order_id"] == other
    body = page(client, cid)
    assert "уже в заказе №%d" % other in body and 'name="chat_move" value="%d"' % tanya["id"] in body
    assert audit(conn, "order_chat_link") == []
    post(client, "/orders/%d/save" % cid, tok, chat_move=str(tanya["id"]))
    assert chats.get_chat(conn, tanya["id"])["order_id"] == cid
    assert any("перенесён в заказ №%d" % cid in e["text"] for e in orders.list_events(conn, other))
    assert audit(conn, "order_chat_link")[-1] == {"order": cid, "chat": tanya["id"], "how": "moved", "from": other}


def test_an_unknown_chat_is_said_and_the_field_is_kept(tmp_path):
    app, client, tok, conn, tanya, ivan = ready(tmp_path)
    cid = make(client, tok, avito_chat="Ч-99999")
    assert orders.get_card(conn, cid)["avito_chat"] == "Ч-99999"
    assert "Чат Ч-99999 не найден" in page(client, cid)


def test_the_chat_found_by_the_phone_is_only_offered_and_bound_by_a_press(tmp_path):
    app, client, tok, conn, tanya, ivan = ready(tmp_path)
    client_id = clients.attach_chat(conn, ivan["id"], None, "2026-10-13T12:00:00", "o")             # the client card of the chat (by its Avito user)
    clients._add_phone(conn, client_id, clients.normalize("+7 900 555-11-22"))
    conn.commit()
    cid = make(client, tok, client_phone="8 (900) 555-11-22")
    body = page(client, cid)
    assert "Найден чат по телефону клиента: Ч-%d" % ivan["id"] in body and 'name="chat_bind" value="%d"' % ivan["id"] in body
    assert chats.get_chat(conn, ivan["id"])["order_id"] is None                                # only offered
    post(client, "/orders/%d/save" % cid, tok, chat_bind=str(ivan["id"]))
    assert chats.get_chat(conn, ivan["id"])["order_id"] == cid and audit(conn, "order_chat_link")[-1]["how"] == "phone"
    assert "Найден чат по телефону" not in page(client, cid)


def test_a_chat_is_unlinked_from_the_order_window(tmp_path):
    app, client, tok, conn, tanya, ivan = ready(tmp_path)
    cid = make(client, tok, avito_chat="Ч-%d" % ivan["id"])
    assert 'name="chat_unlink" value="%d"' % ivan["id"] in page(client, cid)
    post(client, "/orders/%d/save" % cid, tok, chat_unlink=str(ivan["id"]))
    assert chats.get_chat(conn, ivan["id"])["order_id"] is None and audit(conn, "order_chat_unlink")[-1] == {"order": cid, "chat": ivan["id"]}
    post(client, "/orders/%d/save" % cid, tok, chat_unlink=str(tanya["id"]))                 # a chat of another order: nothing
    assert chats.get_chat(conn, tanya["id"])["order_id"] == tanya["order_id"]


def test_without_a_chat_the_window_gives_the_text_to_copy_and_the_manual_mark(tmp_path):
    app, client, tok, conn, tanya, ivan = ready(tmp_path)
    cid = make(client, tok)
    task = message(conn, cid)
    body = page(client, cid)
    state, win = _window(body)
    assert "is-open" in state and "Сообщения по заказу" in win and "нет чата Авито" in win
    assert "Заказ №%d принят." % cid in win and "Скопировать" in win and "Отправлено вручную" in win
    assert 'formaction="/orders/%d/task/%d/run"' % (cid, task["id"]) in win and 'value="%d" data-send-one disabled' % task["id"] in win
    assert 'data-open-modal="order-messages"' in body
    post(client, "/orders/%d/task/%d/run" % (cid, task["id"]), tok)
    assert engine.get_task(conn, task["id"])["status"] == "done" and _texts(app) == []


def test_a_point_manager_does_not_bind_chats(tmp_path):
    app, client, tok, conn, tanya, ivan = ready(tmp_path)
    pid = points.save_point(conn, {"name": "Гамма", "manager_name": "А", "channel": "vk"})
    cid = make(client, tok, point_id=str(pid))
    manager, _ = person(app, "point", pid)
    r = manager.post("/orders/%d/save" % cid, data={"csrf": "tok", "chat_bind": str(ivan["id"]), "avito_chat": "Ч-%d" % ivan["id"]})
    assert r.status_code == 403 and chats.get_chat(conn, ivan["id"])["order_id"] is None      # the window is not his to save (the route checks too)
    assert "chat_bind" not in manager.get("/orders/%d" % cid).get_data(as_text=True)


def test_a_chat_of_the_same_client_in_another_order_is_not_offered(tmp_path):
    app, client, tok, conn, tanya, ivan = ready(tmp_path)
    client_id = clients.client_of_chat(conn, tanya["id"])["id"]
    clients._add_phone(conn, client_id, clients.normalize("+7 900 777-11-22"))
    conn.commit()
    cid = make(client, tok, client_phone="+7 900 777-11-22")
    assert "Найден чат" not in page(client, cid) and chats.get_chat(conn, tanya["id"])["order_id"] == tanya["order_id"]


# ---- «можно забирать» that did not go: the text to copy in «Входящие» (the lead widened the fence 2026-10-21) --------------------------

def test_a_failed_arrival_message_leaves_its_text_to_copy_in_the_inbox(tmp_path):
    from test_order_manager import NOW as MNOW, card_at, setup as manager_setup
    from tires_sync import order_manager as mgr
    conn, pid, _ = manager_setup(tmp_path)
    tpl.add_template(conn, mgr.ARRIVED_TEMPLATE, "client", "Здравствуйте, {client_name}! Заказ №{order_no} можно забирать.")
    card = card_at(conn, pid, "at_point")
    ok, _said = mgr.tell_client_arrived(conn, card, None, MNOW, "бот")
    [row] = mgr.list_inbox(conn)
    assert not ok and "нет чата Авито" in row["text"] and "Текст клиенту" not in row["text"]
    assert row["client_text"] and "Иван" in row["client_text"] and row["client_text"] not in row["text"]


def test_a_failed_arrival_message_lacking_values_names_them_and_gives_no_text(tmp_path):
    from test_order_manager import NOW as MNOW, card_at, setup as manager_setup
    from tires_sync import order_manager as mgr
    conn, pid, _ = manager_setup(tmp_path)
    tpl.add_template(conn, mgr.ARRIVED_TEMPLATE, "client", "Заказ №{order_no} ждёт вас, перемещение {transfer_number}.")
    card = card_at(conn, pid, "at_point")
    mgr.tell_client_arrived(conn, card, None, MNOW, "бот")
    [row] = mgr.list_inbox(conn)
    assert "не хватает" in row["text"] and row["client_text"] == ""


def test_the_inbox_shows_the_text_with_a_copy_button(tmp_path):
    app, client, tok, conn, tanya, ivan = ready(tmp_path)
    from tires_sync import order_manager as mgr
    pid = points.save_point(conn, {"name": "Гамма", "manager_name": "А", "channel": "vk"})
    tpl.add_template(conn, mgr.ARRIVED_TEMPLATE, "client", "Здравствуйте, {client_name}! Заказ №{order_no} можно забирать.")
    cid = make(client, tok, point_id=str(pid))
    mgr.tell_client_arrived(conn, cid, None, "2026-10-13T12:00:00", "бот")
    body = client.get("/orders/inbox").get_data(as_text=True)
    m = re.search(r'<textarea[^>]*id="inbox-copy-(\d+)"[^>]*>([^<]*)</textarea>', body)
    assert m and "Иван" in m.group(2) and 'data-copy="inbox-copy-%s"' % m.group(1) in body


def test_no_text_to_copy_when_it_may_have_reached_the_client(tmp_path, monkeypatch):
    """A send whose outcome is unknown, or that another thread is making: a copy sent by hand would be a second message."""
    from test_order_manager import NOW as MNOW, card_at, setup as manager_setup
    from tires_sync import order_manager as mgr
    conn, pid, _ = manager_setup(tmp_path)
    tpl.add_template(conn, mgr.ARRIVED_TEMPLATE, "client", "Здравствуйте, {client_name}! Заказ №{order_no} можно забирать.")
    monkeypatch.setattr(mgr, "_client_chat", lambda conn, card_id: (5, ""))

    def unknown(chat_row, text, token):
        raise mgr.chats.ChatSendUnknown("no answer")

    def busy(chat_row, text, token):
        conn.execute("INSERT INTO chat_send_log (token, chat_row, at, author, status) VALUES (?, ?, ?, 'бот', 'sending')", (token, chat_row, MNOW))
        conn.commit()
        raise mgr.chats.ChatError("Это сообщение уже отправляется.")
    for deliver in (unknown, busy):
        card = card_at(conn, pid, "at_point")
        ok, _said = mgr.tell_client_arrived(conn, card, deliver, MNOW, "бот")
        row = [r for r in mgr.list_inbox(conn) if r["text"].startswith("Заказ №%d:" % card)][0]
        assert not ok and "повторно не отправляйте" in row["text"] and row["client_text"] == ""


# ---- the review 2026-10-21 -------------------------------------------------------------------------------------------------------------

def test_a_moved_chat_leaves_no_call_back_in_the_old_order_and_an_unlinked_link_does_not_come_back(tmp_path):
    app, client, tok, conn, tanya, ivan = ready(tmp_path)
    other = tanya["order_id"]
    orders.update_card(conn, other, {"avito_chat": "Ч-%d" % tanya["id"]}, "2026-10-13T12:00:00", "o")
    cid = make(client, tok, avito_chat="Ч-%d" % tanya["id"])
    post(client, "/orders/%d/save" % cid, tok, chat_move=str(tanya["id"]))
    assert orders.get_card(conn, other)["avito_chat"] == "" and 'name="chat_move"' not in page(client, other)
    link = "https://www.avito.ru/profile/messenger/channel/%s" % ivan["chat_id"]
    second = make(client, tok, avito_chat=link)
    post(client, "/orders/%d/save" % second, tok, chat_unlink=str(ivan["id"]))
    assert orders.get_card(conn, second)["avito_chat"] == "" and "Найден чат" not in page(client, second)


def test_an_ad_link_in_the_field_is_not_called_a_missing_chat(tmp_path):
    app, client, tok, conn, tanya, ivan = ready(tmp_path)
    cid = make(client, tok, avito_chat="https://www.avito.ru/sankt-peterburg/zapchasti/shiny_123")
    assert "не найден" not in page(client, cid)


def test_the_offer_names_the_client_and_the_ad_of_the_chat(tmp_path):
    app, client, tok, conn, tanya, ivan = ready(tmp_path)
    client_id = clients.attach_chat(conn, ivan["id"], None, "2026-10-13T12:00:00", "o")
    clients._add_phone(conn, client_id, clients.normalize("+7 900 555-11-22"))
    conn.commit()
    body = page(client, make(client, tok, client_phone="+7 900 555-11-22"))
    offer = body[body.index("Найден чат"):]
    offer = offer[:offer.index("</div>")]
    assert "Иван" in offer and (ivan["item_title"] or "Иван") in offer


def test_a_chat_taken_by_a_claim_meanwhile_is_busy_not_another_order(tmp_path, monkeypatch):
    app, client, tok, conn, tanya, ivan = ready(tmp_path)
    cid = make(client, tok)
    from tires_sync import chat_deals
    stale = chats.get_chat(conn, ivan["id"])
    conn.execute("UPDATE avito_chat SET order_id = -1 WHERE id = ?", (ivan["id"],))           # a deal is being made for the chat right now
    conn.commit()
    reads = iter([stale])
    real = chat_deals.chats_store.get_chat
    monkeypatch.setattr(chat_deals.chats_store, "get_chat", lambda c, row: next(reads, None) or real(c, row))
    assert chat_deals.bind_chat(conn, cid, ivan["id"], "field", "2026-10-13T12:00:00", "o")["state"] == "busy"
    assert chats.get_chat(conn, ivan["id"])["order_id"] == -1


def test_a_failure_after_the_take_leaves_the_chat_free_and_no_audit(tmp_path, monkeypatch):
    """The binding, its audit and its journal record are one transaction (the review, round 2)."""
    app, client, tok, conn, tanya, ivan = ready(tmp_path)
    cid = make(client, tok)
    from tires_sync import chat_deals

    def broken(*args):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(chat_deals, "_attach", broken)
    try:
        chat_deals.bind_chat(conn, cid, ivan["id"], "field", "2026-10-13T12:00:00", "o")
    except RuntimeError:
        pass
    conn.commit()                                                                            # whatever the caller does next
    assert chats.get_chat(conn, ivan["id"])["order_id"] is None and audit(conn, "order_chat_link") == []
