"""«Комментарий» of the order's window on EVERY tab of «Рабочее место» (the owner 2026-11-25): Едут, На точке, На ОТХ, На возврат (the card and
the agreed return's row), Выдано and the search — in both views; only when typed, escaped, no automatic part, never an Avito account's name;
another point sees none; the print of the feed (point_rev) carries it also for «Выдано»."""
from __future__ import annotations

import re

import pytest

from test_mypoint import manager, press
from test_point_new_view import flag, world
from test_vk_commands import _card, _stage
from tires_sync import connections, order_points as points, orders
from tires_sync.panel import routes_mypoint

LATER = "2026-10-14T10:00:00"
TEXT = "Позвонить заранее\n<i>после 18:00</i>"


@pytest.fixture(params=["old", "new"])
def view(request):
    return request.param


@pytest.fixture
def w(tmp_path, view, monkeypatch):
    from test_point_new_view import world as make
    app, owner, conn, pid, ids = make(tmp_path)
    if view == "new":
        flag(conn, "all")
    monkeypatch.setattr(routes_mypoint, "local_iso", lambda: "2026-10-13T12:00:00")
    issued = _card(conn, pid, webmim="700210", code="at_point")
    orders.move_card(conn, issued, _stage(conn, "done"), "2026-10-12T09:00:00", "owner")      # closed by the office from «На точке»
    ids["issued"] = issued
    conn.commit()
    return dict(app=app, conn=conn, pid=pid, ids=ids, point=manager(app, conn, pid))


def section(page, order):
    """Everything the page says of one order: its card (or the row of the agreed return)."""
    parts = re.findall(r'<article[^>]*data-(?:order|return)="%d".*?</article>' % order, page, re.S)
    assert parts, "the order %d is not on the page" % order
    return "".join(parts)


TABS = [("moving", "moving"), ("here", "here"), ("here", "asked"), ("oth", "oth"), ("return", "ret"), ("issued", "issued")]


@pytest.mark.parametrize("tab,key", TABS)
def test_every_tab_shows_the_typed_comment_and_nothing_when_empty(w, tab, key):
    order = w["ids"][key]
    page = lambda: w["point"].get("/my-point?tab=%s" % tab).get_data(as_text=True)
    assert "data-point-comment" not in section(page(), order)                               # none typed: no line
    orders.update_card(w["conn"], order, dict(comment=TEXT), LATER, "o")
    w["conn"].commit()
    html = section(page(), order)
    assert "data-point-comment" in html and "Комментарий" in html
    assert "Позвонить заранее\n&lt;i&gt;после 18:00&lt;/i&gt;" in html and "<i>после" not in html
    orders.update_card(w["conn"], order, dict(comment=""), LATER, "o")
    w["conn"].commit()
    assert "data-point-comment" not in section(page(), order)


def test_the_agreed_return_row_and_its_card_both_carry_it_and_no_purchase(w):
    order = w["ids"]["ret"]
    orders.update_card(w["conn"], order, dict(comment="Забрать у входа"), LATER, "o")
    w["conn"].commit()
    page = w["point"].get("/my-point?tab=return").get_data(as_text=True)
    assert 'data-return="%d"' % order in page and "Забрать у входа" in page
    assert "4 000" not in section(page, order) and "supplier" not in section(page, order).lower()


def test_the_search_shows_it_in_issued_and_in_cards(w):
    for key in ("here", "issued"):
        orders.update_card(w["conn"], w["ids"][key], dict(comment="Найдите меня"), LATER, "o")
    w["conn"].commit()
    page = w["point"].get("/my-point?q=Татьяна").get_data(as_text=True)
    assert page.count("Найдите меня") >= 2


def test_no_automatic_part_no_account_name_on_issued_and_return(w):
    connections.create_account(w["conn"], "Шинный Двор", LATER, "o")
    for key, tab in (("issued", "issued"), ("ret", "return")):
        order = w["ids"][key]
        orders.update_card(w["conn"], order, dict(comment="Объявление: Шины https://www.avito.ru/1 — ждёт звонка, Шинный Двор"), LATER, "o")
        w["conn"].commit()
        html = section(w["point"].get("/my-point?tab=%s" % tab).get_data(as_text=True), order)
        assert "ждёт звонка" in html and "avito.ru" not in html and "Шинный Двор" not in html
        orders.update_card(w["conn"], order, dict(comment="Объявление: Шины https://www.avito.ru/1"), LATER, "o")
        w["conn"].commit()
        assert "data-point-comment" not in section(w["point"].get("/my-point?tab=%s" % tab).get_data(as_text=True), order)


def test_another_point_sees_no_comment_on_any_tab(w):
    for key in ("moving", "here", "oth", "ret", "issued"):
        orders.update_card(w["conn"], w["ids"][key], dict(comment="Секрет Беты"), LATER, "o")
    w["conn"].commit()
    other = points.save_point(w["conn"], dict(name="Гамма", channel="vk", vk_peer_id="777"))
    theirs = manager(w["app"], w["conn"], other, login_="delta")
    for tab in ("moving", "here", "oth", "agree", "return", "issued"):
        assert "Секрет Беты" not in theirs.get("/my-point?tab=%s" % tab).get_data(as_text=True)
    assert "Секрет Беты" not in theirs.get("/my-point/feed").get_data(as_text=True)


def test_the_print_changes_with_the_comment_of_an_issued_order_and_the_page_carries_it(w):
    page = w["point"].get("/my-point?tab=issued").get_data(as_text=True)
    shown = re.search(r'data-point-rev="([0-9a-f]+)"', page).group(1)
    rev = w["point"].get("/my-point/feed").get_json()["rev"]
    assert rev == shown
    orders.update_card(w["conn"], w["ids"]["issued"], dict(comment="Выдан, но с пометкой"), LATER, "o")
    w["conn"].commit()
    assert w["point"].get("/my-point/feed").get_json()["rev"] != rev
