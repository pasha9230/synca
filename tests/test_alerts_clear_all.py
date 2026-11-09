"""alerts-clear-all (the owner 2026-11-09 «Почисти у всех уведомления»): every red record closes for everybody, every other record is seen
by every person; nothing deleted; counts only without --apply; a second run finds nothing; one line in the journal of actions."""
from __future__ import annotations

from test_alerts_resolve import LATER, reds
from test_webmim_create import NOW, card, db
from tires_sync import cli, order_alerts as alerts, orders, store


def world(tmp_path):
    conn = db(tmp_path)
    one, two = card(conn), card(conn, client_phone="+7 (911) 111-22-98")
    orders.add_event(conn, one, NOW, "owner", "chat", "Сообщение клиенту «X» не отправлено: нет чата.", level="red")
    orders.add_event(conn, two, NOW, "owner", "webmim", "Сверьте номер.", level="yellow")
    conn.commit()
    return conn, one, two


def unseen(conn, login, order_id):
    last = conn.execute("SELECT MAX(id) FROM order_event WHERE order_id = ?", (order_id,)).fetchone()[0]
    seen = conn.execute("SELECT last_id FROM order_event_seen WHERE login = ? AND order_id = ?", (login, order_id)).fetchone()
    return seen is None or seen[0] < last


def test_counts_only_then_clears_for_everybody_and_a_second_run_finds_nothing(tmp_path):
    conn, one, two = world(tmp_path)
    total = conn.execute("SELECT COUNT(*) FROM order_event").fetchone()[0]
    found = alerts.clear_all(conn, LATER, ["owner", "masha"])
    assert found["red"] >= 1 and found["people"] == 2 and found["seen"] >= 2
    assert reds(conn, one) and unseen(conn, "masha", two)                       # nothing written without apply
    alerts.clear_all(conn, LATER, ["owner", "masha"], apply=True)
    conn.commit()
    assert reds(conn, one) == [] and not unseen(conn, "masha", two) and not unseen(conn, "owner", one)
    assert conn.execute("SELECT COUNT(*) FROM order_event").fetchone()[0] == total      # nothing deleted
    again = alerts.clear_all(conn, LATER, ["owner", "masha"])
    assert again["red"] == 0 and again["seen"] == 0
    assert conn.execute("SELECT COUNT(*) FROM audit WHERE action = ?", (alerts.CLEAR_ALL_AUDIT,)).fetchone()[0] == 1


def test_the_command_counts_without_apply(tmp_path, capsys):
    conn, one, _two = world(tmp_path)
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    conn.close()
    assert cli.main(["alerts-clear-all", "--db", path]) == 0
    assert "ничего не записано" in capsys.readouterr().out
    conn = store.connect(path)
    assert reds(conn, one)
    assert cli.main(["alerts-clear-all", "--db", path, "--apply"]) == 0
    conn = store.connect(path)
    assert reds(conn, one) == []
