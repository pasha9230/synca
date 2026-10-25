"""«Сообщения по заказу» (the owner 2026-10-23): the messages of an order to the client (Avito) and to the manager of the point (VK) in one
window of the card, each sent once by its own button or «Отправить оба»; after «Оформить в WebMIM» the point hears «Менеджеру: заказ
оформлен» by itself, as the client does. Fakes only: nothing leaves the tests."""
from __future__ import annotations

import json
import re
import urllib.parse

import pytest

from http_fakes import ScriptedHttp
from panel_helpers import post
from test_order_placed import Deliver, panel, ready  # noqa: F401  (panel: the fixture of the press on the page)
from test_roles import sign_in
from test_send_popup import _window
from test_webmim_create import NOW, PANEL_NOW, FakeWebmim, db, place
from tires_sync import connections, order_engine as engine, order_placed, order_rules as rules, order_templates as templates, orders, users, vk_send


class FakeVk:
    """vk_send.sender_for: records (peer, text, random_id); `fail` raises instead."""

    def __init__(self, fail=None):
        self.sent, self.fail = [], fail

    def __call__(self, peer_id, text, random_id=None):
        if self.fail is not None:
            raise self.fail
        self.sent.append((peer_id, text, random_id))
        return 1


def with_vk(conn, mine, vk="555"):
    conn.execute("UPDATE point SET vk_peer_id = ?, channel = 'vk' WHERE id = (SELECT point_id FROM order_card WHERE id = ?)", (vk, mine))
    conn.commit()
    return mine


def point_tasks(conn, mine):
    return [t for t in engine.list_tasks(conn, mine) if t["kind"] == "message" and t["title"] == order_placed.POINT_TEMPLATE]


def client_tasks(conn, mine):
    return [t for t in engine.list_tasks(conn, mine) if t["kind"] == "message" and t["title"] == order_placed.CLIENT_TEMPLATE]


def need_transfer(conn):
    for t in templates.list_templates(conn):
        if t["name"] in (order_placed.CLIENT_TEMPLATE, order_placed.POINT_TEMPLATE):
            templates.update_template(conn, t["id"], t["name"], t["audience"], "Заказ {webmim_number}, перемещение {transfer_number}")


def test_the_template_names_are_the_owners_samples():
    assert (order_placed.CLIENT_TEMPLATE, "client") == templates.SAMPLES[0][:2]
    assert (order_placed.POINT_TEMPLATE, "manager") == templates.SAMPLES[1][:2]


# ---- the hook after WebMIM ---------------------------------------------------------------------------------------------------------------

def test_a_full_success_tells_the_point_in_vk_once(tmp_path):
    conn = db(tmp_path)
    mine = with_vk(conn, ready(conn))
    place(conn, mine, FakeWebmim())
    deliver, vk = Deliver(), FakeVk()
    lines = order_placed.on_placed(conn, mine, "1001", NOW, "owner", deliver, vk)
    assert lines[0].startswith("Клиенту отправлено") and lines[1] == "Точке «Гамма-адрес» отправлено «Менеджеру: заказ оформлен» в ВК."
    [(peer, text, random_id)] = vk.sent
    [task] = point_tasks(conn, mine)
    assert peer == "555" and "Заказ 5001 оформлен на перемещение" in text and random_id == engine.vk_random_id(task)
    assert task["status"] == "done" and task["entered_at"] == "webmim-1001-point" and text == "(бот) " + task["body"]      # sent by itself: signed (2026-10-24)
    assert any(e["text"].startswith("Менеджеру: заказ оформлен: выполнено (отправлено точке в ВК") and "Заказ 5001" in e["text"]
               for e in orders.list_events(conn, mine))                                            # the text is in the journal
    again = order_placed.on_placed(conn, mine, "1001", "2026-10-23T12:10:00", "owner", deliver, vk)
    assert len(vk.sent) == 1 and len(deliver.sent) == 1 and "второе не отправляется" in again[1] and len(point_tasks(conn, mine)) == 1


@pytest.mark.parametrize("vk_id, send, why", [("", FakeVk(), "не привязан ВК"), ("555", None, "ВК не настроен"),
                                              ("555", FakeVk(fail=vk_send.VkError("ВК не принял сообщение (код 901): x")), "ВК не принял")])
def test_the_point_message_that_cannot_go_waits_with_a_red_record(tmp_path, vk_id, send, why):
    conn = db(tmp_path)
    mine = with_vk(conn, ready(conn), vk_id)
    place(conn, mine, FakeWebmim())
    line = order_placed.on_placed(conn, mine, "1001", NOW, "owner", Deliver(), send)[1]
    assert line.startswith("Точке не отправлено") and why in line and "ждёт в окне «Сообщения по заказу»" in line
    assert point_tasks(conn, mine)[0]["status"] == "pending" and not (send and send.sent)
    assert any(e["level"] == "red" and why in e["text"] for e in orders.list_events(conn, mine))


def test_a_failed_vk_send_can_be_sent_again_with_the_same_random_id(tmp_path):
    conn = db(tmp_path)
    mine = with_vk(conn, ready(conn))
    place(conn, mine, FakeWebmim())
    order_placed.on_placed(conn, mine, "1001", NOW, "owner", Deliver(), FakeVk(fail=vk_send.VkError("ВК недоступен: нет связи с сервером.")))
    [task] = point_tasks(conn, mine)
    vk = FakeVk()
    assert engine.send_point_task(conn, task["id"], task["body"], vk, "2026-10-23T12:20:00", "owner") == "Гамма-адрес"
    assert vk.sent[0][2] == engine.vk_random_id(task) and point_tasks(conn, mine)[0]["status"] == "done"
    with pytest.raises(orders.OrderError):                                                     # never twice
        engine.send_point_task(conn, task["id"], task["body"], vk, "2026-10-23T12:21:00", "owner")
    assert len(vk.sent) == 1


