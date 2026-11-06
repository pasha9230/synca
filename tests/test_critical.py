"""«Критичное» (слово владельца 06.11): в Telegram только критичное; критичное ещё и в ВК тем, у кого привязана страница, и в колокольчик;
остальное остаётся в колокольчике и журнале. Настройка в одном месте (critical.py). Сбой оповещения не роняет цикл и не порождает нового
оповещения. Настоящих отправок нет: транспорт подменён."""
from __future__ import annotations

import json
import urllib.parse

import pytest
from cryptography.fernet import Fernet

from tires_sync import cli, critical, daily_summary, order_alerts, order_notify, orders, store, users
from tires_sync.secretbox import SecretBox

NOW = "2026-11-06T12:00:00"
BOT = "123456:ABCdef-SECRET"
VK_KEY = "vk1.a.COMMUNITY"


class Http:
    """Отвечает по адресу: Telegram и ВК; `tg_down` — Telegram недоступен (как с сервера с 01.11)."""

    def __init__(self, tg_down=False, vk_down=False):
        self.tg, self.vk, self.tg_down, self.vk_down = [], [], tg_down, vk_down

    def __call__(self, method, url, headers, body):
        if "api.telegram.org" in url:
            if self.tg_down:
                raise OSError("no route")
            self.tg.append(json.loads(body)["chat_id"] if body else url)
            return 200, {}, b'{"ok": true, "result": {}}'
        if "api.vk.com" in url:
            if self.vk_down:
                return 200, {}, b'{"error": {"error_code": 901, "error_msg": "Can\'t send"}}'
            fields = urllib.parse.parse_qs(body.decode())
            self.vk.append((fields["peer_id"][0], fields["message"][0]))
            return 200, {}, b'{"response": 7}'
        raise AssertionError("неожиданный адрес: " + url)


def env(tmp_path, monkeypatch, **kw):
    from tires_sync import connections
    conn = store.connect(str(tmp_path / "x.db"))
    orders.ensure_pipeline(conn, NOW)
    box = SecretBox(Fernet.generate_key())
    connections.set_config(conn, "telegram", 0, {"chat_id": "1000"}, NOW, "owner")
    connections.set_secret(conn, box, "telegram", 0, "bot_token", BOT, NOW, "owner")
    connections.set_secret(conn, box, "vk", 0, "token", VK_KEY, NOW, "owner")
    users.add_user(conn, "zaman", "пароль-второго-владельца", NOW, role="owner")
    http = Http(**kw)
    monkeypatch.setattr(cli, "_http", lambda: http)
    monkeypatch.setattr(cli, "moscow_now", lambda: __import__("datetime").datetime.fromisoformat(NOW))
    return conn, box, http


def audit(conn, action):
    return [e for e in store.list_audit(conn, 100) if e["action"] == action]


# --- что идёт в Telegram ---------------------------------------------------------------------------------------------------------------

def test_a_notice_that_is_not_critical_goes_to_the_journal_only(tmp_path, monkeypatch):
    conn, box, http = env(tmp_path, monkeypatch)
    critical.set_user_vk(conn, "owner", "501", NOW, "owner")
    assert cli._alert(conn, box, "Tires Sync: новые модели не сопоставлены (1): X.", notice=True) is True
    assert cli._alert(conn, box, "Tires Sync: 4 точки: загрузка не удалась.") is True
    assert http.tg == [] and http.vk == []
    assert [e["detail"]["text"] for e in audit(conn, "alert")] == ["Tires Sync: 4 точки: загрузка не удалась."]
    assert audit(conn, "critical") == []                                          # не критичное: не в красный колокольчик


def test_a_critical_alert_goes_to_telegram_to_vk_of_who_has_it_and_to_the_bell(tmp_path, monkeypatch):
    conn, box, http = env(tmp_path, monkeypatch)
    critical.set_user_vk(conn, "owner", "501", NOW, "owner")                     # у второго владельца ВК нет
    text = "Tires Sync: цикл завершился с ошибками.\nОсновной: фид не принят"
    assert cli._alert(conn, box, text, critical=True) is True
    assert http.tg == ["1000"] and http.vk == [("501", text)]
    rows = order_alerts.page_rows(conn, "zaman", NOW)
    assert [(r["kind"], r["level"], r["text"]) for r in rows] == [("critical", "red", text)]
    assert critical.without_vk(conn, order_alerts.owner_people(conn)) == ["zaman"]


