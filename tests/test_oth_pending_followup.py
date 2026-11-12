"""Review of run/2026-11-10f, the owner 2026-11-10: «Выдали» before the agreement closes the task of the return as not needed (no «Продано до
согласования»); an agreed return writes nothing «до согласования»; «Отменить» of a sale or of «Выдали» and «Вернуть на ОТХ» bring back the mark
«На возврат» and its task."""
from __future__ import annotations

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
    sell(manager(app, conn, pid), card, tab="agree")
    assert task_rows(conn, card) == [(None, T1)]
    assert any(t.startswith(order_oth.SOLD_EARLY) for t in texts(conn, card)) and not any(t.startswith(ISSUED) for t in texts(conn, card))


# ---- 2. «Отменить» and «Вернуть на ОТХ» bring the mark «На возврат» and its task back -----------------------------------------------------

def press(client, card, action, tab="agree"):     # a marked order: «Согласование возврата» (2026-11-12)
    from test_mypoint import token
    page = client.get("/my-point?tab=%s" % tab).get_data(as_text=True)
    r = post(client, "/my-point/%d/%s" % (card, action), "tok", token=token(page, card, action), confirm="yes")
    assert r.status_code == 302, r.status_code


def undo(client, conn, card, clock):
    clock["now"] = T2
    r = post(client, "/my-point/%d/undo" % card, "tok")
    assert r.status_code == 302 and "отменена" in client.get(r.headers["Location"]).get_data(as_text=True)


def mark_is_back(conn, card, item, qty=4):
    now = orders.get_card(conn, card)
    assert now["stage"]["code"] == "oth"
    assert conn.execute("SELECT qty FROM oth_return WHERE item_id = ?", (item,)).fetchone() == (qty,)
    mark = order_oth.item_mark(order_oth.item_marks(conn), item, now["entered_at"])
    assert mark and mark["qty"] == qty                                                  # «Ожидает согласование возврата» again
    assert order_oth.pieces_here(conn, now) == {item: 4} and not order_oth.given_back(conn, card)
    assert sum(order_oth.goods_on_oth(conn).values()) == 4                             # and on sale until agreed
    [task] = conn.execute("SELECT id, assignee, title FROM callback_task WHERE order_id = ? AND done_at IS NULL AND cancelled_at IS NULL", (card,)).fetchall()
    first = conn.execute("SELECT assignee FROM callback_task WHERE order_id = ? ORDER BY id LIMIT 1", (card,)).fetchone()[0]
    assert task[2] == order_oth.RETURN_TASK and task[1] == first and mark["task"] == task[0]     # the same responsible
    return task


def test_sold_then_undone_the_mark_and_the_task_are_back_and_the_tyres_on_sale(tmp_path, clock):
    app, conn, pid, card, item, owner = marked(tmp_path)
    mine = manager(app, conn, pid)
    sell(mine, card, tab="agree")
    assert not conn.execute("SELECT 1 FROM oth_return WHERE order_id = ?", (card,)).fetchone() and open_tasks(conn, card) == 0
    undo(mine, conn, card, clock)
    mark_is_back(conn, card, item)
    assert any(t.startswith("Метка «На возврат» возвращена") for t in texts(conn, card))
    assert 'action="/my-point/%d/oth_sold"' % card in mine.get("/my-point?tab=agree").get_data(as_text=True)


def test_issued_then_undone_the_mark_and_the_task_are_back(tmp_path, clock):
    app, conn, pid, card, item, owner = marked(tmp_path)
    ask(conn, card)
    mine = manager(app, conn, pid)
    press(mine, card, "oth_issued")
    assert orders.get_card(conn, card)["stage"]["code"] == "done" and open_tasks(conn, card) == 0
    undo(mine, conn, card, clock)
    mark_is_back(conn, card, item)
    assert order_oth.question_open(conn, orders.get_card(conn, card))


def test_a_part_of_a_mark_marked_then_sold_and_undone_brings_back_only_that_part(tmp_path, clock):
    app, conn, pid, card, item, owner = marked(tmp_path, qty=3)
    mine = manager(app, conn, pid)
    sell(mine, card, tab="agree")
    undo(mine, conn, card, clock)
    mark_is_back(conn, card, item, qty=3)


def test_taken_then_given_back_to_oth_the_mark_is_back(tmp_path):
    """The owner 2026-11-10 (evening): the pieces of a mark «На возврат» are sold by the point only, never taken by «Взять с ОТХ»
    (oth_stock). So only the free piece is taken; its give-back leaves the mark, its pieces and its task as they were."""
    from test_oth_sale import T, code, new_order, take
    from tests.test_order_reserve import card_at, db
    from tires_sync import oth_sale
    conn = db(tmp_path)
    old = card_at(conn, "oth", T)
    item = conn.execute("SELECT id FROM order_item WHERE order_id = ?", (old,)).fetchone()[0]
    assert order_oth.item_to_return(conn, item, T, "o", "3")
    first = new_order(conn, qty=1)
    assert take(conn, first) == 1                                                       # the one free piece
    entered = orders.get_card(conn, old)["entered_at"]
    assert order_oth.item_mark(order_oth.item_marks(conn), item, entered)["qty"] == 3 and open_tasks(conn, old) == 1
    oth_sale.untake(conn, first, orders.get_card(conn, first)["items"][0]["id"], "2026-10-29T12:00:00", "o")
    assert code(conn, old) == "oth" and conn.execute("SELECT qty FROM oth_return WHERE item_id = ?", (item,)).fetchone() == (3,)
    assert order_oth.item_mark(order_oth.item_marks(conn), item, entered)["qty"] == 3 and open_tasks(conn, old) == 1
    assert [i["qty"] for i in orders.get_card(conn, old)["items"]] == [4] and not order_oth.given_back(conn, old)


