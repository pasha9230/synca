"""The day dashboard (the figures are those of analytics.py, the money only with the right), «Мои дела», «Фиды», and the morning summary
in Telegram (off by default, once a day after 9:00, to the owner and the partner)."""
from __future__ import annotations

import json
from datetime import datetime

import pytest
from cryptography.fernet import Fernet

from http_fakes import ScriptedHttp
from panel_helpers import login, make_app, post
from tires_sync import analytics, connections, daily_summary, order_notify, orders, sales, store, users
from tires_sync.secretbox import SecretBox

NOW = datetime(2026, 10, 21, 12, 0, 0)                  # Moscow
UTC_NOW = datetime(2026, 10, 21, 9, 0, 0)
PASSWORD = "длинный-пароль-1"


@pytest.fixture(autouse=True)
def cheap_hashes(monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")


def _sell(conn, card, day, total):
    sales.add_sale(conn, day=day, goods_id="g%d" % card, brand_key="nokian", season="winter", diameter=16, name="Nokian", ad_kind="single", pieces=4,
                   sale_total=total, supplier_price=4000, terms=None, fee_pct=0, promo_pct=0, account_id=None, point="", note="Заказ №%d" % card,
                   now="2026-10-21T12:00:00", author="o", today="2026-10-21", source="order")


def fill(conn):
    """Two orders today (one overdue by its stage limit), one sold today and one sold four days ago, an account that reads chats with a client
    waiting 40 minutes and one with nothing to answer."""
    orders.ensure_pipeline(conn, "2026-10-21T10:00:00")
    stage = next(s for s in orders.list_stages(conn) if s["code"] == "new")
    conn.execute("UPDATE order_stage SET limit_hours = 1 WHERE id = ?", (stage["id"],))
    a = orders.create_card(conn, {"client_name": "Иван", "responsible": "olya"}, "2026-10-21T09:00:00", "o")
    b = orders.create_card(conn, {"client_name": "Пётр"}, "2026-10-21T11:50:00", "o")
    _sell(conn, a, "2026-10-21", 20000)
    _sell(conn, b, "2026-10-17", 10000)
    account = connections.create_account(conn, "Партнёр-2", "2026-10-21T10:00:00", "o")
    connections.set_modes(conn, account, True, False, "2026-10-21T10:00:00", "o")
    conn.execute("INSERT INTO avito_chat (account_id, chat_id, peer_name, last_dir, last_at, unread, status, responsible) VALUES (?,?,?,?,?,?,?,?)",
                 (account, "c1", "Татьяна", "in", "2026-10-21T11:20:00", 1, "open", "olya"))
    conn.execute("INSERT INTO avito_chat (account_id, chat_id, peer_name, last_dir, last_at, unread, status) VALUES (?,?,?,?,?,?,?)",
                 (account, "c2", "Ольга", "out", "2026-10-21T11:00:00", 0, "open"))
    conn.commit()
    return a, b


def test_the_day_figures_are_those_of_analytics(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    a, b = fill(conn)
    day = analytics.day_numbers(conn, NOW)
    assert (day["orders_open"], day["orders_overdue"], day["orders_new_today"]) == (2, 1, 2)
    assert day["money_today"] == analytics.money(conn, "2026-10-21", "2026-10-21") and day["money_today"]["revenue"] == 20000
    assert day["money_week"]["revenue"] == 30000 == analytics.money(conn, "2026-10-15", "2026-10-21")["revenue"]
    assert day["chats_unread"] == 1 and [w["minutes"] for w in day["waiting"]] == [40] and len(day["waiting_long"]) == 1
    mine = analytics.my_day(conn, "olya", NOW)
    assert [o["id"] for o in mine["orders"]] == [a] and mine["orders"][0]["overdue"] and [c["minutes"] for c in mine["chats"]] == [40]


def test_the_dashboard_shows_the_figures_and_the_money_only_with_the_right(tmp_path):
    app = make_app(tmp_path, NOW=lambda: UTC_NOW)
    conn = store.connect(app.config["DB_PATH"])
    fill(conn)
    owner = app.test_client()
    login(owner)
    page = owner.get("/").get_data(as_text=True)
    for text in ("Заказы в работе", "Просрочено", "Новые сегодня", "Выручка", "20 000 ₽", "30 000 ₽", "Клиент ждёт ответа",
                 "дольше всех 40 мин", "Клиенты ждут ответа дольше 15 минут", "Объявления в фидах", "Мои дела на сегодня", "Утренняя сводка"):
        assert text in page, text
    users.add_user(conn, "olya", PASSWORD, "2026-10-21T10:00:00", role="chats")
    users.add_user(conn, "viewer1", PASSWORD, "2026-10-21T10:00:00", role="viewer")
    for name in ("olya", "viewer1"):
        client = app.test_client()
        with client.session_transaction() as s:
            s["user"], s["epoch"], s["csrf"] = name, store.get_session_epoch(conn, name), "tok"
        page = client.get("/").get_data(as_text=True)
        assert "Заказы в работе" in page and "Выручка" not in page and "20 000" not in page and "Утренняя сводка" not in page
        if name == "olya":
            assert "Заказ №1 · Иван" in page and "Ответить" in page                              # her own work
        assert client.post("/summary/toggle", data={"csrf": "tok", "on": "1"}).status_code == 403


def test_the_feeds_page(tmp_path):
    app = make_app(tmp_path, NOW=lambda: UTC_NOW)
    client = app.test_client()
    login(client)
    page = client.get("/feeds").get_data(as_text=True)
    assert "Фиды" in page and "Обновить фид" in page and 'href="/feeds"' in page


# --- the morning summary ------------------------------------------------------------------------------------------------------------

def _telegram(conn):
    box = SecretBox(Fernet.generate_key())
    connections.set_config(conn, "telegram", 0, {"chat_id": "1000"}, "2026-10-21T10:00:00", "owner")
    connections.set_secret(conn, box, "telegram", 0, "bot_token", "123456:ABCdef-SECRET", "2026-10-21T10:00:00", "owner")
    return box


def test_the_summary_is_off_by_default_and_goes_once_a_day_after_nine(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    fill(conn)
    box = _telegram(conn)
    users.add_user(conn, "worker2", PASSWORD, "2026-10-21T10:00:00", role="partner")
    users.add_user(conn, "olya", PASSWORD, "2026-10-21T10:00:00", role="chats")
    order_notify.set_user_chat(conn, "worker2", "555", "2026-10-21T10:00:00", "owner")
    order_notify.set_user_chat(conn, "olya", "777", "2026-10-21T10:00:00", "owner")
    http = ScriptedHttp(*[(200, {"ok": True, "result": {}})] * 10)
    assert daily_summary.is_on(conn) is False and daily_summary.maybe_send(conn, box, NOW, http=http) is None and http.calls == []
    daily_summary.set_on(conn, True, "2026-10-21T10:00:00", "owner")
    assert daily_summary.maybe_send(conn, box, datetime(2026, 10, 21, 8, 59), http=http) is None and http.calls == []     # before 9:00
    assert daily_summary.maybe_send(conn, box, datetime(2026, 10, 21, 9, 7), http=http) == "сводка: отправлено 2, не ушло 0"
    assert sorted(json.loads(c[3])["chat_id"] for c in http.calls) == ["1000", "555"]                    # the owner and the partner, not olya
    assert daily_summary.maybe_send(conn, box, datetime(2026, 10, 21, 10, 7), http=http) is None and len(http.calls) == 2   # once a day
    assert daily_summary.maybe_send(conn, box, datetime(2026, 10, 22, 12, 7), http=http) is None                            # after noon: not today
    assert daily_summary.maybe_send(conn, box, datetime(2026, 10, 23, 9, 7), http=http) is not None                         # the next morning
    text = json.loads(http.calls[0][3])["text"]
    lines = text.split("\n")
    assert 3 <= len(lines) <= 6 and "Заказы: в работе 2, просрочено 0, новых сегодня 2" in text and "ждут ответа 1" in text


def test_the_summary_text_has_the_figures_of_the_day(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    fill(conn)
    text = daily_summary.text(conn, datetime(2026, 10, 22, 9, 0))
    assert "Сводка на 22.10" in text and "Вчера: выручка 20 000 ₽" in text and "За 7 дней по вчера: выручка 30 000 ₽" in text


def test_the_owner_switches_the_summary_on_the_main_page(tmp_path):
    app = make_app(tmp_path, NOW=lambda: UTC_NOW)
    client = app.test_client()
    tok = login(client)
    assert "выключена" in client.get("/").get_data(as_text=True)
    post(client, "/summary/toggle", tok, on="1")
    conn = store.connect(app.config["DB_PATH"])
    assert daily_summary.is_on(conn) and "включена" in client.get("/").get_data(as_text=True)
    assert any(a["action"] == "daily_summary" for a in store.list_audit(conn))



def test_a_partner_sharing_the_owners_chat_gets_one_summary(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    fill(conn)
    box = _telegram(conn)
    users.add_user(conn, "worker2", PASSWORD, "2026-10-21T10:00:00", role="partner")
    order_notify.set_user_chat(conn, "worker2", "1000", "2026-10-21T10:00:00", "owner")              # the same group as the owner's
    daily_summary.set_on(conn, True, "2026-10-21T10:00:00", "owner")
    http = ScriptedHttp(*[(200, {"ok": True, "result": {}})] * 4)
    assert daily_summary.maybe_send(conn, box, datetime(2026, 10, 21, 9, 7), http=http) == "сводка: отправлено 1, не ушло 0"


@pytest.mark.parametrize("role, money_tiles, analytics_money", [("partner", True, True), ("chats", False, False), ("viewer", False, False)])
def test_the_money_by_role_and_the_point_sees_none_of_it(tmp_path, role, money_tiles, analytics_money):
    app = make_app(tmp_path, NOW=lambda: UTC_NOW)
    conn = store.connect(app.config["DB_PATH"])
    fill(conn)
    users.add_user(conn, "someone", PASSWORD, "2026-10-21T10:00:00", role=role)
    client = app.test_client()
    with client.session_transaction() as s:
        s["user"], s["epoch"], s["csrf"] = "someone", store.get_session_epoch(conn, "someone"), "tok"
    assert ("20\u00a0000\u00a0₽" in client.get("/").get_data(as_text=True)) is money_tiles
    assert ("<h3>Продажи и прибыль</h3>" in client.get("/analytics").get_data(as_text=True)) is analytics_money
    pid = conn.execute("INSERT INTO point (name) VALUES ('Бета')").lastrowid
    conn.commit()
    users.add_user(conn, "pointman", PASSWORD, "2026-10-21T10:00:00", role="point", point_id=pid)
    point = app.test_client()
    with point.session_transaction() as s:
        s["user"], s["epoch"], s["csrf"] = "pointman", store.get_session_epoch(conn, "pointman"), "tok"
    assert point.get("/analytics").status_code == 403 and point.get("/feeds").status_code == 403
    assert point.post("/summary/toggle", data={"csrf": "tok", "on": "1"}).status_code == 403



def test_avitos_auto_answer_does_not_put_out_the_waiting_on_the_dashboard(tmp_path):
    """The owner's accounts answer by themselves (an outgoing «system» message that comes last): the client still waits, as on «Чаты»."""
    conn = store.connect(str(tmp_path / "x.db"))
    fill(conn)
    tanya = conn.execute("SELECT id FROM avito_chat WHERE chat_id = 'c1'").fetchone()[0]
    for at, direction, kind in (("2026-10-21T11:20:00", "in", "text"), ("2026-10-21T11:21:00", "out", "system")):
        conn.execute("INSERT INTO avito_message (chat_row, message_id, at, ts, direction, kind, text) VALUES (?,?,?,?,?,?,'x')",
                     (tanya, "m-" + at, at, int(datetime.fromisoformat(at).timestamp()), direction, kind))
    conn.execute("UPDATE avito_chat SET last_dir = 'out', last_at = '2026-10-21T11:21:00' WHERE id = ?", (tanya,))
    conn.commit()
    assert [w["minutes"] for w in analytics.day_numbers(conn, NOW)["waiting"]] == [40]
    assert [c["minutes"] for c in analytics.my_day(conn, "olya", NOW)["chats"]] == [40]
