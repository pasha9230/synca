"""The owner 2026-11-09 (the point: «не видно отработанных заказов, нет возможности откатить статус»): the tab «Выдано» of «Рабочее место»
(the point's own orders closed by a hand-out in the last 30 days, newest first, the moment and who marked it; no money, nothing of WebMIM but
the number), the confirmation of a hand-out (the order's number, the tyres with the quantity, the client; only «Да, выдал» sends the press),
and «Отменить» of the last press in the order's card. Both views of the point; the owner picking a point and working as a point."""
from __future__ import annotations

import re

import pytest

from panel_helpers import post
from test_mypoint import manager, press, token
from test_point_new_view import flag
from test_vk_commands import SENT, _card, _stage, ready
from tires_sync import order_points as points, orders
from tires_sync.panel import routes_mypoint

T1, T2 = "2026-10-16T11:00:00", "2026-10-16T12:30:00"


@pytest.fixture
def clock(monkeypatch):
    moment = {"now": T1}
    monkeypatch.setattr(routes_mypoint, "local_iso", lambda: moment["now"])
    return moment


@pytest.fixture(params=["old", "new"])
def view(request):
    return request.param


def setup(tmp_path, view):
    app, owner, tok, conn, pid, card = ready(tmp_path, SENT)
    if view == "new":
        flag(conn, "all")
    orders.move_card(conn, card, _stage(conn, "at_point"), "2026-10-13T12:00:00", "o")
    conn.commit()
    return app, owner, conn, pid, card


def issued_tab(client, p=None) -> str:
    page = client.get("/my-point?tab=issued" + ("&p=%d" % p if p else "")).get_data(as_text=True)
    return page


def block(page: str) -> str:
    """The list of the tab (the section under the tabs)."""
    start = page.index('id="mp-orders-h"')
    return page[start:page.index("</section>", start)]


def cards(page: str) -> list:
    return [int(n) for n in re.findall(r'data-issued="(\d+)"', page)]


def count(page: str, tab: str) -> int:
    return int(re.search(r'data-tab-count="%s">(\d+)<' % tab, page).group(1))


# --- «Выдано» ----------------------------------------------------------------------------------------------------------------------

def test_issued_lists_the_points_hand_outs_newest_first_with_time_tyres_client_and_who(tmp_path, clock, view):
    app, owner, conn, pid, card = setup(tmp_path, view)
    second = _card(conn, pid, webmim="700301", code="at_point")
    office = _card(conn, pid, webmim="700302", code="at_point")
    old = _card(conn, pid, webmim="700303", code="at_point")
    oth = _card(conn, pid, webmim="700304", code="at_point")
    other_point = points.save_point(conn, {"name": "Гамма", "channel": "vk"})
    foreign = _card(conn, other_point, webmim="700305", code="at_point")
    done = _stage(conn, "done")
    orders.move_card(conn, office, done, "2026-10-15T09:00:00", "owner")                 # the office marked it from «Товар на точке»
    orders.move_card(conn, old, done, "2026-09-10T09:00:00", "owner")                   # more than 30 days ago
    orders.move_card(conn, oth, _stage(conn, "oth"), "2026-10-14T09:00:00", "o")
    orders.move_card(conn, oth, done, "2026-10-15T10:00:00", "owner")                   # not from «На точке»: not a hand-out of the point
    orders.move_card(conn, foreign, done, "2026-10-15T11:00:00", "owner")               # another point's
    conn.commit()
    mine = manager(app, conn, pid)
    press(mine, card, "issued")
    clock["now"] = T2
    press(mine, second, "issued")
    page = issued_tab(mine)
    assert cards(page) == [second, card, office]                                       # newest first
    assert count(page, "issued") == 3
    listed = block(page)
    assert "№ 700301" in listed and "№ 5000100" in listed and "№ 700302" in listed
    assert "выдан 16.10 12:30" in listed and "выдан 16.10 11:00" in listed and "выдан 15.10 09:00" in listed
    assert "Nokian Hakkapeliitta 10" in listed and ("× 4" in listed or "4 шт." in listed) and "Татьяна" in listed
    assert "Отметил: gamma" in listed                                                   # the point's own press
    first_office = listed[listed.index('data-issued="%d"' % office):]
    assert "Отметил:" in first_office and "Отметил: gamma" not in first_office
    for money in ("Итого", "₽", "5 000", "20 000", "4 000", "WebMIM", "Татьяна, ваш"):  # no sums, no WebMIM but the number, no VK text
        assert money not in listed, money
    assert str(foreign) not in re.findall(r'data-issued="(\d+)"', page)


