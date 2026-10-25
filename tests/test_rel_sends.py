"""The sendings that must not go twice and the failures the owner must hear of (the lead 2026-10-24, «надёжность внешних сервисов», lane B):
the random_id of VK made from what the message is, a mark against a repeat in Telegram (which has no key of its own), one alert for a lasting
failure of the Google table and for a bot that cannot answer the points. Fakes only: no request leaves the tests."""
from __future__ import annotations

import json
import urllib.parse

from http_fakes import ScriptedHttp
from test_order_notify import TG_OK, VK_OK, card, env, point, sent_text
from tires_sync import cli, notify, order_manager as mgr, order_notify, order_points as points, order_sheet as sheet, orders, store, vk_callback, vk_send

NOW = "2026-10-15T12:00:00"


def vk_ids(http):
    return [urllib.parse.parse_qs(c[3].decode("utf-8"))["random_id"][0] for c in http.calls]


def marks(conn):
    return conn.execute("SELECT COUNT(*) FROM app_state WHERE key LIKE 'telegram-sent:%'").fetchone()[0]


# ---- VK: the random_id ----------------------------------------------------------------------------------------------------------------

def test_the_stable_id_is_the_same_for_the_same_seed_and_fits_vk():
    assert vk_send.stable_id("a") == vk_send.stable_id("a") != vk_send.stable_id("b")
    assert all(1 <= vk_send.stable_id(str(n)) <= 2 ** 31 - 1 for n in range(500))


def test_a_repeat_of_the_same_vk_alert_in_the_hour_carries_the_same_random_id_and_another_alert_another(tmp_path):
    conn, box = env(tmp_path)
    mine = card(conn, "ghost", point(conn))
    other = card(conn, "ghost", point(conn, peer="778", name="Бета"))
    vk = ScriptedHttp(*[VK_OK] * 6)
    for c, text, now in ((mine, "Есть аналоги", NOW), (mine, "Есть аналоги", "2026-10-15T12:59:00"), (mine, "Позиция появилась", NOW),
                         (other, "Есть аналоги", NOW), (mine, "Есть аналоги", "2026-10-15T13:00:00")):
        assert order_notify.deliver(conn, box, c, text, now, tg_http=ScriptedHttp(), vk_http=vk).startswith("отправлено в ВК")
    first, repeat, another_text, another_point, next_hour = vk_ids(vk)
    assert first == repeat                                                   # VK drops the repeat of one sending
    assert len({first, another_text, another_point, next_hour}) == 4        # a real new alert is never swallowed


def test_the_bots_answer_without_the_keyboard_has_a_random_id_of_its_own_and_both_are_stable(tmp_path):
    from test_vk_commands import ready, hook
    app, client, tok, conn, pid, mine = ready(tmp_path)
    app.config["VK_HTTP"] = ScriptedHttp((200, {"error": {"error_code": 912, "error_msg": "This is a chat bot feature"}}), (200, {"response": 7}),
                                         *[(200, {"response": n}) for n in range(8, 20)])
    hook(app, conn, "Передать под заказ", event="r1", message_id=501)
    ids = vk_ids(app.config["VK_HTTP"])
    answer = mgr.signed(mgr.BUTTON_ANSWER)
    assert ids == [str(vk_send.stable_id("answer|vk-501|555|%s" % answer)), str(vk_send.stable_id("answer|vk-501|555|%s|plain" % answer))]
    seen = []
    vk_callback._answer(conn, lambda peer, text, random_id=None, keyboard=None: seen.append(random_id), "555", "Готово", "vk-501")
    vk_callback._answer(conn, lambda peer, text, random_id=None, keyboard=None: seen.append(random_id), "555", "Готово", "vk-501")
    vk_callback._answer(conn, lambda peer, text, random_id=None, keyboard=None: seen.append(random_id), "555", "Готово", "vk-502")
    assert seen[0] == seen[1] != seen[2]                                     # the same answer to the same message is one message to VK


# ---- Telegram: no repeat within the hour ------------------------------------------------------------------------------------------------

