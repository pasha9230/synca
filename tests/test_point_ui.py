"""«Рабочее место», the new design (the owner 2026-10-30): the tabs «Едут», «На точке», «На ОТХ», «На возврат», «Переданные клиенты»
(an old address opens «На точке»); the question «Клиент забрал?» a yellow line of its card, first on «На точке»; the search over every order
of the point says the tab of each; the point's alerts are a line «Оповещения: N новых» (the bot's answers are not shown), seen only when
opened; the page is made for a phone (its own viewport), the rest of the panel keeps its own; «Передать клиента» of a call lands in the list."""
from __future__ import annotations

import re

import pytest

from panel_helpers import login, make_app
from test_mypoint import manager
from tires_sync import order_points as points, order_reserve, orders, store, users

NOW = "2026-10-30T12:00:00"


@pytest.fixture(autouse=True)
def cheap_hashes(monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")


@pytest.fixture
def w(tmp_path):
    app = make_app(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, NOW)
    alfa = points.save_point(conn, {"name": "Альфа", "manager_name": "А", "channel": "panel"})
    stages = {s["code"]: s["id"] for s in orders.list_stages(conn)}
    made = {}
    for i, code in enumerate(("moving", "at_point", "oth")):
        cid = orders.create_card(conn, {"client_name": "Клиент " + code, "client_phone": "+7900111000%d" % i, "point_id": str(alfa)}, NOW, "o")
        conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (stages[code], cid))
        made[code] = cid
    conn.commit()
    return dict(app=app, conn=conn, alfa=alfa, made=made, point=manager(app, conn, alfa))


def _page(client, url="/my-point"):
    return client.get(url).get_data(as_text=True)


def _active(page):
    return re.search(r'class="mp-tab is-active[^"]*"[^>]*data-tab="([a-z]+)"', page).group(1)


def _cards(page):
    return [int(x) for x in re.findall(r'<article class="mp-card[^"]*" id="order-(\d+)"', page)]


# --- вкладки -----------------------------------------------------------------------------------------------------------------------

def test_the_five_tabs_and_the_default_one(w):
    page = _page(w["point"])
    names = re.findall(r'data-tab="([a-z]+)"', page)
    assert names == ["moving", "here", "oth", "return"]                                      # «Переданные клиенты»: a section of its own
    assert ">Едут<" in page and ">На точке<" in page and ">На ОТХ<" in page and ">На возврат<" in page
    assert _active(page) == "here" and _cards(page) == [w["made"]["at_point"]]
    for old in ("all", "ending", "asked", "nonsense"):
        assert _active(_page(w["point"], "/my-point?tab=" + old)) == "here", old


def test_each_tab_holds_its_own_orders(w):
    assert _cards(_page(w["point"], "/my-point?tab=moving")) == [w["made"]["moving"]]
    oth = _page(w["point"], "/my-point?tab=oth")
    assert _cards(oth) == [w["made"]["oth"]] and "На ОТХ с" in oth and "срока продажи нет" in oth
    assert "/my-point/%d/" % w["made"]["oth"] not in oth                                          # no press on our tyres at the point
    assert _cards(_page(w["point"], "/my-point?tab=return")) == [] and "Возвратов поставщику сейчас нет" in _page(w["point"], "/my-point?tab=return")
    r = w["point"].get("/my-point?tab=handed")                                                # the old tab: its section
    assert r.status_code == 302 and r.headers["Location"].endswith("/my-point/handed")


def test_an_agreed_return_goes_from_oth_to_the_return_tab(w):
    from datetime import date
    from tires_sync import order_oth
    order_oth.agree(w["conn"], orders.get_card(w["conn"], w["made"]["oth"]), date(2026, 10, 30), NOW, "owner")
    assert _cards(_page(w["point"], "/my-point?tab=oth")) == []
    page = _page(w["point"], "/my-point?tab=return")
    assert 'data-return="%d"' % w["made"]["oth"] in page and "Подписал акт и отдал" in page
    assert 'data-return="%d"' % w["made"]["oth"] in _page(w["point"])                              # the plaque of the act over the other tabs


