"""The owner 2026-11-12 (the partner's answers; «Го» on the plan). «Рабочее место» of a point:
1. six tabs, «Согласование возврата» between «На ОТХ» and «На возврат»: the orders of «На возврат» not agreed yet and those of «На ОТХ» with a
   position on the agreement of the return (the whole order: a record is sold or returned whole); the chosen tab says what lies in it;
2. «Продать» of the point is on «На ОТХ» only (over the decision of 2026-11-10 «sold until agreed»): on «Согласование возврата» and «На возврат»
   there is no button and the server refuses; the office sells and takes the agreement off as before."""
from __future__ import annotations

import re
from datetime import date

import pytest

from panel_helpers import post
from test_mypoint import manager, token
from test_point_new_view import flag
from test_point_oth_sell import setup
from test_vk_commands import _stage
from tires_sync import order_oth, orders
from tires_sync.panel import routes_mypoint

T1 = "2026-10-16T11:00:00"
NOTES = {"moving": ["Ждём доставки. Отметьте «Приехал», как прибудут на точку!"],
         "here": ["Эти позиции у нас, ожидаем выкупа заказчиком."],
         "oth": ["Невыкуп, у нас на хранении 7 дней. Если продаёте — впишите данные покупателя.", "Можно продать, +50 рублей / шт!"],
         "agree": ["Отправили запрос на оформление возврата этих позиций. Теперь для продажи нужна проверка."],
         "return": ["Эти товары согласованы на возврат! Возвраты возможны в пн, ср, пт."],
         "issued": ["Заказы, которые отмечены как реализованные."]}


@pytest.fixture
def clock(monkeypatch):
    moment = {"now": T1}
    monkeypatch.setattr(routes_mypoint, "local_iso", lambda: moment["now"])
    return moment


@pytest.fixture(params=["old", "new"])
def view(request):
    return request.param


def item_of(conn, card):
    return conn.execute("SELECT id FROM order_item WHERE order_id = ?", (card,)).fetchone()[0]


def to_return_stage(conn, card):
    orders.move_card(conn, card, _stage(conn, orders.RETURN_GIVEN), "2026-10-15T10:00:00", "o")
    conn.commit()


def tab(client, key):
    return client.get("/my-point?tab=%s" % key).get_data(as_text=True)


def counts(page) -> dict:
    return {key: int(n) for key, n in re.findall(r'data-tab-count="(\w+)">(\d+)<', page)}


def has(page, card) -> bool:
    return 'id="order-%d"' % card in page


def test_six_tabs_in_their_order_with_the_short_names_of_a_phone(tmp_path, clock, view):
    app, owner, conn, pid, card = setup(tmp_path, view)
    page = tab(manager(app, conn, pid), "oth")
    assert re.findall(r'data-tab="(\w+)"', page) == ["moving", "here", "oth", "agree", "return", "issued"]
    assert list(routes_mypoint.TABS.values()) == ["Едут", "На точке", "На ОТХ", "Согласование возврата", "На возврат", "Выдано"]
    assert ">Согласование возврата</span>" in page
    if view == "new":
        assert '<span class="p2-tab-s" aria-hidden="true">Согласование</span>' in page


@pytest.mark.parametrize("key", list(NOTES))
def test_the_chosen_tab_says_what_lies_in_it_word_for_word(tmp_path, clock, view, key):
    app, owner, conn, pid, card = setup(tmp_path, view)
    page = tab(manager(app, conn, pid), key)
    note = re.search(r"<p [^>]*data-tab-note>(.*?)</p>", page, re.S).group(1)
    assert re.findall(r"<span>(.*?)</span>", note) == NOTES[key]                     # «На ОТХ»: two lines, the second on its own
    assert sum(page.count(line) for lines in NOTES.values() for line in lines) == len(NOTES[key])     # of the chosen tab only


def test_an_order_on_oth_with_a_position_on_the_agreement_lies_whole_on_the_new_tab(tmp_path, clock, view):
    app, owner, conn, pid, card = setup(tmp_path, view)
    mine = manager(app, conn, pid)
    page = tab(mine, "oth")
    assert has(page, card) and (counts(page)["oth"], counts(page)["agree"], counts(page)["return"]) == (1, 0, 0)
    assert order_oth.item_to_return(conn, item_of(conn, card), "2026-10-14T10:00:00", "owner", 2)     # 2 of 4: the whole order all the same
    assert order_oth.agreeing(conn, orders.get_card(conn, card))
    page = tab(mine, "agree")
    assert has(page, card) and (counts(page)["oth"], counts(page)["agree"], counts(page)["return"]) == (0, 1, 0)
    assert 'data-tab="agree" aria-current="page"' in page
    assert not has(tab(mine, "oth"), card) and not has(tab(mine, "return"), card)


