"""The owner 2026-11-12: an order without an Avito chat gets no message; its responsible gets the tasks «Позвонить, товары приехали» (when the
order enters «На точке») and «Позвонить, истекает срок резерва» (the last day of the reservation from 12:30), marked «Важное», with the
client's data; no doubles; the open ones are cancelled when the order leaves «На точке»; they ring every 2 hours until done; the order is never
moved to «На ОТХ» by itself; the alerts these tasks replace are gone; an order with a chat is as before. Made-up data, no real send."""
from __future__ import annotations

import pytest

from test_order_reserve import PointVk, card_at, chat_of, db, stage
from test_reserve_noon import Deliver
from tires_sync import order_arrived, order_call, order_manager as mgr, order_reserve as reserve, orders, tasks, users

ARRIVE = "2026-10-15T10:00:00"                         # card_at: the reservation runs to 2026-10-18
DAY = "2026-10-18"


class Notify:
    def __init__(self):
        self.sent = []

    def __call__(self, card, text):
        self.sent.append((card["id"], text))
        return "ok"


@pytest.fixture(autouse=True)
def _vk_question_on(monkeypatch):
    monkeypatch.setattr(reserve, "VK_ASK_OFF", False)


def calls(conn, cid, open_only=True):
    return [r for r in conn.execute("SELECT id, title, assignee, note, created_at, done_at, cancelled_at FROM callback_task WHERE order_id = ? AND title IN (?, ?)"
                                    + (" AND done_at IS NULL AND cancelled_at IS NULL" if open_only else "") + " ORDER BY id",
                                    (cid,) + order_call.TITLES)]


def staffed(conn):
    users.add_user(conn, "manager1", "pw-not-real", ARRIVE, role="partner", display_name="Мария")
    return "manager1"


def at_point(conn, responsible=""):
    """An order of a made-up client moved to «На точке» without a chat, its responsible set before."""
    cid = card_at(conn, "moving", "2026-10-14T10:00:00", avito_chat="")
    if responsible:
        conn.execute("UPDATE order_card SET responsible = ? WHERE id = ?", (responsible, cid))
        conn.commit()
    orders.move_card(conn, cid, stage(conn, "at_point"), ARRIVE, "o")
    return cid


def tick(conn, when, notify=None, deliver=None):
    deliver = deliver if deliver is not None else Deliver(conn)
    deliver.at = when
    order_call.tick(conn, when)
    reserve.check(conn, when, notify or Notify(), deliver, PointVk())
    return deliver


# ---- «Позвонить, товары приехали» ----------------------------------------------------------------------------------------------------

def test_entering_the_point_without_a_chat_makes_the_task_with_the_data(tmp_path):
    conn = db(tmp_path)
    cid = at_point(conn, staffed(conn))
    found = calls(conn, cid)
    assert len(found) == 1
    _id, title, assignee, note, created, _d, _c = found[0]
    assert title == "Важное · Позвонить, товары приехали" and assignee == "manager1" and created.startswith("2026-10-15T10:00")
    card = orders.get_card(conn, cid)
    for part in ("Иван", "900", "Nokian 205/55 R16", "арт. ", "4 шт.", "20 000 ₽", "Оформлен:", "прибыл на точку: 15.10.2026 10:00",
                 "резерв до: 18.10.2026", "/orders/%d" % cid):
        assert part in note, part
    assert card["items"][0]["code"] in note or "арт. 1" in note
    task = tasks.get(conn, found[0][0])
    assert task["kind"] == "other"
    assert any(e["text"].startswith("Задача: Другое: Важное · Позвонить, товары приехали") for e in orders.list_events(conn, cid))


def test_nobody_answers_for_it_the_owner_gets_it_as_the_other_tasks(tmp_path):
    conn = db(tmp_path)
    cid = at_point(conn)                                     # its maker «o» is nobody of the office: tasks.default_assignee → the owner
    assert [r[2] for r in calls(conn, cid)] == ["owner"]


def test_an_order_with_a_chat_gets_no_task(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "moving", "2026-10-14T10:00:00", avito_chat="")
    chat_of(conn, cid)
    orders.move_card(conn, cid, stage(conn, "at_point"), ARRIVE, "o")
    assert calls(conn, cid, open_only=False) == []
    other = card_at(conn, "moving", "2026-10-14T10:00:00", avito_chat="")
    conn.execute("UPDATE order_card SET avito_chat = 'https://example.com/chat/1' WHERE id = ?", (other,))   # the field filled by hand
    conn.commit()
    orders.move_card(conn, other, stage(conn, "at_point"), ARRIVE, "o")
    assert calls(conn, other, open_only=False) == []


