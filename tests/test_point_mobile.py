"""The chats and the card of an order on a point's phone (2026-10-31, point-mobile): a point gets the width of its phone, the frame of
«Рабочее место» (point.css, the sections at the bottom); the office keeps the page of a computer (1280), as before."""
from __future__ import annotations

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


def test_the_office_keeps_the_computer_page(world):
    app, people, pages, ids = world
    for role in ("owner", "partner", "chats", "viewer"):
        for url in _urls(ids):
            r = people[role].get(url)
            assert r.status_code == 200 or role != "owner", url
            if r.status_code != 200:                                       # a role without the section: its own answer, untouched
                continue
            page = r.get_data(as_text=True)
            assert DESK in page and PHONE not in page, (role, url)
            assert "pt-ui" not in page and "point.css" not in page and "data-point-nav" not in page and "pt-chats-back" not in page, (role, url)
