"""The alert to the responsible manager of a waiting order (W2b): who gets it, through what, and what happens when it cannot be sent."""
from __future__ import annotations

import json
import urllib.parse
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from helpers import make_offer
from http_fakes import ScriptedHttp
from tires_sync import connections, order_notify, order_points as points, order_wait, orders, store, users
from tires_sync.models import Season
from tires_sync.secretbox import SecretBox

NOW = "2026-10-15T12:00:00"
BOT = "123456:ABCdef-SECRET"
VK_KEY = "vk1.a.COMMUNITY"
WANT = dict(brand="Ikon Tyres", width=205, profile=55, radius=16, season="winter", studded="no", qty=4)
TG_OK = (200, {"ok": True, "result": {}})
VK_OK = (200, {"response": 1})


def env(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    orders.ensure_pipeline(conn, NOW)
    box = SecretBox(Fernet.generate_key())
    connections.set_config(conn, "telegram", 0, {"chat_id": "1000"}, NOW, "owner")                                # the owner's chat
    connections.set_secret(conn, box, "telegram", 0, "bot_token", BOT, NOW, "owner")
    connections.set_secret(conn, box, "vk", 0, "token", VK_KEY, NOW, "owner")
    users.add_user(conn, "worker2", "пароль-для-партнёра-1", NOW)
    return conn, box


def point(conn, channel="vk", peer="777", name="Гамма"):
    pid = points.save_point(conn, {"name": name, "manager_name": "Алексей", "channel": channel})
    conn.execute("UPDATE point SET vk_peer_id = ? WHERE id = ?", (peer, pid))
    conn.commit()
    return pid


def card(conn, responsible="", point_id=None, author="чаты"):
    fields = {"client_name": "Иван", "responsible": responsible}
    if point_id:
        fields["point_id"] = str(point_id)
    return orders.get_card(conn, orders.create_card(conn, fields, NOW, author))


def test_the_chat_of_a_panel_user_is_kept_checked_and_can_be_removed(tmp_path):
    conn, box = env(tmp_path)
    order_notify.set_user_chat(conn, "worker2", " 555 ", NOW, "owner")
    assert order_notify.user_chats(conn) == {"worker2": "555"}
    order_notify.set_user_chat(conn, "pavel", "-100200", NOW, "owner")                                           # a group chat has a minus
    assert order_notify.user_chats(conn)["pavel"] == "-100200"
    for bad in ("abc", "12 34", "1" * 40):
        with pytest.raises(orders.OrderError):
            order_notify.set_user_chat(conn, "worker2", bad, NOW, "owner")
    with pytest.raises(orders.OrderError):
        order_notify.set_user_chat(conn, "", "5", NOW, "owner")
    order_notify.set_user_chat(conn, "worker2", "", NOW, "owner")
    assert "worker2" not in order_notify.user_chats(conn)


def test_the_route_goes_to_the_telegram_of_a_panel_user_the_owner_included(tmp_path):
    conn, box = env(tmp_path)
    order_notify.set_user_chat(conn, "worker2", "555", NOW, "owner")
    assert order_notify.route(conn, card(conn, "worker2")) == {"channel": "telegram", "to": "555", "why": "worker2"}
    assert order_notify.route(conn, card(conn, "owner")) == {"channel": "telegram", "to": "1000", "why": "owner"}      # the owner's own chat of the failure alerts


def test_a_panel_user_without_a_chat_gets_nothing_directly_and_the_owner_is_told(tmp_path):
    conn, box = env(tmp_path)
    route = order_notify.route(conn, card(conn, "worker2"))
    assert route["channel"] == "owner" and "worker2" in route["why"] and "Telegram" in route["why"]


def test_somebody_who_is_not_in_the_panel_is_answered_through_the_vk_of_the_point_of_the_order(tmp_path):
    conn, box = env(tmp_path)
    pid = point(conn)
    assert order_notify.route(conn, card(conn, "ghost", pid)) == {"channel": "vk", "to": "777", "why": "Гамма"}
    assert order_notify.route(conn, card(conn, "", pid))["channel"] == "vk"                                    # nobody named: the robot made it: not in the panel


def test_without_a_point_or_without_vk_the_owner_is_told_why(tmp_path):
    conn, box = env(tmp_path)
    assert order_notify.route(conn, card(conn, "ghost")) ["channel"] == "owner"
    assert "точк" in order_notify.route(conn, card(conn, "ghost"))["why"]
    unbound = point(conn, peer="", name="Бета")
    assert "ВК" in order_notify.route(conn, card(conn, "ghost", unbound))["why"]
    telegram_point = point(conn, channel="telegram", peer="777", name="Альфа")
    assert order_notify.route(conn, card(conn, "ghost", telegram_point))["channel"] == "owner"


def sent_text(call) -> str:
    body = call[3].decode("utf-8")
    return json.loads(body)["text"] if body.startswith("{") else urllib.parse.parse_qs(body)["message"][0]


def test_the_alert_is_delivered_by_the_chosen_channel_and_written_in_the_feed_of_the_order(tmp_path):
    conn, box = env(tmp_path)
    order_notify.set_user_chat(conn, "worker2", "555", NOW, "owner")
    tg, vk = ScriptedHttp(TG_OK), ScriptedHttp(VK_OK)
    c = card(conn, "worker2")
    assert order_notify.deliver(conn, box, c, "Позиция появилась", NOW, tg_http=tg, vk_http=vk).startswith("отправлено")
    assert json.loads(tg.calls[0][3])["chat_id"] == "555" and "Позиция появилась" in sent_text(tg.calls[0]) and vk.calls == []
    assert any("Оповещение" in e["text"] and "worker2" in e["text"] for e in orders.list_events(conn, c["id"]))
    pid = point(conn)
    tg2, vk2 = ScriptedHttp(), ScriptedHttp(VK_OK)
    assert order_notify.deliver(conn, box, card(conn, "ghost", pid), "Есть аналоги", NOW, tg_http=tg2, vk_http=vk2).startswith("отправлено")
    assert tg2.calls == [] and urllib.parse.parse_qs(vk2.calls[0][3].decode("utf-8"))["peer_id"] == ["777"]


def test_a_refusal_of_the_channel_falls_back_to_the_owner_and_never_raises(tmp_path):
    conn, box = env(tmp_path)
    order_notify.set_user_chat(conn, "worker2", "555", NOW, "owner")
    tg = ScriptedHttp((403, {}), TG_OK)                                                                         # the user's chat refuses, the owner's chat takes the fallback
    c = card(conn, "worker2")
    status = order_notify.deliver(conn, box, c, "Позиция появилась", NOW, tg_http=tg)
    assert status.startswith("не отправлено") and len(tg.calls) == 2
    assert json.loads(tg.calls[1][3])["chat_id"] == "1000" and "некому отправить" in sent_text(tg.calls[1]).lower()
    assert any("не отправлено" in e["text"] for e in orders.list_events(conn, c["id"]))
    bare = store.connect(str(tmp_path / "bare.db"))
    orders.ensure_pipeline(bare, NOW)
    nobody = order_notify.deliver(bare, None, card(bare, "ghost"), "текст", NOW)                                # no key, no Telegram at all: only the card tells
    assert nobody.startswith("не отправлено")


def test_a_new_task_asks_for_one_alert_and_the_next_cycle_does_not_repeat_it(tmp_path):
    conn, box = env(tmp_path)
    card_id = orders.create_card(conn, {"client_name": "Иван"}, NOW, "worker2")
    order_wait.add_want(conn, card_id, WANT, NOW, "worker2")
    orders.move_card(conn, card_id, {s["code"]: s["id"] for s in orders.list_stages(conn)}["wait"], NOW, "worker2")
    offers = [make_offer(brand_key="IKON TYRES", width=205, height=55, diameter=16, season=Season.WINTER, studded=False, stock=8)]
    calls = []
    order_wait.check(conn, offers, NOW, notify=lambda cid, title, body: calls.append((cid, title, body)))
    order_wait.check(conn, offers, "2026-10-15T13:00:00", notify=lambda cid, title, body: calls.append("again"))
    assert len(calls) == 1 and calls[0][0] == card_id and "предложите клиенту" in calls[0][1] and "Заказ №%d" % card_id in calls[0][2]


def test_a_failing_alert_never_stops_the_check(tmp_path):
    conn, box = env(tmp_path)
    ids = []
    for _ in range(2):
        cid = orders.create_card(conn, {"client_name": "Иван"}, NOW, "worker2")
        order_wait.add_want(conn, cid, WANT, NOW, "worker2")
        orders.move_card(conn, cid, {s["code"]: s["id"] for s in orders.list_stages(conn)}["wait"], NOW, "worker2")
        ids.append(cid)

    def boom(cid, title, body):
        raise RuntimeError("boom")
    found = order_wait.check(conn, [make_offer(brand_key="IKON TYRES", width=205, height=55, diameter=16, season=Season.WINTER, studded=False, stock=9)],
                             NOW, notify=boom)
    assert found["exact"] == ids and all("Появилось" in orders.get_card(conn, i)["tags"] for i in ids)


def test_the_cycle_hook_alerts_through_order_notify_when_it_has_the_key(tmp_path, monkeypatch):
    from tires_sync import cli
    conn, box = env(tmp_path)
    cid = orders.create_card(conn, {"client_name": "Иван", "responsible": "worker2"}, NOW, "worker2")
    order_wait.add_want(conn, cid, WANT, NOW, "worker2")
    orders.move_card(conn, cid, {s["code"]: s["id"] for s in orders.list_stages(conn)}["wait"], NOW, "worker2")
    got = []
    monkeypatch.setattr(order_notify, "deliver", lambda conn, box, card, text, now, **k: got.append((card["id"], text)) or "отправлено")
    parsed = SimpleNamespace(offers={"1": make_offer(brand_key="IKON TYRES", width=205, height=55, diameter=16, season=Season.WINTER, studded=False, stock=9)})
    cli._check_waiting(conn, parsed, box)
    assert len(got) == 1 and got[0][0] == cid and "Позиция появилась" in got[0][1]
    got.clear()
    cli._check_waiting(conn, parsed, None)                                                                       # no key: the cards are still tagged, nobody is written to
    assert got == []


def test_the_telegram_tab_keeps_the_chats_of_the_panel_users(tmp_path):
    from datetime import datetime
    from panel_helpers import login, make_app, post
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 10, 15, 9, 0, 0))
    client = app.test_client()
    tok = login(client)
    conn = store.connect(app.config["DB_PATH"])
    users.add_user(conn, "worker2", "пароль-для-партнёра-1", NOW)
    page = client.get("/connections/telegram").get_data(as_text=True)
    assert "Чаты менеджеров панели" in page and 'value="worker2"' in page
    post(client, "/connections/telegram/user-chat", tok, login="worker2", chat_id="555")
    assert order_notify.user_chats(conn) == {"worker2": "555"}
    assert 'value="555"' in client.get("/connections/telegram").get_data(as_text=True)
    post(client, "/connections/telegram/user-chat", tok, login="worker2", chat_id="не число")
    assert order_notify.user_chats(conn) == {"worker2": "555"} and "Чат Telegram" in client.get("/connections/telegram").get_data(as_text=True)
    post(client, "/connections/telegram/user-chat", tok, login="nobody", chat_id="9")
    assert "nobody" not in order_notify.user_chats(conn)


