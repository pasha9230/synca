"""«Закрыть без ответа» in «Входящие» (the owner 2026-11-12): a waiting line is closed as 'ignored', nothing is sent to the point (VK,
«Рабочее место») or to the client, the alert «нет в CRM» goes; a repeat and a person without the right are refused; the command inbox-dismiss."""
from __future__ import annotations

import pytest

from panel_helpers import post
from test_roles import person
from test_vk_commands import hook, ready, to_client, to_points
from tires_sync import cli, order_alerts, order_manager as mgr, order_points as points, store


def _missing(tmp_path, monkeypatch):
    app, client, tok, conn, pid, card = ready(tmp_path)
    hook(app, conn, "приехал 4242")                                     # a number the panel does not know: a line and the yellow alert
    [item] = mgr.missing_orders(conn)
    monkeypatch.setattr(points, "add_note", lambda *a, **k: pytest.fail("«Рабочее место» must get nothing"))
    return app, client, tok, conn, item, len(to_points(app)), len(to_client(app))


def test_dismiss_closes_the_line_sends_nothing_and_the_alert_goes(tmp_path, monkeypatch):
    app, client, tok, conn, item, vk, avito = _missing(tmp_path, monkeypatch)
    assert any(r["id"] == "inbox-%d" % item["id"] for r in order_alerts.missing_rows(conn, "owner"))
    page = client.post("/orders/inbox/%d/dismiss" % item["id"], data={"csrf": tok}, follow_redirects=True).get_data(as_text=True)
    assert "Закрыто без ответа" in page
    status, answer, at = conn.execute("SELECT status, answer, answered_at FROM manager_message WHERE id = ?", (item["id"],)).fetchone()
    assert status == "ignored" and not answer and at
    assert len(to_points(app)) == vk and len(to_client(app)) == avito
    assert mgr.missing_orders(conn) == [] and order_alerts.missing_rows(conn, "owner") == []
    assert all(m["id"] != item["id"] for m in mgr.list_inbox(conn)) and mgr.new_count(conn) == 0
    assert conn.execute("SELECT author FROM audit WHERE action = 'inbox_dismiss'").fetchall() == [("owner",)]
    page = client.post("/orders/inbox/%d/dismiss" % item["id"], data={"csrf": tok}, follow_redirects=True).get_data(as_text=True)
    assert "уже закрыто" in page and conn.execute("SELECT COUNT(*) FROM audit WHERE action = 'inbox_dismiss'").fetchone()[0] == 1


def test_the_button_is_shown_on_a_waiting_line_only(tmp_path, monkeypatch):
    app, client, tok, conn, item, vk, avito = _missing(tmp_path, monkeypatch)
    page = client.get("/orders/inbox").get_data(as_text=True)
    assert 'action="/orders/inbox/%d/dismiss"' % item["id"] in page and "Точке ничего не уйдёт" in page


def test_an_answered_or_unknown_line_is_refused(tmp_path, monkeypatch):
    app, client, tok, conn, item, vk, avito = _missing(tmp_path, monkeypatch)
    mgr.answer(conn, item["id"], "Добавим", "2026-10-13T10:00:00")
    with pytest.raises(mgr.OrderError, match="уже ответили"):
        mgr.dismiss(conn, item["id"], "2026-10-13T11:00:00", "o")
    with pytest.raises(mgr.OrderError, match="нет"):
        mgr.dismiss(conn, 999, "2026-10-13T11:00:00", "o")
    assert conn.execute("SELECT status FROM manager_message WHERE id = ?", (item["id"],)).fetchone()[0] == "answered"


@pytest.mark.parametrize("role", ["viewer", "point"])
def test_a_person_without_the_right_to_answer_is_refused(tmp_path, monkeypatch, role):
    app, client, tok, conn, item, vk, avito = _missing(tmp_path, monkeypatch)
    pid = conn.execute("SELECT point_id FROM manager_message WHERE id = ?", (item["id"],)).fetchone()[0] if role == "point" else None
    other, _ = person(app, role, pid)
    assert other.post("/orders/inbox/%d/answer" % item["id"], data={"csrf": "tok", "text": "x"}).status_code == 403      # the same people
    assert other.post("/orders/inbox/%d/dismiss" % item["id"], data={"csrf": "tok"}).status_code == 403
    assert conn.execute("SELECT status FROM manager_message WHERE id = ?", (item["id"],)).fetchone()[0] == "new"


def test_the_command_dry_run_writes_nothing_and_without_it_closes(tmp_path, monkeypatch, capsys):
    app, client, tok, conn, item, vk, avito = _missing(tmp_path, monkeypatch)
    db = app.config["DB_PATH"]
    assert cli.main(["inbox-dismiss", "--db", db, "--id", str(item["id"]), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "4242" in out and "ничего не записано" in out
    fresh = store.connect(db)
    assert fresh.execute("SELECT status, answered_at FROM manager_message WHERE id = ?", (item["id"],)).fetchone() == ("new", None)
    assert fresh.execute("SELECT COUNT(*) FROM audit WHERE action = 'inbox_dismiss'").fetchone()[0] == 0
    assert cli.main(["inbox-dismiss", "--db", db, "--id", str(item["id"]), "--id", "999"]) == 1          # the unknown one is refused
    out = capsys.readouterr().out
    assert "закрыто без ответа" in out and "999: не закрыто" in out
    assert fresh.execute("SELECT status FROM manager_message WHERE id = ?", (item["id"],)).fetchone()[0] == "ignored"
    assert len(to_points(app)) == vk and len(to_client(app)) == avito