def test_both_wait_for_the_transfer_number_and_become_ready_when_it_is_typed(tmp_path):
    conn = db(tmp_path)
    mine = with_vk(conn, ready(conn))
    need_transfer(conn)
    place(conn, mine, FakeWebmim())
    deliver, vk = Deliver(), FakeVk()
    lines = order_placed.on_placed(conn, mine, "1001", NOW, "owner", deliver, vk)
    assert all("когда впишете номер перемещения" in line for line in lines) and not deliver.sent and not vk.sent
    assert [t["status"] for t in client_tasks(conn, mine) + point_tasks(conn, mine)] == ["error", "error"]
    orders.update_card(conn, mine, {"transfer_number": "26R-922352"}, NOW, "owner")
    engine.refresh_tasks(conn, mine, NOW)
    assert [t["body"] for t in client_tasks(conn, mine) + point_tasks(conn, mine)] == ["Заказ 5001, перемещение 26R-922352"] * 2


def placed_rules(conn):
    """The owner's rules of «Заказ оформлен» (live panel, 2026-10-23): prepare both messages, «спросить»."""
    stage = next(st["id"] for st in orders.list_stages(conn) if st["code"] == "placed")
    for t in templates.list_templates(conn):
        if t["name"] in (order_placed.CLIENT_TEMPLATE, order_placed.POINT_TEMPLATE):
            rules.add_rule(conn, stage, "enter", 0, "message", t["id"], "", "ask", None)
    conn.commit()
    return stage


def test_the_stage_moved_before_the_hook_sends_the_rule_messages_without_a_second(tmp_path):
    """Lane B moves the order to «Заказ оформлен» right after WebMIM, before the hook (webmim_create.place does it now; the move here is then
    «already there» and stays for the order of a press without it): the rule's messages are the ones sent."""
    conn = db(tmp_path)
    mine = with_vk(conn, ready(conn))
    stage = placed_rules(conn)
    place(conn, mine, FakeWebmim())
    orders.move_card(conn, mine, stage, NOW, "owner")
    assert [t["status"] for t in client_tasks(conn, mine) + point_tasks(conn, mine)] == ["pending", "pending"]
    deliver, vk = Deliver(), FakeVk()
    order_placed.on_placed(conn, mine, "1001", "2026-10-23T12:01:00", "owner", deliver, vk)
    assert len(deliver.sent) == 1 and len(vk.sent) == 1
    assert [(t["rule_id"] > 0, t["status"]) for t in client_tasks(conn, mine) + point_tasks(conn, mine)] == [(True, "done"), (True, "done")]


def test_the_stage_entered_after_the_hook_makes_its_point_message_skipped(tmp_path):
    """The hook first, the rule of «Заказ оформлен» after (lane B moves the order there before the hook: the rules here come later, and the
    order enters the stage again)."""
    conn = db(tmp_path)
    mine = with_vk(conn, ready(conn))
    place(conn, mine, FakeWebmim())
    vk = FakeVk()
    order_placed.on_placed(conn, mine, "1001", NOW, "owner", Deliver(), vk)
    stage = placed_rules(conn)
    talk = next(st["id"] for st in orders.list_stages(conn) if st["code"] == "talk")
    orders.move_card(conn, mine, talk, "2026-10-23T12:29:00", "owner")
    orders.move_card(conn, mine, stage, "2026-10-23T12:30:00", "owner")
    made = [t for t in point_tasks(conn, mine) if t["rule_id"] > 0]
    assert [t["status"] for t in made] == ["skipped"] and len(vk.sent) == 1
    assert engine.new_messages(conn, mine, "2026-10-23T12:30:00") == []                       # no window for them


def test_a_message_to_the_client_never_goes_to_vk(tmp_path):
    conn = db(tmp_path)
    mine = with_vk(conn, ready(conn, chat=False))
    place(conn, mine, FakeWebmim())
    order_placed.on_placed(conn, mine, "1001", NOW, "owner", Deliver(), None)
    [task] = client_tasks(conn, mine)
    vk = FakeVk()
    with pytest.raises(orders.OrderError, match="не менеджеру"):
        engine.send_point_task(conn, task["id"], task["body"], vk, NOW, "owner")
    assert vk.sent == [] and client_tasks(conn, mine)[0]["status"] == "pending"


# ---- the window on the page --------------------------------------------------------------------------------------------------------------

VK_OK = (200, {"response": 7})
AVITO_TEXTS = lambda avito: [c for c in avito.calls if c[0] == "POST" and c[1].endswith("/messages")]      # noqa: E731


def vk_calls(app):
    return [urllib.parse.parse_qs(c[3].decode("utf-8")) for c in app.config["VK_HTTP"].calls]


def keys(conn, box, mine, vk=True):
    account = conn.execute("SELECT account_id FROM avito_chat WHERE order_id = ?", (mine,)).fetchone()
    if account:
        connections.set_secret(conn, box, "avito", account[0], "client_id", "CLIENTID-12345678", PANEL_NOW, "owner")
        connections.set_secret(conn, box, "avito", account[0], "client_secret", "SECRETVALUE-XYZ", PANEL_NOW, "owner")
    if vk:
        connections.set_secret(conn, box, "vk", 0, "token", "vk-TOKEN-1", PANEL_NOW, "owner")
    conn.execute("UPDATE order_card SET delivery_date = '2026-10-08' WHERE id = ?", (mine,))
    conn.commit()


def placed_waiting(panel, vk_replies=(VK_OK,), vk=True, vk_id="555"):
    """Placed in WebMIM by the button; both messages wait for the transfer number."""
    app, conn, fake, avito, box, client = panel
    app.config["VK_HTTP"] = ScriptedHttp(*vk_replies)
    mine = with_vk(conn, ready(conn), vk_id)
    need_transfer(conn)
    keys(conn, box, mine, vk)
    assert post(client, "/orders/%d/webmim/create" % mine, "tok").status_code == 302
    return mine


def page(client, mine, **args):
    return client.get("/orders/%d" % mine, query_string=args).get_data(as_text=True)


