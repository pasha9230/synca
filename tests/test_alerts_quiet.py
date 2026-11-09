"""«Уведомления» only of the failures and of what needs an action (the owner 2026-11-09), for every role: the owner, a manager, the manager of
a point and the owner «как точка». No row of: a stage, «Заказ создан/восстановлен»; a note of an order («Заказ №N: …», critical.note); the
green «на точке» («Резерв: товар на точке», «Поступили шины по заказу …», «Отправлен ответ клиенту»); «Клиент написал», «Срочно ответить»;
«Критичное» and «Закупка изменилась» (they go to Telegram and VK). A red record of the order journal and a yellow one stay; the counter of the
menu and of the bell is the number of rows of the page. Only the showing: the journal keeps every record."""
from __future__ import annotations

import re

import pytest

from test_alerts_clean import bell
from test_alerts_page import counter
from test_mypoint import manager
from test_mypoint_board import MORNING, asked, at_point
from test_point_alerts_clean import as_point
from test_vk_commands import ready
from tires_sync import critical, order_alerts as alerts, order_manager, orders, purchase_alert, store, users
from tires_sync.panel import routes_alerts, routes_mypoint

PEOPLE = ["owner", "worker2", "gamma"]
QUIET = [                                                        # (kind, text, level) of the journal: no row of anybody
    ("stage", "Этап: «Новый» → «В работе»", ""),
    ("created", "Заказ создан вручную", ""),
    ("restored", "Заказ восстановлен из удалённых", ""),
    ("field", "Резерв: товар на точке до 20.10", ""),
    ("point", order_manager.ARRIVAL % ("5000100", "Бета", "отправлено"), "yellow"),
    ("chat", "Отправлен ответ клиенту: да", "green"),
    ("chat", "Клиент написал: есть 205/55 R16?", "yellow"),
]
NOTE = "Заказ №%d: Завтра заканчивается резерв"
CRITICAL = "Tires Sync: цикл завершился с ошибками"
RED = "Сообщение клиенту не отправлено («Шины на точке»): Авито отказал"
YELLOW = "Сумма в кабинете MIM другая: 21 000 ₽"
HIDDEN = [t for _, t, _ in QUIET] + ["Завтра заканчивается резерв", CRITICAL, "Закупка у поставщика изменилась",
                                      "Срочно ответить", "Клиент написал: шипы?"]


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(routes_mypoint, "local_iso", lambda: MORNING)
    monkeypatch.setattr(routes_alerts, "local_iso", lambda: MORNING)


@pytest.fixture
def world(tmp_path, monkeypatch, clock):
    app, owner, tok, conn, pid, cid = ready(tmp_path)
    at_point(conn, cid)
    asked(conn, cid)                                                              # the point's own yellow row: «Клиент забрал?»
    users.add_user(conn, "worker2", "password-123", MORNING)
    for kind, text, level in QUIET:
        orders.add_event(conn, cid, MORNING, "система", kind, text, level=level)
    orders.add_event(conn, cid, MORNING, "система", "chat", RED, level="red")
    orders.add_event(conn, cid, MORNING, "система", "field", YELLOW, level="yellow")
    conn.commit()
    red, yellow = (conn.execute("SELECT id FROM order_event WHERE text = ?", (t,)).fetchone()[0] for t in (RED, YELLOW))
    critical.note(conn, PEOPLE, NOTE % cid, MORNING)                              # a note of the order (order_notify.deliver)
    critical.tell(conn, None, PEOPLE, CRITICAL, MORNING)                          # «Критичное»: Telegram, VK and (before) the bell
    monkeypatch.setattr(purchase_alert, "bell_rows", lambda conn_, login: [{     # «Закупка изменилась» of who may see the purchase
        "id": "purchase-1", "order_id": None, "chat_row": None, "client": "", "at": MORNING, "author": "", "kind": "purchase",
        "level": "red", "more": 0, "title": "Закупка изменилась", "text": "Закупка у поставщика изменилась: 1 позиция", "href": "/prices"}])
    monkeypatch.setattr(alerts, "chat_rows", lambda conn_: [{                    # «Клиент написал» of a chat without an order
        "id": "chat-7", "order_id": None, "chat_row": 7, "client": "Олег", "at": MORNING, "author": "", "kind": "chat",
        "level": "yellow", "more": 0, "title": "Чат", "text": "Клиент написал: шипы?"}])
    monkeypatch.setattr(alerts, "urgent_rows", lambda conn_, now, also=(): [{      # «Срочно ответить»
        "id": "urgent-8", "order_id": None, "chat_row": 8, "of_order": None, "client": "Ира", "at": MORNING, "author": "", "kind": "chat",
        "level": "red", "more": 0, "urgent": True, "title": "Чат", "text": "Срочно ответить: ждёт с 10:00: есть?"}])
    return app, owner, conn, pid, cid, red, yellow