def test_telegram_down_the_critical_still_reaches_vk_and_the_bell_once(tmp_path, monkeypatch, capsys):
    conn, box, http = env(tmp_path, monkeypatch, tg_down=True)
    critical.set_user_vk(conn, "owner", "501", NOW, "owner")
    text = "Tires Sync: служба tires-cycle.service завершилась с ошибкой."
    assert cli._alert(conn, box, text, service=True, critical=True) is True
    assert "Telegram недоступен" in capsys.readouterr().err
    assert http.vk == [("501", text)] and len(audit(conn, "critical")) == 1
    store.set_state(conn, "last_alert_at", "")                                   # другой текст между ними: прежняя пауза не мешает
    cli._alert(conn, box, text, critical=True)
    cli._alert(conn, box, text, critical=True)
    assert http.vk == [("501", text)]                                            # один и тот же текст дважды в ВК не шлём
    assert len(audit(conn, "critical")) == 1


def test_two_logins_of_one_vk_page_get_the_text_once(tmp_path, monkeypatch):
    conn, box, http = env(tmp_path, monkeypatch)
    critical.set_user_vk(conn, "owner", "501", NOW, "owner")
    critical.set_user_vk(conn, "zaman", "501", NOW, "owner")
    out = critical.tell(conn, box, ["owner", "zaman"], "Tires Sync: сбой", NOW, vk_http=http)
    assert http.vk == [("501", "Tires Sync: сбой")] and out["vk"] == ["owner", "zaman"]
    critical.tell(conn, box, ["owner"], "Tires Sync: сбой", NOW, vk_http=http)       # повтор вызова: тоже не дважды
    assert len(http.vk) == 1


def test_a_failed_vk_is_printed_never_raises_and_makes_no_new_alert(tmp_path, monkeypatch, capsys):
    conn, box, http = env(tmp_path, monkeypatch, vk_down=True)
    critical.set_user_vk(conn, "owner", "501", NOW, "owner")
    assert cli._alert(conn, box, "Tires Sync: цикл завершился с ошибками.", critical=True) is True
    assert "ВК «owner» не принял" in capsys.readouterr().err
    assert len(audit(conn, "alert")) == 1 and len(audit(conn, "critical")) == 1

    def broken(*a, **k):
        raise RuntimeError("база занята")
    monkeypatch.setattr(critical, "vk_peers", broken)
    assert critical.tell(conn, box, ["owner"], "Tires Sync: другой сбой", NOW, vk_http=http)["vk"] == []


def test_the_vk_page_is_checked_and_can_be_removed(tmp_path, monkeypatch):
    conn, _box, _http = env(tmp_path, monkeypatch)
    critical.set_user_vk(conn, "owner", " 501 ", NOW, "owner")
    assert critical.vk_peers(conn) == {"owner": "501"}
    for bad in ("id501", "-5", "0", "1" * 20):
        with pytest.raises(orders.OrderError):
            critical.set_user_vk(conn, "owner", bad, NOW, "owner")
    critical.set_user_vk(conn, "owner", "", NOW, "owner")
    assert critical.vk_peers(conn) == {}
    order_notify.set_user_chat(conn, "zaman", "555", NOW, "owner")              # ВК и чаты в одной настройке: друг друга не стирают
    critical.set_user_vk(conn, "zaman", "502", NOW, "owner")
    order_notify.set_user_chat(conn, "zaman", "556", NOW, "owner")
    assert order_notify.user_chats(conn) == {"zaman": "556"} and critical.vk_peers(conn) == {"zaman": "502"}


# --- остальное в Telegram больше не идёт ---------------------------------------------------------------------------------------------

