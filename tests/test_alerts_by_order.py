"""«Уведомления» of the office (п. 31 of the audit, 2026-11-08): only what needs an action (red, yellow) and one row per order with all its
troubles; the menu counter = the rows of the page; «Разобрался» of a row takes its red records; nothing is closed in the data."""
from __future__ import annotations

import re

from test_alerts_page import card, counter, event, setup
from tires_sync import order_alerts as alerts, store, users

from panel_helpers import post


def row_ids(html, order_id):
    """The ids the form «Разобрался» of an order's row sends: the red records the page showed."""
    form = re.search(r'<form method="post" action="/alerts/order/%d/ack"[^>]*>(.*?)</form>' % order_id, html, re.S)
    return re.findall(r'name="seen" value="(\d+)"', form.group(1)) if form else []


def rows_of(html):
    return re.findall(r'<td class="nowrap"><a href="([^"]+)">', html)


def test_one_row_per_order_red_first_and_no_green(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    a, b, c = card(conn, "Анна"), card(conn, "Борис"), card(conn, "Вера")
    event(conn, a, "note", "Закупка выросла", level="yellow")
    red = event(conn, a, "note", "Сообщение клиенту не ушло", level="red")
    event(conn, a, "note", "Клиенту отправлено", level="green")
    event(conn, b, "note", "Срок отдать на возврат", level="yellow")
    event(conn, c, "note", "Клиенту отправлено «можно забирать»", level="green")      # only green: no row at all
    html = client.get("/alerts").get_data(as_text=True)
    assert rows_of(html) == ["/orders/%d" % b, "/orders/%d" % a] or rows_of(html) == ["/orders/%d" % a, "/orders/%d" % b]
    assert "Сообщение клиенту не ушло · Закупка выросла" in html                       # red before yellow in the one row
    assert "Клиенту отправлено" not in html
    assert counter(html) == (2, True)                                                 # the menu = the rows of the page
    feed = client.get("/alerts/feed").get_json()
    assert feed["count"] == 2 and feed["red"] and {i["order_id"] for i in feed["items"]} == {a, b}
    assert {e["text"] for e in alerts.open_alerts(conn, "owner") if e["order_id"] == a} >= {"Закупка выросла", "Сообщение клиенту не ушло", "Клиенту отправлено"}   # the data: untouched
    assert html.count('action="/alerts/order/%d/ack"' % a) == 1 and ('action="/alerts/%d/ack"' % red) not in html   # the row's «Разобрался»


def test_the_ack_of_a_row_takes_all_its_red_records_and_only_the_persons(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    a, b = card(conn, "Анна"), card(conn, "Борис")
    r1 = event(conn, a, "note", "сбой 1", level="red")
    r2 = event(conn, a, "note", "сбой 2", level="red")
    y = event(conn, a, "note", "проверьте", level="yellow")
    other = event(conn, b, "note", "сбой Бориса", level="red")
    html = client.get("/alerts").get_data(as_text=True)
    assert html.count('action="/alerts/order/%d/ack"' % a) == 1 and counter(html) == (2, True)
    assert client.post("/alerts/order/%d/ack" % a).status_code == 400                    # no CSRF
    assert sorted(row_ids(html, a)) == sorted([str(r1), str(r2)])
    post(client, "/alerts/order/%d/ack" % a, tok, seen=row_ids(html, a))
    left = {e["id"] for e in alerts.open_alerts(conn, "owner")}
    assert r1 not in left and r2 not in left and {y, other} <= left                      # the yellow waits for the order to be opened
    html = client.get("/alerts").get_data(as_text=True)
    assert counter(html) == (2, True) and "проверьте" in html and "сбой 1" not in html
    users.add_user(conn, "zaman", "password-123", "2026-10-20T12:00:00", role="partner")
    assert {e["id"] for e in alerts.open_alerts(conn, "zaman")} >= {r1, r2}            # «Разобрался» is the person's own


def test_accept_all_counts_the_rows_and_takes_their_records(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    a = card(conn, "Анна")
    event(conn, a, "note", "сбой", level="red")
    event(conn, a, "note", "проверьте", level="yellow")
    html = client.get("/alerts?accept=1").get_data(as_text=True)
    assert "Да, принять 1 уведомление" in html and html.count('name="ids"') == 2
    ids = re.findall(r'name="ids" value="(\d+)"', html)
    post(client, "/alerts/accept-all", tok, ids=ids)
    with client.session_transaction() as s:
        assert [m for _, m in s.get("_flashes", [])] == ["Принято уведомлений: 1."]    # the rows, as the button said
    assert counter(client.get("/alerts").get_data(as_text=True)) == (0, False)


def test_a_handed_over_client_in_the_row_of_its_order_keeps_its_buttons(tmp_path):
    from tires_sync import order_manager as mgr
    app, client, tok, conn = setup(tmp_path)
    a = card(conn, "Анна")
    hand = event(conn, a, "handoff", mgr.ASK + " Иван", level="yellow")
    red = event(conn, a, "note", "сбой", level="red")                                # newer: the row's id is not the handoff's
    html = client.get("/alerts").get_data(as_text=True)
    assert counter(html) == (1, True)
    assert 'href="/alerts/%d/go"' % hand in html and 'action="/alerts/%d/take"' % hand in html
    assert 'action="/alerts/%d/take"' % red not in html and html.count('action="/alerts/order/%d/ack"' % a) == 1
    assert row_ids(html, a) == [str(red)]                                            # the handoff is never in «Разобрался»
    post(client, "/alerts/order/%d/ack" % a, tok, seen=row_ids(html, a))
    assert {e["id"] for e in alerts.open_alerts(conn, "owner")} >= {hand} and red not in {e["id"] for e in alerts.open_alerts(conn, "owner")}


def test_a_point_keeps_its_own_rows(tmp_path):
    """The manager of a point: its own list (order_alerts.point_row, the owner 2026-11-07), not grouped nor cut here (п. 31 is the office's)."""
    from tires_sync import order_manager as mgr, order_points as points
    app, client, tok, conn = setup(tmp_path)
    pid = points.save_point(conn, {"name": "Юнона", "manager_name": "А", "channel": "panel"})
    a = card(conn, "Анна")
    conn.execute("UPDATE order_card SET point_id = ? WHERE id = ?", (pid, a))
    one = event(conn, a, "point", mgr.ARRIVAL % (a, "Юнона", "отправлено"), level="yellow")
    two = event(conn, a, "point", mgr.ARRIVAL % (a, "Юнона", "ещё раз"), level="yellow")
    users.add_user(conn, "yun", "password-123", "2026-10-20T12:00:00", role="point", point_id=pid)
    pt = app.test_client()
    with pt.session_transaction() as s:
        s["user"], s["epoch"], s["csrf"] = "yun", store.get_session_epoch(conn, "yun"), "t"
    feed = pt.get("/alerts/feed").get_json()
    assert sorted(i["id"] for i in feed["items"]) == sorted([one, two]) and feed["count"] == 2
    assert "/alerts/order/" not in pt.get("/alerts").get_data(as_text=True)
    assert pt.post("/alerts/order/%d/ack" % a, data={"csrf": "t"}).status_code == 403


def test_the_ack_of_a_row_leaves_a_red_record_that_came_after_the_page(tmp_path):
    """The review 09.11: «Разобрался» of an order's row takes the red records the page showed, never one that came after it was loaded."""
    app, client, tok, conn = setup(tmp_path)
    a = card(conn, "Анна")
    seen = event(conn, a, "note", "сбой 1", level="red")
    html = client.get("/alerts").get_data(as_text=True)
    ids = re.findall(r'<form method="post" action="/alerts/order/%d/ack"[^>]*>(.*?)</form>' % a, html, re.S)
    assert ids, "no form of the row"
    shown = re.findall(r'name="seen" value="(\d+)"', ids[0])
    assert shown == [str(seen)]
    late = event(conn, a, "note", "сбой 2", level="red")                              # after the page was loaded
    post(client, "/alerts/order/%d/ack" % a, tok, seen=shown)
    left = {e["id"] for e in alerts.open_alerts(conn, "owner")}
    assert seen not in left and late in left
    post(client, "/alerts/order/%d/ack" % a, tok)                                      # no ids: nothing is taken
    assert late in {e["id"] for e in alerts.open_alerts(conn, "owner")}



def test_a_long_row_shows_three_texts_and_the_rest_as_more(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    a = card(conn, "Анна")
    for i in range(5):
        event(conn, a, "note", "проверьте %d" % i, level="yellow")
    item = client.get("/alerts/feed").get_json()["items"][0]
    assert item["text"].count(" · ") == 2 and item["more"] == 2
    assert "ещё 2" in client.get("/alerts").get_data(as_text=True)
