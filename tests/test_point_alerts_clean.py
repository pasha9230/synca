"""The point hears only of what it does (the owner 2026-11-07: «Убери у точек все лишние уведомления, они им не нужны»): its bell, the counter,
«Уведомления», the sound (/my-point/feed) and the dots, for the manager of a point and for «Работать как точка», show a list of what is the
point's — «Поступили шины по заказу …», «Клиент забрал?», «возврат отмечен» (the return agreed or cancelled is allowed too, but its record is
grey: no alert) — and nothing of the office's records; the 7th day of «На ОТХ» and its orders on the way ring in /my-point/feed as before. The office, the owner and the journal of the order are
not changed; what goes to the point's VK and «Рабочее место» (order_notify) is not changed here."""
from __future__ import annotations

import pytest

from test_alerts_clean import bell
from test_mypoint import manager
from test_mypoint_board import MORNING, asked, at_point
from test_vk_commands import ready
from tires_sync import order_alerts as alerts, order_manager, orders
from tires_sync.panel import routes_alerts, routes_mypoint

OFFICE = [                                                       # what the point saw before (the owner's list of 2026-11-07)
    ("created", "Заказ создан вручную", ""),
    ("stage", "Этап: «Новый» → «В работе»", ""),
    ("reserve", "Резерв: срок до 14.10", ""),
    ("reserve", "Резерв кончается завтра", ""),
    ("chat", "Сообщение клиенту не отправлено («Шины на точке»): Авито отказал", "red"),
    ("notify", "Оповещение не отправлено: Telegram → owner: нет чата", ""),
    ("notify", "Оповещение о конце резерва: отправлено в Telegram", "yellow"),
    ("rule", "Задача не подготовлена: нет шаблона", ""),
    ("chat", "Клиент звонил: пропущен", "yellow"),
    ("chat", "Клиент написал после закрытия сделки: спасибо", ""),
    ("chat", "Отправлен ответ клиенту: да", ""),
    ("chat", "Позиция не определена: нет кода", "red"),
    ("refused", "Перевод не выполнен: нет телефона клиента", ""),
    ("sale", "Продажа проведена: 20 000 ₽", ""),
    ("field", "Шины с ОТХ взяты не полностью (нет остатка): проверьте заказ", "red"),
    ("handoff", "Клиент хочет заказать шины", "yellow"),
]
ARRIVED = order_manager.ARRIVAL % ("5000100", "Бета", "отправлено")


@pytest.fixture
def clock(monkeypatch):
    moment = {"now": MORNING}
    monkeypatch.setattr(routes_mypoint, "local_iso", lambda: moment["now"])
    monkeypatch.setattr(routes_alerts, "local_iso", lambda: moment["now"])
    return moment


def world(tmp_path):
    app, owner, tok, conn, pid, cid = ready(tmp_path)
    at_point(conn, cid)
    asked(conn, cid)                                                              # «Клиент забрал?» is open
    for kind, text, level in OFFICE:
        orders.add_event(conn, cid, MORNING, "o", kind, text, level=level)
    orders.add_event(conn, cid, MORNING, "менеджер Бета", "point", ARRIVED, level="yellow")
    conn.commit()
    return app, owner, conn, pid, cid


def as_point(client, pid):
    with client.session_transaction() as session:
        session["as_point"] = pid
    return client


@pytest.mark.parametrize("who", ["manager", "as_point"])
def test_the_point_sees_only_its_own_rows_everywhere(tmp_path, clock, who):
    app, owner, conn, pid, cid = world(tmp_path)
    person = manager(app, conn, pid) if who == "manager" else as_point(owner, pid)
    data = bell(person)
    assert sorted(i["text"] for i in data["items"]) == sorted([ARRIVED, "Ответьте на «Рабочем месте»: «Выдан» или «На точке»."])
    assert data["count"] == 2 and data["red"] is False
    page = person.get("/alerts").get_data(as_text=True)
    assert ARRIVED in page and not [t for _, t, _ in OFFICE if t in page]
    feed = person.get("/my-point/feed").get_json()["items"]
    rung = [i["text"] for i in feed if i["key"].startswith("alert-")]
    assert not [t for _, t, _ in OFFICE if t in rung]
    assert any(i["key"].startswith("question-") for i in feed) and any(i["key"].startswith("order-") for i in feed)   # «едет к вам / на точке»


def test_the_point_counts_no_office_red(tmp_path, clock):
    app, owner, conn, pid, cid = world(tmp_path)
    manager(app, conn, pid)
    assert alerts.counts(conn, "gamma") == {"red": 0, "yellow": 1, "green": 0}            # the arrival only


def test_the_office_sees_what_it_saw_and_the_journal_keeps_everything(tmp_path, clock):
    app, owner, conn, pid, cid = world(tmp_path)
    owner.get("/alerts")                                                            # the flash messages of the setup are shown once
    before = owner.get("/alerts").get_data(as_text=True)
    texts = [r["text"] for r in alerts.page_rows(conn, "owner")]                     # the source of the office's rows: all its records
    assert "Резерв кончается завтра" in texts and "Задача не подготовлена: нет шаблона" in texts
    assert "Позиция не определена: нет кода" in texts
    assert before.count('href="/orders/%d"' % cid) == 1 and "ещё " in before        # one row of the order, its first texts (п. 31)
    count = bell(owner)["count"]
    manager(app, conn, pid).get("/alerts/feed")                                     # the point looked: nothing of the office's changes
    assert owner.get("/alerts").get_data(as_text=True) == before and bell(owner)["count"] == count
    journal = [e["text"] for e in orders.list_events(conn, cid)]
    assert all(t in journal for _, t, _ in OFFICE) and ARRIVED in journal


def test_the_row_rule():
    ok = alerts.point_row
    assert ok({"id": 1, "kind": "point", "level": "yellow", "text": ARRIVED})
    assert ok({"id": 1, "kind": "point", "level": "yellow", "text": order_manager.ARRIVAL_INTERCITY % ("1", "Бета")})
    assert ok({"id": "question-1-2", "kind": "question", "level": "yellow", "text": "Ответьте"})
    assert ok({"id": "return-1", "kind": "return", "level": "green", "text": "Сумму возврата вносит менеджер"})
    assert ok({"id": 1, "kind": "field", "level": "yellow", "text": "Возврат с ОТХ согласован с поставщиком: передать на возврат 15.10 (акт МХ-3)."})
    for kind, text, level in OFFICE + [("point", "Точка ответила: клиент придёт завтра", "yellow"),
                                       ("point", "Клиент не пришёл за заказом (точка «Бета»)", "red")]:
        assert not ok({"id": 1, "kind": kind, "level": level or "yellow", "text": text}), text
