"""The owner 2026-11-10: «Продать» on «На ОТХ» of «Рабочее место». The tyres of an order the client refused lie at the point and are sold to a
buyer from the street: the point marks it at any day (no question of the seventh day is needed), with a confirmation (the order's number, the
tyres, the quantity, the price of one and the sum). The whole order goes to «Выполнено», the old client is NOT linked again and hears
nothing; the sale is the rule's of «Выполнено», once; after the act МХ-3: refused; «Отменить» puts everything back. Both views of the point."""
from __future__ import annotations

import re
from datetime import date

import pytest

from panel_helpers import post
from test_mypoint import manager, token
from test_point_new_view import flag
from test_point_webmim_hide import assert_no_webmim
from test_vk_commands import SENT, _card, _stage, ready, to_client
from tires_sync import order_engine as engine, order_oth, order_points as points, order_rules as rules, order_templates as tpl, orders
from tires_sync.panel import routes_mypoint

ON_OTH, T1, T2 = "2026-10-14T09:00:00", "2026-10-16T11:00:00", "2026-10-16T12:30:00"      # the third day on «На ОТХ»: no question yet


@pytest.fixture
def clock(monkeypatch):
    moment = {"now": T1}
    monkeypatch.setattr(routes_mypoint, "local_iso", lambda: moment["now"])
    return moment


@pytest.fixture(params=["old", "new"])
def view(request):
    return request.param


def setup(tmp_path, view="old"):
    """Татьяна's order (5000100, 4 tyres at 5 000, bought at 4 000) refused at the point: on «На ОТХ», its client put aside."""
    app, owner, tok, conn, pid, card = ready(tmp_path, SENT)
    if view == "new":
        flag(conn, "all")
    orders.move_card(conn, card, _stage(conn, "at_point"), "2026-10-13T12:00:00", "o")
    orders.move_card(conn, card, _stage(conn, "oth"), ON_OTH, "точка", changes={"cancel_reason": "Клиент не пришёл"})
    conn.commit()
    return app, owner, conn, pid, card


def sell(client, card, tab="oth", again=None, p=None):
    page = client.get("/my-point?tab=%s" % tab + ("&p=%d" % p if p else "")).get_data(as_text=True)
    data = dict(token=again or token(page, card, "oth_sold"), confirm="yes")
    if p:
        data["p"] = str(p)
    r = post(client, "/my-point/%d/oth_sold" % card, "tok", **data)
    assert r.status_code == 302, r.status_code
    return data["token"]


def linked(conn, card):
    return conn.execute("SELECT client_id FROM client_order WHERE order_id = ?", (card,)).fetchone()


def sales(conn, card) -> list:
    return conn.execute("SELECT pieces, sale_total, cost_total FROM sale WHERE note = ? AND reversed_at IS NULL", ("Заказ №%d" % card,)).fetchall()


def texts(conn, card) -> list:
    return [e["text"] for e in orders.list_events(conn, card)]


def test_the_button_is_on_the_card_with_no_question_and_confirms_with_number_tyres_quantity_and_price(tmp_path, clock, view):
    app, owner, conn, pid, card = setup(tmp_path, view)
    mine = manager(app, conn, pid)
    page = mine.get("/my-point?tab=oth").get_data(as_text=True)
    assert not order_oth.question_open(conn, orders.get_card(conn, card)) and "data-oth-question" not in page
    form = re.search(r'<form method="post" action="/my-point/%d/oth_sold"[^>]*>.*?</form>' % card, page, re.S).group(0)
    assert 'data-oth-sell="%d"' % card in page and ">Продать</button>" in form and "btn--primary" in form and 'name="csrf"' in form
    confirm = re.search(r'data-confirm="([^"]*)"', form).group(1)
    assert confirm.split("\n") == ["№ заказа: 5000100", "Nokian Hakkapeliitta 10 × 4 шт.: 5 000 ₽ за шт., сумма 20 000 ₽",
                                   "Шины проданы с ОТХ; заказ закроется как выполненный."]
    assert "Продать шины заказа № 5000100?" in form and 'data-confirm-ok="Да, продал"' in form and "Татьяна" not in confirm
    assert_no_webmim(page.replace("5000100", ""), "the tab «На ОТХ» with «Продать»", numbers=False)
    assert "oth_sold" not in mine.get("/my-point?tab=here").get_data(as_text=True)          # only what lies on «На ОТХ»


