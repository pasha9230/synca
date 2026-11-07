"""После «Критичного» (06.11) утренняя сводка и лист «Фото на проверке» не критичны: в Telegram и ВК не идут. Задача lane/digest-bell:
оба приходят строкой в колокольчик владельцев (critical.note, «Оповещение»), не чаще одного раза за выпуск, без дублей. Настоящих отправок нет."""
from __future__ import annotations

import io
from datetime import datetime

from test_agent_api import add_set, jpeg, setup
from test_critical import NOW, env
from tires_sync import critical, daily_summary, store, users

OWNERS = ("owner", "second1")


def notes(conn, login, part):
    return [r["text"] for r in critical.bell_rows(conn, login, NOW, action=critical.NOTE) if part in r["text"]]


# --- утренняя сводка -------------------------------------------------------------------------------------------------------------------

def test_the_summary_is_a_row_of_the_owners_bell_once_a_day_and_not_telegram_nor_vk(tmp_path, monkeypatch):
    conn, box, http = env(tmp_path, monkeypatch)
    users.add_user(conn, "worker2", "пароль-для-партнёра-1", NOW, role="partner")
    monkeypatch.setattr(daily_summary, "is_on", lambda conn: True)
    said = daily_summary.maybe_send(conn, box, datetime(2026, 11, 6, 10, 0), http=http)
    assert "колокольчик" in said
    for login in OWNERS:
        assert len(notes(conn, login, "Сводка на 06.11")) == 1
    assert notes(conn, "worker2", "Сводка") == []                                 # только владельцы
    assert http.tg == [] and http.vk == []
    assert daily_summary.maybe_send(conn, box, datetime(2026, 11, 6, 11, 0), http=http) is None    # второй тик того же дня
    assert len(notes(conn, "owner", "Сводка")) == 1


def test_the_summary_switched_off_or_before_nine_writes_nothing(tmp_path, monkeypatch):
    conn, box, http = env(tmp_path, monkeypatch)
    assert daily_summary.maybe_send(conn, box, datetime(2026, 11, 6, 10, 0), http=http) is None
    monkeypatch.setattr(daily_summary, "is_on", lambda conn: True)
    assert daily_summary.maybe_send(conn, box, datetime(2026, 11, 6, 8, 59), http=http) is None
    assert notes(conn, "owner", "Сводка") == []


def test_a_busy_base_loses_no_summary_and_makes_no_second_one(tmp_path, monkeypatch):
    conn, box, http = env(tmp_path, monkeypatch)
    monkeypatch.setattr(daily_summary, "is_on", lambda conn: True)
    real = store.write_audit
    monkeypatch.setattr(store, "write_audit", lambda *a, **k: (_ for _ in ()).throw(__import__("sqlite3").OperationalError("database is locked")))
    assert "не записана" in daily_summary.maybe_send(conn, box, datetime(2026, 11, 6, 10, 0), http=http)
    monkeypatch.setattr(store, "write_audit", real)
    assert "колокольчик" in daily_summary.maybe_send(conn, box, datetime(2026, 11, 6, 10, 5), http=http)     # следующий тик: пишет
    assert daily_summary.maybe_send(conn, box, datetime(2026, 11, 6, 10, 10), http=http) is None
    assert len(notes(conn, "owner", "Сводка")) == 1


# --- лист «Фото на проверке» --------------------------------------------------------------------------------------------------------------

class NoTelegram:
    def __init__(self):
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        raise AssertionError("в Telegram лист больше не идёт")


def test_the_review_sheet_is_a_row_of_the_owners_bell_once_per_sheet(tmp_path):
    from cryptography.fernet import Fernet
    from tires_sync import connections, users as users_
    from tires_sync.secretbox import SecretBox
    http = NoTelegram()
    box = SecretBox(Fernet.generate_key())
    app, conn, client, auth = setup(tmp_path, SECRET_BOX=box, TELEGRAM_HTTP=http)
    connections.set_config(conn, "telegram", 0, {"chat_id": "1000"}, NOW, "owner")
    connections.set_secret(conn, box, "telegram", 0, "bot_token", "123456:ABCdef-SECRET", NOW, "owner")
    users_.add_user(conn, "second1", "пароль-второго-владельца", NOW, role="owner")
    key = add_set(app, conn)
    sheet = jpeg()

    def post(data):
        return client.post("/api/review-sheet", headers=auth, data={"set_key": key, "file": (io.BytesIO(data), "s.jpg")})
    assert post(sheet).status_code == 200
    for login in OWNERS:
        [row] = notes(conn, login, "Фото на проверке: Ikon Tyres Test Model")
        assert "/photos/review" in row
    assert post(sheet).status_code == 200                                         # тот же лист ещё раз (повтор агента): без дубля
    assert len(notes(conn, "owner", "Фото на проверке")) == 1
    assert post(jpeg((10, 200, 10))).status_code == 200                           # новый выпуск того же набора: новая строка
    assert len(notes(conn, "owner", "Фото на проверке")) == 2
    assert http.calls == []
    assert client.post("/api/review-sheet", headers=auth, data={"set_key": "nope", "file": (io.BytesIO(sheet), "s.jpg")}).status_code == 404
