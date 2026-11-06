"""«Уведомления» without the noise (the owner 2026-11-05: «из уведомлений убери весь мусор, чаты не нужны там», «этапы тоже там не нужны»):
the chat records but the red failures of a send to the client, the stages and «Резерв: товар на точке» are no rows of the page, of the bell,
of its counter, of notify.js and (2026-11-06) of the dots by the order numbers. Only the showing changes: the records stay in the journal of the
order with their colour, nothing is written, the counter of «Чаты» is as it was, the point keeps its question and its return row."""
from __future__ import annotations

import re

import pytest

from test_alerts_page import NOW, as_user, card, counter, event, setup
from test_inquiry import at, chat, message
from test_mypoint import manager
from test_mypoint_board import MORNING, asked, at_point
from test_order_dots import dots
from test_vk_commands import ready
from tires_sync import order_alerts as alerts, orders, store
from tires_sync.panel import routes_alerts, routes_mypoint

HIDDEN = [
    ("chat", "Клиент написал: есть 205/55 R16?", "yellow"),
    ("chat", "Клиент звонил: пропущен", "yellow"),
    ("chat", "Клиент написал после закрытия сделки: спасибо", ""),               # yellow by level_for
    ("chat", "Отправлен ответ клиенту: да", ""),                                    # green
    ("chat", "Отправлено фото клиенту: 2 шт.", ""),                                 # green
    ("chat", "Клиенту сообщили, что шины на точке, другим способом", "yellow"),
    ("stage", "Этап: «Новый» → «В работе»", ""),
    ("created", "Заказ создан вручную", ""),
    ("restored", "Заказ восстановлен из удалённых", ""),
    ("field", "Резерв: товар на точке до 20.10", ""),                               # green
]

KEPT = [
    ("chat", "Позиция не добавлена: нет кода", "", "red"),
    ("chat", "Сообщение клиенту не отправлено («Шины на точке»): нет чата", "red", "red"),
    ("chat", "Фото клиенту не отправлено: нет чата", "", "red"),
    ("chat", "Сообщение клиенту «появилось в наличии» не отправлено: нет чата", "red", "red"),   # order_wait: a failed send too
    ("point", "Точка ответила: клиент придёт завтра", "yellow", "yellow"),
    ("reserve", "Резерв кончается завтра", "", "yellow"),
    ("notify", "Оповещение не отправлено: у точки нет ВК", "", "red"),
    ("refused", "Перевод не выполнен: нет телефона клиента", "", "red"),
    ("rule", "Задача не подготовлена: нет шаблона", "", "red"),
    ("sale", "Продажа проведена: 20 000 ₽", "", "green"),
    ("sale", "Продажа сторнирована: 20 000 ₽", "", "yellow"),
    ("field", "Шины с ОТХ взяты не полностью (нет остатка): проверьте заказ", "red", "red"),
]


def state(conn):
    """Everything the panel could write about the alerts, and the journal itself."""
    return [conn.execute("SELECT * FROM %s ORDER BY 1, 2" % table).fetchall() for table in ("order_event", "order_event_seen", "order_event_ack")]


def page(client):
    return client.get("/alerts").get_data(as_text=True)


def bell(client):
    return client.get("/alerts/feed").get_json()


# --- the office ---------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("kind,text,level", HIDDEN)
def test_a_noise_record_is_no_row_of_the_page_nor_of_the_bell_and_stays_in_the_journal(tmp_path, kind, text, level):
    app, client, tok, conn = setup(tmp_path)
    cid = card(conn, "Ольга")
    eid = event(conn, cid, kind, text, level=level)
    stored = conn.execute("SELECT level FROM order_event WHERE id = ?", (eid,)).fetchone()[0]
    assert stored in alerts.ALERT_LEVELS                                         # it is an alert by its colour ...
    assert eid in [e["id"] for e in alerts.open_alerts(conn, "owner")]
    assert any(r["id"] == eid or r["text"] == text for r in alerts.page_rows(conn, "owner", NOW))     # ... and a row of the source
    before = state(conn)
    assert text not in page(client)                                              # ... but not of the page
    assert bell(client) == {"count": 0, "red": False, "items": []}               # nor of the bell
    assert counter(client.get("/").get_data(as_text=True)) == (0, False)         # nor of the menu counter
    assert state(conn) == before                                                 # nothing is written by the showing
    assert cid not in dots(client.get("/orders?v=list").get_data(as_text=True))     # nor of the dot by the number (the owner 2026-11-06)
    journal = orders.list_events(conn, cid)
    assert [(e["kind"], e["level"]) for e in journal if e["id"] == eid] == [(kind, stored)]  # the journal: the record, its colour
    assert text in client.get("/orders/%d" % cid).get_data(as_text=True)          # seen in the order


