"""In the panel: «Срочно ответить» (a client waits longer than an hour of working time), «Перезвонить» (the form, the reminder, «Сделано»),
the status of the inquiry in «Чаты» / «Звонки» / the client card, and «в работе» when the staff open a chat or a card."""
from __future__ import annotations

import re
from datetime import datetime

import pytest

from panel_helpers import login, make_app, post
from test_inquiry import at, call, chat, message
from tires_sync import inquiry, order_alerts as alerts, order_manager as mgr, orders, store, users

NOW = "2026-10-23T12:00:00"                                       # Moscow; the panel clock below is UTC


def setup(tmp_path, utc=datetime(2026, 10, 23, 9, 0, 0)):
    app = make_app(tmp_path, NOW=lambda: utc)
    client = app.test_client()
    tok = login(client)
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, "2026-10-01T10:00:00")
    conn.execute("INSERT INTO account (id, name, feed_token, created, chats_enabled) VALUES (1, 'Альфа', 't1', '2026-10-01', 1), "
                 "(2, 'Бета', 't2', '2026-10-01', 1)")
    store.set_state(conn, alerts.URGENT_START_KEY, "2026-10-01T00:00:00")
    conn.commit()
    return app, client, tok, conn


def urgent(conn, now=NOW):
    return [r for r in alerts.page_rows(conn, "owner", now) if r.get("urgent")]


# ---- «Срочно ответить» -----------------------------------------------------------------------------------------------------------------

