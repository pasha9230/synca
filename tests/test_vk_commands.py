"""The points write to the VK community: «приехал», «выдал», «клиент не пришёл». The bot acts on an unambiguous command, answers the point in VK,
tells the client «можно забирать» on the arrival only; anything doubtful waits in «Входящие» for the owner and reaches no client."""
from __future__ import annotations

import json
import urllib.parse

from http_fakes import ScriptedHttp
from panel_helpers import post
from test_chats_pages import NOW, _sendable_page
from tires_sync import order_manager as mgr, order_points as points, order_templates as tpl, orders, vk_callback

GROUP, SECRET, PEER = "220945892", "Sup3r-Secret_key", "555"
SENT = (200, {"id": "m-out", "created": 1_791_900_600, "direction": "out", "type": "text", "content": {"text": "x"}})
POINT = dict(name="Бета", channel="vk", vk_peer_id=PEER, address="Санкт-Петербург, ул. Генерала Примерова, 35А", hours="ежедневно 10:00–20:00",
             map_url="https://yandex.ru/maps/-/CDabc")


def _stage(conn, code):
    return next(s["id"] for s in orders.list_stages(conn) if s["code"] == code)


def _card(conn, pid, card=None, code="moving", webmim="5000100"):
    """An order of the point standing on `code`, with everything «Заказ оформлен» needs; the deal of a chat when `card` is given."""
    if card is None:
        card = orders.create_card(conn, dict(client_name="Иван"), NOW, "o")
    orders.update_card(conn, card, dict(client_name="Татьяна", client_phone="+7 900 111 22 33", point_id=pid, delivery_date="2026-10-15",
                                        webmim_number=webmim), NOW, "o")
    orders.add_item(conn, card, goods_id="1", name="Nokian Hakkapeliitta 10", brand_key="nokian", season="winter", diameter=16, territory="1", qty=4,
                    sale_unit=5000, supplier_price=4000, terms=None, fee_pct=0, promo_pct=0, now=NOW, author="o")
    for step in ("placed", "moving", "at_point")[: ("placed", "moving", "at_point").index(code) + 1]:
        orders.move_card(conn, card, _stage(conn, step), NOW, "o")
    return card


def ready(tmp_path, *avito):
    """The panel with a sendable chat of Татьяна, the VK community set up and the point Бета whose manager is VK 555;
    Татьяна's deal is the order 5000100 on «Перемещение на точку»."""
    app, client, tok, conn, tanya = _sendable_page(tmp_path, *avito)
    post(client, "/connections/vk", tok, group_id=GROUP, confirmation="3d6defda", callback_secret=SECRET, token="vk1.a.COMMUNITYKEY")
    app.config["VK_HTTP"] = ScriptedHttp(*[(200, {"response": n}) for n in range(1, 20)])
    app.config["VK_RUN"] = lambda work: work()                                                  # in place: the tests read the result at once
    pid = points.save_point(conn, POINT)
    card = _card(conn, pid, tanya["order_id"])
    return app, client, tok, conn, pid, card


def hook(app, conn, text, event="e1", sender=PEER, peer=None, reply=None, message_id=None):
    msg = {"id": message_id or abs(hash(event)) % 10 ** 6, "from_id": int(sender), "peer_id": int(peer or sender), "text": text}
    if reply:
        msg["reply_message"] = reply
    body = {"type": "message_new", "group_id": int(GROUP), "secret": SECRET, "event_id": event, "object": {"message": msg}}
    r = app.test_client().post("/hook/vk/%s" % vk_callback.path_token(conn), data=json.dumps(body), content_type="application/json")
    assert r.status_code == 200 and r.get_data(as_text=True) == "ok"


def to_client(app):
    return [json.loads(c[3])["message"]["text"] for c in app.config["AVITO_HTTP"].calls if c[0] == "POST" and c[1].endswith("/messages")]


def to_points(app):
    return [(f["peer_id"][0], f["message"][0]) for f in (urllib.parse.parse_qs(c[3].decode()) for c in app.config["VK_HTTP"].calls)]


