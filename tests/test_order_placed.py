"""After «Оформить в WebMIM» fully succeeds, the client hears «Клиенту: заказ оформлен» by itself (order_placed.on_placed): once per order,
the way a person's «Отправить» goes; what does not go waits in the order's tasks with a warning. Fakes only: nothing leaves the tests."""
from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet

from http_fakes import ScriptedHttp
from panel_helpers import make_app, post, shared_mark_of
from test_webmim_create import FakeWebmim, NOW, PANEL_NOW, card, db, place, point
from tires_sync import chats, connections, order_engine as engine, order_placed, order_templates as templates, orders, store, users
from tires_sync.secretbox import SecretBox


def ready(conn, chat=True, send=True, address="СПб, Гамма, Новая ул., 1"):
    """A tagged order of a point with an address, the owner's templates, and (unless told otherwise) an Avito chat of the order that may write."""
    templates.add_samples(conn)
    mine = card(conn, point_id=point(conn, name="Гамма-адрес", address=address))
    if chat:
        account = connections.create_account(conn, "Основной", NOW, "owner")
        connections.set_modes(conn, account, True, True, NOW, "owner")
        connections.set_chats_send(conn, account, send, NOW, "owner")
        conn.execute("INSERT INTO avito_chat (account_id, chat_id, peer_name, last_at, order_id) VALUES (?, 'c-1', 'Иван', ?, ?)", (account, NOW, mine))
    conn.commit()
    return mine


class Deliver:
    def __init__(self, fail=None):
        self.sent, self.fail = [], fail

    def __call__(self, chat_row, text, token):
        if self.fail is not None:
            raise self.fail
        self.sent.append((chat_row, text, token))


def placed(conn, mine, deliver, now=NOW):
    """The client's part of on_placed (the point's: tests/test_order_messages.py)."""
    return [order_placed.tell_client(conn, mine, "1001", now, "owner", deliver)]


def tasks(conn, mine):
    return [t for t in engine.list_tasks(conn, mine) if t["rule_id"] == order_placed.RULE and t["title"] == order_placed.CLIENT_TEMPLATE]


def test_the_template_name_is_the_owners_sample():
    assert (order_placed.CLIENT_TEMPLATE, "client") == templates.SAMPLES[0][:2]


def test_a_full_success_tells_the_client_once(tmp_path):
    conn = db(tmp_path)
    mine = ready(conn)
    assert place(conn, mine, FakeWebmim())["state"] == "done"
    deliver = Deliver()
    lines = placed(conn, mine, deliver)
    assert len(deliver.sent) == 1 and lines[0].startswith("Клиенту отправлено")
    text = deliver.sent[0][1]
    assert "Заказ 5001 оформлен на перемещение" in text and "СПб, Гамма, Новая ул., 1" in text and "Иван" in text
    [task] = tasks(conn, mine)
    assert task["status"] == "done" and task["entered_at"] == "webmim-1001" and task["body"] == text
    journal = " | ".join(e["text"] for e in orders.list_events(conn, mine))
    assert "Сообщение клиенту после оформления в WebMIM" in journal and "Заказ 5001 оформлен" in journal
    again = placed(conn, mine, deliver, now="2026-10-23T12:10:00")                     # a second call (a second worker, a repeated press)
    assert len(deliver.sent) == 1 and "второе не отправляется" in again[0] and len(tasks(conn, mine)) == 1


def test_no_chat_leaves_the_message_waiting_with_a_warning(tmp_path):
    conn = db(tmp_path)
    mine = ready(conn, chat=False)
    place(conn, mine, FakeWebmim())
    deliver = Deliver()
    [line] = placed(conn, mine, deliver)
    assert not deliver.sent and "нет чата Авито" in line and "ждёт в окне «Сообщения по заказу»" in line
    [task] = tasks(conn, mine)
    assert task["status"] == "pending" and "Заказ 5001" in task["body"]
    assert orders.list_events(conn, mine)[0]["level"] == "red"