def test_a_client_waiting_an_hour_is_urgent_and_one_row_per_chat(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    h = chat(conn)
    message(conn, h, at(-70), text="Есть 205/55 R16?")
    message(conn, h, at(-30), text="Ау")
    conn.execute("UPDATE avito_chat SET unread = 2 WHERE id = ?", (h,))
    conn.commit()
    [row] = urgent(conn)
    assert row["level"] == "red" and row["text"] == "Срочно ответить: ждёт с 10:50: Есть 205/55 R16?" and row["chat_row"] == h
    assert [r["id"] for r in alerts.page_rows(conn, "owner", NOW) if r.get("chat_row") == h] == [row["id"]]   # not «Клиент написал» as well
    assert urgent(conn, at(-11)) == []                                           # 59 minutes: not yet


def test_working_hours_count_the_hour(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    mgr.set_duty(conn, {}, (10, 20), "", NOW[:10])
    h = chat(conn)
    message(conn, h, "2026-10-22T19:30:00")                                       # 30 minutes in the evening, 30 in the morning
    assert urgent(conn, "2026-10-23T10:29:00") == [] and len(urgent(conn, "2026-10-23T10:30:00")) == 1


def test_an_answer_or_no_reply_or_closing_puts_it_out(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    h = chat(conn)
    message(conn, h, at(-70))
    message(conn, h, at(-1), direction="out", text="Есть")
    assert urgent(conn) == []
    message(conn, h, at(-65), text="А цена?")                                     # an older timestamp than our answer: answered
    assert urgent(conn) == []
    other = chat(conn, peer="p2")
    message(conn, other, at(-90))
    page = client.get("/alerts").get_data(as_text=True).split("<tbody>")[1]
    assert "Срочно ответить" in page and 'action="/chats/%d/status"' % other in page and "Разобрался" not in page
    r = post(client, "/chats/%d/status" % other, tok, status="no_reply", back="alerts")
    assert r.status_code == 302 and r.headers["Location"].endswith("/alerts")
    assert urgent(conn) == []
    chats_of = lambda: [r for r in alerts.page_rows(conn, "owner", NOW) if r.get("chat_row") == other]     # noqa: E731
    assert chats_of() == []


def test_point_accounts_closed_chats_and_waits_from_before_the_release_are_not_urgent(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    conn.execute("INSERT INTO point (name, avito_account_id) VALUES ('Бета', 2)")
    message(conn, chat(conn, account=2), at(-90))
    closed = chat(conn, peer="p2")
    message(conn, closed, at(-90))
    conn.execute("UPDATE avito_chat SET status = 'closed' WHERE id = ?", (closed,))
    store.set_state(conn, alerts.URGENT_START_KEY, at(-80))
    message(conn, chat(conn, peer="p3"), at(-90))                                 # waited from before the release
    assert urgent(conn) == []
    store.set_state(conn, alerts.URGENT_START_KEY, "")
    message(conn, chat(conn, peer="p4"), at(-90))
    assert urgent(conn) == []                                                    # the look of every minute has not started it yet


def test_the_bell_shows_urgent_and_callback_rows(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    message(conn, chat(conn), at(-90))
    inquiry.add_callback(conn, at(-5)[:16], "", at(-30), "owner", chat_row=chat(conn, peer="p9", name="Олег"))
    feed = client.get("/alerts/feed").get_json()
    titles = sorted(i["title"] for i in feed["items"])
    assert feed["red"] is True and titles == ["Чат", "Чат"] and any(i["text"].startswith("Перезвонить") for i in feed["items"])


# ---- «Перезвонить» -------------------------------------------------------------------------------------------------------------------

def test_the_callback_form_of_an_order_and_done_from_the_alerts(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    card_id = orders.create_card(conn, {"client_name": "Иван", "client_phone": "+79001112233"}, NOW, "owner")
    conn.commit()
    page = client.get("/orders/%d" % card_id).get_data(as_text=True)
    assert 'action="/callbacks/new"' in page and 'name="order_id" value="%d"' % card_id in page
    r = post(client, "/callbacks/new", tok, order_id=str(card_id), date="2026-10-23", time="12:30", note="Подумает", back="/orders/%d" % card_id)
    assert r.status_code == 302 and r.headers["Location"].endswith("/orders/%d" % card_id)
    [task] = inquiry.open_callbacks(conn, order_id=card_id)
    assert task["assignee"] == "owner" and task["note"] == "Подумает"
    assert "Подумает" in client.get("/orders/%d" % card_id).get_data(as_text=True)
    assert "Перезвонить" not in client.get("/alerts").get_data(as_text=True).split("<tbody>")[1]           # not due yet
    app.config["NOW"] = lambda: datetime(2026, 10, 23, 9, 31, 0)
    page = client.get("/alerts").get_data(as_text=True)
    assert "Перезвонить: +7 900 111-22-33 — Подумает" in page and 'action="/callbacks/%d/done"' % task["id"] in page
    r = post(client, "/callbacks/%d/done" % task["id"], tok, back="/alerts")
    assert r.status_code == 302 and inquiry.open_callbacks(conn, order_id=card_id) == []
    assert "Перезвонить" not in client.get("/alerts").get_data(as_text=True).split("<tbody>")[1]


def test_a_bad_callback_is_explained_and_a_foreign_back_is_ignored(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    h = chat(conn)
    r = post(client, "/callbacks/new", tok, chat_row=str(h), date="2026-10-22", time="10:00", back="https://evil.example/")
    assert r.status_code == 302 and r.headers["Location"].endswith("/") and "evil" not in r.headers["Location"]
    assert inquiry.open_callbacks(conn, chat_row=h) == []
    assert client.post("/callbacks/new", data={"chat_row": str(h)}).status_code == 400                     # no CSRF token


def test_a_point_and_an_observer_cannot_set_callbacks(tmp_path):
    from test_mypoint import manager
    app, client, tok, conn = setup(tmp_path)
    pid = conn.execute("INSERT INTO point (name) VALUES ('Бета')").lastrowid
    conn.commit()
    mine = manager(app, conn, pid)
    h = chat(conn)
    assert post(mine, "/callbacks/new", "tok", chat_row=str(h), date="2026-10-24", time="10:00").status_code == 403
    users.add_user(conn, "viewer", "password-123", NOW, role="viewer")
    conn.commit()
    watcher = app.test_client()
    with watcher.session_transaction() as session:
        session["user"], session["epoch"], session["csrf"] = "viewer", store.get_session_epoch(conn, "viewer"), "tok"
    assert post(watcher, "/callbacks/new", "tok", chat_row=str(h), date="2026-10-24", time="10:00").status_code == 403


# ---- the status in the pages and «в работе» on opening -------------------------------------------------------------------------------

def test_the_status_is_in_the_chats_and_the_calls_and_opening_the_chat_puts_it_in_work(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    h = chat(conn)
    message(conn, h, at(-5))
    k = call(conn, at(-5), phone="+79005556677")
    inquiry.tick(conn, NOW)
    page = client.get("/chats").get_data(as_text=True)
    assert 'data-inquiry="new"' in page
    assert 'data-inquiry="new"' in client.get("/calls").get_data(as_text=True)
    client.get("/chats/%d" % h)
    client_id = conn.execute("SELECT client_id FROM client_chat WHERE chat_row = ?", (h,)).fetchone()[0]
    assert inquiry.get(conn, client_id)["status"] == "work"
    assert 'data-inquiry="work"' in client.get("/chats/%d" % h).get_data(as_text=True)
    other = conn.execute("SELECT client_id FROM client_call WHERE call_row = ?", (k,)).fetchone()[0]
    client.get("/clients/%d" % other)
    assert inquiry.get(conn, other)["status"] == "work"


def test_the_client_card_partial_shows_the_status_and_the_callbacks(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    h = chat(conn)
    message(conn, h, at(-5))
    inquiry.tick(conn, NOW)
    client_id = conn.execute("SELECT client_id FROM client_chat WHERE chat_row = ?", (h,)).fetchone()[0]
    inquiry.add_callback(conn, "2026-10-24T10:00", "Подумает", NOW, "owner", client_id=client_id)
    with app.test_request_context("/"):
        from flask import render_template_string, session
        session["user"], session["csrf"] = "owner", "tok"
        html = render_template_string('{% with inquiry_client_id = cid, inquiry_place = "card" %}{% include "_client_inquiry.html" %}{% endwith %}',
                                      cid=client_id)
        cell = render_template_string('{% with inquiry_client_id = cid, inquiry_place = "list" %}{% include "_client_inquiry.html" %}{% endwith %}',
                                      cid=client_id)
        none = render_template_string('{% with inquiry_client_id = 999, inquiry_place = "list" %}{% include "_client_inquiry.html" %}{% endwith %}')
    assert 'data-inquiry="new"' in html and "Подумает" in html and 'name="client_id" value="%d"' % client_id in html and "Перезвонить (1)" in html
    assert re.search(r'data-inquiry="new"', cell) and "Перезвонить" not in cell and none.strip() == ""


def test_avito_notes_do_not_make_a_chat_urgent(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    message(conn, chat(conn), at(-90), kind="system", text="Покупатель оставил отзыв")
    assert urgent(conn) == []


def test_avitos_auto_answer_is_not_an_answer(tmp_path):
    """The owner's accounts answer by themselves (Avito's auto-answer, on since 2026-10-13): an outgoing «system» message that comes last."""
    app, client, tok, conn = setup(tmp_path)
    h = chat(conn)
    message(conn, h, at(-90))
    message(conn, h, at(-89), direction="out", kind="system", text="[Системное сообщение] Здравствуйте! Ответим в ближайшее время.")
    assert conn.execute("SELECT last_dir FROM avito_chat WHERE id = ?", (h,)).fetchone()[0] == "out"
    [row] = urgent(conn)
    assert row["chat_row"] == h and "ждёт с 10:30" in row["text"]
    inquiry.tick(conn, NOW)
    assert inquiry.of_chats(conn, [h])[h]["code"] == "new"                           # nor is the inquiry «в работе»
    message(conn, h, at(-1), direction="out", text="Есть, 4 шт.")                    # a person answers: it goes out
    assert urgent(conn) == []
    inquiry.tick(conn, at(1))
    assert inquiry.of_chats(conn, [h])[h]["code"] == "work"


def test_a_chat_silent_for_a_week_is_no_longer_urgent(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    message(conn, chat(conn), at(-60 * 24 * 8))
    assert urgent(conn) == []
    message(conn, chat(conn, peer="p2"), at(-60 * 24 * 6))
    assert len(urgent(conn)) == 1


def test_for_a_chat_with_an_order_urgent_takes_the_place_of_the_yellow_record(tmp_path):
    """Хвост З13 (c): «Срочно ответить» of a chat with an order replaces the order's yellow «Клиент написал», as for a chat without one;
    the order's dot is red. Other records of the order stay."""
    app, client, tok, conn = setup(tmp_path)
    cid = orders.create_card(conn, {"client_name": "Татьяна"}, NOW, "owner")
    h = chat(conn, order_id=cid)
    message(conn, h, at(-70), text="Есть 205/55 R16?")
    orders.add_event(conn, cid, at(-70), "клиент", "chat", "Клиент написал: Есть 205/55 R16?", level="yellow")
    conn.execute("DELETE FROM order_event WHERE order_id = ? AND kind = 'created'", (cid,))
    conn.commit()
    from test_order_dots import dots
    assert dots(client.get("/orders?v=list").get_data(as_text=True)).get(cid, ("",))[0] == "red"       # the dot by the number: red, not gone
    orders.add_event(conn, cid, at(-60), "owner", "note", "Позвонить завтра", level="red")
    conn.commit()
    rows = alerts.page_rows(conn, "worker", NOW)
    [row] = [r for r in rows if r.get("urgent")]
    assert row["chat_row"] == h and row["of_order"] == cid and row["title"] == "Чат · заказ №%d" % cid
    assert not [r for r in rows if r["order_id"] == cid and r["kind"] == "chat"]                  # not «Клиент написал» as well
    assert "Позвонить завтра" in [r["text"] for r in rows if r["order_id"] == cid]
    assert alerts.order_levels(conn, "worker", NOW) == {cid: "red"}
    message(conn, h, at(-1), direction="out", text="Есть")                                         # answered: the yellow record is as before
    assert [r["text"] for r in alerts.page_rows(conn, "worker", NOW) if r["order_id"] == cid and r["kind"] == "chat"] == ["Клиент написал: Есть 205/55 R16?"]


def test_avitos_stub_is_a_label_with_the_chat_in_avito_on_the_card_and_in_the_alerts(tmp_path):
    """Хвост З19: «Клиент написал: Сообщение не поддерживается…» is shown as a label (the card: with «Открыть чат в Авито»); the record stays."""
    app, client, tok, conn = setup(tmp_path)
    cid = orders.create_card(conn, {"client_name": "Татьяна"}, NOW, "owner")
    h = chat(conn, order_id=cid)
    stub = "Клиент написал: Сообщение не поддерживается. Чтобы посмотреть сообщение, перейдите в Авито мессенджер"
    orders.add_event(conn, cid, at(-5), "клиент", "chat", stub, level="yellow")
    conn.commit()
    [row] = [r for r in alerts.page_rows(conn, "worker", NOW) if r["order_id"] == cid and r["kind"] == "chat"]
    assert row["text"] == alerts.NO_TEXT and "не поддерживается" not in row["text"]
    feed = client.get("/alerts/feed").get_data(as_text=True)
    assert "не поддерживается" not in feed
    html = client.get("/orders/%d" % cid).get_data(as_text=True)
    journal = html[html.index('<ul class="feed card-body">'):]
    assert "без текста" in journal and "не поддерживается" not in journal
    assert 'href="https://www.avito.ru/profile/messenger/channel/c-p1-1"' in journal and "Открыть чат в Авито" in journal
    assert conn.execute("SELECT text FROM order_event WHERE order_id = ? AND kind = 'chat'", (cid,)).fetchone()[0] == stub


@pytest.mark.parametrize("headers, works", [({"Sec-Fetch-Site": "same-origin"}, True), ({"Sec-Fetch-Site": "none"}, True), ({}, True),
                                            ({"Sec-Fetch-Site": "cross-site"}, False), ({"Sec-Fetch-Site": "same-site"}, False)])
def test_a_chat_opened_from_another_site_stays_new(tmp_path, headers, works):
    """tails2 П10: the «в работе» of an opened chat is done only inside the panel or opened directly, as the other GETs that do something."""
    app, client, tok, conn = setup(tmp_path)
    h = chat(conn)
    message(conn, h, at(-5))
    inquiry.tick(conn, NOW)
    assert client.get("/chats/%d" % h, headers=headers).status_code == 200
    client_id = conn.execute("SELECT client_id FROM client_chat WHERE chat_row = ?", (h,)).fetchone()[0]
    assert inquiry.get(conn, client_id)["status"] == ("work" if works else "new")