def rows_of_page(html):
    return len(re.findall(r"<tr[ >]", html)) - 1                                  # the head of the table


def look(client):
    """What a person sees: the bell (/alerts/feed), the page, the menu counter."""
    feed = bell(client)
    page = client.get("/alerts").get_data(as_text=True)
    shown = " ".join(i["text"] for i in feed["items"]) + page
    assert not [t for t in HIDDEN if t in shown], [t for t in HIDDEN if t in shown]
    assert feed["count"] == len(feed["items"])
    return feed, page


def test_the_office_sees_the_red_and_the_yellow_only_and_the_counter_is_the_page(world):
    app, owner, conn, pid, cid, red, yellow = world
    assert {r["kind"] for r in alerts.all_rows(conn, "owner", MORNING)} >= {"stage", "staffnote", "critical", "purchase", "chat"}  # the sources
    rows = alerts.page_rows(conn, "owner", MORNING)
    assert sorted(r["id"] for r in rows) == sorted([red, yellow])               # the one source of every surface
    assert alerts.counts(conn, "owner") == {"red": 1, "yellow": 1, "green": 0}
    assert alerts.order_levels(conn, "owner", MORNING) == {cid: "red"}           # the dots by the order numbers
    feed, page = look(owner)
    assert feed["count"] == 1 and feed["red"] is True                            # one row of the order (п. 31): its red and its yellow
    assert RED in feed["items"][0]["text"] and YELLOW in feed["items"][0]["text"]
    assert RED in page and rows_of_page(page) == 1
    assert counter(owner.get("/").get_data(as_text=True)) == (1, True)
    journal = {e["text"] for e in orders.list_events(conn, cid)}
    assert {t for _, t, _ in QUIET} | {RED, YELLOW} <= journal                   # the journal keeps everything


def test_a_manager_sees_the_same_rule(world):
    app, owner, conn, pid, cid, red, yellow = world
    conn.execute("UPDATE order_card SET responsible = 'worker2' WHERE id = ?", (cid,))
    conn.commit()
    worker = app.test_client()
    with worker.session_transaction() as session:
        session["user"], session["epoch"], session["csrf"] = "worker2", store.get_session_epoch(conn, "worker2"), "tok"
    assert sorted(r["id"] for r in alerts.page_rows(conn, "worker2", MORNING)) == sorted([red, yellow])
    feed, page = look(worker)
    assert feed["count"] == 1 and RED in feed["items"][0]["text"] and YELLOW in feed["items"][0]["text"]
    assert counter(worker.get("/").get_data(as_text=True)) == (1, True)


@pytest.mark.parametrize("who", ["manager", "as_point"])
def test_the_point_and_the_owner_as_a_point_see_only_the_question(world, who):
    app, owner, conn, pid, cid, red, yellow = world
    person = manager(app, conn, pid) if who == "manager" else as_point(owner, pid)
    feed, page = look(person)
    assert [i["text"] for i in feed["items"]] == ["Ответьте на «Рабочем месте»: «Выдан» или «На точке»."]
    assert feed["count"] == 1 and feed["red"] is False and rows_of_page(page) == 1
    rung = [i["text"] for i in person.get("/my-point/feed").get_json()["items"] if i["key"].startswith("alert-")]
    assert not [t for t in HIDDEN if any(t in r for r in rung)]                   # notify.js of the point: the same rows
    if who == "manager":
        assert alerts.counts(conn, "gamma") == {"red": 0, "yellow": 0, "green": 0}