def test_a_chat_that_may_not_write_leaves_it_waiting(tmp_path):
    conn = db(tmp_path)
    mine = ready(conn, send=False)
    place(conn, mine, FakeWebmim())
    deliver = Deliver()
    [line] = placed(conn, mine, deliver)
    assert not deliver.sent and "выключена отправка" in line and tasks(conn, mine)[0]["status"] == "pending"


def test_a_failed_send_leaves_it_waiting_and_it_can_be_sent_by_hand(tmp_path):
    conn = db(tmp_path)
    mine = ready(conn)
    place(conn, mine, FakeWebmim())
    [line] = placed(conn, mine, Deliver(fail=chats.ChatError("Авито отказал")))
    assert "Авито отказал" in line and "ждёт" in line
    [task] = tasks(conn, mine)
    assert task["status"] == "pending"
    by_hand = Deliver()
    engine.send_task(conn, task["id"], task["body"], by_hand, "2026-10-23T12:20:00", "owner")
    assert len(by_hand.sent) == 1 and tasks(conn, mine)[0]["status"] == "done"


def test_an_unknown_send_is_never_offered_to_send_again_blindly(tmp_path):
    conn = db(tmp_path)
    mine = ready(conn)
    place(conn, mine, FakeWebmim())
    [line] = placed(conn, mine, Deliver(fail=chats.ChatSendUnknown("timeout")))
    assert "неизвестно, ушло ли" in line and "повторно не отправляйте" in line


def test_a_crash_of_the_send_never_breaks_the_press(tmp_path):
    conn = db(tmp_path)
    mine = ready(conn)
    place(conn, mine, FakeWebmim())
    [line] = placed(conn, mine, Deliver(fail=OSError("network")))
    assert "не отправлено" in line and tasks(conn, mine)[0]["status"] == "pending"


def test_a_lacking_value_keeps_the_message_unsent(tmp_path):
    conn = db(tmp_path)
    mine = ready(conn, address="")
    place(conn, mine, FakeWebmim())
    deliver = Deliver()
    [line] = placed(conn, mine, deliver)
    assert not deliver.sent and "не хватает" in line and "Адрес точки" in line
    assert tasks(conn, mine)[0]["status"] == "error"


def test_a_template_waiting_for_the_transfer_number_says_so(tmp_path):
    conn = db(tmp_path)
    mine = ready(conn)
    template = next(t for t in templates.list_templates(conn) if t["name"] == order_placed.CLIENT_TEMPLATE)
    templates.update_template(conn, template["id"], template["name"], "client", "Заказ {webmim_number}, перемещение {transfer_number}")
    place(conn, mine, FakeWebmim())
    deliver = Deliver()
    [line] = placed(conn, mine, deliver)
    assert not deliver.sent and "подготовится, когда впишете номер перемещения" in line and tasks(conn, mine)[0]["status"] == "error"
    orders.update_card(conn, mine, {"transfer_number": "26R-922352"}, NOW, "owner")    # typed later: the task is ready to send by hand
    engine.refresh_tasks(conn, mine, NOW)
    assert tasks(conn, mine)[0]["status"] == "pending" and tasks(conn, mine)[0]["body"] == "Заказ 5001, перемещение 26R-922352"


def test_a_rule_message_waiting_for_the_transfer_number_says_so(tmp_path):
    conn = db(tmp_path)
    mine = ready(conn)
    template = next(t for t in templates.list_templates(conn) if t["name"] == order_placed.CLIENT_TEMPLATE)
    templates.update_template(conn, template["id"], template["name"], "client", "Заказ {webmim_number}, перемещение {transfer_number}")
    rule_task(conn, mine, "pending")
    place(conn, mine, FakeWebmim())
    [line] = placed(conn, mine, Deliver())
    assert "подготовится, когда впишете номер перемещения" in line and tasks(conn, mine) == []