def send(client, mine, task, **fields):
    data = {"csrf": "tok", "task": task}
    data.update(fields)
    return client.post("/orders/%d/messages/send" % mine, data=data, headers={"X-Requested-With": "fetch"})


def form_of(conn, mine, text_edit=""):
    """What the window sends for every ready message: its text (the client's corrected by `text_edit`), its chat or point."""
    fields = {"ready": []}
    for t in engine.list_tasks(conn, mine, ("pending",)):
        if t["kind"] != "message":
            continue
        fields["ready"].append(str(t["id"]))
        fields["whom_%d" % t["id"]] = engine.audience(conn, t)
        fields["text_%d" % t["id"]] = t["body"] + (text_edit if engine.audience(conn, t) == "client" else "")
        if engine.audience(conn, t) == "client":
            fields["chat_%d" % t["id"]] = str(engine.send_target(conn, t)[0]["id"])
        else:
            fields["point_%d" % t["id"]] = str(engine.point_target(conn, t)[0]["id"])
    return fields


def test_the_press_tells_the_client_and_the_point_and_opens_no_window(panel):
    app, conn, fake, avito, box, client = panel
    app.config["VK_HTTP"] = ScriptedHttp(VK_OK)
    mine = with_vk(conn, ready(conn))
    keys(conn, box, mine)
    post(client, "/orders/%d/webmim/create" % mine, "tok")
    html = page(client, mine)
    assert "Клиенту отправлено «Клиенту: заказ оформлен»" in html and "Точке «Гамма-адрес» отправлено «Менеджеру: заказ оформлен» в ВК." in html
    [call] = vk_calls(app)
    assert call["peer_id"] == ["555"] and call["message"][0].startswith("(бот) Заказ 5001 оформлен") and call["access_token"] == ["vk-TOKEN-1"]
    assert len(AVITO_TEXTS(avito)) == 1 and 'id="order-messages"' not in html                    # nothing waits: no window
    post(client, "/orders/%d/webmim/create" % mine, "tok")                                      # a second press: nothing more
    assert len(vk_calls(app)) == 1 and len(AVITO_TEXTS(avito)) == 1


def test_lacking_the_transfer_number_the_window_says_so_and_opens_when_it_is_typed(panel):
    app, conn, fake, avito, box, client = panel
    mine = placed_waiting(panel)
    html = page(client, mine)
    state, win = _window(html)
    assert "is-open" not in state and win.count("Не хватает данных. Впишите номер перемещения") == 2       # nothing to send yet: closed
    assert win.count("data-send-one disabled") == 2 and "Отправить оба" not in win and "Не хватает данных: см. в окне." in html
    assert vk_calls(app) == [] and AVITO_TEXTS(avito) == []
    post(client, "/orders/%d/save" % mine, "tok", transfer_number="26R-922352")
    state, win = _window(page(client, mine))
    assert "is-open" in state and "Отправить оба" in win and "Отправить клиенту в Авито" in win and "Отправить точке в ВК" in win
    assert "Клиенту · чат Ч-" in win and "Точке · точка «Гамма-адрес» в ВК · Менеджеру: заказ оформлен" in win
    assert win.count("Заказ 5001, перемещение 26R-922352") == 2 and "data-send-one disabled" not in win
    assert vk_calls(app) == [] and AVITO_TEXTS(avito) == []                                     # only a press sends
    state, _ = _window(page(client, mine))
    assert "is-open" not in state                                                               # «Позже»: once; the button opens it again
    assert 'data-open-modal="order-messages"' in page(client, mine) and "is-open" in _window(page(client, mine, messages=1))[0]


def test_send_both_sends_each_once_with_the_corrected_text(panel):
    app, conn, fake, avito, box, client = panel
    mine = placed_waiting(panel)
    post(client, "/orders/%d/save" % mine, "tok", transfer_number="26R-922352")
    fields = form_of(conn, mine, text_edit="\nСпасибо!")
    res = send(client, mine, "ready", **fields).get_json()
    assert res["ok"] and [r["sent"] for r in res["results"]] == [True, True]
    assert len(AVITO_TEXTS(avito)) == 1 and len(vk_calls(app)) == 1
    assert json.loads(AVITO_TEXTS(avito)[0][3].decode("utf-8"))["message"]["text"].endswith("Спасибо!")
    assert vk_calls(app)[0]["message"] == ["Заказ 5001, перемещение 26R-922352"]                 # a person's press: never signed «(бот)»
    assert all(t["status"] == "done" for t in client_tasks(conn, mine) + point_tasks(conn, mine))
    journal = " | ".join(e["text"] for e in orders.list_events(conn, mine))
    assert "отправлено клиенту, текст изменён):\nЗаказ 5001, перемещение 26R-922352\nСпасибо!" in journal
    assert "отправлено точке в ВК (точка «Гамма-адрес»)):\nЗаказ 5001, перемещение 26R-922352" in journal
    again = send(client, mine, "ready", **fields).get_json()                                    # a second window, a double press
    assert again["ok"] and [r["sent"] for r in again["results"]] == [False, False] and "уже отправлено" in again["results"][0]["message"]
    assert len(AVITO_TEXTS(avito)) == 1 and len(vk_calls(app)) == 1


def test_send_both_after_one_went_sends_only_the_other(panel):
    app, conn, fake, avito, box, client = panel
    mine = placed_waiting(panel)
    post(client, "/orders/%d/save" % mine, "tok", transfer_number="26R-922352")
    fields = form_of(conn, mine)
    [point_task] = point_tasks(conn, mine)
    one = send(client, mine, str(point_task["id"]), **fields).get_json()
    assert one["ok"] and one["results"][0]["message"] == "Точке «Гамма-адрес» отправлено «Менеджеру: заказ оформлен» в ВК."
    both = send(client, mine, "ready", **fields).get_json()
    assert both["ok"] and [r["sent"] for r in both["results"]] == [True, False]
    assert len(vk_calls(app)) == 1 and len(AVITO_TEXTS(avito)) == 1