# ---- the review of W2

def test_ten_tags_do_not_take_the_alert_away(tmp_path):
    conn, box = env(tmp_path)
    cid = orders.create_card(conn, {"client_name": "Иван", "tags": ",".join("т%d" % i for i in range(10))}, NOW, "worker2")
    order_wait.add_want(conn, cid, WANT, NOW, "worker2")
    orders.move_card(conn, cid, {s["code"]: s["id"] for s in orders.list_stages(conn)}["wait"], NOW, "worker2")
    calls = []
    order_wait.check(conn, [make_offer(brand_key="IKON TYRES", width=205, height=55, diameter=16, season=Season.WINTER, studded=False, stock=9)],
                     NOW, notify=lambda c, title, body: calls.append(c))
    assert calls == [cid]                                                                                       # the tag did not fit, the person is still told


def test_no_key_of_telegram_or_vk_reaches_the_feed_or_the_message_to_the_owner(tmp_path):
    conn, box = env(tmp_path)
    order_notify.set_user_chat(conn, "worker2", "555", NOW, "owner")
    pid = point(conn)
    for responsible, point_id in (("worker2", None), ("ghost", pid)):
        c = card(conn, responsible, point_id)
        tg = ScriptedHttp((403, {}), TG_OK)
        vk = ScriptedHttp((200, {"error": {"error_code": 901, "error_msg": "no permission"}}))
        order_notify.deliver(conn, box, c, "Позиция появилась", NOW, tg_http=tg, vk_http=vk)
        feed = " ".join(e["text"] for e in orders.list_events(conn, c["id"]))
        owner_message = " ".join(sent_text(call) for call in tg.calls[1:])
        assert BOT not in feed + owner_message and VK_KEY not in feed + owner_message


