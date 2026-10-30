"""A point forwards the bot's messages about orders with its word (30.10 12:25, Янино: «Приехали» + two forwarded «Заказ … оформлен на
перемещение …»): the numbers of the community's forwarded messages are read as of a reply; the word acts on each order with the checks of
one command, the client hears «можно забирать» once per order, the journal has a line per order, the answer lists every order; a number
unknown or of another point waits in «Входящие» and the others go on."""
from __future__ import annotations

import json

from test_vk_commands import GROUP, PEER, SECRET, SENT, _card, _stage, ready, to_client, to_points
from tires_sync import order_manager as mgr, orders, vk_callback

BOT = int("-" + GROUP)


def line(number, sender=BOT, inner=None):
    msg = {"from_id": sender, "text": "Заказ %s оформлен на перемещение 26R-1447431. Клиент Иван +7 900 111 22 33" % number, "date": 1_791_900_000}
    if inner:
        msg["fwd_messages"] = inner
    return msg


def send(app, conn, text, fwd=(), reply=None, event="f1", message_id=4242):
    msg = {"id": message_id, "from_id": int(PEER), "peer_id": int(PEER), "text": text, "fwd_messages": list(fwd)}
    if reply:
        msg["reply_message"] = reply
    body = {"type": "message_new", "group_id": int(GROUP), "secret": SECRET, "event_id": event, "object": {"message": msg}}
    r = app.test_client().post("/hook/vk/%s" % vk_callback.path_token(conn), data=json.dumps(body), content_type="application/json")
    assert r.status_code == 200 and r.get_data(as_text=True) == "ok"


def stage(conn, card):
    return orders.get_card(conn, card)["stage"]["code"]


def log(conn):
    return dict(conn.execute("SELECT message_id, outcome FROM order_command_log").fetchall())


