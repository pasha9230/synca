"""The bell in the header: the same count as the menu, the feed of the latest rows, nothing is written."""
from __future__ import annotations

import re

from test_alerts_page import as_user, card, counter, event, setup
from tires_sync import order_alerts as alerts


def bell_count(html):
    found = re.search(r'<span class="bell-count" id="bell-count"( hidden)?>(\d*)</span>', html)
    assert found, "no bell"
    return 0 if found.group(1) else int(found.group(2))


def test_the_bell_is_on_every_page_with_the_menu_number(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    cid = card(conn, "Ольга")
    event(conn, cid, "notify", "Оповещение не отправлено: нет ВК")
    for url in ("/", "/alerts", "/orders", "/chats"):
        html = client.get(url).get_data(as_text=True)
        assert 'id="bell-btn"' in html and "bell.js" in html and "bell.css" in html, url
        assert bell_count(html) == counter(html)[0] == 2, url


def test_no_alerts_no_number_and_no_bell_when_logged_out(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    assert bell_count(client.get("/").get_data(as_text=True)) == 0
    out = app.test_client().get("/login").get_data(as_text=True)
    assert "bell-btn" not in out and "bell.js" not in out


def test_the_feed_needs_a_login(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    assert app.test_client().get("/alerts/feed").status_code == 302


def test_the_feed_is_the_page_rows_folded_and_limited(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    cid = card(conn, "Ольга")                                           # «Заказ создан» (green)
    for i in range(3):
        event(conn, cid, "chat", "Клиент написал %d" % i, level="green")  # folds into one row
    red = event(conn, cid, "notify", "Оповещение не отправлено: нет ВК")
    data = client.get("/alerts/feed").get_json()
    page = client.get("/alerts").get_data(as_text=True)
    assert data["count"] == counter(client.get("/").get_data(as_text=True))[0] == page.count("<tr>") - 1
    assert data["red"] is True
    assert {"id", "order_id", "level", "client", "text", "more", "at"} <= set(data["items"][0])
    assert all(i["order_id"] == cid and i["client"] == "Ольга" for i in data["items"])
    assert any(i["id"] == red and i["level"] == "red" for i in data["items"])
    assert any(i["more"] == 2 for i in data["items"])
    for i in range(15):
        event(conn, card(conn, "К%d" % i), "reserve", "Оповещение о конце резерва")
    data = client.get("/alerts/feed").get_json()
    assert len(data["items"]) == 10 and data["count"] > 10


def test_the_feed_hides_own_non_red_and_other_users_state(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    cid = card(conn)
    event(conn, cid, "reserve", "Оповещение о конце резерва")
    worker2 = as_user(app, conn, "worker2")
    assert worker2.get("/alerts/feed").get_json()["count"] == client.get("/alerts/feed").get_json()["count"]
    client.get("/orders/%d" % cid)                                      # owner opened it: yellow is out for owner only
    assert client.get("/alerts/feed").get_json()["count"] < worker2.get("/alerts/feed").get_json()["count"]


def test_the_feed_writes_nothing(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    cid = card(conn)
    event(conn, cid, "reserve", "Оповещение о конце резерва")
    before = alerts.open_alerts(conn, "owner")
    n = conn.execute("SELECT COUNT(*) FROM order_event_ack").fetchone()[0]
    client.get("/alerts/feed")
    client.get("/alerts/feed")
    assert alerts.open_alerts(conn, "owner") == before
    assert conn.execute("SELECT COUNT(*) FROM order_event_ack").fetchone()[0] == n


def test_the_static_files_are_served_under_the_csp(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    for name in ("bell.js", "bell.css"):
        r = client.get("/static/" + name)
        assert r.status_code == 200
    csp = client.get("/").headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]
