"""A message that waited for a lacking value becomes sendable once the value is typed (the owner 2026-11-02, order 504: «можно забирать»
made without the WebMIM number stayed «ошибка» after the number was filled in): it is built again from the card as it is now, on the card's
refresh and before the send; a value taken away makes it an error again. Nothing goes by itself on a refresh. Fakes only."""
from __future__ import annotations

import pytest

from test_order_placed import Deliver, panel, placed, ready, tasks  # noqa: F401  (panel: the fixture)
from panel_helpers import post
from test_webmim_create import NOW, FakeWebmim, db, place
from tires_sync import order_engine as engine, order_manager as mgr, order_placed, order_templates as templates, orders
from tires_sync.panel.routes_orders import messages_view

LATER = "2026-10-23T13:00:00"
BODY = "Заказ {webmim_number} приехал, можно забирать."


def at_point(conn, mine):
    stage = conn.execute("SELECT id FROM order_stage WHERE code = 'at_point'").fetchone()[0]
    conn.execute("UPDATE order_card SET stage_id = ?, entered_at = ? WHERE id = ?", (stage, NOW, mine))
    conn.commit()


def arrival(conn, mine):
    return [t for t in engine.list_tasks(conn, mine) if t["rule_id"] == engine.ARRIVED_RULE]


@pytest.fixture
def sample(monkeypatch):
    """The live case: no owner's template of that name, the sample's text (the task keeps no template id)."""
    monkeypatch.setattr(mgr, "_arrived_body", lambda conn: BODY)


def no_arrival_template(conn):
    conn.execute("DELETE FROM order_template WHERE name = ?", (mgr.ARRIVED_TEMPLATE,))
    conn.commit()


def test_arrival_without_the_number_waits_then_becomes_sendable(tmp_path, sample):
    conn = db(tmp_path)
    mine = ready(conn)
    no_arrival_template(conn)
    at_point(conn, mine)
    deliver = Deliver()
    sent, _note = mgr.tell_client_arrived(conn, mine, deliver, NOW, "точка")
    [task] = arrival(conn, mine)
    assert not sent and not deliver.sent and task["status"] == "error" and task["arg"] == "" and "Номер заказа WebMIM" in task["result"]
    assert not messages_view(conn, mine, True, LATER)[0]["can"]

    orders.update_card(conn, mine, {"webmim_number": "5000504"}, LATER, "owner")
    engine.refresh_tasks(conn, mine, LATER)
    [task] = arrival(conn, mine)
    assert task["status"] == "pending" and task["body"] == "Заказ 5000504 приехал, можно забирать." and task["result"] == ""
    assert deliver.sent == []                                            # a refresh sends nothing by itself
    assert messages_view(conn, mine, True, LATER)[0]["can"]

    engine.send_task(conn, task["id"], task["body"], deliver, LATER, "owner")
    assert [(text, token) for _row, text, token in deliver.sent] == [(task["body"], task["entered_at"])]     # the arrival's own mark
    assert arrival(conn, mine)[0]["status"] == "done"
    assert mgr.tell_client_arrived(conn, mine, deliver, LATER, "точка")[0] and len(deliver.sent) == 1        # once per arrival


def test_the_number_taken_away_makes_it_an_error_again(tmp_path, sample):
    conn = db(tmp_path)
    mine = ready(conn)
    no_arrival_template(conn)
    at_point(conn, mine)
    mgr.tell_client_arrived(conn, mine, Deliver(), NOW, "точка")
    orders.update_card(conn, mine, {"webmim_number": "5000504"}, LATER, "owner")
    engine.refresh_tasks(conn, mine, LATER)
    orders.update_card(conn, mine, {"webmim_number": ""}, LATER, "owner")
    engine.refresh_tasks(conn, mine, LATER)
    [task] = arrival(conn, mine)
    assert task["status"] == "error" and task["body"] == "" and "Номер заказа WebMIM" in task["result"]
    with pytest.raises(orders.OrderError):
        engine.send_task(conn, task["id"], "Заказ приехал", Deliver(), LATER, "owner")


