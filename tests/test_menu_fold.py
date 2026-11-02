"""Menu sections fold by their title (the owner 2026-11-02): base.html + static/navfold.js."""
from __future__ import annotations

import re
from pathlib import Path

from panel_helpers import login, make_app

SECTIONS = {"sales": "Продажи", "goods": "Товар и фид", "analytics": "Аналитика", "setup": "Настройка"}
NAVFOLD = Path(__file__).resolve().parents[1] / "src" / "tires_sync" / "panel" / "static" / "navfold.js"


def page(tmp_path, url, client=None):
    if client is None:
        client = make_app(tmp_path).test_client()
        login(client)
    response = client.get(url)
    assert response.status_code == 200
    return response.get_data(as_text=True)


def section_tags(html):
    return {m.group(1): m.group(0) for m in re.finditer(r'<nav class="sb-group" data-sec="(\w+)"[^>]*>', html)}


def test_every_section_has_a_fold_button_tied_to_its_links(tmp_path):
    html = page(tmp_path, "/")
    assert set(section_tags(html)) == set(SECTIONS)
    for key, label in SECTIONS.items():
        assert ('<button type="button" class="sb-fold" aria-expanded="true" aria-controls="sb-sec-%s">'
                '<span class="sb-label">%s</span>' % (key, label)) in html
        assert '<div class="sb-items" id="sb-sec-%s">' % key in html
    assert html.count('class="i sb-chev"') == len(SECTIONS)


def test_the_section_of_the_current_page_is_marked_open(tmp_path):
    client = make_app(tmp_path).test_client()
    login(client)
    for url, current in (("/feeds", "goods"), ("/settings", "setup"), ("/analytics", "analytics"), ("/chats", "sales")):
        tags = section_tags(page(tmp_path, url, client))
        assert [k for k, tag in tags.items() if " data-current" in tag] == [current], url


def test_the_home_page_marks_no_section_current(tmp_path):
    assert not [tag for tag in section_tags(page(tmp_path, "/")).values() if "data-current" in tag]


def test_fold_state_is_applied_from_a_static_head_script(tmp_path):
    html = page(tmp_path, "/")
    head = html[:html.index("</head>")]
    assert '<script src="/static/navfold.js"></script>' in head   # CSP script-src 'self': no inline script
    assert not re.search(r"<script(?![^>]*\bsrc=)", html)


def test_navfold_touches_storage_only_inside_try():
    js = NAVFOLD.read_text(encoding="utf-8")
    lines = [line for line in js.splitlines() if "localStorage" in line and not line.lstrip().startswith("//")]
    assert lines and all("try {" in line for line in lines)
    assert all('"%s"' % key in js for key in SECTIONS)


def test_folding_menu_leaves_the_page_variables_alone(tmp_path):
    """The menu captures its links without a page-level variable: /catalog and /shares/new get their own `items` (review 2026-11-02)."""
    client = make_app(tmp_path).test_client()
    login(client)
    assert 'aria-label="Выбрать TS1"' in page(tmp_path, "/catalog", client)   # a product row of the table
    shared = client.post("/shares/new", data={"csrf": "tok", "g": "TS1"})   # not in this unload: its own `items` is empty, no card of items
    assert shared.status_code == 200 and "Позиции в ссылке:" not in shared.get_data(as_text=True)
