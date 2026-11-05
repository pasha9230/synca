"""«Новый вид» of the point (lane port-point, the prototype's role «Точка»): with the point group's flag on for the viewer the point's pages are
drawn from templates/point2/ (Рабочее место with its tabs, cards, questions, «Отменить», «Передать клиента», Звонки, Чаты, Задачи, «Что
нового»); off — the same pages as before. The presses keep their routes, tokens and results."""
from __future__ import annotations

import datetime as dt
import re

import pytest

from panel_helpers import login, post
from test_mypoint import manager, press, token, workplace
from test_vk_commands import _card, ready
from tires_sync import connections, order_intercity, order_oth, order_reserve, orders, store

NOW = "2026-10-13T12:00:00"
PAGES = ("/my-point", "/my-point/handed", "/calls", "/chats", "/tasks", "/whats-new")
NEW = 'class="p2w"'


def flag(conn, state):
    store.set_state(conn, "ui_new.point", state)
    conn.commit()


def is_new(html: str) -> bool:
    return "point2.css" in html and 'class="p2-nav"' in html and 'class="pt-nav"' not in html


def world(tmp_path):
    """Бета with one order of every kind its workplace shows: on the way, at the point asked «Клиент забрал?», at the point, to another
    city with a label, on «На ОТХ» asked on day 7, a return agreed (the act МХ-3)."""
    app, client, tok, conn, pid, moving = ready(tmp_path)
    asked = _card(conn, pid, webmim="700201", code="at_point")
    orders.update_card(conn, asked, dict(reserve_until="2026-10-13"), NOW, "o")
    entered = re.sub(r"\D", "", orders.get_card(conn, asked)["entered_at"])
    store.set_state(conn, order_reserve.ASK_KEY % (asked, entered), "asked|2026-10-13T11:30:00")
    here = _card(conn, pid, webmim="700202", code="at_point")
    far = _card(conn, pid, webmim="700203", code="at_point")
    order_intercity.save(conn, far, {"enabled": "1", "city": "Казань", "carrier": "ПЭК", "track": ""}, NOW, "o")
    conn.commit()
    order_intercity.add_label(conn, far, "labels.pdf", b"%PDF-1.4", NOW, "o")
    oth = _card(conn, pid, webmim="700204", code="at_point")
    orders.move_card(conn, oth, next(s["id"] for s in orders.list_stages(conn) if s["code"] == "oth"), NOW, "o")
    store.set_state(conn, order_oth.ASK_KEY % (oth, re.sub(r"\D", "", orders.get_card(conn, oth)["entered_at"])), "asked|2026-10-13T11:30:00")
    ret = _card(conn, pid, webmim="700205", code="at_point")
    orders.move_card(conn, ret, next(s["id"] for s in orders.list_stages(conn) if s["code"] == "oth"), NOW, "o")
    order_oth.agree(conn, orders.get_card(conn, ret), dt.date(2026, 10, 13), NOW, "owner")
    conn.commit()
    ids = dict(moving=moving, asked=asked, here=here, far=far, oth=oth, ret=ret)
    return app, client, conn, pid, ids


@pytest.fixture
def w(tmp_path):
    app, owner, conn, pid, ids = world(tmp_path)
    return dict(app=app, owner=owner, conn=conn, pid=pid, ids=ids, point=manager(app, conn, pid))


def pages(client):
    return {page: client.get(page) for page in PAGES}


# --- off: exactly the old pages ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("state", ["", "off", "owner_only"])
def test_a_point_manager_keeps_the_old_pages_unless_it_is_on_for_everybody(w, state):
    if state:
        flag(w["conn"], state)
    for page, r in pages(w["point"]).items():
        html = r.get_data(as_text=True)
        assert r.status_code == 200, page
        assert NEW not in html and "point2.css" not in html and "p2-nav" not in html, page
    assert 'class="pt mp-wrap"' in w["point"].get("/my-point").get_data(as_text=True)          # the old workplace itself


