"""The owner 2026-11-02: «На ОТХ» is held six days. The owner 2026-11-04: on the seventh from 12:30 the order goes to «На возврат» by itself and the
point is asked on its «Рабочее место» «Заказ выдали или он на точке?». «Выдали» closes the order as issued (its client linked again); «На точке» only marks
the answer. The order's responsible has one task."""
from __future__ import annotations

import re

import pytest

from panel_helpers import post
from test_mypoint import manager, press, workplace
from test_order_reserve import card_at, db, ok, stage
from test_vk_commands import _card, _stage, ready
from tires_sync import order_oth, order_reserve as reserve, orders, point_log, store, tasks, users
from tires_sync.panel import routes_mypoint

ENTERED = "2026-10-20T11:00:00"                       # the first day on «На ОТХ»; the seventh is 2026-10-26


def started(conn, at="2026-10-01T00:00:00"):
    """The first tick of the rule (the deploy): orders that enter «На ОТХ» since are asked (the owner 2026-11-02)."""
    conn.execute("INSERT OR IGNORE INTO app_state (key, value) VALUES (?, ?)", (order_oth.SINCE_KEY, at))
    conn.commit()


def on_oth(conn, cid, when=ENTERED):
    started(conn)
    orders.move_card(conn, cid, stage(conn, "oth"), when, "точка", changes={"cancel_reason": "Клиент не пришёл"})
    return orders.get_card(conn, cid)


def office(conn):
    users.add_user(conn, "zaman", "password-123", ENTERED, role="partner")
    users.add_user(conn, "viewer1", "password-123", ENTERED, role="viewer")
    conn.commit()