def test_no_double_on_a_second_tick_or_a_move_back_and_forth(tmp_path):
    conn = db(tmp_path)
    started(conn)
    cid = at_point(conn)
    for when in ("2026-10-15T11:07:00", "2026-10-15T12:07:00"):
        order_call.tick(conn, when)
    assert len(calls(conn, cid, open_only=False)) == 1
    first = calls(conn, cid)[0][0]
    tasks.close(conn, first, "done", "2026-10-15T12:10:00", "owner", "позвонил")
    orders.move_card(conn, cid, stage(conn, "done"), "2026-10-15T12:20:00", "o")
    orders.move_card(conn, cid, stage(conn, "at_point"), "2026-10-15T12:30:00", "o")      # back by hand the same day: called already
    order_call.tick(conn, "2026-10-15T13:07:00")
    assert len(calls(conn, cid, open_only=False)) == 1 and calls(conn, cid) == []


def started(conn, when="2026-10-01T00:07:00"):
    """The first tick of the tasks «Позвонить» (order_call.SINCE_KEY): the stays after it are the tick's."""
    order_call.tick(conn, when)


def test_left_and_back_the_same_day_opens_the_same_task_again(tmp_path):
    conn = db(tmp_path)
    cid = at_point(conn)
    first = calls(conn, cid)[0][0]
    orders.move_card(conn, cid, stage(conn, "moving"), "2026-10-15T11:00:00", "o")     # by mistake
    assert calls(conn, cid) == []
    orders.move_card(conn, cid, stage(conn, "at_point"), "2026-10-15T11:05:00", "o")
    rows = calls(conn, cid, open_only=False)
    assert [r[0] for r in rows] == [first] and rows[0][6] is None                       # one task, open again
    assert any(e["text"].startswith("Задача снова открыта") for e in orders.list_events(conn, cid))


def test_a_client_called_is_not_called_again_after_a_move_back_another_day(tmp_path):
    conn = db(tmp_path)
    cid = at_point(conn)
    tasks.close(conn, calls(conn, cid)[0][0], "done", "2026-10-15T12:10:00", "owner", "позвонил")
    orders.move_card(conn, cid, stage(conn, "done"), "2026-10-15T12:20:00", "o")         # by mistake
    orders.move_card(conn, cid, stage(conn, "at_point"), "2026-10-16T10:00:00", "o")
    order_call.tick(conn, "2026-10-16T11:07:00")
    assert len(calls(conn, cid, open_only=False)) == 1 and calls(conn, cid) == []


def test_a_task_a_person_cancelled_is_not_made_again_by_the_tick(tmp_path):
    conn = db(tmp_path)
    started(conn)
    cid = at_point(conn)
    tasks.close(conn, calls(conn, cid)[0][0], "cancel", "2026-10-15T10:30:00", "owner")
    for when in ("2026-10-15T11:07:00", "2026-10-15T12:07:00", "2026-10-16T09:07:00"):
        order_call.tick(conn, when)
    assert len(calls(conn, cid, open_only=False)) == 1 and calls(conn, cid) == []


def test_the_tick_makes_the_task_of_a_move_past_move_card_once_also_the_next_day(tmp_path):
    conn = db(tmp_path)
    started(conn)
    cid = card_at(conn, "moving", "2026-10-14T10:00:00", avito_chat="")
    orders.move_card(conn, cid, stage(conn, "at_point"), "2026-10-15T23:30:00", "o", fire_rules=False)   # the bulk move, late
    assert calls(conn, cid, open_only=False) == []
    order_call.tick(conn, "2026-10-16T00:07:00")
    order_call.tick(conn, "2026-10-16T01:07:00")
    assert len(calls(conn, cid, open_only=False)) == 1


def test_an_undo_of_the_point_brings_the_task_back(tmp_path):
    conn = db(tmp_path)
    started(conn)
    cid = at_point(conn)
    first = calls(conn, cid)[0][0]
    orders.move_card(conn, cid, stage(conn, "done"), "2026-10-15T11:00:00", "o")        # «Выдал» by mistake
    conn.execute("UPDATE order_card SET stage_id = ?, entered_at = ? WHERE id = ?", (stage(conn, "at_point"), ARRIVE, cid))   # «Отменить» (point_log.undo)
    conn.commit()
    order_call.tick(conn, "2026-10-15T11:07:00")
    assert [r[0] for r in calls(conn, cid)] == [first]


def test_orders_standing_there_before_the_first_tick_get_no_arrival_task_but_the_last_day_one(tmp_path):
    conn = db(tmp_path)
    cid = at_point(conn)
    conn.execute("DELETE FROM callback_task")                                           # as an order that came before this code
    conn.commit()
    order_call.tick(conn, "2026-10-16T09:07:00")                                        # the first tick
    assert calls(conn, cid, open_only=False) == []
    order_call.tick(conn, DAY + "T12:30:00")
    assert [r[1] for r in calls(conn, cid)] == [order_call.EXPIRES]