def test_a_renamed_template_sends_nothing_of_our_own(tmp_path):
    conn = db(tmp_path)
    mine = ready(conn)
    template = next(t for t in templates.list_templates(conn) if t["name"] == order_placed.CLIENT_TEMPLATE)
    templates.update_template(conn, template["id"], "Клиенту: оформлено", "client", template["body"])
    place(conn, mine, FakeWebmim())
    deliver = Deliver()
    [line] = placed(conn, mine, deliver)
    assert not deliver.sent and "шаблона с таким именем" in line and tasks(conn, mine) == []
    assert orders.list_events(conn, mine)[0]["level"] == "red"


def test_a_template_for_the_manager_of_that_name_is_not_sent_to_the_client(tmp_path):
    conn = db(tmp_path)
    mine = ready(conn)
    template = next(t for t in templates.list_templates(conn) if t["name"] == order_placed.CLIENT_TEMPLATE)
    templates.update_template(conn, template["id"], template["name"], "manager", template["body"])
    place(conn, mine, FakeWebmim())
    deliver = Deliver()
    placed(conn, mine, deliver)
    assert not deliver.sent and tasks(conn, mine) == []


def rule_task(conn, mine, status, body="Заказ 5001 оформлен на перемещение (по правилу)"):
    """The task the rule of the stage «Заказ оформлен» prepares (the owner's rule 1 on the live panel): the same template, another rule."""
    template = next(t for t in templates.list_templates(conn) if t["name"] == order_placed.CLIENT_TEMPLATE)
    cur = conn.execute("INSERT INTO order_task (order_id, rule_id, entered_at, kind, arg, stage_id, status, title, body, result, created_at) "
                       "VALUES (?, 1, ?, 'message', ?, 0, ?, ?, ?, '', ?)", (mine, NOW, str(template["id"]), status, template["name"], body, NOW))
    conn.commit()
    return cur.lastrowid


def test_a_message_sent_by_the_rule_of_the_stage_is_not_sent_again(tmp_path):
    conn = db(tmp_path)
    mine = ready(conn)
    rule_task(conn, mine, "done")
    place(conn, mine, FakeWebmim())
    deliver = Deliver()
    [line] = placed(conn, mine, deliver)
    assert not deliver.sent and "второе не отправляется" in line and tasks(conn, mine) == []
    assert "уже отправлено по правилу этапа" in orders.list_events(conn, mine)[0]["text"]


def test_a_waiting_message_of_the_rule_is_the_one_sent(tmp_path):
    conn = db(tmp_path)
    mine = ready(conn)
    task_id = rule_task(conn, mine, "pending")
    place(conn, mine, FakeWebmim())
    deliver = Deliver()
    [line] = placed(conn, mine, deliver)
    assert len(deliver.sent) == 1 and line.startswith("Клиенту отправлено") and tasks(conn, mine) == []        # no second task in the card
    assert engine.get_task(conn, task_id)["status"] == "done" and "Заказ 5001 оформлен" in deliver.sent[0][1]   # the text as the card is now
    assert len(placed(conn, mine, deliver)) == 1 and len(deliver.sent) == 1


def test_a_message_of_the_rule_skipped_by_a_person_is_not_sent(tmp_path):
    conn = db(tmp_path)
    mine = ready(conn)
    rule_task(conn, mine, "skipped")
    place(conn, mine, FakeWebmim())
    deliver = Deliver()
    [line] = placed(conn, mine, deliver)
    assert not deliver.sent and "пропущено вручную" in line and tasks(conn, mine) == []


def stage_rule(conn):
    """The owner's rule 1 on the live panel: entering «Заказ оформлен» prepares «Клиенту: заказ оформлен», «спросить»."""
    from tires_sync import order_rules as rules
    template = next(t for t in templates.list_templates(conn) if t["name"] == order_placed.CLIENT_TEMPLATE)
    placed_stage = next(st["id"] for st in orders.list_stages(conn) if st["code"] == "placed")
    rules.add_rule(conn, placed_stage, "enter", 0, "message", template["id"], "", "ask", None)
    conn.commit()
    return placed_stage


