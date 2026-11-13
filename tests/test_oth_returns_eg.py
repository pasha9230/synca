"""The owner 2026-11-12, the task E–G of the return of «На ОТХ»:
E1 the seventh day: the task of the responsible stays, the point is asked nothing (no question, nothing to VK);
E2 «Есть покупатель» of «Согласование возврата»: 2 days, one urgent task «Важное», refused after 2 days and for another point;
E3 one act for several orders of one point: one PDF on «point + date», every order agreed for the date; two points refused;
E4 what counts as the agreement: one function (order_oth.agreed), the date only for now;
G «Водитель не забрал» of «Сегодня возврат!»: the window, one task, the office's bell, the sound stops, the date stays, nothing to VK."""
from __future__ import annotations

import io
import logging
import os
import threading
from datetime import date, datetime

import pytest

from panel_helpers import post
from test_mypoint import manager, token
from test_point_oth_sell import setup
from test_point_return_tabs import to_return_stage
from test_return_act import PDF, person
from test_return_day import DAY, agreed, clock, feed_keys, in_panel  # noqa: F401  (clock: a fixture)
from test_vk_commands import POINT, _card, _stage, to_client, to_points
from tires_sync import order_oth, order_points as points, orders, oth_point_return as opr, tasks
from tires_sync.panel import MOSCOW_AUDIT, routes_orders


def open_tasks(conn, title, order_id=None):
    sql = "SELECT id FROM callback_task WHERE title = ? AND done_at IS NULL AND cancelled_at IS NULL" + (" AND order_id = ?" if order_id else "")
    return [tasks.get(conn, r[0]) for r in conn.execute(sql, (title,) + ((order_id,) if order_id else ()))]


def other_point(conn, name="Гамма"):
    return points.save_point(conn, dict(POINT, name=name, vk_peer_id="777001"))


# ---- E1 ---------------------------------------------------------------------------------------------------------------------------------

def test_e1_the_seventh_day_leaves_the_task_and_asks_the_point_nothing(tmp_path):
    from test_oth_seventh_day import on_oth
    from test_order_reserve import card_at, db
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", "2026-10-15T10:00:00")
    on_oth(conn, cid)
    told = []
    assert order_oth.ask_seventh_day(conn, "2026-10-26T12:30:00", lambda card, text: told.append((card["responsible"], text))) == 1
    order_oth.finish_moved(conn, "2026-10-26T13:30:00", lambda card, text: told.append((card["responsible"], text)))
    card = orders.get_card(conn, cid)
    assert card["stage"]["code"] == orders.RETURN_AGREE
    assert [t["title"] for t in open_tasks(conn, order_oth.RETURN_TASK, cid)] == [order_oth.RETURN_TASK]     # «Согласовать возврат» stays
    assert not order_oth.question_open(conn, card)
    assert [t for who, t in told if who == ""] == []                                      # nothing to the point (it is on VK)
    assert not [e for e in orders.list_events(conn, cid) if "задан вопрос" in e["text"]]


# ---- E2 ---------------------------------------------------------------------------------------------------------------------------------

ENTERED = "2026-10-15T10:00:00"                     # to_return_stage: «На возврат», not agreed — «Согласование возврата»