@pytest.mark.parametrize("vk_id, vk, reply, why", [
    ("", True, VK_OK, "не привязан ВК"),
    ("555", False, VK_OK, "ВК не настроен: нет ключа сообщества"),
    ("555", True, (200, {"error": {"error_code": 901, "error_msg": "Can't send messages for users without permission"}}), "ВК не принял сообщение (код 901)"),
])
def test_a_refused_vk_send_says_why_and_the_message_waits(panel, vk_id, vk, reply, why):
    app, conn, fake, avito, box, client = panel
    mine = placed_waiting(panel, vk_replies=(reply,), vk=vk, vk_id=vk_id)
    post(client, "/orders/%d/save" % mine, "tok", transfer_number="26R-922352")
    [task] = point_tasks(conn, mine)
    res = send(client, mine, str(task["id"]), **{"text_%d" % task["id"]: task["body"], "whom_%d" % task["id"]: "manager"}).get_json()
    assert not res["ok"] and why in res["results"][0]["message"] and point_tasks(conn, mine)[0]["status"] == "pending"
    if vk_id:
        _, win = _window(page(client, mine, messages=1))
        assert (why in win) == (not vk)                                                         # no key: said in the window before a press


def test_a_cut_off_vk_answer_is_repeated_with_the_same_random_id(panel):
    app, conn, fake, avito, box, client = panel
    mine = placed_waiting(panel, vk_replies=((500, {}), VK_OK))
    post(client, "/orders/%d/save" % mine, "tok", transfer_number="26R-922352")
    [task] = point_tasks(conn, mine)
    fields = {"text_%d" % task["id"]: task["body"], "point_%d" % task["id"]: str(engine.point_target(conn, task)[0]["id"]), "whom_%d" % task["id"]: "manager"}
    assert not send(client, mine, str(task["id"]), **fields).get_json()["ok"]
    assert send(client, mine, str(task["id"]), **fields).get_json()["ok"]
    first, second = vk_calls(app)
    assert first["random_id"] == second["random_id"] == [str(engine.vk_random_id(task))]


def test_the_window_named_another_point_sends_nothing(panel):
    app, conn, fake, avito, box, client = panel
    mine = placed_waiting(panel)
    post(client, "/orders/%d/save" % mine, "tok", transfer_number="26R-922352")
    [task] = point_tasks(conn, mine)
    res = send(client, mine, str(task["id"]), **{"text_%d" % task["id"]: task["body"], "point_%d" % task["id"]: "999", "whom_%d" % task["id"]: "manager"}).get_json()
    assert not res["ok"] and "Точка заказа изменилась" in res["results"][0]["message"] and vk_calls(app) == []


def test_an_empty_text_or_a_task_of_another_order_sends_nothing(panel):
    app, conn, fake, avito, box, client = panel
    mine = placed_waiting(panel)
    post(client, "/orders/%d/save" % mine, "tok", transfer_number="26R-922352")
    [task] = point_tasks(conn, mine)
    assert "от 1 до 4000 знаков" in send(client, mine, str(task["id"]), **{"text_%d" % task["id"]: "  ", "whom_%d" % task["id"]: "manager"}).get_json()["results"][0]["message"]
    other = orders.create_card(conn, {"client_name": "Пётр", "client_phone": "+7 900 000 00 01"}, NOW, "owner")
    res = client.post("/orders/%d/messages/send" % other, data={"csrf": "tok", "task": str(task["id"]), "text_%d" % task["id"]: "x"},
                      headers={"X-Requested-With": "fetch"}).get_json()
    assert not res["ok"] and "нет" in res["results"][0]["message"] and vk_calls(app) == []


def test_the_plain_form_flashes_and_comes_back(panel):
    app, conn, fake, avito, box, client = panel
    mine = placed_waiting(panel)
    post(client, "/orders/%d/save" % mine, "tok", transfer_number="26R-922352")
    data = dict(form_of(conn, mine), csrf="tok", task="ready")
    r = client.post("/orders/%d/messages/send" % mine, data=data)
    assert r.status_code == 302 and r.headers["Location"].endswith("/orders/%d" % mine)
    assert "Точке «Гамма-адрес» отправлено" in page(client, mine)


def test_the_manual_mark_stays_for_the_point_message(panel):
    app, conn, fake, avito, box, client = panel
    mine = placed_waiting(panel, vk_id="")
    post(client, "/orders/%d/save" % mine, "tok", transfer_number="26R-922352")
    [task] = point_tasks(conn, mine)
    _, win = _window(page(client, mine, messages=1))
    assert 'formaction="/orders/%d/task/%d/run"' % (mine, task["id"]) in win and 'value="%d" data-send-one disabled' % task["id"] in win
    post(client, "/orders/%d/task/%d/run" % (mine, task["id"]), "tok")
    assert point_tasks(conn, mine)[0]["status"] == "done" and point_tasks(conn, mine)[0]["result"] == "отправлено вручную" and vk_calls(app) == []


def test_a_point_sees_no_messages_and_may_not_send(panel):
    app, conn, fake, avito, box, client = panel
    mine = placed_waiting(panel)
    post(client, "/orders/%d/save" % mine, "tok", transfer_number="26R-922352")
    point_id = orders.get_card(conn, mine)["point_id"]
    users.add_user(conn, "point1", "длинный-пароль-1", NOW, role="point", point_id=point_id)
    them = app.test_client()
    sign_in(them, conn, "point1")
    html = them.get("/orders/%d" % mine).get_data(as_text=True)
    assert "Сообщения по заказу" not in html and "order-messages" not in html and "перемещение 26R-922352" not in html
    assert send(them, mine, "ready", **form_of(conn, mine)).status_code == 403 and vk_calls(app) == []