def test_arrived_moves_the_order_tells_the_client_once_and_answers_the_point(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    hook(app, conn, "Приехал 5000100")
    got = orders.get_card(conn, card)
    assert got["stage"]["code"] == "at_point" and got["reserve_until"] == "2026-10-16"              # three days from the arrival, as before
    [text] = to_client(app)
    assert text == ("Заказ 5000100 приехал в пункт выдачи, можно забирать.\nТерритория Санкт-Петербург, ул. Генерала Примерова, 35А\n"
                    "График работы ежедневно 10:00–20:00\nЛокация на Яндекс Картах https://yandex.ru/maps/-/CDabc\n\n"
                    "Nokian Hakkapeliitta 10 × 4\n\nХранение резерва до 16.10.2026\n\nПокупатель Татьяна +7 900 111 22 33\n\n"
                    "! Обратите внимание. К оплате принимаем наличные.")                       # the owner's text 2026-10-21, word for word
    [(peer, answer)] = to_points(app)
    assert peer == PEER and answer.startswith("(бот) Заказ №5000100 — отмечено: приехал") and "Клиенту отправлено" in answer     # signed (2026-10-24)
    hook(app, conn, "Приехал 5000100")                                                           # VK repeats the same event
    hook(app, conn, "приехал 5000100", event="e2")                                               # the point writes it again
    assert len(to_client(app)) == 1 and "уже отмечен" in to_points(app)[-1][1] and len(to_points(app)) == 2
    assert mgr.new_count(conn) == 0
    levels = [e["level"] for e in orders.list_events(conn, card) if e["kind"] == "chat"]
    assert levels and "red" not in levels


def test_issued_closes_the_order_and_sends_nothing_to_the_client(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    orders.move_card(conn, card, _stage(conn, "at_point"), NOW, "o")
    for n, word in enumerate(("выдал", "Забрали")):
        hook(app, conn, "%s заказ 5000100" % word, event="i%d" % n)
    assert orders.get_card(conn, card)["stage"]["code"] == "done" and to_client(app) == []
    assert "отмечено: выдан" in to_points(app)[0][1] and "уже отмечен" in to_points(app)[1][1]


def test_doubtful_commands_ask_the_point_wait_in_the_inbox_and_reach_no_client(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    other = points.save_point(conn, dict(name="Гамма", channel="vk", vk_peer_id="777"))
    twin_a, twin_b = _card(conn, pid, webmim="700100"), _card(conn, pid, webmim="700 100")
    fresh = orders.create_card(conn, dict(client_name="Пётр", point_id=pid, webmim_number="600600"), NOW, "o")      # «Новый заказ»
    for n, (sender, text) in enumerate(((PEER, "приехал"), (PEER, "приехал 999999"), ("777", "приехал 5000100"), (PEER, "приехал 700100"),
                                        (PEER, "выдал 600600"))):                     # «выдал» from «Перемещение» closes (owner 2026-10-21): from «Новый» not
        hook(app, conn, text, event="d%d" % n, sender=sender)
    assert all(orders.get_card(conn, c)["stage"]["code"] == "moving" for c in (card, twin_a, twin_b)) and to_client(app) == []
    assert orders.get_card(conn, fresh)["stage"]["code"] == "new"
    answers = [a for _, a in to_points(app)]
    assert all(a.startswith("(бот) ") for a in answers)                                         # every answer of the bot is signed (2026-10-24)
    assert "О каком заказе речь" in answers[0] and "не на вашей точке" in answers[2]
    assert answers[1] == "(бот) Заказа №999999 нет в CRM, необходимо добавить заказ и отправить сообщение клиенту"
    assert "несколько заказов" in answers[3] and "такая команда не нужна" in answers[4]
    reasons = [m["dispute"] for m in reversed(mgr.list_inbox(conn))]
    assert reasons[0] == "нет номера заказа" and reasons[1].startswith("заказа №999999 нет в CRM") and reasons[2] == "заказ другой точки"
    assert reasons[3].startswith("под №700100 несколько заказов") and reasons[4].startswith("слово «выдал» не подходит")
    assert mgr.new_count(conn) == 5 and other


def test_the_owner_confirms_a_doubtful_arrival_and_then_the_client_is_told_once(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    hook(app, conn, "приехал")                                                                   # no number: waits
    [item] = mgr.list_inbox(conn)
    page = client.get("/orders/inbox").get_data(as_text=True)
    assert "Ждёт подтверждения" in page and "/orders/inbox/%d/confirm" % item["id"] in page and "нет номера заказа" in page
    post(client, "/orders/inbox/%d/confirm" % item["id"], tok, order_no="5000100")
    assert orders.get_card(conn, card)["stage"]["code"] == "at_point" and len(to_client(app)) == 1
    assert to_points(app)[-1][1].startswith("(бот) ") and "подтвердил владелец" in to_points(app)[-1][1] and mgr.new_count(conn) == 0   # the panel's text: signed
    post(client, "/orders/inbox/%d/confirm" % item["id"], tok, order_no="5000100")              # a second press
    assert len(to_client(app)) == 1
    assert "подтверждено" in conn.execute("SELECT outcome FROM order_command_log").fetchone()[0]


def test_a_refused_confirmation_leaves_the_line_waiting(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    hook(app, conn, "приехал")
    [item] = mgr.list_inbox(conn)
    for bad in ("", "abc", "999999"):
        post(client, "/orders/inbox/%d/confirm" % item["id"], tok, order_no=bad)
    assert mgr.get_message(conn, item["id"])["status"] == "new" and orders.get_card(conn, card)["stage"]["code"] == "moving"


def test_no_chat_missing_point_data_or_a_failed_send_is_red_and_waits_in_the_inbox(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path, (400, {"error": "forbidden"}))
    bare = _card(conn, pid, webmim="600200")                                                     # no Avito chat
    hook(app, conn, "приехал 600200", event="n1")
    assert orders.get_card(conn, bare)["stage"]["code"] == "at_point" and to_client(app) == []
    assert "нет чата Авито" in mgr.list_inbox(conn)[0]["text"] and "не отправлено" in to_points(app)[-1][1].lower()
    assert [e["level"] for e in orders.list_events(conn, bare) if e["kind"] == "chat"] == ["red"]
    hook(app, conn, "приехал 5000100", event="n2")                                               # Avito refuses
    assert orders.get_card(conn, card)["stage"]["code"] == "at_point"
    assert "сбой отправки" in mgr.list_inbox(conn)[0]["text"] and "red" in [e["level"] for e in orders.list_events(conn, card)]
    plain = _card(conn, points.save_point(conn, dict(name="Альфа", channel="vk", vk_peer_id="888")), webmim="600300")
    hook(app, conn, "приехал 600300", event="n3", sender="888")                                  # the point has no address, hours, map
    assert "не хватает" in mgr.list_inbox(conn)[0]["text"] and len(to_client(app)) == 1                 # only the refused try of 5000100


def test_the_owners_template_of_the_same_name_is_used_instead_of_the_sample(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    tpl.add_template(conn, "Клиенту: товар на точке, можно забирать", "client", "Заказ {webmim_number} ждёт вас: {point_address}")
    hook(app, conn, "приехал 5000100")
    assert to_client(app) == ["Заказ 5000100 ждёт вас: Санкт-Петербург, ул. Генерала Примерова, 35А"]


def test_client_did_not_come_is_a_red_record_and_an_alert_and_nothing_for_the_client(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    orders.move_card(conn, card, _stage(conn, "at_point"), NOW, "o")
    told = []
    answer = mgr.dispatch(conn, "x1", PEER, "клиент не пришёл 5000100", None, NOW, None, lambda c, t: told.append(t) or "отправлено")
    assert "клиент не пришёл" in answer and orders.get_card(conn, card)["stage"]["code"] == "at_point" and to_client(app) == []
    [red] = [e for e in orders.list_events(conn, card) if e["kind"] == "point"]
    assert red["level"] == "red" and "Свяжитесь с клиентом" in red["text"] and "16.10.2026" in red["text"]
    assert told and told[0].startswith("Заказ №%d: Клиент не пришёл" % card)
    assert mgr.order_number("клиент не пришёл, заказ 17") == 17 and mgr.order_number("клиент не пришел 17") == 17 and mgr.order_number("клиент не пришёл") is None
    orders.move_card(conn, card, _stage(conn, "done"), NOW, "o")
    mgr.dispatch(conn, "x2", PEER, "клиент не пришел 5000100", None, NOW, None, None)                  # the order is closed: doubtful
    assert mgr.list_inbox(conn)[0]["dispute"].startswith("«клиент не пришёл», а заказ на этапе")


def test_the_order_is_found_by_the_webmim_number_or_the_panel_number_or_the_bots_line(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    assert mgr.find_orders(conn, 5000100) == [card] and mgr.find_orders(conn, 424242) == []                    # the panel number: see the review guard below
    hook(app, conn, "приехал", reply={"from_id": -int(GROUP), "text": "Заказ №5000100 — отмечено: …"})
    assert orders.get_card(conn, card)["stage"]["code"] == "at_point"
    hook(app, conn, "выдал", event="r2", reply={"from_id": 12345, "text": "Заказ №5000100"})       # a line of somebody else names nothing
    assert orders.get_card(conn, card)["stage"]["code"] == "at_point"


def test_messages_of_chats_and_communities_are_recorded_but_not_acted_on(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    hook(app, conn, "приехал 5000100", event="c1", peer="2000000001")                          # a group chat
    hook(app, conn, "приехал 5000100", event="c2", sender="-5", peer="-5")                      # a community
    assert orders.get_card(conn, card)["stage"]["code"] == "moving" and to_points(app) == []
    assert conn.execute("SELECT COUNT(*) FROM vk_event").fetchone()[0] == 2


def test_a_stranger_gets_no_answer_and_a_binding_code_binds_the_vk_of_the_manager_once(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    hook(app, conn, "приехал 5000100", event="s1", sender="999")
    assert to_points(app) == [] and orders.get_card(conn, card)["stage"]["code"] == "moving"
    code = points.new_bind_code(conn, pid)
    hook(app, conn, code.lower(), event="s2", sender="999")
    assert points.get_point(conn, pid)["vk_peer_id"] == "999" and points.get_point(conn, pid)["bind_code"] == ""
    assert to_points(app) == [("999", to_points(app)[0][1])] and "привязан к точке «Бета»" in to_points(app)[0][1]
    hook(app, conn, code, event="s3", sender="321")                                              # the code is used up
    assert points.get_point(conn, pid)["vk_peer_id"] == "999" and "не подходит" in to_points(app)[-1][1]


def test_an_answer_in_the_inbox_goes_to_the_point_in_vk(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    hook(app, conn, "Подскажите, когда завоз?")
    [item] = mgr.list_inbox(conn)
    r = post(client, "/orders/inbox/%d/answer" % item["id"], tok, text="Завтра утром")
    assert to_points(app)[-1] == (PEER, "Завтра утром") and mgr.list_inbox(conn)[0]["status"] == "answered"      # the owner's own words: not signed
    app.config["VK_HTTP"] = ScriptedHttp((200, {"error": {"error_code": 901, "error_msg": "Can't send messages"}}))
    hook(app, conn, "И ещё вопрос", event="q2")
    page = client.post("/orders/inbox/%d/answer" % mgr.list_inbox(conn)[0]["id"], data={"csrf": tok, "text": "Ответ"}, follow_redirects=True).get_data(as_text=True)
    assert "В ВК не ушло" in page and r


def test_vk_gets_ok_before_the_work_and_a_repeat_meanwhile_does_not_act_twice(tmp_path, monkeypatch):
    import threading
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    del app.config["VK_RUN"]                                                                    # the real way: a thread after the answer
    gate, done, calls = threading.Event(), threading.Event(), []
    real = mgr.dispatch

    def slow(*args, **kwargs):
        calls.append(args[1])
        assert gate.wait(10)                                                                     # Avito and VK are slow: VK must not wait
        try:
            return real(*args, **kwargs)
        finally:
            done.set()
    monkeypatch.setattr(mgr, "dispatch", slow)
    hook(app, conn, "приехал 5000100")                                                          # «ok» came while the work waits
    hook(app, conn, "приехал 5000100")                                                          # VK repeats it 10 seconds later
    assert orders.get_card(conn, card)["stage"]["code"] == "moving" and len(calls) <= 1
    gate.set()
    assert done.wait(10)
    for thread in [t for t in threading.enumerate() if t.name == "vk-message"]:
        thread.join(10)
    assert calls == ["vk-%d" % (abs(hash("e1")) % 10 ** 6)] and orders.get_card(conn, card)["stage"]["code"] == "at_point"
    assert len(to_client(app)) == 1 and len(to_points(app)) == 1


def test_a_confirmation_after_the_arrival_was_told_does_not_tell_the_client_again(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT, (200, {"access_token": "t", "expires_in": 3600}), SENT)       # room for a wrong second send
    hook(app, conn, "приехал 5000100")                                                          # told once
    hook(app, conn, "приехал", event="e2")                                                       # a doubtful second one
    item = mgr.list_inbox(conn)[0]
    post(client, "/orders/inbox/%d/confirm" % item["id"], tok, order_no="5000100")             # the same arrival: the same send mark
    assert len(to_client(app)) == 1 and mgr.get_message(conn, item["id"])["status"] == "answered"


# ---- review round 1: guards (each was red before its fix)

def test_a_reply_to_one_order_with_another_number_in_the_text_is_doubtful(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    other = _card(conn, pid, webmim="654321")
    hook(app, conn, "приехал 654321", reply={"from_id": -int(GROUP), "text": "Заказ №5000100 — отмечено: …"})
    assert orders.get_card(conn, card)["stage"]["code"] == "moving" and orders.get_card(conn, other)["stage"]["code"] == "moving"
    assert to_client(app) == [] and mgr.list_inbox(conn)[0]["dispute"].startswith("в ответе заказ №5000100")
    hook(app, conn, "приехал 5000100", event="e2", reply={"from_id": -int(GROUP), "text": "Заказ №5000100 — отмечено: …"})
    assert orders.get_card(conn, card)["stage"]["code"] == "at_point"                            # the same number twice is one order


def test_the_panel_number_never_names_an_order_that_has_a_webmim_number_and_two_numbers_are_doubtful(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    assert mgr.find_orders(conn, card) == []                                                     # it has 5000100: the points know that one
    bare = _card(conn, pid, webmim="")
    assert mgr.find_orders(conn, bare) == [bare]
    assert mgr.order_number("Приехали заказы: 5000100 и 654321") is None and mgr.order_number("приехал заказ 5000100, 4 шт") is None
    assert mgr.order_number("Принял 4 шины, заказ 5000100") == 5000100                          # a quantity before the order stays allowed


def test_a_confirmation_keeps_the_rules_of_the_stage_and_of_the_point(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    hook(app, conn, "приехал")
    item = mgr.list_inbox(conn)[0]
    closed = _card(conn, pid, webmim="700700", code="at_point")
    orders.move_card(conn, closed, _stage(conn, "done"), NOW, "o")
    alien = _card(conn, points.save_point(conn, dict(name="Гамма")), webmim="700800")
    for number in ("700700", "700800"):
        post(client, "/orders/inbox/%d/confirm" % item["id"], tok, order_no=number)
    assert orders.get_card(conn, closed)["stage"]["code"] == "done" and orders.get_card(conn, alien)["stage"]["code"] == "moving"
    assert mgr.get_message(conn, item["id"])["dispute"] and to_client(app) == []


def test_arrived_after_a_move_by_hand_still_tells_the_client_once(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    orders.move_card(conn, card, _stage(conn, "at_point"), NOW, "Партнёр")
    hook(app, conn, "приехал 5000100")
    hook(app, conn, "приехал 5000100", event="e2")
    assert len(to_client(app)) == 1 and "уже отмечен" in to_points(app)[0][1] and "Клиенту отправлено" in to_points(app)[0][1]


def test_a_send_in_progress_or_unknown_is_never_offered_for_a_second_send_by_hand(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    orders.move_card(conn, card, _stage(conn, "at_point"), NOW, "o")
    entered = orders.get_card(conn, card)["entered_at"]
    token = "arrived-%d-%s" % (card, "".join(ch for ch in entered if ch.isdigit()))
    chat = conn.execute("SELECT id FROM avito_chat WHERE order_id = ?", (card,)).fetchone()[0]
    conn.execute("INSERT INTO chat_send_log (token, chat_row, at, author, status) VALUES (?, ?, ?, 'бот', 'unknown')", (token, chat, NOW))
    conn.commit()
    sent, note = mgr.tell_client_arrived(conn, card, lambda *a: (_ for _ in ()).throw(AssertionError("sent again")), NOW, "t")
    assert not sent and "повторно не отправляйте" in mgr.list_inbox(conn)[0]["text"]
    conn.execute("UPDATE chat_send_log SET status = 'sent' WHERE token = ?", (token,))
    conn.commit()
    assert mgr.tell_client_arrived(conn, card, None, NOW, "t")[0] is True


def test_an_answer_does_not_take_away_the_confirmation_and_a_failed_one_keeps_the_answer(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    hook(app, conn, "приехал")
    item = mgr.list_inbox(conn)[0]
    post(client, "/orders/inbox/%d/answer" % item["id"], tok, text="Какой номер?")
    post(client, "/orders/inbox/%d/confirm" % item["id"], tok, order_no="999999")              # refused
    assert mgr.get_message(conn, item["id"])["answer"] == "Какой номер?"
    post(client, "/orders/inbox/%d/confirm" % item["id"], tok, order_no="5000100")
    assert orders.get_card(conn, card)["stage"]["code"] == "at_point" and len(to_client(app)) == 1


def test_only_a_client_who_did_not_come_is_a_no_show_not_goods_that_did_not_come(tmp_path):
    for text in ("клиент не пришёл 17", "Клиентка не пришла 17", "клиенты не пришли 17", "клиент не пришел, заказ 17"):
        assert mgr.order_number(text) == 17 and mgr._word(text) == mgr.NO_SHOW, text
    assert mgr._word("Не пришёл заказ 17") != mgr.NO_SHOW


def test_a_message_lost_when_the_panel_restarted_is_shown_in_the_inbox(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    app.config["VK_RUN"] = lambda work: None                                                     # the process died right after «ok»
    hook(app, conn, "приехал 5000100")
    conn.execute("INSERT INTO order_command_log (message_id, at, sender, word, order_id, outcome) VALUES ('vk-1', '2026-10-13T11:00:00', ?, 'выдал', ?, 'обрабатывается')",
                 (PEER, card))
    conn.commit()
    assert mgr.list_inbox(conn) == []
    vk_callback.sweep(conn, datetime_at("2026-10-13T09:10:00"))                                  # ten minutes later (UTC; the panel is UTC+3)
    texts = [m["text"] for m in mgr.list_inbox(conn)]
    assert any("не обработано" in t and "приехал 5000100" in t for t in texts) and any("не завершена" in t for t in texts)
    vk_callback.sweep(conn, datetime_at("2026-10-13T09:20:00"))                                  # once
    assert len(mgr.list_inbox(conn)) == 2


def datetime_at(text):
    from datetime import datetime
    return datetime.fromisoformat(text)


def test_the_next_vk_event_and_the_inbox_page_show_a_message_cut_by_a_restart(tmp_path):
    from datetime import datetime
    app, client, tok, conn, pid, card = ready(tmp_path)
    app.config["VK_RUN"] = lambda work: None                                                     # cut right after «ok»
    hook(app, conn, "приехал 5000100")
    app.config["NOW"] = lambda: datetime(2026, 10, 13, 9, 10, 0)
    app.config["VK_RUN"] = lambda work: work()
    hook(app, conn, "Подскажите график", event="e2")                                             # the next message of any point
    assert any("не обработано" in m["text"] and "приехал 5000100" in m["text"] for m in mgr.list_inbox(conn))
    page = client.get("/orders/inbox").get_data(as_text=True)
    assert "не обработано (обработка прервалась" in page and len(mgr.list_inbox(conn)) == 2


# ---- review round 2: guards

def test_a_second_arrival_while_the_first_send_is_running_never_asks_for_a_send_by_hand(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    orders.move_card(conn, card, _stage(conn, "at_point"), NOW, "o")
    token = "arrived-%d-%s" % (card, "".join(ch for ch in orders.get_card(conn, card)["entered_at"] if ch.isdigit()))

    def busy(chat_row, text, tok_):                                                             # the other thread took the mark a moment ago
        conn.execute("INSERT INTO chat_send_log (token, chat_row, at, author, status) VALUES (?, ?, ?, 'бот', 'sending')", (tok_, chat_row, NOW))
        conn.commit()
        raise mgr.chats.ChatError("Это сообщение уже отправляется.")
    sent, note = mgr.tell_client_arrived(conn, card, busy, NOW, "t")
    assert not sent and "повторно не отправляйте" in mgr.list_inbox(conn)[0]["text"] and "Отправьте вручную" not in mgr.list_inbox(conn)[0]["text"]
    conn.execute("UPDATE chat_send_log SET status = 'sent' WHERE token = ?", (token,))
    conn.commit()
    other = _card(conn, pid, webmim="600900", code="at_point")
    assert other


def test_sweep_leaves_alone_handled_messages_empty_ones_strangers_and_what_came_before_the_bot(tmp_path):
    from datetime import datetime
    app, client, tok, conn, pid, card = ready(tmp_path)
    app.config["VK_RUN"] = lambda work: None
    hook(app, conn, "приехал 5000100", event="old")                                             # before the bot was switched on: never shown
    conn.execute("UPDATE vk_event SET at = '2026-10-13T08:00:00' WHERE event_id = 'old'")
    conn.commit()
    vk_callback.sweep(conn, datetime(2026, 10, 13, 8, 30, 0))                                    # the first sweep: the bot starts now
    app.config["VK_RUN"] = lambda work: work()
    for n, (sender, text) in enumerate(((PEER, "Где склад?"), (PEER, "приехал 5000100"), ("999", "приехал 1"), (PEER, ""))):
        hook(app, conn, text, event="h%d" % n, sender=sender)
    code = points.new_bind_code(conn, points.save_point(conn, dict(name="Альфа")))
    hook(app, conn, code, event="bind", sender="888")
    before = len(mgr.list_inbox(conn))
    vk_callback.sweep(conn, datetime(2026, 10, 13, 9, 30, 0))
    assert len(mgr.list_inbox(conn)) == before and not any("не обработано" in m["text"] for m in mgr.list_inbox(conn))


def test_a_reply_to_a_long_line_of_the_community_finds_its_order(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    placed = "Заказ 5000100 оформлен на перемещение.\nТерритория СПб, ул. Примерова, 35А\n\nNokian 205/55 R16 × 4\n\nПокупатель Татьяна +7 900 111 22 33"
    hook(app, conn, "приехал", reply={"from_id": -int(GROUP), "text": placed})
    assert orders.get_card(conn, card)["stage"]["code"] == "at_point"
    hook(app, conn, "выдал", event="e2", reply={"from_id": -int(GROUP), "text": "Заказ №%d: резерв заканчивается 16.10.2026" % card})   # an alert names the panel number
    assert orders.get_card(conn, card)["stage"]["code"] == "done"


def test_the_inbox_confirms_with_the_number_it_offers_and_with_the_panel_number_of_twins(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    twin = _card(conn, pid, webmim="5000100")
    hook(app, conn, "приехал 5000100")
    item = mgr.list_inbox(conn)[0]
    assert item["dispute"].startswith("под №5000100 несколько заказов")
    post(client, "/orders/inbox/%d/confirm" % item["id"], tok, order_no=str(card))
    assert orders.get_card(conn, card)["stage"]["code"] == "at_point" and orders.get_card(conn, twin)["stage"]["code"] == "moving"


def test_a_long_number_next_to_the_marked_one_is_doubtful(tmp_path):
    assert mgr.order_number("выдал 5000100 и заказ 654321") is None and mgr.order_number("Принял 4 шины, заказ 5000100") == 5000100


# ---- review round 3: guards

def test_a_confirmation_running_now_is_never_called_cut_off(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    conn.execute("INSERT INTO order_command_log (message_id, at, sender, word, order_id, outcome) VALUES ('vk-9', '2026-10-13T08:00:00', ?, 'приехал', ?, 'подтверждается')",
                 (PEER, card))                                                                   # an old message, the owner pressed just now
    conn.commit()
    assert mgr.note_stuck(conn, "2026-10-13T12:00:00", "2026-10-13T11:55:00") == 0 and mgr.list_inbox(conn) == []


def test_a_busy_base_at_the_first_message_still_answers_ok_and_acts(tmp_path, monkeypatch):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    monkeypatch.setattr(vk_callback, "started", lambda *a: (_ for _ in ()).throw(vk_callback.store.sqlite3.OperationalError("database is locked")))
    hook(app, conn, "приехал 5000100")
    assert orders.get_card(conn, card)["stage"]["code"] == "at_point"


def test_the_bots_not_found_line_does_not_name_the_order_it_did_not_find(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    hook(app, conn, "приехал 4242")
    assert "нет в crm" in to_points(app)[0][1].lower() and mgr.line_number(to_points(app)[0][1]) is None      # the owner's words 2026-10-25


def test_a_command_about_an_order_of_no_point_is_refused_plainly_and_changes_nothing(tmp_path):
    """The owner 2026-10-22: an order of no point is not the points' to see. A point that still names it (the WebMIM number on the goods,
    a reply to an old line, a slip of the finger) is told to ask the manager: no dispute in «Входящие», the order stays as it was."""
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    loose = orders.create_card(conn, dict(client_name="Пётр", webmim_number="600300"), NOW, "o")     # only «Новый» goes without a point
    before = orders.get_card(conn, loose)
    hook(app, conn, "приехал 600300", event="p1")
    hook(app, conn, "выдал", event="p2", reply={"from_id": -int(GROUP), "text": "Заказ №600300 едет на точку"})
    after = orders.get_card(conn, loose)
    assert after["stage"]["code"] == "new" and after["point_id"] is None and after["updated_at"] == before["updated_at"]
    assert to_client(app) == [] and mgr.new_count(conn) == 0 and mgr.list_inbox(conn) == []
    answers = [a for _, a in to_points(app)]
    assert answers == ["(бот) Заказ №600300 пока не назначен вашей точке — уточните у менеджера."] * 2
    assert all("другой точки" not in a and "передано владельцу" not in a for a in answers)
    log = conn.execute("SELECT order_id, outcome FROM order_command_log ORDER BY at, message_id").fetchall()
    assert log == [(loose, "заказ без точки: не выполнено")] * 2
    notes = [e for e in orders.list_events(conn, loose) if "не назначен ни одной точке" in e["text"]]
    assert [(e["kind"], e["author"]) for e in notes] == [("point", "менеджер Бета")] * 2
    assert notes[-1]["text"] == "Точка «Бета» написала «приехал», но заказ не назначен ни одной точке: команда не выполнена"
    assert orders.get_card(conn, card)["stage"]["code"] == "moving"                     # the point's own order is not touched either


def test_the_owner_cannot_confirm_a_command_for_an_order_of_no_point_and_is_told_why(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    loose = orders.create_card(conn, dict(client_name="Пётр", webmim_number="600300"), NOW, "o")
    hook(app, conn, "приехал")
    [item] = mgr.list_inbox(conn)
    try:
        mgr.confirm(conn, item["id"], "600300", NOW, "o")
    except orders.OrderError as err:
        assert str(err) == "Заказ №600300 не назначен ни одной точке: поставьте точку в заказе или переведите его вручную."
    else:
        raise AssertionError("confirmed an order of no point")
    assert orders.get_card(conn, loose)["stage"]["code"] == "new" and mgr.get_message(conn, item["id"])["status"] == "new"


def test_a_journal_that_cannot_be_written_keeps_the_plain_refusal_for_an_order_of_no_point(tmp_path, monkeypatch):
    import sqlite3
    app, client, tok, conn, pid, card = ready(tmp_path)
    loose = orders.create_card(conn, dict(client_name="Пётр", webmim_number="600300"), NOW, "o")
    real = orders.add_event

    def busy(conn_, card_id, now, author, kind, text, level=""):
        if "не назначен ни одной точке" in text:
            raise sqlite3.OperationalError("database is locked")
        return real(conn_, card_id, now, author, kind, text, level)
    monkeypatch.setattr(orders, "add_event", busy)
    hook(app, conn, "приехал 600300", event="b1")
    assert [a for _, a in to_points(app)] == ["(бот) Заказ №600300 пока не назначен вашей точке — уточните у менеджера."]
    assert conn.execute("SELECT outcome FROM order_command_log").fetchone()[0] == "заказ без точки: не выполнено"
    assert orders.get_card(conn, loose)["point_id"] is None


def test_the_arrival_is_a_yellow_record_for_the_bell_once(tmp_path):
    from tires_sync import order_alerts
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    hook(app, conn, "Приехал 5000100")
    hook(app, conn, "приехал 5000100", event="e2")                                               # already marked: no second record
    notes = [e for e in orders.list_events(conn, card) if e["text"].startswith("Поступили шины")]
    assert [(e["text"], e["level"]) for e in notes] == [
        ("Поступили шины по заказу №5000100 (точка «Бета»). Клиенту «можно забирать»: отправлено.", "yellow")]   # the owner 2026-10-23, п. 18
    assert any(r["text"].startswith("Поступили шины по заказу №5000100") for r in order_alerts.page_rows(conn, "owner", NOW))


def test_an_unknown_number_tells_the_owner_in_the_bell_until_the_inbox_line_is_dealt_with(tmp_path):
    from tires_sync import order_alerts, users
    app, client, tok, conn, pid, card = ready(tmp_path)
    hook(app, conn, "приехал 999999", event="m1")
    [(_peer, answer)] = to_points(app)
    assert answer == "(бот) Заказа №999999 нет в CRM, необходимо добавить заказ и отправить сообщение клиенту"
    [row] = [r for r in order_alerts.page_rows(conn, "owner", NOW) if r["kind"] == "inbox"]      # no partner in the panel: the owner (2026-10-24)
    assert row["text"] == "Точка «Бета» сообщила о заказе №999999 («приехал»), его нет в CRM — добавьте"
    assert row["href"] == "/orders/inbox" and row["level"] == "yellow"
    assert "Точка «Бета» сообщила о заказе №999999" in client.get("/alerts").get_data(as_text=True)
    outcome = conn.execute("SELECT outcome FROM order_command_log").fetchone()[0]
    assert outcome == "спорно: заказа №999999 нет в CRM; Telegram: ни у кого нет чата (owner)"    # no chat set up in this panel
    users.add_user(conn, "worker2", "пароль-для-партнёра-1", NOW, role="partner")
    assert [r for r in order_alerts.page_rows(conn, "owner", NOW) if r["kind"] == "inbox"] == []  # a partner now: it is his
    assert [r["kind"] for r in order_alerts.page_rows(conn, "worker2", NOW)].count("inbox") == 1
    [item] = mgr.list_inbox(conn)
    mgr.answer(conn, item["id"], "Добавили", NOW)
    assert [r for r in order_alerts.page_rows(conn, "worker2", NOW) if r["kind"] == "inbox"] == []


def test_an_unknown_number_reaches_the_partner_in_telegram_once(tmp_path):
    import datetime as dt
    from tires_sync import connections, order_notify, users
    app, client, tok, conn, pid, card = ready(tmp_path)
    box = app.config["SECRET_BOX"]
    connections.set_secret(conn, box, "telegram", 0, "bot_token", "123456:ABCdef-SECRET", NOW, "o")
    users.add_user(conn, "worker2", "пароль-для-партнёра-1", NOW, role="partner")
    order_notify.set_user_chat(conn, "worker2", "555", NOW, "o")
    tg = ScriptedHttp((200, {"ok": True, "result": {}}))
    msg = {"id": 41, "from_id": int(PEER), "peer_id": int(PEER), "text": "выдал 888888"}
    body = json.dumps({"type": "message_new", "group_id": int(GROUP), "secret": SECRET, "event_id": "t1", "object": {"message": msg}}).encode()
    sent = []
    channel = lambda db, local: (lambda peer, text, **kw: sent.append(text), None, None)
    for _ in range(2):                                                                            # VK repeats the event: told once
        assert vk_callback.handle(conn, box, vk_callback.path_token(conn), body, dt.datetime(2026, 10, 14, 9, 0), channel=channel, tg_http=tg) == (200, "ok")
    other = body.replace(b'"event_id": "t1"', b'"event_id": "t2"')                               # the same message under another event of VK
    assert vk_callback.handle(conn, box, vk_callback.path_token(conn), other, dt.datetime(2026, 10, 14, 9, 1), channel=channel, tg_http=tg) == (200, "ok")
    assert len(tg.calls) == 1 and json.loads(tg.calls[0][3])["chat_id"] == "555"
    assert json.loads(tg.calls[0][3])["text"] == "Точка «Бета» сообщила о заказе №888888 («выдал»), его нет в CRM — добавьте."
    assert sent[0] == "(бот) Заказа №888888 нет в CRM, необходимо добавить заказ и отправить сообщение клиенту"
    assert conn.execute("SELECT outcome FROM order_command_log").fetchone()[0].endswith("; Telegram: отправлено (worker2)")


def test_arrived_after_a_move_by_hand_still_tells_the_bell_once(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    orders.move_card(conn, card, _stage(conn, "at_point"), NOW, "o")                             # the manager moved it before the tyres came
    assert not [e for e in orders.list_events(conn, card) if e["text"].startswith("Поступили шины")]
    hook(app, conn, "приехал 5000100", event="h1")
    hook(app, conn, "приехал 5000100", event="h2")
    notes = [e for e in orders.list_events(conn, card) if e["text"].startswith("Поступили шины")]
    assert len(notes) == 1 and notes[0]["level"] == "yellow" and len(to_client(app)) == 1
