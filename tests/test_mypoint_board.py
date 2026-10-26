"""«Моя точка», the owner's answers of 2026-10-26: the counters at the top, one list newest first by the order's last event, the card says
what the VK message «Менеджеру: заказ оформлен» says (with the client's prices: the point takes the cash), «Возврат» on the orders at the
point does what «возврат отдал» does, and the question of the last day of the reservation («Клиент забрал?») is answered on the page with
the same effect as in VK and never twice."""
from __future__ import annotations

import re

import pytest

from panel_helpers import post
from test_mypoint import journal, manager, press, token, two
from test_vk_commands import PEER, SENT, _card, _stage, hook, ready, to_client
from tires_sync import order_manager as mgr, order_points as points, order_reserve as reserve, order_templates as tpl, orders, store
from tires_sync.panel import routes_mypoint

DAY = "2026-10-16"                                     # the last day of the reservation of the order at the point
MORNING = DAY + "T11:00:00"


@pytest.fixture
def clock(monkeypatch):
    """The panel's time is the test's (the last day of the reservation, in the working hours) unless a test moves it."""
    moment = {"now": MORNING}
    monkeypatch.setattr(routes_mypoint, "local_iso", lambda: moment["now"])
    return moment


def at_point(conn, card, until=DAY):
    orders.move_card(conn, card, _stage(conn, "at_point"), "2026-10-13T12:00:00", "o")
    orders.update_card(conn, card, dict(reserve_until=until), "2026-10-13T12:00:00", "o")
    conn.commit()


def asked(conn, card, when=MORNING):
    """The tick of the last day asked the point in VK (order_reserve.ask_point, the VK send itself faked)."""
    assert reserve.ask_point(conn, orders.get_card(conn, card), lambda *a, **k: None, when) == "asked"


def counter(page, name):
    found = re.search(r'data-counter="%s"[^>]*>.*?<b[^>]*>(\d+)</b>' % name, page, re.S)
    assert found, name
    return int(found.group(1))


# --- counters, order, the card's text ------------------------------------------------------------------------------------------------

def test_the_counters_say_what_lies_comes_ends_and_waits(tmp_path, clock):
    app, client, tok, conn, pid, card = ready(tmp_path)                                     # 5000100: on its way
    placed = _card(conn, pid, webmim="700300", code="placed")
    ends = _card(conn, pid, webmim="700400", code="at_point")
    at_point(conn, ends)
    late = _card(conn, pid, webmim="700500", code="at_point")
    at_point(conn, late, until="2026-10-15")
    later = _card(conn, pid, webmim="700600", code="at_point")
    at_point(conn, later, until="2026-10-20")
    asked(conn, ends)
    other = points.save_point(conn, dict(name="Гамма", channel="vk", vk_peer_id="777"))
    _card(conn, other, webmim="700700")                                                      # another point's: counted nowhere
    page = manager(app, conn, pid).get("/my-point").get_data(as_text=True)
    assert (counter(page, "coming"), counter(page, "here"), counter(page, "ending"), counter(page, "asked")) == (2, 3, 2, 1)
    assert placed


def test_one_list_newest_first_by_the_last_event(tmp_path, clock):
    app, client, tok, conn, pid, card = ready(tmp_path)                                     # 5000100: on its way
    lying = _card(conn, pid, webmim="700200", code="at_point")
    orders.add_event(conn, card, "2026-10-14T09:00:00", "o", "note", "что-то случилось позже")
    conn.commit()
    mine = manager(app, conn, pid)
    page = mine.get("/my-point").get_data(as_text=True)
    assert page.index('id="order-%d"' % card) < page.index('id="order-%d"' % lying)       # its last event is the newest
    orders.add_event(conn, lying, "2026-10-15T09:00:00", "o", "point", "приехал на точку")
    conn.commit()
    page = mine.get("/my-point").get_data(as_text=True)
    assert page.index('id="order-%d"' % lying) < page.index('id="order-%d"' % card)