def test_a_viewer_may_not_send(panel):
    app, conn, fake, avito, box, client = panel
    mine = placed_waiting(panel)
    post(client, "/orders/%d/save" % mine, "tok", transfer_number="26R-922352")
    users.add_user(conn, "viewer1", "длинный-пароль-1", NOW, role="viewer")
    them = app.test_client()
    sign_in(them, conn, "viewer1")
    _, win = _window(them.get("/orders/%d?messages=1" % mine).get_data(as_text=True))
    assert "data-send-one" not in win and "Отправить оба" not in win
    assert send(them, mine, "ready", **form_of(conn, mine)).status_code == 403 and vk_calls(app) == []


# ---- the board and «Чаты»: a link to the window ----------------------------------------------------------------------------------------

def test_the_chats_page_links_to_the_window_for_a_message_to_the_point(tmp_path):
    from test_send_popup import SENT, _ready, _stage
    app, client, tok, conn, tanya, card_id = _ready(tmp_path, SENT, audience="manager")
    client.post("/chats/%d/stage" % tanya["id"], data={"csrf": tok, "stage_id": _stage(conn, "talk")})
    html = client.get("/chats/%d" % tanya["id"]).get_data(as_text=True)
    assert re.search(r'id="order-messages-%d".*?href="/orders/%d\?messages=1#messages"[^>]*>Открыть сообщения заказа' % (card_id, card_id), html, re.S)
    assert "Открыть сообщения заказа" not in client.get("/chats/%d" % tanya["id"]).get_data(as_text=True)      # once


def test_the_chats_window_for_the_client_links_to_the_other_messages(tmp_path):
    from test_send_popup import SENT, _ready, _rule, _stage
    app, client, tok, conn, tanya, card_id = _ready(tmp_path, SENT)
    _rule(conn, "manager", code="talk")
    client.post("/chats/%d/stage" % tanya["id"], data={"csrf": tok, "stage_id": _stage(conn, "talk")})
    state, win = _window(client.get("/chats/%d" % tanya["id"]).get_data(as_text=True))
    assert "is-open" in state and "Отправить клиенту?" in win and 'href="/orders/%d?messages=1#messages"' % card_id in win


def test_a_point_moving_a_card_on_the_board_gets_no_window(tmp_path, monkeypatch):
    from test_send_popup import SENT, _board_move, _ready, _stage
    from tires_sync import order_points as points
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")
    app, client, tok, conn, tanya, card_id = _ready(tmp_path, SENT, audience="manager")
    point_id = points.save_point(conn, {"name": "Гамма", "manager_name": "Алексей", "channel": "vk"})
    conn.execute("UPDATE order_card SET point_id = ? WHERE id = ?", (point_id, card_id))
    conn.commit()
    users.add_user(conn, "point1", "длинный-пароль-1", NOW, role="point", point_id=point_id)
    them = app.test_client()
    sign_in(them, conn, "point1")
    res = _board_move(them, "tok", card_id, _stage(conn, "talk"))
    assert res["ok"] and res["send"] is None


# ---- review round 1 ----------------------------------------------------------------------------------------------------------------------

def test_a_template_turned_to_the_client_meanwhile_sends_nothing_to_the_client(panel):
    """Д1: the window showed «Точке»; the template's audience became «клиенту» before the press: nothing goes anywhere."""
    app, conn, fake, avito, box, client = panel
    mine = placed_waiting(panel)
    post(client, "/orders/%d/save" % mine, "tok", transfer_number="26R-922352")
    fields = form_of(conn, mine)
    [task] = point_tasks(conn, mine)
    template = templates.get_template(conn, int(task["arg"]))
    templates.update_template(conn, template["id"], template["name"], "client", template["body"])
    res = send(client, mine, "ready", **fields).get_json()
    by_task = {r["task"]: r for r in res["results"]}
    assert not by_task[task["id"]]["ok"] and "адресат сообщения изменился" in by_task[task["id"]]["message"]
    assert len(AVITO_TEXTS(avito)) == 1 and vk_calls(app) == []                                # only the client's own message went
    assert point_tasks(conn, mine)[0]["status"] == "pending"


def test_a_message_without_the_addressee_of_the_window_sends_nothing(panel):
    app, conn, fake, avito, box, client = panel
    mine = placed_waiting(panel)
    post(client, "/orders/%d/save" % mine, "tok", transfer_number="26R-922352")
    [task] = point_tasks(conn, mine)
    res = send(client, mine, str(task["id"]), **{"text_%d" % task["id"]: task["body"]}).get_json()
    assert not res["ok"] and "адресат сообщения изменился" in res["results"][0]["message"] and vk_calls(app) == []


def test_a_cut_off_vk_answer_says_it_may_have_gone(panel):
    """Д2: no answer from VK is not «не ушло»: the repeat must be the same press, the same text."""
    app, conn, fake, avito, box, client = panel
    mine = placed_waiting(panel, vk_replies=())
    app.config["VK_HTTP"] = lambda *a: (_ for _ in ()).throw(OSError("timeout"))
    post(client, "/orders/%d/save" % mine, "tok", transfer_number="26R-922352")
    [task] = point_tasks(conn, mine)
    fields = {"text_%d" % task["id"]: task["body"], "point_%d" % task["id"]: str(engine.point_target(conn, task)[0]["id"]), "whom_%d" % task["id"]: "manager"}
    message = send(client, mine, str(task["id"]), **fields).get_json()["results"][0]["message"]
    assert "Неизвестно, ушло ли" in message and "текст не меняйте" in message and point_tasks(conn, mine)[0]["status"] == "pending"


def test_a_text_too_long_for_vk_is_not_cut_but_waits(tmp_path):
    """Д3: VK takes 4000 signs; a longer text is never cut silently, by the hook either."""
    conn = db(tmp_path)
    mine = with_vk(conn, ready(conn))
    conn.execute("UPDATE order_template SET body = ? WHERE name = ?", ("Заказ {webmim_number} " + "я" * 4000, order_placed.POINT_TEMPLATE))
    conn.commit()                                                  # a template is up to 2000 signs; the items it names can make it longer
    place(conn, mine, FakeWebmim())
    vk = FakeVk()
    line = order_placed.on_placed(conn, mine, "1001", NOW, "owner", Deliver(), vk)[1]
    assert "не больше 4000 знаков" in line and vk.sent == [] and point_tasks(conn, mine)[0]["status"] == "pending"