def test_off_written_is_the_same_markup_as_nothing_written(w):
    def snap():
        return {p: re.sub(r'value="[0-9a-f]{24}"', "", w["point"].get(p).get_data(as_text=True)) for p in PAGES}
    snap()                                                               # the first look marks things seen (the tour, the alerts)
    before = snap()
    flag(w["conn"], "off")
    assert snap() == before


def test_the_old_pages_keep_the_old_sections_at_the_bottom(w):
    for page in ("/chats", "/tasks", "/whats-new", "/my-point", "/my-point/handed"):
        html = w["point"].get(page).get_data(as_text=True)
        assert 'class="pt-nav"' in html and "p2-nav" not in html, page


# --- on: the new screens ------------------------------------------------------------------------------------------------------------

def test_on_for_everybody_every_screen_of_the_point_is_the_new_one(w):
    flag(w["conn"], "all")
    for page, r in pages(w["point"]).items():
        assert r.status_code == 200, page
        assert is_new(r.get_data(as_text=True)), page
    for page in ("/chats", "/tasks", "/whats-new"):                       # the old page under the new sections: one nav, the new one
        html = w["point"].get(page).get_data(as_text=True)
        assert html.count("data-point-nav") == 1 and 'class="pt-nav"' not in html, page


def test_the_office_never_gets_the_point_screens(w):
    flag(w["conn"], "all")
    store.set_state(w["conn"], "ui_new.office", "all")                  # the office's own «Новый вид» on too: still not the point's screens
    w["conn"].commit()
    for page in ("/my-point", "/calls", "/chats", "/tasks", "/whats-new"):
        assert NEW not in w["owner"].get(page).get_data(as_text=True), page


def test_the_workplace_cards_tabs_and_questions(w):
    flag(w["conn"], "all")
    ids = w["ids"]
    here = w["point"].get("/my-point").get_data(as_text=True)
    assert 'data-tab-count="here">3<' in here and 'data-tab-count="moving">1<' in here and 'data-tab-count="oth">1<' in here
    assert "mp-tab--hot" in here
    # «Клиент забрал?»: its answers in the question, «Выдал» and «ОТКАЗ» hidden while it hangs (the owner 2026-11-02, 2026-11-05)
    assert 'data-question="%d"' % ids["asked"] in here
    assert "/my-point/%d/reserve_issued" % ids["asked"] in here and "/my-point/%d/reserve_here" % ids["asked"] in here
    assert "/my-point/%d/issued\"" % ids["asked"] not in here and "/my-point/%d/refuse" % ids["asked"] not in here
    assert here.index('data-order="%d"' % ids["asked"]) < here.index('data-order="%d"' % ids["here"])        # the question first
    assert "/my-point/%d/issued\"" % ids["here"] in here and "/my-point/%d/refuse" % ids["here"] in here
    assert 'data-confirm-title="Отказ по заказу № 700202?"' in here
    # «В другой город»: «Передал курьеру ТК» and the labels, no «Выдал»
    assert "/my-point/%d/shipped" % ids["far"] in here and "/my-point/%d/issued\"" % ids["far"] not in here
    assert 'href="/orders/%d/labels/' % ids["far"] in here and "data-intercity-tag" in here
    # the act МХ-3 over every tab
    assert 'data-return="%d"' % ids["ret"] in here and "/my-point/%d/act_signed" % ids["ret"] in here
    oth = w["point"].get("/my-point?tab=oth").get_data(as_text=True)                  # an «asked» left on «На ОТХ» by the old rule still works
    assert 'data-oth-question="%d"' % ids["oth"] in oth and "/my-point/%d/oth_issued" % ids["oth"] in oth
    assert 'data-return="%d"' % ids["ret"] in oth
    moving = w["point"].get("/my-point?tab=moving").get_data(as_text=True)
    assert "/my-point/%d/arrived" % ids["moving"] in moving


