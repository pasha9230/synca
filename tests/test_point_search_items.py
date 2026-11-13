"""«Рабочее место»: the search shows the positions found only, as in their tab with their buttons, never a link into the order (the owner
2026-11-13: «Когда в поиске точка ищет заказ, показывай только найденную позицию»). Made-up orders and names only."""
from __future__ import annotations

import pytest

from test_mypoint import manager
from test_point_new_view import flag
from test_vk_commands import SENT, _card, ready
from tires_sync import orders


@pytest.fixture(params=["old", "new"])
def view(request):
    return request.param


def two_at_point(tmp_path, view):
    app, owner, tok, conn, pid, first = ready(tmp_path, SENT)
    if view == "new":
        flag(conn, "all")
    orders.move_card(conn, first, conn.execute("SELECT id FROM order_stage WHERE code = 'at_point'").fetchone()[0], "2026-10-13T12:00:00", "o")
    second = _card(conn, pid, code="at_point", webmim="5000200")
    conn.commit()
    return app, conn, pid, first, second


def test_the_search_shows_the_found_card_only_with_its_buttons(tmp_path, view):
    app, conn, pid, first, second = two_at_point(tmp_path, view)
    mine = manager(app, conn, pid)
    whole = mine.get("/my-point?tab=here").get_data(as_text=True)
    assert 'data-order="%d"' % first in whole and 'data-order="%d"' % second in whole
    page = mine.get("/my-point?q=5000200").get_data(as_text=True)
    assert 'data-order="%d"' % second in page and 'data-order="%d"' % first not in page
    assert "data-found-list" in page and 'action="/my-point/%d/' % second in page             # the buttons of the card are there
    assert 'href="/orders/%d"' % second not in page                                           # no link into the order
    assert 'data-found-tab="here"' in page


def test_nothing_found_shows_no_cards(tmp_path, view):
    app, conn, pid, first, second = two_at_point(tmp_path, view)
    page = manager(app, conn, pid).get("/my-point?q=9999999").get_data(as_text=True)
    assert "Среди заказов точки ничего не нашлось." in page
    assert 'data-order="%d"' % first not in page and 'data-order="%d"' % second not in page
