"""Order alerts: the colour of journal records, what every user has seen, «Разобрался» and «исправлено» (no Flask)."""
from __future__ import annotations

import sqlite3

import pytest

from tires_sync import order_alerts as alerts, orders, store, trash

NOW = "2026-10-20T12:00:00"
LATER = "2026-10-20T13:00:00"


def db(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    orders.ensure_pipeline(conn, NOW)
    return conn


def card(conn):
    return orders.create_card(conn, {"client_name": "Иван"}, NOW, "owner")


@pytest.mark.parametrize("kind, text, level", [
    ("created", "Заказ создан на этапе «Новый заказ»", "green"),
    ("stage", "Этап: «Новый заказ» → «Заказ оформлен»", "green"),
    ("sale", "Записана продажа: 2 поз.", "green"),
    ("sale", "Продажа сторнирована: 2 поз.", "yellow"),
    ("chat", "Позиция не добавлена: нет кода", "red"),
    ("chat", "Позиция не определена: объявление Авито №1 не найдено", "red"),
    ("chat", "Отправлен ответ клиенту: здравствуйте", "green"),
    ("chat", "Отправлено фото клиенту", "green"),
    ("chat", "Сообщение клиенту не отправлено: Авито не принял сообщение (код 403)", "red"),
    ("chat", "Сообщение клиенту не подтверждено: Неизвестно, ушло ли сообщение", "red"),
    ("chat", "Фото клиенту не отправлено: Авито не принял фото (код 400)", "red"),
    ("chat", "Фото клиенту не подтверждено: Неизвестно, ушло ли фото", "red"),
    ("refused", "Перевод не выполнен: Чтобы перейти в «Заказ оформлен», заполните: точка.", "red"),
    ("notify", "Оповещение не отправлено: у точки «А» не привязан ВК", "red"),
    ("notify", "Оповещение отправлено: Telegram → owner", "grey"),
    ("reserve", "Оповещение о конце резерва: …", "yellow"),
    ("rule", "Позвонить клиенту: ошибка (нет шаблона)", "red"),
    ("rule", "Позвонить клиенту: ошибка", "red"),
    ("rule", "Задача создана: позвонить", "grey"),
    ("rule", "Задача не подготовлена: позвонить", "red"),
    ("chat", "Клиент написал после закрытия сделки (2026-10-20 12:00)", "yellow"),
    ("restored", "Заказ восстановлен из удалённых", "green"),
    ("field", "Резерв: товар на точке, срок до 2026-10-23", "green"),
    ("field", "Телефон: +7 900", "grey"),
    ("item", "Добавлено: шина × 4 по 5000 ₽", "grey"),
    ("note", "ошибка в адресе", "grey"),
    ("something new", "", "grey"),
])
def test_the_colour_follows_from_the_kind_and_the_text(kind, text, level):
    assert alerts.level_for(kind, text) == level


def test_new_records_store_their_colour_and_a_given_colour_wins(tmp_path):
    conn = db(tmp_path)
    cid = card(conn)
    orders.add_event(conn, cid, NOW, "система", "note", "Перемещение не прошло", level="red")
    rows = conn.execute("SELECT kind, level FROM order_event WHERE order_id = ? ORDER BY id", (cid,)).fetchall()
    assert rows == [("created", "green"), ("note", "red")]
    assert [e["level"] for e in orders.list_events(conn, cid)] == ["red", "green"]
    with pytest.raises(alerts.AlertError):
        orders.add_event(conn, cid, NOW, "система", "note", "x", level="purple")


def test_an_old_base_gets_the_column_and_its_journal_counts_as_seen(tmp_path):
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.executescript("""CREATE TABLE order_event (id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL, at TEXT NOT NULL,
        author TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL);
        INSERT INTO order_event (order_id, at, author, kind, text) VALUES (1, '2026-10-01', 'owner', 'created', 'Заказ создан'),
            (1, '2026-10-02', 'система', 'notify', 'Оповещение не отправлено: нет чата');""")
    old.commit()
    old.close()
    conn = store.connect(path)
    assert conn.execute("SELECT COUNT(*), MIN(level), MAX(level) FROM order_event").fetchone() == (2, "", "")    # old rows are not rewritten
    assert [e["level"] for e in orders.list_events(conn, 1)] == ["red", "green"]                              # their colour is worked out
    assert alerts.open_alerts(conn, "owner") == []                                                            # written before: seen by all
    conn.execute("INSERT INTO order_event (order_id, at, author, kind, text) VALUES (1, ?, 'owner', 'stage', 'Этап: «A» → «B»')", (NOW,))
    conn.commit()
    conn.close()
    conn = store.connect(path)                                                                                # a second start keeps the mark
    assert [e["text"] for e in alerts.open_alerts(conn, "owner")] == ["Этап: «A» → «B»"]


def test_a_fresh_base_starts_from_zero(tmp_path):
    conn = db(tmp_path)
    cid = card(conn)
    assert [(e["order_id"], e["level"]) for e in alerts.open_alerts(conn, "owner")] == [(cid, "green")]


def test_opening_an_order_closes_green_and_yellow_for_that_user_only(tmp_path):
    conn = db(tmp_path)
    one, two = card(conn), card(conn)
    orders.add_event(conn, one, NOW, "система", "reserve", "Оповещение о конце резерва")
    orders.add_event(conn, one, NOW, "owner", "field", "Телефон: +7")                                         # grey: never an alert
    assert alerts.counts(conn, "owner") == {"red": 0, "yellow": 1, "green": 2}
    alerts.mark_seen(conn, "owner", one, LATER)
    assert alerts.counts(conn, "owner") == {"red": 0, "yellow": 0, "green": 1}
    assert alerts.counts(conn, "worker2") == {"red": 0, "yellow": 1, "green": 2}                                # every user has their own
    orders.add_event(conn, one, LATER, "owner", "stage", "Этап: «A» → «B»")
    assert [e["text"] for e in alerts.open_alerts(conn, "owner", one)] == ["Этап: «A» → «B»"]                 # newer records are new again
    alerts.mark_seen(conn, "owner", two, LATER)
    alerts.mark_seen(conn, "owner", two, LATER)                                                               # twice is the same
    assert alerts.open_alerts(conn, "owner", two) == []


def test_a_red_record_waits_for_razobralsya_or_for_the_fix(tmp_path):
    conn = db(tmp_path)
    cid = card(conn)
    orders.add_event(conn, cid, NOW, "система", "note", "Перемещение не прошло", level="red")
    red = alerts.open_alerts(conn, "owner", cid)[0]
    assert red["level"] == "red"
    alerts.mark_seen(conn, "owner", cid, LATER)
    assert [e["id"] for e in alerts.open_alerts(conn, "owner")] == [red["id"]]                                # opening does not close red
    assert alerts.order_levels(conn, "owner") == {cid: "red"}
    alerts.ack(conn, red["id"], "owner", LATER)
    alerts.ack(conn, red["id"], "owner", LATER)                                                               # twice is the same
    assert alerts.open_alerts(conn, "owner") == []
    assert alerts.order_levels(conn, "worker2") == {cid: "red"}                                                 # Партнёр has not looked yet
    alerts.resolve(conn, red["id"], LATER, "система")
    assert conn.execute("SELECT author FROM order_event_ack WHERE login = '*'").fetchone() == ("система",)
    assert alerts.order_levels(conn, "worker2") == {cid: "green"}                                               # fixed: closed for everybody


def test_razobralsya_only_on_a_red_record_by_a_named_user(tmp_path):
    conn = db(tmp_path)
    cid = card(conn)
    green = alerts.open_alerts(conn, "owner", cid)[0]
    with pytest.raises(alerts.AlertError):
        alerts.ack(conn, green["id"], "owner", LATER)
    with pytest.raises(alerts.AlertError):
        alerts.resolve(conn, green["id"], LATER, "owner")
    with pytest.raises(alerts.AlertError):
        alerts.ack(conn, 999999, "owner", LATER)
    orders.add_event(conn, cid, NOW, "система", "note", "сбой", level="red")
    red = alerts.open_alerts(conn, "owner", cid)[0]
    for login in ("", alerts.EVERYBODY):
        with pytest.raises(alerts.AlertError):
            alerts.ack(conn, red["id"], login, LATER)


def test_the_most_important_colour_marks_the_order(tmp_path):
    conn = db(tmp_path)
    cid = card(conn)
    orders.add_event(conn, cid, NOW, "система", "reserve", "Оповещение о конце резерва")
    assert alerts.order_levels(conn, "owner") == {cid: "yellow"}


def test_a_deleted_and_restored_order_keeps_its_journal_and_marks(tmp_path):
    conn = db(tmp_path)
    cid = card(conn)
    orders.add_event(conn, cid, NOW, "система", "note", "сбой", level="red")
    conn.commit()
    record = trash.delete_order(conn, cid, LATER, "owner")
    assert alerts.open_alerts(conn, "owner") == []
    assert trash.restore_order(conn, record, LATER, "owner") == cid
    levels = [e["level"] for e in orders.list_events(conn, cid)]
    assert levels[-2:] == ["red", "green"] and "red" in alerts.order_levels(conn, "owner").values()


def test_seen_stops_at_the_last_record_shown(tmp_path):
    conn = db(tmp_path)
    cid = card(conn)
    shown = alerts.open_alerts(conn, "owner", cid)[0]["id"]
    orders.add_event(conn, cid, NOW, "система", "reserve", "Оповещение о конце резерва")       # came after the page was drawn
    alerts.mark_seen(conn, "owner", cid, LATER, up_to=shown)
    assert [e["level"] for e in alerts.open_alerts(conn, "owner", cid)] == ["yellow"]