def test_the_owner_login_comes_from_the_panel_setting_and_is_owner_by_default(tmp_path):
    conn, box = env(tmp_path)
    assert order_notify.get_owner_login(conn) == "owner"
    order_notify.set_user_chat(conn, "worker2", "555", NOW, "owner")
    order_notify.set_owner_login(conn, " pavel ", NOW, "owner")
    assert order_notify.get_owner_login(conn) == "pavel" and order_notify.user_chats(conn) == {"worker2": "555"}
    assert order_notify.route(conn, card(conn, "pavel")) == {"channel": "telegram", "to": "1000", "why": "pavel"}     # the owner's chat of the failure alerts
    assert order_notify.route(conn, card(conn, "owner"))["channel"] == "owner"                                       # «owner» is not a panel login any more
    tg = ScriptedHttp(TG_OK)
    assert order_notify.deliver(conn, box, card(conn, "pavel"), "Позиция появилась", NOW, tg_http=tg).startswith("отправлено")
    assert json.loads(tg.calls[0][3])["chat_id"] == "1000"
    assert order_notify.route(conn, card(conn, "owner"), owner_login="owner")["to"] == "1000"                      # a login given by the caller wins
    order_notify.set_user_chat(conn, "worker2", "", NOW, "owner")
    assert order_notify.get_owner_login(conn) == "pavel"                                                           # a chat saved later keeps the setting
    assert [a["detail"] for a in store.list_audit(conn) if a["action"] == "owner_login"] == [{"from": "owner", "to": "pavel"}]
    order_notify.set_owner_login(conn, "", NOW, "owner")
    assert order_notify.get_owner_login(conn) == "owner" and "owner_login" not in connections.get_config(conn, order_notify.KIND, 0)
    with pytest.raises(orders.OrderError):
        order_notify.set_owner_login(conn, "x" * 61, NOW, "owner")