def test_the_number_saved_with_a_refused_position_still_opens_the_window(panel):
    """Д4: «Сохранить» with a row that is not added: the fields (the number) are saved, the messages are ready, the window opens."""
    app, conn, fake, avito, box, client = panel
    mine = placed_waiting(panel)
    html = client.post("/orders/%d/save" % mine, data={"csrf": "tok", "transfer_number": "26R-922352", "query": "нет такой шины", "qty": "4"}).get_data(as_text=True)
    assert "Позиция не добавлена" in html
    assert "is-open" in _window(html)[0] or "is-open" in _window(page(client, mine))[0]


def test_the_window_does_not_open_by_itself_for_who_may_not_send(panel):
    app, conn, fake, avito, box, client = panel
    mine = placed_waiting(panel)
    users.add_user(conn, "viewer1", "длинный-пароль-1", NOW, role="viewer")
    post(client, "/orders/%d/save" % mine, "tok", transfer_number="26R-922352")
    them = app.test_client()
    sign_in(them, conn, "viewer1")
    with them.session_transaction() as session:
        session["send_offer"] = {"card": mine, "task": None, "more": True}
    state, win = _window(them.get("/orders/%d" % mine).get_data(as_text=True))
    assert "is-open" not in state and "Заказ 5001, перемещение 26R-922352" in win


def test_a_second_press_while_vk_is_sending_sends_nothing(tmp_path):
    """Two windows at once: the second press comes while the first is still talking to VK. The task is taken first: one message."""
    conn = db(tmp_path)
    mine = with_vk(conn, ready(conn))
    place(conn, mine, FakeWebmim())
    order_placed.on_placed(conn, mine, "1001", NOW, "owner", Deliver(), None)                 # no key: the point's message waits
    [task] = point_tasks(conn, mine)
    sent, inner = [], []

    def send(peer_id, text, random_id=None):
        try:
            engine.send_point_task(conn, task["id"], text, lambda *a, **k: sent.append(a), NOW, "owner")
        except orders.OrderError as err:
            inner.append(str(err))
        sent.append((peer_id, text))
    engine.send_point_task(conn, task["id"], task["body"], send, NOW, "owner")
    assert len(sent) == 1 and inner and "уже" in inner[0] and point_tasks(conn, mine)[0]["status"] == "done"


# ---- the journal as a point sees it (the lead 2026-10-23: the client's correspondence, «Права точек») --------------------------------------

def test_a_point_sees_that_the_client_was_written_to_but_not_the_text(panel):
    app, conn, fake, avito, box, client = panel
    mine = placed_waiting(panel)
    for t in templates.list_templates(conn):
        if t["name"] == order_placed.CLIENT_TEMPLATE:
            templates.update_template(conn, t["id"], t["name"], "client", "Клиенту-секрет {webmim_number}, перемещение {transfer_number}")
    post(client, "/orders/%d/save" % mine, "tok", transfer_number="26R-922352")
    assert send(client, mine, "ready", **form_of(conn, mine)).get_json()["ok"]
    owner = page(client, mine)
    assert "Клиенту-секрет 5001" in owner and "Заказ 5001, перемещение 26R-922352" in owner              # the owner: everything as before
    point_id = orders.get_card(conn, mine)["point_id"]
    users.add_user(conn, "point1", "длинный-пароль-1", NOW, role="point", point_id=point_id)
    them = app.test_client()
    sign_in(them, conn, "point1")
    alerts = them.get("/alerts").get_data(as_text=True)                  # «Уведомления» before the card (opening it puts its green records out)
    assert "Клиенту-секрет" not in alerts and "Отправлен ответ клиенту" in alerts                # the chat's green record, named only
    feed = json.dumps(them.get("/alerts/feed").get_json(), ensure_ascii=False)                  # the bell: the same rows
    assert "Клиенту-секрет" not in feed and "Отправлен ответ клиенту" in feed
    html = them.get("/orders/%d" % mine).get_data(as_text=True)
    assert "Клиенту-секрет" not in html                                                         # neither the task's record nor the chat's
    assert "Клиенту отправлено сообщение «Клиенту: заказ оформлен»" in html and "Отправлен ответ клиенту" in html
    assert "Заказ 5001, перемещение 26R-922352" in html                                          # the message to the point itself: its own


def test_the_records_of_the_journal_as_a_point_sees_them():
    seen = lambda text, kind="rule": engine.for_point({"kind": kind, "text": text, "level": ""})["text"]      # noqa: E731
    assert seen("Клиенту: заказ оформлен: выполнено (отправлено клиенту, текст изменён):\nСекрет") == "Клиенту отправлено сообщение «Клиенту: заказ оформлен»"
    assert seen("Сообщение клиенту после оформления в WebMIM («Клиенту: заказ оформлен»):\nСекрет") == \
        "Сообщение клиенту после оформления в WebMIM («Клиенту: заказ оформлен»)"               # the hook's record before this lane
    assert seen("Отправлен ответ клиенту: Секрет…", kind="chat") == "Отправлен ответ клиенту"
    point = "Менеджеру: заказ оформлен: выполнено (отправлено точке в ВК (точка «Гамма»)):\nЗаказ 5001"
    assert seen(point) == point and seen("Заказ перенесён", kind="stage") == "Заказ перенесён"
    earlier = "Клиенту: заказ оформлен: выполнено (отправлено клиенту раньше (текст — в чате))"
    assert seen(earlier) == earlier                                                            # no text in it: as it is
    note = "Клиенту: заказ оформлен: выполнено (отправлено клиенту):\nсвоя заметка"
    assert seen(note, kind="note") == note                                                     # a person's note is never cut