def test_the_question_of_the_last_day_is_a_line_of_its_card_and_first(w, monkeypatch):
    conn, stages = w["conn"], {s["code"]: s["id"] for s in orders.list_stages(w["conn"])}
    asked = orders.create_card(conn, {"client_name": "Последний день", "client_phone": "+79001119999", "point_id": str(w["alfa"])}, NOW, "o")
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (stages["at_point"], asked))
    conn.commit()
    monkeypatch.setattr(order_reserve, "question_open", lambda conn, card, now: card["id"] == asked)
    page = _page(w["point"])
    assert _cards(page)[0] == asked
    card = page[page.index('id="order-%d"' % asked):].split("</article>")[0]
    assert "Клиент забрал заказ № " in card and "/my-point/%d/reserve_issued" % asked in card and "/my-point/%d/reserve_here" % asked in card
    assert 'class="mp-ask"' in card
    assert "/my-point/%d/issued" % asked in card and "/my-point/%d/no_show" % asked in card              # the card's own presses stay
    assert "mp-tab--hot" in page.split('data-tab="here"')[0].rsplit("<a", 1)[-1]                       # «На точке» waits for the point


def test_the_search_looks_at_every_order_of_the_point_and_names_its_tab(w):
    page = _page(w["point"], "/my-point?tab=moving&q=Клиент oth")
    assert 'data-found-tab="oth"' in page and "/orders/%d" % w["made"]["oth"] in page


# --- оповещения точке -----------------------------------------------------------------------------------------------------------

def test_the_alerts_are_a_line_seen_only_when_opened_and_the_bots_answers_are_not_shown(w):
    conn, alfa = w["conn"], w["alfa"]
    points.add_note(conn, alfa, "answer", "Клиенту ответили: шины приедут завтра", NOW, w["made"]["moving"], asked="Где мой заказ?")
    points.add_note(conn, alfa, "alert", "Шины с ОТХ отложены под клиента — не продавать", "2026-10-30T12:01:00", w["made"]["at_point"])
    conn.commit()
    page = _page(w["point"])
    assert "Оповещения: 1 новое" in page and "Шины с ОТХ отложены" not in page and "шины приедут завтра" not in page
    assert points.seen_at(conn, alfa) == ""                                                         # a look at the page is not a look at the alert
    opened = _page(w["point"], "/my-point?notes=1")
    assert "Шины с ОТХ отложены под клиента" in opened and "шины приедут завтра" not in opened
    assert points.seen_at(conn, alfa) == "2026-10-30T12:01:00"
    assert "Оповещения:" not in _page(w["point"])                                                    # nothing new: no line at all


def test_the_owner_opening_the_alerts_of_a_point_marks_nothing(w):
    conn, alfa = w["conn"], w["alfa"]
    points.add_note(conn, alfa, "alert", "Возврат отменён", NOW, w["made"]["oth"])
    conn.commit()
    owner = w["app"].test_client()
    login(owner)
    assert "Возврат отменён" in _page(owner, "/my-point?notes=1&p=%d" % alfa)
    assert points.seen_at(conn, alfa) == ""


# --- телефон и остальная панель ----------------------------------------------------------------------------------------------------

def test_the_workplace_is_made_for_a_phone_and_the_rest_keeps_its_width(w):
    page = _page(w["point"])
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in page and 'class="pt-ui"' in page
    assert "point.css" in page and 'class="pt mp-wrap"' in page
    other = _page(w["point"], "/orders")
    assert '<meta name="viewport" content="width=1280">' in other and "pt-ui" not in other and "point.css" not in other


def test_the_phone_menu_names_only_the_points_own_pages(w):
    page = _page(w["point"])
    menu = page[page.index('class="pt-menu"'):].split("</details>")[0]
    assert 'href="/orders"' in menu and 'href="/chats"' in menu and 'href="/calls"' in menu
    assert 'href="/catalog"' not in menu and 'href="/users"' not in menu
    assert '<form method="post" action="/logout"' in menu and 'name="csrf"' in menu                 # the sign-out is a POST (the route takes no GET)


# --- передача из звонка ------------------------------------------------------------------------------------------------------------

from test_calls_panel import person as call_person, setup  # noqa: E402,F401
from test_under_order import hand_call  # noqa: E402