def test_e2_the_button_for_two_days_one_task_and_the_server_refuses_after(tmp_path, clock):
    app, owner, conn, pid, card = setup(tmp_path)
    to_return_stage(conn, card)
    assert order_oth.agreeing_since(conn, orders.get_card(conn, card)) == ENTERED
    mine = manager(app, conn, pid)
    clock["now"] = "2026-10-16T11:00:00"
    page = mine.get("/my-point?tab=agree").get_data(as_text=True)
    assert 'data-oth-buyer="%d"' % card in page and 'data-confirm-title="Есть покупатель на эти шины?"' in page
    assert "Есть покупатель" not in mine.get("/my-point?tab=oth").get_data(as_text=True)
    kept = token(page, card, "oth_buyer")
    assert post(mine, "/my-point/%d/oth_buyer" % card, "tok", token=kept).status_code == 302
    [task] = open_tasks(conn, opr.BUYER_TITLE)
    assert task["title"] == "Важное · Точка нашла покупателя на шины с ОТХ: проверьте возможность продажи и оформите продажу"
    assert task["order_id"] == card and task["due_at"] == "2026-10-16T11:00" and task["kind"] == "other"
    assert "Точка «Бета»" in task["note"] and "/orders/%d" % card in task["note"] and "Состав:" in task["note"] and "шт." in task["note"]
    assert task["assignee"] == order_oth.office_assignee(conn, card)
    again = token(mine.get("/my-point?tab=agree").get_data(as_text=True), card, "oth_buyer")
    assert post(mine, "/my-point/%d/oth_buyer" % card, "tok", token=again).status_code == 302
    assert len(open_tasks(conn, opr.BUYER_TITLE)) == 1                                     # a second press: no second open task
    late = token(mine.get("/my-point?tab=agree").get_data(as_text=True), card, "oth_buyer")
    clock["now"] = "2026-10-17T10:00:00"                                                   # 2 days since 15.10 10:00: gone
    assert 'data-oth-buyer="%d"' % card not in mine.get("/my-point?tab=agree").get_data(as_text=True)
    tasks.close(conn, task["id"], "done", "2026-10-17T10:00:00", "owner")
    assert post(mine, "/my-point/%d/oth_buyer" % card, "tok", token=late).status_code == 302
    assert open_tasks(conn, opr.BUYER_TITLE) == []                                         # the server refuses too
    outcome, _answer, done = opr.tell_buyer(conn, orders.get_card(conn, card), "Бета", "менеджер", "2026-10-17T10:00:00")
    assert not done and outcome.startswith("не подходит")


def test_e2_another_point_cannot_press_it(tmp_path, clock):
    app, owner, conn, pid, card = setup(tmp_path)
    to_return_stage(conn, card)
    clock["now"] = "2026-10-16T11:00:00"
    stranger = manager(app, conn, other_point(conn), "delta")
    r = post(stranger, "/my-point/%d/oth_buyer" % card, "tok", token="a" * 24)
    assert r.status_code in (403, 404)
    assert open_tasks(conn, opr.BUYER_TITLE) == []


def test_e2_the_moment_of_the_status_on_oth_is_the_first_mark_and_unagree_starts_it_again(tmp_path):
    app, owner, conn, pid, card = setup(tmp_path)
    item = orders.list_items(conn, card)[0]["id"]
    assert order_oth.agreeing_since(conn, orders.get_card(conn, card)) == ""
    assert order_oth.item_to_return(conn, item, "2026-10-16T12:00:00", "owner", qty=2)
    assert order_oth.agreeing_since(conn, orders.get_card(conn, card)) == "2026-10-16T12:00:00"
    assert opr.buyer_until(conn, orders.get_card(conn, card)) == "2026-10-18T12:00:00"
    other = _card(conn, pid, code="at_point", webmim="5000300")
    orders.move_card(conn, other, _stage(conn, orders.RETURN_GIVEN), "2026-10-20T10:00:00", "o")
    order_oth.agree(conn, orders.get_card(conn, other), date(2026, 10, 20), "2026-10-20T11:00:00", "owner", "2026-10-23")
    assert order_oth.agreeing_since(conn, orders.get_card(conn, other)) == ""             # agreed: not on the agreement
    assert order_oth.unagree(conn, orders.get_card(conn, other), "2026-10-21T09:00:00", "owner")
    assert order_oth.agreeing_since(conn, orders.get_card(conn, other)) == "2026-10-21T09:00:00"
    order_oth.ask_seventh_day(conn, "2026-10-21T12:30:00")                                              # the 7th day after the mark:
    assert orders.get_card(conn, card)["stage"]["code"] == orders.RETURN_AGREE                          # on the stage since the mark (2026-11-13)
    assert order_oth.agreeing_since(conn, orders.get_card(conn, card)) == "2026-10-16T12:00:00"           # no new 2 days


# ---- E3 ---------------------------------------------------------------------------------------------------------------------------------

