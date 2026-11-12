"""The owner 2026-11-12 (the partner's answers; «Го» on the plan). «Рабочее место» of a point:
1. six tabs, «Согласование возврата» between «На ОТХ» and «На возврат»: the orders of «На возврат» not agreed yet and those of «На ОТХ» with a
   position on the agreement of the return (the whole order: a record is sold or returned whole); the chosen tab says what lies in it."""
from __future__ import annotations

import re
from datetime import date

import pytest

from test_mypoint import manager
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