def test_the_owner_sees_the_chosen_points_issued_and_the_manager_only_the_own(tmp_path, clock):
    app, owner, conn, pid, card = setup(tmp_path, "old")
    other_point = points.save_point(conn, {"name": "Гамма", "channel": "vk"})
    foreign = _card(conn, other_point, webmim="700305", code="at_point")
    orders.move_card(conn, foreign, _stage(conn, "done"), "2026-10-15T11:00:00", "owner")
    mine = manager(app, conn, pid)
    press(mine, card, "issued")
    assert cards(issued_tab(owner, pid)) == [card]
    assert cards(issued_tab(owner, other_point)) == [foreign]
    assert cards(issued_tab(mine, other_point)) == [card]                              # ?p= of a manager: their own point, always
    r = post(mine, "/my-point/%d/undo" % foreign, "tok")
    assert r.status_code in (403, 404) and orders.get_card(conn, foreign)["stage"]["code"] == "done"     # another point's order


def test_the_owner_working_as_the_point_sees_its_issued(tmp_path, clock, view):
    app, owner, conn, pid, card = setup(tmp_path, view)
    mine = manager(app, conn, pid)
    press(mine, card, "issued")
    assert post(owner, "/as-point", "tok", point_id=str(pid)).status_code == 302
    page = issued_tab(owner)
    assert cards(page) == [card] and count(page, "issued") == 1
    assert ('action="/my-point/%d/undo"' % card) in block(page)                          # the press is of this point: it can be undone


# --- the confirmation of a hand-out ----------------------------------------------------------------------------------------------

def test_the_press_form_carries_the_lines_and_an_empty_confirm(tmp_path, clock, view):
    app, owner, conn, pid, card = setup(tmp_path, view)
    page = manager(app, conn, pid).get("/my-point").get_data(as_text=True)
    form = re.search(r'<form method="post" action="/my-point/%d/issued"[^>]*>.*?</form>' % card, page, re.S).group(0)
    assert 'data-confirm-ok="Да, выдал"' in form and "data-confirm-lines" in form
    assert "Номер заказа MIM: 5000100\nNokian Hakkapeliitta 10 × 4\nКлиент: Татьяна" in form
    assert '<input type="hidden" name="confirm" value="" data-confirm-field>' in form


def test_without_yes_nothing_changes_and_the_server_asks(tmp_path, clock, view):
    app, owner, conn, pid, card = setup(tmp_path, view)
    mine = manager(app, conn, pid)
    tok = token(mine.get("/my-point").get_data(as_text=True), card, "issued")
    r = post(mine, "/my-point/%d/issued" % card, "tok", token=tok)
    page = r.get_data(as_text=True)
    assert r.status_code == 200 and "data-issue-confirm" in page
    assert "Номер заказа MIM: 5000100" in page and "Nokian Hakkapeliitta 10 × 4" in page and "Клиент: Татьяна" in page
    assert "Да, выдал" in page and 'name="confirm" value="yes"' in page and ('value="%s"' % tok) in page and "Отмена" in page
    assert orders.get_card(conn, card)["stage"]["code"] == "at_point"
    assert conn.execute("SELECT 1 FROM order_command_log WHERE message_id = ?", ("page-" + tok,)).fetchone() is None
    r = post(mine, "/my-point/%d/issued" % card, "tok", token=tok, confirm="yes")       # «Да, выдал»: the press as before
    assert r.status_code == 302 and orders.get_card(conn, card)["stage"]["code"] == "done"
    assert "отмечено: выдан" in mine.get(r.headers["Location"]).get_data(as_text=True)