def test_a_bulk_move_makes_no_task_at_once(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "moving", "2026-10-14T10:00:00", avito_chat="")
    orders.move_card(conn, cid, stage(conn, "at_point"), ARRIVE, "o", fire_rules=False)
    assert calls(conn, cid, open_only=False) == []


def test_an_order_of_a_points_own_account_is_the_points(tmp_path, monkeypatch):
    from tires_sync import order_points
    monkeypatch.setattr(order_points, "of_account", lambda c, order_id: {"id": 1})
    conn = db(tmp_path)
    cid = at_point(conn)
    assert calls(conn, cid, open_only=False) == []
    point = []
    order_arrived.check(conn, "2026-10-15T12:07:00", lambda logins, text: None, lambda card, text: point.append(text))
    assert len(point) == 1                                                              # the point is told as before (the owner 2026-10-26)


# ---- leaving «На точке» ----------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("to", ["done", "cancel", "oth"])
def test_leaving_the_point_cancels_the_open_tasks_with_a_line_in_the_journal(tmp_path, to):
    conn = db(tmp_path)
    cid = at_point(conn)
    order_call.expiring(conn, dict(orders.get_card(conn, cid)), DAY + "T12:30:00")
    assert len(calls(conn, cid)) == 2
    changes = {"cancel_reason": "Передумал"} if to == "cancel" else None
    orders.move_card(conn, cid, stage(conn, to), "2026-10-18T13:00:00", "o", changes=changes)
    assert orders.get_card(conn, cid)["stage"]["code"] != "at_point"
    rows = calls(conn, cid, open_only=False)
    assert len(rows) == 2 and all(r[6] for r in rows) and calls(conn, cid) == []
    lines = [e["text"] for e in orders.list_events(conn, cid) if e["text"].startswith("Задача отменена: Другое: Важное · Позвонить")]
    assert len(lines) == 2 and all("задача не нужна" in t for t in lines)
    assert order_call.sounds(conn, "owner", "2026-10-18T15:00:00") == []                # the sound stops


def test_the_tick_cancels_the_tasks_of_an_order_moved_another_way(tmp_path):
    conn = db(tmp_path)
    cid = at_point(conn)
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (stage(conn, "done"), cid))   # as a merge does it, past move_card
    conn.commit()
    order_call.tick(conn, "2026-10-15T11:07:00")
    assert calls(conn, cid) == [] and len(calls(conn, cid, open_only=False)) == 1


# ---- «Позвонить, истекает срок резерва» ------------------------------------------------------------------------------------------------

def test_the_second_task_comes_at_12_30_of_the_last_day_once(tmp_path):
    conn = db(tmp_path)
    cid = at_point(conn, staffed(conn))
    expires = lambda: [r for r in calls(conn, cid, open_only=False) if r[1] == order_call.EXPIRES]
    for when in ("2026-10-17T12:30:00", DAY + "T10:07:00", DAY + "T12:29:00"):
        tick(conn, when)
    assert expires() == []
    tick(conn, DAY + "T12:30:00")
    tick(conn, DAY + "T13:07:00")
    tick(conn, DAY + "T19:07:00")
    rows = expires()
    assert len(rows) == 1 and rows[0][2] == "manager1" and "резерв до: 18.10.2026" in rows[0][3] and "Иван" in rows[0][3]
    tasks.close(conn, rows[0][0], "done", DAY + "T14:00:00", "manager1", "продлили")
    tick(conn, DAY + "T15:07:00")                                                       # done: the same date makes no second one
    assert len(expires()) == 1


def test_a_date_moved_later_makes_a_new_task_on_the_new_last_day(tmp_path):
    conn = db(tmp_path)
    cid = at_point(conn)
    tick(conn, DAY + "T12:30:00")
    first = [r for r in calls(conn, cid) if r[1] == order_call.EXPIRES]
    tasks.close(conn, first[0][0], "done", DAY + "T13:00:00", "owner", "продлили")
    conn.execute("UPDATE order_card SET reserve_until = '2026-10-20' WHERE id = ?", (cid,))
    conn.commit()
    tick(conn, "2026-10-20T12:30:00")
    assert len([r for r in calls(conn, cid, open_only=False) if r[1] == order_call.EXPIRES]) == 2


# ---- the client, the stage, the alerts replaced ------------------------------------------------------------------------------------------