def test_the_tour_of_the_point_finds_its_places_on_the_new_workplace(w):
    flag(w["conn"], "all")
    html = w["point"].get("/my-point").get_data(as_text=True)
    for mark in ('class="mp-tabs"', 'class="mp-search', 'data-tour="handoff"', 'class="mp-block"', 'class="mp-card p2-card', 'class="mp-returns"'):
        assert mark in html, mark
    assert re.search(r'<section class="mp-block"[^>]*><h2[^>]*>.*?</h2>\s*<article class="mp-card', html, re.S)   # .mp-block > .mp-card:first-of-type


# --- on: the presses still do what they did -------------------------------------------------------------------------------------------

def test_the_presses_of_the_new_workplace_move_the_orders_as_before(w):
    flag(w["conn"], "all")
    conn, ids, pt = w["conn"], w["ids"], w["point"]
    _tok, page = press(pt, ids["moving"], "arrived")
    assert orders.get_card(conn, ids["moving"])["stage"]["code"] == "at_point" and is_new(page)
    press(pt, ids["here"], "issued")
    assert orders.get_card(conn, ids["here"])["stage"]["code"] == "done"
    press(pt, ids["asked"], "reserve_here")
    assert orders.get_card(conn, ids["asked"])["stage"]["code"] == "at_point"
    press(pt, ids["oth"], "oth_here")
    assert not order_oth.question_open(conn, orders.get_card(conn, ids["oth"]))         # the owner 2026-11-04: «На точке» only marks the answer
    press(pt, ids["ret"], "act_signed")
    assert orders.get_card(conn, ids["ret"])["stage"]["code"] == "return"           # 2026-11-03: the act closes it with the purchase


def test_refuse_from_the_new_workplace_goes_to_oth(w):
    """«ОТКАЗ» with its reason (the owner 2026-11-05; it was «Клиент не пришёл», then the question «на ОТХ?»)."""
    flag(w["conn"], "all")
    conn, ids, pt = w["conn"], w["ids"], w["point"]
    press(pt, ids["moving"], "arrived")
    _tok, page = press(pt, ids["moving"], "refuse")
    assert is_new(page) and "data-oth-offer" not in page
    assert orders.get_card(conn, ids["moving"])["stage"]["code"] == "oth"


def test_undo_from_the_new_workplace(w):
    flag(w["conn"], "all")
    conn, ids, pt = w["conn"], w["ids"], w["point"]
    _tok, page = press(pt, ids["moving"], "arrived")
    assert 'data-undo="%d"' % ids["moving"] in page and 'href="#p2-undo"' in page
    r = post(pt, "/my-point/%d/undo" % ids["moving"], "tok")
    assert r.status_code == 302
    assert orders.get_card(conn, ids["moving"])["stage"]["code"] == "moving"


def test_hand_over_and_cancel_from_the_new_page(w):
    flag(w["conn"], "all")
    pt = w["point"]
    page = pt.get("/my-point/handed").get_data(as_text=True)
    assert is_new(page) and 'id="handoff"' in page
    assert page.index('id="mp-handed-h"') < page.index('class="mp-handoff-form"')         # the list first, the form under it (the owner 2026-10-30)
    tok = re.search(r'name="token" value="([0-9a-f]+)"', page).group(1)
    r = post(pt, "/my-point/handed", "tok", token=tok, phone="+7 900 555 11 22", need="4 шт")
    assert r.status_code == 302
    page = pt.get(r.headers["Location"]).get_data(as_text=True)
    assert is_new(page) and "+7 900 555-11-22" in page
    cancel = re.search(r'action="/my-point/handoff/(\d+)/cancel"', page)
    assert cancel
    assert post(pt, "/my-point/handoff/%s/cancel" % cancel.group(1), "tok").status_code == 302
    assert orders.get_card(w["conn"], int(cancel.group(1)))["stage"]["code"] == "cancel"


