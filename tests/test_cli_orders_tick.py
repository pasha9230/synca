"""`orders-tick`: the hourly check for orders that hang in a stage."""
from __future__ import annotations

from tires_sync import cli, order_engine as engine, order_rules as rules, orders, store
from tires_sync.cli import main


def test_the_tick_makes_stale_tasks_once_and_changes_nothing_else(tmp_path, monkeypatch, capsys):
    db = tmp_path / "t.db"
    conn = store.connect(db)
    orders.ensure_pipeline(conn, "2026-10-10T09:00:00")
    talk = next(s["id"] for s in orders.list_stages(conn) if s["code"] == "talk")
    rules.add_rule(conn, talk, "stale", 24, "remind", None, "Позвоните", "ask", None)
    card = orders.create_card(conn, dict(client_name="Иван"), "2026-10-10T09:00:00", "o")
    orders.move_card(conn, card, talk, "2026-10-10T09:00:00", "o")
    conn.close()
    monkeypatch.setattr(cli, "moscow_now", lambda: __import__("datetime").datetime(2026, 10, 11, 12, 0, 0))
    assert main(["orders-tick", "--db", str(db)]) == 0
    assert "1" in capsys.readouterr().out
    assert main(["orders-tick", "--db", str(db)]) == 0                       # the same stay: nothing new
    conn = store.connect(db)
    assert len(engine.list_tasks(conn, card)) == 1 and orders.get_card(conn, card)["stage_id"] == talk
