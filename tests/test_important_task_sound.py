"""The owner 2026-11-28: the important tasks ring in the working day only (10:00–19:00 Moscow) — the office at most 2 times a day per person,
the panel of a point at most 3 times a day per point, 3 hours apart, while the task is open; each with its own switch (on by default).
Made-up data only."""
from __future__ import annotations

from panel_helpers import post
from test_call_tasks import at_point, staffed
from test_order_reserve import db
from test_point_notify import clock, env, office_task, view  # noqa: F401 - the fixtures
from tires_sync import office_notify, task_sound, tasks


def keys(conn, login, now):
    return [(i["key"], bool(i.get("quiet"))) for i in office_notify.calls(conn, login, now)]


# ---- the office: 2 a day, 3 hours apart, in the working hours ------------------------------------------------------------------------

def test_the_office_rings_at_most_twice_a_working_day_three_hours_apart(tmp_path):
    conn = db(tmp_path)
    at_point(conn, staffed(conn))                                             # «Важное · Позвонить, товары приехали» at 10:00
    [(first, quiet)] = keys(conn, "manager1", "2026-10-15T10:00:00")
    assert first.endswith("-20261015-1") and not quiet                        # rings when it comes
    assert keys(conn, "manager1", "2026-10-15T12:59:00") == [(first, False)]   # less than 3 hours: the same key, no ring
    [(second, _)] = keys(conn, "manager1", "2026-10-15T13:00:00")
    assert second.endswith("-20261015-2")                                     # the second ring
    assert keys(conn, "manager1", "2026-10-15T16:30:00") == [(second, False)]  # 2 a day: no third
    assert keys(conn, "manager1", "2026-10-16T09:59:00")[0][1] is True         # before 10:00: quiet
    assert keys(conn, "manager1", "2026-10-16T10:00:00")[0][0].endswith("-20261016-1")   # the next day: again, while open


def test_a_task_after_the_ring_waits_quietly_and_plain_tasks_never_ring(tmp_path):
    conn = db(tmp_path)
    staffed(conn)
    one = tasks.add(conn, kind="other", title="Важное · Разобраться", due_at="2026-10-15T10:00", note="", now="2026-10-15T10:00:00",
                    author="owner", assignee="manager1")
    tasks.add(conn, kind="other", title="Обычная", due_at="2026-10-14T10:00", note="", now="2026-10-14T10:00:00", author="owner",
              assignee="manager1")                                           # overdue but not important: no sound (2026-10-31)
    assert [k for k, _ in keys(conn, "manager1", "2026-10-15T10:00:00")] == ["call-task-%d-20261015-1" % one]
    two = tasks.add(conn, kind="other", title="Важное · Ещё одна", due_at="2026-10-15T11:00", note="", now="2026-10-15T11:00:00",
                    author="owner", assignee="manager1")
    assert keys(conn, "manager1", "2026-10-15T11:00:00") == [("call-task-%d-20261015-1" % one, False), ("call-task-%d-wait" % two, True)]
    assert keys(conn, "manager1", "2026-10-15T14:00:00") == [("call-task-%d-20261015-2" % one, False), ("call-task-%d-20261015-2" % two, False)]
    tasks.close(conn, one, "done", "2026-10-15T14:10:00", "owner")
    tasks.close(conn, two, "done", "2026-10-15T14:10:00", "owner")
    assert keys(conn, "manager1", "2026-10-15T18:00:00") == []               # done: no more sound


def test_out_of_hours_and_switched_off_take_no_ring(tmp_path):
    conn = db(tmp_path)
    staffed(conn)
    tasks.add(conn, kind="other", title="Важное · Вечером", due_at="2026-10-15T20:00", note="", now="2026-10-15T20:00:00", author="owner",
              assignee="manager1")
    assert keys(conn, "manager1", "2026-10-15T20:00:00")[0][1] is True        # 19:00 and later: quiet
    assert task_sound.rings(conn, "office:manager1", "2026-10-15T20:00:00", 2, due=False) == []
    office_notify.put(conn, "manager1", False)
    assert keys(conn, "manager1", "2026-10-16T10:00:00")[0][1] is True        # off: quiet, and no ring is spent
    assert task_sound.rings(conn, "office:manager1", "2026-10-16T10:00:00", 2, due=False) == []
    from tires_sync import store
    store.set_state(conn, office_notify.KEY % "manager1", "1,0")              # the value of #97: read by its first part
    assert office_notify.get(conn, "manager1") == {"sound": True}
    store.set_state(conn, office_notify.KEY % "manager1", "garbage")
    assert office_notify.get(conn, "manager1") == office_notify.DEFAULT


# ---- the point: 3 a day, its own switch ---------------------------------------------------------------------------------------------

def point_keys(client):
    return [(i["key"], bool(i.get("quiet"))) for i in client.get("/my-point/feed").get_json()["items"] if i["key"].startswith("office-task-")]


def test_the_point_rings_at_most_three_times_a_working_day_and_switches_it(tmp_path, clock):
    app, owner, tok, conn, a, b, mine = env(tmp_path)
    task = office_task(conn)                                                  # due 2026-11-26: the red «!» from that day
    clock["now"] = "2026-11-25T10:00:00"
    assert point_keys(mine) == []                                             # its day has not come
    rung = []
    for at in ("2026-11-26T10:00:00", "2026-11-26T12:00:00", "2026-11-26T13:00:00", "2026-11-26T16:00:00", "2026-11-26T18:59:00"):
        clock["now"] = at
        rung.append(point_keys(mine))
    base = "office-task-%d-20261126-" % task
    assert rung == [[(base + "1", False)], [(base + "1", False)], [(base + "2", False)], [(base + "3", False)], [(base + "3", False)]]
    page = mine.get("/my-point").get_data(as_text=True)
    assert 'action="/my-point/notify/tasks"' in page and "не чаще 3 раз в день" in page and "Выключить звук задач" in page
    post(mine, "/my-point/notify/tasks", "tok", on="0", back="/my-point")
    assert not task_sound.point_on(conn, a) and task_sound.point_on(conn, b)
    clock["now"] = "2026-11-27T10:00:00"
    assert point_keys(mine) == []                                             # off: no sound (the task stays on «Сводки»)
    assert "Включить звук задач" in mine.get("/my-point").get_data(as_text=True)
    post(mine, "/my-point/notify/tasks", "tok", on="1", back="/my-point")
    assert point_keys(mine) == [("office-task-%d-20261127-1" % task, False)]
    assert post(owner, "/my-point/notify/tasks", tok, on="0").status_code == 403   # the office does not switch it for the point


def test_the_office_working_as_the_point_gets_no_ring_of_its_tasks(tmp_path, clock):
    app, owner, tok, conn, a, b, mine = env(tmp_path)
    office_task(conn)
    assert post(owner, "/as-point", tok, point_id=str(a)).status_code in (200, 302)
    assert point_keys(owner) == []
