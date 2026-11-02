"""The owner 2026-11-02: «На ОТХ» is held six days; on the seventh from 12:30 the point is asked on its «Рабочее место» «Заказ выдали или он
на точке?». «Выдали» closes the order as issued (its client linked again); «На точке» leaves it on «На ОТХ» for the return: the point's tab
«На возврат», an alert and a task «Согласовать возврат» for the owner and the partner (Заман: found by the role, never by the login)."""
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


def on_oth(conn, cid, when=ENTERED):
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
    assert asked["text"] == "Точке «Бета» задан вопрос на «Рабочем месте»: «Заказ выдали или он на точке?»"


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


def test_here_is_for_the_return_a_task_and_an_alert_for_the_owner_and_the_partner(tmp_path):
    conn = db(tmp_path)
    office(conn)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    on_oth(conn, cid)
    order_oth.ask_seventh_day(conn, "2026-10-26T12:30:00")
    told = []
    outcome, answer, done = order_oth.answer_here(conn, orders.get_card(conn, cid), "менеджер Бета", "2026-10-26T13:00:00",
                                                  lambda logins, text: told.append((logins, text)))
    assert done and "Возврат согласуют" in answer
    card = orders.get_card(conn, cid)
    assert card["stage"]["code"] == "oth" and order_oth.to_agree(conn, card) and not order_oth.question_open(conn, card)
    assert told == [(["owner", "zaman"], "Заказ №5200001 (точка «Бета») 7 дней на ОТХ, шины на точке: согласуйте возврат с поставщиком — /orders/%d" % cid)]
    made = [tasks.get(conn, r[0]) for r in conn.execute("SELECT id FROM callback_task WHERE order_id = ? ORDER BY id", (cid,))]
    assert [(t["assignee"], t["title"], t["kind"]) for t in made] == [("owner", "Согласовать возврат", "other"), ("zaman", "Согласовать возврат", "other")]
    assert made[0]["note"] == "Заказ №5200001, точка «Бета», /orders/%d" % cid
    assert any(e["level"] == "yellow" and "шины на точке" in e["text"] for e in orders.list_events(conn, cid))
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
    orders.move_card(conn, here, _stage(conn, "oth"), ENTERED, "o", changes={"cancel_reason": "Клиент не пришёл"})
    order_oth.ask_seventh_day(conn, "2026-10-26T12:30:00")
    return app, conn, pid, here, manager(app, conn, pid)


def test_the_banner_is_on_the_oth_tab_and_its_feed_rings(tmp_path, clock):
    app, conn, pid, here, mine = asked_on_page(tmp_path)
    page = mine.get("/my-point?tab=oth").get_data(as_text=True)
    block = page[page.index('data-oth-question="%d"' % here):].split("</div></div>")[0]
    assert "Заказ выдали или он на точке?" in block
    assert "/my-point/%d/oth_issued" % here in block and "/my-point/%d/oth_here" % here in block
    assert 'data-tab="oth"' in page and re.search(r'mp-tab--hot" href="/my-point\?tab=oth', page)
    keys = [i["key"] for i in mine.get("/my-point/feed").get_json()["items"]]
    assert any(k.startswith("oth-question-%d-" % here) for k in keys)


def test_here_on_the_page_moves_the_card_to_the_return_tab(tmp_path, clock, monkeypatch):
    from tires_sync import order_notify
    sent = []
    monkeypatch.setattr(order_notify, "tell_people", lambda conn, box, logins, text, **k: sent.append((logins, text)) or "ok")
    app, conn, pid, here, mine = asked_on_page(tmp_path)
    _t, page = press(mine, here, "oth_here")
    assert "Возврат согласуют с поставщиком" in page and sent and sent[0][0][0] == "owner"
    assert 'id="order-%d"' % here not in mine.get("/my-point?tab=oth").get_data(as_text=True)
    back = mine.get("/my-point?tab=return").get_data(as_text=True)
    assert "Ждут согласования возврата" in back and 'id="order-%d"' % here in back and "возврат согласуют с поставщиком" in back
    assert [r["kind"] for r in point_log.history(conn, pid)][0] == "oth_here"


def test_issued_on_the_page_closes_it_and_can_be_undone_with_the_question_open_again(tmp_path, clock):
    app, conn, pid, here, mine = asked_on_page(tmp_path)
    press(mine, here, "oth_issued")
    assert orders.get_card(conn, here)["stage"]["code"] == "done"
    page = workplace(mine)
    assert 'action="/my-point/%d/undo"' % here in page
    clock["now"] = "2026-10-26T14:00:00"
    r = post(mine, "/my-point/%d/undo" % here, "tok")
    assert "отменена" in mine.get(r.headers["Location"]).get_data(as_text=True)
    card = orders.get_card(conn, here)
    assert card["stage"]["code"] == "oth" and card["entered_at"] == ENTERED and order_oth.question_open(conn, card)
    assert conn.execute("SELECT 1 FROM client_order WHERE order_id = ?", (here,)).fetchone() is None      # unlinked again on «На ОТХ»
    assert store.get_state(conn, order_oth._ask_key(card)).startswith("asked|")