def client_messages(conn, mine, moved_at):
    """The waiting messages to the client that the move at `moved_at` has just made."""
    return [t for t in engine.new_messages(conn, mine, moved_at) if engine.audience(conn, t) == "client"]


def rule_tasks(conn, mine):
    return [t for t in engine.list_tasks(conn, mine) if t["kind"] == "message" and t["rule_id"] > 0]


def ahead(conn, mine):
    """The card moved past «Заказ оформлен» by hand before the press: the press leaves its stage, the hook prepares the message first.
    (An earlier card goes to «Заказ оформлен» with the press itself: test_the_move_comes_before_the_hook_and_the_client_hears_once.)"""
    moving = next(st["id"] for st in orders.list_stages(conn) if st["code"] == "moving")
    orders.move_card(conn, mine, moving, NOW, "owner")


@pytest.mark.parametrize("deliver", [Deliver(), None])                                  # sent after WebMIM, or still waiting
def test_entering_the_stage_after_webmim_makes_no_second_message(tmp_path, deliver):
    conn = db(tmp_path)
    mine = ready(conn)
    placed_stage = stage_rule(conn)
    ahead(conn, mine)
    place(conn, mine, FakeWebmim())
    placed(conn, mine, deliver)
    orders.move_card(conn, mine, placed_stage, "2026-10-23T12:30:00", "owner")
    [rule_made] = rule_tasks(conn, mine)
    assert rule_made["status"] == "skipped" and rule_made["result"] == engine.PLACED_SKIP
    assert client_messages(conn, mine, "2026-10-23T12:30:00") == []          # no «Отправить клиенту?» window
    assert any("Задача пропущена (подготовлено после оформления в WebMIM)" in e["text"] for e in orders.list_events(conn, mine))
    engine.refresh_tasks(conn, mine, "2026-10-23T12:31:00")
    assert rule_tasks(conn, mine)[0]["status"] == "skipped"


@pytest.mark.parametrize("deliver", [Deliver(), None])                                  # sent, or no keys: waiting
def test_the_move_comes_before_the_hook_and_the_client_hears_once(tmp_path, deliver):
    """The press moves a «Новый заказ» to «Заказ оформлен» (its rule prepares the message), then the hook sends that very task: one message."""
    conn = db(tmp_path)
    mine = ready(conn)
    stage_rule(conn)
    result = place(conn, mine, FakeWebmim())
    assert "Этап: «Новый заказ» → «Заказ оформлен»" in result["text"]
    [rule_made] = rule_tasks(conn, mine)
    assert rule_made["status"] == "pending"                                             # the rule's message never goes by itself
    lines = placed(conn, mine, deliver)
    every = [t for t in engine.list_tasks(conn, mine) if t["kind"] == "message" and t["status"] != "skipped"]
    assert [t["id"] for t in every] == [rule_made["id"]] and tasks(conn, mine) == []     # one message task: the rule's, no second
    if deliver is not None:
        assert len(deliver.sent) == 1 and lines[0].startswith("Клиенту отправлено") and engine.get_task(conn, rule_made["id"])["status"] == "done"
        assert "Заказ 5001 оформлен" in deliver.sent[0][1]
    else:
        assert "ключи Авито недоступны" in lines[0] and engine.get_task(conn, rule_made["id"])["status"] == "pending"
    placed(conn, mine, deliver)                                                          # a second call sends nothing more
    assert deliver is None or len(deliver.sent) == 1


def test_entering_the_stage_without_webmim_prepares_the_message_as_before(tmp_path):
    conn = db(tmp_path)
    mine = ready(conn)
    placed_stage = stage_rule(conn)
    orders.update_card(conn, mine, {"webmim_number": "5000300"}, NOW, "owner")       # typed by hand, as before the button
    orders.move_card(conn, mine, placed_stage, "2026-10-23T12:30:00", "owner")
    [rule_made] = rule_tasks(conn, mine)
    assert rule_made["status"] == "pending" and len(client_messages(conn, mine, "2026-10-23T12:30:00")) == 1


