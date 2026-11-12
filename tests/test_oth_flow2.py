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
    assert [t for who, t in told if who == ""] == []                                       # the point hears nothing (the owner 2026-11-10)
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
    assert order_oth.reserve_dates(conn)[cid] == {"arrived": "2026-10-15", "until": "2026-10-22", "oth_since": "2026-10-20"}


def test_the_old_orders_on_oth_get_their_dates_from_the_journal(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    on_oth(conn, cid)
    conn.execute("DELETE FROM order_event WHERE order_id = ? AND text LIKE 'Резерв был до%'", (cid,))     # before this rule
    conn.commit()
    assert order_oth.reserve_dates(conn)[cid] == {"arrived": "2026-10-15", "until": "2026-10-18", "oth_since": "2026-10-20"}


def test_an_order_never_at_the_point_has_no_dates_and_the_page_shows_dashes(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    cid = _card(conn, pid, webmim="700200", code="moving")
    orders.move_card(conn, cid, _stage(conn, "oth"), ENTERED, "o", changes={"cancel_reason": "Отказ"})
    assert order_oth.reserve_dates(conn)[cid] == {"arrived": "", "until": "", "oth_since": "2026-10-20"}
    page = client.get("/oth").get_data(as_text=True)
    assert "Товар на точке" not in page and "Крайняя дата резерва" not in page              # no reserve columns on «На ОТХ» (п. 22, П19, 2026-11-08)


def test_the_page_the_board_and_the_card_show_the_dates(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    cid = _card(conn, pid, webmim="700200", code="at_point")
    conn.execute("UPDATE order_event SET at = '2026-10-15T10:00:00' WHERE order_id = ? AND text LIKE 'Резерв: товар на точке%'", (cid,))
    conn.execute("UPDATE order_card SET reserve_until = '2026-10-18' WHERE id = ?", (cid,))
    conn.commit()
    orders.move_card(conn, cid, _stage(conn, "oth"), ENTERED, "o", changes={"cancel_reason": "Отказ"})
    assert order_oth.reserve_dates(conn)[cid]["arrived"] == "2026-10-15"                    # the page has no reserve columns since 2026-11-08 (П19)
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
    assert len([t for _w, t in told if t.startswith("Нужно согласовать")]) == 1


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
    assert order_oth.label_of(order_oth.return_plan(conn, cid)) == "Ожидает согласование возврата"
    assert "data-return-label>Ожидает согласование возврата<" in client.get("/orders?r=agree").get_data(as_text=True)
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
    assert len(open_tasks(conn, cid)) == 1
    order_oth.ask_seventh_day(conn, "2026-10-21T13:00:00")                                 # not on «На ОТХ»: nothing more
    assert len(open_tasks(conn, cid)) == 1
    orders.move_card(conn, cid, stage(conn, "oth"), "2026-10-21T14:00:00", "o")            # back on sale: that task is over (review D3)
    assert open_tasks(conn, cid) == []
    assert order_oth.ask_seventh_day(conn, "2026-10-27T12:30:00") == 1                     # the new stay has its own, with its own due
    [task] = open_tasks(conn, cid)
    assert task["title"] == order_oth.RETURN_TASK and task["due_at"] == "2026-10-29T12:30"


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


def test_a_move_by_hand_asks_the_question_too(tmp_path):
    """Was: a move by hand had no question. The owner 2026-11-07: the point is asked as on the seventh day (test_return_tweaks, п. 4)."""
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    before = on_oth(conn, cid)
    orders.move_card(conn, cid, stage(conn, orders.RETURN_GIVEN), "2026-10-21T12:40:00", "o")
    order_oth.moved_by_hand(conn, before, "2026-10-21T12:40:00", "o")
    assert order_oth.question_open(conn, orders.get_card(conn, cid))
    assert order_oth.answer_here(conn, before, "точка", "2026-10-21T13:00:00")[2] is True
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
    assert oth_stock.pieces(conn, manual=False) == {"1": 1, "2": 2}  # the owner 2026-11-10 (later): a mark «На возврат» is sold by the point only, not on Avito
    assert [(r["qty"], r["left"], r["marked_qty"]) for r in order_oth.stock(conn)["stock"] if r["item_id"] == item] == [(4, 1, 3)]
    assert [r["qty"] for r in order_oth.stock(conn)["returned"] if r["item_id"] == item] == [3]
    [task] = open_tasks(conn, cid)
    assert task["kind"] == "return" and task["assignee"] == "ivan" and "Nokian 205/55 R16, 3 шт." in task["note"]
    assert order_oth.item_to_return(conn, item, "2026-10-21T11:00:00", "o", "1") is False  # once while its task is open
    assert len(open_tasks(conn, cid)) == 1
    order_oth.item_to_return(conn, card["items"][1]["id"], "2026-10-21T12:00:00", "o")     # another position: one open task per order (the owner 2026-11-05)
    [task] = open_tasks(conn, cid)
    assert "Nokian 205/55 R16, 3 шт." in task["note"] and "Michelin 225/45 R17, 2 шт." in task["note"]
    assert order_oth.item_mark(order_oth.item_marks(conn), card["items"][1]["id"], now["entered_at"])


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
    assert 'formaction="/oth/to-return"' not in page
    assert "data-item-return>Ожидает согласование возврата · 1 шт. на возврат" in client.get("/oth?tab=returned").get_data(as_text=True)
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
    page = mine.get("/my-point?tab=agree").get_data(as_text=True)                         # «Согласование возврата»: its own tab (the owner 2026-11-12)
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


def _vk_order(tmp_path, code="oth"):
    app, client, tok, conn, pid, card = ready(tmp_path)
    started(conn)
    cid = _card(conn, pid, webmim="700300", code="at_point")
    orders.move_card(conn, cid, _stage(conn, "oth"), ENTERED, "o", changes={"cancel_reason": "Отказ"})
    if code == orders.RETURN_GIVEN:
        orders.move_card(conn, cid, _stage(conn, code), "2026-10-21T10:00:00", "o")
    return app, conn, cid


def _outcomes(conn, cid):
    return [r[0] for r in conn.execute("SELECT outcome FROM order_command_log WHERE order_id = ? ORDER BY rowid", (cid,))]


@pytest.mark.parametrize("code", ["oth", orders.RETURN_GIVEN])
@pytest.mark.parametrize("text", ["возврат отдал 700300", "№700300 возврат отдал подтверждаю"])
def test_the_vk_return_without_an_agreement_is_refused(tmp_path, code, text):
    from test_vk_commands import hook
    app, conn, cid = _vk_order(tmp_path, code)
    hook(app, conn, text)
    card = orders.get_card(conn, cid)
    assert card["stage"]["code"] == code and not float(card["return_amount"] or 0) and order_oth.return_plan(conn, cid) is None
    assert _outcomes(conn, cid) == ["не выполнено: возврат ещё не согласован"]


def test_the_vk_return_of_an_agreed_order_asks_first(tmp_path):
    from test_vk_commands import hook
    app, conn, cid = _vk_order(tmp_path, orders.RETURN_GIVEN)
    order_oth.agree(conn, orders.get_card(conn, cid), orders.date.fromisoformat("2026-10-21"), "2026-10-21T11:00:00", "owner")
    hook(app, conn, "возврат отдал 700300", event="e1")
    assert orders.get_card(conn, cid)["stage"]["code"] == orders.RETURN_GIVEN
    assert _outcomes(conn, cid) == ["ждёт подтверждения: шины не отмечены отданными"]
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

# «Выполнено» with no agreement: sold before it, nothing was agreed — cancelled (the owner 2026-11-10; was done)
@pytest.mark.parametrize("code, column", [("at_point", "cancelled_at"), ("done", "cancelled_at"), ("return", "done_at")])
def test_any_manual_move_out_closes_the_task(tmp_path, code, column):
    conn, cid = _moved_on_day7(tmp_path)
    assert open_tasks(conn, cid)
    orders.move_card(conn, cid, stage(conn, code), "2026-10-27T10:00:00", "o",
                     changes={"return_kind": "supplier", "return_reason": "x", "return_amount": 1} if code == "return" else None)
    assert open_tasks(conn, cid) == []
    assert conn.execute("SELECT %s FROM callback_task WHERE order_id = ?" % column, (cid,)).fetchone()[0] == "2026-10-27T10:00:00"


def test_the_way_out_after_a_move_through_the_panel(tmp_path):
    app, client, tok, conn, cid = _on_oth_page(tmp_path)
    post(client, "/orders/%d/move" % cid, tok, stage_id=str(_stage(conn, orders.RETURN_GIVEN)))
    assert open_tasks(conn, cid)
    orders.move_card(conn, cid, _stage(conn, "at_point"), "2026-10-27T11:00:00", "o")
    assert open_tasks(conn, cid) == []
    assert [e for e in orders.list_events(conn, cid) if e["text"].startswith("Задача отменена: Вернуть")]


def test_issued_before_the_agreement_closes_it_as_not_needed(tmp_path):
    """«Выдали» with no agreement: the client took the tyres, the task is done as not needed — «Заказ выдан, возврат не нужен», never «продано
    до согласования», which is a sale's only (the owner 2026-11-10, review of run/2026-11-10f; the run had it cancelled)."""
    conn, cid = _moved_on_day7(tmp_path)
    assert order_oth.answer_issued(conn, orders.get_card(conn, cid), "точка", "2026-10-26T13:00:00")[2]
    assert conn.execute("SELECT done_at, cancelled_at FROM callback_task WHERE order_id = ?", (cid,)).fetchone() == ("2026-10-26T13:00:00", None)
    assert any(e["text"].endswith("заказ выдан, возврат не нужен") for e in orders.list_events(conn, cid))
    assert not any("продано до согласования" in e["text"].lower() for e in orders.list_events(conn, cid))


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
    assert sorted(who for who, _t in told) == ["ivan"] and any(t.startswith("Позвонить клиенту") for _w, t in told)     # never the point (2026-11-10)
    reserve.check(conn, NEXT + "T10:07:00", calls(told), deliver, vk)
    assert len(told) == 1


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


# ---- 8. the review of 2026-11-05 -------------------------------------------------------------------------------------------------------

def _agreed_and_asked(tmp_path):
    from test_mypoint import manager
    app, client, tok, conn, pid, card = ready(tmp_path)
    started(conn)
    cid = _card(conn, pid, webmim="700200", code="at_point")
    orders.move_card(conn, cid, _stage(conn, "oth"), ENTERED, "o", changes={"cancel_reason": "Отказ"})
    assert order_oth.ask_seventh_day(conn, "2026-10-26T12:30:00") == 1
    order_oth.agree(conn, orders.get_card(conn, cid), orders.date.fromisoformat("2026-10-26"), "2026-10-26T13:00:00", "owner")
    return app, conn, cid, manager(app, conn, pid)


@pytest.mark.parametrize("new_view", [False, True])
def test_the_question_stays_on_the_page_after_the_agreement_and_issued_drops_the_plan(tmp_path, new_view):
    from test_mypoint import press
    app, conn, cid, mine = _agreed_and_asked(tmp_path)
    if new_view:
        from test_point_new_view import flag
        flag(conn, "all")
    page = mine.get("/my-point?tab=return").get_data(as_text=True)
    assert 'data-oth-question="%d"' % cid in page and "/my-point/%d/oth_issued" % cid in page and "/my-point/%d/act_signed" % cid in page
    press(mine, cid, "oth_issued")
    assert orders.get_card(conn, cid)["stage"]["code"] == "done" and order_oth.return_plan(conn, cid) is None
    assert open_tasks(conn, cid) == []


def test_here_on_the_plaque_closes_only_the_question(tmp_path):
    from test_mypoint import press
    app, conn, cid, mine = _agreed_and_asked(tmp_path)
    press(mine, cid, "oth_here")
    page = mine.get("/my-point?tab=return").get_data(as_text=True)
    assert 'data-oth-question="%d"' % cid not in page and "/my-point/%d/act_signed" % cid in page
    assert order_oth.return_plan(conn, cid) is not None


def test_the_cancel_of_the_agreement_does_not_promise_the_ad(tmp_path):
    from pathlib import Path
    root = Path(__file__).parent.parent / "src" / "tires_sync" / "panel" / "templates"
    for name in ("order_card.html", "office2/order_card.html"):
        text = (root / name).read_text(encoding="utf-8")
        assert "снова будут в объявлении" not in text and "шины останутся снятыми с продажи" in text


def test_the_cancel_of_the_agreement_puts_the_task_back_once(tmp_path):
    conn, cid = _moved_on_day7(tmp_path)
    order_oth.agree(conn, orders.get_card(conn, cid), orders.date.fromisoformat("2026-10-26"), "2026-10-26T13:00:00", "owner")
    assert open_tasks(conn, cid) == []
    assert order_oth.unagree(conn, orders.get_card(conn, cid), "2026-10-26T14:00:00", "owner")
    [task] = open_tasks(conn, cid)
    assert task["title"] == order_oth.RETURN_TASK and task["assignee"] == "ivan" and task["due_at"] == "2026-10-28T14:00"


def test_a_failed_task_leaves_a_yellow_record(tmp_path, monkeypatch):
    conn, cid, card = _held(tmp_path)
    monkeypatch.setattr(tasks, "add", lambda **kw: (_ for _ in ()).throw(RuntimeError("busy")))
    assert order_oth.ask_seventh_day(conn, "2026-10-26T12:30:00") == 1
    assert orders.get_card(conn, cid)["stage"]["code"] == orders.RETURN_GIVEN
    assert [e for e in orders.list_events(conn, cid) if e["level"] == "yellow" and e["text"].startswith("Задача возврата не поставлена")]
    other = card_at(conn, "at_point", "2026-10-15T10:00:00")
    was = on_oth(conn, other)
    orders.move_card(conn, other, stage(conn, orders.RETURN_GIVEN), "2026-10-21T10:00:00", "o")
    order_oth.moved_by_hand(conn, was, "2026-10-21T10:00:00", "o")
    assert [e for e in orders.list_events(conn, other) if e["level"] == "yellow" and e["text"].startswith("Задача возврата не поставлена")]


def test_the_settings_name_the_real_task(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    page = client.get("/orders/settings").get_data(as_text=True)
    assert order_oth.RETURN_TASK in page and "Согласовать на возврат" not in page and "при ручном переводе" not in page


def test_a_busy_base_in_the_failure_itself_raises_nothing(tmp_path, monkeypatch):
    conn, cid, card = _held(tmp_path)
    monkeypatch.setattr(tasks, "add", lambda **kw: (_ for _ in ()).throw(RuntimeError("busy")))
    real = orders.add_event

    def locked(conn_, id_, now, who, kind, text, level=""):
        if text.startswith("Задача возврата не поставлена"):
            raise RuntimeError("database is locked")
        return real(conn_, id_, now, who, kind, text, level)
    monkeypatch.setattr(orders, "add_event", locked)
    told = []
    assert order_oth.ask_seventh_day(conn, "2026-10-26T12:30:00", calls(told)) == 1        # the alert still goes
    assert told


def test_moved_by_hand_never_raises_for_an_order_gone(tmp_path):
    conn, cid, card = _held(tmp_path)
    order_oth.moved_by_hand(conn, dict(card, id=999999), "2026-10-21T10:00:00", "o")


def test_the_undo_of_issued_brings_the_agreement_back(tmp_path):
    from test_mypoint import press
    app, conn, cid, mine = _agreed_and_asked(tmp_path)
    plan = order_oth.return_plan(conn, cid)
    press(mine, cid, "oth_issued")
    assert order_oth.return_plan(conn, cid) is None
    r = post(mine, "/my-point/%d/undo" % cid, "tok")
    assert r.status_code == 302
    card = orders.get_card(conn, cid)
    assert card["stage"]["code"] == orders.RETURN_GIVEN and order_oth.return_plan(conn, cid) == plan
    assert open_tasks(conn, cid) == []                                                      # agreed: nothing to agree


def test_issued_without_an_agreement_leaves_nothing_to_bring_back(tmp_path):
    from tires_sync import store
    conn, cid = _moved_on_day7(tmp_path)
    order_oth.answer_issued(conn, orders.get_card(conn, cid), "точка", "2026-10-26T13:00:00")
    assert not store.get_state(conn, order_oth.DROPPED_KEY % cid)


# ---- 9. the independent review of 2026-11-05 ---------------------------------------------------------------------------------------------

def test_the_seventh_day_never_pulls_back_an_order_that_left_during_the_alerts(tmp_path):
    conn = db(tmp_path)
    first, second = (card_at(conn, "at_point", "2026-10-15T10:00:00") for _ in range(2))
    for cid in (first, second):
        on_oth(conn, cid)

    def notify(card, text):
        if orders.get_card(conn, second)["stage"]["code"] == "oth":
            orders.move_card(conn, second, stage(conn, "done"), "2026-10-26T12:30:30", "o")
        return "ok"
    order_oth.ask_seventh_day(conn, "2026-10-26T12:31:00", notify)
    assert orders.get_card(conn, second)["stage"]["code"] in ("done", orders.RETURN_GIVEN)
    if orders.get_card(conn, second)["stage"]["code"] == orders.RETURN_GIVEN:      # moved before the alerts: left afterwards by the person
        orders.move_card(conn, second, stage(conn, "done"), "2026-10-26T12:32:00", "o")
    order_oth.ask_seventh_day(conn, "2026-10-26T13:07:00", notify)
    assert orders.get_card(conn, second)["stage"]["code"] == "done" and open_tasks(conn, second) == []


def test_the_loop_reads_the_order_again_before_the_move(tmp_path, monkeypatch):
    conn = db(tmp_path)
    first, second = (card_at(conn, "at_point", "2026-10-15T10:00:00") for _ in range(2))
    for cid in (first, second):
        on_oth(conn, cid)
    real = orders.move_card

    def moving(conn_, card_id, stage_id, now, author, *a, **k):                             # the first move: the second order leaves meanwhile
        done = real(conn_, card_id, stage_id, now, author, *a, **k)
        if card_id == first:                                                                # the stage changed, the moment not: only the check of the stage holds
            conn_.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (stage(conn, "done"), second))
            conn_.commit()
        return done
    monkeypatch.setattr(orders, "move_card", moving)
    assert order_oth.ask_seventh_day(conn, "2026-10-26T12:31:00") == 1
    assert orders.get_card(conn, second)["stage"]["code"] == "done"


def test_a_late_catch_up_moves_on_time_and_tells_in_the_morning(tmp_path):
    conn, cid, card = _held(tmp_path)
    told = []
    reserve.check(conn, "2026-10-26T23:40:00", calls(told))
    assert orders.get_card(conn, cid)["stage"]["code"] == orders.RETURN_GIVEN and len(open_tasks(conn, cid)) == 1      # the move and the task: on time
    assert told == []
    reserve.check(conn, "2026-10-27T08:59:00", calls(told))
    assert told == []
    reserve.check(conn, "2026-10-27T09:07:00", calls(told))
    assert [t for _w, t in told if t.startswith("Нужно согласовать")] and len(told) == len({t for _w, t in told})
    before = len(told)
    reserve.check(conn, "2026-10-27T10:07:00", calls(told))
    assert len(told) == before


def test_the_6d12h_alert_late_at_night_waits(tmp_path):
    conn, cid, card = _held(tmp_path)
    conn.execute("UPDATE order_card SET point_id = NULL WHERE id = ?", (cid,))
    conn.commit()
    told = []
    reserve.check(conn, DUE, calls(told))                                                   # 23:00
    assert told == []
    reserve.check(conn, "2026-10-27T09:07:00", calls(told))
    assert len(told) == 1


def test_the_silent_move_late_in_the_evening_holds_its_alerts(tmp_path):
    conn, cid, deliver, vk = _silent(tmp_path)
    told = []
    conn.execute("UPDATE chat_send_log SET at = ? WHERE status = 'sent'", (DAY + "T12:30:00",))
    conn.commit()
    reserve.check(conn, NEXT + "T00:37:00", calls(told), deliver, vk)
    assert told == []
    (tmp_path / "b").mkdir()
    other_conn, other, deliver2, vk2 = _silent(tmp_path / "b")
    late = []
    assert reserve._no_answer_step(other_conn, orders.get_card(other_conn, other), NEXT + "T23:07:00", calls(late))
    assert late == []
    reserve.check(other_conn, "2026-10-20T09:07:00", calls(late), deliver2, vk2)
    assert any(t.startswith("Позвонить клиенту") for _w, t in late)


def test_the_end_of_the_reservation_alert_waits_for_the_morning(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    told = []
    reserve.check(conn, "2026-10-18T23:07:00", calls(told))
    assert told == []
    reserve.check(conn, "2026-10-19T09:07:00", calls(told))
    assert len(told) == 1 and orders.get_card(conn, cid)["stage"]["code"] == "at_point"


def test_a_position_mark_does_not_keep_the_order_from_the_seventh_day(tmp_path):
    conn, cid, card = _two_positions(tmp_path)
    order_oth.item_to_return(conn, card["items"][0]["id"], "2026-10-21T10:00:00", "o", "1")
    told = []
    assert order_oth.tell_agree_due(conn, "2026-10-26T12:00:00", calls(told)) == 0
    assert order_oth.ask_seventh_day(conn, "2026-10-26T12:30:00", calls(told)) == 1
    assert orders.get_card(conn, cid)["stage"]["code"] == orders.RETURN_GIVEN and len(open_tasks(conn, cid)) == 1
    assert [t for _w, t in told if t.startswith("Нужно согласовать")]


def test_a_real_return_mark_still_keeps_it_and_a_real_press_on_a_marked_position_counts(tmp_path):
    conn, cid, card = _two_positions(tmp_path)
    order_oth.item_to_return(conn, card["items"][0]["id"], "2026-10-21T10:00:00", "o", "1")
    order_oth.mark_returned(conn, card["items"][0]["id"], "2026-10-22T10:00:00", "o", 1)
    assert order_oth.ask_seventh_day(conn, "2026-10-26T12:30:00") == 0
    (tmp_path / "b").mkdir()
    other_conn, other, other_card = _two_positions(tmp_path / "b")
    order_oth.mark_returned(other_conn, other_card["items"][1]["id"], "2026-10-22T10:00:00", "o", 1)
    assert order_oth.ask_seventh_day(other_conn, "2026-10-26T12:30:00") == 0


def test_the_undo_of_one_position_keeps_the_task_of_the_other(tmp_path):
    conn, cid, card = _two_positions(tmp_path)
    one, two = card["items"][0]["id"], card["items"][1]["id"]
    order_oth.item_to_return(conn, one, "2026-10-21T10:00:00", "o")
    order_oth.item_to_return(conn, two, "2026-10-21T11:00:00", "o")
    order_oth.unmark_returned(conn, one, "2026-10-21T12:00:00", "o")
    assert len(open_tasks(conn, cid)) == 1 and order_oth.item_mark(order_oth.item_marks(conn), two, card["entered_at"])
    order_oth.unmark_returned(conn, two, "2026-10-21T13:00:00", "o")
    assert open_tasks(conn, cid) == []


def test_a_vk_point_gets_the_status_and_the_question_and_answers_issued(tmp_path):
    from test_vk_commands import hook
    app, conn, cid = _vk_order(tmp_path)
    told = []
    order_oth.ask_seventh_day(conn, "2026-10-26T12:30:00", calls(told))
    [to_point] = [t for who, t in told if who == ""]
    assert "Ожидает согласование возврата" in to_point and "выдал 700300" in to_point and "на точке 700300" in to_point
    assert not [e for e in orders.list_events(conn, cid) if "она может не увидеть" in e["text"]]
    hook(app, conn, "выдал 700300")
    assert orders.get_card(conn, cid)["stage"]["code"] == "done" and open_tasks(conn, cid) == []
    assert conn.execute("SELECT 1 FROM client_order WHERE order_id = ?", (cid,)).fetchone() is not None


def test_a_vk_point_answers_here_and_a_second_word_goes_to_the_owner(tmp_path):
    from test_vk_commands import hook
    app, conn, cid = _vk_order(tmp_path)
    order_oth.ask_seventh_day(conn, "2026-10-26T12:30:00", calls([]))
    hook(app, conn, "на точке 700300", event="e1")
    card = orders.get_card(conn, cid)
    assert card["stage"]["code"] == orders.RETURN_GIVEN and not order_oth.question_open(conn, card)
    assert _outcomes(conn, cid)[-1] == "отмечено: шины на точке"
    hook(app, conn, "выдал 700300", event="e2")                                             # answered: no question open
    assert orders.get_card(conn, cid)["stage"]["code"] == orders.RETURN_GIVEN


def test_a_vk_issued_after_the_act_is_refused_clearly(tmp_path):
    from test_vk_commands import hook
    app, conn, cid = _vk_order(tmp_path)
    order_oth.ask_seventh_day(conn, "2026-10-26T12:30:00", calls([]))
    order_oth.agree(conn, orders.get_card(conn, cid), orders.date.fromisoformat("2026-10-26"), "2026-10-26T13:00:00", "owner")
    conn.execute("UPDATE order_item SET supplier_price = 0 WHERE order_id = ?", (cid,))
    conn.commit()
    assert order_oth.sign_act(conn, orders.get_card(conn, cid), "Бета", "2026-10-26T14:00:00", "точка") == "signed"
    hook(app, conn, "выдал 700300")
    assert _outcomes(conn, cid)[-1] == "не выполнено: акт МХ-3 подписан"


def test_a_vk_point_hears_the_agreed_date_and_a_cancelled_one_is_not_sent(tmp_path):
    app, conn, cid = _vk_order(tmp_path, orders.RETURN_GIVEN)
    told = []
    order_oth.agree(conn, orders.get_card(conn, cid), orders.date.fromisoformat("2026-10-26"), "2026-10-26T10:00:00", "owner")
    reserve.check(conn, "2026-10-26T11:07:00", calls(told))
    [text] = [t for who, t in told if who == "" and "Согласовано на возврат" in t]
    assert "26.10" in text and "№700300 возврат отдал подтверждаю" in text
    reserve.check(conn, "2026-10-26T12:07:00", calls(told))
    assert len([t for who, t in told if "Согласовано на возврат" in t]) == 1
    (tmp_path / "b").mkdir()
    app2, conn2, cid2 = _vk_order(tmp_path / "b", orders.RETURN_GIVEN)
    order_oth.agree(conn2, orders.get_card(conn2, cid2), orders.date.fromisoformat("2026-10-26"), "2026-10-26T10:00:00", "owner")
    order_oth.unagree(conn2, orders.get_card(conn2, cid2), "2026-10-26T10:10:00", "owner")
    late = []
    reserve.check(conn2, "2026-10-26T11:07:00", calls(late))
    assert not [t for who, t in late if "Согласовано на возврат" in t]


def test_a_manual_move_tells_a_vk_point_the_status_by_the_tick(tmp_path):
    app, conn, cid = _vk_order(tmp_path)
    before = orders.get_card(conn, cid)
    orders.move_card(conn, cid, _stage(conn, orders.RETURN_GIVEN), "2026-10-21T10:00:00", "o")
    order_oth.moved_by_hand(conn, before, "2026-10-21T10:00:00", "o")
    told = []
    reserve.check(conn, "2026-10-21T11:07:00", calls(told))
    [text] = [t for who, t in told if who == ""]
    assert "Ожидает согласование возврата" in text and "выдал" in text                      # by hand: the question too (the owner 2026-11-07)
    assert not [t for _w, t in told if t.startswith("Нужно согласовать")]


def test_a_panel_point_gets_no_vk_status(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    conn.execute("UPDATE point SET channel = 'panel'")
    conn.commit()
    on_oth(conn, cid)
    told = []
    order_oth.ask_seventh_day(conn, "2026-10-26T12:30:00", calls(told))
    assert not [t for who, t in told if who == ""]


def test_the_0859_reminder_goes_the_vk_way(tmp_path):
    from tires_sync import order_notify
    conn, cid = _late_return(tmp_path)
    assert order_notify.route(conn, dict(orders.get_card(conn, cid), responsible="", to_staff=False))["channel"] == "vk"
    told = []
    reserve.check(conn, "2026-11-04T08:59:00", calls(told))
    assert [who for who, t in told if "передать поставщику" in t] == [""]


def test_a_crash_after_the_move_is_repaired_by_the_next_tick_once(tmp_path, monkeypatch):
    conn, cid, card = _held(tmp_path)
    second = card_at(conn, "at_point", "2026-10-15T10:00:00")
    on_oth(conn, second)
    from tires_sync import order_alerts as alerts
    told_real = alerts.told_people
    calls_seen = []

    def broken(conn_, order_id, usual):
        calls_seen.append(order_id)
        if order_id == cid and len(calls_seen) <= 1:
            raise RuntimeError("boom")
        return told_real(conn_, order_id, usual)
    monkeypatch.setattr(alerts, "told_people", broken)
    told = []
    reserve.check(conn, "2026-10-26T12:30:00", calls(told))
    assert [t for _w, t in told if "№%d" % (5200000 + second) in t and t.startswith("Нужно согласовать")]    # the other order is not stopped
    assert not [t for _w, t in told if "№%d" % (5200000 + cid) in t and t.startswith("Нужно согласовать")]
    reserve.check(conn, "2026-10-26T13:07:00", calls(told))
    assert len([t for _w, t in told if "№%d" % (5200000 + cid) in t and t.startswith("Нужно согласовать")]) == 1
    reserve.check(conn, "2026-10-26T14:07:00", calls(told))
    assert len([t for _w, t in told if "№%d" % (5200000 + cid) in t and t.startswith("Нужно согласовать")]) == 1


def test_a_failed_task_is_made_by_the_next_tick(tmp_path, monkeypatch):
    conn, cid, card = _held(tmp_path)
    real = tasks.add
    monkeypatch.setattr(tasks, "add", lambda **kw: (_ for _ in ()).throw(RuntimeError("busy")))
    reserve.check(conn, "2026-10-26T12:30:00", calls([]))
    assert open_tasks(conn, cid) == []
    reserve.check(conn, "2026-10-26T13:07:00", calls([]))
    assert len([e for e in orders.list_events(conn, cid) if e["text"].startswith("Задача возврата не поставлена")]) == 1
    monkeypatch.setattr(tasks, "add", real)
    reserve.check(conn, "2026-10-26T14:07:00", calls([]))
    assert len(open_tasks(conn, cid)) == 1
    reserve.check(conn, "2026-10-26T15:07:00", calls([]))
    assert len(open_tasks(conn, cid)) == 1


def test_a_responsible_of_a_point_the_task_goes_to_the_owner(tmp_path):
    conn = db(tmp_path)
    users.add_user(conn, "pointuser", "password-123", ENTERED, role="point", point_id=orders.get_card(conn, card_at(conn, "at_point", "2026-10-15T10:00:00"))["point_id"])
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    conn.execute("UPDATE order_card SET responsible = 'pointuser' WHERE id = ?", (cid,))
    conn.commit()
    on_oth(conn, cid)
    order_oth.ask_seventh_day(conn, "2026-10-26T12:30:00")
    assert open_tasks(conn, cid)[0]["assignee"] == "owner"


def test_the_link_is_full_when_the_base_is_set(tmp_path):
    from tires_sync import store
    conn, cid, card = _held(tmp_path)
    store.set_state(conn, "public_base", "https://panel.example/")
    told = []
    order_oth.tell_agree_due(conn, DUE, calls(told))
    assert told[0][1].endswith("— https://panel.example/orders/%d" % cid)


def test_the_page_shows_the_day_it_came_to_oth_on_a_return_row(tmp_path):
    app, client, tok, conn, cid = _on_oth_page(tmp_path)
    orders.move_card(conn, cid, _stage(conn, orders.RETURN_GIVEN), "2026-10-27T10:00:00", "o")
    page = client.get("/oth").get_data(as_text=True)
    assert re.search(r"<td data-oth-since>20\.10(\.2026)?<", page)


def test_the_bell_of_a_leftover_question_goes_to_the_oth_tab(tmp_path):
    from test_mypoint import manager
    app, client, tok, conn, cid = _on_oth_page(tmp_path)
    card = orders.get_card(conn, cid)
    conn.execute("INSERT INTO app_state (key, value) VALUES (?, 'asked|2026-10-22T12:30:00')", (order_oth._ask_key(card),))
    conn.commit()
    mine = manager(app, conn, card["point_id"])
    [item] = [i for i in mine.get("/my-point/feed").get_json()["items"] if i["key"].startswith("oth-question-")]
    assert item["href"] == "/my-point?tab=oth"


# ---- 10. the review of 85bf858 ----------------------------------------------------------------------------------------------------------

def _asked_vk(tmp_path, agreed=True):
    app, conn, cid = _vk_order(tmp_path)
    order_oth.ask_seventh_day(conn, "2026-10-26T12:30:00", calls([]))
    if agreed:
        order_oth.agree(conn, orders.get_card(conn, cid), orders.date.fromisoformat("2026-10-26"), "2026-10-26T13:00:00", "owner")
    return app, conn, cid


@pytest.mark.parametrize("text", ["отдал 700300", "отдали 700300", "700300 забрали", "забрал 700300"])
@pytest.mark.parametrize("agreed", [True, False])
def test_given_to_the_driver_never_closes_the_return_as_issued(tmp_path, text, agreed):
    """Never «выдали»: the order stays, the client is not linked. The owner 2026-11-07: «отдал» of an agreed return is the hand-over to the driver
    (it asks the confirmation as «возврат отдал», test_return_tweaks п. 1); otherwise the owner sees the message in «Входящие» as before."""
    from test_vk_commands import hook
    app, conn, cid = _asked_vk(tmp_path, agreed)
    plan = order_oth.return_plan(conn, cid)
    hook(app, conn, text)
    card = orders.get_card(conn, cid)
    assert card["stage"]["code"] == orders.RETURN_GIVEN and order_oth.return_plan(conn, cid) == plan
    assert order_oth.question_open(conn, card)
    assert conn.execute("SELECT 1 FROM client_order WHERE order_id = ?", (cid,)).fetchone() is None
    to_driver = agreed and text.split()[0] in mgr.GIVE
    assert (conn.execute("SELECT 1 FROM manager_message WHERE text LIKE ?", (text + "%",)).fetchone() is None) == to_driver
    if to_driver:
        assert conn.execute("SELECT outcome FROM order_command_log WHERE order_id = ? ORDER BY rowid DESC", (cid,)).fetchone()[0].startswith("ждёт подтверждения")


@pytest.mark.parametrize("text", ["выдал 700300", "выдали 700300", "№700300 выдала", "отдал клиенту 700300"])
def test_the_words_of_the_question_close_it_as_issued(tmp_path, text):
    from test_vk_commands import hook
    app, conn, cid = _asked_vk(tmp_path)
    hook(app, conn, text)
    assert orders.get_card(conn, cid)["stage"]["code"] == "done" and order_oth.return_plan(conn, cid) is None


def test_a_closed_task_of_a_move_by_hand_does_not_come_back(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    before = on_oth(conn, cid)
    orders.move_card(conn, cid, stage(conn, orders.RETURN_GIVEN), "2026-10-21T19:00:00", "o")
    order_oth.moved_by_hand(conn, before, "2026-10-21T19:00:00", "o")
    [task_id] = [r[0] for r in conn.execute("SELECT id FROM callback_task WHERE order_id = ?", (cid,))]
    tasks.close(conn, task_id, "done", "2026-10-21T19:05:00", "o")
    for when in ("2026-10-21T19:07:00", "2026-10-21T20:07:00", "2026-10-22T09:07:00"):
        reserve.check(conn, when, calls([]))
    assert open_tasks(conn, cid) == []


def test_a_closed_task_of_the_seventh_day_held_at_night_does_not_come_back(tmp_path):
    conn, cid, card = _held(tmp_path)
    reserve.check(conn, "2026-10-26T23:07:00", calls([]))
    [task_id] = [r[0] for r in conn.execute("SELECT id FROM callback_task WHERE order_id = ?", (cid,))]
    tasks.close(conn, task_id, "done", "2026-10-26T23:30:00", "o")
    told = []
    reserve.check(conn, "2026-10-27T09:07:00", calls(told))
    assert open_tasks(conn, cid) == []
    assert [t for _w, t in told if t.startswith("Нужно согласовать")]                       # the alert still goes


# ---- 11. the review of 7afd3f9 ----------------------------------------------------------------------------------------------------------

def test_the_alerts_of_a_move_agreed_meanwhile_are_not_sent(tmp_path):
    app, conn, cid = _vk_order(tmp_path)
    before = orders.get_card(conn, cid)
    orders.move_card(conn, cid, _stage(conn, orders.RETURN_GIVEN), "2026-10-21T22:00:00", "o")
    order_oth.moved_by_hand(conn, before, "2026-10-21T22:00:00", "o")
    order_oth.agree(conn, orders.get_card(conn, cid), orders.date.fromisoformat("2026-10-21"), "2026-10-21T22:05:00", "owner")
    told = []
    reserve.check(conn, "2026-10-22T09:07:00", calls(told))
    assert not [t for _w, t in told if "Ожидает согласование" in t]
    assert [t for _w, t in told if "Согласовано на возврат" in t]
    assert conn.execute("SELECT 1 FROM app_state WHERE key = ?", (order_oth.PENDING_KEY % cid,)).fetchone() is None


def test_day_7_agreed_before_the_morning_tells_nobody_to_agree(tmp_path):
    conn, cid, card = _held(tmp_path)
    reserve.check(conn, "2026-10-26T23:07:00", calls([]))
    order_oth.agree(conn, orders.get_card(conn, cid), orders.date.fromisoformat("2026-10-26"), "2026-10-26T23:30:00", "owner")
    told = []
    reserve.check(conn, "2026-10-27T09:07:00", calls(told))
    assert not [t for _w, t in told if t.startswith("Нужно согласовать") or "Ожидает согласование" in t]


def test_the_bulk_undo_puts_the_task_back(tmp_path):
    from tires_sync import order_bulk
    conn = db(tmp_path)
    path = str(tmp_path / "x.db")
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    before = on_oth(conn, cid)
    orders.move_card(conn, cid, stage(conn, orders.RETURN_GIVEN), "2026-10-21T10:00:00", "o")
    order_oth.moved_by_hand(conn, before, "2026-10-21T10:00:00", "o")
    assert open_tasks(conn, cid)
    target = stage(conn, "cancel")
    order_bulk.run(conn, path, [cid], target, "Чистка", order_bulk.check(conn, [cid], target, "Чистка")["sign"], "2026-10-22T10:00:00", "o")
    assert open_tasks(conn, cid) == []
    audit = conn.execute("SELECT id FROM audit WHERE action = 'orders_bulk_move' ORDER BY id DESC").fetchone()[0]
    order_bulk.undo(conn, path, audit, [cid], "2026-10-22T11:00:00", "o")
    assert orders.get_card(conn, cid)["stage"]["code"] == orders.RETURN_GIVEN
    assert [t["title"] for t in open_tasks(conn, cid)] == [order_oth.RETURN_TASK]


# ---- 12. the review of 53d735f ----------------------------------------------------------------------------------------------------------

def test_an_agreement_of_an_earlier_stay_does_not_survive_a_return_to_work(tmp_path):
    from test_vk_commands import hook
    app, conn, cid = _vk_order(tmp_path, orders.RETURN_GIVEN)
    order_oth.agree(conn, orders.get_card(conn, cid), orders.date.fromisoformat("2026-10-21"), "2026-10-21T11:00:00", "owner")
    orders.move_card(conn, cid, _stage(conn, "at_point"), "2026-10-22T10:00:00", "o")          # the client came back
    assert order_oth.return_plan(conn, cid) is None
    assert [e for e in orders.list_events(conn, cid) if e["text"].startswith("Согласование возврата снято")]
    before = orders.get_card(conn, cid)
    orders.move_card(conn, cid, _stage(conn, orders.RETURN_GIVEN), "2026-10-23T10:00:00", "o")  # refused again, straight back by hand
    order_oth.moved_by_hand(conn, before, "2026-10-23T10:00:00", "o")
    assert order_oth.label_of(order_oth.return_plan(conn, cid)) == "Ожидает согласование возврата"
    assert [t["title"] for t in open_tasks(conn, cid)] == [order_oth.RETURN_TASK]
    told = []
    assert order_oth.remind_handover(conn, "2026-10-26T09:00:00", calls(told)) == 0
    hook(app, conn, "№700300 возврат отдал подтверждаю")
    assert orders.get_card(conn, cid)["stage"]["code"] == orders.RETURN_GIVEN


def test_a_closed_stage_keeps_the_agreement_for_the_undo(tmp_path):
    from tires_sync import order_bulk
    conn, cid = _moved_on_day7(tmp_path)
    path = str(tmp_path / "x.db")
    order_oth.agree(conn, orders.get_card(conn, cid), orders.date.fromisoformat("2026-10-26"), "2026-10-26T13:00:00", "owner")
    plan = order_oth.return_plan(conn, cid)
    target = stage(conn, "cancel")
    order_bulk.run(conn, path, [cid], target, "Чистка", order_bulk.check(conn, [cid], target, "Чистка")["sign"], "2026-10-27T10:00:00", "o")
    assert order_oth.return_plan(conn, cid) == plan
    audit = conn.execute("SELECT id FROM audit WHERE action = 'orders_bulk_move' ORDER BY id DESC").fetchone()[0]
    order_bulk.undo(conn, path, audit, [cid], "2026-10-27T11:00:00", "o")
    assert order_oth.return_plan(conn, cid) == plan


def test_a_position_with_a_real_return_cannot_be_marked_by_hand(tmp_path):
    conn, cid, card = _two_positions(tmp_path)
    item = card["items"][0]["id"]
    order_oth.mark_returned(conn, item, "2026-10-21T10:00:00", "o", 1)
    with pytest.raises(order_oth.OthError, match="возвращено поставщику"):
        order_oth.item_to_return(conn, item, "2026-10-21T11:00:00", "o", "2")
    assert open_tasks(conn, cid) == [] and order_oth.ask_seventh_day(conn, "2026-10-26T12:30:00") == 0   # the real mark still keeps it