def test_the_card_says_what_the_vk_message_says_with_the_clients_prices(tmp_path, clock):
    app, client, tok, conn, pid, card = ready(tmp_path)
    mine = manager(app, conn, pid)
    page = mine.get("/my-point").get_data(as_text=True)
    text = tpl.render(conn, orders.get_card(conn, card), tpl.SAMPLES[1][2], MORNING)[0]
    assert "Цена ед. 5 000 ₽" in text and "Сумма 20 000 ₽" in text
    shown = re.search(r'<div class="mp-text"[^>]*>(.*?)</div>', page, re.S).group(1)
    assert shown.strip() == text.replace("+", "&#43;").strip() or shown.strip() == text.strip()
    assert "4 000" not in page and "закуп" not in page.lower()                                 # the purchase price is never on the page
    template = conn.execute("SELECT id FROM order_template WHERE name = 'Менеджеру: заказ оформлен' AND audience = 'manager'").fetchone()
    if template is None:
        tpl.add_template(conn, "Менеджеру: заказ оформлен", "manager", "Особый текст {webmim_number} для {client_name}")
    else:
        tpl.update_template(conn, template[0], "Менеджеру: заказ оформлен", "manager", "Особый текст {webmim_number} для {client_name}")
    page = mine.get("/my-point").get_data(as_text=True)
    assert "Особый текст 5000100 для Татьяна" in page                                      # the owner's edit of the template is on the page
    assert 'href="tel:+79001112233"' in page and "едет на точку" in page


# --- «Возврат» -------------------------------------------------------------------------------------------------------------------------

def test_return_only_on_orders_at_the_point_and_does_what_the_vk_command_does(tmp_path, clock):
    (vk_app, _c, _t, vk_conn, _p, vk_card), (app, client, tok, conn, pid, card) = two(tmp_path)
    at_point(vk_conn, vk_card)
    hook(vk_app, vk_conn, "возврат отдал 5000100")
    coming = _card(conn, pid, webmim="700200")
    at_point(conn, card)
    mine = manager(app, conn, pid)
    page = mine.get("/my-point").get_data(as_text=True)
    assert '/my-point/%d/returned' % card in page and '/my-point/%d/returned' % coming not in page
    assert 'data-confirm-title="Возврат поставщику' in page
    first, page = press(mine, card, "returned")
    assert [(k, t, l) for k, t, l in journal(conn, card)][-3:] == [(k, t, l) for k, t, l in journal(vk_conn, vk_card)][-3:]
    got, vk_got = orders.get_card(conn, card), orders.get_card(vk_conn, vk_card)
    assert (got["stage"]["code"], got["return_kind"], got["return_reason"]) == (vk_got["stage"]["code"], vk_got["return_kind"], vk_got["return_reason"])
    assert "отмечено — шины отданы на возврат" in page and to_client(app) == []
    _t, page = press(mine, card, "returned", again=first)                                  # a double tap
    assert "уже выполнена" in page
    r = post(mine, "/my-point/%d/returned" % card, "tok", token="b" * 24)                   # a new press: the VK command's own «уже отмечено»
    assert "уже отмечен" in mine.get(r.headers["Location"]).get_data(as_text=True)
    assert len([t for _k, t, _l in journal(conn, card) if t == mgr.RETURN_LINE]) == 1


def test_return_on_an_order_on_its_way_changes_nothing(tmp_path, clock):
    app, client, tok, conn, pid, card = ready(tmp_path)
    mine = manager(app, conn, pid)
    r = post(mine, "/my-point/%d/returned" % card, "tok", token="c" * 24)
    assert "«возврат» отмечают, когда заказ на точке" in mine.get(r.headers["Location"]).get_data(as_text=True)
    assert conn.execute("SELECT outcome FROM order_command_log WHERE message_id = ?", ("page-" + "c" * 24,)).fetchone()[0].startswith("не подходит")
    got = orders.get_card(conn, card)
    assert got["stage"]["code"] == "moving" and not got["return_kind"]
    assert mgr.RETURN_LINE not in [t for _k, t, _l in journal(conn, card)]


