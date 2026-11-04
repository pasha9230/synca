"""The owner 2026-11-04: the 12-hour move to «На ОТХ» tells the responsible «Позвонить клиенту» with a line of the order; «Товар на точке» and
«Крайняя дата резерва» on /oth and the order's views; 6 days 12 hours on «На ОТХ» tell the responsible once; «На возврат» by hand (an order, a position):
the task «Согласовать на возврат», «Согласуем», no double task with the seventh day."""
from __future__ import annotations

import re

import pytest

from panel_helpers import post
from test_oth_seventh_day import ENTERED, on_oth, started
from test_order_reserve import card_at, db, ok, stage
from test_reserve_noon import DAY, NEXT, ready as reserve_ready, tick
from test_vk_commands import _card, _stage, ready
from tires_sync import message_texts, order_alerts, order_oth, order_reserve as reserve, orders, tasks, users

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
    fresh(conn, chat, NEXT + "T00:29:00")
    users.add_user(conn, "zaman", "password-123", ENTERED, role="owner")
    conn.execute("UPDATE order_card SET responsible = '', created_by = 'робот' WHERE id = ?", (cid,))
    conn.commit()
    tick(conn, DAY + "T12:30:00", deliver, vk)
    told = []
    assert reserve._no_answer_step(conn, orders.get_card(conn, cid), NEXT + "T00:30:00", calls(told))
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
    assert told == [("ivan", "Нужно согласовать на возврат: заказ №%d (точка «Бета») 6 дней 12 часов на ОТХ — /orders/%d" % (5200000 + cid, cid))]
    for when in (DUE, "2026-10-27T09:00:00", "2026-11-05T09:00:00"):
        assert order_oth.tell_agree_due(conn, when, calls(told)) == 0
    assert len(told) == 1
    assert orders.get_card(conn, cid)["stage"]["code"] == "oth"                            # the stage stays
    [record] = [e for e in orders.list_events(conn, cid) if e["text"].startswith("Нужно согласовать")]
    assert record["level"] == "yellow"


def test_the_hourly_tick_makes_it_and_a_new_stay_tells_again(tmp_path):
    conn, cid, _card_ = _held(tmp_path)
    told = []
    reserve.check(conn, "2026-10-27T09:00:00", calls(told))
    assert sum(t.startswith("Нужно согласовать") for _w, t in told) == 1
    orders.move_card(conn, cid, stage(conn, "at_point"), "2026-10-28T09:00:00", "o")
    orders.move_card(conn, cid, stage(conn, "oth"), "2026-10-28T10:00:00", "o")
    assert order_oth.tell_agree_due(conn, "2026-11-03T22:00:00", calls(told)) == 1
    assert sum(t.startswith("Нужно согласовать") for _w, t in told) == 2


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


def test_the_question_of_the_seventh_day_stays_and_its_answer_stops_the_alert(tmp_path):
    conn, cid, card = _held(tmp_path)
    told = []
    reserve.check(conn, "2026-10-26T12:30:00", calls(told))                                # before 6 days 12 hours of this order
    assert order_oth.question_open(conn, orders.get_card(conn, cid)) and told == []
    order_oth.answer_here(conn, orders.get_card(conn, cid), "точка", "2026-10-26T13:00:00")
    assert order_oth.tell_agree_due(conn, DUE, calls(told)) == 0


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
    assert order_oth.task_days(conn) == 1
    assert order_oth.set_task_days(conn, " 3 ") == 3 and order_oth.task_days(conn) == 3
    for bad in ("0", "31", "", "1.5", "-1", "два", None, "٣"):
        with pytest.raises(order_oth.OthError):
            order_oth.set_task_days(conn, bad)
    assert order_oth.task_days(conn) == 3
    conn.execute("UPDATE app_state SET value = 'x' WHERE key = ?", (order_oth.TASK_DAYS_KEY,))
    assert order_oth.task_days(conn) == 1


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
    assert task["title"] == "Согласовать на возврат" and task["kind"] == "return" and task["assignee"] == "ivan"
    assert tasks.KINDS[task["kind"]] == "Вернуть"
    assert order_oth.label_of(order_oth.return_plan(conn, cid)) == "Согласуем"
    assert "data-return-label>Согласуем<" in client.get("/orders?r=agree").get_data(as_text=True)
    assert "data-to-return" not in client.get("/orders/%d" % cid).get_data(as_text=True)     # already there
    assert [e for e in orders.list_events(conn, cid) if e["text"].startswith("Задача: Вернуть: Согласовать на возврат")]
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
    on_oth(conn, cid)
    order_oth.ask_seventh_day(conn, "2026-10-26T12:30:00")
    before = orders.get_card(conn, cid)
    order_oth.answer_here(conn, before, "точка", "2026-10-26T13:00:00")
    day7 = open_tasks(conn, cid)
    assert {t["title"] for t in day7} == {order_oth.RETURN_TASK} and len(day7) == 2
    order_oth.moved_by_hand(conn, orders.get_card(conn, cid), "2026-10-26T14:00:00", "o")     # stays: no move
    order_oth.moved_by_hand(conn, before, "2026-10-26T14:00:00", "o")                         # a stale press
    assert open_tasks(conn, cid) == day7