ACT_DAY = "2026-11-16"                               # a Monday


@pytest.fixture
def office_clock(monkeypatch):
    monkeypatch.setattr(routes_orders, "local_now", lambda: datetime(2026, 11, 12, 15, 0))
    monkeypatch.setattr(routes_orders, "local_iso", lambda: "2026-11-12T15:00:00")


def two_orders(conn, pid):
    to_return_stage(conn, 1)
    more = _card(conn, pid, code="at_point", webmim="5000200")
    orders.move_card(conn, more, _stage(conn, "oth"), "2026-10-14T09:00:00", "точка", changes={"cancel_reason": "Клиент не пришёл"})
    to_return_stage(conn, more)
    return [1, more]


def act_post(client, ids, day=ACT_DAY, data=PDF, name="act.pdf"):
    return client.post("/oth/return-act/orders", data={"csrf": "tok", "order": [str(i) for i in ids], "return_date": day,
                                                       "file": (io.BytesIO(data), name)}, content_type="multipart/form-data")


def test_e3_one_act_for_several_orders_of_one_point(tmp_path, office_clock):
    app, owner, conn, pid, card = setup(tmp_path)
    assert card == 1
    ids = two_orders(conn, pid)
    page = owner.get("/oth").get_data(as_text=True)
    assert all(page.count('data-act-order="%d"' % i) == 1 for i in ids) and 'id="oth-act-orders"' in page
    assert act_post(owner, ids).status_code == 302
    assert [order_oth.return_plan(conn, i)["return_date"] for i in ids] == [ACT_DAY, ACT_DAY]
    assert conn.execute("SELECT point_id, return_date, data FROM oth_return_act").fetchall() == [(pid, ACT_DAY, PDF)]     # one PDF
    assert conn.execute("SELECT COUNT(*) FROM audit WHERE action = 'oth_return_agreed'").fetchone()[0] == 2
    assert all(not order_oth.agreeing(conn, orders.get_card(conn, i)) for i in ids)       # agreed as by «Согласовано»


def test_e3_orders_of_two_points_are_refused_and_nothing_changes(tmp_path, office_clock):
    app, owner, conn, pid, card = setup(tmp_path)
    to_return_stage(conn, card)
    gamma = other_point(conn)
    theirs = _card(conn, gamma, code="at_point", webmim="5000400")
    orders.move_card(conn, theirs, _stage(conn, orders.RETURN_GIVEN), "2026-10-15T10:00:00", "o")
    r = act_post(owner, [card, theirs])
    assert r.status_code == 302
    assert "разных точек" in owner.get(r.headers["Location"]).get_data(as_text=True)
    assert order_oth.return_plan(conn, card) is None and order_oth.return_plan(conn, theirs) is None
    assert conn.execute("SELECT COUNT(*) FROM oth_return_act").fetchone()[0] == 0


def test_e3_the_pdf_is_checked_and_another_date_of_an_agreed_order_is_refused(tmp_path, office_clock):
    app, owner, conn, pid, card = setup(tmp_path)
    ids = two_orders(conn, pid)
    r = act_post(owner, ids, data=b"not a pdf", name="act.txt")
    assert "только файл PDF" in owner.get(r.headers["Location"]).get_data(as_text=True)
    assert all(order_oth.return_plan(conn, i) is None for i in ids)                        # checked before anything is agreed
    order_oth.agree(conn, orders.get_card(conn, ids[0]), date(2026, 11, 12), "2026-11-12T14:00:00", "owner", "2026-11-18")
    r = act_post(owner, ids)
    assert "уже согласован на 18.11" in owner.get(r.headers["Location"]).get_data(as_text=True)
    assert order_oth.return_plan(conn, ids[1]) is None


def test_e3_a_point_has_no_such_form_and_cannot_post_it(tmp_path, office_clock):
    app, owner, conn, pid, card = setup(tmp_path)
    ids = two_orders(conn, pid)
    mine = manager(app, conn, pid)
    assert act_post(mine, ids).status_code in (302, 403)
    assert all(order_oth.return_plan(conn, i) is None for i in ids)