def test_a_wrong_phone_comes_back_on_the_new_page(w):
    flag(w["conn"], "all")
    pt = w["point"]
    tok = re.search(r'name="token" value="([0-9a-f]+)"', pt.get("/my-point/handed").get_data(as_text=True)).group(1)
    r = post(pt, "/my-point/handed", "tok", token=tok, phone="12", need="x")
    html = r.get_data(as_text=True)
    assert r.status_code == 400 and is_new(html) and 'id="mp-handoff-err"' in html


def test_the_calls_of_the_point_with_the_main_button_to_hand_over(w):
    conn = w["conn"]
    acc = connections.create_account(conn, "Аккаунт точки", NOW, "o")
    conn.execute("UPDATE point SET avito_account_id = ? WHERE id = ?", (acc, w["pid"]))
    conn.execute("INSERT INTO avito_call (account_id, call_id, call_at, buyer_phone, answered, talk_seconds) VALUES (?,?,?,?,0,0)",
                 (acc, "c1", "2026-10-13T11:00:00", "79005551122"))
    conn.commit()
    old = w["point"].get("/calls").get_data(as_text=True)
    flag(conn, "all")
    new = w["point"].get("/calls").get_data(as_text=True)
    assert is_new(new) and not is_new(old)
    for html in (old, new):                                              # the same window and the same address of the handover
        assert 'data-handoff-action="/calls/' in html and 'id="handoff"' in html and "handoff.js" in html
    assert re.search(r'class="btn btn--primary btn--sm" data-open-modal="handoff"', new)
    assert "Аккаунт точки" not in new                                    # a point never sees an account's name


# --- «только у владельца»: the owner working as the point ------------------------------------------------------------------------------

def test_owner_only_the_owner_working_as_the_point_sees_the_new_screens(w):
    flag(w["conn"], "owner_only")
    owner = w["owner"]
    assert NEW not in owner.get("/my-point?p=%d" % w["pid"]).get_data(as_text=True)    # the owner's own office view: not the point's
    assert post(owner, "/as-point", "tok", point_id=str(w["pid"])).status_code == 302
    for page in PAGES:
        r = owner.get(page)
        assert r.status_code == 200 and is_new(r.get_data(as_text=True)), page
    assert NEW not in w["point"].get("/my-point").get_data(as_text=True)                # the point's manager: still the old
    _tok, page = press(owner, w["ids"]["moving"], "arrived")
    assert orders.get_card(w["conn"], w["ids"]["moving"])["stage"]["code"] == "at_point"
    assert post(owner, "/as-point/stop", "tok").status_code == 302
    assert NEW not in owner.get("/my-point?p=%d" % w["pid"]).get_data(as_text=True)


def test_off_the_owner_working_as_the_point_sees_the_old_screens(w):
    owner = w["owner"]
    post(owner, "/as-point", "tok", point_id=str(w["pid"]))
    html = owner.get("/my-point").get_data(as_text=True)
    assert NEW not in html and 'class="pt mp-wrap"' in html


# --- after the review ---------------------------------------------------------------------------------------------------------------

def test_the_hint_of_an_oth_card_is_there_as_before(w):
    old = w["point"].get("/my-point?tab=oth").get_data(as_text=True)
    flag(w["conn"], "all")
    new = w["point"].get("/my-point?tab=oth").get_data(as_text=True)
    assert 'data-point-hint="oth"' in old and 'data-point-hint="oth"' in new


def test_the_more_menu_of_the_phone_keeps_the_alerts_page(w):
    flag(w["conn"], "all")
    html = w["point"].get("/my-point").get_data(as_text=True)
    more = html[html.index('class="p2-more-list"'):].split("</details>")[0]
    assert 'href="/alerts"' in more and 'href="/my-point?notes=1"' in more and 'action="/logout"' in more
    assert 'href="/orders"' not in more


def test_the_old_pages_under_the_new_nav_keep_their_own_frame(w):
    flag(w["conn"], "all")
    for page in ("/chats", "/tasks", "/whats-new"):
        html = w["point"].get(page).get_data(as_text=True)
        assert NEW not in html and 'class="p2-nav"' in html, page               # no second frame (its padding) around the nav