@pytest.mark.parametrize("own", [False, True])
def test_a_point_reads_the_clients_texts_only_in_an_order_of_its_own_account(panel, own):
    """The owner 2026-10-22 («Права точек»): in an order not from the point's own Avito account the point does not read the client's
    correspondence — neither the client's words nor what was written to the client (named only); in an order of its own account it reads
    them as before. The card and «Уведомления» alike; the message to the point itself is always whole."""
    from tires_sync import chats
    app, conn, fake, avito, box, client = panel
    mine = placed_waiting(panel)
    for t in templates.list_templates(conn):
        if t["name"] == order_placed.CLIENT_TEMPLATE:
            templates.update_template(conn, t["id"], t["name"], "client", "Клиенту-секрет {webmim_number}, перемещение {transfer_number}")
    post(client, "/orders/%d/save" % mine, "tok", transfer_number="26R-922352")
    assert send(client, mine, "ready", **form_of(conn, mine)).get_json()["ok"]
    orders.add_event(conn, mine, NOW, chats.CLIENT_WROTE_AUTHOR, "chat", "Клиент написал: входящий-секрет", level="yellow")
    point_id = orders.get_card(conn, mine)["point_id"]
    if own:                                                        # the order's chat is of the point's own account
        conn.execute("UPDATE point SET avito_account_id = (SELECT account_id FROM avito_chat WHERE order_id = ?) WHERE id = ?", (mine, point_id))
    conn.commit()
    users.add_user(conn, "point1", "длинный-пароль-1", NOW, role="point", point_id=point_id)
    them = app.test_client()
    sign_in(them, conn, "point1")
    alerts = them.get("/alerts").get_data(as_text=True) + json.dumps(them.get("/alerts/feed").get_json(), ensure_ascii=False)   # before the card
    card = them.get("/orders/%d" % mine).get_data(as_text=True)
    assert ("входящий-секрет" in alerts) == own and ("входящий-секрет" in card) == own
    assert ("Клиенту-секрет" in card) == own and ("Клиенту отправлено сообщение «Клиенту: заказ оформлен»" in card) == (not own)
    assert "Заказ 5001, перемещение 26R-922352" in card                                          # the message to the point: always its own
    owner = page(client, mine)
    assert "входящий-секрет" in owner and "Клиенту-секрет" in owner                              # the owner: everything


def test_the_client_records_a_point_sees_named():
    seen = lambda text: engine.for_point({"kind": "chat", "text": text, "level": "yellow"})["text"]      # noqa: E731
    assert seen("Клиент написал: Секрет…") == "Клиент написал сообщение"
    closed = "Клиент написал после закрытия заказа (2026-10-23 12:00)"
    assert seen(closed) == closed and seen("Клиент звонил: пропущенный звонок") == "Клиент звонил: пропущенный звонок"


def test_send_all_takes_only_the_messages_of_this_stay_on_the_stage(panel):
    """Хвост З2 (the lead 2026-10-25): a message left from an earlier stage keeps its own button, «Отправить оба» is not offered for it."""
    app, conn, fake, avito, box, client = panel
    mine = placed_waiting(panel)
    [old] = point_tasks(conn, mine)
    conn.execute("UPDATE order_task SET created_at = '2026-01-05T10:00:00' WHERE id = ?", (old["id"],))     # made before the order entered its stage
    conn.commit()
    post(client, "/orders/%d/save" % mine, "tok", transfer_number="26R-922352")
    _state, win = _window(page(client, mine, messages=1))
    assert "Отправить оба" not in win and "Отправить все" not in win and 'name="ready" value="%d"' % old["id"] not in win
    assert "Отправить клиенту в Авито" in win and "Отправить точке в ВК" in win and "data-send-one disabled" not in win


def test_a_rule_prepares_no_vk_message_for_a_point_in_the_panel(tmp_path):
    """Хвост З3: a message to the manager of a point that works in the panel is skipped at once (grey), it never waits for a person."""
    from tires_sync import order_points as points
    conn = db(tmp_path)
    mine = ready(conn)
    conn.execute("UPDATE point SET channel = ? WHERE id = (SELECT point_id FROM order_card WHERE id = ?)", (points.PANEL, mine))
    moving = next(st["id"] for st in orders.list_stages(conn) if st["code"] == "moving")
    manager = next(t for t in templates.list_templates(conn) if t["name"] == order_placed.POINT_TEMPLATE)
    client = next(t for t in templates.list_templates(conn) if t["name"] == order_placed.CLIENT_TEMPLATE)
    for template in (manager, client):
        rules.add_rule(conn, moving, "enter", 0, "message", template["id"], "", "ask", None)
    conn.commit()
    orders.move_card(conn, mine, moving, "2026-10-23T12:30:00", "owner")
    made = {t["title"]: t for t in engine.list_tasks(conn, mine) if t["kind"] == "message"}
    assert made[order_placed.POINT_TEMPLATE]["status"] == "skipped" and made[order_placed.POINT_TEMPLATE]["result"] == engine.PANEL_SKIP
    assert made[order_placed.CLIENT_TEMPLATE]["status"] in ("pending", "error")                 # the client's message is as before
    record = next(e for e in orders.list_events(conn, mine) if e["text"].startswith("Задача пропущена"))
    assert engine.PANEL_SKIP in record["text"] and record["level"] == "grey"
    conn.execute("UPDATE point SET channel = 'vk' WHERE id = (SELECT point_id FROM order_card WHERE id = ?)", (mine,))
    from test_webmim_create import card
    other = card(conn, point_id=conn.execute("SELECT point_id FROM order_card WHERE id = ?", (mine,)).fetchone()[0])
    orders.move_card(conn, other, moving, "2026-10-23T12:40:00", "owner")
    assert [t["status"] for t in engine.list_tasks(conn, other) if t["title"] == order_placed.POINT_TEMPLATE] != ["skipped"]   # a VK point: as before