# ---- E4 ---------------------------------------------------------------------------------------------------------------------------------

def test_e4_what_counts_as_the_agreement_is_one_function(tmp_path, monkeypatch):
    app, owner, conn, pid, card = setup(tmp_path)
    to_return_stage(conn, card)
    assert not order_oth.agreed(conn, orders.get_card(conn, card)) and order_oth.agreeing(conn, orders.get_card(conn, card))
    order_oth.agree(conn, orders.get_card(conn, card), date(2026, 11, 12), "2026-11-12T10:00:00", "owner", ACT_DAY)
    assert order_oth.NEED_ACT is False                                                     # for now: the date is enough (as before)
    assert order_oth.agreed(conn, orders.get_card(conn, card)) and not order_oth.agreeing(conn, orders.get_card(conn, card))
    monkeypatch.setattr(order_oth, "NEED_ACT", True)                                       # the owner may switch it: the date and the act
    assert not order_oth.agreed(conn, orders.get_card(conn, card)) and order_oth.agreeing(conn, orders.get_card(conn, card))
    from tires_sync import oth_return_act
    oth_return_act.save(conn, pid, ACT_DAY, "act.pdf", PDF, "2026-11-12T10:05:00", "owner")
    assert order_oth.agreed(conn, orders.get_card(conn, card)) and not order_oth.to_agree(conn, orders.get_card(conn, card))


# ---- G ----------------------------------------------------------------------------------------------------------------------------------

def told_today(tmp_path):
    app, owner, conn, pid, card = setup(tmp_path)
    in_panel(conn, pid)
    agreed(conn, card)
    assert order_oth.tell_return_day(conn, DAY + "T09:00:00") == 1
    return app, owner, conn, pid, card


def test_g_the_window_the_task_the_bell_the_sound_and_no_doubles(tmp_path, clock):
    app, owner, conn, pid, card = told_today(tmp_path)
    mine = manager(app, conn, pid)
    office = {role: person(app, conn, login, role) for login, role in (("zaman", "partner"), ("olga", "chats"), ("vika", "viewer"))}
    page = mine.get("/my-point").get_data(as_text=True)
    box = page[page.index('data-not-taken="%s"' % DAY):].split("</details>")[0]
    assert "Водитель не забрал" in box and 'action="/my-point/return-day/not-taken"' in box and 'name="comment"' in box
    assert "Подписал акт и отдал" in page                                                  # the old button stays
    rang = feed_keys(mine)
    assert "return-day-%s-09" % DAY.replace("-", "") in rang
    before = (len(to_client(app)), len(to_points(app)))
    assert post(mine, "/my-point/return-day/not-taken", "tok", comment="Машина сломалась").status_code == 302
    page = mine.get("/my-point").get_data(as_text=True)
    box = page[page.index('data-not-taken="%s"' % DAY):].split("</div>")[0]
    assert "font-size:24px" in box and "Отправьте в следующие дни возвратов" in box and 'name="comment"' not in box
    assert "<details" not in box and "mp-handover" not in box             # review 13.11: an open «mp-handover» would stop the page's own refresh
    [task] = open_tasks(conn, opr.NOT_TAKEN_TITLE)
    assert task["title"] == "Важное · Водитель не забрал возвраты, согласовать с менеджером" and task["order_id"] == card
    assert "Машина сломалась" in task["note"] and "/orders/%d" % card in task["note"] and "13.11" in task["note"]
    assert task["assignee"] == order_oth.office_assignee(conn, card)
    clock["now"] = DAY + "T12:05:00"
    later = feed_keys(mine)
    assert not [k for k in later if k.startswith("return-day-")] and set(later) <= set(rang)     # the sound of the date stopped: no new key
    assert order_oth.return_day(conn, pid, clock["now"])["sound"] == "09"                  # the bell row keeps its key: no new ring
    for client in (owner, office["partner"], office["chats"]):
        items = client.get("/alerts/feed").get_json()["items"]
        [row] = [i for i in items if i["title"] == opr.NOT_TAKEN_TITLE]
        assert "Машина сломалась" in row["text"] and row["href"] == "/orders/%d" % card
    assert not [i for i in office["viewer"].get("/alerts/feed").get_json()["items"] if i["title"] == opr.NOT_TAKEN_TITLE]
    assert not [i for i in mine.get("/alerts/feed").get_json()["items"] if i["title"] == opr.NOT_TAKEN_TITLE]
    assert post(mine, "/my-point/return-day/not-taken", "tok", comment="ещё раз").status_code == 302
    assert len(open_tasks(conn, opr.NOT_TAKEN_TITLE)) == 1                                 # no double
    assert order_oth.return_plan(conn, card)["return_date"] == DAY                         # the date is not moved
    assert (len(to_client(app)), len(to_points(app))) == before                            # nothing to VK nor to a client
    tasks.close(conn, task["id"], "done", DAY + "T13:00:00", "owner")
    assert not [i for i in owner.get("/alerts/feed").get_json()["items"] if i["title"] == opr.NOT_TAKEN_TITLE]