def test_without_the_script_the_press_asks_on_a_page_and_changes_nothing(tmp_path, clock):
    app, owner, conn, pid, card = setup(tmp_path)
    mine = manager(app, conn, pid)
    tok = token(mine.get("/my-point?tab=oth").get_data(as_text=True), card, "oth_sold")
    r = post(mine, "/my-point/%d/oth_sold" % card, "tok", token=tok)
    page = r.get_data(as_text=True)
    assert r.status_code == 200 and "data-issue-confirm" in page and "Продать шины заказа № 5000100?" in page and "Да, продал" in page
    assert "5 000 ₽ за шт., сумма 20 000 ₽" in page and "заказ закроется как выполненный" in page
    assert_no_webmim(page.replace("5000100", ""), "the page of the confirmation", numbers=False)
    assert orders.get_card(conn, card)["stage"]["code"] == "oth"
    r = post(mine, "/my-point/%d/oth_sold" % card, "tok", token=tok, confirm="yes")
    assert r.status_code == 302 and orders.get_card(conn, card)["stage"]["code"] == "done"


def test_sold_closes_the_order_without_its_client_records_one_sale_and_writes_nobody(tmp_path, clock, view):
    app, owner, conn, pid, card = setup(tmp_path, view)
    done = _stage(conn, "done")
    rules.add_rule(conn, done, "enter", 0, "record_sale", None, "", "auto", None)           # the sale of «Выполнено», as for any order
    thanks = tpl.add_template(conn, "Клиенту: спасибо за покупку", "client", "{client_name}, спасибо за покупку!")
    rules.add_rule(conn, done, "enter", 0, "message", thanks, "", "auto", None)
    conn.commit()
    assert linked(conn, card) is None                                                       # put aside on «На ОТХ»
    assert conn.execute("SELECT 1 FROM avito_chat WHERE order_id = ?", (card,)).fetchone()     # the old client's chat is still the order's
    before = len(to_client(app))
    mine = manager(app, conn, pid)
    sell(mine, card)
    now = orders.get_card(conn, card)
    assert now["stage"]["code"] == "done" and linked(conn, card) is None                    # the buyer is another person: nobody is linked
    assert not any("Клиент снова привязан" in t for t in texts(conn, card))
    assert sales(conn, card) == [(4, 20000.0, 16000.0)] and len(now["sale_ids"]) == 1        # once, at the order's own prices
    assert "Точка продала шины с ОТХ" in texts(conn, card)
    assert [e["kind"] for e in orders.list_events(conn, card) if e["text"] == "Точка продала шины с ОТХ"] == ["point"]
    assert len(to_client(app)) == before                                                    # nothing went to the old client's chat
    [message] = [t for t in engine.list_tasks(conn, card) if t["kind"] == "message" and t["stage_id"] == done]
    assert message["status"] == "skipped" and message["result"] == order_oth.SOLD_SKIP      # and nobody can press it later
    with pytest.raises(orders.OrderError):
        engine.send_task(conn, message["id"], "спасибо", lambda *a: "sent", T2, "owner")
    assert len(to_client(app)) == before
    issued = mine.get("/my-point?tab=issued").get_data(as_text=True)
    assert 'data-issued="%d"' % card in issued                                              # «Выдано» of the point
    assert "oth_sold" not in mine.get("/my-point?tab=oth").get_data(as_text=True)
    log = conn.execute("SELECT word, outcome FROM order_command_log WHERE order_id = ? ORDER BY rowid DESC LIMIT 1", (card,)).fetchone()
    assert log == ("продано (ОТХ)", "заказ перенесён на «Выполнено»")


