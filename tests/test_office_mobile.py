"""The office on a phone (the owner 2026-11-10, office-mobile): the pages of the owner, the partner and the managers get the width of the
phone and their own frame (body.ofc, the «бургер», office-mobile.css). A computer sees nothing of it: every rule of office-mobile.css sits
in a @media for a narrow window and is scoped by body.ofc; a point's cabinet and the client's /shop never get body.ofc."""
from __future__ import annotations

import re
from pathlib import Path

from test_buttons_by_rights import cheap_hashes, world  # noqa: F401  (fixtures)

STATIC = Path(__file__).resolve().parents[1] / "src" / "tires_sync" / "panel" / "static"
PHONE = '<meta name="viewport" content="width=device-width, initial-scale=1">'


def _css() -> str:
    return re.sub(r"/\*.*?\*/", "", (STATIC / "office-mobile.css").read_text(encoding="utf-8"), flags=re.S)


def _top_level(css: str) -> list:
    """(prelude, body) of every top-level block: a @media keeps its whole inside as the body."""
    out, depth, start, head = [], 0, 0, ""
    for i, ch in enumerate(css):
        if ch == "{":
            if depth == 0:
                head, start = css[:i].rsplit("}", 1)[-1].strip(), i + 1
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                out.append((head, css[start:i]))
    return out


def _rules(body: str) -> list:
    return [(sel.strip(), decl) for sel, decl in re.findall(r"([^{}]+)\{([^{}]*)\}", re.sub(r"@media[^{]*\{", "", body))]


def test_a_computer_sees_nothing_of_the_phone_styles():
    blocks = _top_level(_css())
    assert blocks
    outside = [head for head, _ in blocks if not head.startswith("@media")]
    assert outside == [".mnav-btn,.mnav-shade"], outside                         # only: the «бургер» and its shade hidden
    assert re.sub(r"\s", "", dict(blocks)[".mnav-btn,.mnav-shade"]) == "display:none"
    for head, _ in blocks:
        if head.startswith("@media"):
            widths = [int(w) for w in re.findall(r"max-width:\s*(\d+)px", head)]
            assert widths and max(widths) <= 1023, head                           # never the width of a computer (1024+)
            assert "min-width" not in head or "max-width" in head, head


def _selectors(sel: str) -> list:
    """The selectors of a list, a comma inside :has(…) / :is(…) / :not(…) kept."""
    out, depth, cur = [], 0, ""
    for ch in sel:
        depth += (ch == "(") - (ch == ")")
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    return out + [cur]


def test_every_rule_is_the_offices_own():
    for head, body in _top_level(_css()):
        if not head.startswith("@media"):
            continue
        for sel, _ in _rules(body):
            for one in _selectors(sel):
                one = one.strip()
                assert one.startswith(("body.ofc", "html.is-nav-lock")), one     # never a point's page (pt-ui) or the shop (body.shop)


def test_the_phone_rules_people_asked_for():
    css = re.sub(r"\s", "", _css())
    assert "body.ofc,body.ofc.o2{min-width:0}" in css                            # no page wider than the phone
    assert "font-size:16px" in css                                                # fields: iOS does not zoom in
    assert "min-height:44px" in css                                               # a finger's size
    assert "body.ofc{--fs-xs:14px;--fs-sm:14px;" in css                           # the small print: 14 px
    assert "table.m-cards" in css and "content:attr(data-mlabel)" in css          # the tables as cards
    assert ".modal-foot{position:sticky" in css                                   # the buttons of a window in sight
    assert ".o2-cl-comp{position:sticky;bottom:0" in css                          # the answer of a chat at the bottom, in the page


def test_the_script_labels_the_cells_and_opens_the_menu():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'matchMedia("(max-width:760px)")' in js and 'setAttribute("data-mlabel"' in js and '"m-cards"' in js
    assert "data-m-scroll" in js                                                   # a table that cannot be cards keeps its own scroll
    assert 'querySelector("[data-mnav]")' in js and '"is-nav-open"' in js and 'contains("ofc")' in js


def test_the_office_gets_the_phone_frame_and_the_point_keeps_its_own(world):
    app, people, pages, ids = world
    for url in ("/", "/orders", "/orders/%d" % ids["card"], "/chats/%d" % ids["chat"], "/catalog", "/oth", "/alerts"):
        page = people["owner"].get(url).get_data(as_text=True)
        assert PHONE in page, url
        assert re.search(r'<body class="[^"]*\bofc\b', page), url
        assert 'class="mnav-btn" data-mnav' in page and 'aria-controls="sidebar"' in page and '<aside class="sidebar" id="sidebar">' in page, url
        assert page.index("office-mobile.css") > page.index("panel.css"), url      # after the page's own: its rules win on a phone
    for url in ("/my-point", "/chats", "/alerts"):
        page = people["point"].get(url).get_data(as_text=True)
        assert "mnav-btn" not in page and not re.search(r'<body class="[^"]*\bofc\b', page), url
    alerts = people["point"].get("/alerts").get_data(as_text=True)       # the design review 2026-11-12 (Б4 of 08.11): the point's frame on a phone
    assert PHONE in alerts and re.search(r'<body class="[^"]*\bpt-ui\b', alerts) and 'class="pt-nav"' in alerts and "point.css" in alerts


def test_the_sign_in_follows_the_phone(world):
    app, people, pages, ids = world
    page = app.test_client().get("/login").get_data(as_text=True)
    assert PHONE in page and re.search(r'<body class="[^"]*\bofc\b', page) and "mnav-btn" not in page