def test_g_without_a_return_today_nothing_is_marked(tmp_path, clock):
    app, owner, conn, pid, card = setup(tmp_path)
    in_panel(conn, pid)
    mine = manager(app, conn, pid)
    assert post(mine, "/my-point/return-day/not-taken", "tok").status_code == 302
    assert open_tasks(conn, opr.NOT_TAKEN_TITLE) == [] and opr.not_taken(conn, pid, DAY) is None


def test_the_new_actions_of_the_journal_are_on_the_moscow_side():
    assert {"oth_buyer_found", "oth_not_taken"} <= MOSCOW_AUDIT


# ---- a phone of 375 px ------------------------------------------------------------------------------------------------------------------

try:
    from playwright import sync_api
except ImportError:                                                                       # pragma: no cover
    sync_api = None
CHROMIUM = "/opt/pw-browsers/chromium"


@pytest.mark.parametrize("new_view", [False, True])
def test_a_phone_of_375_px_has_no_horizontal_scroll(tmp_path, clock, new_view):
    if sync_api is None:
        pytest.skip("no Playwright")
    from werkzeug.serving import make_server
    app, owner, conn, pid, card = told_today(tmp_path)
    if new_view:
        from test_point_new_view import flag
        flag(conn, "all")
    more = _card(conn, pid, code="at_point", webmim="5000200")
    to_return_stage(conn, more)                                                            # «Есть покупатель» on «Согласование возврата»
    clock["now"] = "2026-10-16T11:00:00"
    mine = manager(app, conn, pid)
    assert 'data-oth-buyer="%d"' % more in mine.get("/my-point?tab=agree").get_data(as_text=True)
    clock["now"] = DAY + "T09:05:00"
    post(mine, "/my-point/return-day/not-taken", "tok", comment="Длинный комментарий точки " * 8)
    cookie = mine.get_cookie("session").value
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_api.sync_playwright() as p:
            try:
                b = p.chromium.launch(executable_path=CHROMIUM) if os.path.exists(CHROMIUM) else p.chromium.launch()
            except Exception as err:                                                       # noqa: BLE001 - no browser on this machine
                pytest.skip("no Chromium: %s" % err)
            try:
                ctx = b.new_context(viewport={"width": 375, "height": 800})
                ctx.add_cookies([{"name": "session", "value": cookie, "domain": "127.0.0.1", "path": "/"}])
                page = ctx.new_page()
                for when, tab in ((DAY + "T09:05:00", "return"), ("2026-10-16T11:00:00", "agree")):
                    clock["now"] = when
                    page.goto("http://127.0.0.1:%d/my-point?tab=%s" % (server.server_port, tab))
                    if tab == "return":
                        assert page.locator("[data-not-taken-next]").is_visible()
                    else:
                        assert page.locator('[data-oth-buyer="%d"] button' % more).is_visible()
                    wide = page.evaluate("document.documentElement.scrollWidth")
                    assert wide <= 375, (tab, wide)
            finally:
                b.close()
    finally:
        server.shutdown()