def test_no_rule_of_the_sale_no_sale_made_up_and_the_order_still_closes(tmp_path, clock):
    app, owner, conn, pid, card = setup(tmp_path)
    sell(manager(app, conn, pid), card)
    assert orders.get_card(conn, card)["stage"]["code"] == "done" and sales(conn, card) == []     # «Продажа» is the office's then, as after «Выдал»


def test_a_double_press_and_a_second_tab_sell_once(tmp_path, clock):
    app, owner, conn, pid, card = setup(tmp_path)
    rules.add_rule(conn, _stage(conn, "done"), "enter", 0, "record_sale", None, "", "auto", None)
    mine = manager(app, conn, pid)
    other_tab = token(mine.get("/my-point?tab=oth").get_data(as_text=True), card, "oth_sold")
    used = sell(mine, card)
    for tok in (used, other_tab):                                                           # the same press again; the press of a stale tab
        r = post(mine, "/my-point/%d/oth_sold" % card, "tok", token=tok, confirm="yes")
        assert r.status_code == 302
    assert sales(conn, card) == [(4, 20000.0, 16000.0)]
    assert texts(conn, card).count("Точка продала шины с ОТХ") == 1
    assert conn.execute("SELECT outcome FROM order_command_log WHERE message_id = ?", ("page-" + other_tab,)).fetchone() == ("уже отмечено",)


def test_another_points_order_is_404_and_csrf_is_needed(tmp_path, clock):
    app, owner, conn, pid, card = setup(tmp_path)
    other = points.save_point(conn, {"name": "Гамма", "channel": "vk"})
    stranger = manager(app, conn, other, "delta")
    mine = manager(app, conn, pid)
    tok = token(mine.get("/my-point?tab=oth").get_data(as_text=True), card, "oth_sold")
    assert post(stranger, "/my-point/%d/oth_sold" % card, "tok", token=tok, confirm="yes").status_code in (403, 404)     # the gate of the panel
    # past the gate (the owner working as another point): the order is not of the point chosen
    assert post(owner, "/my-point/%d/oth_sold" % card, "tok", token=tok, confirm="yes", p=str(other)).status_code == 404
    assert mine.post("/my-point/%d/oth_sold" % card, data={"token": tok, "confirm": "yes"}).status_code in (400, 403)     # no csrf
    assert orders.get_card(conn, card)["stage"]["code"] == "oth"
    sell(owner, card, p=pid)                                                                # the owner working as the point
    assert orders.get_card(conn, card)["stage"]["code"] == "done"


def test_after_the_signed_act_it_is_refused_and_the_button_is_gone(tmp_path, clock):
    app, owner, conn, pid, card = setup(tmp_path)
    mine = manager(app, conn, pid)
    tok = token(mine.get("/my-point?tab=oth").get_data(as_text=True), card, "oth_sold")
    orders.move_card(conn, card, _stage(conn, orders.RETURN_GIVEN), "2026-10-15T09:00:00", "owner")
    order_oth.agree(conn, orders.get_card(conn, card), date.fromisoformat("2026-10-17"), "2026-10-15T10:00:00", "owner")
    assert 'action="/my-point/%d/oth_sold"' % card in mine.get("/my-point?tab=return").get_data(as_text=True)     # agreed, not signed: still sold
    conn.execute("UPDATE oth_return_plan SET signed_at = ?, signed_by = 'gamma' WHERE order_id = ?", ("2026-10-16T10:00:00", card))
    conn.commit()
    assert "oth_sold" not in "".join(mine.get("/my-point?tab=" + t).get_data(as_text=True) for t in ("oth", "return"))
    r = post(mine, "/my-point/%d/oth_sold" % card, "tok", token=tok, confirm="yes")
    assert r.status_code == 302 and orders.get_card(conn, card)["stage"]["code"] == orders.RETURN_GIVEN
    outcome, answer, done = order_oth.sell(conn, orders.get_card(conn, card), "gamma", T2)
    assert not done and outcome == "не выполнено: акт МХ-3 подписан" and "акт МХ-3 уже подписан" in answer and "«Продать» нельзя" in answer
    assert "Точка продала шины с ОТХ" not in texts(conn, card)


