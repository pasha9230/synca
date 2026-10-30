"""A point that works in the panel (channel «panel», the owner 2026-10-23: «Точки работают в панели, ВК — запасной»): the bot writes nothing
to its VK — neither «Менеджеру: заказ оформлен» (after WebMIM or by the button), nor alerts, nor answers; its VK commands are still done;
what it would have heard is on its «Рабочее место», which is its start page with «На точке» and «Едут к нам» and asks /my-point/feed for the
sound and the browser's notification. A point on «ВК» is as before. Fakes only: nothing leaves the tests."""
from __future__ import annotations

import re

import pytest

from http_fakes import ScriptedHttp
from panel_helpers import post
from test_mypoint import manager
from test_order_messages import FakeVk, client_tasks, need_transfer, point_tasks, with_vk
from test_order_notify import card as notify_card, env, point as notify_point
from test_order_placed import Deliver, panel, ready as placed_ready  # noqa: F401  (panel: the fixture of the press)
from test_vk_commands import PEER, SENT, _card, hook, ready, to_client, to_points
from test_webmim_create import NOW, FakeWebmim, db, place
from tires_sync import order_engine as engine, order_manager as mgr, order_notify, order_placed, order_points as points, orders


def to_panel(conn, pid):
    conn.execute("UPDATE point SET channel = 'panel' WHERE id = ?", (pid,))
    conn.commit()


def point_of(conn, card_id):
    return orders.get_card(conn, card_id)["point_id"]


# ---- the switch ---------------------------------------------------------------------------------------------------------------------

def test_the_panel_is_a_channel_of_a_point_and_binding_by_code_keeps_it(tmp_path):
    conn = db(tmp_path)
    pid = points.save_point(conn, dict(name="Бета", channel="panel"))
    assert points.get_point(conn, pid)["channel"] == "panel" and "panel" in points.CHANNEL_NAMES
    code = points.new_bind_code(conn, pid)
    assert points.bind_vk(conn, code, "555")["channel"] == "panel"                 # the owner 2026-10-23: binding does not change the channel
    other = points.save_point(conn, dict(name="Гамма", channel=""))
    assert points.bind_vk(conn, points.new_bind_code(conn, other), "556")["channel"] == "vk"       # as before
    with pytest.raises(points.PointError):
        points.save_point(conn, dict(name="Альфа", channel="sms"))
    assert points.panel_point_of(conn, "555")["id"] == pid and points.panel_point_of(conn, "556") is None