def test_a_client_handed_over_from_a_call_is_in_the_list_with_its_cancel(setup):
    app, conn, ids = setup
    orders.ensure_pipeline(conn, NOW)
    point = call_person(app, conn, "point", ids["point"])
    row = ids["rows"][3]
    calls_page = point.get("/calls").get_data(as_text=True)
    assert ">Передать клиента</button>" in calls_page and "Передать под заказ</button>" not in calls_page
    assert hand_call(point, row, phone="+7 911 333-00-00").status_code == 302
    page = point.get("/my-point/handed").get_data(as_text=True)
    assert "+7 911 333-00-00" in page and "Отменить передачу" in page



# --- the card and «Переданные клиенты» (the owner 2026-10-30, second round) ---------------------------------------------------------

def test_the_card_is_the_webmim_number_the_tyres_and_the_client_and_the_phone_is_under_more(w):
    conn = w["conn"]
    orders.update_card(conn, w["made"]["at_point"], {"webmim_number": "5287827"}, NOW, "o")
    conn.commit()
    page = _page(w["point"])
    card = page[page.index('id="order-%d"' % w["made"]["at_point"]):].split("</article>")[0]
    top, more = card.split('<details class="mp-more">')
    assert "№ 5287827" in top and "data-tyres" in top and "Клиент at_point" in top
    assert top.index("data-tyres") < top.index("Клиент at_point")                                 # the tyres first
    assert 'href="tel:' not in top and "Телефон клиента" in more and 'href="tel:+79001110001"' in more
    moving = _page(w["point"], "/my-point?tab=moving")
    mcard = moving[moving.index('id="order-%d"' % w["made"]["moving"]):].split("</article>")[0]
    assert "№ панель %d" % w["made"]["moving"] in mcard                                           # no WebMIM number: the panel's
    assert "/my-point/%d/arrived" % w["made"]["moving"] in mcard and "/my-point/%d/issued" % w["made"]["moving"] not in mcard   # one button


def test_an_oth_card_has_the_tyres_and_no_client(w):
    page = _page(w["point"], "/my-point?tab=oth")
    card = page[page.index('id="order-%d"' % w["made"]["oth"]):].split("</article>")[0]
    assert "data-tyres" in card and "Клиент oth" not in card and 'href="tel:' not in card


def test_handed_clients_is_a_section_with_the_list_first_and_the_form_below(w):
    page = _page(w["point"], "/my-point/handed")
    assert page.index('id="mp-handed-h"') < page.index('class="mp-handoff-form"') and 'action="/my-point/handed"' in page
    assert "← Заказы" in page and 'href="/my-point"' in page
    work = _page(w["point"])
    assert 'href="/my-point/handed"' in work and "Передать клиента" in work                     # the button of «Рабочее место» and the menu
    assert work.count('href="/my-point/handed"') >= 2



def test_the_form_of_the_section_posts_to_it_and_a_refusal_comes_back_there_with_what_was_typed(w):
    from panel_helpers import post
    page = _page(w["point"], "/my-point/handed")
    token = re.search(r'action="/my-point/handed" class="mp-handoff-form".*?name="token" value="([0-9a-f]+)"', page, re.S).group(1)
    r = post(w["point"], "/my-point/handed", "tok", token=token, phone="12", need="зима")
    assert r.status_code == 400 and "Телефон клиента: 10 цифр" in r.get_data(as_text=True) and 'value="12"' in r.get_data(as_text=True)
    r = post(w["point"], "/my-point/handed", "tok", token=token, phone="+7 900 222-33-44", need="зима")
    assert r.status_code == 302 and r.headers["Location"].endswith("/my-point/handed")
    assert "+7 900 222-33-44" in _page(w["point"], "/my-point/handed")


@pytest.mark.parametrize("role", ["chats", "viewer", "client"])
def test_the_section_is_the_points_and_the_owners_only(w, role):
    from test_roles import person
    other, _ = person(w["app"], role)
    assert other.get("/my-point/handed").status_code in (302, 403)
    r = other.post("/my-point/handed", data={"csrf": "tok", "token": "a" * 24, "phone": "+7 900 222-33-44"})
    assert r.status_code in (302, 403) and w["conn"].execute("SELECT COUNT(*) FROM order_handoff").fetchone()[0] == 0