def test_nothing_goes_to_the_client_and_the_order_stays_at_the_point(tmp_path):
    conn = db(tmp_path)
    cid = at_point(conn)
    notify = Notify()
    deliver = tick(conn, DAY + "T12:30:00", notify)
    for when in (DAY + "T23:07:00", "2026-10-19T03:07:00", "2026-10-19T12:30:00", "2026-10-20T12:30:00"):
        tick(conn, when, notify, deliver)
    assert deliver.sent == []
    assert orders.get_card(conn, cid)["stage"]["code"] == "at_point"                    # never «На ОТХ» by itself
    assert conn.execute("SELECT COUNT(*) FROM chat_send_log").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM manager_message").fetchone()[0] == 0      # no line of «Входящие»
    texts = [t for _c, t in notify.sent]
    assert not [t for t in texts if t.startswith("Напоминание о сроке резерва") or t.startswith(("Сегодня (", "Резерв по заказу"))]
    assert not [e for e in orders.list_events(conn, cid) if e["level"] == "red"]


def test_the_arrival_writes_no_red_record_nor_a_line_of_the_inbox(tmp_path):
    conn = db(tmp_path)
    cid = at_point(conn)
    sent, note = mgr.tell_client_arrived(conn, cid, Deliver(conn), ARRIVE, "менеджер Бета")
    assert not sent and "Позвонить, товары приехали" in note
    assert not [e for e in orders.list_events(conn, cid) if e["level"] == "red"]
    assert conn.execute("SELECT COUNT(*) FROM manager_message").fetchone()[0] == 0
    told = []
    order_arrived.check(conn, "2026-10-15T12:07:00", lambda logins, text: told.append(text))
    assert told == []                                                                   # «Приехал, а клиенту не сообщили»: the task instead
    assert not [e for e in orders.list_events(conn, cid) if e["text"].startswith(order_arrived.LINE)]


def test_an_order_with_a_chat_is_as_before(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "moving", "2026-10-14T10:00:00", avito_chat="")
    chat = chat_of(conn, cid)
    orders.move_card(conn, cid, stage(conn, "at_point"), ARRIVE, "o")
    notify = Notify()
    deliver = tick(conn, DAY + "T09:30:00", notify)
    tick(conn, DAY + "T12:30:00", notify, deliver)
    assert [c for c, _t, _k in deliver.sent] == [chat]                                  # the reminder goes in the chat
    assert any(t.startswith("Сегодня (") for _c, t in notify.sent)                      # the alert of the last day as before
    assert calls(conn, cid, open_only=False) == []


# ---- the sound ---------------------------------------------------------------------------------------------------------------------

def test_the_sound_has_a_new_key_every_2_hours_until_done(tmp_path):
    conn = db(tmp_path)
    cid = at_point(conn, staffed(conn))
    task_id = calls(conn, cid)[0][0]
    keys = lambda when: [i["key"] for i in order_call.sounds(conn, "manager1", when)]
    assert keys("2026-10-15T10:00:00") == ["call-task-%d-0" % task_id]
    assert keys("2026-10-15T11:59:59") == ["call-task-%d-0" % task_id]
    assert keys("2026-10-15T12:00:00") == ["call-task-%d-1" % task_id]
    assert keys("2026-10-16T03:00:00") == ["call-task-%d-8" % task_id]                  # day and night (the owner's decision)
    assert order_call.sounds(conn, "owner", "2026-10-15T12:00:00") == []                # the assignee's only
    item = order_call.sounds(conn, "manager1", "2026-10-15T12:00:00")[0]
    assert item["href"] == "/orders/%d" % cid and item["title"] == order_call.ARRIVED
    tasks.close(conn, task_id, "done", "2026-10-15T12:30:00", "manager1", "позвонил")
    assert keys("2026-10-15T14:00:00") == []


def test_the_office_feed_gives_its_own_call_tasks(tmp_path):
    from panel_helpers import login, make_app
    from tires_sync import store
    app = make_app(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, "2026-10-01T10:00:00")
    cid = at_point(conn)
    client = app.test_client()
    login(client)
    data = client.get("/tasks/feed").get_json()
    assert data["who"] == "owner" and [i["title"] for i in data["items"]] == [order_call.ARRIVED]
    assert data["items"][0]["key"].startswith("call-task-") and data["items"][0]["href"] == "/orders/%d" % cid
    page = client.get("/tasks").get_data(as_text=True)
    assert 'data-feed="/tasks/feed"' in page and "Важное · Позвонить, товары приехали" in page
    assert "Клиенту не сообщили" not in client.get("/orders/%d" % cid).get_data(as_text=True)     # the card's line: the task instead


def test_the_journal_actions_are_moscow_time():
    from tires_sync import panel
    for action in ("callback", "callback_cancel", "callback_done"):                     # tasks.add / close of the tasks «Позвонить…»
        assert action in panel.MOSCOW_AUDIT