def test_undo_puts_the_order_back_on_oth_and_it_can_be_sold_again(tmp_path, clock, view):
    app, owner, conn, pid, card = setup(tmp_path, view)
    mine = manager(app, conn, pid)
    sell(mine, card)
    assert "Отменить «Продать»" in mine.get("/my-point?tab=issued").get_data(as_text=True)
    clock["now"] = T2
    r = post(mine, "/my-point/%d/undo" % card, "tok")
    now = orders.get_card(conn, card)
    assert r.status_code == 302 and now["stage"]["code"] == "oth" and now["entered_at"] == ON_OTH and linked(conn, card) is None
    assert not order_oth.question_open(conn, now)                                           # none was open: none is asked by the undo
    assert 'action="/my-point/%d/oth_sold"' % card in mine.get("/my-point?tab=oth").get_data(as_text=True)


def test_undo_from_the_return_brings_back_the_agreement_the_question_and_the_task(tmp_path, clock):
    app, owner, conn, pid, card = setup(tmp_path)
    conn.execute("INSERT OR IGNORE INTO app_state (key, value) VALUES (?, ?)", (order_oth.SINCE_KEY, "2026-10-01T00:00:00"))
    orders.move_card(conn, card, _stage(conn, orders.RETURN_GIVEN), "2026-10-15T09:00:00", "owner")
    before = orders.get_card(conn, card)
    order_oth.moved_by_hand(conn, dict(before, stage=dict(before["stage"], code="oth")), "2026-10-15T09:00:00", "owner")     # its task, its question
    order_oth.agree(conn, orders.get_card(conn, card), date.fromisoformat("2026-10-17"), "2026-10-15T10:00:00", "owner")
    plan = order_oth.return_plan(conn, card)
    assert plan and order_oth.question_open(conn, orders.get_card(conn, card))
    mine = manager(app, conn, pid)
    sell(mine, card, tab="return")
    assert orders.get_card(conn, card)["stage"]["code"] == "done" and order_oth.return_plan(conn, card) is None
    assert "Согласование возврата снято: точка продала шины с ОТХ" in texts(conn, card)
    assert not conn.execute("SELECT 1 FROM callback_task WHERE order_id = ? AND done_at IS NULL AND cancelled_at IS NULL", (card,)).fetchone()
    clock["now"] = T2
    assert post(mine, "/my-point/%d/undo" % card, "tok").status_code == 302
    now = orders.get_card(conn, card)
    assert now["stage"]["code"] == orders.RETURN_GIVEN and linked(conn, card) is None
    back = order_oth.return_plan(conn, card)
    assert back and back["return_date"] == plan["return_date"] and not back["signed_at"]
    assert order_oth.question_open(conn, now)                                               # the question the sale closed is open again


def test_a_recorded_sale_is_not_undone_by_the_point_until_the_office_reverses_it(tmp_path, clock):
    app, owner, conn, pid, card = setup(tmp_path)
    rules.add_rule(conn, _stage(conn, "done"), "enter", 0, "record_sale", None, "", "auto", None)
    mine = manager(app, conn, pid)
    sell(mine, card)
    clock["now"] = T2
    assert post(mine, "/my-point/%d/undo" % card, "tok").status_code == 302                 # as for «Выдал»: money in «Продажи» is the office's
    assert orders.get_card(conn, card)["stage"]["code"] == "done" and len(sales(conn, card)) == 1
    orders.reverse_sales(conn, card, T2, "owner")
    assert post(mine, "/my-point/%d/undo" % card, "tok").status_code == 302
    assert orders.get_card(conn, card)["stage"]["code"] == "oth" and sales(conn, card) == []