def test_a_skipped_webmim_message_does_not_keep_the_rule_from_preparing_its_own(tmp_path):
    conn = db(tmp_path)
    mine = ready(conn, chat=False)
    placed_stage = stage_rule(conn)
    ahead(conn, mine)
    place(conn, mine, FakeWebmim())
    placed(conn, mine, Deliver())
    engine.skip_task(conn, tasks(conn, mine)[0]["id"], NOW, "owner")                  # a person skipped the message after WebMIM
    orders.move_card(conn, mine, placed_stage, "2026-10-23T12:30:00", "owner")
    assert rule_tasks(conn, mine)[0]["status"] == "pending"


# ---- the press on the page --------------------------------------------------------------------------------------------------------------

@pytest.fixture
def panel(tmp_path, monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")
    box = SecretBox(Fernet.generate_key())
    fake = FakeWebmim()
    avito = ScriptedHttp((200, {"access_token": "t", "expires_in": 3600}), (200, {"id": 42}),
                         (200, {"id": "m-out", "created": 1_791_900_600, "direction": "out", "type": "text", "content": {"text": "ok"}}))
    app = make_app(tmp_path, SECRET_BOX=box, WEBMIM_HTTP=fake, AVITO_HTTP=avito)
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, PANEL_NOW)
    connections.set_config(conn, "webmim_orders", 0, {"server": "test", "login": "api-bot"}, PANEL_NOW, "owner")
    connections.set_secret(conn, box, "webmim_orders", 0, "password", "pw-SECRET-1", PANEL_NOW, "owner")
    conn.commit()
    client = app.test_client()
    with client.session_transaction() as session:
        session["user"], session["epoch"], session["csrf"] = "owner", 0, "tok"
        session["pw"] = shared_mark_of(client.application)
    return app, conn, fake, avito, box, client


def test_the_press_tells_the_client_once_and_says_so(panel):
    app, conn, fake, avito, box, client = panel
    mine = ready(conn)
    conn.execute("UPDATE order_card SET delivery_date = '2026-10-08' WHERE id = ?", (mine,))
    conn.commit()
    account = conn.execute("SELECT account_id FROM avito_chat WHERE order_id = ?", (mine,)).fetchone()[0]
    connections.set_secret(conn, box, "avito", account, "client_id", "CLIENTID-12345678", PANEL_NOW, "owner")
    connections.set_secret(conn, box, "avito", account, "client_secret", "SECRETVALUE-XYZ", PANEL_NOW, "owner")
    conn.commit()
    assert post(client, "/orders/%d/webmim/create" % mine, "tok").status_code == 302
    html = client.get("/orders/%d" % mine).get_data(as_text=True)
    assert "Оформлен в WebMIM" in html and "Клиенту отправлено «Клиенту: заказ оформлен»" in html
    sent = [c for c in avito.calls if c[0] == "POST" and c[1].endswith("/messages")]
    assert len(sent) == 1 and "Заказ 5001 оформлен" in json.loads(sent[0][3].decode("utf-8"))["message"]["text"]
    post(client, "/orders/%d/webmim/create" % mine, "tok")                            # a second press: refused, no second message
    assert len([c for c in avito.calls if c[0] == "POST" and c[1].endswith("/messages")]) == 1


