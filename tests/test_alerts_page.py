"""The «Уведомления» page, its menu counter, «Разобрался» and «seen» when an order is opened."""
from __future__ import annotations

import re
from datetime import datetime

from panel_helpers import login, make_app, post
from tires_sync import order_alerts as alerts, orders, store, users

NOW = "2026-10-20T12:00:00"


def setup(tmp_path):
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 10, 20, 9, 0, 0))
    client = app.test_client()
    tok = login(client)
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, NOW)
    conn.commit()
    return app, client, tok, conn


def card(conn, name="Иван", by="система"):
    cid = orders.create_card(conn, {"client_name": name}, NOW, by)
    conn.commit()
    return cid


def event(conn, cid, kind, text, level=""):
    orders.add_event(conn, cid, NOW, "система", kind, text, level=level)
    conn.commit()
    return conn.execute("SELECT MAX(id) FROM order_event").fetchone()[0]


def counter(html):
    found = re.search(r'href="/alerts">.*?Уведомления(?:<span class="sb-count([^"]*)">(\d+)</span>)?</a>', html)
    assert found, "no menu entry"
    return int(found.group(2) or 0), "sb-count--red" in (found.group(1) or "")


def as_user(app, conn, login_name):
    users.add_user(conn, login_name, "password-123", NOW)
    conn.commit()
    client = app.test_client()
    with client.session_transaction() as session:
        session["user"] = login_name
        session["epoch"] = store.get_session_epoch(conn, login_name)
        session["csrf"] = "tok2"
    return client