def test_the_return_not_agreed_is_on_the_new_tab_and_agreed_goes_to_na_vozvrat(tmp_path, clock, view):
    app, owner, conn, pid, card = setup(tmp_path, view)
    to_return_stage(conn, card)
    mine = manager(app, conn, pid)
    page = tab(mine, "agree")
    assert has(page, card) and (counts(page)["oth"], counts(page)["agree"], counts(page)["return"]) == (0, 1, 0)
    assert "Ждут согласования возврата" not in page                                  # the block of «На возврат» is this tab now
    page = tab(mine, "return")
    assert not has(page, card) and "Возвратов поставщику сейчас нет." in page
    order_oth.agree(conn, orders.get_card(conn, card), date.fromisoformat("2026-10-16"), "2026-10-15T12:00:00", "owner")
    assert not order_oth.agreeing(conn, orders.get_card(conn, card))
    page = tab(mine, "return")
    assert 'data-return="%d"' % card in page and "Согласовано на возврат, 16.10" in page
    assert (counts(page)["oth"], counts(page)["agree"], counts(page)["return"]) == (0, 0, 1)
    assert not has(tab(mine, "agree"), card)


def test_the_search_names_the_new_tab(tmp_path, clock, view):
    app, owner, conn, pid, card = setup(tmp_path, view)
    to_return_stage(conn, card)
    page = manager(app, conn, pid).get("/my-point?q=%d" % card).get_data(as_text=True)
    assert 'data-found-tab="agree">Согласование возврата<' in page


# ---- 2. «Продать» of the point: on «На ОТХ» only ------------------------------------------------------------------------------------

REFUSED = "Шины на согласовании возврата: продать можно после отмены согласования оператором"


def events(conn, card) -> list:
    return [r[0] for r in conn.execute("SELECT text FROM order_event WHERE order_id = ? ORDER BY id", (card,))]


def test_the_button_is_on_oth_only(tmp_path, clock, view):
    app, owner, conn, pid, card = setup(tmp_path, view)
    mine = manager(app, conn, pid)
    assert 'action="/my-point/%d/oth_sold"' % card in tab(mine, "oth")
    order_oth.item_to_return(conn, item_of(conn, card), "2026-10-14T10:00:00", "owner")
    assert has(tab(mine, "agree"), card)
    assert all("oth_sold" not in tab(mine, key) for key in routes_mypoint.TABS)           # in the card
    to_return_stage(conn, card)
    assert has(tab(mine, "agree"), card) and all("oth_sold" not in tab(mine, key) for key in routes_mypoint.TABS)
    order_oth.agree(conn, orders.get_card(conn, card), date.fromisoformat("2026-10-16"), "2026-10-15T12:00:00", "owner")
    page = tab(mine, "return")
    assert 'data-return="%d"' % card in page and "oth_sold" not in page                   # on the plaque of the return


@pytest.mark.parametrize("how", ["marked", "stage"])
def test_a_press_of_an_old_page_is_refused_in_plain_words_and_changes_nothing(tmp_path, clock, how):
    app, owner, conn, pid, card = setup(tmp_path)
    mine = manager(app, conn, pid)
    tok = token(tab(mine, "oth"), card, "oth_sold")                                       # the page was open before the office marked the return
    if how == "marked":
        order_oth.item_to_return(conn, item_of(conn, card), "2026-10-14T10:00:00", "owner", 1)
    else:
        to_return_stage(conn, card)
    before = orders.get_card(conn, card)["stage"]["code"]
    r = post(mine, "/my-point/%d/oth_sold" % card, "tok", token=tok, confirm="yes")
    assert r.status_code == 302 and REFUSED in mine.get(r.headers["Location"]).get_data(as_text=True)
    assert orders.get_card(conn, card)["stage"]["code"] == before and "Точка продала шины с ОТХ" not in events(conn, card)
    assert order_oth.sell(conn, orders.get_card(conn, card), "gamma", T1) == (order_oth.AGREEING_REFUSED, REFUSED, False)


def test_the_office_sells_on_the_agreement_as_before(tmp_path, clock):
    app, owner, conn, pid, card = setup(tmp_path)
    order_oth.item_to_return(conn, item_of(conn, card), "2026-10-14T10:00:00", "owner")
    outcome, answer, done = order_oth.sell(conn, orders.get_card(conn, card), "owner", T1, office=True)
    assert done and orders.get_card(conn, card)["stage"]["code"] == "done"


def test_the_agreement_taken_off_by_the_office_leaves_the_order_on_the_agreement_tab(tmp_path, clock):
    """«Отменить согласование» of the office works as before: the date goes, the order waits for the agreement again — still not the point's to sell."""
    app, owner, conn, pid, card = setup(tmp_path)
    to_return_stage(conn, card)
    order_oth.agree(conn, orders.get_card(conn, card), date.fromisoformat("2026-10-16"), "2026-10-15T12:00:00", "owner")
    assert order_oth.unagree(conn, orders.get_card(conn, card), "2026-10-15T13:00:00", "owner")
    mine = manager(app, conn, pid)
    page = tab(mine, "agree")
    assert has(page, card) and "oth_sold" not in page and 'data-return="%d"' % card not in tab(mine, "return")


def test_the_mark_taken_off_by_the_office_gives_the_button_back(tmp_path, clock):
    app, owner, conn, pid, card = setup(tmp_path)
    item = item_of(conn, card)
    order_oth.item_to_return(conn, item, "2026-10-14T10:00:00", "owner")
    order_oth.unmark_returned(conn, item, "2026-10-14T11:00:00", "owner")
    mine = manager(app, conn, pid)
    assert not order_oth.agreeing(conn, orders.get_card(conn, card))
    assert 'action="/my-point/%d/oth_sold"' % card in tab(mine, "oth") and not has(tab(mine, "agree"), card)