def test_an_alert_to_a_point_in_vk_is_signed_by_the_bot(tmp_path):
    conn, box = env(tmp_path)
    vk = ScriptedHttp(VK_OK)
    assert order_notify.deliver(conn, box, card(conn, "ghost", point(conn)), "Есть аналоги", NOW, tg_http=ScriptedHttp(), vk_http=vk).startswith("отправлено")
    assert sent_text(vk.calls[0]) == "(бот) Есть аналоги"                                   # the owner 2026-10-24: every message of the bot in VK


# A point names an order the panel does not know: the partner is told; with no active partner the owners (the lead 2026-10-24).

def test_the_people_told_of_a_missing_order_are_the_active_partners(tmp_path):
    conn, box = env(tmp_path)
    users.add_user(conn, "pasha", "пароль-для-паши-123", NOW)
    users.set_role(conn, "worker2", "partner", None, "", NOW, "owner")
    assert order_notify.missing_order_people(conn) == ["worker2"]
    users.add_user(conn, "worker3", "пароль-для-партнёра-2", NOW, role="partner")
    users.set_active(conn, "worker3", False, NOW, "owner")
    assert order_notify.missing_order_people(conn) == ["worker2"]                              # a partner switched off is not told


def test_with_no_active_partner_the_owners_are_told_so_that_it_is_never_lost(tmp_path):
    conn, box = env(tmp_path)                                                                # «worker2» has no role row: an owner, as on the server now
    assert order_notify.missing_order_people(conn) == ["owner", "worker2"]
    users.set_role(conn, "worker2", "partner", None, "", NOW, "owner")
    users.set_active(conn, "worker2", False, NOW, "owner")
    users.add_user(conn, "beta", "пароль-для-беты-1", NOW, role="point", point_id=point(conn))
    assert order_notify.missing_order_people(conn) == ["owner"]                             # the owner's login of the alerts; never a point


def test_telegram_goes_once_per_chat_to_the_people_who_have_one(tmp_path):
    conn, box = env(tmp_path)
    users.add_user(conn, "pasha", "пароль-для-паши-123", NOW)
    order_notify.set_user_chat(conn, "worker2", "555", NOW, "owner")
    order_notify.set_user_chat(conn, "pasha", "1000", NOW, "owner")                         # the same chat as the owner's alerts
    tg = ScriptedHttp(TG_OK, TG_OK)
    how = order_notify.tell_people(conn, box, ["owner", "pasha", "worker2", "nobody"], "Точка сообщила о заказе", tg)
    assert sorted(json.loads(c[3])["chat_id"] for c in tg.calls) == ["1000", "555"]
    assert how == "Telegram: отправлено (owner, pasha, worker2)"
    assert order_notify.tell_people(conn, box, ["nobody"], "x", ScriptedHttp()) == "Telegram: ни у кого нет чата (nobody)"
    assert order_notify.tell_people(conn, box, ["worker2"], "x", ScriptedHttp((403, {}))).startswith("Telegram не отправлен (worker2:")
