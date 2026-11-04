"""The owner 2026-11-04: the 12-hour move to «На ОТХ» tells the responsible «Позвонить клиенту» with a line of the order; «Товар на точке» and
«Крайняя дата резерва» on /oth and the order's views; 6 days 12 hours on «На ОТХ» tell the responsible once; «На возврат» by hand (an order, a position)
and by itself on the seventh day with the point's question, one task, off sale in every label; the date of the return (Mon, Wed, Fri), the point's statuses,
the reminder from 08:59, the confirmation of the hand-over."""
from __future__ import annotations

import re

import pytest

from panel_helpers import agree_return, post
from test_oth_seventh_day import ENTERED, on_oth, started
from test_order_reserve import card_at, db, ok, stage
from test_reserve_noon import DAY, NEXT, ready as reserve_ready, tick
from test_vk_commands import _card, _stage, ready
from tires_sync import message_texts, order_alerts, order_manager as mgr, order_oth, order_reserve as reserve, orders, oth_stock, tasks, users

DUE = "2026-10-26T23:00:00"                           # ENTERED (2026-10-20T11:00:00) + 6 days 12 hours


def open_tasks(conn, cid):
    return [dict(zip(("title", "kind", "assignee", "due_at", "note"), r)) for r in conn.execute(
        "SELECT title, kind, assignee, due_at, note FROM callback_task WHERE order_id = ? AND done_at IS NULL AND cancelled_at IS NULL ORDER BY id", (cid,))]


def staff(conn, login="ivan"):
    users.add_user(conn, login, "password-123", ENTERED, role="partner")
    conn.commit()


def calls(store):
    def notify(card, text):
        store.append((card["responsible"], text))
        return "ok"
    return notify


# ---- 1. «Позвонить клиенту» ----------------------------------------------------------------------------------------------------------

def fresh(conn, chat, at):
    conn.execute("UPDATE avito_chat SET synced_at = ? WHERE id = ?", (at, chat))
    conn.commit()


def _moved(tmp_path):
    conn, cid, chat, deliver, vk = reserve_ready(tmp_path)
    fresh(conn, chat, NEXT + "T00:29:00")
    staff(conn)
    conn.execute("UPDATE order_card SET responsible = 'ivan', tags = 'VIP,срочно' WHERE id = ?", (cid,))
    conn.commit()
    tick(conn, DAY + "T12:30:00", deliver, vk)
    told = []
    assert reserve._no_answer_step(conn, orders.get_card(conn, cid), NEXT + "T00:30:00", calls(told))
    assert told == []                                                                      # at night: held
    reserve._release_held(conn, NEXT + "T09:07:00", calls(told))
    return conn, cid, told


def test_the_move_after_12_hours_tells_the_responsible_to_call_with_the_line(tmp_path):
    conn, cid, told = _moved(tmp_path)
    created = orders.get_card(conn, cid)["created_at"]
    line = "Nokian 205/55 R16, 4 шт.; сумма 20 000 ₽; заказ от %s.%s.%s; резерв до 18.10.2026; точка «Бета»; теги: VIP, срочно" % (
        created[8:10], created[5:7], created[:4])
    to_ivan = [t for who, t in told if who == "ivan"]
    assert to_ivan == ["Позвонить клиенту: заказ №%d переведён на «На ОТХ», клиент не ответил на напоминание о сроке резерва. %s — /orders/%d" % (
        5200000 + cid, line, cid)]
    assert [t for who, t in told if who == ""] == ["Заказ №%d переведён на «На ОТХ»: клиент не ответил на напоминание о сроке резерва" % (5200000 + cid)]
    [record] = [e for e in orders.list_events(conn, cid) if e["text"].startswith("Позвонить клиенту")]
    assert record["level"] == "yellow" and line in record["text"]
    assert not [e for e in orders.list_events(conn, cid) if "не ответил за 12 часов" in e["text"] and e["level"] != "grey"]      # one alert, not two


def test_the_call_goes_once_and_the_rules_of_the_move_are_the_same(tmp_path):
    conn, cid, told = _moved(tmp_path)
    assert orders.get_card(conn, cid)["stage"]["code"] == "oth"
    reserve.check(conn, NEXT + "T09:00:00", calls(told))                                 # not «Товар на точке» any more
    assert sum(t.startswith("Позвонить клиенту") for _who, t in told) == 1
    (tmp_path / "b").mkdir()
    conn2, cid2, chat2, deliver, vk = reserve_ready(tmp_path / "b")
    fresh(conn2, chat2, NEXT + "T00:29:00")
    tick(conn2, DAY + "T12:30:00", deliver, vk)
    late = []
    assert not reserve._no_answer_step(conn2, orders.get_card(conn2, cid2), NEXT + "T00:29:59", calls(late))
    assert late == []


def test_nobody_answers_both_owners_are_told(tmp_path):
    conn, cid, chat, deliver, vk = reserve_ready(tmp_path)
    fresh(conn, chat, NEXT + "T09:29:00")
    users.add_user(conn, "zaman", "password-123", ENTERED, role="owner")
    conn.execute("UPDATE order_card SET responsible = '', created_by = 'робот' WHERE id = ?", (cid,))
    conn.commit()
    tick(conn, DAY + "T12:30:00", deliver, vk)
    told = []
    assert reserve._no_answer_step(conn, orders.get_card(conn, cid), NEXT + "T09:30:00", calls(told))      # by day: at once
    assert sorted(who for who, t in told if t.startswith("Позвонить")) == sorted(order_alerts.owner_people(conn))


# ---- 2. the dates --------------------------------------------------------------------------------------------------------------------

