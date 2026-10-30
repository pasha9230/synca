"""The tails of «Точки в панели» (the lead 2026-10-23): an alert on «Рабочее место» that nobody of the point opened in 15 minutes of working time
goes to the owner in Telegram, once; the answer to a binding code goes to VK even for a point in the panel. Fakes only."""
from __future__ import annotations

import json

from http_fakes import ScriptedHttp
from test_mypoint import manager
from test_order_notify import TG_OK, card, env, point
from test_points_panel import to_panel
from test_vk_commands import hook, ready, to_points
from tires_sync import order_manager as mgr, order_notify, order_points as points, orders, store

NOW = "2026-10-15T12:00:00"


def later(minutes):
    from datetime import datetime, timedelta
    return (datetime.fromisoformat(NOW) + timedelta(minutes=minutes)).isoformat(timespec="seconds")


def alert(conn, box, pid):
    store.set_state(conn, order_notify.UNSEEN_START_KEY, "2026-10-01T00:00:00")
    order = card(conn, point_id=pid)
    assert order_notify.deliver(conn, box, order, "Заказ №%d едет к вам" % order["id"], NOW).startswith("показано точке")
    return order


def test_an_alert_nobody_opened_in_15_working_minutes_goes_to_the_owner_once(tmp_path):
    conn, box = env(tmp_path)
    pid = point(conn, channel="panel")
    order = alert(conn, box, pid)
    tg = ScriptedHttp(TG_OK)
    assert order_notify.escalate_unseen(conn, box, later(14), tg_http=tg) == 0 and tg.calls == []
    assert order_notify.escalate_unseen(conn, box, later(15), tg_http=tg) == 1
    text = json.loads(tg.calls[0][3])["text"]
    assert json.loads(tg.calls[0][3])["chat_id"] == "1000" and "Точка «Гамма» не открыла оповещение по заказу №%d" % order["id"] in text
    assert "едет к вам" in text
    assert order_notify.escalate_unseen(conn, box, later(30), tg_http=tg) == 0 and len(tg.calls) == 1          # once
    [event] = [e for e in orders.list_events(conn, order["id"]) if "не открыла" in e["text"]]
    assert event["level"] == "yellow" and "владельцу написано в Telegram" in event["text"]


def test_the_working_hours_hold_the_clock(tmp_path):
    conn, box = env(tmp_path)
    mgr.set_duty(conn, {}, (10, 20), "", NOW[:10])
    pid = point(conn, channel="panel")
    store.set_state(conn, order_notify.UNSEEN_START_KEY, "2026-10-01T00:00:00")
    points.add_note(conn, pid, "alert", "Заказ едет", "2026-10-15T19:50:00", None)
    tg = ScriptedHttp(TG_OK)
    assert order_notify.escalate_unseen(conn, box, "2026-10-16T10:04:00", tg_http=tg) == 0
    assert order_notify.escalate_unseen(conn, box, "2026-10-16T10:05:00", tg_http=tg) == 1


def test_opening_my_point_by_its_person_puts_the_alerts_out(tmp_path):
    from panel_helpers import login, make_app
    from datetime import datetime
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 10, 15, 9, 5, 0))          # 12:05 Moscow
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, NOW)
    pid = points.save_point(conn, dict(name="Бета", channel="panel"))
    store.set_state(conn, order_notify.UNSEEN_START_KEY, "2026-10-01T00:00:00")
    points.add_note(conn, pid, "alert", "Заказ едет", NOW, None)
    owner = app.test_client()
    login(owner)
    owner.get("/my-point?p=%d" % pid)                                              # the owner looking is not the point looking
    assert len(points.unseen_alerts(conn, pid, "2026-10-01")) == 1
    assert manager(app, conn, pid).get("/my-point").status_code == 200
    assert points.unseen_alerts(conn, pid, "2026-10-01") == []
    assert points.seen_at(conn, pid) == NOW                                          # up to the newest note shown, not the clock
    points.mark_seen(conn, pid, "2026-10-01T00:00:00")
    assert points.seen_at(conn, pid) == NOW                                          # never back
    assert order_notify.escalate_unseen(conn, None, later(60), tg_http=ScriptedHttp()) == 0


def test_a_point_on_vk_and_notes_from_before_the_safety_net_are_left_alone(tmp_path):
    conn, box = env(tmp_path)
    pid = point(conn, channel="panel")
    points.add_note(conn, pid, "alert", "старое", NOW, None)
    tg = ScriptedHttp()
    assert order_notify.escalate_unseen(conn, box, later(60), tg_http=tg) == 0                 # the first look only marks the start
    assert order_notify.escalate_unseen(conn, box, later(61), tg_http=tg) == 0 and tg.calls == []
    vk_point = point(conn, channel="vk", peer="778", name="Купчино")
    points.add_note(conn, vk_point, "alert", "по ВК", later(62), None)
    assert order_notify.escalate_unseen(conn, box, later(120), tg_http=tg) == 0


def test_no_telegram_is_kept_in_the_journal_and_not_retried(tmp_path):
    conn, box = env(tmp_path)
    pid = point(conn, channel="panel")
    order = alert(conn, box, pid)
    tg = ScriptedHttp((500, {}))
    assert order_notify.escalate_unseen(conn, box, later(20), tg_http=tg) == 1
    assert order_notify.escalate_unseen(conn, box, later(40), tg_http=tg) == 0 and len(tg.calls) == 1
    assert any("владельцу написать не удалось" in e["text"] for e in orders.list_events(conn, order["id"]))


# ---- the binding code ------------------------------------------------------------------------------------------------------------------

def test_the_answer_to_a_binding_code_goes_to_vk_even_for_a_point_in_the_panel(tmp_path):
    app, client, tok, conn, pid, card_id = ready(tmp_path)
    conn.execute("UPDATE point SET vk_peer_id = '' WHERE id = ?", (pid,))
    to_panel(conn, pid)
    code = points.new_bind_code(conn, pid)
    hook(app, conn, code, sender="999")
    [(peer, answer)] = to_points(app)
    assert peer == "999" and answer.startswith("(бот) ВК привязан к точке «Бета»")
    assert points.get_point(conn, pid)["channel"] == "panel"
    assert [n["text"][:16] for n in points.notes(conn, pid)] == ["ВК привязан к то"]
    hook(app, conn, "привет", event="e2", sender="999")                                         # anything else: still nothing in VK
    assert len(to_points(app)) == 1
