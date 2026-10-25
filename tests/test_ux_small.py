"""Small layout fixes of the UX review 2026-10-21 (1.0, 1.0a, 1.0i, 1.0j, 1.5, 2.15, 4.7, 5.0, 5.0a): colours only from the palette, crosses
from the icon set, no dead styles, a broken photo falls back to «нет фото», the two «точки» tabs differ, tables have no inner scroll, focus is seen."""
from __future__ import annotations

import re
from pathlib import Path

from panel_helpers import post
from test_layout_1280 import new_card, setup

PANEL = Path(__file__).resolve().parents[1] / "src" / "tires_sync" / "panel"
STATIC, TEMPLATES = PANEL / "static", PANEL / "templates"
CSS = (STATIC / "panel.css").read_text(encoding="utf-8")
ROOT, BODY = CSS.split("/* ============ БАЗА ============ */", 1)


def tokens(text: str) -> set[str]:
    return set(re.findall(r"(--[\w-]+)\s*:", text))


def rule(selector: str, css: str = CSS) -> str:
    return re.search(re.escape(selector) + r"\{([^}]*)\}", css).group(1)


def test_every_colour_token_used_by_the_panel_exists_in_the_palette():
    defined = tokens(ROOT)
    for name in ("panel.css", "bell.css"):
        used = set(re.findall(r"var\((--[\w-]+)", (STATIC / name).read_text(encoding="utf-8")))
        assert used <= defined, (name, sorted(used - defined))
    for page in TEMPLATES.glob("*.html"):                                                      # the charts of «Экономика» drew with --brand-600
        used = set(re.findall(r"var\((--[\w-]+)", page.read_text(encoding="utf-8")))
        assert used <= defined, (page.name, sorted(used - defined))
    assert "--brand-" not in CSS and "--warning-" not in CSS


def test_the_styles_take_colours_from_tokens_not_from_hex_literals():
    assert not re.findall(r"#[0-9a-fA-F]{3,6}\b", BODY)
    assert "rgba(" not in BODY                                                                  # the modal backdrop is the --overlay token
    assert "#" not in (STATIC / "bell.css").read_text(encoding="utf-8")
    share = (STATIC / "share.css").read_text(encoding="utf-8")
    share_root, share_body = share.split("}", 1)[0], share.split("}", 1)[1]
    assert ":root{" in share_root and not re.findall(r"#[0-9a-fA-F]{3,6}\b", share_body)
    palette = dict(re.findall(r"(--[\w-]+):(#[0-9A-Fa-f]{6})", ROOT))
    for name, value in re.findall(r"(--[\w-]+):(#[0-9A-Fa-f]{6})", share_root):                   # the public page keeps the panel's values
        assert palette[name].upper() == value.upper(), name


def test_the_focus_ring_is_a_visible_primary_ring_on_links_tabs_and_icon_buttons():
    ring = re.search(r"--ring:([^;]+);", ROOT).group(1)
    assert "--primary-600" in ring and "--primary-100" not in ring
    focus = re.search(r"([^}\n]*):focus-visible\{outline:0;box-shadow:var\(--ring\)\}", BODY.split(".sb-link.is-active svg", 1)[1]).group(1)
    for selector in (".sb-link", ".tabs a", ".tabs2 a", ".icon-btn", ".menu-item"):
        assert selector + ":focus-visible" in focus + ":focus-visible", selector


def test_the_menu_group_labels_are_dark_enough_to_read():
    assert "color:var(--gray-500)" in rule(".sb-label")


def test_tables_have_no_inner_vertical_scroll_only_a_sideways_one():
    assert "max-height" not in rule(".table-wrap") and "overflow-x:auto" in rule(".table-wrap")


def test_dead_styles_of_the_old_component_page_and_order_card_are_gone():
    for dead in (".lib-", ".swatch", ".skel", ".sortable", ".demo{", ".funnel", ".otile", ".ocard-tiles", ".ocard-head", ".chat-item",
                 ".price-old", ".section-title", ".brand-section", ".statusbar--", ".arrow{", "shimmer", ".table-wrap--free"):
        assert dead not in CSS, dead
    for name in ("_addresses_form.html", "_pricing.html", "catalog_preview.html"):
        text = (TEMPLATES / name).read_text(encoding="utf-8")
        assert "table-wrap--free" not in text and "note-now" not in text and "preview-text" not in text, name


