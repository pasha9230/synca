"""«Отмена» and «Отказ/невыкуп» are one stage «Отмена / невыкуп» with the reason (the owner 2026-10-29): the merge of an old pipeline by the
code of the pipeline (orders._merge_refused_stage), the reason chosen on the card, the question «на ОТХ?», the point's «возврат отдал»."""
from __future__ import annotations

import pytest

from tires_sync import order_sheet, orders, store

NOW = "2026-10-29T12:00:00"
LATER = "2026-10-30T09:00:00"
OLD = (("new", "Новый заказ", "#7F56D9", "open"), ("at_point", "Товар на точке", "#17B26A", "open"), ("oth", "На ОТХ", "#667085", "open"),
       ("done", "Выполнено", "#079455", "won"), ("cancel", "Отмена", "#D92D20", "lost"), ("refused", "Отказ/невыкуп", "#C01048", "lost"),
       ("return", "Возврат", "#B54708", "closed"))


def old_db(tmp_path, cancel_name="Отмена"):
    """A base of the pipeline before the merge: «Отмена» and «Отказ/невыкуп» apart, orders on both."""
    conn = store.connect(str(tmp_path / "old.db"))
    for position, (code, name, color, kind) in enumerate(OLD):
        conn.execute("INSERT INTO order_stage (code, name, color, position, kind) VALUES (?,?,?,?,?)",
                     (code, cancel_name if code == "cancel" else name, color, position, kind))
    conn.commit()
    return conn


def sid(conn, code):
    row = conn.execute("SELECT id FROM order_stage WHERE code = ?", (code,)).fetchone()
    return row[0] if row else None


def put(conn, code, reason="", entered="2026-10-20T10:00:00"):
    card = orders.create_card(conn, {"client_name": "Иван", "cancel_reason": reason}, NOW, "owner")
    conn.execute("UPDATE order_card SET stage_id = ?, entered_at = ? WHERE id = ?", (sid(conn, code), entered, card))
    conn.commit()
    return card


def test_the_orders_of_refused_go_to_the_one_stage_with_the_reason(tmp_path):
    conn = old_db(tmp_path)
    a, b, c = put(conn, "refused"), put(conn, "refused", "клиент не пришёл"), put(conn, "cancel", "передумал")
    refused = sid(conn, "refused")
    orders.ensure_pipeline(conn, LATER)
    assert sid(conn, "refused") is None and conn.execute("SELECT COUNT(*) FROM order_stage WHERE id = ?", (refused,)).fetchone()[0] == 0
    cancel = orders.get_stage(conn, sid(conn, "cancel"))
    assert cancel["name"] == "Отмена / невыкуп" and cancel["kind"] == "lost"
    for card, reason in ((a, "не выкупил"), (b, "не выкупил: клиент не пришёл"), (c, "передумал")):
        got = orders.get_card(conn, card)
        assert got["stage"]["code"] == "cancel" and got["cancel_reason"] == reason
    assert orders.get_card(conn, a)["entered_at"] == "2026-10-20T10:00:00"                 # the time on the stage is kept
    assert any("объединён" in e["text"] and "не выкупил" in e["text"] for e in orders.list_events(conn, a))
    assert not any("объединён" in e["text"] for e in orders.list_events(conn, c))
    stages = orders.list_stages(conn)
    assert [s["position"] for s in stages] == list(range(len(stages)))
    orders.ensure_pipeline(conn, LATER)                                                     # again: nothing more
    assert len([e for e in orders.list_events(conn, a) if "объединён" in e["text"]]) == 1
    assert sid(conn, "refused") is None


def test_the_reports_count_the_merged_orders_as_before(tmp_path):
    """The Google table «Реализация»: «О» for both before and after; the analytics count kind «lost» — the stage stays «lost»."""
    conn = old_db(tmp_path)
    card = put(conn, "refused")
    before = order_sheet.realization(orders.get_card(conn, card))
    orders.ensure_pipeline(conn, LATER)
    after = orders.get_card(conn, card)
    assert before == order_sheet.realization(after) == order_sheet.REFUSED and after["stage"]["kind"] == "lost"