def test_a_part_made_tells_nobody_and_finishing_it_tells_the_client(panel):
    app, conn, fake, avito, box, client = panel
    mine = ready(conn, chat=False)
    conn.execute("UPDATE order_card SET delivery_date = '2026-10-08' WHERE id = ?", (mine,))
    conn.commit()
    fake.fail[("POST", "/api/v2/orders/{id}/items")] = ["timeout"]
    post(client, "/orders/%d/webmim/create" % mine, "tok")
    assert tasks(conn, mine) == []                                                      # a part made: nothing to the client
    post(client, "/orders/%d/webmim/create" % mine, "tok")                            # «Досоздать»: done now
    html = client.get("/orders/%d" % mine).get_data(as_text=True)
    assert "Клиенту не отправлено" in html and len(tasks(conn, mine)) == 1


def test_the_clients_words_never_colour_a_sent_message_red(tmp_path):
    """Хвост З1: the record of a sent message carries its text; «: ошибка (» in that text is the client's words, not a failure."""
    conn = db(tmp_path)
    mine = ready(conn)
    placed_stage = stage_rule(conn)
    orders.update_card(conn, mine, {"webmim_number": "5000300"}, NOW, "owner")
    orders.move_card(conn, mine, placed_stage, "2026-10-23T12:30:00", "owner")
    [rule_made] = rule_tasks(conn, mine)
    engine.send_task(conn, rule_made["id"], "Адрес: ошибка (исправили), заказ оформлен: ошибка", Deliver(), "2026-10-23T12:31:00", "owner")
    sent = [e for e in orders.list_events(conn, mine) if e["kind"] == "rule" and "выполнено" in e["text"]]
    assert len(sent) == 1 and sent[0]["level"] == "grey"
    conn.execute("UPDATE order_task SET status = 'error', result = 'Не хватает: номер.' WHERE id = ?", (rule_made["id"],))
    failed = engine.get_task(conn, rule_made["id"])
    engine._finish(conn, failed, "error", "нет чата", "2026-10-23T12:32:00", "owner")
    assert orders.list_events(conn, mine)[0]["level"] == "red"


def test_back_in_placed_the_rule_does_not_offer_a_sent_message_again(tmp_path):
    """Хвост З5 (L1): the order left «Заказ оформлен» and came back: «Клиенту: заказ оформлен» went already, the rule's new task is skipped."""
    conn = db(tmp_path)
    mine = ready(conn)
    placed_stage = stage_rule(conn)
    orders.update_card(conn, mine, {"webmim_number": "5000300"}, NOW, "owner")
    orders.move_card(conn, mine, placed_stage, "2026-10-23T12:30:00", "owner")
    [first] = rule_tasks(conn, mine)
    engine.send_task(conn, first["id"], first["body"], Deliver(), "2026-10-23T12:31:00", "owner")
    moving = next(st["id"] for st in orders.list_stages(conn) if st["code"] == "moving")
    orders.move_card(conn, mine, moving, "2026-10-23T12:40:00", "owner")
    orders.move_card(conn, mine, placed_stage, "2026-10-23T12:50:00", "owner")
    again = [t for t in rule_tasks(conn, mine) if t["id"] != first["id"]]
    assert len(again) == 1 and again[0]["status"] == "skipped" and again[0]["result"] == engine.SENT_SKIP
    assert client_messages(conn, mine, "2026-10-23T12:50:00") == []


def test_back_in_placed_a_message_not_sent_before_is_prepared_as_before(tmp_path):
    conn = db(tmp_path)
    mine = ready(conn)
    placed_stage = stage_rule(conn)
    orders.update_card(conn, mine, {"webmim_number": "5000300"}, NOW, "owner")
    orders.move_card(conn, mine, placed_stage, "2026-10-23T12:30:00", "owner")
    [first] = rule_tasks(conn, mine)
    engine.skip_task(conn, first["id"], "2026-10-23T12:31:00", "owner")
    moving = next(st["id"] for st in orders.list_stages(conn) if st["code"] == "moving")
    orders.move_card(conn, mine, moving, "2026-10-23T12:40:00", "owner")
    orders.move_card(conn, mine, placed_stage, "2026-10-23T12:50:00", "owner")
    assert len(client_messages(conn, mine, "2026-10-23T12:50:00")) == 1
