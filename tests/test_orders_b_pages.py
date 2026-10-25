"""Pages of the automation part of Orders: templates, rules and commands in the settings, tasks in the card."""
from __future__ import annotations

from datetime import datetime

from panel_helpers import login, make_app, post
from tires_sync import order_engine as engine, order_points as points, order_rules as rules, order_templates as tpl, orders, store


def setup(tmp_path):
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 10, 12, 9, 0, 0))
    client = app.test_client()
    tok = login(client)
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, "2026-10-12T12:00:00")
    return app, client, tok, conn


def stage(conn, code):
    return next(s["id"] for s in orders.list_stages(conn) if s["code"] == code)


def test_templates_page_saves_edits_and_deletes_and_refuses_bad_variables(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    assert app.test_client().get("/orders/templates").status_code == 302
    post(client, "/orders/templates/save", tok, name="Клиенту", audience="client", body="Заказ {order_no} для {client_name}")
    [t] = tpl.list_templates(conn)
    page = client.get("/orders/templates").get_data(as_text=True)
    assert "Клиенту" in page and "{order_no}" in page
    post(client, "/orders/templates/save", tok, id=str(t["id"]), name="Клиенту 2", audience="manager", body="Заказ {order_no}")
    assert tpl.get_template(conn, t["id"])["name"] == "Клиенту 2"
    post(client, "/orders/templates/save", tok, name="Плохой", audience="client", body="{nope}")
    assert len(tpl.list_templates(conn)) == 1
    post(client, "/orders/templates/%d/delete" % t["id"], tok)
    assert tpl.list_templates(conn) == []
    assert client.post("/orders/templates/save", data={"name": "x"}).status_code == 400


def test_settings_page_adds_and_removes_rules_and_commands(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    talk, placed = stage(conn, "talk"), stage(conn, "placed")
    t = tpl.add_template(conn, "Клиенту", "client", "x {order_no}")
    post(client, "/orders/settings/rule/add", tok, stage_id=str(placed), trigger="enter", hours="0", action="message", template_id=str(t), mode="ask")
    post(client, "/orders/settings/rule/add", tok, stage_id=str(talk), trigger="stale", hours="24", action="remind", arg="Позвоните", mode="auto")
    post(client, "/orders/settings/rule/add", tok, stage_id=str(talk), trigger="stale", hours="0", action="remind", arg="плохо", mode="auto")       # refused
    post(client, "/orders/settings/rule/add", tok, stage_id=str(talk), trigger="enter", hours="0", action="move", arg_stage=str(placed), mode="ask")
    assert rules.list_rules(conn, talk)[-1]["arg"] == str(placed)
    rules.delete_rule(conn, rules.list_rules(conn, talk)[-1]["id"])
    assert [r["action"] for r in rules.list_rules(conn)] == ["remind", "message"] or len(rules.list_rules(conn)) == 2
    page = client.get("/orders/settings").get_data(as_text=True)
    assert "Позвоните" in page and "Правила" in page
    rule = rules.list_rules(conn, talk)[0]
    post(client, "/orders/settings/rule/%d/toggle" % rule["id"], tok)
    assert rules.get_rule(conn, rule["id"])["enabled"] == 0
    post(client, "/orders/settings/rule/%d/delete" % rule["id"], tok)
    assert rules.list_rules(conn, talk) == []
    post(client, "/orders/settings/command/add", tok, stage_id=str(stage(conn, "moving")), word="Принял", to_stage_id=str(stage(conn, "at_point")))
    [cmd] = rules.list_commands(conn)
    assert cmd["word"] == "принял" and "принял" in client.get("/orders/settings").get_data(as_text=True)
    post(client, "/orders/settings/command/%d/delete" % cmd["id"], tok)
    assert rules.list_commands(conn) == []


def test_the_card_shows_tasks_and_runs_or_skips_them_and_the_board_marks_the_card(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    talk = stage(conn, "talk")
    t = tpl.add_template(conn, "Клиенту", "client", "Привет, {client_name}")
    rules.add_rule(conn, talk, "enter", 0, "message", t, "", "ask", None)
    rules.add_rule(conn, talk, "enter", 0, "tag_add", None, "срочно", "ask", None)
    card = orders.create_card(conn, dict(client_name="Иван"), "2026-10-12T12:00:00", "o")
    orders.move_card(conn, card, talk, "2026-10-12T12:10:00", "o")
    page = client.get("/orders/%d" % card).get_data(as_text=True)
    assert "Что ждёт действия" in page and "Привет, Иван" in page and "Добавить тег" in page
    board = client.get("/orders").get_data(as_text=True)
    assert "waiting" in board and "Ждут действия" in board and "Ждёт действия: 2" in board
    msg, tag = engine.list_tasks(conn, card)
    post(client, "/orders/%d/task/%d/run" % (card, msg["id"]), tok)
    post(client, "/orders/%d/task/%d/skip" % (card, tag["id"]), tok)
    assert [t["status"] for t in engine.list_tasks(conn, card)] == ["done", "skipped"]
    assert "Что ждёт действия" not in client.get("/orders/%d" % card).get_data(as_text=True)
    assert post(client, "/orders/%d/task/999/run" % card, tok).status_code in (302, 404)
    other = orders.create_card(conn, dict(client_name="Пётр"), "2026-10-12T12:00:00", "o")
    assert post(client, "/orders/%d/task/%d/run" % (other, msg["id"]), tok).status_code == 404         # a task of another order


def test_the_inbox_lists_questions_answers_them_and_keeps_auto_answers(tmp_path):
    from tires_sync import order_manager as mgr
    app, client, tok, conn = setup(tmp_path)
    pid = points.save_point(conn, dict(name="Бета", channel="vk"))
    conn.execute("UPDATE point SET vk_peer_id = '111' WHERE id = ?", (pid,))
    conn.commit()
    assert app.test_client().get("/orders/inbox").status_code == 302
    mgr.handle_message(conn, "q1", "111", "Когда завоз?", "2026-10-12T12:00:00")
    page = client.get("/orders/inbox").get_data(as_text=True)
    assert "Когда завоз?" in page and "Бета" in page
    assert "Входящие" in client.get("/orders").get_data(as_text=True)
    [m] = mgr.list_inbox(conn)
    post(client, "/orders/inbox/%d/answer" % m["id"], tok, text="Завтра")
    assert mgr.list_inbox(conn)[0]["answer"] == "Завтра"
    post(client, "/orders/inbox/%d/answer" % m["id"], tok, text="")                      # refused
    post(client, "/orders/inbox/answers/add", tok, keywords="график", answer="С 10 до 20")
    [a] = mgr.list_bot_answers(conn)
    assert a["enabled"] == 0 and "график" in client.get("/orders/inbox").get_data(as_text=True)
    post(client, "/orders/inbox/answers/%d/toggle" % a["id"], tok)
    assert mgr.list_bot_answers(conn)[0]["enabled"] == 1
    post(client, "/orders/inbox/answers/%d/delete" % a["id"], tok)
    assert mgr.list_bot_answers(conn) == []
