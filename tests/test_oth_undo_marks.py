"""Review 11.11 of run/2026-11-11a: «Отменить» of «Выдали» after the agreement keeps the marks «На возврат»; a broken saved mark never
breaks drop_marks; restore_marks leaves the commit to its caller, so «Отменить» is one transaction."""
from __future__ import annotations

from datetime import date

import pytest

from test_mypoint import manager
from test_oth_pending_followup import ask, issue, press, undo
from test_point_oth_sell import T2, clock, marked, open_tasks, texts  # noqa: F401 - clock is a fixture
from tires_sync import order_oth, orders, point_log

AGREED_AT = "2026-10-14T12:00:00"


def held(conn, card) -> list:
    return conn.execute("SELECT item_id, qty, at, author FROM oth_return WHERE order_id = ? ORDER BY item_id", (card,)).fetchall()


def marks(conn) -> list:
    return conn.execute("SELECT key, value FROM app_state WHERE key LIKE 'oth_item_return:%' ORDER BY key").fetchall()


def tasks_of(conn, card) -> list:
    return conn.execute("SELECT id, done_at, cancelled_at FROM callback_task WHERE order_id = ? ORDER BY id", (card,)).fetchall()


# ---- 1. метка → согласовано → «Выдали» → «Отменить»: всё как до «Выдали» -----------------------------------------------------------------

def test_issued_after_the_agreement_then_undone_keeps_the_marks_and_the_task_as_they_were(tmp_path, clock):
    app, conn, pid, card, item, owner = marked(tmp_path)
    order_oth.agree(conn, orders.get_card(conn, card), date.fromisoformat("2026-10-16"), AGREED_AT, "owner")
    before = (held(conn, card), marks(conn), tasks_of(conn, card), order_oth.return_plan(conn, card))
    assert before[0] and before[3] and orders.get_card(conn, card)["stage"]["code"] == orders.RETURN_GIVEN
    ask(conn, card)
    mine = manager(app, conn, pid)
    press(mine, card, "oth_issued")
    assert orders.get_card(conn, card)["stage"]["code"] == "done" and order_oth.return_plan(conn, card) is None
    undo(mine, conn, card, clock)
    assert orders.get_card(conn, card)["stage"]["code"] == orders.RETURN_GIVEN
    assert (held(conn, card), marks(conn), tasks_of(conn, card), order_oth.return_plan(conn, card)) == before
    assert not order_oth.given_back(conn, card) and open_tasks(conn, card) == 0      # the task closed by the agreement stays closed


# ---- 2. битое сохранённое значение метки не роняет drop_marks ------------------------------------------------------------------------------

def broken(conn, item):
    conn.execute("INSERT INTO app_state (key, value) VALUES (?, ?)", ("oth_item_return:%d:19990101000000" % item, "{not json"))
    conn.commit()


def test_a_broken_mark_is_skipped_when_the_order_is_issued(tmp_path, clock):
    app, conn, pid, card, item, owner = marked(tmp_path)
    broken(conn, item)
    ask(conn, card)
    issue(conn, card)
    assert orders.get_card(conn, card)["stage"]["code"] == "done" and not held(conn, card)
    assert any(t.startswith(order_oth.ISSUED_NO_RETURN) and "4 шт." in t for t in texts(conn, card))


def test_a_broken_mark_is_skipped_when_a_part_is_dropped(tmp_path, clock):
    app, conn, pid, card, item, owner = marked(tmp_path)
    broken(conn, item)
    assert order_oth.drop_marks(conn, card, T2, "owner", item_id=item, pieces=1) == 1
    conn.commit()
    assert held(conn, card)[0][1] == 3
    good = [v for k, v in marks(conn) if not k.endswith("19990101000000")]
    assert len(good) == 1 and '"qty": 3' in good[0]


# ---- 3. «Отменить» одной транзакцией: restore_marks не коммитит --------------------------------------------------------------------------

def test_restore_marks_leaves_the_commit_to_the_caller(tmp_path, clock):
    app, conn, pid, card, item, owner = marked(tmp_path)
    ask(conn, card)
    press(manager(app, conn, pid), card, "oth_issued")
    point = dict(zip(("id", "name"), conn.execute("SELECT id, name FROM point WHERE id = ?", (pid,)).fetchone()))
    stored = (held(conn, card), marks(conn), tasks_of(conn, card), conn.execute("SELECT key, value FROM app_state WHERE key LIKE 'point_undo:%'").fetchall())
    assert stored[3], "«Выдали» left its undo"
    real = orders.add_event

    def fail(conn_, order_id, now, who, kind, text, *a, **kw):
        if kind == "point" and text.startswith("Точка «"):                         # the step right after restore_marks
            raise RuntimeError("сбой")
        return real(conn_, order_id, now, who, kind, text, *a, **kw)

    card_now = orders.get_card(conn, card)
    orig = point_log.orders.add_event
    point_log.orders.add_event = fail
    try:
        with pytest.raises(RuntimeError):
            point_log.undo(conn, card_now, point, "gamma", T2)
    finally:
        point_log.orders.add_event = orig
    conn.rollback()
    assert orders.get_card(conn, card)["stage"]["code"] == "done"                      # nothing of the undo was saved
    assert (held(conn, card), marks(conn), tasks_of(conn, card),
            conn.execute("SELECT key, value FROM app_state WHERE key LIKE 'point_undo:%'").fetchall()) == stored