def test_a_repeat_of_the_same_telegram_alert_within_the_hour_is_not_sent_and_the_order_says_so(tmp_path):
    conn, box = env(tmp_path)
    order_notify.set_user_chat(conn, "worker2", "555", NOW, "owner")
    mine = card(conn, "worker2")
    tg = ScriptedHttp(TG_OK, TG_OK, TG_OK)
    assert order_notify.deliver(conn, box, mine, "Позиция появилась", NOW, tg_http=tg) == "отправлено в Telegram (worker2)"
    assert order_notify.deliver(conn, box, mine, "Позиция появилась", "2026-10-15T12:30:00", tg_http=tg).endswith("повтор не отправлен)")
    assert len(tg.calls) == 1
    assert any("повтор не отправлен" in e["text"] for e in orders.list_events(conn, mine["id"]))
    assert order_notify.deliver(conn, box, mine, "Есть аналоги", "2026-10-15T12:30:00", tg_http=tg) == "отправлено в Telegram (worker2)"
    assert order_notify.deliver(conn, box, mine, "Позиция появилась", "2026-10-15T13:00:01", tg_http=tg) == "отправлено в Telegram (worker2)"
    assert len(tg.calls) == 3                                                # another text, and the same one an hour later, do go


def test_the_marks_against_a_repeat_older_than_the_hour_are_removed(tmp_path):
    conn, box = env(tmp_path)
    for n in range(5):
        notify.remember_sent(conn, "k%d" % n, NOW)
    conn.commit()
    assert marks(conn) == 5
    notify.remember_sent(conn, "fresh", "2026-10-15T13:00:00")              # exactly an hour later: the old ones are past the window
    conn.commit()
    assert marks(conn) == 1 and notify.sent_recently(conn, "fresh", "2026-10-15T13:10:00")
    assert not notify.sent_recently(conn, "k1", "2026-10-15T12:10:00")


def test_the_fallback_to_the_owner_is_not_repeated_within_the_hour(tmp_path):
    conn, box = env(tmp_path)
    mine = card(conn, "ghost")                                               # no point: nobody to write to, the owner is told
    tg = ScriptedHttp(TG_OK, TG_OK)
    assert order_notify.deliver(conn, box, mine, "Есть аналоги", NOW, tg_http=tg).endswith("владельцу написано в Telegram")
    assert order_notify.deliver(conn, box, mine, "Есть аналоги", "2026-10-15T12:20:00", tg_http=tg).endswith("(повтор не отправлен)")
    assert len(tg.calls) == 1 and "Некому отправить" in sent_text(tg.calls[0])


def test_a_failed_mark_after_the_owner_was_told_of_an_unseen_alert_does_not_tell_him_twice(tmp_path, monkeypatch):
    conn, box = env(tmp_path)
    pid = point(conn, channel="panel")
    store.set_state(conn, order_notify.UNSEEN_START_KEY, "2026-10-01T00:00:00")
    mine = card(conn, point_id=pid)
    assert order_notify.deliver(conn, box, mine, "Заказ едет к вам", NOW).startswith("показано точке")
    real = points.mark_escalated

    def busy(conn, note_id, now, outcome):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(points, "mark_escalated", busy)
    tg = ScriptedHttp(TG_OK, TG_OK)
    try:
        order_notify.escalate_unseen(conn, box, "2026-10-15T12:20:00", tg_http=tg)
    except RuntimeError:
        conn.rollback()
    monkeypatch.setattr(points, "mark_escalated", real)
    assert order_notify.escalate_unseen(conn, box, "2026-10-15T12:21:00", tg_http=tg) == 1
    assert len(tg.calls) == 1                                                # the next minute marks it, without a second message


def test_the_news_to_people_is_not_repeated_within_the_hour_with_the_time_and_is_without_it(tmp_path):
    conn, box = env(tmp_path)
    order_notify.set_user_chat(conn, "worker2", "555", NOW, "owner")
    tg = ScriptedHttp(TG_OK, TG_OK, TG_OK)
    assert order_notify.tell_people(conn, box, ["worker2"], "Точка сообщила о заказе", tg, now=NOW) == "Telegram: отправлено (worker2)"
    assert order_notify.tell_people(conn, box, ["worker2"], "Точка сообщила о заказе", tg, now="2026-10-15T12:05:00") == "Telegram: отправлено (worker2)"
    assert len(tg.calls) == 1
    order_notify.tell_people(conn, box, ["worker2"], "Точка сообщила о заказе", tg)                     # no time: as before, it goes
    assert len(tg.calls) == 2


