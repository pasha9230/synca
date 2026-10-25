"""The automation engine: rules fire on entering a stage or hanging in it and make tasks."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from tires_sync import order_engine as engine, order_points as points, order_rules as rules, order_templates as tpl, orders, store

NOW = "2026-10-12T12:00:00"


def db(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    orders.ensure_pipeline(conn, NOW)
    return conn


def stage(conn, code):
    return next(s["id"] for s in orders.list_stages(conn) if s["code"] == code)


def full_card(conn, point_name="Бета"):
    existing = [p for p in points.list_points(conn) if p["name"] == point_name]
    pid = existing[0]["id"] if existing else points.save_point(conn, dict(name=point_name, manager_name="Алексей", channel="vk"))
    card_id = orders.create_card(conn, dict(client_name="Иван", client_phone="+7 900 111 22 33", point_id=pid, delivery_date="2026-10-15",
                                            delivery_time="14:00"), NOW, "o")
    orders.add_item(conn, card_id, goods_id="1", name="Nokian 205/55 R16", brand_key="nokian", season="winter", diameter=16, territory="1",
                    qty=4, sale_unit=5000, supplier_price=4000, terms=None, fee_pct=0, promo_pct=0, now=NOW, author="o")
    return card_id


def tasks(conn, card_id):
    return engine.list_tasks(conn, card_id)


def test_an_enter_rule_makes_one_message_task_per_stay_in_the_stage(tmp_path):
    conn = db(tmp_path)
    t = tpl.add_template(conn, "Клиенту", "client", "Заказ №{order_no} для {client_name}")
    rules.add_rule(conn, stage(conn, "talk"), "enter", 0, "message", t, "", "ask", None)
    card = full_card(conn)
    assert tasks(conn, card) == []
    orders.move_card(conn, card, stage(conn, "talk"), "2026-10-12T12:10:00", "o")
    [task] = tasks(conn, card)
    assert task["status"] == "pending" and task["body"] == "Заказ №%d для Иван" % card and task["kind"] == "message"
    engine.fire_enter(conn, card, "2026-10-12T12:11:00")                       # the same stay again: nothing new
    assert len(tasks(conn, card)) == 1
    orders.move_card(conn, card, stage(conn, "new"), "2026-10-12T13:00:00", "o")
    orders.move_card(conn, card, stage(conn, "talk"), "2026-10-12T14:00:00", "o")   # a new stay: a new task
    assert len(tasks(conn, card)) == 2


def test_a_missing_value_makes_an_error_task_with_no_text(tmp_path):
    conn = db(tmp_path)
    t = tpl.add_template(conn, "Менеджеру", "manager", "{point}: {client_phone}")
    rules.add_rule(conn, stage(conn, "talk"), "enter", 0, "message", t, "", "ask", None)
    card = orders.create_card(conn, dict(client_name="Иван"), NOW, "o")
    orders.move_card(conn, card, stage(conn, "talk"), "2026-10-12T12:10:00", "o")
    [task] = tasks(conn, card)
    assert task["status"] == "error" and task["body"] == "" and "Точка" in task["result"] and "Телефон" in task["result"]


def test_a_rule_for_a_point_fires_only_for_that_point_and_a_disabled_rule_never(tmp_path):
    conn = db(tmp_path)
    card = full_card(conn, "Бета")
    other = points.save_point(conn, dict(name="Гамма", channel="vk"))
    mine = points.list_points(conn)[0]["id"] if points.list_points(conn)[0]["name"] == "Бета" else other
    rules.add_rule(conn, stage(conn, "talk"), "enter", 0, "remind", None, "для Гамма", "ask", other)
    off = rules.add_rule(conn, stage(conn, "talk"), "enter", 0, "remind", None, "выключено", "ask", None)
    rules.set_enabled(conn, off, False)
    rules.add_rule(conn, stage(conn, "talk"), "enter", 0, "remind", None, "для всех", "ask", None)
    orders.move_card(conn, card, stage(conn, "talk"), "2026-10-12T12:10:00", "o")
    assert [t["title"] for t in tasks(conn, card)] == ["для всех"]
    assert mine


def test_ask_waits_for_the_owner_and_auto_runs_at_once(tmp_path):
    conn = db(tmp_path)
    talk = stage(conn, "talk")
    rules.add_rule(conn, talk, "enter", 0, "tag_add", None, "срочно", "ask", None)
    rules.add_rule(conn, talk, "enter", 0, "tag_add", None, "новый", "auto", None)
    card = full_card(conn)
    orders.move_card(conn, card, talk, "2026-10-12T12:10:00", "o")
    assert orders.get_card(conn, card)["tags"] == ["новый"]
    ask = next(t for t in tasks(conn, card) if t["status"] == "pending")
    engine.run_task(conn, ask["id"], "2026-10-12T12:20:00", "owner")
    assert orders.get_card(conn, card)["tags"] == ["новый", "срочно"]
    rules.add_rule(conn, talk, "enter", 0, "tag_remove", None, "новый", "auto", None)
    orders.move_card(conn, card, stage(conn, "new"), "2026-10-12T13:00:00", "o")
    orders.move_card(conn, card, talk, "2026-10-12T14:00:00", "o")
    assert "новый" not in orders.get_card(conn, card)["tags"]                  # the auto add and then the auto remove, in the order of the rules
    done = [t for t in tasks(conn, card) if t["status"] == "done"]
    assert len(done) >= 2
    events = [e["text"] for e in orders.list_events(conn, card)]
    assert any("Задача создана" in e for e in events)
    with pytest.raises(orders.OrderError):
        engine.run_task(conn, ask["id"], NOW, "owner")                          # it is done already


def test_skipping_closes_a_task_without_doing_it(tmp_path):
    conn = db(tmp_path)
    rules.add_rule(conn, stage(conn, "talk"), "enter", 0, "tag_add", None, "срочно", "ask", None)
    card = full_card(conn)
    orders.move_card(conn, card, stage(conn, "talk"), "2026-10-12T12:10:00", "o")
    [task] = tasks(conn, card)
    engine.skip_task(conn, task["id"], NOW, "owner")
    assert engine.list_tasks(conn, card)[0]["status"] == "skipped" and orders.get_card(conn, card)["tags"] == []


def test_a_move_rule_moves_with_all_checks_and_a_loop_is_stopped(tmp_path):
    conn = db(tmp_path)
    placed, moving = stage(conn, "placed"), stage(conn, "moving")
    rules.add_rule(conn, placed, "enter", 0, "move", None, str(moving), "auto", None)
    rules.add_rule(conn, moving, "enter", 0, "move", None, str(placed), "auto", None)
    card = full_card(conn)
    orders.move_card(conn, card, placed, "2026-10-12T12:10:00", "o")
    assert orders.get_card(conn, card)["stage_id"] == moving                    # moved on by the first rule
    [loop] = [t for t in tasks(conn, card) if t["status"] == "error"]
    assert "петля" in loop["result"].lower()
    bare = orders.create_card(conn, dict(client_name="Пётр"), NOW, "o")
    rules.add_rule(conn, stage(conn, "new"), "enter", 0, "move", None, str(placed), "auto", None)
    engine.fire_enter(conn, bare, NOW)
    assert orders.get_card(conn, bare)["stage_id"] == stage(conn, "new")        # not enough data for «Заказ оформлен»: stays, the task shows why
    assert any(t["status"] == "error" and "заполните" in t["result"] for t in tasks(conn, bare))


def test_a_task_of_a_stage_the_card_has_left_is_not_done(tmp_path):
    conn = db(tmp_path)
    placed = stage(conn, "placed")
    rules.add_rule(conn, placed, "enter", 0, "move", None, str(stage(conn, "moving")), "ask", None)
    card = full_card(conn)
    orders.move_card(conn, card, placed, "2026-10-12T12:10:00", "o")
    orders.move_card(conn, card, stage(conn, "talk"), "2026-10-12T12:20:00", "o")
    [task] = tasks(conn, card)
    engine.run_task(conn, task["id"], "2026-10-12T12:30:00", "owner")
    assert engine.list_tasks(conn, card)[0]["status"] == "skipped" and orders.get_card(conn, card)["stage_id"] == stage(conn, "talk")


def test_a_sale_rule_records_the_sale_once(tmp_path):
    conn = db(tmp_path)
    done = stage(conn, "done")
    rules.add_rule(conn, done, "enter", 0, "record_sale", None, "", "auto", None)
    card = full_card(conn)
    orders.move_card(conn, card, stage(conn, "placed"), "2026-10-12T12:10:00", "o")
    orders.move_card(conn, card, done, "2026-10-12T12:20:00", "o")
    assert [i for i in orders.get_card(conn, card)["sale_ids"] if i > 0]
    assert [t["status"] for t in tasks(conn, card)] == ["done"]


def test_a_stale_rule_fires_after_the_threshold_once_per_stay(tmp_path):
    conn = db(tmp_path)
    rules.add_rule(conn, stage(conn, "talk"), "stale", 24, "remind", None, "Позвоните", "ask", None)
    card = full_card(conn)
    orders.move_card(conn, card, stage(conn, "talk"), "2026-10-12T12:00:00", "o")
    start = datetime(2026, 10, 12, 12, 0, 0)
    assert engine.fire_stale(conn, (start + timedelta(hours=23)).isoformat()) == 0 and tasks(conn, card) == []
    assert engine.fire_stale(conn, (start + timedelta(hours=25)).isoformat()) == 1
    assert engine.fire_stale(conn, (start + timedelta(hours=26)).isoformat()) == 0 and len(tasks(conn, card)) == 1


def test_a_failing_engine_never_breaks_moving_a_card(tmp_path, monkeypatch):
    conn = db(tmp_path)
    card = full_card(conn)
    monkeypatch.setattr(engine, "fire_enter", lambda *a, **k: 1 / 0)
    assert orders.move_card(conn, card, stage(conn, "talk"), "2026-10-12T12:10:00", "o") is True
    assert orders.get_card(conn, card)["stage_id"] == stage(conn, "talk")


def test_counts_of_waiting_tasks_per_card(tmp_path):
    conn = db(tmp_path)
    rules.add_rule(conn, stage(conn, "talk"), "enter", 0, "remind", None, "a", "ask", None)
    rules.add_rule(conn, stage(conn, "talk"), "enter", 0, "remind", None, "b", "ask", None)
    card = full_card(conn)
    orders.move_card(conn, card, stage(conn, "talk"), "2026-10-12T12:10:00", "o")
    assert engine.waiting_counts(conn) == {card: 2}


def test_a_move_task_still_works_after_its_rule_is_deleted_and_a_tag_task_is_not_faked(tmp_path):
    conn = db(tmp_path)
    talk = stage(conn, "talk")
    move = rules.add_rule(conn, talk, "enter", 0, "move", None, str(stage(conn, "new")), "ask", None)
    tag = rules.add_rule(conn, talk, "enter", 0, "tag_add", None, "срочно", "ask", None)
    card = full_card(conn)
    orders.move_card(conn, card, talk, "2026-10-12T12:10:00", "o")
    rules.delete_rule(conn, move)
    rules.delete_rule(conn, tag)
    for task in tasks(conn, card):
        engine.run_task(conn, task["id"], "2026-10-12T12:30:00", "owner")                  # no crash
    assert orders.get_card(conn, card)["stage_id"] == stage(conn, "new") and orders.get_card(conn, card)["tags"] == ["срочно"]


def test_one_task_is_run_once_even_when_pressed_twice(tmp_path):
    conn = db(tmp_path)
    rules.add_rule(conn, stage(conn, "talk"), "enter", 0, "tag_add", None, "срочно", "ask", None)
    card = full_card(conn)
    orders.move_card(conn, card, stage(conn, "talk"), "2026-10-12T12:10:00", "o")
    [task] = tasks(conn, card)
    engine.run_task(conn, task["id"], "2026-10-12T12:20:00", "owner")
    n = len(orders.list_events(conn, card))
    with pytest.raises(orders.OrderError):
        engine.run_task(conn, task["id"], "2026-10-12T12:21:00", "owner")
    assert len(orders.list_events(conn, card)) == n


def test_a_card_that_breaks_the_tick_does_not_stop_the_others(tmp_path, monkeypatch):
    conn = db(tmp_path)
    talk = stage(conn, "talk")
    rules.add_rule(conn, talk, "stale", 1, "remind", None, "Позвоните", "ask", None)
    first, second = full_card(conn), full_card(conn)
    for c in (first, second):
        orders.move_card(conn, c, talk, "2026-10-12T12:00:00", "o")
    real = engine._make_task

    def flaky(conn, card, rule, now, chain):
        if card["id"] == first:
            raise RuntimeError("boom")
        return real(conn, card, rule, now, chain)
    monkeypatch.setattr(engine, "_make_task", flaky)
    assert engine.fire_stale(conn, "2026-10-12T15:00:00") == 1 and len(tasks(conn, second)) == 1


def test_a_task_that_is_already_there_is_skipped_quietly(tmp_path):
    conn = db(tmp_path)
    talk = stage(conn, "talk")
    rule = rules.add_rule(conn, talk, "enter", 0, "remind", None, "x", "ask", None)
    card = full_card(conn)
    orders.move_card(conn, card, talk, "2026-10-12T12:10:00", "o")
    c = orders.get_card(conn, card)
    assert engine._make_task(conn, c, rules.get_rule(conn, rule), NOW, (talk,)) is False and len(tasks(conn, card)) == 1


def test_a_message_task_shows_the_card_as_it_is_now_and_an_error_task_recovers(tmp_path):
    conn = db(tmp_path)
    t = tpl.add_template(conn, "Клиенту", "client", "{client_phone}: {items_full} {total}")
    rules.add_rule(conn, stage(conn, "talk"), "enter", 0, "message", t, "", "ask", None)
    card = orders.create_card(conn, dict(client_name="Иван"), NOW, "o")
    orders.move_card(conn, card, stage(conn, "talk"), "2026-10-12T12:10:00", "o")
    [task] = tasks(conn, card)
    assert task["status"] == "error"
    orders.update_card(conn, card, {"client_phone": "+7 900 111 22 33"}, NOW, "o")
    orders.add_item(conn, card, goods_id="1", name="Nokian", brand_key="nokian", season="winter", diameter=16, territory="1", qty=4,
                    sale_unit=5000, supplier_price=4000, terms=None, fee_pct=0, promo_pct=0, now=NOW, author="o")
    engine.refresh_tasks(conn, card, "2026-10-12T12:20:00")
    [task] = tasks(conn, card)
    assert task["status"] == "pending" and "Кол-во 4 шт" in task["body"] and "20 000" in task["body"]
    orders.update_item(conn, orders.list_items(conn, card)[0]["id"], 2, 4500, NOW, "o")                      # changed after the task was made
    engine.refresh_tasks(conn, card, "2026-10-12T12:30:00")
    assert "Кол-во 2 шт" in tasks(conn, card)[0]["body"] and "9 000" in tasks(conn, card)[0]["body"]
    orders.update_card(conn, card, {"client_phone": ""}, NOW, "o")
    engine.refresh_tasks(conn, card, "2026-10-12T12:40:00")
    assert tasks(conn, card)[0]["status"] == "error"
    orders.update_card(conn, card, {"client_phone": "+7 900 111 22 33"}, NOW, "o")
    assert engine.run_task(conn, tasks(conn, card)[0]["id"], "2026-10-12T12:50:00", "owner") == "done"      # run_task looks at the card as well


def test_a_task_stuck_in_running_comes_back_after_a_while(tmp_path):
    conn = db(tmp_path)
    rules.add_rule(conn, stage(conn, "talk"), "enter", 0, "tag_add", None, "срочно", "ask", None)
    card = full_card(conn)
    orders.move_card(conn, card, stage(conn, "talk"), "2026-10-12T12:10:00", "o")
    [task] = tasks(conn, card)
    conn.execute("UPDATE order_task SET status = 'running', done_at = '2026-10-12T12:11:00' WHERE id = ?", (task["id"],))
    conn.commit()
    engine.refresh_tasks(conn, card, "2026-10-12T12:15:00")
    assert tasks(conn, card)[0]["status"] == "running"                                  # four minutes: maybe still working
    engine.refresh_tasks(conn, card, "2026-10-12T12:30:00")
    assert tasks(conn, card)[0]["status"] == "pending"


def test_an_old_task_is_not_run_on_another_stay_of_the_same_second(tmp_path):
    conn = db(tmp_path)
    talk, new = stage(conn, "talk"), stage(conn, "new")
    rules.add_rule(conn, talk, "enter", 0, "move", None, str(new), "ask", None)
    card = full_card(conn)
    orders.move_card(conn, card, talk, "2026-10-12T12:10:00", "o")
    [task] = tasks(conn, card)
    orders.move_card(conn, card, stage(conn, "placed"), "2026-10-12T12:10:00", "o")                           # left, in the same second
    orders.move_card(conn, card, stage(conn, "moving"), "2026-10-12T12:10:00", "o")
    assert engine.run_task(conn, task["id"], "2026-10-12T12:11:00", "owner") == "skipped"
    assert orders.get_card(conn, card)["stage_id"] == stage(conn, "moving")


def test_stale_rules_do_not_touch_cards_on_finished_stages(tmp_path):
    conn = db(tmp_path)
    done = stage(conn, "done")
    rules.add_rule(conn, done, "stale", 1, "remind", None, "x", "ask", None)
    card = full_card(conn)
    orders.move_card(conn, card, stage(conn, "placed"), "2026-10-12T12:00:00", "o")
    orders.move_card(conn, card, done, "2026-10-12T12:00:00", "o")
    assert engine.fire_stale(conn, "2026-10-13T12:00:00") == 0
