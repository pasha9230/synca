"""The owner 2026-11-10 of «Рабочее место» of a point (both views): «Включить уведомления» in the head beside «Оповещения» (its state a small
line under the head, none when empty), and the page refreshing itself on news while the point is idle (no «Есть новое. Обновить»)."""
from __future__ import annotations

import re

import pytest

from test_mypoint import manager
from test_point_issued import setup


@pytest.fixture(params=["old", "new"])
def view(request):
    return request.param


def head(page: str) -> str:
    return re.search(r"<header class=\"(?:pt-top mp-head|p2-head)\">.*?</header>", page, re.S).group(0)


def test_notify_button_is_in_the_head_beside_the_alerts(tmp_path, view):
    app, owner, conn, pid, card = setup(tmp_path, view)
    page = manager(app, conn, pid).get("/my-point").get_data(as_text=True)
    top = head(page)
    assert page.count("data-notify-enable") == 1 and "data-notify-enable" in top
    alerts, button = top.index('aria-label="Оповещения'), top.index("data-notify-enable")
    between = top[alerts:button]
    assert alerts < button and between.count("<a ") == 0                                    # right after «Оповещения», nothing between
    assert re.search(r'<button type="button" class="[^"]*(btn--secondary|p2-ico)[^"]*" data-notify-enable hidden>Включить уведомления</button>', top)
    assert "data-notify-box" not in page and 'class="mp-notify"' not in page                 # no line of its own over the search
    after = page[page.index("</header>"):]
    assert re.match(r'</header>\n<p class="[^"]*(mp|p2)-notify-state" data-notify-state></p>', after)   # small, under the head


def test_no_notify_button_for_the_owner(tmp_path, view):
    app, owner, conn, pid, card = setup(tmp_path, view)
    page = owner.get("/my-point").get_data(as_text=True)
    assert "data-notify-enable" not in page and "data-notify-state" not in page


def test_the_state_line_takes_no_room_when_empty():
    from pathlib import Path
    static = Path(__file__).resolve().parents[1] / "src" / "tires_sync" / "panel" / "static"
    assert ".mp-notify-state:empty{display:none}" in (static / "mypoint.css").read_text(encoding="utf-8")
    css = (static / "point2.css").read_text(encoding="utf-8")
    assert ".p2-notify-state:empty{display:none}" in css
    assert ".p2-head-acts:has(.p2-notify-on:not([hidden])){flex-wrap:wrap}" in css           # a phone: the head wraps, it does not break