def test_an_existing_task_without_a_template_id_is_fixed(tmp_path, sample):
    """Order 504's task 173 as it is in the base: rule −3, empty arg, «ошибка» with the old words."""
    conn = db(tmp_path)
    mine = ready(conn)
    at_point(conn, mine)
    card = orders.get_card(conn, mine)
    conn.execute("INSERT INTO order_task (order_id, rule_id, entered_at, kind, arg, stage_id, status, title, body, result, created_at) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (mine, engine.ARRIVED_RULE, mgr.arrived_token(card, NOW), "message", "", card["stage_id"], "error", mgr.ARRIVED_TEMPLATE, "",
                  "Не отправлено само: не хватает: Номер заказа WebMIM.", NOW))
    conn.commit()
    orders.update_card(conn, mine, {"webmim_number": "5000504", "transfer_number": "26R-1"}, LATER, "owner")
    task = arrival(conn, mine)[0]
    deliver = Deliver()
    engine.send_task(conn, task["id"], "Заказ 5000504 приехал, можно забирать.", deliver, LATER, "owner")   # refreshed before the send
    assert len(deliver.sent) == 1 and arrival(conn, mine)[0]["status"] == "done"


def test_arrival_with_the_owners_template_keeps_its_id(tmp_path):
    conn = db(tmp_path)
    mine = ready(conn)
    template = next(t for t in templates.list_templates(conn) if t["name"] == mgr.ARRIVED_TEMPLATE)
    templates.update_template(conn, template["id"], template["name"], "client", BODY)
    at_point(conn, mine)
    mgr.tell_client_arrived(conn, mine, Deliver(), NOW, "точка")
    [task] = arrival(conn, mine)
    assert task["status"] == "error" and task["arg"] == str(template["id"])
    orders.update_card(conn, mine, {"webmim_number": "5000504"}, LATER, "owner")
    engine.refresh_tasks(conn, mine, LATER)
    assert arrival(conn, mine)[0]["status"] == "pending" and arrival(conn, mine)[0]["body"] == "Заказ 5000504 приехал, можно забирать."


def test_skipped_or_done_by_hand_is_left_alone(tmp_path, sample):
    conn = db(tmp_path)
    mine = ready(conn)
    no_arrival_template(conn)
    at_point(conn, mine)
    mgr.tell_client_arrived(conn, mine, Deliver(), NOW, "точка")
    engine.skip_task(conn, arrival(conn, mine)[0]["id"], LATER, "owner")
    orders.update_card(conn, mine, {"webmim_number": "5000504"}, LATER, "owner")
    engine.refresh_tasks(conn, mine, LATER)
    assert arrival(conn, mine)[0]["status"] == "skipped" and arrival(conn, mine)[0]["body"] == ""


def test_placed_message_comes_back_and_goes_again_with_its_value(tmp_path):
    conn = db(tmp_path)
    mine = ready(conn)
    template = next(t for t in templates.list_templates(conn) if t["name"] == order_placed.CLIENT_TEMPLATE)
    templates.update_template(conn, template["id"], template["name"], "client", "Заказ {webmim_number}, перемещение {transfer_number}")
    place(conn, mine, FakeWebmim())
    deliver = Deliver()
    placed(conn, mine, deliver)
    assert tasks(conn, mine)[0]["status"] == "error" and tasks(conn, mine)[0]["arg"] == str(template["id"])
    orders.update_card(conn, mine, {"transfer_number": "26R-1"}, LATER, "owner")
    engine.refresh_tasks(conn, mine, LATER)
    assert tasks(conn, mine)[0]["status"] == "pending" and not deliver.sent
    orders.update_card(conn, mine, {"transfer_number": ""}, LATER, "owner")
    engine.refresh_tasks(conn, mine, LATER)
    assert tasks(conn, mine)[0]["status"] == "error" and "Номер перемещения" in tasks(conn, mine)[0]["result"]
    orders.update_card(conn, mine, {"transfer_number": "26R-2"}, LATER, "owner")
    task = tasks(conn, mine)[0]
    engine.send_task(conn, task["id"], "Заказ 5001, перемещение 26R-2", deliver, LATER, "owner")
    assert [text for _row, text, _token in deliver.sent] == ["Заказ 5001, перемещение 26R-2"] and tasks(conn, mine)[0]["status"] == "done"