def test_back_in_placed_the_message_to_the_point_is_prepared_again(tmp_path):
    """Review 1 of хвост З5: the point may be another one after the order left «Заказ оформлен»: its message is not skipped as «sent»."""
    conn = db(tmp_path)
    mine = with_vk(conn, ready(conn))
    placed_stage = next(st["id"] for st in orders.list_stages(conn) if st["code"] == "placed")
    manager = next(t for t in templates.list_templates(conn) if t["name"] == order_placed.POINT_TEMPLATE)
    rules.add_rule(conn, placed_stage, "enter", 0, "message", manager["id"], "", "ask", None)
    conn.commit()
    orders.update_card(conn, mine, {"webmim_number": "5000300", "transfer_number": "26R-1"}, NOW, "owner")
    orders.move_card(conn, mine, placed_stage, "2026-10-23T12:30:00", "owner")
    [first] = point_tasks(conn, mine)
    engine.send_point_task(conn, first["id"], first["body"], FakeVk(), "2026-10-23T12:31:00", "owner")
    moving = next(st["id"] for st in orders.list_stages(conn) if st["code"] == "moving")
    orders.move_card(conn, mine, moving, "2026-10-23T12:40:00", "owner")
    orders.move_card(conn, mine, placed_stage, "2026-10-23T12:50:00", "owner")
    again = [t for t in point_tasks(conn, mine) if t["id"] != first["id"]]
    assert len(again) == 1 and again[0]["status"] == "pending"


# ---- «Отправить все» in parts (match3, lane misc-tails): a press never runs into the 120 s of gunicorn and nginx -------------------------

def _ready_client_first(conn, mine):
    fields = form_of(conn, mine)
    [c], [p] = client_tasks(conn, mine), point_tasks(conn, mine)
    fields["ready"] = [str(c["id"]), str(p["id"])]
    return fields, c, p


class _Clock:
    """time.monotonic of the route: each call moves on by `step` seconds (a slow Avito)."""

    def __init__(self, step):
        self.now, self.step = 0.0, step

    def monotonic(self):
        self.now += self.step
        return self.now


def test_send_all_past_its_time_leaves_the_rest_untouched_for_the_next_press(panel):
    app, conn, fake, avito, box, client = panel
    mine = placed_waiting(panel)
    post(client, "/orders/%d/save" % mine, "tok", transfer_number="26R-922352")
    fields, c, p = _ready_client_first(conn, mine)
    app.config["SEND_ALL_SECONDS"] = -1                                                       # the time is out after the first one
    res = send(client, mine, "ready", **fields).get_json()
    assert not res["ok"] and [r["task"] for r in res["results"]] == [c["id"], p["id"]]
    assert res["results"][0]["sent"] is True and res["results"][1]["sent"] is False
    assert res["results"][1]["message"].startswith("«Менеджеру: заказ оформлен» ещё не отправлено") and "ещё раз" in res["results"][1]["message"]
    assert len(AVITO_TEXTS(avito)) == 1 and vk_calls(app) == []                               # the second one: not sent, not marked, not taken
    assert point_tasks(conn, mine)[0]["status"] == "pending" and not point_tasks(conn, mine)[0]["done_at"]
    fields["ready"] = [str(p["id"])]                                                          # the window drops the one that went (app.js)
    again = send(client, mine, "ready", **fields).get_json()
    assert again["ok"] and again["results"][0]["sent"] is True and len(vk_calls(app)) == 1 and len(AVITO_TEXTS(avito)) == 1


def test_send_all_after_a_slow_send_does_not_start_the_next_one(panel, monkeypatch):
    from tires_sync.panel import routes_orders
    app, conn, fake, avito, box, client = panel
    mine = placed_waiting(panel)
    post(client, "/orders/%d/save" % mine, "tok", transfer_number="26R-922352")
    fields, c, p = _ready_client_first(conn, mine)
    monkeypatch.setattr(routes_orders, "time", _Clock(routes_orders.SEND_ALL_SECONDS + 1))
    res = send(client, mine, "ready", **fields).get_json()
    assert [r.get("sent") for r in res["results"]] == [True, False] and vk_calls(app) == []
    monkeypatch.setattr(routes_orders, "time", _Clock(1))                                     # quick sends: both in one press, as before
    res = send(client, mine, "ready", **fields).get_json()
    assert res["ok"] and [r["sent"] for r in res["results"]] == [False, True] and len(AVITO_TEXTS(avito)) == 1 and len(vk_calls(app)) == 1


def test_send_all_keeps_an_unknown_send_as_it_was_and_the_rest_wait(panel):
    app, conn, fake, avito, box, client = panel
    mine = placed_waiting(panel)
    post(client, "/orders/%d/save" % mine, "tok", transfer_number="26R-922352")
    fields, c, p = _ready_client_first(conn, mine)
    replies = [(200, {"access_token": "t", "expires_in": 3600}), (200, {"id": 42})]

    def cut(method, url, headers, body):
        if url.endswith("/messages"):
            raise OSError("timed out")
        return ScriptedHttp(replies.pop(0))(method, url, headers, body)
    app.config["AVITO_HTTP"], app.config["SEND_ALL_SECONDS"] = cut, -1
    res = send(client, mine, "ready", **fields).get_json()
    first, second = res["results"]
    assert not first["ok"] and "Неизвестно, ушло ли сообщение" in first["message"] and "До итога не отправляйте его ещё раз" in first["message"]
    assert conn.execute("SELECT status FROM chat_send_log WHERE token LIKE ?", ("task-%d-%%" % c["id"],)).fetchone()[0] == "unknown"
    assert not second["ok"] and "ещё не отправлено" in second["message"] and vk_calls(app) == [] and point_tasks(conn, mine)[0]["status"] == "pending"
    app.config["SEND_ALL_SECONDS"] = 15
    res = send(client, mine, "ready", **fields).get_json()                                    # the unknown one is never sent again
    assert "не подтверждена" in res["results"][0]["message"] and res["results"][1]["sent"] is True and len(vk_calls(app)) == 1