def test_the_confirm_page_keeps_the_point_and_the_tab_of_the_owner(tmp_path, clock):
    app, owner, conn, pid, card = setup(tmp_path, "old")
    tok = token(owner.get("/my-point?p=%d" % pid).get_data(as_text=True), card, "issued")
    r = owner.post("/my-point/%d/issued" % card, data={"csrf": "tok", "token": tok, "p": str(pid)},
                   headers={"Referer": "http://localhost/my-point?p=%d&tab=here" % pid})
    page = r.get_data(as_text=True)
    assert r.status_code == 200 and ('name="p" value="%d"' % pid) in page
    r = owner.post("/my-point/%d/issued" % card, data={"csrf": "tok", "token": tok, "p": str(pid), "confirm": "yes", "tab": "issued"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/my-point?p=%d&tab=issued" % pid)
    assert orders.get_card(conn, card)["stage"]["code"] == "done"


def test_a_crafted_confirm_of_an_own_sale_or_a_bad_token_still_changes_nothing(tmp_path, clock):
    app, owner, conn, pid, card = setup(tmp_path, "old")
    mine = manager(app, conn, pid)
    r = post(mine, "/my-point/%d/issued" % card, "tok", token="not-a-token")
    assert r.status_code == 302 and orders.get_card(conn, card)["stage"]["code"] == "at_point"


# --- «Отменить» in the card ------------------------------------------------------------------------------------------------------

def test_undo_in_the_card_only_while_undoable_and_it_works(tmp_path, clock, view):
    app, owner, conn, pid, card = setup(tmp_path, view)
    mine = manager(app, conn, pid)
    here = mine.get("/my-point").get_data(as_text=True)
    assert 'data-undo-card="%d"' % card not in here                                     # nothing pressed yet
    press(mine, card, "issued")
    page = issued_tab(mine)
    assert 'data-undo-card="%d"' % card in block(page) and "Отменить «Выдал»" in block(page)
    assert 'data-undo-list' in page and "<details class=\"p2-box mp-undo\"" not in page and "<details class=\"mp-handoff mp-undo\"" not in page
    clock["now"] = T2
    r = post(mine, "/my-point/%d/undo" % card, "tok")
    assert r.status_code == 302 and orders.get_card(conn, card)["stage"]["code"] == "at_point"
    assert cards(issued_tab(mine)) == []
    assert 'data-undo-card="%d"' % card not in mine.get("/my-point").get_data(as_text=True)    # undone: nothing more to undo


def test_no_undo_in_the_card_after_a_later_move(tmp_path, clock, view):
    app, owner, conn, pid, card = setup(tmp_path, view)
    second = _card(conn, pid, webmim="700301", code="moving")
    mine = manager(app, conn, pid)
    press(mine, second, "arrived")
    page = mine.get("/my-point").get_data(as_text=True)
    assert 'data-undo-card="%d"' % second in page and "Отменить «Приехал»" in page
    orders.move_card(conn, second, _stage(conn, "moving"), "2026-10-16T12:00:00", "owner")      # the office moved it since
    conn.commit()
    page = mine.get("/my-point?tab=moving").get_data(as_text=True)
    assert 'id="order-%d"' % second in page and 'data-undo-card="%d"' % second not in page


def test_the_answers_vydan_and_vydali_ask_too(tmp_path, clock):
    from test_point_new_view import world
    app, owner, conn, pid, ids = world(tmp_path)
    clock["now"] = "2026-10-13T12:00:00"                                                   # the day the questions are asked
    mine = manager(app, conn, pid)
    page = "".join(mine.get("/my-point?tab=" + t).get_data(as_text=True) for t in ("here", "oth"))
    for action, card in (("reserve_issued", ids["asked"]), ("oth_issued", ids["oth"])):
        before = orders.get_card(conn, card)["stage"]["code"]
        r = post(mine, "/my-point/%d/%s" % (card, action), "tok", token=token(page, card, action))
        assert r.status_code == 200 and "data-issue-confirm" in r.get_data(as_text=True), action
        assert orders.get_card(conn, card)["stage"]["code"] == before, action
        r = post(mine, "/my-point/%d/%s" % (card, action), "tok", token=token(page, card, action), confirm="yes")
        assert r.status_code == 302 and orders.get_card(conn, card)["stage"]["code"] == "done", action