def test_the_inbox_line_send_by_hand_is_answered_after_the_press(tmp_path, sample):
    """«…не хватает: Номер заказа WebMIM. Отправьте вручную.» of «Входящие» is closed once the press sent it: nobody writes it twice."""
    conn = db(tmp_path)
    mine = ready(conn)
    no_arrival_template(conn)
    at_point(conn, mine)
    mgr.tell_client_arrived(conn, mine, Deliver(), NOW, "точка")
    inbox = "SELECT status, answer FROM manager_message WHERE message_id LIKE 'arrived-%'"
    assert [r[0] for r in conn.execute(inbox)] == ["new"]
    orders.update_card(conn, mine, {"webmim_number": "5000504"}, LATER, "owner")
    task = arrival(conn, mine)[0]
    engine.send_task(conn, task["id"], "Заказ 5000504 приехал, можно забирать.", Deliver(), LATER, "owner")
    [(status, answer)] = conn.execute(inbox).fetchall()
    assert status != "new" and answer.startswith("Итог:")


def test_a_deleted_template_falls_back_to_the_arrival_text(tmp_path, sample):
    conn = db(tmp_path)
    mine = ready(conn)
    template = next(t for t in templates.list_templates(conn) if t["name"] == mgr.ARRIVED_TEMPLATE)
    templates.update_template(conn, template["id"], template["name"], "client", BODY)
    at_point(conn, mine)
    mgr.tell_client_arrived(conn, mine, Deliver(), NOW, "точка")
    assert arrival(conn, mine)[0]["arg"] == str(template["id"])
    no_arrival_template(conn)
    orders.update_card(conn, mine, {"webmim_number": "5000504"}, LATER, "owner")
    engine.refresh_tasks(conn, mine, LATER)
    assert arrival(conn, mine)[0]["status"] == "pending" and "5000504" in arrival(conn, mine)[0]["body"]


def test_a_failure_building_the_arrival_text_does_not_break_the_card(tmp_path, sample, monkeypatch):
    conn = db(tmp_path)
    mine = ready(conn)
    no_arrival_template(conn)
    at_point(conn, mine)
    mgr.tell_client_arrived(conn, mine, Deliver(), NOW, "точка")

    def broken(conn, card):
        raise RuntimeError("oth")
    monkeypatch.setattr(mgr, "_oth_arrived_body", broken)
    engine.refresh_tasks(conn, mine, LATER)                              # never raises: the card opens, the task stays as it was
    assert arrival(conn, mine)[0]["status"] == "error"


def test_a_failure_building_the_arrival_text_keeps_the_rest_of_the_refresh(tmp_path, sample, monkeypatch):
    """A task stuck «running» after a killed panel comes back even when the arrival's text cannot be built in the same refresh."""
    conn = db(tmp_path)
    mine = ready(conn)
    no_arrival_template(conn)
    card = orders.get_card(conn, mine)
    conn.execute("INSERT INTO order_task (order_id, rule_id, entered_at, kind, arg, stage_id, status, title, body, result, created_at, done_at) "
                 "VALUES (?, 0, 'x', 'remind', '', ?, 'running', 'Позвонить', '', '', ?, ?)", (mine, card["stage_id"], NOW, NOW))
    conn.commit()
    at_point(conn, mine)
    mgr.tell_client_arrived(conn, mine, Deliver(), NOW, "точка")

    def broken(conn, card):
        raise RuntimeError("oth")
    monkeypatch.setattr(mgr, "_oth_arrived_body", broken)
    engine.refresh_tasks(conn, mine, "2026-10-23T18:00:00")
    assert [t["status"] for t in engine.list_tasks(conn, mine) if t["kind"] == "remind"] == ["pending"]


def test_the_draft_marks_what_is_lacking(tmp_path, sample):
    conn = db(tmp_path)
    mine = ready(conn)
    no_arrival_template(conn)
    at_point(conn, mine)
    mgr.tell_client_arrived(conn, mine, Deliver(), NOW, "точка")
    [view] = messages_view(conn, mine, True, LATER)
    assert view["draft"] == "Заказ [Номер заказа WebMIM] приехал, можно забирать." and view["fields"] == [("webmim_number", "Номер заказа WebMIM")]