# ---- the bot of VK cannot answer: one alert, the details in the journal -------------------------------------------------------------------

def test_an_answer_that_did_not_go_is_one_alert_of_the_same_words_and_the_journal_keeps_whom_and_what(tmp_path, monkeypatch):
    from test_vk_commands import ready, hook
    app, client, tok, conn, pid, mine = ready(tmp_path)
    said = []
    monkeypatch.setattr(vk_callback, "_alarm", lambda conn, box, text: said.append(text))
    app.config["VK_HTTP"] = ScriptedHttp(*[(200, {"error": {"error_code": 901, "error_msg": "Can't send messages for users without permission"}})] * 4)
    hook(app, conn, "Передать под заказ", event="f1")
    hook(app, conn, "Передать под заказ", event="f2")
    assert said == [vk_callback.NOT_ANSWERED % "ВК не принял сообщение (код 901)"] * 2      # the same words: cli._alert keeps them 6 hours
    assert "Передать" not in said[0] and "without permission" not in said[0]
    rows = [json.loads(r[0]) for r in conn.execute("SELECT detail FROM audit WHERE action = 'vk_answer_failed' ORDER BY id")]
    assert len(rows) == 2 and rows[0]["peer"] == "555" and rows[0]["point"] == "Бета" and rows[0]["answer"] == mgr.BUTTON_ANSWER
    assert "without permission" in rows[0]["error"]


def test_no_key_of_the_community_is_the_same_alert_with_its_own_reason(tmp_path, monkeypatch):
    conn, box = env(tmp_path)
    said = []
    monkeypatch.setattr(vk_callback, "_alarm", lambda conn, box, text: said.append(text))
    from datetime import datetime
    vk_callback._not_answered(conn, box, datetime(2026, 10, 15, 9), "555", "Готово", vk_callback.NO_KEY)
    assert said == []                                                        # a stranger: only the journal
    point(conn, peer="555")
    vk_callback._not_answered(conn, box, datetime(2026, 10, 15, 9), "555", "Готово", vk_callback.NO_KEY)
    assert said == [vk_callback.NOT_ANSWERED % vk_callback.NO_KEY]
    assert conn.execute("SELECT COUNT(*) FROM audit WHERE action = 'vk_answer_failed'").fetchone()[0] == 2
    assert vk_callback._reason(vk_send.VkError("ВК недоступен: нет связи с сервером.")) == "ВК недоступен: нет связи с сервером"
    assert vk_callback._reason(KeyError("x")) == "сбой панели (KeyError)"


def test_the_alarm_of_the_bot_is_the_alert_of_the_commands(tmp_path, monkeypatch):
    conn, box = env(tmp_path)
    tg = ScriptedHttp(TG_OK)
    monkeypatch.setattr(cli, "_http", lambda: tg)
    vk_callback._alarm(conn, box, "Tires Sync: бот ВК не может ответить точкам: x.")
    vk_callback._alarm(conn, box, "Tires Sync: бот ВК не может ответить точкам: x.")
    assert len(tg.calls) == 1 and json.loads(tg.calls[0][3])["chat_id"] == "1000"        # the same text again: kept quiet (6 hours)


# ---- the Google table: a lasting failure --------------------------------------------------------------------------------------------------

def _failing(conn, card_id, since, error="Google не ответил (OSError): повторим позже."):
    conn.execute("INSERT OR REPLACE INTO sheet_order (order_id, placed_at, failing, error) VALUES (?, ?, 2, ?)", (card_id, since, error))
    orders.add_event(conn, card_id, since, sheet.AUTHOR, "sheet", "Ошибка Google-таблицы: " + error)
    conn.commit()


