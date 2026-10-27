"""«Принять все» on «Уведомления» (the owner 2026-10-27): the current user's own rows of order records only; red = «Разобрался», yellow and
green = seen; what the user does not see on the page at that moment, other users' and points' rows, and chat-based rows stay."""
from __future__ import annotations

import re
from datetime import datetime

from panel_helpers import login, make_app, post
from tires_sync import order_alerts as alerts, order_points as points, orders, store, users

NOW = "2026-10-20T12:00:00"


def setup(tmp_path):
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 10, 20, 9, 0, 0))
    client = app.test_client()
    tok = login(client)
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, NOW)
    conn.commit()
    return app, client, tok, conn


def card(conn, name="Иван"):
    cid = orders.create_card(conn, {"client_name": name}, NOW, "система")
    conn.commit()
    return cid


def event(conn, cid, kind, text, level=""):
    orders.add_event(conn, cid, NOW, "система", kind, text, level=level)
    conn.commit()
    return conn.execute("SELECT MAX(id) FROM order_event").fetchone()[0]


def three(conn):
    """A red, a yellow and a green record on three orders (the cards' own «Заказ создан» are green too)."""
    a, b = card(conn, "Ольга"), card(conn, "Пётр")
    return event(conn, a, "notify", "Оповещение не отправлено: нет ВК"), event(conn, b, "reserve", "Оповещение о конце резерва"), a, b


def shown(client):
    """The ids the page asks to accept (the hidden fields of the confirming form)."""
    html = client.get("/alerts?accept=1").get_data(as_text=True)
    return sorted(int(i) for i in re.findall(r'name="ids" value="(\d+)"', html)), html


def other_user(app, conn, name="worker2", **kw):
    users.add_user(conn, name, "password-123", NOW, **kw)
    conn.commit()
    c = app.test_client()
    with c.session_transaction() as s:
        s["user"], s["epoch"], s["csrf"] = name, store.get_session_epoch(conn, name), "tok2"
    return c


def open_ids(conn, name="owner"):
    return sorted(e["id"] for e in alerts.open_alerts(conn, name))


