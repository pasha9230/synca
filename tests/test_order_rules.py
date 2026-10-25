"""Rules of the stages and the manager's command words (storage and checks)."""
from __future__ import annotations

import pytest

from tires_sync import order_rules as rules, order_templates as tpl, orders, store

NOW = "2026-10-12T12:00:00"


def db(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    orders.ensure_pipeline(conn, NOW)
    return conn


def stage(conn, code):
    return next(s["id"] for s in orders.list_stages(conn) if s["code"] == code)


def test_rules_are_added_listed_changed_switched_and_deleted(tmp_path):
    conn = db(tmp_path)
    placed = stage(conn, "placed")
    t = tpl.add_template(conn, "Клиенту", "client", "Заказ {order_no}")
    first = rules.add_rule(conn, placed, "enter", 0, "message", t, "", "ask", None)
    second = rules.add_rule(conn, placed, "stale", 24, "remind", None, "Позвоните клиенту", "auto", None)
    assert [r["id"] for r in rules.list_rules(conn, placed)] == [first, second] and rules.list_rules(conn, stage(conn, "talk")) == []
    rules.update_rule(conn, second, "stale", 48, "tag_add", None, "срочно", "auto", None)
    assert rules.get_rule(conn, second)["hours"] == 48 and rules.get_rule(conn, second)["arg"] == "срочно"
    rules.set_enabled(conn, second, False)
    assert rules.get_rule(conn, second)["enabled"] == 0
    rules.delete_rule(conn, first)
    assert [r["id"] for r in rules.list_rules(conn, placed)] == [second]


def test_bad_rules_are_refused(tmp_path):
    conn = db(tmp_path)
    placed, talk = stage(conn, "placed"), stage(conn, "talk")
    t = tpl.add_template(conn, "Клиенту", "client", "x")
    bad = [
        (9999, "enter", 0, "remind", None, "x", "ask", None),                 # no such stage
        (placed, "sometimes", 0, "remind", None, "x", "ask", None),
        (placed, "stale", 0, "remind", None, "x", "ask", None),               # stale needs hours
        (placed, "stale", 99999, "remind", None, "x", "ask", None),
        (placed, "enter", 0, "fly", None, "", "ask", None),
        (placed, "enter", 0, "message", None, "", "ask", None),               # message needs a template
        (placed, "enter", 0, "message", 9999, "", "ask", None),
        (placed, "enter", 0, "remind", None, "", "ask", None),                # remind needs a text
        (placed, "enter", 0, "remind", None, "x" * 201, "ask", None),
        (placed, "enter", 0, "tag_add", None, "", "ask", None),
        (placed, "enter", 0, "move", None, str(placed), "ask", None),         # move to itself
        (placed, "enter", 0, "move", None, "9999", "ask", None),
        (placed, "enter", 0, "move", None, "abc", "ask", None),
        (placed, "enter", 0, "remind", None, "x", "maybe", None),
        (placed, "enter", 0, "remind", None, "x", "ask", 9999),               # no such point
    ]
    for args in bad:
        with pytest.raises(orders.OrderError):
            rules.add_rule(conn, *args)
    assert rules.list_rules(conn) == []
    assert rules.add_rule(conn, placed, "enter", 0, "move", None, str(talk), "ask", None)
    assert rules.add_rule(conn, placed, "enter", 0, "record_sale", None, "", "ask", None)


def test_a_template_in_use_cannot_be_deleted_and_stage_data_goes_with_the_stage(tmp_path):
    conn = db(tmp_path)
    mine = orders.add_stage(conn, "Мой", "#7F56D9", "open", "", 0)
    other = orders.add_stage(conn, "Другой", "#7F56D9", "open", "", 0)
    t = tpl.add_template(conn, "Клиенту", "client", "x")
    rules.add_rule(conn, mine, "enter", 0, "message", t, "", "ask", None)
    with pytest.raises(orders.OrderError):
        tpl.delete_template(conn, t)
    rules.add_command(conn, mine, "Принял ", other)
    rules.add_rule(conn, other, "enter", 0, "move", None, str(mine), "ask", None)
    with pytest.raises(orders.OrderError):                       # other stage points at mine: a hanging reference is not allowed
        orders.delete_stage(conn, mine)
    rules.delete_rule(conn, rules.list_rules(conn, other)[0]["id"])
    orders.delete_stage(conn, mine)
    assert rules.list_rules(conn, mine) == [] and rules.list_commands(conn, mine) == []
    tpl.delete_template(conn, t)


def test_command_words_are_normalised_unique_per_stage_and_checked(tmp_path):
    conn = db(tmp_path)
    at_point, moving, done = stage(conn, "at_point"), stage(conn, "moving"), stage(conn, "done")
    cid = rules.add_command(conn, moving, " Принял ", at_point)
    assert rules.list_commands(conn, moving)[0]["word"] == "принял" and rules.find_command(conn, moving, "ПРИНЯЛ")["to_stage_id"] == at_point
    assert rules.find_command(conn, moving, "выдал") is None and rules.find_command(conn, done, "принял") is None
    for args in ((moving, "принял", done), (moving, "", done), (moving, "две слова", done), (moving, "а" * 31, done),
                 (moving, "ok", moving), (moving, "ok", 9999), (9999, "ok", done)):
        with pytest.raises(orders.OrderError):
            rules.add_command(conn, *args)
    rules.delete_command(conn, cid)
    assert rules.list_commands(conn, moving) == []


def test_a_tag_with_a_comma_is_refused_and_a_command_word_has_no_digits(tmp_path):
    conn = db(tmp_path)
    placed = stage(conn, "placed")
    with pytest.raises(orders.OrderError):
        rules.add_rule(conn, placed, "enter", 0, "tag_add", None, "VIP, срочно", "ask", None)
    with pytest.raises(orders.OrderError):
        rules.add_command(conn, stage(conn, "moving"), "ок1", placed)
    rules.add_command(conn, stage(conn, "moving"), "в-пути", placed)


def test_a_point_used_by_a_rule_cannot_be_deleted(tmp_path):
    from tires_sync import order_points as points
    conn = db(tmp_path)
    pid = points.save_point(conn, dict(name="Бета", channel="vk"))
    rule = rules.add_rule(conn, stage(conn, "placed"), "enter", 0, "remind", None, "x", "ask", pid)
    with pytest.raises(points.PointError):
        points.delete_point(conn, pid)
    rules.delete_rule(conn, rule)
    points.delete_point(conn, pid)