def test_the_crosses_are_icons_of_the_set():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'textContent = "✕"' not in js and 'textContent = "×"' not in js and '"icon-btn ml-auto", "×"' not in js
    assert js.count("x.innerHTML = X_ICON") == 4
    assert '<path d="M6 6l12 12M18 6L6 18"/>' in js and '"x": \'<path d="M6 6l12 12M18 6L6 18"/>\'' in (TEMPLATES / "_ui.html").read_text(encoding="utf-8")
    chats = (TEMPLATES / "chats.html").read_text(encoding="utf-8")
    assert re.search(r'data-photo-clear[^>]*>\{\{ icon\("x", "i-sm"\) \}\}</button>', chats)
    assert 'chat-photo-pick">{{ icon("paperclip", "i-sm") }} Фото<input' in chats and "📎" not in chats
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert 'id="bell-close" aria-label="Закрыть">{{ icon("x") }}</button>' in base and "&times;" not in base


def test_the_tag_cross_of_the_order_window_is_an_icon(tmp_path, monkeypatch):
    client, tok, _conn = setup(tmp_path, monkeypatch)
    cid = new_card(client, tok)
    post(client, "/orders/%d/save" % cid, tok, tags="срочно")
    page = client.get("/orders/%d" % cid).get_data(as_text=True)
    tag = re.search(r'<button type="button" class="owin-tag-x"[^>]*>(.*?)</button>', page).group(1)
    assert tag.startswith('<svg class="i i-sm"') and "✕" not in page


def test_a_photo_that_did_not_load_becomes_the_no_photo_plate():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    part = js[js.index("// 14) Каталог: фото"):]
    assert 'matches("img.thumb")' in part and 'box.className = "thumb"; box.textContent = "нет фото"' in part
    assert "}, true);" in part and "img.complete && !img.naturalWidth" in part
    assert '<div class="thumb">нет фото</div>' in (TEMPLATES / "_catalog_table.html").read_text(encoding="utf-8")   # the same plate as without a photo


def test_the_two_points_tabs_say_what_they_are(tmp_path, monkeypatch):
    client, _tok, _conn = setup(tmp_path, monkeypatch)
    page = client.get("/connections/points").get_data(as_text=True)
    nav = page[page.index('<nav class="tabs">'):page.index("</nav>", page.index('<nav class="tabs">'))]
    assert ">4 точки (поставщик)</a>" in nav and ">Точки выдачи</a>" in nav and ">Точки</a>" not in nav


def test_the_chat_deal_link_leads_to_the_order_without_a_dead_tab():
    chats = (TEMPLATES / "chats.html").read_text(encoding="utf-8")
    assert "?tab=" not in chats and """<a href="/orders/{{ deal.id }}{{ '#items' if 'позиции' in deal.gaps else '' }}">Заполнить в заказе</a>""" in chats
    assert 'id="items"' in (TEMPLATES / "_order_window.html").read_text(encoding="utf-8")


def test_the_table_head_sticks_to_the_page_while_wide_tables_still_scroll_sideways():
    """.table-wrap scrolls sideways, so it is the nearest scroller and th{position:sticky} sticks to it, not to the page:
    app.js moves the head of a wrapped table down itself; the plain tables keep a head that does not stick."""
    assert ".table-wrap{overflow-x:auto" in CSS and ".table--plain th{position:static}" in CSS
    assert ".table-wrap>.table:not(.table--plain)>thead{position:relative;z-index:3}" in CSS
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    block = js[js.index("// 15) Шапка таблицы"):]
    assert 'main.addEventListener("scroll", headsLater, {passive: true})' in block and "requestAnimationFrame(placeHeads)" in block
    assert 'window.addEventListener("resize"' in block and '"tires:table-loaded"' in block
    assert "if (!wraps.length) return;" in block and ":scope > .table:not(.table--plain) > thead" in block
    assert 'attributeFilter: ["hidden"]' in block                                                  # the draft bar appears without a scroll