def test_the_button_asks_on_the_page_and_the_second_button_names_the_count(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    three(conn)
    page = client.get("/alerts").get_data(as_text=True)
    assert "Принять все" in page and "Да, принять" not in page and 'name="ids"' not in page
    ids, html = shown(client)
    assert "Да, принять %d уведомл" % len(ids) in html and len(ids) == 4        # red + yellow + two green «Заказ создан»
    assert 'action="/alerts/accept-all"' in html and "confirm(" not in html
    assert alerts.open_alerts(conn, "owner")                                     # the question changed nothing


def test_red_is_acked_and_yellow_green_are_seen_for_this_user_only(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    red, yellow, a, b = three(conn)
    worker2 = other_user(app, conn)
    before = open_ids(conn, "worker2")
    ids, _ = shown(client)
    r = client.post("/alerts/accept-all", data={"csrf": tok, "ids": ids})
    assert r.status_code == 302 and r.headers["Location"].endswith("/alerts")
    assert open_ids(conn, "owner") == []
    assert conn.execute("SELECT login, author FROM order_event_ack WHERE event_id = ?", (red,)).fetchall() == [("owner", "owner")]   # as the single press
    assert open_ids(conn, "worker2") == before                                    # the other user keeps everything, red included
    assert "Новых уведомлений нет" in client.get("/alerts").get_data(as_text=True)
    assert worker2.get("/alerts").get_data(as_text=True).count("<tr>") >= 4


def test_a_new_alert_after_loading_the_page_stays(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    red, yellow, a, b = three(conn)
    ids, _ = shown(client)                                                       # the question is on the screen ...
    late_red = event(conn, a, "notify", "Оповещение не отправлено: опоздало")   # ... a record comes ...
    late_yellow = event(conn, b, "reserve", "Ещё одно оповещение о резерве")    # ... on an order with an accepted row
    client.post("/alerts/accept-all", data={"csrf": tok, "ids": ids})
    assert open_ids(conn) == sorted([late_red, late_yellow])


def test_rows_of_other_users_and_ids_not_on_their_page_are_untouched(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    red, yellow, a, b = three(conn)
    own = event(conn, a, "notify", "Оповещение не отправлено: моё", level="red")
    conn.execute("UPDATE order_event SET author = 'owner' WHERE id = ?", (own,))
    grey = event(conn, a, "notify", "Оповещение отправлено: Telegram")           # grey: no alert
    conn.commit()
    ids, _ = shown(client)
    client.post("/alerts/accept-all", data={"csrf": tok, "ids": ids + [grey, 999999, "x"]})
    assert conn.execute("SELECT COUNT(*) FROM order_event_ack WHERE event_id IN (?, ?)", (grey, 999999)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM order_event_ack WHERE login != 'owner'").fetchone()[0] == 0
    assert open_ids(conn) == []


def test_rows_of_a_point_stay_for_a_point_manager_and_a_point_user_accepts_only_its_own(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    mine = points.save_point(conn, {"name": "Юнона", "manager_name": "А", "channel": "panel"})
    foreign = points.save_point(conn, {"name": "Руставели", "manager_name": "Б", "channel": "panel"})
    a, b = card(conn, "Свой"), card(conn, "Чужой")
    conn.execute("UPDATE order_card SET point_id = ? WHERE id = ?", (mine, a))
    conn.execute("UPDATE order_card SET point_id = ? WHERE id = ?", (foreign, b))
    ra = event(conn, a, "notify", "Оповещение не отправлено: свой")
    rb = event(conn, b, "notify", "Оповещение не отправлено: чужой")
    conn.commit()
    pt = other_user(app, conn, "yun", role="point", point_id=mine)
    # the staff's page also holds the orders of both: a point's ids posted by a point manager are checked against that manager's own page
    r = pt.post("/alerts/accept-all", data={"csrf": "tok2", "ids": [ra, rb]})
    assert r.status_code == 302
    assert ra not in open_ids(conn, "yun") and rb in open_ids(conn, "owner") and ra in open_ids(conn, "owner")
    assert conn.execute("SELECT COUNT(*) FROM order_event_ack WHERE event_id = ? AND login = 'yun'", (rb,)).fetchone()[0] == 0


def test_chat_based_rows_and_handoffs_are_left_out(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    red, yellow, a, b = three(conn)
    from tires_sync import order_manager as mgr
    hand = event(conn, a, "handoff", mgr.ASK + " Иван", level="yellow")
    note = event(conn, b, "chat", "Клиент написал после закрытия: привет")                      # a record of the order journal: seen as by opening the order
    assert hand in [e["id"] for e in alerts.open_alerts(conn, "owner")]            # it is an alert ...
    ids, _ = shown(client)
    assert hand not in ids and note in ids                                       # ... that this button leaves alone
    client.post("/alerts/accept-all", data={"csrf": tok, "ids": ids})
    assert open_ids(conn) == [hand]                                              # «Взять в работу» stays its own button


def test_csrf_login_and_rights(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path)
    three(conn)
    ids, _ = shown(client)
    assert client.post("/alerts/accept-all", data={"ids": ids}).status_code == 400            # no CSRF token
    assert app.test_client().post("/alerts/accept-all", data={"ids": ids}).status_code in (302, 400)
    assert open_ids(conn)
    monkeypatch.setitem(users.MATRIX["viewer"]["sections"], "alerts", users.VIEW)           # a role that only looks
    viewer = other_user(app, conn, "obs", role="viewer")
    assert viewer.post("/alerts/accept-all", data={"csrf": "tok2", "ids": ids}).status_code == 403
    assert open_ids(conn, "obs")


def test_nothing_to_accept_does_not_fail(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    r = client.post("/alerts/accept-all", data={"csrf": tok})
    assert r.status_code == 302 and r.headers["Location"].endswith("/alerts")


def test_a_handoff_below_an_accepted_green_row_is_not_swallowed(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    from tires_sync import order_manager as mgr
    a = card(conn)
    hand = event(conn, a, "handoff", mgr.ASK + " Иван", level="yellow")
    later = event(conn, a, "stage", "Этап изменён", level="green")                # a newer, acceptable record of the same order
    ids, _ = shown(client)
    assert later in ids and hand not in ids
    client.post("/alerts/accept-all", data={"csrf": tok, "ids": ids})
    assert hand in open_ids(conn)                                                 # the handed-over client still waits for «Взять в работу»


def test_a_bad_id_in_the_form_is_ignored(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    three(conn)
    r = client.post("/alerts/accept-all", data={"csrf": tok, "ids": ["²", "①", "-1", ""]})
    assert r.status_code == 302 and open_ids(conn)