def test_return_and_answers_on_another_points_order_are_refused(tmp_path, clock):
    app, client, tok, conn, pid, card = ready(tmp_path)
    other = points.save_point(conn, dict(name="Гамма", channel="vk", vk_peer_id="777"))
    theirs = _card(conn, other, webmim="700100", code="at_point")
    mine = manager(app, conn, pid)
    for action in ("returned", "reserve_issued", "reserve_here"):
        assert post(mine, "/my-point/%d/%s" % (theirs, action), "tok", token="d" * 24, p=str(other)).status_code in (403, 404), action
    assert orders.get_card(conn, theirs)["stage"]["code"] == "at_point"


# --- «Клиент забрал?» --------------------------------------------------------------------------------------------------------------------

def question(page):
    found = re.search(r'<section class="mp-questions".*?</section>', page, re.S)
    return found.group(0) if found else ""


def test_no_question_before_the_bot_asked(tmp_path, clock):
    app, client, tok, conn, pid, card = ready(tmp_path)
    at_point(conn, card)
    page = manager(app, conn, pid).get("/my-point").get_data(as_text=True)
    assert question(page) == "" and counter(page, "asked") == 0


def test_the_question_is_on_top_and_here_tells_the_client_once_as_in_vk(tmp_path, clock):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT, SENT)
    at_point(conn, card)
    asked(conn, card)
    mine = manager(app, conn, pid)
    page = mine.get("/my-point").get_data(as_text=True)
    block = question(page)
    assert "Клиент забрал заказ № 5000100?" in block and "Татьяна" in block
    assert page.index('class="mp-questions"') < page.index('id="order-%d"' % card)
    assert '/my-point/%d/reserve_issued' % card in block and '/my-point/%d/reserve_here' % card in block
    clock["now"] = DAY + "T12:00:00"
    first, page = press(mine, card, "reserve_here")
    assert "отмечено: на точке" in page and "напоминание" in page
    [text] = to_client(app)
    assert text.startswith("Добрый день. Срок хранения вашего заказа №5000100 истекает 16.10.2026")
    assert "Точка ответила: заказ ещё на точке" in [t for _k, t, _l in journal(conn, card)]
    assert question(page) == "" and orders.get_card(conn, card)["stage"]["code"] == "at_point"
    _t, page = press(mine, card, "reserve_here", again=first)                               # a double tap
    r = post(mine, "/my-point/%d/reserve_here" % card, "tok", token="e" * 24)               # a stale page
    assert "уже отмечен" in mine.get(r.headers["Location"]).get_data(as_text=True)
    assert mgr.dispatch(conn, "vk-late", PEER, "на точке 5000100", None, DAY + "T12:30:00") == "Заказ №5000100 уже отмечен: на точке."
    assert len(to_client(app)) == 1


def test_an_answer_in_vk_first_closes_the_question_on_the_page(tmp_path, clock):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT, SENT)
    at_point(conn, card)
    asked(conn, card)
    mine = manager(app, conn, pid)
    stale = token(mine.get("/my-point").get_data(as_text=True), card, "reserve_here")
    reserve.here(conn, orders.get_card(conn, card), "менеджер Бета", DAY + "T11:30:00", lambda *a, **k: ("sent", "", ""))
    page = mine.get("/my-point").get_data(as_text=True)
    assert question(page) == "" and counter(page, "asked") == 0
    r = post(mine, "/my-point/%d/reserve_here" % card, "tok", token=stale)
    assert "уже отмечен" in mine.get(r.headers["Location"]).get_data(as_text=True) and to_client(app) == []


def test_issued_from_the_question_is_the_vk_issued(tmp_path, clock):
    (vk_app, _c, _t, vk_conn, _p, vk_card), (app, client, tok, conn, pid, card) = two(tmp_path)
    at_point(vk_conn, vk_card)
    hook(vk_app, vk_conn, "выдал 5000100")
    at_point(conn, card)
    asked(conn, card)
    mine = manager(app, conn, pid)
    _t, page = press(mine, card, "reserve_issued")
    assert orders.get_card(conn, card)["stage"]["code"] == "done" and to_client(app) == []
    assert [(k, t) for k, t, _l in journal(conn, card) if k != "reserve"][-2:] == [(k, t) for k, t, _l in journal(vk_conn, vk_card)][-2:]
    assert "отмечено: выдан" in page and question(page) == ""