def test_sent_by_hand_while_lacking_closes_it_for_good(tmp_path, sample):
    """«Отправлено вручную» of a message that lacks a value: done, the inbox line answered; never sent by itself after (a chat, a refresh,
    a second «приехал»)."""
    conn = db(tmp_path)
    mine = ready(conn)
    no_arrival_template(conn)
    at_point(conn, mine)
    mgr.tell_client_arrived(conn, mine, Deliver(), NOW, "точка")
    engine.sent_by_hand(conn, arrival(conn, mine)[0]["id"], LATER, "owner")
    [task] = arrival(conn, mine)
    assert task["status"] == "done" and task["result"] == "отправлено вручную"
    assert any("отправлено вручную" in e["text"] for e in orders.list_events(conn, mine))
    [(status, answer)] = conn.execute("SELECT status, answer FROM manager_message WHERE message_id LIKE 'arrived-%'").fetchall()
    assert status != "new" and "вручную" in answer
    orders.update_card(conn, mine, {"webmim_number": "5000504"}, LATER, "owner")
    engine.refresh_tasks(conn, mine, LATER)
    deliver = Deliver()
    assert mgr.arrived_after_link(conn, mine, deliver, LATER, "owner") == ""
    mgr.tell_client_arrived(conn, mine, deliver, LATER, "точка")
    assert deliver.sent == [] and arrival(conn, mine)[0]["status"] == "done"
    with pytest.raises(orders.OrderError):
        engine.sent_by_hand(conn, task["id"], LATER, "owner")


def test_skipped_is_never_sent_after_the_value_comes(tmp_path, sample):
    """Order 504: the owner wrote it by hand and pressed «Пропустить»; the number filled in later sends nothing."""
    conn = db(tmp_path)
    mine = ready(conn)
    no_arrival_template(conn)
    at_point(conn, mine)
    mgr.tell_client_arrived(conn, mine, Deliver(), NOW, "точка")
    engine.skip_task(conn, arrival(conn, mine)[0]["id"], LATER, "owner")
    orders.update_card(conn, mine, {"webmim_number": "5000504"}, LATER, "owner")
    engine.refresh_tasks(conn, mine, LATER)
    deliver = Deliver()
    assert mgr.arrived_after_link(conn, mine, deliver, LATER, "owner") == ""
    mgr.tell_client_arrived(conn, mine, deliver, LATER, "точка")
    assert deliver.sent == [] and arrival(conn, mine)[0]["status"] == "skipped" and messages_view(conn, mine, True, LATER) == []


def test_the_window_shows_the_draft_the_field_and_the_manual_mark(panel):
    from test_order_messages import client_tasks, need_transfer, page, placed_waiting  # noqa: F401
    from test_send_popup import _window
    app, conn, fake, avito, box, client = panel
    mine = placed_waiting(panel)
    [task] = client_tasks(conn, mine)
    assert task["status"] == "error"
    _, win = _window(page(client, mine, messages=1))
    # the owner 2026-11-04: a client got the marks copied by hand, so the draft is to read, «Скопировать» waits for the value (test_order_draft_safety)
    assert "[Номер перемещения]" in win and 'data-focus-field="transfer_number"' in win and "data-copy" not in win
    assert 'formaction="/orders/%d/task/%d/run"' % (mine, task["id"]) in win
    post(client, "/orders/%d/task/%d/run" % (mine, task["id"]), "tok")
    assert client_tasks(conn, mine)[0]["status"] == "done" and client_tasks(conn, mine)[0]["result"] == "отправлено вручную"


def test_sent_by_hand_closes_the_unsent_block_and_the_sync_alerts(tmp_path):
    """A rule's message that did not reach the client («Не дошло клиенту», order_unsent) marked «Отправлено вручную» in the window: the
    block and the sync's lines of «Входящие» about its mark are closed — nobody writes it a second time."""
    conn = db(tmp_path)
    mine = ready(conn)
    template = next(t for t in templates.list_templates(conn) if t["name"] == order_placed.CLIENT_TEMPLATE)
    templates.update_template(conn, template["id"], template["name"], "client", "Заказ {webmim_number} оформлен")
    place(conn, mine, FakeWebmim())
    placed(conn, mine, Deliver(fail=RuntimeError("down")))
    [task] = tasks(conn, mine)
    token = engine.task_token(task)
    conn.execute("INSERT OR IGNORE INTO order_unsent (order_id, token, at, why, text) VALUES (?, ?, ?, 'Авито отказал', ?)", (mine, token, NOW, task["body"]))
    conn.execute("INSERT INTO manager_message (message_id, at, point_id, sender, text, status) VALUES (?, ?, NULL, 'система', 'не отправлено', 'new')",
                 ("send-%s-1" % token, NOW))
    conn.commit()
    engine.sent_by_hand(conn, task["id"], LATER, "owner")
    assert conn.execute("SELECT closed_at FROM order_unsent WHERE token = ?", (token,)).fetchone()[0] == LATER
    assert conn.execute("SELECT status FROM manager_message WHERE message_id = ?", ("send-%s-1" % token,)).fetchone()[0] != "new"