def test_no_double_task_the_seventh_day_after_a_move_by_hand(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    before = on_oth(conn, cid)
    orders.move_card(conn, cid, stage(conn, orders.RETURN_GIVEN), "2026-10-21T10:00:00", "o")
    order_oth.moved_by_hand(conn, before, "2026-10-21T10:00:00", "o")
    orders.move_card(conn, cid, stage(conn, "oth"), "2026-10-21T11:00:00", "o")               # back; the task stays: the return is still on
    order_oth.moved_by_hand(conn, orders.get_card(conn, cid), "2026-10-21T11:00:00", "o")
    assert len(open_tasks(conn, cid)) == 1
    order_oth.ask_seventh_day(conn, "2026-10-27T12:30:00")
    order_oth.answer_here(conn, orders.get_card(conn, cid), "точка", "2026-10-27T13:00:00")
    assert [t["title"] for t in open_tasks(conn, cid)] == [order_oth.MANUAL_TASK]


def test_a_move_by_hand_answers_no_seventh_day_question(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    on_oth(conn, cid)
    order_oth.ask_seventh_day(conn, "2026-10-26T12:30:00")
    before = orders.get_card(conn, cid)
    orders.move_card(conn, cid, stage(conn, orders.RETURN_GIVEN), "2026-10-26T12:40:00", "o")
    order_oth.moved_by_hand(conn, before, "2026-10-26T12:40:00", "o")
    assert not order_oth.question_open(conn, orders.get_card(conn, cid))
    assert order_oth.answer_here(conn, before, "точка", "2026-10-26T13:00:00")[2] is False     # a stale page
    assert [t["title"] for t in open_tasks(conn, cid)] == [order_oth.MANUAL_TASK]


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


def test_a_position_keeps_the_stage_and_the_sale_has_the_label_and_the_task(tmp_path):
    conn, cid, card = _two_positions(tmp_path)
    item = card["items"][0]["id"]
    assert order_oth.item_to_return(conn, item, "2026-10-21T10:00:00", "o", "3") is True
    now = orders.get_card(conn, cid)
    assert now["stage"]["code"] == "oth" and order_oth.return_plan(conn, cid) is None
    assert order_oth.item_mark(order_oth.item_marks(conn), item, now["entered_at"])["qty"] == 3
    [row] = [r for r in order_oth.stock(conn)["stock"] if r["item_id"] == item]
    assert row["qty"] == 4 and row["returned_qty"] == 0                                        # still ours, still on sale
    [task] = open_tasks(conn, cid)
    assert task["kind"] == "return" and task["assignee"] == "ivan" and "Nokian 205/55 R16, 3 шт." in task["note"]
    assert order_oth.item_to_return(conn, item, "2026-10-21T11:00:00", "o") is False           # once
    assert len(open_tasks(conn, cid)) == 1
    order_oth.item_to_return(conn, card["items"][1]["id"], "2026-10-21T12:00:00", "o")          # another position: its own
    assert len(open_tasks(conn, cid)) == 2


def test_the_mark_returned_finishes_the_position_part_by_part(tmp_path):
    conn, cid, card = _two_positions(tmp_path)
    item = card["items"][0]["id"]
    order_oth.item_to_return(conn, item, "2026-10-21T10:00:00", "o", "3")
    order_oth.mark_returned(conn, item, "2026-10-22T10:00:00", "o", 2)
    assert open_tasks(conn, cid) and order_oth.item_mark(order_oth.item_marks(conn), item, card["entered_at"])
    order_oth.mark_returned(conn, item, "2026-10-22T11:00:00", "o", 1)
    assert open_tasks(conn, cid) == [] and order_oth.item_mark(order_oth.item_marks(conn), item, card["entered_at"]) is None


def test_a_closed_task_of_the_position_does_not_spoil_the_mark(tmp_path):
    conn, cid, card = _two_positions(tmp_path)
    item = card["items"][0]["id"]
    order_oth.item_to_return(conn, item, "2026-10-21T10:00:00", "o")
    [task_id] = [r[0] for r in conn.execute("SELECT id FROM callback_task WHERE order_id = ?", (cid,))]
    tasks.close(conn, task_id, "cancel", "2026-10-21T11:00:00", "o")
    order_oth.mark_returned(conn, item, "2026-10-22T10:00:00", "o")
    assert [r for r in order_oth.stock(conn)["returned"] if r["item_id"] == item]


@pytest.mark.parametrize("qty", ["0", "5", "", "x", "-1"])
def test_a_position_s_quantity_is_checked(tmp_path, qty):
    conn, cid, card = _two_positions(tmp_path)
    with pytest.raises(order_oth.OthError):
        order_oth.item_to_return(conn, card["items"][0]["id"], "2026-10-21T10:00:00", "o", qty)
    assert open_tasks(conn, cid) == [] and order_oth.item_marks(conn) == {}


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