@pytest.mark.parametrize("kind,text,level,shown", KEPT)
def test_what_is_kept_is_a_row_with_its_colour(tmp_path, kind, text, level, shown):
    app, client, tok, conn = setup(tmp_path)
    cid = card(conn, "Ольга")
    eid = event(conn, cid, kind, text, level=level)
    data = bell(client)
    assert [(i["id"], i["level"], i["text"]) for i in data["items"]] == [(eid, shown, text)]
    assert data["count"] == 1 and data["red"] is (shown == "red")
    assert text in page(client)
    assert counter(client.get("/").get_data(as_text=True)) == (1, shown == "red")


def test_the_counter_counts_what_is_left_and_old_records_are_as_they_were(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    cid, other = card(conn, "Ольга"), card(conn, "Пётр")                        # two green «Заказ создан»
    for kind, text, level in HIDDEN:
        event(conn, cid, kind, text, level=level)
    red = event(conn, other, "notify", "Оповещение не отправлено: нет ВК")
    yellow = event(conn, other, "reserve", "Резерв кончается завтра")
    failed = event(conn, cid, "chat", "Позиция не добавлена: нет кода")
    opened = alerts.open_alerts(conn, "owner")
    assert len(opened) == len(HIDDEN) + 2 + 3                                     # the data: every record is still an open alert
    before = state(conn)
    data = bell(client)
    assert data["count"] == 3 and data["red"] is True and sorted(i["id"] for i in data["items"]) == sorted([red, yellow, failed])
    assert counter(client.get("/").get_data(as_text=True)) == (3, True)
    assert page(client).count("<tr>") == 1 + 3
    assert state(conn) == before and alerts.open_alerts(conn, "owner") == opened
    assert alerts.counts(conn, "owner")["red"] == 2                              # the red of the day dashboard: as before
    client.get("/orders/%d" % other)                                             # the yellow of the order is seen as before
    assert bell(client)["count"] == 2


def test_the_staff_and_another_user_see_the_same_rule(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    cid = card(conn, "Ольга")
    conn.execute("UPDATE order_card SET responsible = 'worker2' WHERE id = ?", (cid,))
    conn.commit()
    worker2 = as_user(app, conn, "worker2")
    event(conn, cid, "stage", "Этап: «Новый» → «В работе»")
    event(conn, cid, "chat", "Клиент написал: привет", level="yellow")
    kept = event(conn, cid, "reserve", "Резерв кончается завтра")
    assert [i["id"] for i in bell(worker2)["items"]] == [kept]
    assert counter(worker2.get("/").get_data(as_text=True)) == (1, False)


def test_chats_without_an_order_and_urgent_are_no_rows_and_the_chats_counter_stays(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    conn.execute("INSERT INTO account (id, name, feed_token, created, chats_enabled) VALUES (1, 'Альфа', 't1', '2026-10-01', 1)")
    store.set_state(conn, alerts.URGENT_START_KEY, "2026-10-01T00:00:00")
    waits = chat(conn)
    message(conn, waits, at(-90, NOW), text="Есть 205/55 R16?")                  # «Срочно ответить» by now
    later = chat(conn, peer="p2", name="Олег")
    message(conn, later, at(-5, NOW), text="А шипы?")                            # «Клиент написал» of a chat without an order
    conn.execute("UPDATE avito_chat SET unread = 1, status = 'open'")
    conn.commit()
    rows = alerts.page_rows(conn, "owner", NOW)
    assert {r["chat_row"] for r in rows if r.get("urgent")} == {waits} and later in {r["chat_row"] for r in rows}   # the source has them
    data = bell(client)
    assert data == {"count": 0, "red": False, "items": []}
    html = client.get("/").get_data(as_text=True)
    assert counter(html) == (0, False)
    chats = re.search(r'href="/chats">.*?Чаты(?:<span class="sb-count">(\d+)</span>)?</a>', html)
    assert chats and chats.group(1) == "2"                                       # «Чаты» counts its unread as before


@pytest.mark.parametrize("kind,text,level,shown", KEPT)
def test_a_kept_record_lights_the_dot_by_the_number(tmp_path, kind, text, level, shown):
    app, client, tok, conn = setup(tmp_path)
    cid = card(conn, "Ольга")
    event(conn, cid, kind, text, level=level)
    for url in ("/orders?v=list", "/orders"):
        assert dots(client.get(url).get_data(as_text=True))[cid][0] == shown


def test_after_accept_all_no_dot_burns_for_hidden_records(tmp_path):
    """The owner 2026-11-06: the dots by the numbers count what the bell counts; after «Принять все» a chat or a stage of the order
    (hidden, never accepted) lights nothing."""
    app, client, tok, conn = setup(tmp_path)
    cid, other = card(conn, "Ольга"), card(conn, "Пётр")
    kept = event(conn, cid, "reserve", "Резерв кончается завтра")
    red = event(conn, other, "chat", "Сообщение клиенту не отправлено («Шины на точке»): нет чата", level="red")
    for kind, text, level in HIDDEN:
        event(conn, cid, kind, text, level=level)
    event(conn, cid, "stage", "Этап: «В работе» → «Заказ оформлен»")       # a hidden record newer than the kept one
    html = client.get("/orders?v=list").get_data(as_text=True)
    assert dots(html)[cid][0] == "yellow" and dots(html)[other][0] == "red"
    client.post("/alerts/accept-all", data={"csrf": tok, "ids": [kept, red]})
    for url in ("/orders?v=list", "/orders"):
        shown = dots(client.get(url).get_data(as_text=True))
        assert cid not in shown and other not in shown
    assert bell(client)["count"] == 0
    assert any(e["id"] > kept for e in alerts.open_alerts(conn, "owner", cid))   # the hidden records are still open alerts: only the showing


def test_bell_row_by_its_kind_and_text():
    assert alerts.bell_row({"id": 1, "kind": "chat", "level": "red", "text": "Позиция не добавлена"})
    assert not alerts.bell_row({"id": 1, "kind": "chat", "level": "yellow", "text": "Клиент написал: привет"})
    assert not alerts.bell_row({"id": "urgent-3", "kind": "chat", "level": "red", "text": "Срочно ответить: ждёт с 10:50"})
    assert not alerts.bell_row({"id": "chat-3", "kind": "chat", "level": "yellow", "text": "Клиент написал: привет"})
    for kind in ("stage", "created", "restored"):
        assert not alerts.bell_row({"id": 1, "kind": kind, "level": "green", "text": "Этап"})
    assert not alerts.bell_row({"id": 1, "kind": "field", "level": "green", "text": "Резерв: товар на точке до 20.10"})
    assert alerts.bell_row({"id": 1, "kind": "field", "level": "red", "text": "Шины с ОТХ взяты не полностью"})
    for row in ({"id": "inbox-1", "kind": "inbox", "level": "yellow", "text": "Точка сообщила о заказе"},
                {"id": "question-1-2", "kind": "question", "level": "yellow", "text": "Ответьте"},
                {"id": "return-1", "kind": "return", "level": "green", "text": "Сумму возврата вносит менеджер"},
                {"id": 1, "kind": "handoff", "level": "yellow", "text": "Клиент хочет заказать шины"},
                {"id": 1, "kind": "task", "level": "yellow", "text": "Задача"}):
        assert alerts.bell_row(row), row


# --- the point ----------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def clock(monkeypatch):
    moment = {"now": MORNING}
    monkeypatch.setattr(routes_mypoint, "local_iso", lambda: moment["now"])
    monkeypatch.setattr(routes_alerts, "local_iso", lambda: moment["now"])
    return moment


def test_the_point_bell_and_notify_keep_the_question_and_lose_the_noise(tmp_path, clock):
    app, owner, tok, conn, pid, cid = ready(tmp_path)
    at_point(conn, cid)
    asked(conn, cid)
    mine = manager(app, conn, pid)
    noise = []
    for kind, text, level in HIDDEN:
        orders.add_event(conn, cid, MORNING, "o", kind, text, level=level)
        noise.append(text)
    orders.add_event(conn, cid, MORNING, "o", "chat", "Позиция не добавлена: нет кода")
    orders.add_event(conn, cid, MORNING, "o", "reserve", "Резерв кончается завтра")
    conn.commit()
    data = bell(mine)
    texts = [i["text"] for i in data["items"]]
    assert "Ответьте на «Рабочем месте»: «Выдан» или «На точке»." in texts       # «Клиент забрал?» stays
    assert "Позиция не добавлена: нет кода" in texts and "Резерв кончается завтра" in texts
    assert not [t for t in texts if t in noise or t.startswith("Этап")]
    assert data["count"] == 3 and data["red"] is True
    feed = mine.get("/my-point/feed").get_json()["items"]
    rung = [i["text"] for i in feed if i["key"].startswith("alert-")]           # what notify.js rings for of the bell
    assert "Позиция не добавлена: нет кода" in rung and "Резерв кончается завтра" in rung
    assert not [t for t in rung if t in noise or t.startswith(("Этап", "Заказ создан"))]
    assert any(i["key"].startswith("question-") for i in feed)
    page_html = mine.get("/alerts").get_data(as_text=True)
    assert "Клиент забрал заказ" in page_html and not [t for t in noise if t in page_html]