def test_the_page_and_ack_need_a_login_and_csrf(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    cid = card(conn)
    red = event(conn, cid, "note", "сбой", level="red")
    assert app.test_client().get("/alerts").status_code == 302
    assert app.test_client().post("/alerts/%d/ack" % red).status_code in (302, 400)
    assert client.post("/alerts/%d/ack" % red).status_code == 400                     # no CSRF token
    assert [e["id"] for e in alerts.open_alerts(conn, "owner") if e["level"] == "red"] == [red]


def test_the_page_lists_coloured_alerts_and_the_menu_counts_them(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    cid = card(conn, "Ольга")                                  # «Заказ создан» is green, but no row (a stage: order_alerts.bell_row)
    event(conn, cid, "notify", "Оповещение не отправлено: у точки нет ВК")
    event(conn, cid, "reserve", "Оповещение о конце резерва")
    event(conn, cid, "sale", "Продажа проведена: 5 000 ₽")                     # green: no action needed, no row (п. 31, 2026-11-08)
    event(conn, cid, "notify", "Оповещение отправлено: Telegram")        # grey: never an alert
    other = card(conn, "Пётр")
    event(conn, other, "reserve", "Резерв кончается")
    html = client.get("/alerts").get_data(as_text=True)
    assert "Оповещение не отправлено: у точки нет ВК · Оповещение о конце резерва" in html and "Продажа проведена" not in html
    assert "Заказ создан" not in html
    assert "Оповещение отправлено: Telegram" not in html
    assert 'href="/orders/%d"' % cid in html and "Ольга" in html
    assert "badge--err" in html and "badge--warn" in html and "badge--ok" not in html
    assert html.count("Разобрался</button>") == 1
    assert counter(client.get("/").get_data(as_text=True)) == (2, True)             # one row per order


def test_an_empty_page_and_no_counter(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    assert "Новых уведомлений нет" in client.get("/alerts").get_data(as_text=True)
    assert counter(client.get("/").get_data(as_text=True)) == (0, False)


def test_opening_the_order_puts_out_all_but_red(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    cid = card(conn)
    event(conn, cid, "notify", "Оповещение не отправлено: нет ВК")
    event(conn, cid, "reserve", "Оповещение о конце резерва")
    assert client.get("/orders/%d" % cid).status_code == 200
    left = alerts.open_alerts(conn, "owner")
    assert [e["level"] for e in left] == ["red"]
    assert counter(client.get("/").get_data(as_text=True)) == (1, True)
    event(conn, cid, "reserve", "Резерв снова кончается")                # a record after the visit is a new alert: in the order's row
    assert counter(client.get("/").get_data(as_text=True)) == (1, True)
    assert "Оповещение не отправлено: нет ВК · Резерв снова кончается" in client.get("/alerts").get_data(as_text=True)


def test_ack_closes_a_red_alert_for_this_user_only(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    cid = card(conn)
    red = event(conn, cid, "notify", "Оповещение не отправлено: нет ВК")
    worker2 = as_user(app, conn, "worker2")
    assert post(client, "/alerts/%d/ack" % red, tok).status_code == 302
    assert "Оповещение не отправлено" not in client.get("/alerts").get_data(as_text=True)
    assert "Оповещение не отправлено" in worker2.get("/alerts").get_data(as_text=True)
    assert worker2.get("/orders/%d" % cid).status_code == 200                # opening the order does not put out red
    assert [e["id"] for e in alerts.open_alerts(conn, "worker2")] == [red]
    assert alerts.open_alerts(conn, "owner") and all(e["level"] != "red" for e in alerts.open_alerts(conn, "owner"))  # worker2's visit is not owner's


def test_ack_on_a_record_that_is_not_red_is_refused(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    cid = card(conn)
    yellow = event(conn, cid, "reserve", "Оповещение о конце резерва")
    r = post(client, "/alerts/%d/ack" % yellow, tok)
    assert r.status_code == 302 and r.headers["Location"].endswith("/alerts")
    assert "только на красную" in client.get("/alerts").get_data(as_text=True)
    assert conn.execute("SELECT COUNT(*) FROM order_event_ack").fetchone()[0] == 0
    post(client, "/alerts/999999/ack", tok)
    assert conn.execute("SELECT COUNT(*) FROM order_event_ack").fetchone()[0] == 0


def test_client_messages_are_one_alert_per_order(tmp_path):
    """The folding is of all_rows; page_rows (the page, the bell, the dots) keeps of the chat records the red only (2026-11-05, 2026-11-09)."""
    app, client, tok, conn = setup(tmp_path)
    cid = card(conn)
    other = card(conn, "Пётр")
    conn.execute("UPDATE order_event SET level = 'grey'")                       # only the chat records below matter here
    conn.commit()
    event(conn, cid, "chat", "Клиент написал после закрытия сделки (1)")
    event(conn, cid, "chat", "Клиент написал после закрытия сделки (2)")
    event(conn, cid, "chat", "Отправлен ответ клиенту: да", level="green")
    event(conn, other, "chat", "Клиент написал после закрытия сделки (3)")
    red = event(conn, cid, "chat", "Позиция не добавлена: нет кода")             # red stays on its own
    assert [r["id"] for r in alerts.page_rows(conn, "owner", NOW)] == [red]
    rows = alerts.all_rows(conn, "owner", NOW)
    folded = [r for r in rows if r["text"].startswith("Отправлен ответ клиенту")]
    assert [(r["more"], r["level"]) for r in folded] == [(2, "yellow")]         # the most important colour of the folded records
    assert [r["text"] for r in rows if r["order_id"] == other] == ["Клиент написал после закрытия сделки (3)"]
    assert [r["id"] for r in rows if r["level"] == "red"] == [red]
    html = client.get("/alerts").get_data(as_text=True)                         # the page: the red failure only (2026-11-05)
    assert "Позиция не добавлена" in html and "Отправлен ответ клиенту" not in html and "Клиент написал" not in html
    assert counter(client.get("/").get_data(as_text=True)) == (1, True)
    client.get("/orders/%d" % cid)
    assert [e["id"] for e in alerts.open_alerts(conn, "owner", cid)] == [red]


def test_the_users_own_records_are_not_alerts_for_them_except_red(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    cid = card(conn, by="owner")                                              # «Заказ создан» by owner
    orders.add_event(conn, cid, NOW, "owner", "chat", "Отправлен ответ клиенту: да", level="green")
    orders.add_event(conn, cid, NOW, "owner", "reserve", "Резерв кончается", level="yellow")
    orders.add_event(conn, cid, NOW, "owner", "note", "сбой", level="red")
    conn.commit()
    worker2 = as_user(app, conn, "worker2")
    html = client.get("/alerts").get_data(as_text=True)                       # one's own failure stays until «Разобрался»
    assert "сбой" in html and "Отправлен ответ клиенту" not in html and "Резерв кончается" not in html and "Заказ создан" not in html
    assert counter(client.get("/").get_data(as_text=True)) == (1, True)
    assert counter(worker2.get("/").get_data(as_text=True)) == (0, False)        # not theirs: the owner answers for it (2026-11-03)
    conn.execute("UPDATE order_card SET responsible = 'worker2' WHERE id = ?", (cid,))
    conn.commit()
    html = worker2.get("/alerts").get_data(as_text=True)                        # somebody else's records are alerts
    assert "Резерв кончается" in html and "сбой" in html                       # a chat and a stage are no rows (2026-11-05)
    assert "Отправлен ответ клиенту" not in html and "Заказ создан" not in html
    assert counter(worker2.get("/").get_data(as_text=True)) == (1, True)             # one row per order (п. 31)


def test_a_broken_counter_does_not_break_the_pages(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path)
    cid = card(conn)

    def broken(*args, **kwargs):
        raise RuntimeError("boom")
    monkeypatch.setattr(alerts, "open_alerts", broken)
    r = client.get("/")
    assert r.status_code == 200 and counter(r.get_data(as_text=True)) == (0, False)
    assert client.get("/orders/%d" % cid).status_code == 200


def test_a_record_after_the_shown_ones_stays_unseen(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path)
    cid = card(conn)
    shown = orders.list_events

    def list_then_write(c, card_id):                                         # a record comes between showing the journal and «seen»
        events = shown(c, card_id)
        orders.add_event(c, card_id, NOW, "система", "reserve", "Пришло позже")
        c.commit()
        return events
    monkeypatch.setattr(orders, "list_events", list_then_write)
    assert client.get("/orders/%d" % cid).status_code == 200
    assert [e["text"] for e in alerts.open_alerts(conn, "owner")] == ["Пришло позже"]