def test_the_question_leaves_at_the_end_of_the_working_day_and_a_late_here_tells_nobody(tmp_path, clock):
    app, client, tok, conn, pid, card = ready(tmp_path, SENT)
    at_point(conn, card)
    asked(conn, card)
    mine = manager(app, conn, pid)
    stale = token(mine.get("/my-point").get_data(as_text=True), card, "reserve_here")
    clock["now"] = DAY + "T20:30:00"
    page = mine.get("/my-point").get_data(as_text=True)
    assert question(page) == ""
    r = post(mine, "/my-point/%d/reserve_here" % card, "tok", token=stale)
    assert "клиенту не написали" in mine.get(r.headers["Location"]).get_data(as_text=True) and to_client(app) == []


def test_the_feed_has_one_key_per_open_question(tmp_path, clock):
    app, client, tok, conn, pid, card = ready(tmp_path)
    at_point(conn, card)
    mine = manager(app, conn, pid)
    assert not [i for i in mine.get("/my-point/feed").get_json()["items"] if i["key"].startswith("question-")]
    asked(conn, card)
    items = [i for i in mine.get("/my-point/feed").get_json()["items"] if i["key"].startswith("question-")]
    assert [i["key"] for i in items] == ["question-%d" % card] and "5000100" in items[0]["title"]
    assert store.get_state(conn, reserve._ask_key(orders.get_card(conn, card))).startswith("asked|")       # the feed reads, never moves


def test_a_point_in_the_panel_is_asked_on_its_page_and_its_here_tells_the_client(tmp_path, clock):
    """The lead 2026-10-26: a point on «Панель» gets the question on «Моя точка» (no VK, no yellow «точку не спросить»); the rest as in VK."""
    app, client, tok, conn, pid, card = ready(tmp_path, SENT, SENT)
    conn.execute("UPDATE point SET channel = ? WHERE id = ?", (points.PANEL, pid))
    conn.commit()
    at_point(conn, card)
    sent = []
    reserve.check(conn, MORNING, lambda c, t: "ok", lambda *a, **k: None, lambda *a, **k: sent.append(a))
    assert sent == []                                                                       # nothing goes to VK
    events = orders.list_events(conn, card)
    assert any(e["text"].startswith("Точке «Бета» задан вопрос в «Моей точке»") and e["level"] == "grey" for e in events)
    assert not [e for e in events if e["level"] == "yellow" and "точку не спросить" in e["text"]]
    mine = manager(app, conn, pid)
    assert "Клиент забрал заказ № 5000100?" in question(mine.get("/my-point").get_data(as_text=True))
    _t, page = press(mine, card, "reserve_here")
    assert "напоминание" in page and len(to_client(app)) == 1
    reserve.check(conn, DAY + "T21:00:00", lambda c, t: "ok", lambda *a, **k: None, None)   # the evening tick: answered, no silence record
    assert not [e for e in orders.list_events(conn, card) if "не ответила" in e["text"]]


def test_a_silent_point_in_the_panel_is_a_yellow_record_as_in_vk(tmp_path, clock):
    app, client, tok, conn, pid, card = ready(tmp_path)
    conn.execute("UPDATE point SET channel = ? WHERE id = ?", (points.PANEL, pid))
    conn.commit()
    at_point(conn, card)
    reserve.check(conn, MORNING, lambda c, t: "ok", lambda *a, **k: None, None)
    reserve.check(conn, DAY + "T21:00:00", lambda c, t: "ok", lambda *a, **k: None, None)
    assert [e["text"] for e in orders.list_events(conn, card) if "не ответила до конца рабочего дня" in e["text"]]
    clock["now"] = DAY + "T21:05:00"
    assert question(manager(app, conn, pid).get("/my-point").get_data(as_text=True)) == ""