def test_a_failure_of_three_hours_is_one_alert_and_its_end_one_note(tmp_path):
    conn, box = env(tmp_path)
    sheet.save_settings(conn, "https://script.google.com/macros/s/AKfycbTEST_1234567890abc/exec", True, NOW, "owner")
    first, second = card(conn, "worker2"), card(conn, "worker2")
    _failing(conn, first["id"], NOW)
    assert sheet.lasting_alarm(conn, "2026-10-15T14:59:00") is None                       # under three hours: a hiccup
    text, notice = sheet.lasting_alarm(conn, "2026-10-15T15:00:00")
    assert not notice and "дольше 3 ч: №%d " % first["id"] in text and "Google не ответил" in text
    assert sheet.lasting_alarm(conn, "2026-10-15T15:30:00") == (text, False)               # not told yet (Telegram failed): said again
    sheet.alarm_told(conn, "2026-10-15T15:30:00", False)
    _failing(conn, second["id"], "2026-10-15T13:00:00")
    assert sheet.lasting_alarm(conn, "2026-10-15T16:00:00") is None                       # the same episode: no second alert
    conn.execute("UPDATE sheet_order SET failing = 0, error = ''")
    conn.commit()
    assert sheet.lasting_alarm(conn, "2026-10-15T17:00:00") == (sheet.RECOVERED, True)
    sheet.alarm_told(conn, "2026-10-15T17:00:00", True)
    assert sheet.lasting_alarm(conn, "2026-10-15T18:00:00") is None
    _failing(conn, first["id"], "2026-10-15T18:00:00")
    assert sheet.lasting_alarm(conn, "2026-10-15T21:00:00")[1] is False                    # a new episode is told again


def test_a_streak_that_began_with_rows_the_script_did_not_take_counts_too(tmp_path):
    conn, box = env(tmp_path)
    sheet.save_settings(conn, "https://script.google.com/macros/s/AKfycbTEST_1234567890abc/exec", True, NOW, "owner")
    mine = card(conn, "worker2")["id"]
    conn.execute("INSERT INTO sheet_order (order_id, placed_at, failing, error) VALUES (?, ?, 1, 'строки не приняты')", (mine, NOW))
    orders.add_event(conn, mine, NOW, sheet.AUTHOR, "sheet", "Google-таблица не приняла 2 стр.: отправим ещё раз.")
    orders.add_event(conn, mine, "2026-10-15T14:00:00", "owner", "sheet", "Ошибка Google-таблицы: чужая запись")     # not the table's own
    conn.commit()
    assert [f["order_id"] for f in sheet.lasting(conn, "2026-10-15T15:00:00")] == [mine]


def test_the_episode_ends_without_a_word_when_the_table_is_switched_off(tmp_path):
    conn, box = env(tmp_path)
    url = "https://script.google.com/macros/s/AKfycbTEST_1234567890abc/exec"
    sheet.save_settings(conn, url, True, NOW, "owner")
    _failing(conn, card(conn, "worker2")["id"], NOW)
    assert sheet.lasting_alarm(conn, "2026-10-15T16:00:00")
    sheet.save_settings(conn, url, False, NOW, "owner")
    assert sheet.lasting_alarm(conn, "2026-10-15T17:00:00") is None and not store.get_state(conn, sheet.ALERTED_KEY)


def test_the_hourly_look_sends_the_alert_of_the_table(tmp_path, monkeypatch):
    conn, box = env(tmp_path)
    said = []
    monkeypatch.setattr(sheet, "flush", lambda *a, **k: {})
    monkeypatch.setattr(sheet, "lasting_alarm", lambda conn, now: ("Tires Sync: Google-таблица…", False))
    monkeypatch.setattr(cli, "_alert", lambda conn, box, text, service=False, notice=False, recovery=False:
                        said.append((text, notice, recovery)) or True)
    cli._google_sheet(conn, None)
    assert said == [("Tires Sync: Google-таблица…", False, False)] and store.get_state(conn, sheet.ALERTED_KEY)   # a failure: not a recovery (N1)