def test_the_dates_survive_entering_oth_and_a_date_moved_by_hand_counts(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    orders.update_card(conn, cid, {"reserve_until": "2026-10-22"}, "2026-10-16T10:00:00", "o")
    assert order_oth.reserve_dates(conn) == {}                                            # still at the point
    card = on_oth(conn, cid)
    assert card["reserve_until"] == ""
    assert order_oth.reserve_dates(conn)[cid] == {"arrived": "2026-10-15", "until": "2026-10-22"}


def test_the_old_orders_on_oth_get_their_dates_from_the_journal(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    on_oth(conn, cid)
    conn.execute("DELETE FROM order_event WHERE order_id = ? AND text LIKE 'Резерв был до%'", (cid,))     # before this rule
    conn.commit()
    assert order_oth.reserve_dates(conn)[cid] == {"arrived": "2026-10-15", "until": "2026-10-18"}


def test_an_order_never_at_the_point_has_no_dates_and_the_page_shows_dashes(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    cid = _card(conn, pid, webmim="700200", code="moving")
    orders.move_card(conn, cid, _stage(conn, "oth"), ENTERED, "o", changes={"cancel_reason": "Отказ"})
    assert cid not in order_oth.reserve_dates(conn)
    page = client.get("/oth").get_data(as_text=True)
    assert "<th>Товар на точке</th><th>Крайняя дата резерва</th>" in page
    assert "data-oth-arrived>—<" in page and "data-oth-until>—<" in page


def test_the_page_the_board_and_the_card_show_the_dates(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    cid = _card(conn, pid, webmim="700200", code="at_point")
    conn.execute("UPDATE order_event SET at = '2026-10-15T10:00:00' WHERE order_id = ? AND text LIKE 'Резерв: товар на точке%'", (cid,))
    conn.execute("UPDATE order_card SET reserve_until = '2026-10-18' WHERE id = ?", (cid,))
    conn.commit()
    orders.move_card(conn, cid, _stage(conn, "oth"), ENTERED, "o", changes={"cancel_reason": "Отказ"})
    page = client.get("/oth").get_data(as_text=True)
    assert re.search(r"data-oth-arrived>15\.10(\.2026)?<", page) and re.search(r"data-oth-until>18\.10(\.2026)?<", page)      # the year when not this one
    assert "data-oth-dates" in client.get("/orders").get_data(as_text=True)
    assert "data-oth-dates" in client.get("/orders/%d" % cid).get_data(as_text=True)


def test_other_stages_have_no_dates_on_the_board(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    _card(conn, pid, webmim="700200", code="at_point")
    assert "data-oth-dates" not in client.get("/orders").get_data(as_text=True)


# ---- 3. 6 days 12 hours ---------------------------------------------------------------------------------------------------------------

def _held(tmp_path):
    conn = db(tmp_path)
    staff(conn)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    conn.execute("UPDATE order_card SET responsible = 'ivan' WHERE id = ?", (cid,))
    conn.commit()
    return conn, cid, on_oth(conn, cid)


def test_the_alert_comes_at_6_days_12_hours_to_the_second_and_once(tmp_path):
    conn, cid, _card_ = _held(tmp_path)
    told = []
    assert order_oth.tell_agree_due(conn, "2026-10-26T22:59:59", calls(told)) == 0 and told == []
    assert order_oth.tell_agree_due(conn, DUE, calls(told)) == 1
    assert told == [("ivan", "Нужно согласовать на возврат: заказ №%d (точка «Бета») на ОТХ с 20.10.2026 — /orders/%d" % (5200000 + cid, cid))]
    for when in (DUE, "2026-10-27T09:00:00", "2026-11-05T09:00:00"):
        assert order_oth.tell_agree_due(conn, when, calls(told)) == 0
    assert len(told) == 1
    assert orders.get_card(conn, cid)["stage"]["code"] == "oth"                            # the stage stays
    [record] = [e for e in orders.list_events(conn, cid) if e["text"].startswith("Нужно согласовать")]
    assert record["level"] == "yellow"


def test_the_hourly_tick_tells_once_with_the_move_and_a_new_stay_tells_again(tmp_path):
    conn, cid, _card_ = _held(tmp_path)
    told = []
    reserve.check(conn, "2026-10-27T09:00:00", calls(told))                                # the seventh day was missed: moved and told, one alert
    assert sum(t.startswith("Нужно согласовать") for _w, t in told) == 1 and orders.get_card(conn, cid)["stage"]["code"] == orders.RETURN_GIVEN
    reserve.check(conn, "2026-10-27T10:00:00", calls(told))
    assert sum(t.startswith("Нужно согласовать") for _w, t in told) == 1
    orders.move_card(conn, cid, stage(conn, "oth"), "2026-10-28T10:00:00", "o")
    assert order_oth.tell_agree_due(conn, "2026-11-03T22:00:00", calls(told)) == 1         # a new stay
    assert sum(t.startswith("Нужно согласовать") for _w, t in told) == 2


def test_6d12h_first_then_the_move_tells_nothing_more(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    started(conn)
    orders.move_card(conn, cid, stage(conn, "oth"), "2026-10-20T00:10:00", "o", changes={"cancel_reason": "Отказ"})     # the only case: before 00:30
    told = []
    assert order_oth.tell_agree_due(conn, "2026-10-26T12:10:00", calls(told)) == 1 and orders.get_card(conn, cid)["stage"]["code"] == "oth"
    assert order_oth.ask_seventh_day(conn, "2026-10-26T12:30:00", calls(told)) == 1
    assert len(told) == 1


def test_an_order_without_a_point_is_told_at_6d12h_and_stays(tmp_path):
    conn, cid, _card_ = _held(tmp_path)
    conn.execute("UPDATE order_card SET point_id = NULL WHERE id = ?", (cid,))
    conn.commit()
    told = []
    assert order_oth.ask_seventh_day(conn, DUE, calls(told)) == 0 and told == []
    assert order_oth.tell_agree_due(conn, DUE, calls(told)) == 1
    assert orders.get_card(conn, cid)["stage"]["code"] == "oth" and "«не указана»" in told[0][1]


def test_no_alert_for_an_agreed_or_marked_or_older_order(tmp_path):
    conn, cid, card = _held(tmp_path)
    order_oth.agree(conn, card, orders.date.fromisoformat("2026-10-21"), "2026-10-21T10:00:00", "owner")
    told = []
    assert order_oth.tell_agree_due(conn, DUE, calls(told)) == 0                           # agreed: on «На возврат» already
    other = card_at(conn, "at_point", "2026-10-15T10:00:00")
    item = on_oth(conn, other)["items"][0]["id"]
    order_oth.mark_returned(conn, item, "2026-10-21T10:00:00", "o", 1)
    assert order_oth.tell_agree_due(conn, DUE, calls(told)) == 0                           # as the question of the seventh day
    conn.execute("UPDATE app_state SET value = '2026-10-21T00:00:00' WHERE key = ?", (order_oth.SINCE_KEY,))
    third = card_at(conn, "at_point", "2026-10-15T10:00:00")
    on_oth(conn, third)                                                                    # on_oth keeps the key: entered before it
    assert order_oth.tell_agree_due(conn, DUE, calls(told)) == 0 and told == []


def test_a_broken_alert_does_not_stop_the_others_and_is_not_repeated(tmp_path):
    conn, cid, _card_ = _held(tmp_path)
    second = card_at(conn, "at_point", "2026-10-15T10:00:00")
    on_oth(conn, second)
    seen = []

    def notify(card, text):
        seen.append(card["id"])
        raise RuntimeError("no")
    assert order_oth.tell_agree_due(conn, DUE, notify) == 2
    assert order_oth.tell_agree_due(conn, DUE, notify) == 0 and sorted(seen) == sorted([cid, second])


def test_the_new_texts_are_the_owner_s_to_change(tmp_path):
    conn = db(tmp_path)
    assert {message_texts.CALL_CLIENT, message_texts.AGREE_DUE} <= set(message_texts.BY_KEY)
    assert "{line}" in message_texts.body(conn, message_texts.CALL_CLIENT) and "{link}" in message_texts.body(conn, message_texts.AGREE_DUE)
    assert "{date}" in message_texts.body(conn, message_texts.HANDOVER_REMIND)


# ---- 4. «На возврат» by hand -----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("now, days, due", [
    ("2026-11-04T15:20:30", 1, "2026-11-05T15:20"),    # Wednesday → Thursday
    ("2026-11-06T15:20:00", 1, "2026-11-09T15:20"),    # Friday → Monday
    ("2026-11-07T15:20:00", 1, "2026-11-09T15:20"),    # Saturday → Monday
    ("2026-11-08T15:20:00", 1, "2026-11-09T15:20"),    # Sunday → Monday
    ("2026-11-06T15:20:00", 3, "2026-11-11T15:20"),
])
def test_working_days(now, days, due):
    assert order_oth.working_due(now, days) == due


def test_the_setting_has_a_default_is_checked_and_a_broken_value_falls_back(tmp_path):
    conn = db(tmp_path)
    assert order_oth.task_days(conn) == 2                                                  # the owner 2026-11-04
    assert order_oth.working_due("2026-11-06T12:30:00", order_oth.task_days(conn)) == "2026-11-10T12:30"     # Friday → Tuesday
    assert order_oth.set_task_days(conn, " 3 ") == 3 and order_oth.task_days(conn) == 3
    for bad in ("0", "31", "", "1.5", "-1", "два", None, "٣"):
        with pytest.raises(order_oth.OthError):
            order_oth.set_task_days(conn, bad)
    assert order_oth.task_days(conn) == 3
    conn.execute("UPDATE app_state SET value = 'x' WHERE key = ?", (order_oth.TASK_DAYS_KEY,))
    assert order_oth.task_days(conn) == 2


def test_the_settings_page_changes_it(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    assert "Срок задачи на возврат" in client.get("/orders/settings").get_data(as_text=True)
    post(client, "/orders/settings/return-task", tok, days="4")
    assert order_oth.task_days(conn) == 4
    post(client, "/orders/settings/return-task", tok, days="99")
    assert order_oth.task_days(conn) == 4


def _on_oth_page(tmp_path, responsible="ivan"):
    app, client, tok, conn, pid, card = ready(tmp_path)
    staff(conn)
    started(conn)
    cid = _card(conn, pid, webmim="700200", code="at_point")
    conn.execute("UPDATE order_card SET responsible = ? WHERE id = ?", (responsible, cid))
    orders.move_card(conn, cid, _stage(conn, "oth"), ENTERED, "o", changes={"cancel_reason": "Отказ"})
    return app, client, tok, conn, cid


def test_the_button_of_the_card_moves_the_order_with_the_task_and_the_label(tmp_path):
    app, client, tok, conn, cid = _on_oth_page(tmp_path)
    page = client.get("/orders/%d" % cid).get_data(as_text=True)
    assert "data-to-return" in page
    order_oth.set_task_days(conn, 2)
    post(client, "/orders/%d/move" % cid, tok, stage_id=str(_stage(conn, orders.RETURN_GIVEN)))
    card = orders.get_card(conn, cid)
    assert card["stage"]["code"] == orders.RETURN_GIVEN
    [task] = open_tasks(conn, cid)
    assert task["title"] == order_oth.RETURN_TASK and task["kind"] == "return" and task["assignee"] == "ivan"
    assert tasks.KINDS[task["kind"]] == "Вернуть"
    assert order_oth.label_of(order_oth.return_plan(conn, cid)) == "Согласуем"
    assert "data-return-label>Согласуем<" in client.get("/orders?r=agree").get_data(as_text=True)
    assert "data-to-return" not in client.get("/orders/%d" % cid).get_data(as_text=True)     # already there
    assert [e for e in orders.list_events(conn, cid) if e["text"].startswith("Задача: Вернуть: " + order_oth.RETURN_TASK)]
    assert "/orders/%d" % cid in task["note"]


def test_the_window_save_and_the_due_date_of_the_setting(tmp_path):
    app, client, tok, conn, cid = _on_oth_page(tmp_path)
    order_oth.set_task_days(conn, 2)
    order_oth.moved_by_hand(conn, orders.get_card(conn, cid), "2026-11-06T15:20:00", "o")      # still on «На ОТХ»: nothing
    assert open_tasks(conn, cid) == []
    before = orders.get_card(conn, cid)
    orders.move_card(conn, cid, _stage(conn, orders.RETURN_GIVEN), "2026-11-06T15:20:00", "o")
    order_oth.moved_by_hand(conn, before, "2026-11-06T15:20:00", "o")
    assert open_tasks(conn, cid)[0]["due_at"] == "2026-11-10T15:20"


def test_nobody_answers_the_task_goes_to_the_owner(tmp_path):
    app, client, tok, conn, cid = _on_oth_page(tmp_path, responsible="")
    conn.execute("UPDATE order_card SET created_by = 'робот' WHERE id = ?", (cid,))
    conn.commit()
    post(client, "/orders/%d/move" % cid, tok, stage_id=str(_stage(conn, orders.RETURN_GIVEN)))
    assert open_tasks(conn, cid)[0]["assignee"] == "owner"


def test_no_double_task_moved_by_hand_after_the_seventh_day_nor_twice(tmp_path):
    conn = db(tmp_path)
    users.add_user(conn, "zaman", "password-123", ENTERED, role="partner")
    conn.commit()
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    before = on_oth(conn, cid)
    order_oth.ask_seventh_day(conn, "2026-10-26T12:30:00")
    order_oth.answer_here(conn, orders.get_card(conn, cid), "точка", "2026-10-26T13:00:00")
    day7 = open_tasks(conn, cid)
    assert [t["title"] for t in day7] == [order_oth.RETURN_TASK]                           # the responsible's only
    order_oth.moved_by_hand(conn, orders.get_card(conn, cid), "2026-10-26T14:00:00", "o")  # stays: no move
    order_oth.moved_by_hand(conn, before, "2026-10-26T14:00:00", "o")                      # a stale press
    assert open_tasks(conn, cid) == day7


def test_no_double_task_the_seventh_day_after_a_move_by_hand(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    before = on_oth(conn, cid)
    orders.move_card(conn, cid, stage(conn, orders.RETURN_GIVEN), "2026-10-21T10:00:00", "o")
    order_oth.moved_by_hand(conn, before, "2026-10-21T10:00:00", "o")
    orders.move_card(conn, cid, stage(conn, "oth"), "2026-10-21T11:00:00", "o")            # back; the task stays: the return is still on
    order_oth.moved_by_hand(conn, orders.get_card(conn, cid), "2026-10-21T11:00:00", "o")
    assert len(open_tasks(conn, cid)) == 1
    assert order_oth.ask_seventh_day(conn, "2026-10-27T12:30:00") == 1
    assert [t["title"] for t in open_tasks(conn, cid)] == [order_oth.RETURN_TASK]


def test_a_task_of_before_counts_and_closes(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    on_oth(conn, cid)
    tasks.add(conn, kind="other", title="Согласовать возврат", due_at="2026-10-30T10:00", note="", now=ENTERED, author="система", assignee="owner", order_id=cid)
    conn.commit()
    order_oth.ask_seventh_day(conn, "2026-10-26T12:30:00")
    assert [t["title"] for t in open_tasks(conn, cid)] == ["Согласовать возврат"]
    order_oth.agree(conn, orders.get_card(conn, cid), orders.date.fromisoformat("2026-10-27"), "2026-10-27T10:00:00", "owner")
    assert open_tasks(conn, cid) == []


def test_a_move_by_hand_has_no_question_and_a_stale_answer_marks_nothing(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    before = on_oth(conn, cid)
    orders.move_card(conn, cid, stage(conn, orders.RETURN_GIVEN), "2026-10-21T12:40:00", "o")
    order_oth.moved_by_hand(conn, before, "2026-10-21T12:40:00", "o")
    assert not order_oth.question_open(conn, orders.get_card(conn, cid))
    assert order_oth.answer_here(conn, before, "точка", "2026-10-21T13:00:00")[2] is False
    assert [t["title"] for t in open_tasks(conn, cid)] == [order_oth.RETURN_TASK]


def test_an_agreed_return_gets_no_task_and_agreeing_closes_it(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    before = on_oth(conn, cid)
    orders.move_card(conn, cid, stage(conn, orders.RETURN_GIVEN), "2026-10-21T10:00:00", "o")
    order_oth.moved_by_hand(conn, before, "2026-10-21T10:00:00", "o")
    assert open_tasks(conn, cid)
    order_oth.agree(conn, orders.get_card(conn, cid), orders.date.fromisoformat("2026-10-21"), "2026-10-21T11:00:00", "owner")
    assert open_tasks(conn, cid) == []
    assert order_oth.label_of(order_oth.return_plan(conn, cid)).startswith("Согласовано")
    order_oth.moved_by_hand(conn, before, "2026-10-21T12:00:00", "o")                         # agreed: nothing to agree
    assert open_tasks(conn, cid) == []


def test_back_to_work_cancels_the_task_by_hand_not_the_seventh_day_s(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    before = on_oth(conn, cid)
    orders.move_card(conn, cid, stage(conn, orders.RETURN_GIVEN), "2026-10-21T10:00:00", "o")
    order_oth.moved_by_hand(conn, before, "2026-10-21T10:00:00", "o")
    returning = orders.get_card(conn, cid)
    orders.move_card(conn, cid, stage(conn, "at_point"), "2026-10-21T11:00:00", "o")
    order_oth.moved_by_hand(conn, returning, "2026-10-21T11:00:00", "o")
    assert open_tasks(conn, cid) == []
    assert [e for e in orders.list_events(conn, cid) if e["text"].startswith("Задача отменена")]


def test_the_automatic_moves_make_no_task_by_hand(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    card = on_oth(conn, cid)
    order_oth.agree(conn, card, orders.date.fromisoformat("2026-10-21"), "2026-10-21T10:00:00", "owner")
    assert orders.get_card(conn, cid)["stage"]["code"] == orders.RETURN_GIVEN and open_tasks(conn, cid) == []


def test_a_broken_task_never_undoes_the_move(tmp_path, monkeypatch):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    before = on_oth(conn, cid)
    orders.move_card(conn, cid, stage(conn, orders.RETURN_GIVEN), "2026-10-21T10:00:00", "o")
    monkeypatch.setattr(tasks, "add", lambda **kw: (_ for _ in ()).throw(RuntimeError("busy")))
    order_oth.moved_by_hand(conn, before, "2026-10-21T10:00:00", "o")
    assert orders.get_card(conn, cid)["stage"]["code"] == orders.RETURN_GIVEN


# ---- 4b. a position ---------------------------------------------------------------------------------------------------------------------

def _two_positions(tmp_path):
    conn = db(tmp_path)
    staff(conn)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    conn.execute("UPDATE order_card SET responsible = 'ivan' WHERE id = ?", (cid,))
    orders.add_item(conn, cid, goods_id="2", name="Michelin 225/45 R17", brand_key="michelin", season="winter", diameter=17, territory="1",
                    qty=2, sale_unit=7000, supplier_price=6000, terms=None, fee_pct=0, promo_pct=0, now=ENTERED, author="o")
    conn.commit()
    return conn, cid, on_oth(conn, cid)


def test_a_position_keeps_the_stage_leaves_the_sale_has_the_label_and_the_task(tmp_path):
    conn, cid, card = _two_positions(tmp_path)
    item = card["items"][0]["id"]
    assert oth_stock.pieces(conn, manual=False) == {"1": 4, "2": 2}
    assert order_oth.item_to_return(conn, item, "2026-10-21T10:00:00", "o", "3") is True
    now = orders.get_card(conn, cid)
    assert now["stage"]["code"] == "oth" and order_oth.return_plan(conn, cid) is None
    assert order_oth.item_mark(order_oth.item_marks(conn), item, now["entered_at"])["qty"] == 3
    assert oth_stock.pieces(conn, manual=False) == {"1": 1, "2": 2}                        # off sale as «Согласуем» is (the owner 2026-11-04)
    assert [r["qty"] for r in order_oth.stock(conn)["returned"] if r["item_id"] == item] == [3]
    [task] = open_tasks(conn, cid)
    assert task["kind"] == "return" and task["assignee"] == "ivan" and "Nokian 205/55 R16, 3 шт." in task["note"]
    assert order_oth.item_to_return(conn, item, "2026-10-21T11:00:00", "o", "1") is False  # once while its task is open
    assert len(open_tasks(conn, cid)) == 1
    order_oth.item_to_return(conn, card["items"][1]["id"], "2026-10-21T12:00:00", "o")     # another position: its own
    assert len(open_tasks(conn, cid)) == 2


def test_the_undo_of_the_mark_cancels_the_task_and_the_label(tmp_path):
    conn, cid, card = _two_positions(tmp_path)
    item = card["items"][0]["id"]
    order_oth.item_to_return(conn, item, "2026-10-21T10:00:00", "o", "3")
    order_oth.unmark_returned(conn, item, "2026-10-22T10:00:00", "o", 1)
    assert open_tasks(conn, cid) and order_oth.item_mark(order_oth.item_marks(conn), item, card["entered_at"])
    order_oth.unmark_returned(conn, item, "2026-10-22T11:00:00", "o")
    assert open_tasks(conn, cid) == [] and order_oth.item_mark(order_oth.item_marks(conn), item, card["entered_at"]) is None
    assert oth_stock.pieces(conn, manual=False)["1"] == 4
    assert order_oth.item_to_return(conn, item, "2026-10-22T12:00:00", "o") is True        # may go again


def test_a_closed_task_ends_the_label_and_the_position_may_go_again(tmp_path):
    conn, cid, card = _two_positions(tmp_path)
    item = card["items"][0]["id"]
    order_oth.item_to_return(conn, item, "2026-10-21T10:00:00", "o", "1")
    [task_id] = [r[0] for r in conn.execute("SELECT id FROM callback_task WHERE order_id = ?", (cid,))]
    tasks.close(conn, task_id, "done", "2026-10-21T11:00:00", "o")
    assert order_oth.item_marks(conn) == {}
    assert order_oth.item_to_return(conn, item, "2026-10-22T10:00:00", "o", "1") is True
    assert [r["qty"] for r in order_oth.stock(conn)["returned"] if r["item_id"] == item] == [2]


@pytest.mark.parametrize("qty", ["0", "5", "", "x", "-1"])
def test_a_position_s_quantity_is_checked(tmp_path, qty):
    conn, cid, card = _two_positions(tmp_path)
    with pytest.raises(order_oth.OthError):
        order_oth.item_to_return(conn, card["items"][0]["id"], "2026-10-21T10:00:00", "o", qty)
    assert open_tasks(conn, cid) == [] and order_oth.item_marks(conn) == {} and not order_oth.stock(conn)["returned"]


def test_a_position_of_an_order_on_return_or_agreed_is_refused(tmp_path):
    conn, cid, card = _two_positions(tmp_path)
    order_oth.agree(conn, card, orders.date.fromisoformat("2026-10-21"), "2026-10-21T10:00:00", "owner")
    with pytest.raises(order_oth.OthError):
        order_oth.item_to_return(conn, card["items"][0]["id"], "2026-10-21T11:00:00", "o")


def test_a_new_stay_has_no_old_label(tmp_path):
    conn, cid, card = _two_positions(tmp_path)
    item = card["items"][0]["id"]
    order_oth.item_to_return(conn, item, "2026-10-21T10:00:00", "o")
    orders.move_card(conn, cid, stage(conn, "at_point"), "2026-10-22T10:00:00", "o")
    entered = orders.move_card(conn, cid, stage(conn, "oth"), "2026-10-23T10:00:00", "o") and orders.get_card(conn, cid)["entered_at"]
    assert order_oth.item_mark(order_oth.item_marks(conn), item, entered) is None


def test_the_page_buttons_and_the_rights(tmp_path):
    app, client, tok, conn, cid = _on_oth_page(tmp_path)
    item = orders.get_card(conn, cid)["items"][0]["id"]
    page = client.get("/oth").get_data(as_text=True)
    assert 'formaction="/oth/to-return"' in page
    post(client, "/oth/to-return", tok, item=str(item), qty="1")
    page = client.get("/oth").get_data(as_text=True)
    assert "data-item-return>Согласуем · 1 шт. на возврат" in page and 'formaction="/oth/to-return"' not in page
    assert "data-item-return" in client.get("/orders/%d" % cid).get_data(as_text=True)
    assert open_tasks(conn, cid)[0]["assignee"] == "ivan"
    post(client, "/oth/to-return", tok, item="999999")                                          # no such position: nothing, no error page
    assert len(open_tasks(conn, cid)) == 1
    conn.commit()


def test_a_point_has_no_button_and_cannot_move_a_position(tmp_path):
    from test_mypoint import manager
    app, client, tok, conn, cid = _on_oth_page(tmp_path)
    pid = orders.get_card(conn, cid)["point_id"]
    point = manager(app, conn, pid)
    assert "data-to-return" not in point.get("/orders/%d" % cid).get_data(as_text=True)
    item = orders.get_card(conn, cid)["items"][0]["id"]
    from flask import session  # noqa: F401
    with point.session_transaction() as sess:
        token = sess.get("csrf", "")
    answer = point.post("/oth/to-return", data={"csrf": token, "item": str(item)})
    assert answer.status_code in (302, 403)
    assert open_tasks(conn, cid) == []


# ---- 5. the owner's corrections of 2026-11-04 -------------------------------------------------------------------------------------------

def _moved_on_day7(tmp_path):
    conn, cid, card = _held(tmp_path)
    assert order_oth.ask_seventh_day(conn, "2026-10-26T12:30:00") == 1
    return conn, cid


def test_day_7_12_30_moves_not_12_29_and_the_old_orders_stay(tmp_path):
    conn = db(tmp_path)
    old = card_at(conn, "at_point", "2026-10-15T10:00:00")
    orders.move_card(conn, old, stage(conn, "oth"), "2026-10-19T10:00:00", "o", changes={"cancel_reason": "Отказ"})
    conn.execute("INSERT INTO app_state (key, value) VALUES (?, '2026-10-20T00:00:00')", (order_oth.SINCE_KEY,))
    conn.commit()
    new = card_at(conn, "at_point", "2026-10-15T10:00:00")
    orders.move_card(conn, new, stage(conn, "oth"), ENTERED, "o", changes={"cancel_reason": "Отказ"})
    assert order_oth.ask_seventh_day(conn, "2026-10-26T12:29:59") == 0
    assert orders.get_card(conn, new)["stage"]["code"] == "oth"
    assert order_oth.ask_seventh_day(conn, "2026-10-26T12:30:00") == 1
    assert orders.get_card(conn, new)["stage"]["code"] == orders.RETURN_GIVEN
    assert orders.get_card(conn, old)["stage"]["code"] == "oth"                            # entered before the rule: no mass move
    assert order_oth.ask_seventh_day(conn, "2026-10-26T13:07:00") == 0


def test_a_stale_asked_on_oth_does_not_break_the_move(tmp_path):
    conn, cid, card = _held(tmp_path)
    conn.execute("INSERT INTO app_state (key, value) VALUES (?, 'asked|2026-10-22T12:30:00')", (order_oth._ask_key(card),))
    conn.commit()
    assert order_oth.question_open(conn, card)
    assert order_oth.ask_seventh_day(conn, "2026-10-26T12:30:00") == 1
    now = orders.get_card(conn, cid)
    assert now["stage"]["code"] == orders.RETURN_GIVEN and order_oth.question_open(conn, now)


def test_a_sale_blocks_the_move_with_one_yellow_record(tmp_path):
    conn, cid, card = _held(tmp_path)
    conn.execute("UPDATE order_card SET sale_ids = '[1]' WHERE id = ?", (cid,))
    conn.commit()
    for when in ("2026-10-26T13:07:00", "2026-10-26T14:07:00"):
        assert order_oth.ask_seventh_day(conn, when) == 0
    assert orders.get_card(conn, cid)["stage"]["code"] == "oth"
    assert len([e for e in orders.list_events(conn, cid) if e["text"].startswith("7-й день на ОТХ: заказ не переведён") and e["level"] == "yellow"]) == 1
    assert not [e for e in orders.list_events(conn, cid) if "задан вопрос" in e["text"]]    # the question went back with the move


def test_silence_changes_nothing(tmp_path):
    conn, cid = _moved_on_day7(tmp_path)
    reserve.check(conn, "2026-11-02T10:00:00", calls([]))
    card = orders.get_card(conn, cid)
    assert card["stage"]["code"] == orders.RETURN_GIVEN and order_oth.question_open(conn, card) and len(open_tasks(conn, cid)) == 1


def test_issued_from_na_vozvrat_links_the_client_closes_the_task_and_no_reminder_follows(tmp_path):
    conn, cid = _moved_on_day7(tmp_path)
    assert conn.execute("SELECT 1 FROM client_order WHERE order_id = ?", (cid,)).fetchone() is None
    assert order_oth.answer_issued(conn, orders.get_card(conn, cid), "точка", "2026-10-26T13:00:00")[2]
    assert orders.get_card(conn, cid)["stage"]["code"] == "done"
    assert conn.execute("SELECT 1 FROM client_order WHERE order_id = ?", (cid,)).fetchone() is not None
    assert open_tasks(conn, cid) == []
    told = []
    assert order_oth.remind_handover(conn, "2026-11-02T08:59:00", calls(told)) == 0 and told == []


def test_issued_after_the_agreement_drops_the_plan_and_after_the_act_is_refused(tmp_path):
    conn, cid = _moved_on_day7(tmp_path)
    order_oth.agree(conn, orders.get_card(conn, cid), orders.date.fromisoformat("2026-10-26"), "2026-10-26T12:40:00", "owner")
    assert order_oth.question_open(conn, orders.get_card(conn, cid))
    assert order_oth.answer_issued(conn, orders.get_card(conn, cid), "точка", "2026-10-26T13:00:00")[2]
    assert order_oth.return_plan(conn, cid) is None and orders.get_card(conn, cid)["stage"]["code"] == "done"
    assert order_oth.remind_handover(conn, "2026-11-02T09:00:00", calls([])) == 0
    other = card_at(conn, "at_point", "2026-10-15T10:00:00")
    on_oth(conn, other, "2026-10-21T11:00:00")
    assert order_oth.ask_seventh_day(conn, "2026-10-27T12:30:00") == 1
    order_oth.agree(conn, orders.get_card(conn, other), orders.date.fromisoformat("2026-10-27"), "2026-10-27T12:40:00", "owner")
    conn.execute("UPDATE order_item SET supplier_price = 0 WHERE order_id = ?", (other,))    # no purchase: the act signed, still «На возврат»
    conn.commit()
    assert order_oth.sign_act(conn, orders.get_card(conn, other), "Бета", "2026-10-28T12:00:00", "точка") == "signed"
    outcome, answer, done = order_oth.answer_issued(conn, orders.get_card(conn, other), "точка", "2026-10-28T13:00:00")
    assert not done and "акт МХ-3 уже подписан" in answer and order_oth.return_plan(conn, other)["signed_at"]
    assert orders.get_card(conn, other)["stage"]["code"] == orders.RETURN_GIVEN


@pytest.mark.parametrize("step", ["agree", "agreed", "unagreed"])
def test_na_vozvrat_is_off_sale_in_every_label(tmp_path, step):
    conn, cid = _moved_on_day7(tmp_path)
    if step != "agree":
        order_oth.agree(conn, orders.get_card(conn, cid), orders.date.fromisoformat("2026-10-26"), "2026-10-26T13:00:00", "owner")
    if step == "unagreed":
        assert order_oth.unagree(conn, orders.get_card(conn, cid), "2026-10-26T14:00:00", "owner")
    assert oth_stock.pieces(conn) == {} and oth_stock.places(conn) == {} and oth_stock.by_point(conn) == {}
    assert order_oth.goods_on_oth(conn) == {}                                              # the catalog's mark
    assert order_oth.stock(conn)["stock"][0]["stage_code"] == orders.RETURN_GIVEN          # /oth still says where the tyres lie


def test_the_unagree_words_say_off_sale(tmp_path):
    conn, cid = _moved_on_day7(tmp_path)
    order_oth.agree(conn, orders.get_card(conn, cid), orders.date.fromisoformat("2026-10-26"), "2026-10-26T13:00:00", "owner")
    order_oth.unagree(conn, orders.get_card(conn, cid), "2026-10-26T14:00:00", "owner")
    texts = [e["text"] for e in orders.list_events(conn, cid)]
    assert any("шины не в продаже" in t for t in texts) and not any("снова в продаже" in t for t in texts)


def test_the_point_sees_the_statuses_and_no_button_before_the_agreement(tmp_path):
    from test_mypoint import manager
    app, client, tok, conn, cid = _on_oth_page(tmp_path)
    pid = orders.get_card(conn, cid)["point_id"]
    mine = manager(app, conn, pid)
    post(client, "/orders/%d/move" % cid, tok, stage_id=str(_stage(conn, orders.RETURN_GIVEN)))
    page = mine.get("/my-point?tab=return").get_data(as_text=True)
    assert "Ожидает согласование возврата" in page and "/act_signed" not in page
    agree_return(client, cid, tok)
    day = order_oth.return_plan(conn, cid)["return_date"]
    page = mine.get("/my-point?tab=return").get_data(as_text=True)
    assert "Согласовано на возврат, %s.%s" % (day[8:10], day[5:7]) in page and "/act_signed" in page


@pytest.mark.parametrize("day, words", [
    ("2026-11-05", "только в понедельник, среду или пятницу"),    # Thursday
    ("2026-11-07", "только в понедельник, среду или пятницу"),    # Saturday
    ("2026-11-02", "уже прошла"),                                 # Monday, in the past
    ("", "Укажите дату"),
    ("завтра", "Укажите дату"),
])
def test_the_date_of_the_return_is_refused_with_a_clear_message(tmp_path, day, words):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    card = on_oth(conn, cid)
    with pytest.raises(order_oth.OthError, match=words):
        order_oth.agree(conn, card, orders.date.fromisoformat("2026-11-04"), "2026-11-04T10:00:00", "owner", day)
    assert order_oth.return_plan(conn, cid) is None and orders.get_card(conn, cid)["stage"]["code"] == "oth"


def test_the_entered_date_is_kept_today_counts_and_a_second_press_keeps_it(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    card = on_oth(conn, cid)
    today = orders.date.fromisoformat("2026-11-04")
    assert order_oth.agree(conn, card, today, "2026-11-04T10:00:00", "owner", "2026-11-13") == "2026-11-13"
    assert order_oth.agree(conn, orders.get_card(conn, cid), today, "2026-11-04T10:01:00", "owner", "2026-11-04") == "2026-11-13"
    other = card_at(conn, "at_point", "2026-10-15T10:00:00")
    assert order_oth.agree(conn, on_oth(conn, other), today, "2026-11-04T10:02:00", "owner", "2026-11-04") == "2026-11-04"


def test_the_form_offers_the_day_and_the_server_refuses_another(tmp_path):
    app, client, tok, conn, cid = _on_oth_page(tmp_path)
    page = client.get("/orders/%d" % cid).get_data(as_text=True)
    offered = re.search(r'name="return_date" value="([0-9-]+)"', page).group(1)
    assert orders.date.fromisoformat(offered).weekday() in (0, 2, 4)
    r = post(client, "/orders/%d/oth-agreed" % cid, tok)                                   # no date: nothing is made up
    assert "Укажите дату" in client.get(r.headers["Location"]).get_data(as_text=True) and order_oth.return_plan(conn, cid) is None
    assert agree_return(client, cid, tok).status_code == 302 and order_oth.return_plan(conn, cid)["return_date"] == offered


def _late_return(tmp_path):
    conn, cid = _moved_on_day7(tmp_path)
    order_oth.agree(conn, orders.get_card(conn, cid), orders.date.fromisoformat("2026-11-02"), "2026-11-02T10:00:00", "owner", "2026-11-02")   # a Monday
    return conn, cid


def test_the_reminder_goes_on_the_next_supplier_days_from_08_59_once_a_day_until_pressed(tmp_path):
    conn, cid = _late_return(tmp_path)
    told = []
    for when in ("2026-11-02T09:00:00", "2026-11-03T09:00:00", "2026-11-04T08:58:59"):     # the day itself, Tuesday, a minute early
        assert order_oth.remind_handover(conn, when, calls(told)) == 0
    assert order_oth.remind_handover(conn, "2026-11-04T08:59:00", calls(told)) == 1        # Wednesday
    assert order_oth.remind_handover(conn, "2026-11-04T09:07:00", calls(told)) == 0        # once a day
    assert order_oth.remind_handover(conn, "2026-11-05T09:07:00", calls(told)) == 0        # Thursday
    assert order_oth.remind_handover(conn, "2026-11-06T09:07:00", calls(told)) == 1        # Friday (a late tick still tells)
    assert [who for who, _t in told] == ["", ""]                                           # the point's way
    assert told[0][1] == ("Заказ №%d: шины нужно передать поставщику (водителю), дата возврата была 02.11. Когда отдадите, нажмите «Подписал акт и отдал» на «Рабочем месте» (в ВК напишите: №%d возврат отдал подтверждаю)."
                          % (5200000 + cid, 5200000 + cid))
    assert order_oth.sign_act(conn, orders.get_card(conn, cid), "Бета", "2026-11-06T10:00:00", "точка") == "return"
    assert order_oth.remind_handover(conn, "2026-11-09T09:07:00", calls(told)) == 0


def test_the_tick_reminds_at_08_59_before_its_first_hour_and_nothing_else_wakes(tmp_path):
    conn, cid = _late_return(tmp_path)
    told = []
    reserve.check(conn, "2026-11-04T08:59:00", calls(told))
    assert [t for _w, t in told if "передать поставщику" in t]
    assert not order_oth.remind_handover(conn, "2026-11-04T09:30:00", calls(told))


def test_the_act_needs_the_second_step_on_the_server(tmp_path):
    from test_mypoint import manager, token, workplace
    app, client, tok, conn, cid = _on_oth_page(tmp_path)
    pid = orders.get_card(conn, cid)["point_id"]
    agree_return(client, cid, tok)
    mine = manager(app, conn, pid)
    page = workplace(mine)
    assert "Подтверждаете передачу поставщику (водителю)?" in page and 'name="handover" value="yes"' in page and 'data-handover="%d"' % cid in page
    first = token(page, cid, "act_signed")
    r = post(mine, "/my-point/%d/act_signed" % cid, "tok", token=first)                    # one click without the second step
    assert "Передача не отмечена" in mine.get(r.headers["Location"]).get_data(as_text=True)
    assert orders.get_card(conn, cid)["stage"]["code"] == orders.RETURN_GIVEN and not order_oth.return_plan(conn, cid)["signed_at"]
    post(mine, "/my-point/%d/act_signed" % cid, "tok", token=first, handover="yes")        # the token was not spent by the refusal
    assert orders.get_card(conn, cid)["stage"]["code"] == "return"


def test_the_vk_return_of_the_tyres_of_oth_asks_first(tmp_path):
    from test_vk_commands import hook
    app, client, tok, conn, pid, card = ready(tmp_path)
    cid = _card(conn, pid, webmim="700300", code="at_point")
    orders.move_card(conn, cid, _stage(conn, "oth"), ENTERED, "o", changes={"cancel_reason": "Отказ"})
    hook(app, conn, "возврат отдал 700300", event="e1")
    assert orders.get_card(conn, cid)["stage"]["code"] == "oth"
    assert any("подтверждаете передачу" in str(r[0]).lower() or "ждёт подтверждения" in str(r[0]) for r in conn.execute("SELECT outcome FROM order_command_log WHERE order_id = ?", (cid,)))
    hook(app, conn, "№700300 возврат отдал подтверждаю", event="e2")
    assert orders.get_card(conn, cid)["stage"]["code"] == "return"
    assert mgr.RETURN_SURE == "подтверждаю"


def test_the_task_of_the_seventh_day_is_due_in_two_working_days(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    started(conn)
    orders.move_card(conn, cid, stage(conn, "oth"), "2026-10-24T11:00:00", "o", changes={"cancel_reason": "Отказ"})
    assert order_oth.ask_seventh_day(conn, "2026-10-30T12:30:00") == 1                     # a Friday
    assert open_tasks(conn, cid)[0]["due_at"] == "2026-11-03T12:30"


# ---- 6. the task closes by itself on any way out ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("code, column", [("at_point", "cancelled_at"), ("done", "done_at"), ("return", "done_at")])
def test_any_manual_move_out_closes_the_task(tmp_path, code, column):
    conn, cid = _moved_on_day7(tmp_path)
    assert open_tasks(conn, cid)
    orders.move_card(conn, cid, stage(conn, code), "2026-10-27T10:00:00", "o",
                     changes={"return_kind": "supplier", "return_reason": "x", "return_amount": 1} if code == "return" else None)
    assert open_tasks(conn, cid) == []
    assert conn.execute("SELECT %s FROM callback_task WHERE order_id = ?" % column, (cid,)).fetchone()[0] == "2026-10-27T10:00:00"


def test_the_way_out_through_the_panel_and_back_and_forth_inside_keeps_it(tmp_path):
    app, client, tok, conn, cid = _on_oth_page(tmp_path)
    post(client, "/orders/%d/move" % cid, tok, stage_id=str(_stage(conn, orders.RETURN_GIVEN)))
    assert open_tasks(conn, cid)
    orders.move_card(conn, cid, _stage(conn, "oth"), "2026-10-27T10:00:00", "o")           # between the two: the return goes on
    assert open_tasks(conn, cid)
    post(client, "/orders/%d/move" % cid, tok, stage_id=str(_stage(conn, "at_point")), answer="same")
    orders.move_card(conn, cid, _stage(conn, "at_point"), "2026-10-27T11:00:00", "o")
    assert open_tasks(conn, cid) == []
    assert [e for e in orders.list_events(conn, cid) if e["text"].startswith("Задача отменена: Вернуть")]


def test_issued_still_closes_it_as_done(tmp_path):
    conn, cid = _moved_on_day7(tmp_path)
    assert order_oth.answer_issued(conn, orders.get_card(conn, cid), "точка", "2026-10-26T13:00:00")[2]
    assert conn.execute("SELECT done_at FROM callback_task WHERE order_id = ?", (cid,)).fetchone()[0] == "2026-10-26T13:00:00"


# ---- 7. the timer: extra ticks are harmless, windows hold ------------------------------------------------------------------------------

def test_the_timer_has_the_exact_times_and_no_delay():
    from pathlib import Path
    text = (Path(__file__).parent.parent / "deploy" / "tires-orders.timer").read_text(encoding="ascii")
    assert "OnCalendar=*:07:00\n" in text
    assert "OnCalendar=Mon,Wed,Fri *-*-* 08:59:00 Europe/Moscow\n" in text and "OnCalendar=*-*-* 12:30:00 Europe/Moscow\n" in text
    assert "RandomizedDelaySec=0\n" in text and "AccuracySec=1s\n" in text and "Persistent=true\n" in text


def test_a_late_catch_up_never_reminds_at_night_nor_out_of_hours(tmp_path):
    conn, cid = _late_return(tmp_path)
    told = []
    for when in ("2026-11-04T03:00:00", "2026-11-04T20:00:00", "2026-11-04T23:40:00"):     # the working hours end at 20
        reserve.check(conn, when, calls(told))
    assert told == []
    assert order_oth.remind_handover(conn, "2026-11-04T19:59:00", calls(told)) == 1        # late, in the hours: still that day, once
    assert order_oth.remind_handover(conn, "2026-11-04T19:59:30", calls(told)) == 0


def test_the_extra_ticks_repeat_nothing(tmp_path):
    conn, cid, _chat, deliver, vk = reserve_ready(tmp_path)
    told = []

    def go(when):
        deliver.at = when
        reserve.check(conn, when, calls(told), deliver, vk)
    go("2026-10-17T08:59:00")                                                               # before the first hour: nothing
    assert told == [] and deliver.sent == [] and vk.sent == []
    go("2026-10-17T09:07:00")
    before = len(told)
    go("2026-10-17T12:30:00")                                                               # the day before: told at 09:07 already
    assert len(told) == before and deliver.sent == []
    go(DAY + "T12:30:00")
    assert len(deliver.sent) == 1                                                           # the client's reminder exactly at 12:30
    go(DAY + "T13:07:00")
    assert len(deliver.sent) == 1


def test_the_client_reminder_is_not_earlier_nor_out_of_hours_after_a_catch_up(tmp_path):
    conn, cid, _chat, deliver, vk = reserve_ready(tmp_path)
    for when in (DAY + "T12:29:59", DAY + "T21:00:00", DAY + "T02:00:00"):
        deliver.at = when
        reserve.check(conn, when, calls([]), deliver, vk)
    assert deliver.sent == []


def _silent(tmp_path):
    conn, cid, chat, deliver, vk = reserve_ready(tmp_path)
    staff(conn)
    conn.execute("UPDATE order_card SET responsible = 'ivan' WHERE id = ?", (cid,))
    conn.commit()
    tick(conn, DAY + "T12:30:00", deliver, vk)
    conn.execute("UPDATE avito_chat SET synced_at = '2099-01-01T00:00:00'")
    conn.commit()
    return conn, cid, deliver, vk


def test_the_night_move_is_on_time_and_its_alerts_wait_for_nine_once(tmp_path):
    conn, cid, deliver, vk = _silent(tmp_path)
    told = []
    reserve.check(conn, NEXT + "T00:29:00", calls(told), deliver, vk)
    assert orders.get_card(conn, cid)["stage"]["code"] == "at_point"
    reserve.check(conn, NEXT + "T00:37:00", calls(told), deliver, vk)
    assert orders.get_card(conn, cid)["stage"]["code"] == "oth" and told == []              # nobody is woken
    reserve.check(conn, NEXT + "T08:59:00", calls(told), deliver, vk)
    assert told == []
    reserve.check(conn, NEXT + "T09:07:00", calls(told), deliver, vk)
    assert sorted(who for who, _t in told) == ["", "ivan"] and any(t.startswith("Позвонить клиенту") for _w, t in told)
    reserve.check(conn, NEXT + "T10:07:00", calls(told), deliver, vk)
    assert len(told) == 2


def test_an_order_taken_back_before_nine_has_no_call(tmp_path):
    conn, cid, deliver, vk = _silent(tmp_path)
    told = []
    reserve.check(conn, NEXT + "T01:07:00", calls(told), deliver, vk)
    orders.move_card(conn, cid, stage(conn, "at_point"), NEXT + "T08:00:00", "o")
    reserve.check(conn, NEXT + "T09:07:00", calls(told), deliver, vk)
    assert not [t for _w, t in told if t.startswith("Позвонить")]


def test_a_broken_held_row_stops_nothing(tmp_path):
    conn, cid, deliver, vk = _silent(tmp_path)
    conn.execute("INSERT INTO app_state (key, value) VALUES ('reserve_moved_tell:999999', 'x')")
    conn.commit()
    reserve.check(conn, NEXT + "T09:07:00", calls([]), deliver, vk)
    assert conn.execute("SELECT 1 FROM app_state WHERE key LIKE 'reserve_moved_tell:%'").fetchone() is None

