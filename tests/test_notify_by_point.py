"""The alerts of an Avito account of a point go only to that point (the owner 2026-10-26): never to the owner or the partner in Telegram, in
the bell or on «Уведомления». The accounts of no point, and their orders delivered to a point, are as before."""
from __future__ import annotations

import json
import urllib.parse

from cryptography.fernet import Fernet

from http_fakes import ScriptedHttp
from tires_sync import connections, order_alerts, order_manager, order_notify, order_points as points, orders, store, users
from tires_sync.secretbox import SecretBox

NOW = "2026-10-15T12:00:00"
TG_OK = (200, {"ok": True, "result": {}})
VK_OK = (200, {"response": 1})
VK_FAIL = (200, {"error": {"error_code": 901, "error_msg": "Can't send messages"}})
OWN_ACCOUNT, POINT_ACCOUNT = 1, 3


def env(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    orders.ensure_pipeline(conn, NOW)
    box = SecretBox(Fernet.generate_key())
    connections.set_config(conn, "telegram", 0, {"chat_id": "1000"}, NOW, "owner")                                # the owner's chat
    connections.set_secret(conn, box, "telegram", 0, "bot_token", "123456:ABCdef-TEST", NOW, "owner")
    connections.set_secret(conn, box, "vk", 0, "token", "vk1.a.TEST", NOW, "owner")
    conn.execute("INSERT INTO account (id, name, feed_token, created, chats_enabled) VALUES (1, 'Альфа', 't1', '2026-10-01', 1), "
                 "(3, 'Гамма-точка', 't3', '2026-10-01', 1)")
    users.add_user(conn, "partner1", "пароль-для-партнёра-1", NOW, role="partner")
    conn.commit()
    return conn, box


def point(conn, channel="vk", peer="777", account=POINT_ACCOUNT, name="Гамма"):
    pid = points.save_point(conn, {"name": name, "manager_name": "Алексей", "channel": channel})
    conn.execute("UPDATE point SET vk_peer_id = ?, avito_account_id = ? WHERE id = ?", (peer, account, pid))
    conn.commit()
    return pid


def card(conn, responsible="owner", point_id=None):
    fields = {"client_name": "Иван", "responsible": responsible}
    if point_id:
        fields["point_id"] = str(point_id)
    return orders.get_card(conn, orders.create_card(conn, fields, NOW, "чаты"))


def chat(conn, account, order_id=None, peer="p1", unread=0):
    row = conn.execute("INSERT INTO avito_chat (account_id, chat_id, peer_id, peer_name, order_id, unread) VALUES (?,?,?,?,?,?)",
                       (account, "c-%s-%d-%s" % (peer, account, order_id), peer, "Клиент", order_id, unread)).lastrowid
    conn.commit()
    return row


def call(conn, account, order_id):
    conn.execute("INSERT INTO avito_call (account_id, call_id, call_at, order_id) VALUES (?,?,?,?)", (account, order_id * 10, NOW, order_id))
    conn.commit()


def sent_text(call_) -> str:
    body = call_[3].decode("utf-8")
    return json.loads(body)["text"] if body.startswith("{") else urllib.parse.parse_qs(body)["message"][0]


# ---- the alert to whoever answers for the order (order_notify) --------------------------------------------------------------------------

def test_an_order_of_the_point_account_goes_to_the_point_not_to_the_owner_who_answers_for_it(tmp_path):
    conn, box = env(tmp_path)
    pid = point(conn)
    mine = card(conn, "owner", pid)
    chat(conn, POINT_ACCOUNT, mine["id"])
    assert order_notify.route(conn, mine)["channel"] == "vk"
    tg, vk = ScriptedHttp(TG_OK), ScriptedHttp(VK_OK)
    assert order_notify.deliver(conn, box, mine, "Заказ №1: есть аналоги", NOW, tg_http=tg, vk_http=vk).startswith("отправлено в ВК")
    assert tg.calls == [] and len(vk.calls) == 1


def test_the_partner_who_answers_for_a_point_account_order_gets_no_telegram_either(tmp_path):
    conn, box = env(tmp_path)
    order_notify.set_user_chat(conn, "partner1", "555", NOW, "owner")
    pid = point(conn, channel="panel")
    mine = card(conn, "partner1", pid)
    chat(conn, POINT_ACCOUNT, mine["id"])
    tg = ScriptedHttp(TG_OK)
    assert order_notify.deliver(conn, box, mine, "Конец резерва", NOW, tg_http=tg).startswith("показано точке")
    assert tg.calls == []


def test_a_point_user_who_answers_for_its_own_account_order_gets_it_from_the_point_not_in_telegram(tmp_path):
    """Changed on purpose (the owner 2026-10-26, plan answer 8: no Telegram for a point, the bell with the sound is enough): it was the
    user's Telegram; now the point's own way (its VK here, «Моя точка» in the panel), even with a chat saved for the user."""
    conn, box = env(tmp_path)
    pid = point(conn)
    users.add_user(conn, "gamma1", "пароль-для-точки-1", NOW, role="point", point_id=pid)
    order_notify.set_user_chat(conn, "gamma1", "777000", NOW, "owner")
    mine = card(conn, "gamma1", pid)
    chat(conn, POINT_ACCOUNT, mine["id"])
    way = order_notify.route(conn, mine)
    assert (way["channel"], way["to"], way["why"]) == ("vk", "777", "Гамма")
    conn.execute("UPDATE point SET channel = 'panel' WHERE id = ?", (pid,))
    conn.commit()
    tg = ScriptedHttp(TG_OK)
    assert order_notify.deliver(conn, box, mine, "Конец резерва", NOW, tg_http=tg).startswith("показано точке")
    assert tg.calls == []


def test_a_point_user_who_answers_for_an_own_account_order_gets_it_from_the_point_not_in_telegram(tmp_path):
    """The same for an order of an account of no point: a point user is told through its point (the owner 2026-10-26), never Telegram."""
    conn, box = env(tmp_path)
    pid = point(conn, channel="panel", peer="")
    users.add_user(conn, "gamma1", "пароль-для-точки-1", NOW, role="point", point_id=pid)
    order_notify.set_user_chat(conn, "gamma1", "777000", NOW, "owner")
    mine = card(conn, "gamma1", pid)
    chat(conn, OWN_ACCOUNT, mine["id"])
    tg = ScriptedHttp(TG_OK)
    assert order_notify.route(conn, mine) == {"channel": "panel", "to": str(pid), "why": "Гамма"}
    assert order_notify.deliver(conn, box, mine, "Конец резерва", NOW, tg_http=tg).startswith("показано точке")
    assert tg.calls == [] and [n["text"] for n in points.notes(conn, pid)] == ["Конец резерва"]
    elsewhere = card(conn, "gamma1")                                                       # no point on the order: the user's own point
    assert order_notify.route(conn, elsewhere)["channel"] == "panel"


def test_with_no_way_to_the_point_the_alert_stays_on_its_page_and_the_owner_is_not_told(tmp_path):
    conn, box = env(tmp_path)
    pid = point(conn, channel="", peer="")
    mine = card(conn, "owner", pid)
    chat(conn, POINT_ACCOUNT, mine["id"])
    tg = ScriptedHttp(TG_OK)
    told = order_notify.deliver(conn, box, mine, "Конец резерва", NOW, tg_http=tg)
    assert tg.calls == [] and "владельц" not in told.replace("владельцу не пишется", "")
    assert [n["text"] for n in points.unseen_alerts(conn, pid, "")] == ["Конец резерва"]
    journal = [e["text"] for e in orders.list_events(conn, mine["id"])]
    assert any("Гамма" in t and "Моя точка" in t for t in journal)


def test_a_refusal_of_the_point_vk_leaves_it_on_the_point_page_not_with_the_owner(tmp_path):
    conn, box = env(tmp_path)
    pid = point(conn)
    mine = card(conn, "owner", pid)
    call(conn, POINT_ACCOUNT, mine["id"])                                     # a call of the point's account makes it the point's too
    tg, vk = ScriptedHttp(TG_OK), ScriptedHttp(VK_FAIL)
    order_notify.deliver(conn, box, mine, "Клиент не пришёл", NOW, tg_http=tg, vk_http=vk)
    assert tg.calls == []
    assert [n["text"] for n in points.unseen_alerts(conn, pid, "")] == ["Клиент не пришёл"]


def test_an_own_account_order_delivered_to_a_point_still_goes_to_its_responsible(tmp_path):
    conn, box = env(tmp_path)
    pid = point(conn)
    mine = card(conn, "owner", pid)
    chat(conn, OWN_ACCOUNT, mine["id"])
    assert order_notify.route(conn, mine) == {"channel": "telegram", "to": "1000", "why": "owner"}
    no_chat = card(conn, "owner", pid)                                        # an order with no chat nor call: as before
    assert order_notify.route(conn, no_chat)["channel"] == "telegram"


def test_an_order_with_a_chat_of_an_own_account_too_is_never_lost_for_the_owner(tmp_path):
    conn, box = env(tmp_path)
    pid = point(conn)
    mine = card(conn, "owner", pid)
    chat(conn, POINT_ACCOUNT, mine["id"], peer="a")
    chat(conn, OWN_ACCOUNT, mine["id"], peer="b")
    assert order_notify.route(conn, mine)["channel"] == "telegram"


def test_a_client_the_point_handed_over_stays_with_the_staff(tmp_path):
    conn, box = env(tmp_path)
    pid = point(conn)
    mine = card(conn, "owner", pid)
    chat(conn, POINT_ACCOUNT, mine["id"])
    conn.execute("INSERT INTO order_handoff (order_id, message_id, point_id, created_at) VALUES (?, 'h1', ?, ?)", (mine["id"], pid, NOW))
    conn.commit()
    assert order_notify.route(conn, mine) == {"channel": "telegram", "to": "1000", "why": "owner"}


def _no_show(conn, box, mine, tg, vk):
    return order_manager.no_show(conn, mine, "менеджер Гамма", NOW,
                                 lambda c, text: order_notify.deliver(conn, box, c, text, NOW, tg_http=tg, vk_http=vk))


def test_a_no_show_the_point_reports_on_its_own_account_order_goes_to_the_owner_not_back_to_the_point(tmp_path):
    conn, box = env(tmp_path)                                                 # the owner 2026-10-26: «С клиентом свяжутся» stays true
    pid = point(conn)
    mine = card(conn, "owner", pid)
    chat(conn, POINT_ACCOUNT, mine["id"])
    tg, vk = ScriptedHttp(TG_OK), ScriptedHttp(VK_OK)
    outcome, answer = _no_show(conn, box, mine, tg, vk)
    assert vk.calls == [] and len(tg.calls) == 1 and "Клиент не пришёл" in sent_text(tg.calls[0])
    assert "отправлено в Telegram (owner)" in outcome and "С клиентом свяжутся" in answer
    assert points.unseen_alerts(conn, pid, "") == []
    assert order_notify.route(conn, mine)["channel"] == "vk"                  # the other alerts of the order still go to the point


def test_a_no_show_of_a_point_account_order_goes_to_the_partner_who_answers_for_it(tmp_path):
    conn, box = env(tmp_path)
    order_notify.set_user_chat(conn, "partner1", "555", NOW, "owner")
    pid = point(conn, channel="panel")
    mine = card(conn, "partner1", pid)
    chat(conn, POINT_ACCOUNT, mine["id"])
    tg = ScriptedHttp(TG_OK)
    outcome, _answer = _no_show(conn, box, mine, tg, ScriptedHttp())
    assert "отправлено в Telegram (partner1)" in outcome and len(tg.calls) == 1 and "555" in tg.calls[0][3].decode("utf-8")
    assert points.unseen_alerts(conn, pid, "") == []


def test_a_no_show_of_a_point_account_order_nobody_in_the_panel_answers_for_goes_to_the_owner(tmp_path):
    conn, box = env(tmp_path)
    for responsible, channel in (("chats-robot", "vk"), ("chats-robot", "panel")):
        pid = point(conn, channel=channel, name="Гамма-" + channel)
        conn.execute("UPDATE point SET avito_account_id = NULL WHERE id != ?", (pid,))
        mine = card(conn, responsible, pid)
        chat(conn, POINT_ACCOUNT, mine["id"], peer=channel)
        tg, vk = ScriptedHttp(TG_OK), ScriptedHttp(VK_OK)
        outcome, _answer = _no_show(conn, box, mine, tg, vk)
        assert vk.calls == [] and len(tg.calls) == 1 and "отправлено в Telegram (owner)" in outcome, outcome
        assert points.unseen_alerts(conn, pid, "") == []


def test_a_no_show_of_an_order_a_point_user_answers_for_goes_to_the_owner_not_to_that_user(tmp_path):
    conn, box = env(tmp_path)                                                 # review 1: a deal the point made answers to its user
    pid = point(conn)
    users.add_user(conn, "gamma1", "пароль-для-точки-1", NOW, role="point", point_id=pid)
    order_notify.set_user_chat(conn, "gamma1", "777000", NOW, "owner")
    mine = card(conn, "gamma1", pid)
    chat(conn, POINT_ACCOUNT, mine["id"])
    tg, vk = ScriptedHttp(TG_OK), ScriptedHttp(VK_OK)
    outcome, _answer = _no_show(conn, box, mine, tg, vk)
    assert vk.calls == [] and len(tg.calls) == 1 and "отправлено в Telegram (owner)" in outcome, outcome
    assert "777000" not in tg.calls[0][3].decode("utf-8")


def test_a_no_show_with_nobody_to_tell_goes_to_the_owner_not_to_the_point_page(tmp_path):
    conn, box = env(tmp_path)
    users.add_user(conn, "owner2", "пароль-для-владельца-2", NOW, role="owner")
    pid = point(conn)
    mine = card(conn, "owner2", pid)                                          # a staff member with no Telegram chat
    chat(conn, POINT_ACCOUNT, mine["id"])
    tg, vk = ScriptedHttp(TG_OK), ScriptedHttp(VK_OK)
    outcome, _answer = _no_show(conn, box, mine, tg, vk)
    assert vk.calls == [] and "владельцу написано" in outcome
    assert points.unseen_alerts(conn, pid, "") == []


def test_the_safety_net_of_unseen_alerts_skips_the_point_account_orders(tmp_path):
    conn, box = env(tmp_path)
    pid = point(conn, channel="panel")
    store.set_state(conn, order_notify.UNSEEN_START_KEY, "2026-10-01T00:00:00")
    theirs = card(conn, "chats-robot", pid)
    chat(conn, POINT_ACCOUNT, theirs["id"])
    ours = card(conn, "chats-robot", pid)
    chat(conn, OWN_ACCOUNT, ours["id"], peer="b")
    assert order_notify.deliver(conn, box, theirs, "Их заказ", NOW).startswith("показано точке")
    assert order_notify.deliver(conn, box, ours, "Наш заказ", NOW).startswith("показано точке")
    tg = ScriptedHttp(TG_OK, TG_OK)
    assert order_notify.escalate_unseen(conn, box, "2026-10-15T12:30:00", tg_http=tg) == 1
    assert len(tg.calls) == 1 and "Наш заказ" in sent_text(tg.calls[0])


# ---- the bell, its counter and «Уведомления» (order_alerts.page_rows) -------------------------------------------------------------------

def test_the_staff_do_not_see_the_point_account_alerts_and_the_point_does(tmp_path):
    conn, box = env(tmp_path)
    pid = point(conn, channel="panel")
    users.add_user(conn, "gamma1", "пароль-для-точки-1", NOW, role="point", point_id=pid)
    theirs = card(conn, "owner", pid)
    chat(conn, POINT_ACCOUNT, theirs["id"])
    ours = card(conn, "owner", pid)
    chat(conn, OWN_ACCOUNT, ours["id"], peer="b")
    handed = card(conn, "owner", pid)
    chat(conn, POINT_ACCOUNT, handed["id"], peer="h")
    conn.execute("INSERT INTO order_handoff (order_id, message_id, point_id, created_at) VALUES (?, 'h1', ?, ?)", (handed["id"], pid, NOW))
    for cid in (theirs["id"], ours["id"], handed["id"]):
        orders.add_event(conn, cid, NOW, "система", "reserve", "Резерв заканчивается", level="yellow")
    for account, peer in ((POINT_ACCOUNT, "x"), (OWN_ACCOUNT, "y")):         # chats without an order whose client waits
        row = chat(conn, account, None, peer=peer, unread=1)
        conn.execute("INSERT INTO avito_message (chat_row, message_id, at, ts, direction, kind, text) VALUES (?,?,?,?,?,?,?)",
                     (row, "m-%s" % peer, NOW, 1, "in", "text", "Есть шины?"))
    conn.commit()
    for login in ("owner", "partner1"):
        rows = order_alerts.page_rows(conn, login, NOW)
        orders_seen = {r["order_id"] for r in rows if r["order_id"] is not None}
        assert theirs["id"] not in orders_seen and {ours["id"], handed["id"]} <= orders_seen
        chats_seen = {r["chat_row"] for r in rows if r.get("chat_row")}
        accounts = {conn.execute("SELECT account_id FROM avito_chat WHERE id = ?", (c,)).fetchone()[0] for c in chats_seen}
        assert accounts == {OWN_ACCOUNT}
        assert theirs["id"] not in order_alerts.order_levels(conn, login, NOW)
    orders.add_event(conn, theirs["id"], NOW, "система", "rule", "Задача не подготовлена", level="red")
    conn.commit()
    assert order_alerts.counts(conn, "owner")["red"] == 0                     # «Красные уведомления» of the dashboard
    assert order_alerts.counts(conn, "gamma1")["red"] == 1
    point_rows = order_alerts.page_rows(conn, "gamma1", NOW)
    assert theirs["id"] in {r["order_id"] for r in point_rows}
    assert any(r.get("chat_row") and r["order_id"] is None and "Есть шины" in r["text"] for r in point_rows)


def test_an_order_of_the_point_account_with_another_point_or_none_stays_with_the_staff(tmp_path):
    conn, box = env(tmp_path)
    gamma = point(conn)
    delta = point(conn, peer="888", account=None, name="Дельта")
    elsewhere = card(conn, "owner", delta)                                    # the staff moved it to another point: that point's bell
    chat(conn, POINT_ACCOUNT, elsewhere["id"], peer="a")                      # would not be the one told: as before, nothing lost
    nowhere = card(conn, "owner")
    chat(conn, POINT_ACCOUNT, nowhere["id"], peer="b")
    for mine in (elsewhere, nowhere):
        assert order_notify.route(conn, mine)["channel"] == "telegram"
        orders.add_event(conn, mine["id"], NOW, "система", "rule", "Задача не подготовлена", level="red")
    conn.commit()
    assert {elsewhere["id"], nowhere["id"]} <= {r["order_id"] for r in order_alerts.page_rows(conn, "owner", NOW)}
    assert points.of_account(conn, elsewhere["id"]) is None and gamma


def test_the_safety_net_marks_a_point_account_note_so_that_it_is_not_looked_at_again(tmp_path):
    conn, box = env(tmp_path)
    pid = point(conn, channel="panel")
    store.set_state(conn, order_notify.UNSEEN_START_KEY, "2026-10-01T00:00:00")
    theirs = card(conn, "chats-robot", pid)
    chat(conn, POINT_ACCOUNT, theirs["id"])
    order_notify.deliver(conn, box, theirs, "Их заказ", NOW)
    tg = ScriptedHttp(TG_OK)
    assert order_notify.escalate_unseen(conn, box, "2026-10-15T12:30:00", tg_http=tg) == 0
    assert points.unseen_alerts(conn, pid, "") == [] and tg.calls == []


# ---- lane 3: the safety net never tells the owner what the point already opened ---------------------------------------------------------

def unseen_order_alert(tmp_path):
    """An alert of an own-account order (it may go to the owner) left on the «Моя точка» of a point in the panel at NOW."""
    conn, box = env(tmp_path)
    pid = point(conn, channel="panel")
    users.add_user(conn, "gamma1", "пароль-для-точки-1", NOW, role="point", point_id=pid)
    store.set_state(conn, order_notify.UNSEEN_START_KEY, "2026-10-01T00:00:00")
    ours = card(conn, "chats-robot", pid)
    chat(conn, OWN_ACCOUNT, ours["id"], peer="b")
    assert order_notify.deliver(conn, box, ours, "Наш заказ", NOW).startswith("показано точке")
    return conn, box, pid, ours


def test_the_safety_net_tells_the_owner_nothing_of_an_order_the_point_opened(tmp_path):
    conn, box, pid, ours = unseen_order_alert(tmp_path)
    order_alerts.mark_seen(conn, "gamma1", ours["id"], "2026-10-15T12:05:00")          # the bell → the order's card, not «Моя точка»
    conn.commit()
    tg = ScriptedHttp(TG_OK)
    assert order_notify.escalate_unseen(conn, box, "2026-10-15T12:30:00", tg_http=tg) == 0
    assert tg.calls == [] and points.unseen_alerts(conn, pid, "") == []                    # marked: not looked at again


def test_the_safety_net_still_tells_the_owner_when_only_the_staff_or_an_older_look_opened_it(tmp_path):
    conn, box, pid, ours = unseen_order_alert(tmp_path)
    order_alerts.mark_seen(conn, "gamma1", ours["id"], "2026-10-15T11:00:00")          # before the alert: not a look at it
    order_alerts.mark_seen(conn, "owner", ours["id"], "2026-10-15T12:05:00")           # the owner's look is not the point's
    conn.commit()
    tg = ScriptedHttp(TG_OK)
    assert order_notify.escalate_unseen(conn, box, "2026-10-15T12:30:00", tg_http=tg) == 1
    assert len(tg.calls) == 1 and "Наш заказ" in sent_text(tg.calls[0])
