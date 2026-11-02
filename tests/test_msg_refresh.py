"""A message that waited for a lacking value becomes sendable once the value is typed (the owner 2026-11-02, order 504: «можно забирать»
made without the WebMIM number stayed «ошибка» after the number was filled in): it is built again from the card as it is now, on the card's
refresh and before the send; a value taken away makes it an error again. Nothing goes by itself on a refresh. Fakes only."""
from __future__ import annotations

import pytest

from test_order_placed import Deliver, placed, ready, tasks
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
    assert not messages_view(conn, mine, True)[0]["can"]

    orders.update_card(conn, mine, {"webmim_number": "5000504"}, LATER, "owner")
    engine.refresh_tasks(conn, mine, LATER)
    [task] = arrival(conn, mine)
    assert task["status"] == "pending" and task["body"] == "Заказ 5000504 приехал, можно забирать." and task["result"] == ""
    assert deliver.sent == []                                            # a refresh sends nothing by itself
    assert messages_view(conn, mine, True)[0]["can"]

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
