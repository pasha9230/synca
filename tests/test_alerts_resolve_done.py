"""alerts-resolve-done: tasks done before the records closed by themselves (order_engine.resolve_done, the owner 2026-11-08: orders done on
the 7th kept their «не подготовлена / не отправлено» red). The same check as resolve_message_done for every done task; shown only, closed
with --apply, once a run in the journal. A skipped task is no fix; a record made after the task was done is alive. Fakes only."""
from __future__ import annotations

from test_alerts_resolve import LATER, reds, unsent_after_placing
from test_order_placed import Deliver
from test_webmim_create import NOW, card
from tires_sync import cli, order_alerts as alerts, order_engine as engine, order_placed, orders, store


def done_before_the_fix(conn, task_id):
    """As on the 7th: the task went done, its red records stayed (no resolve_done then)."""
    conn.execute("UPDATE order_task SET status = 'done', done_at = ? WHERE id = ?", (LATER, task_id))
    conn.commit()


def old_reds(tmp_path):
    conn, mine, task = unsent_after_placing(tmp_path)
    title = order_placed.CLIENT_TEMPLATE
    orders.add_event(conn, mine, NOW, "правило №%d" % task["rule_id"], "rule", "Задача не подготовлена: " + title, level="red")
    orders.add_event(conn, mine, NOW, "правило №999", "rule", "Задача не подготовлена: " + title, level="red")      # another rule
    orders.add_event(conn, mine, NOW, "owner", "chat", "Сообщение клиенту «Клиенту: другое» не отправлено: нет чата.", level="red")
    another = card(conn, client_phone="+7 (911) 111-22-99")
    orders.add_event(conn, another, NOW, order_placed.AUTHOR, "chat", "Сообщение клиенту «%s» не отправлено: нет чата." % title, level="red")
    conn.commit()
    done_before_the_fix(conn, task["id"])
    return conn, mine, another, task


def test_only_the_done_tasks_records_close(tmp_path):
    conn, mine, another, _task = old_reds(tmp_path)
    before = reds(conn, mine)
    found = engine.resolve_done_backlog(conn, LATER, apply=True)
    assert {e["order_id"] for e in found} == {mine}
    left = reds(conn, mine)
    assert not any(t.startswith("Сообщение клиенту «Клиенту: заказ оформлен»") for t in left)
    assert len(before) - len(left) == len(found) == 2                                  # the hook's record and the rule's
    assert "Задача не подготовлена: Клиенту: заказ оформлен" in left                    # rule №999 has no done task
    assert "Сообщение клиенту «Клиенту: другое» не отправлено: нет чата." in left       # another template
    assert reds(conn, another) == ["Сообщение клиенту «Клиенту: заказ оформлен» не отправлено: нет чата."]   # another order
    who = {a for (a,) in conn.execute("SELECT author FROM order_event_ack WHERE login = ?", (alerts.EVERYBODY,))}
    assert who == {engine.RESOLVE_DONE_AUTHOR}


def test_a_skipped_task_closes_nothing(tmp_path):
    conn, mine, task = unsent_after_placing(tmp_path)
    engine.skip_task(conn, task["id"], LATER, "owner")
    before = reds(conn, mine)
    assert before and engine.resolve_done_backlog(conn, LATER, apply=True) == []
    assert reds(conn, mine) == before


def test_without_apply_nothing_is_written_and_a_second_run_changes_nothing(tmp_path, capsys):
    conn, mine, _another, _task = old_reds(tmp_path)
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    before = reds(conn, mine)
    assert cli.main(["alerts-resolve-done", "--db", path]) == 0
    out = capsys.readouterr().out
    assert "№%d: Сообщение клиенту «Клиенту: заказ оформлен» не отправлено" % mine in out and "нужен --apply" in out and ": 2" in out
    assert reds(conn, mine) == before
    assert conn.execute("SELECT COUNT(*) FROM order_event_ack").fetchone()[0] == 0
    assert cli.main(["alerts-resolve-done", "--db", path, "--apply"]) == 0
    assert "закрыто: 2" in capsys.readouterr().out
    acks = conn.execute("SELECT COUNT(*) FROM order_event_ack").fetchone()[0]
    assert cli.main(["alerts-resolve-done", "--db", path, "--apply"]) == 0
    assert "закрыто: 0" in capsys.readouterr().out
    assert conn.execute("SELECT COUNT(*) FROM order_event_ack").fetchone()[0] == acks == 2
    assert len(reds(conn, mine)) == len(before) - 2


def test_a_record_of_a_later_stay_stays_open(tmp_path):
    """Review 2026-11-08: the task of rule №N went done; the order went back to the stage (or to another point) and the task of the same
    rule for the new stay failed «Не хватает: Номер перемещения». Its records are made after the old task was done: alive, not closed."""
    conn, mine, _another, task = old_reds(tmp_path)
    title = order_placed.CLIENT_TEMPLATE
    again = "2026-10-24T09:00:00"                                                       # after LATER, the old task's done_at
    rule = "Задача не подготовлена: " + title
    chat = "Сообщение клиенту «%s» не отправлено: Не хватает: Номер перемещения." % title
    orders.add_event(conn, mine, again, "правило №%d" % task["rule_id"], "rule", rule, level="red")
    orders.add_event(conn, mine, again, order_placed.AUTHOR, "chat", chat, level="red")
    conn.commit()
    found = engine.resolve_done_backlog(conn, again, apply=True)
    assert len(found) == 2 and all(e["at"] <= LATER for e in found)                     # the old stay's records only
    left = reds(conn, mine)
    assert rule in left and chat in left


def test_a_run_with_apply_is_one_line_in_the_journal(tmp_path):
    conn, mine, _another, _task = old_reds(tmp_path)
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    assert cli.main(["alerts-resolve-done", "--db", path]) == 0
    mine_only = lambda: [e for e in store.list_audit(conn) if e["action"] == engine.RESOLVE_DONE_AUDIT]
    assert mine_only() == []                                                            # a look writes nothing
    assert cli.main(["alerts-resolve-done", "--db", path, "--apply"]) == 0
    assert cli.main(["alerts-resolve-done", "--db", path, "--apply"]) == 0
    assert [(e["action"], e["author"], e["detail"]) for e in mine_only()] == [
        (engine.RESOLVE_DONE_AUDIT, engine.RESOLVE_DONE_AUTHOR, {"closed": 0, "orders": []}),
        (engine.RESOLVE_DONE_AUDIT, engine.RESOLVE_DONE_AUTHOR, {"closed": 2, "orders": [mine]})]