def test_the_rules_of_refused_come_switched_off_and_the_pointers_follow(tmp_path):
    conn = old_db(tmp_path)
    refused, cancel = sid(conn, "refused"), sid(conn, "cancel")
    conn.execute("INSERT INTO order_rule (stage_id, trigger, action, arg, enabled) VALUES (?, 'enter', 'task', 'позвонить', 1)", (refused,))
    conn.execute("INSERT INTO order_rule (stage_id, trigger, action, arg, enabled) VALUES (?, 'enter', 'move', ?, 1)", (sid(conn, "at_point"), str(refused)))
    conn.execute("INSERT INTO order_command (stage_id, word, to_stage_id) VALUES (?, 'вернул', ?)", (refused, sid(conn, "return")))
    conn.execute("INSERT INTO order_command (stage_id, word, to_stage_id) VALUES (?, 'вернул', ?)", (cancel, sid(conn, "return")))
    conn.execute("INSERT INTO order_command (stage_id, word, to_stage_id) VALUES (?, 'не пришёл', ?)", (sid(conn, "at_point"), refused))
    card = put(conn, "refused")
    conn.execute("INSERT INTO order_task (order_id, rule_id, entered_at, kind, stage_id, status, title, created_at) VALUES (?, 1, 'x', 'task', ?, 'pending', 't', ?)",
                 (card, refused, NOW))
    conn.commit()
    orders.ensure_pipeline(conn, LATER)
    assert conn.execute("SELECT stage_id, enabled FROM order_rule WHERE action = 'task'").fetchone() == (cancel, 0)      # never fires by itself
    assert conn.execute("SELECT arg FROM order_rule WHERE action = 'move'").fetchone()[0] == str(cancel)
    assert conn.execute("SELECT COUNT(*) FROM order_command WHERE stage_id = ? AND word = 'вернул'", (cancel,)).fetchone()[0] == 1
    assert conn.execute("SELECT to_stage_id FROM order_command WHERE word = 'не пришёл'").fetchone()[0] == cancel
    assert conn.execute("SELECT COUNT(*) FROM order_command WHERE stage_id = ? OR to_stage_id = ?", (refused, refused)).fetchone()[0] == 0
    assert conn.execute("SELECT stage_id FROM order_task WHERE order_id = ?", (card,)).fetchone()[0] == cancel


def test_the_owners_own_name_of_cancel_is_kept(tmp_path):
    conn = old_db(tmp_path, cancel_name="Отказ клиента")
    card = put(conn, "refused")
    orders.ensure_pipeline(conn, LATER)
    assert orders.get_stage(conn, sid(conn, "cancel"))["name"] == "Отказ клиента"
    assert any("«Отказ клиента»" in e["text"] for e in orders.list_events(conn, card))