def test_one_forwarded_line_of_the_bot_is_as_a_reply(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    send(app, conn, "Приехали", [line(5000100)])
    assert stage(conn, card) == "at_point" and len(to_client(app)) == 1
    [(peer, answer)] = to_points(app)
    assert answer.startswith("(бот) Заказ №5000100 — отмечено: приехал")
    assert mgr.new_count(conn) == 0


def test_two_forwarded_lines_move_both_and_the_answer_lists_both(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    second = _card(conn, pid, webmim="5000200")
    send(app, conn, "Приехали", [line(5000100), line(5000200)])
    assert stage(conn, card) == stage(conn, second) == "at_point"
    assert len(to_client(app)) == 1                                       # Татьяна's order has the chat; the other has none to tell
    [(peer, answer)] = to_points(app)
    assert "Заказ №5000100 — отмечено: приехал" in answer and "Заказ №5000200 — отмечено: приехал" in answer
    rows = log(conn)
    assert rows["vk-4242#5000100"].startswith("заказ перенесён") and rows["vk-4242#5000200"].startswith("заказ перенесён")
    assert rows["vk-4242"].startswith("пакет: 2 заказа")
    assert [m["text"] for m in mgr.list_inbox(conn)] == [                 # no dispute: only the usual line of an order without a chat
        "Заказ №5000200: клиенту не отправлено «можно забирать» — у заказа нет чата Авито. Отправьте вручную."]


def test_a_repeat_of_the_event_does_nothing_more(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    _card(conn, pid, webmim="5000200")
    send(app, conn, "Приехали", [line(5000100), line(5000200)])
    send(app, conn, "Приехали", [line(5000100), line(5000200)])            # VK repeats the event
    send(app, conn, "Приехали", [line(5000100), line(5000200)], event="f2")  # the same message under another event
    assert len(to_client(app)) == 1 and len(to_points(app)) == 1
    assert mgr.is_handled(conn, "vk-4242")                                 # its trace: vk_callback.sweep and vk_catchup leave it alone


def test_an_unknown_number_waits_and_the_others_go_on(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    send(app, conn, "Приехали", [line(5000100), line(999999)])
    assert stage(conn, card) == "at_point" and len(to_client(app)) == 1
    [(peer, answer)] = to_points(app)
    assert "Заказ №5000100 — отмечено: приехал" in answer and "Заказа №999999 нет в CRM" in answer
    [item] = mgr.list_inbox(conn)
    assert item["dispute"].startswith("заказа №999999 нет в CRM")


def test_an_order_of_another_point_waits(tmp_path):
    from tires_sync import order_points as points
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    other = points.save_point(conn, dict(name="Гамма", channel="vk", vk_peer_id="777"))
    theirs = _card(conn, other, webmim="5000300")
    send(app, conn, "Приехали", [line(5000100), line(5000300)])
    assert stage(conn, card) == "at_point" and stage(conn, theirs) == "moving"
    assert [m["dispute"] for m in mgr.list_inbox(conn)] == ["заказ другой точки"]


def test_a_forwarded_message_not_of_the_community_names_no_order(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    send(app, conn, "Приехали", [line(5000100, sender=int(PEER)), line(5000100, sender=123456)])
    assert stage(conn, card) == "moving" and to_client(app) == []
    assert [m["dispute"] for m in mgr.list_inbox(conn)] == ["нет номера заказа"]


def test_a_reply_and_a_forward_together_act_on_both(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    second = _card(conn, pid, webmim="5000200")
    send(app, conn, "приехали", [line(5000200)], reply=line(5000100))
    assert stage(conn, card) == stage(conn, second) == "at_point"


def test_a_forward_inside_a_forward_is_read(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    wrapper = {"from_id": int(PEER), "text": "смотри", "fwd_messages": [line(5000100)]}
    send(app, conn, "Приехали", [wrapper])
    assert stage(conn, card) == "at_point"


def test_a_number_in_the_text_wins_over_the_forwards(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    second = _card(conn, pid, webmim="5000200")
    send(app, conn, "приехал 5000100", [line(5000100), line(5000200)])
    assert stage(conn, card) == "at_point" and stage(conn, second) == "moving"


def test_issued_for_two_forwarded_orders_tells_no_client(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    second = _card(conn, pid, webmim="5000200")
    for c in (card, second):
        orders.move_card(conn, c, _stage(conn, "at_point"), "2026-10-13T09:00:00", "o")
    send(app, conn, "выдал", [line(5000100), line(5000200)])
    assert stage(conn, card) == stage(conn, second) == "done" and to_client(app) == []


def test_the_backstop_of_vk_reads_the_forwards_too(tmp_path):
    """vk_catchup gives the message to the same vk_callback._message and _act."""
    msg = {"id": 4243, "from_id": int(PEER), "peer_id": int(PEER), "text": "Приехали", "fwd_messages": [line(5000100), line(5000200)]}
    parsed = vk_callback._message({"type": "message_new", "group_id": int(GROUP), "object": {"message": msg}}, GROUP)
    assert parsed["reply_nos"] == [5000100, 5000200] and parsed["reply_no"] is None
    msg["reply_message"] = line(5000300)
    assert vk_callback._message({"object": {"message": msg}}, GROUP)["reply_nos"] == [5000300, 5000100, 5000200]
    msg["fwd_messages"] = [line(n) for n in range(5000000, 5000030)]
    assert len(vk_callback._message({"object": {"message": msg}}, GROUP)["reply_nos"]) == 31            # the reply and all 30: the batch names those over its limit


# ---- review, round 1 ------------------------------------------------------------------------------------------------------------------

def test_an_unknown_forwarded_number_reaches_the_partner_in_telegram_once(tmp_path):
    import datetime as dt
    from http_fakes import ScriptedHttp
    from tires_sync import connections, order_notify, users
    app, client, tok, conn, pid, card = ready(tmp_path)
    box = app.config["SECRET_BOX"]
    connections.set_secret(conn, box, "telegram", 0, "bot_token", "123456:ABCdef-SECRET", "2026-10-13T09:00:00", "o")
    users.add_user(conn, "worker2", "пароль-для-партнёра-1", "2026-10-13T09:00:00", role="partner")
    order_notify.set_user_chat(conn, "worker2", "555", "2026-10-13T09:00:00", "o")
    tg = ScriptedHttp((200, {"ok": True, "result": {}}))
    msg = {"id": 51, "from_id": int(PEER), "peer_id": int(PEER), "text": "выдал", "fwd_messages": [line(888888), line(777777)]}
    body = json.dumps({"type": "message_new", "group_id": int(GROUP), "secret": SECRET, "event_id": "t1", "object": {"message": msg}}).encode()
    channel = lambda db, local: (lambda peer, text, **kw: None, None, None)
    for _ in range(2):
        vk_callback.handle(conn, box, vk_callback.path_token(conn), body, dt.datetime(2026, 10, 14, 9, 0), channel=channel, tg_http=tg)
    texts = sorted(json.loads(c[3])["text"] for c in tg.calls)
    assert texts == ["Точка «Бета» сообщила о заказе №777777 («выдал»), его нет в CRM — добавьте.",
                     "Точка «Бета» сообщила о заказе №888888 («выдал»), его нет в CRM — добавьте."]


def test_a_cut_batch_names_its_orders_in_the_inbox(tmp_path):
    """The work cut in the middle of a batch (a restart): the line of «Входящие» names the orders forwarded."""
    app, client, tok, conn, pid, card = ready(tmp_path)
    conn.execute("INSERT INTO order_command_log (message_id, at, sender, word, order_id, outcome) VALUES "
                 "('vk-9', '2026-10-13T08:00:00', ?, ?, NULL, 'обрабатывается')", (PEER, mgr._batch_word("приехали", [5000100, 5000200])))
    conn.commit()
    assert mgr.note_stuck(conn, "2026-10-13T12:00:00", "2026-10-13T11:00:00") == 1
    [item] = mgr.list_inbox(conn)
    assert "№5000100" in item["text"] and "№5000200" in item["text"]


def test_numbers_over_the_limit_are_named_in_the_answer(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    numbers = [5000100] + list(range(6000001, 6000001 + mgr.MAX_BATCH + 1))
    send(app, conn, "Приехали", [line(n) for n in numbers])
    [(peer, answer)] = to_points(app)
    assert stage(conn, card) == "at_point"
    assert "не разобраны: №%d, №%d" % (numbers[-2], numbers[-1]) in answer


def test_each_answer_of_a_batch_names_its_order(tmp_path):
    answers = mgr._numbered(["Не получилось выполнить команду. Напишите владельцу.", "Заказ №5000200 — отмечено: приехал"], [5000100, 5000200])
    assert answers == ["№5000100: Не получилось выполнить команду. Напишите владельцу.", "Заказ №5000200 — отмечено: приехал"]


def test_a_number_in_the_text_not_among_the_forwards_waits(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    second = _card(conn, pid, webmim="5000200")
    send(app, conn, "приехал 5000200", [line(5000100), line(5000300)])
    assert stage(conn, card) == "moving" and stage(conn, second) == "moving" and to_client(app) == []
    [item] = mgr.list_inbox(conn)
    assert "5000200" in item["dispute"]