def test_the_question_comes_on_the_seventh_day_at_12_30_not_on_the_sixth_nor_at_12_29(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    on_oth(conn, cid)
    for when in ("2026-10-25T23:59:00", "2026-10-26T12:29:59"):
        assert order_oth.ask_seventh_day(conn, when) == 0, when
    assert not order_oth.question_open(conn, orders.get_card(conn, cid))
    assert reserve.check(conn, "2026-10-26T12:30:00", ok) == 0                           # the hourly tick asks it
    assert order_oth.question_open(conn, orders.get_card(conn, cid))
    assert order_oth.ask_seventh_day(conn, "2026-10-26T13:07:00") == 0                   # once per stay on «На ОТХ»
    assert order_oth.ask_seventh_day(conn, "2026-10-27T13:07:00") == 0
    [asked] = [e for e in orders.list_events(conn, cid) if "задан вопрос" in e["text"]]
    assert asked["text"] == "Точке «Бета» задан вопрос: «Заказ выдали или он на точке?»"     # the point on VK gets it there too (2026-11-05)


def test_a_later_day_still_asks_and_an_agreed_return_is_never_asked(tmp_path):
    conn = db(tmp_path)
    late = card_at(conn, "at_point", "2026-10-15T10:00:00")
    on_oth(conn, late)
    agreed = card_at(conn, "at_point", "2026-10-15T10:00:00")
    order_oth.agree(conn, on_oth(conn, agreed), orders.date.fromisoformat("2026-10-21"), "2026-10-21T10:00:00", "owner")
    assert order_oth.ask_seventh_day(conn, "2026-10-29T09:07:00") == 1                   # a missed tick: asked at the next one
    assert order_oth.question_open(conn, orders.get_card(conn, late))
    assert not order_oth.question_open(conn, orders.get_card(conn, agreed))


def test_issued_closes_the_order_and_links_its_client_again(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    linked = conn.execute("SELECT client_id FROM client_order WHERE order_id = ?", (cid,)).fetchone()
    on_oth(conn, cid)
    assert conn.execute("SELECT 1 FROM client_order WHERE order_id = ?", (cid,)).fetchone() is None     # unlinked on «На ОТХ»
    order_oth.ask_seventh_day(conn, "2026-10-26T12:30:00")
    outcome, answer, done = order_oth.answer_issued(conn, orders.get_card(conn, cid), "менеджер Бета", "2026-10-26T13:00:00")
    assert done and outcome == "заказ перенесён на «Выполнено»" and "отмечено: выдан" in answer
    assert orders.get_card(conn, cid)["stage"]["code"] == "done"
    assert conn.execute("SELECT client_id FROM client_order WHERE order_id = ?", (cid,)).fetchone() == linked
    assert not order_oth.question_open(conn, orders.get_card(conn, cid))
    assert order_oth.answer_issued(conn, orders.get_card(conn, cid), "менеджер Бета", "2026-10-26T13:01:00")[2] is False     # a second press


def test_no_answer_without_the_question(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    on_oth(conn, cid)
    assert order_oth.answer_issued(conn, orders.get_card(conn, cid), "менеджер Бета", "2026-10-25T13:00:00")[2] is False
    assert order_oth.answer_here(conn, orders.get_card(conn, cid), "менеджер Бета", "2026-10-25T13:00:00")[2] is False
    assert orders.get_card(conn, cid)["stage"]["code"] == "oth"


def test_the_seventh_day_moves_the_order_with_one_task_of_the_responsible_and_here_only_marks(tmp_path):
    conn = db(tmp_path)
    office(conn)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    on_oth(conn, cid)
    told = []
    assert order_oth.ask_seventh_day(conn, "2026-10-26T12:30:00", lambda card, text: told.append((card["responsible"], text))) == 1
    to_point = [t for who, t in told if who == ""]                                       # Бета is on VK: the status and the question (2026-11-05)
    assert len(to_point) == 1 and "на точке 5200001" in to_point[0]
    told = [(who, t) for who, t in told if who]
    card = orders.get_card(conn, cid)
    assert card["stage"]["code"] == orders.RETURN_GIVEN and card["entered_at"] == "2026-10-26T12:30:00"
    assert order_oth.to_agree(conn, card) and order_oth.question_open(conn, card)
    assert told == [("owner", "Нужно согласовать на возврат: заказ №5200001 (точка «Бета») на ОТХ с 20.10.2026 — /orders/%d" % cid)]
    made = [tasks.get(conn, r[0]) for r in conn.execute("SELECT id FROM callback_task WHERE order_id = ? ORDER BY id", (cid,))]
    assert [(t["assignee"], t["title"], t["kind"]) for t in made] == [("owner", order_oth.RETURN_TASK, "return")]     # one, not the owner's and the partner's
    assert order_oth.RETURN_TASK == "Написать поставщику и согласовать возврат, узнать дату забора груза"
    outcome, answer, done = order_oth.answer_here(conn, card, "менеджер Бета", "2026-10-26T13:00:00")
    assert done and "Возврат поставщику идёт дальше" in answer and outcome == "отмечено: шины на точке"
    card = orders.get_card(conn, cid)
    assert card["stage"]["code"] == orders.RETURN_GIVEN and not order_oth.question_open(conn, card)
    assert conn.execute("SELECT COUNT(*) FROM callback_task WHERE order_id = ?", (cid,)).fetchone()[0] == 1     # no second task
    order_oth.agree(conn, card, orders.date.fromisoformat("2026-10-27"), "2026-10-27T10:00:00", "owner")
    assert not order_oth.to_agree(conn, orders.get_card(conn, cid))                      # agreed: the plaque of the act МХ-3 as before


# ---- the page ---------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def clock(monkeypatch):
    moment = {"now": "2026-10-26T13:00:00"}
    monkeypatch.setattr(routes_mypoint, "local_iso", lambda: moment["now"])
    return moment


def asked_on_page(tmp_path):
    app, client, tok, conn, pid, card = ready(tmp_path)
    here = _card(conn, pid, webmim="700200", code="at_point")
    started(conn)
    orders.move_card(conn, here, _stage(conn, "oth"), ENTERED, "o", changes={"cancel_reason": "Клиент не пришёл"})
    order_oth.ask_seventh_day(conn, "2026-10-26T12:30:00")
    return app, conn, pid, here, manager(app, conn, pid)


def test_the_banner_is_on_the_return_tab_and_its_feed_rings(tmp_path, clock):
    app, conn, pid, here, mine = asked_on_page(tmp_path)
    page = mine.get("/my-point?tab=return").get_data(as_text=True)
    block = page[page.index('data-oth-question="%d"' % here):].split("</div></div>")[0]
    assert "Заказ выдали или он на точке?" in block
    assert "/my-point/%d/oth_issued" % here in block and "/my-point/%d/oth_here" % here in block
    assert re.search(r'mp-tab--hot" href="/my-point\?tab=return', page)
    keys = [i["key"] for i in mine.get("/my-point/feed").get_json()["items"]]
    assert any(k.startswith("oth-question-%d-" % here) for k in keys)


def test_here_on_the_page_only_closes_the_question(tmp_path, clock):
    app, conn, pid, here, mine = asked_on_page(tmp_path)
    _t, page = press(mine, here, "oth_here")
    assert "Возврат поставщику идёт дальше" in page
    assert orders.get_card(conn, here)["stage"]["code"] == orders.RETURN_GIVEN
    back = mine.get("/my-point?tab=return").get_data(as_text=True)
    assert "Ждут согласования возврата" in back and 'id="order-%d"' % here in back and "возврат согласуют с поставщиком" in back
    assert 'data-oth-question="%d"' % here not in back
    assert [r["kind"] for r in point_log.history(conn, pid)][0] == "oth_here"


def test_issued_on_the_page_closes_it_and_can_be_undone_with_the_question_open_again(tmp_path, clock):
    app, conn, pid, here, mine = asked_on_page(tmp_path)
    press(mine, here, "oth_issued")
    assert orders.get_card(conn, here)["stage"]["code"] == "done"
    page = mine.get("/my-point?tab=issued").get_data(as_text=True)    # the owner 2026-11-10: «Отменить» in «Выдано», no list on the page
    assert 'action="/my-point/%d/undo"' % here in page
    clock["now"] = "2026-10-26T14:00:00"
    r = post(mine, "/my-point/%d/undo" % here, "tok")
    assert "отменена" in mine.get(r.headers["Location"]).get_data(as_text=True)
    card = orders.get_card(conn, here)
    assert card["stage"]["code"] == orders.RETURN_GIVEN and card["entered_at"] == "2026-10-26T12:30:00" and order_oth.question_open(conn, card)
    assert conn.execute("SELECT 1 FROM client_order WHERE order_id = ?", (here,)).fetchone() is None      # unlinked again on «На возврат»
    assert store.get_state(conn, order_oth._ask_key(card)).startswith("asked|")
    assert conn.execute("SELECT COUNT(*) FROM callback_task WHERE order_id = ? AND done_at IS NULL AND cancelled_at IS NULL", (here,)).fetchone()[0] == 1     # on again


def test_issued_after_a_partial_return_cannot_be_undone(tmp_path, clock):
    """Review D3: the undo of «Выдали» would enter «На ОТХ» again, and entering drops the marks «возвращено поставщику»: refused."""
    app, conn, pid, here, mine = asked_on_page(tmp_path)
    item = orders.list_items(conn, here)[0]["id"]
    order_oth.mark_returned(conn, item, "2026-10-26T12:40:00", "owner", qty=1)
    press(mine, here, "oth_issued")
    assert orders.get_card(conn, here)["stage"]["code"] == "done"
    assert 'action="/my-point/%d/undo"' % here not in workplace(mine)
    r = post(mine, "/my-point/%d/undo" % here, "tok")
    assert "уже не отменить" in mine.get(r.headers["Location"]).get_data(as_text=True)
    assert conn.execute("SELECT qty FROM oth_return WHERE item_id = ?", (item,)).fetchone()[0] == 1


# ---- the answers of 2026-11-02 (the owner, the lead) --------------------------------------------------------------------------------

def test_only_orders_that_entered_oth_after_the_first_tick_are_asked(tmp_path):
    conn = db(tmp_path)
    old = card_at(conn, "at_point", "2026-10-15T10:00:00")
    orders.move_card(conn, old, stage(conn, "oth"), ENTERED, "точка", changes={"cancel_reason": "Клиент не пришёл"})
    assert order_oth.ask_seventh_day(conn, "2026-10-25T13:07:00") == 0                   # the deploy: the first tick keeps its moment
    assert store.get_state(conn, order_oth.SINCE_KEY) == "2026-10-25T13:07:00"
    new = card_at(conn, "at_point", "2026-10-15T10:00:00")
    orders.move_card(conn, new, stage(conn, "oth"), "2026-10-26T10:00:00", "точка", changes={"cancel_reason": "Клиент не пришёл"})
    assert order_oth.ask_seventh_day(conn, "2026-11-01T12:30:00") == 1
    assert order_oth.question_open(conn, orders.get_card(conn, new)) and not order_oth.question_open(conn, orders.get_card(conn, old))
    assert order_oth.ask_seventh_day(conn, "2026-11-02T12:30:00") == 0 and store.get_state(conn, order_oth.SINCE_KEY) == "2026-10-25T13:07:00"


def test_a_partly_returned_order_is_not_asked(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    on_oth(conn, cid)
    order_oth.mark_returned(conn, orders.list_items(conn, cid)[0]["id"], "2026-10-22T10:00:00", "owner", qty=1)
    assert order_oth.ask_seventh_day(conn, "2026-10-26T12:30:00") == 0


def test_the_tasks_close_by_themselves_on_the_agreement_or_on_issued(tmp_path):
    def open_tasks(conn, cid):
        return conn.execute("SELECT COUNT(*) FROM callback_task WHERE order_id = ? AND done_at IS NULL AND cancelled_at IS NULL", (cid,)).fetchone()[0]
    conn = db(tmp_path)
    office(conn)
    agreed, issued = (card_at(conn, "at_point", "2026-10-15T10:00:00") for _ in range(2))
    for cid in (agreed, issued):
        on_oth(conn, cid)
    order_oth.ask_seventh_day(conn, "2026-10-26T12:30:00")
    for cid in (agreed, issued):
        assert open_tasks(conn, cid) == 1                                               # the responsible's only (the owner 2026-11-04)
    order_oth.agree(conn, orders.get_card(conn, agreed), orders.date.fromisoformat("2026-10-27"), "2026-10-27T10:00:00", "owner")
    assert open_tasks(conn, agreed) == 0
    assert order_oth.answer_issued(conn, orders.get_card(conn, issued), "менеджер Бета", "2026-10-27T11:00:00")[2]
    assert open_tasks(conn, issued) == 0
    assert any(e["text"].startswith("Задача выполнена: Вернуть: %s — согласовано с поставщиком" % order_oth.RETURN_TASK) for e in orders.list_events(conn, agreed))