def test_a_pipeline_without_cancel_gets_refused_as_it(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    conn.execute("INSERT INTO order_stage (code, name, color, position, kind) VALUES ('new', 'Новый заказ', '#7F56D9', 0, 'open')")
    conn.execute("INSERT INTO order_stage (code, name, color, position, kind) VALUES ('refused', 'Отказ/невыкуп', '#C01048', 1, 'lost')")
    conn.commit()
    orders.ensure_pipeline(conn, LATER)
    assert sid(conn, "refused") is None and orders.get_stage(conn, sid(conn, "cancel"))["name"] == "Отмена / невыкуп"


def test_a_new_pipeline_has_one_stage_of_cancel(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    orders.ensure_pipeline(conn, NOW)
    codes = [s["code"] for s in orders.list_stages(conn)]
    assert "refused" not in codes and codes.count("cancel") == 1


@pytest.mark.parametrize("kind, text, reason", [
    ("отменил клиент", "", "отменил клиент"), ("не выкупил", " не пришёл  за шинами ", "не выкупил: не пришёл за шинами"),
    ("другое", "нашёл дешевле", "другое: нашёл дешевле"),
])
def test_the_reason_is_its_choice_and_its_text(kind, text, reason):
    assert orders.cancel_reason_of(kind, text) == reason


@pytest.mark.parametrize("kind, text", [("", ""), ("потерялся", "x"), ("другое", "  ")])
def test_a_reason_without_its_choice_or_other_without_a_text_is_refused(kind, text):
    with pytest.raises(orders.OrderError):
        orders.cancel_reason_of(kind, text)


# ---- review round 1 (2026-10-30, chats-orders) -----------------------------------------------------------------------------------------

def test_a_rule_moving_into_refused_comes_switched_off_and_a_loop_is_gone(tmp_path):
    """D3: a move into «Отказ/невыкуп» needed no reason, into «cancel» it does: the rule comes switched off; «cancel → refused» would be a move to
    itself: its command is gone."""
    conn = old_db(tmp_path)
    refused, cancel = sid(conn, "refused"), sid(conn, "cancel")
    conn.execute("INSERT INTO order_rule (stage_id, trigger, hours, action, arg, enabled) VALUES (?, 'stale', 72, 'move', ?, 1)", (sid(conn, "at_point"), str(refused)))
    conn.execute("INSERT INTO order_command (stage_id, word, to_stage_id) VALUES (?, 'передумал', ?)", (cancel, refused))
    conn.commit()
    orders.ensure_pipeline(conn, LATER)
    assert conn.execute("SELECT arg, enabled FROM order_rule WHERE action = 'move'").fetchone() == (str(cancel), 0)
    assert conn.execute("SELECT COUNT(*) FROM order_command WHERE stage_id = to_stage_id").fetchone()[0] == 0


def test_a_command_of_the_point_into_the_merged_stage_gives_a_reason(tmp_path):
    """A word of the point into «Отмена / невыкуп» gives a reason: «не выкупил» only for a word that led to «Отказ/невыкуп» before the merge
    (it lets «возврат отдал» through), a neutral one for the others (the review, round 2)."""
    from tires_sync import order_manager as mgr
    conn = old_db(tmp_path)
    conn.execute("INSERT INTO order_command (stage_id, word, to_stage_id) VALUES (?, 'не забрал', ?)", (sid(conn, "at_point"), sid(conn, "refused")))
    conn.execute("INSERT INTO order_command (stage_id, word, to_stage_id) VALUES (?, 'отменили', ?)", (sid(conn, "new"), sid(conn, "cancel")))
    conn.commit()
    plain, was_refused = put(conn, "at_point"), put(conn, "at_point")
    orders.ensure_pipeline(conn, LATER)
    ids = dict(conn.execute("SELECT word, id FROM order_command").fetchall())
    assert orders.merged_command(conn, ids["не забрал"]) and not orders.merged_command(conn, ids["отменили"])
    outcome, _answer = mgr.apply(conn, orders.get_card(conn, plain), sid(conn, "cancel"), "менеджер Юнона", LATER, None)
    got = orders.get_card(conn, plain)
    assert got["stage"]["code"] == "cancel" and got["cancel_reason"] == "команда точки", outcome
    assert not mgr._return_from(got)
    mgr.apply(conn, orders.get_card(conn, was_refused), sid(conn, "cancel"), "менеджер Юнона", LATER, None, orders.NOT_BOUGHT + " (команда точки)")
    assert mgr._return_from(orders.get_card(conn, was_refused))


def test_a_deleted_order_of_refused_comes_back_to_the_merged_stage(tmp_path):
    from tires_sync import trash
    conn = old_db(tmp_path)
    card = put(conn, "refused")
    record = trash.delete_order(conn, card, NOW, "owner")
    orders.ensure_pipeline(conn, LATER)
    trash.restore_order(conn, record, LATER, "owner")
    got = orders.get_card(conn, card)
    assert got["stage"]["code"] == "cancel" and got["cancel_reason"] == "не выкупил"


def test_the_reason_is_read_without_case_for_a_return(tmp_path):
    from tires_sync import order_manager as mgr
    conn = old_db(tmp_path)
    card = put(conn, "cancel", "Не выкупил")
    assert mgr._return_from(orders.get_card(conn, card))


def test_the_reason_of_a_word_is_the_same_by_the_point_and_by_the_owners_confirmation(tmp_path):
    """N1: the owner's «Подтвердить» of a disputed word gives the same reason as the point's own command."""
    from tires_sync import order_manager as mgr
    conn = old_db(tmp_path)
    conn.execute("INSERT INTO order_command (stage_id, word, to_stage_id) VALUES (?, 'не забрал', ?)", (sid(conn, "at_point"), sid(conn, "refused")))
    conn.commit()
    orders.ensure_pipeline(conn, LATER)
    command = conn.execute("SELECT id, to_stage_id FROM order_command WHERE word = 'не забрал'").fetchone()
    assert mgr.merged_reason(conn, {"id": command[0], "to_stage_id": command[1]}) == "не выкупил (команда точки)"
    assert mgr.merged_reason(conn, {"to_stage_id": command[1]}) == "" and mgr.merged_reason(conn, None) == ""
    source = open(mgr.__file__, encoding="utf-8").read()
    assert source.count("merged_reason(conn, ") == 3                                       # _command and confirm both pass it
