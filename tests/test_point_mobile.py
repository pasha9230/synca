"""The chats and the card of an order on a point's phone (2026-10-31, point-mobile): a point gets the width of its phone, the frame of
«Рабочее место» (point.css, the sections at the bottom). The office (the owner 2026-11-10, office-mobile): the width of its phone too, but its
own frame (body.ofc, the «бургер», office-mobile.css), never the point's; a computer sees the same page as before."""
from __future__ import annotations

import re

from test_buttons_by_rights import cheap_hashes, world  # noqa: F401  (fixtures)

PHONE = '<meta name="viewport" content="width=device-width, initial-scale=1">'
DESK = '<meta name="viewport" content="width=1280">'


def _urls(ids):
    return ["/chats", "/chats/%d" % ids["chat"], "/orders/%d" % ids["card"]]


def test_a_point_gets_its_phone_on_the_chats_and_the_card(world):
    app, people, pages, ids = world
    for url in _urls(ids):
        r = people["point"].get(url)
        page = r.get_data(as_text=True)
        assert r.status_code == 200, url
        assert PHONE in page and DESK not in page, url
        assert "pt-ui" in page and "point.css" in page and "data-point-nav" in page, url
    chat = people["point"].get("/chats/%d" % ids["chat"]).get_data(as_text=True)
    assert "is-chat-open" in chat and 'class="pt-chats-back"' in chat
    assert 'data-sec="chats"' in chat and 'aria-current="page" data-sec="chats"' in chat
    listed = people["point"].get("/chats").get_data(as_text=True)
    assert "is-chat-open" not in listed and "pt-chats-back" not in listed
    card = people["point"].get("/orders/%d" % ids["card"]).get_data(as_text=True)
    assert "pt-card-page" in card and "data-point-nav" in card and 'data-sec="orders"' not in card     # «Заказы» hidden (the owner 2026-11-02)


def test_the_office_gets_its_own_phone_frame_not_the_points(world):
    app, people, pages, ids = world
    for role in ("owner", "partner", "chats", "viewer"):
        for url in _urls(ids):
            r = people[role].get(url)
            assert r.status_code == 200 or role != "owner", url
            if r.status_code != 200:                                       # a role without the section: its own answer, untouched
                continue
            page = r.get_data(as_text=True)
            assert PHONE in page and DESK not in page, (role, url)
            assert 'class="mnav-btn" data-mnav' in page and "office-mobile.css" in page and re.search(r'<body class="[^"]*\bofc\b', page), (role, url)
            assert "pt-ui" not in page and "point.css" not in page and "data-point-nav" not in page and "pt-chats-back" not in page, (role, url)


def test_one_phone_rule_for_every_page_of_a_point_and_the_old_calls(world):
    """The owner 2026-11-28 (a point on a phone): point-mobile.css on every page (only body.pt-ui inside, the office sees nothing of it);
    the old view of «Звонки» gets the point's phone frame instead of a computer's 1280 px; «Уведомления» are cards on a phone."""
    app, people, pages, ids = world
    calls = people["point"].get("/calls")
    assert calls.status_code == 200
    page = calls.get_data(as_text=True)
    assert PHONE in page and DESK not in page
    assert "pt-ui" in page and "point.css" in page and "point-mobile.css" in page and 'aria-current="page" data-sec="calls"' in page
    assert "data-m-cards" in page
    alerts = people["point"].get("/alerts").get_data(as_text=True)
    assert "point-mobile.css" in alerts and "data-m-cards" in alerts and 'class="pt-scroll"' in alerts
    office = people["owner"].get("/calls").get_data(as_text=True)
    assert "pt-ui" not in office and "data-point-nav" not in office and re.search(r'<body class="[^"]*\bofc\b', office)


def test_the_phone_rule_of_a_point_is_for_its_cabinet_only():
    from test_ui_new import STATIC
    css = (STATIC / "point-mobile.css").read_text(encoding="utf-8")
    body = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for rule in re.findall(r"([^{}]+)\{[^{}]*\}", re.sub(r"@media[^{]*\{", "}", body)):
        for sel in re.split(r",(?![^()]*\))", rule):
            assert sel.strip().startswith("body.pt-ui"), sel
    assert "font-size:16px" in body and "min-height:44px" in body and "#toasts" in body
