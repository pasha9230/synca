"""Утренняя сводка и лист «Фото на проверке» в колокольчике владельцев (lane/digest-bell) уходят оттуда сами через сутки (слово владельца
07.11, lane/digest-expire); в журнале действий («История») запись остаётся; «Разобрался» убирает раньше, как прежде. Сводку видят только
владельцы (роль owner и логин владельца для уведомлений), партнёр — нет. Другие «Оповещения» не истекают. Настоящих отправок нет."""
from __future__ import annotations

import io
import json
from datetime import datetime

from test_agent_api import add_set, jpeg, setup
from test_critical import NOW, env
from tires_sync import critical, daily_summary, order_alerts, store, users

MORNING = datetime(2026, 11, 6, 10, 0)                      # the summary is written at 10:00 Moscow


def bell(conn, login, now, part):
    return [r for r in critical.bell_rows(conn, login, now, action=critical.NOTE) if part in r["text"]]


def page(conn, login, now, part):
    return [r for r in order_alerts.page_rows(conn, login, now) if part in str(r.get("text") or "")]


def summary(tmp_path, monkeypatch):
    conn, box, http = env(tmp_path, monkeypatch)
    users.add_user(conn, "zaman", "пароль-владельца-заман", NOW, role="owner")
    users.add_user(conn, "worker2", "пароль-для-партнёра-1", NOW, role="partner")
    monkeypatch.setattr(daily_summary, "is_on", lambda conn: True)
    assert "колокольчик" in daily_summary.maybe_send(conn, box, MORNING, http=http)
    return conn


def test_the_summary_leaves_the_bell_after_a_day_and_stays_in_the_journal(tmp_path, monkeypatch):
    conn = summary(tmp_path, monkeypatch)
    for login in ("owner", "second1", "zaman"):
        assert len(bell(conn, login, "2026-11-07T09:59:00", "Сводка на 06.11")) == 1, login        # still there just before a day
        assert len(page(conn, login, "2026-11-07T09:59:00", "Сводка на 06.11")) == 1, login
        assert bell(conn, login, "2026-11-07T10:00:00", "Сводка") == [], login                       # a day: gone by itself
        assert page(conn, login, "2026-11-07T10:01:00", "Сводка") == [], login
    [detail] = [json.loads(r[0]) for r in conn.execute("SELECT detail FROM audit WHERE action = ?", (critical.NOTE,))]
    assert "Сводка на 06.11" in detail["text"]                                                      # «История» keeps it


def test_the_partner_never_sees_the_summary(tmp_path, monkeypatch):
    conn = summary(tmp_path, monkeypatch)
    assert "worker2" not in order_alerts.owner_people(conn)
    assert bell(conn, "worker2", "2026-11-06T10:30:00", "Сводка") == [] and page(conn, "worker2", "2026-11-06T10:30:00", "Сводка") == []


def test_разобрался_still_takes_it_off_at_once(tmp_path, monkeypatch):
    conn = summary(tmp_path, monkeypatch)
    [row] = bell(conn, "owner", "2026-11-06T10:30:00", "Сводка")
    critical.ack(conn, int(row["ack_href"].split("/")[3]), "owner")
    conn.commit()
    assert bell(conn, "owner", "2026-11-06T10:31:00", "Сводка") == []
    assert len(bell(conn, "second1", "2026-11-06T10:31:00", "Сводка")) == 1                         # the other owner's: as before


def test_another_note_does_not_expire(tmp_path, monkeypatch):
    conn, _box, _http = env(tmp_path, monkeypatch)
    assert critical.note(conn, ["owner"], "Заказ №5: клиент написал", "2026-11-06T10:00:00")
    assert len(bell(conn, "owner", "2026-11-09T10:00:00", "Заказ №5")) == 1                         # three days on: as before (BELL_DAYS)


def test_the_review_sheet_leaves_the_bell_after_a_day(tmp_path, monkeypatch):
    app, conn, client, auth = setup(tmp_path)
    users.add_user(conn, "second1", "пароль-второго-владельца", NOW, role="owner")
    users.add_user(conn, "worker2", "пароль-для-партнёра-1", NOW, role="partner")
    monkeypatch.setattr(store, "moscow_iso", lambda: "2026-11-06T15:00:00")
    key = add_set(app, conn)
    reply = client.post("/api/review-sheet", headers=auth, data={"set_key": key, "file": (io.BytesIO(jpeg()), "s.jpg")})
    assert reply.status_code == 200
    for login in ("owner", "second1"):
        assert len(bell(conn, login, "2026-11-07T14:59:00", "Фото на проверке")) == 1, login
        assert bell(conn, login, "2026-11-07T15:00:00", "Фото на проверке") == [], login
    assert bell(conn, "worker2", "2026-11-06T15:30:00", "Фото на проверке") == []
    assert conn.execute("SELECT COUNT(*) FROM audit WHERE action = ? AND detail LIKE '%Фото на проверке%'", (critical.NOTE,)).fetchone()[0] == 1