def test_an_alert_of_the_table_that_did_not_go_is_said_again_the_next_hour(tmp_path, monkeypatch):
    conn, box = env(tmp_path)
    monkeypatch.setattr(sheet, "flush", lambda *a, **k: {})
    monkeypatch.setattr(sheet, "lasting_alarm", lambda conn, now: ("Tires Sync: Google-таблица…", False))
    monkeypatch.setattr(cli, "_alert", lambda conn, box, text, service=False, notice=False, recovery=False: False)
    cli._google_sheet(conn, None)
    assert not store.get_state(conn, sheet.ALERTED_KEY)


# ---- cli._alert: a refused Telegram is not «said already» --------------------------------------------------------------------------------

def test_an_alert_telegram_refused_goes_the_next_hour_and_true_comes_only_after_a_real_sending(tmp_path, monkeypatch):
    conn, box = env(tmp_path)
    tg = ScriptedHttp((502, {}), TG_OK, TG_OK)
    monkeypatch.setattr(cli, "_http", lambda: tg)
    clock = [1_000_000.0]
    monkeypatch.setattr(cli, "_epoch", lambda: clock[0])
    assert cli._alert(conn, box, "Tires Sync: Google-таблица…") is False
    assert not store.get_state(conn, "last_alert_text") and not store.get_state(conn, "last_alert_at")   # as before the call
    clock[0] += 3600
    assert cli._alert(conn, box, "Tires Sync: Google-таблица…") is True and len(tg.calls) == 2           # really sent this time
    clock[0] += 3600
    assert cli._alert(conn, box, "Tires Sync: Google-таблица…") is True and len(tg.calls) == 2           # now said: six hours of quiet
    assert conn.execute("SELECT COUNT(*) FROM audit WHERE action = 'alert'").fetchone()[0] == 2            # the journal keeps both tries


def test_a_refused_alert_gives_back_the_moment_of_the_last_one_that_went(tmp_path, monkeypatch):
    conn, box = env(tmp_path)
    tg = ScriptedHttp(TG_OK, (502, {}))
    monkeypatch.setattr(cli, "_http", lambda: tg)
    monkeypatch.setattr(cli, "_epoch", lambda: 1_000_000.0)
    assert cli._alert(conn, box, "первая") is True
    said = (store.get_state(conn, "last_alert_at"), store.get_state(conn, "last_alert_text"))
    monkeypatch.setattr(cli, "_epoch", lambda: 1_000_100.0)
    assert cli._alert(conn, box, "вторая") is False
    assert (store.get_state(conn, "last_alert_at"), store.get_state(conn, "last_alert_text")) == said
    assert cli._alert(conn, box, "первая") is True and len(tg.calls) == 2                                # the first is still said


def test_the_services_alert_goes_right_after_a_cycle_alert_telegram_refused(tmp_path, monkeypatch):
    conn, box = env(tmp_path)
    tg = ScriptedHttp((502, {}), TG_OK)
    monkeypatch.setattr(cli, "_http", lambda: tg)
    monkeypatch.setattr(cli, "_epoch", lambda: 1_000_000.0)
    assert cli._alert(conn, box, "цикл завершился с ошибками") is False
    monkeypatch.setattr(cli, "_epoch", lambda: 1_000_060.0)
    assert cli._alert(conn, box, "служба упала", service=True) is True and len(tg.calls) == 2       # nothing went: the service is not quieted


def test_a_refused_alert_does_not_wipe_the_marks_another_process_wrote_meanwhile(tmp_path, monkeypatch):
    conn, box = env(tmp_path)

    def busy(method, url, headers, body):                                    # while this one waits for Telegram, the cycle tells «B»
        store.set_state(conn, "last_alert_at", "1000050.0")
        store.set_state(conn, "last_alert_text", "b-digest")
        return 502, {}, b"{}"
    monkeypatch.setattr(cli, "_http", lambda: busy)
    monkeypatch.setattr(cli, "_epoch", lambda: 1_000_000.0)
    assert cli._alert(conn, box, "A") is False
    assert (store.get_state(conn, "last_alert_at"), store.get_state(conn, "last_alert_text")) == ("1000050.0", "b-digest")