def test_an_order_alert_is_no_longer_sent_to_telegram_it_is_in_the_bell(tmp_path, monkeypatch):
    conn, box, http = env(tmp_path, monkeypatch)
    users.add_user(conn, "worker2", "пароль-для-партнёра-1", NOW)
    order_notify.set_user_chat(conn, "worker2", "555", NOW, "owner")
    card = orders.get_card(conn, orders.create_card(conn, {"client_name": "Иван", "responsible": "worker2"}, NOW, "owner"))
    how = order_notify.deliver(conn, box, card, "Резерв на точке заканчивается", NOW, tg_http=http, vk_http=http)
    assert how == "в колокольчике (worker2)" and http.tg == []
    rows = order_alerts.page_rows(conn, "worker2", NOW)
    assert any(r["level"] == "yellow" and "Резерв на точке заканчивается" in r["text"] for r in rows)


def test_nobody_to_write_to_is_not_told_in_telegram_either(tmp_path, monkeypatch):
    conn, box, http = env(tmp_path, monkeypatch)
    card = orders.get_card(conn, orders.create_card(conn, {"client_name": "Иван", "responsible": "robot"}, NOW, "robot"))
    how = order_notify.deliver(conn, box, card, "Шина появилась", NOW, tg_http=http, vk_http=http)
    assert how.startswith("не отправлено") and http.tg == [] and http.vk == []


def test_tell_people_sends_only_the_critical(tmp_path, monkeypatch):
    conn, box, http = env(tmp_path, monkeypatch)
    assert order_notify.tell_people(conn, box, ["owner"], "Новая задача", tg_http=http).startswith("Telegram: не отправляется")
    assert http.tg == []
    assert order_notify.tell_people(conn, box, ["owner"], "Закупка изменилась", tg_http=http, critical=True).startswith("Telegram: отправлено")
    assert http.tg == ["1000"]


def test_the_morning_summary_no_longer_goes_to_telegram(tmp_path, monkeypatch):
    from datetime import datetime
    conn, box, http = env(tmp_path, monkeypatch)
    monkeypatch.setattr(daily_summary, "is_on", lambda conn: True)
    assert "не отправлена" in daily_summary.maybe_send(conn, box, datetime(2026, 11, 6, 10, 0), http=http)
    assert http.tg == []


def test_the_one_setting_brings_back_everything_in_telegram(tmp_path, monkeypatch):
    conn, box, http = env(tmp_path, monkeypatch)
    monkeypatch.setattr(critical, "TELEGRAM_ONLY_CRITICAL", False)
    cli._alert(conn, box, "Tires Sync: новые модели не сопоставлены (1): X.", notice=True)
    assert http.tg == ["1000"]


# --- красные сбои по заказам ---------------------------------------------------------------------------------------------------------

def test_a_failed_send_to_a_client_and_a_red_rule_go_once_to_the_responsible(tmp_path, monkeypatch):
    conn, box, http = env(tmp_path, monkeypatch)
    users.add_user(conn, "worker2", "пароль-для-партнёра-1", NOW)
    order_notify.set_user_chat(conn, "worker2", "555", NOW, "owner")
    critical.set_user_vk(conn, "worker2", "503", NOW, "owner")
    card_id = orders.create_card(conn, {"client_name": "Иван", "responsible": "worker2"}, NOW, "owner")
    orders.add_event(conn, card_id, NOW, "система", "chat", "Сообщение клиенту не отправлено: старое", level="red")
    conn.commit()
    assert critical.forward_red(conn, box, NOW, tg_http=http, vk_http=http) == 0    # первый запуск только запоминает
    orders.add_event(conn, card_id, NOW, "система", "chat", "Сообщение клиенту не отправлено: Авито отказал", level="red")
    orders.add_event(conn, card_id, NOW, "правило №3", "rule", "Задача не подготовлена: Напомнить", level="red")
    orders.add_event(conn, card_id, NOW, "система", "chat", "Клиент написал: привет", level="yellow")    # не сбой
    conn.commit()
    assert critical.forward_red(conn, box, NOW, tg_http=http, vk_http=http) == 2
    assert http.tg == ["555"] and len(http.vk) == 1 and http.vk[0][0] == "503"
    assert "Авито отказал" in http.vk[0][1] and "Напомнить" in http.vk[0][1] and "старое" not in http.vk[0][1] and "привет" not in http.vk[0][1]
    assert critical.forward_red(conn, box, NOW, tg_http=http, vk_http=http) == 0
    assert len(http.tg) == 1 and len(http.vk) == 1


