"""Review of run/2026-11-10f, the owner 2026-11-10: «Выдали» before the agreement closes the task of the return as not needed (no «Продано до
согласования»); an agreed return writes nothing «до согласования»; «Отменить» of a sale or of «Выдали» and «Вернуть на ОТХ» bring back the mark
«На возврат» and its task."""
from __future__ import annotations

import re
from datetime import date

from panel_helpers import post
from test_mypoint import manager
from test_point_oth_sell import T1, T2, clock, marked, open_tasks, sell, setup, texts  # noqa: F401 - clock is a fixture
from test_vk_commands import _stage
from tires_sync import order_oth, orders

ISSUED = "Заказ выдан, возврат не нужен"


def ask(conn, card):
    """The question of the seventh day is open: «Выдали» is on the card."""
    conn.execute("INSERT OR REPLACE INTO app_state (key, value) VALUES (?, ?)", (order_oth._ask_key(orders.get_card(conn, card)), "asked|" + T1))
    conn.commit()


def issue(conn, card, now=T1):
    outcome, answer, done = order_oth.answer_issued(conn, orders.get_card(conn, card), "gamma", now)
    assert done, answer


def task_rows(conn, card) -> list:
    return conn.execute("SELECT done_at, cancelled_at FROM callback_task WHERE order_id = ? ORDER BY id", (card,)).fetchall()


def test_issued_before_the_agreement_closes_the_task_as_not_needed(tmp_path, clock):
    app, conn, pid, card, item, owner = marked(tmp_path)
    ask(conn, card)
    issue(conn, card)
    assert orders.get_card(conn, card)["stage"]["code"] == "done" and open_tasks(conn, card) == 0
    assert task_rows(conn, card) == [(T1, None)]                                           # done (not needed), not cancelled
    assert not conn.execute("SELECT 1 FROM oth_return WHERE order_id = ?", (card,)).fetchone()
    assert not any(order_oth.SOLD_EARLY.lower() in t.lower() for t in texts(conn, card))
    assert any(t.startswith(ISSUED) for t in texts(conn, card))
    assert any(t.startswith("Задача выполнена:") and t.endswith("заказ выдан, возврат не нужен") for t in texts(conn, card))


def test_issued_from_the_return_not_agreed_closes_the_task_as_not_needed(tmp_path, clock):
    app, owner, conn, pid, card = setup(tmp_path)
    orders.move_card(conn, card, _stage(conn, orders.RETURN_GIVEN), "2026-10-15T09:00:00", "owner")
    before = orders.get_card(conn, card)
    order_oth.moved_by_hand(conn, dict(before, stage=dict(before["stage"], code="oth")), "2026-10-15T09:00:00", "owner")
    assert open_tasks(conn, card) == 1
    issue(conn, card)
    assert task_rows(conn, card) == [(T1, None)]
    assert not any(order_oth.SOLD_EARLY.lower() in t.lower() for t in texts(conn, card))


def test_issued_after_the_agreement_writes_nothing_of_a_sale_before_it(tmp_path, clock):
    app, conn, pid, card, item, owner = marked(tmp_path)
    order_oth.agree(conn, orders.get_card(conn, card), date.fromisoformat("2026-10-16"), "2026-10-14T12:00:00", "owner")
    assert orders.get_card(conn, card)["stage"]["code"] == orders.RETURN_GIVEN and task_rows(conn, card) == [("2026-10-14T12:00:00", None)]
    ask(conn, card)
    issue(conn, card, T2)
    assert orders.get_card(conn, card)["stage"]["code"] == "done"
    assert not any(order_oth.SOLD_EARLY.lower() in t.lower() or t.startswith(ISSUED) for t in texts(conn, card))
    assert "Согласование возврата снято: точка ответила, что заказ выдали" in texts(conn, card)


def test_a_sale_still_writes_sold_before_the_agreement(tmp_path, clock):
    app, conn, pid, card, item, owner = marked(tmp_path)
    sell(manager(app, conn, pid), card)
    assert task_rows(conn, card) == [(None, T1)]
    assert any(t.startswith(order_oth.SOLD_EARLY) for t in texts(conn, card)) and not any(t.startswith(ISSUED) for t in texts(conn, card))