def test_the_switch_is_on_the_page_of_the_points(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    page = client.get("/connections/points").get_data(as_text=True)
    assert '<option value="panel"' in page and "бот в ВК этой точке ничего не пишет" in page
    r = post(client, "/connections/points/save", tok, id=str(pid), name="Бета", channel="panel", vk_peer_id=PEER)
    assert r.status_code == 302 and points.get_point(conn, pid)["channel"] == "panel"


# ---- «Менеджеру: заказ оформлен» -----------------------------------------------------------------------------------------------------

def test_after_webmim_a_point_in_the_panel_gets_nothing_in_vk_and_the_client_still_does(tmp_path):
    conn = db(tmp_path)
    mine = with_vk(conn, placed_ready(conn))
    to_panel(conn, point_of(conn, mine))
    place(conn, mine, FakeWebmim())
    deliver, vk = Deliver(), FakeVk()
    lines = order_placed.on_placed(conn, mine, "1001", NOW, "owner", deliver, vk)
    assert lines[0].startswith("Клиенту отправлено") and len(deliver.sent) == 1               # nothing changes for the client
    assert vk.sent == [] and "работает в панели" in lines[1] and point_tasks(conn, mine) == []
    [event] = [e for e in orders.list_events(conn, mine) if "работает в панели" in e["text"]]
    assert event["level"] == "grey"                                                          # no alert for the staff
    order_placed.on_placed(conn, mine, "1001", NOW, "owner", deliver, vk)
    assert vk.sent == [] and len(deliver.sent) == 1


def test_the_button_does_not_send_the_point_message_to_a_point_in_the_panel(tmp_path):
    conn = db(tmp_path)
    mine = with_vk(conn, placed_ready(conn))
    need_transfer(conn)
    place(conn, mine, FakeWebmim())
    vk = FakeVk()
    order_placed.on_placed(conn, mine, "1001", NOW, "owner", Deliver(), vk)
    orders.update_card(conn, mine, {"transfer_number": "26R-922352"}, NOW, "owner")
    engine.refresh_tasks(conn, mine, NOW)
    [task] = point_tasks(conn, mine)
    assert task["status"] == "pending" and len(client_tasks(conn, mine)) == 1
    to_panel(conn, point_of(conn, mine))
    with pytest.raises(orders.OrderError, match="работает в панели"):
        engine.send_point_task(conn, task["id"], task["body"], vk, NOW, "owner")
    assert vk.sent == [] and engine.get_task(conn, task["id"])["status"] == "pending"
    assert "работает в панели" in engine.point_target(conn, engine.get_task(conn, task["id"]))[1]       # the window says why


# ---- the alerts ------------------------------------------------------------------------------------------------------------------------

def test_an_alert_for_a_point_in_the_panel_is_on_its_page_and_grey_for_the_staff(tmp_path):
    conn, box = env(tmp_path)
    pid = notify_point(conn, channel="panel")
    c = notify_card(conn, "ghost", pid)
    assert order_notify.route(conn, c) == {"channel": "panel", "to": str(pid), "why": "Гамма"}
    tg, vk = ScriptedHttp(), ScriptedHttp()
    status = order_notify.deliver(conn, box, c, "Заказ №%d: резерв заканчивается" % c["id"], NOW, tg_http=tg, vk_http=vk)
    assert status.startswith("показано точке в панели") and tg.calls == [] and vk.calls == []        # neither VK nor «некому» to the owner
    [note] = points.notes(conn, pid)
    assert note["kind"] == "alert" and note["text"] == "Заказ №%d: резерв заканчивается" % c["id"] and note["order_id"] == c["id"]
    [event] = [e for e in orders.list_events(conn, c["id"]) if e["kind"] == "notify"]
    assert event["level"] == "grey" and "в панели" in event["text"]
    assert order_notify.route(conn, notify_card(conn, "ghost", notify_point(conn, name="Бета", peer="778")))["channel"] == "vk"   # «ВК»: as before


def test_a_handed_over_client_of_a_point_in_the_panel_is_told_to_the_staff_never_to_the_point(tmp_path):
    conn, box = env(tmp_path)
    pid = notify_point(conn, channel="panel")
    told = []
    mgr._tell_staff(conn, notify_card(conn, "ghost", pid), "Новый клиент", lambda card, text: told.append(card["responsible"]) or "ok")
    assert told == [order_notify.get_owner_login(conn)] and points.notes(conn, pid) == []


# ---- the commands from VK ---------------------------------------------------------------------------------------------------------------

def test_a_command_from_vk_of_a_point_in_the_panel_is_done_and_its_answer_stays_in_the_panel(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    to_panel(conn, pid)
    hook(app, conn, "Приехал 5000100")
    assert orders.get_card(conn, card)["stage"]["code"] == "at_point" and len(to_client(app)) == 1    # done, the client hears «можно забирать»
    assert to_points(app) == []                                                                       # nothing to VK: no answer, no keyboard
    [note] = points.notes(conn, pid)
    assert note["kind"] == "answer" and note["text"].startswith("Заказ №5000100 — отмечено: приехал") and note["asked"] == "Приехал 5000100"
    hook(app, conn, "привет", event="e2")                                                             # a hint would go to VK: not now
    assert to_points(app) == []


def test_the_owners_answer_in_the_inbox_to_a_point_in_the_panel_is_on_its_page(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    to_panel(conn, pid)
    hook(app, conn, "Подскажите, когда завоз?")
    [item] = mgr.list_inbox(conn)
    page = client.post("/orders/inbox/%d/answer" % item["id"], data={"csrf": tok, "text": "Завтра утром"}, follow_redirects=True).get_data(as_text=True)
    assert to_points(app) == [] and "ответ показан на её «Рабочем месте»" in page
    assert [(n["text"], n["asked"]) for n in points.notes(conn, pid)] == [("Завтра утром", "Подскажите, когда завоз?")]


def test_a_point_on_vk_is_answered_in_vk_as_before(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    hook(app, conn, "Приехал 5000100")
    [(peer, answer)] = to_points(app)
    assert peer == PEER and answer.startswith("(бот) Заказ №5000100 — отмечено") and points.notes(conn, pid) == []


# ---- «Рабочее место» ------------------------------------------------------------------------------------------------------------------------

def test_my_point_shows_at_the_point_and_coming_with_what_the_message_to_the_manager_said(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    orders.update_card(conn, card, dict(transfer_number="26R-922352", delivery_time="до 18:00"), NOW, "o")
    here = _card(conn, pid, webmim="700200", code="at_point")
    mine = manager(app, conn, pid)
    page = "".join(mine.get("/my-point?tab=" + t).get_data(as_text=True) for t in ("moving", "here"))     # each on its own tab (2026-10-30)
    coming, lying = (re.search(r'<article[^>]*id="order-%d".*?</article>' % c, page, re.S).group(0) for c in (card, here))   # one list (owner 2026-10-26)
    assert "едет на точку" in coming and "на точке" in lying and "700200" in lying
    assert "26R-922352" in coming and "15.10, до 18:00" in coming and "Nokian Hakkapeliitta 10" in coming and "Кол-во 4 шт" in coming
    assert "Татьяна" in coming and 'href="tel:+79001112233"' in coming
    assert "Включить уведомления" in page and "notify.js" in page
    assert here


def test_the_point_lands_on_my_point_and_notify_runs_on_its_pages_only(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    mine = manager(app, conn, pid)
    r = mine.get("/")
    assert r.status_code == 302 and r.headers["Location"].endswith("/my-point")
    assert "notify.js" in mine.get("/chats").get_data(as_text=True)
    assert "notify.js" not in client.get("/orders").get_data(as_text=True)                       # the owner: not
    assert "Включить уведомления" not in client.get("/my-point").get_data(as_text=True)


def test_the_feed_has_the_own_orders_notes_and_bell_only(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    other = points.save_point(conn, dict(name="Гамма", channel="vk", vk_peer_id="777"))
    theirs = _card(conn, other, webmim="700100")
    to_panel(conn, pid)
    points.add_note(conn, pid, "alert", "Готово", NOW)
    points.add_note(conn, other, "alert", "Чужое", NOW)
    points.add_note(conn, pid, "answer", "Ответ бота клиенту", NOW)                           # the bot's answers are not the point's (2026-10-30)
    mine = manager(app, conn, pid)
    data = mine.get("/my-point/feed?p=%d" % other).get_json()
    keys = [it["key"] for it in data["items"]]
    assert "order-%d" % card in keys and "order-%d" % theirs not in keys
    assert any(it["text"] == "Готово" for it in data["items"]) and not any(it["text"] == "Чужое" for it in data["items"])
    assert not any(it["text"] == "Ответ бота клиенту" for it in data["items"])
    page = mine.get("/my-point?notes=1").get_data(as_text=True)
    assert "Оповещения" in page and "Готово" in page and "Чужое" not in page and "Ответ бота клиенту" not in page


def test_a_point_without_vk_hands_a_client_over_from_its_page(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    conn.execute("UPDATE point SET vk_peer_id = '', channel = 'panel' WHERE id = ?", (pid,))
    conn.commit()
    mine = manager(app, conn, pid)
    page = mine.get("/my-point/handed").get_data(as_text=True)
    token = re.search(r'action="/my-point(?:/handed)?" class="mp-handoff-form".*?name="token" value="([0-9a-f]+)"', page, re.S).group(1)
    r = post(mine, "/my-point", "tok", token=token, phone="+7 900 222-33-44", need="зима")
    assert r.status_code == 302
    [(order_id,)] = conn.execute("SELECT order_id FROM order_handoff WHERE point_id = ?", (pid,)).fetchall()
    assert orders.get_card(conn, order_id)["stage"]["code"] == orders.FROM_POINT[0] and to_points(app) == []


# ---- the review, round 1 ----------------------------------------------------------------------------------------------------------------

def test_the_press_in_webmim_says_the_point_works_in_the_panel_without_a_red_error(panel):
    app, conn, fake, avito, box, client = panel
    mine = placed_ready(conn, chat=False)
    to_panel(conn, point_of(conn, mine))
    conn.execute("UPDATE order_card SET delivery_date = '2026-10-08' WHERE id = ?", (mine,))
    conn.commit()
    assert post(client, "/orders/%d/webmim/create" % mine, "tok").status_code == 302
    html = client.get("/orders/%d" % mine).get_data(as_text=True)
    found = re.search(r'<div class="flash ([a-z-]+)"[^>]*>(?:(?!<div class="flash ).)*?работает в панели', html, re.S)
    assert found and found.group(1) == "flash--info"


def test_a_waiting_message_of_the_rule_to_a_point_in_the_panel_is_skipped_by_the_hook(tmp_path):
    from test_order_messages import placed_rules
    conn = db(tmp_path)
    mine = with_vk(conn, placed_ready(conn))
    stage = placed_rules(conn)
    place(conn, mine, FakeWebmim())
    orders.move_card(conn, mine, stage, NOW, "owner")                                      # the rule prepares both messages first
    to_panel(conn, point_of(conn, mine))
    vk = FakeVk()
    order_placed.on_placed(conn, mine, "1001", "2026-10-23T12:01:00", "owner", Deliver(), vk)
    [task] = point_tasks(conn, mine)
    assert task["status"] == "skipped" and vk.sent == []                                   # nobody has to skip it by hand on every order
    assert [t["title"] for t in engine.list_tasks(conn, mine, ("pending",)) if t["kind"] == "message"] == []      # the client's went


def test_an_alert_that_cannot_be_kept_for_the_point_never_raises(tmp_path, monkeypatch):
    conn, box = env(tmp_path)
    pid = notify_point(conn, channel="panel")
    c = notify_card(conn, "ghost", pid)

    def busy(*args, **kwargs):
        raise __import__("sqlite3").OperationalError("database is locked")
    monkeypatch.setattr(points, "add_note", busy)
    tg = ScriptedHttp((200, {"ok": True, "result": {}}))
    status = order_notify.deliver(conn, box, c, "Заказ: клиент не пришёл", NOW, tg_http=tg, vk_http=ScriptedHttp())
    assert status.startswith("не отправлено") and len(tg.calls) == 1                       # the owner is told, as when nobody can be written to


def test_the_notes_are_shown_only_while_the_point_works_in_the_panel(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    points.add_note(conn, pid, "alert", "Готово", NOW)
    mine = manager(app, conn, pid)
    assert "Оповещения:" not in mine.get("/my-point").get_data(as_text=True)                # back on «ВК»: the old notes are not shown
    to_panel(conn, pid)
    assert "Оповещения: 1" in mine.get("/my-point").get_data(as_text=True)


def test_the_feed_names_whose_it_is_for_the_memory_of_the_browser(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    assert manager(app, conn, pid).get("/my-point/feed").get_json()["who"] == "gamma"