def test_a_red_failure_of_an_order_nobody_answers_for_goes_to_both_owners(tmp_path, monkeypatch):
    conn, box, http = env(tmp_path, monkeypatch, tg_down=True)
    critical.set_user_vk(conn, "zaman", "502", NOW, "owner")
    critical.forward_red(conn, box, NOW, tg_http=http, vk_http=http)
    card_id = orders.create_card(conn, {"client_name": "Иван", "responsible": "robot"}, NOW, "robot")
    orders.add_event(conn, card_id, NOW, "система", "chat", "Сообщение клиенту не отправлено: нет связи", level="red")
    conn.commit()
    assert critical.forward_red(conn, box, NOW, tg_http=http, vk_http=http) == 1
    assert [p for p, _ in http.vk] == ["502"]


# --- колокольчик ---------------------------------------------------------------------------------------------------------------------

def test_the_bell_row_of_the_critical_goes_on_разобрался(tmp_path):
    from panel_helpers import login, make_app, post
    app = make_app(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    critical.tell(conn, None, ["owner"], "Tires Sync: первый сбой", NOW)
    critical.tell(conn, None, ["owner"], "Tires Sync: второй сбой", NOW)
    rows = critical.bell_rows(conn, "owner")
    assert len(rows) == 1 and rows[0]["more"] == 1 and rows[0]["text"] == "Tires Sync: второй сбой"
    assert critical.bell_rows(conn, "zaman") == []
    client = app.test_client()
    tok = login(client)
    assert "Tires Sync: второй сбой" in client.get("/alerts").get_data(as_text=True)
    assert post(client, rows[0]["ack_href"], tok).status_code == 302
    assert critical.bell_rows(conn, "owner") == []


def test_the_vk_page_is_saved_with_the_chat_on_the_telegram_page(tmp_path):
    from panel_helpers import login, make_app, post
    app = make_app(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    users.add_user(conn, "zaman", "пароль-второго-владельца", NOW, role="owner")
    client = app.test_client()
    tok = login(client)
    post(client, "/connections/telegram/user-chat", tok, login="zaman", chat_id="555", vk_peer="502")
    assert critical.vk_peers(conn) == {"zaman": "502"} and order_notify.user_chats(conn) == {"zaman": "555"}
    assert 'value="502"' in client.get("/connections/telegram").get_data(as_text=True)


def test_a_purchase_change_goes_to_vk_of_who_may_see_it_with_its_own_bell_row_only(tmp_path, monkeypatch):
    from helpers import make_offer
    from tires_sync import catalog_archive, purchase_alert
    from tires_sync.webmim import ParseResult
    conn, box, http = env(tmp_path, monkeypatch, tg_down=True)
    users.add_user(conn, "partner1", "пароль-партнёра-длинный", NOW, role="partner")
    critical.set_user_vk(conn, "partner1", "504", NOW, "owner")
    critical.set_user_vk(conn, "owner", "501", NOW, "owner")
    for at, price in (("2026-11-05T10:00:00", 1000.0), ("2026-11-05T11:00:00", 1100.0)):
        catalog_archive.record(conn, ParseResult(offers={"a": make_offer(goods_id="a", name="Шина a", base_price=price)}), at)
    assert purchase_alert.collect(conn, NOW) is None                            # первый запуск только запоминает
    catalog_archive.record(conn, ParseResult(offers={"a": make_offer(goods_id="a", name="Шина a", base_price=1300.0)}), "2026-11-06T11:00:00")
    cli._alert_purchase(conn, box, now=NOW)
    assert sorted(p for p, _ in http.vk) == ["501", "504"] and all("Закупка у поставщика изменилась" in t for _, t in http.vk)
    assert audit(conn, "critical") == []                                         # своя красная строка у закупки, второй не нужно
    assert [r["kind"] for r in order_alerts.page_rows(conn, "partner1", NOW) if r["level"] == "red"] == ["purchase"]
