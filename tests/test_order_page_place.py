"""ТЗ №87 (the owner 2026-11-24): the order card jumped to its end on opening, F5, «Сохранить» and a new stage (app.js scrolled .main of
the panel to the last message of the column «Чат Авито»), and «Чаты» put the list back to its top on another chat (the list of «Новый вид»
has no scroll of its own: .main moves it, the place of the list itself read 0). Now: the card opens at its top, the same address loaded
again keeps the place (sessionStorage), an anchor in the address goes where it points; the list keeps the place of the box it moves with.
The browser itself is not run here: the scripts are read as text, the pages and the redirects through the app."""
from __future__ import annotations

from pathlib import Path

from panel_helpers import post
from test_order_chat_window import page, view, window  # noqa: F401  (view: the fixture of both views)
from tires_sync import orders

JS = (Path(__file__).resolve().parents[1] / "src" / "tires_sync" / "panel" / "static" / "app.js").read_text(encoding="utf-8")


def block(start, end):
    return JS[JS.index(start):JS.index(end, JS.index(start))]


def test_the_card_has_the_mark_of_its_place_in_both_views(tmp_path, view):
    app, client, tok, conn, tanya = page(tmp_path, view)
    html = window(client, tanya["order_id"])
    assert html.count("data-keep-place") == 1
    assert '<div class="order-chat" data-order-chat="%d" data-open-end>' % tanya["id"] in html   # the office's chat column, as before
    assert "data-keep-place" not in client.get("/chats/%d" % tanya["id"]).get_data(as_text=True)   # «Чаты»: its own place of the list


def test_a_move_to_the_same_stage_comes_back_without_an_anchor(tmp_path, view):
    app, client, tok, conn, tanya = page(tmp_path, view)
    card = orders.get_card(conn, tanya["order_id"])
    r = post(client, "/orders/%d/move" % card["id"], tok, stage_id=str(card["stage_id"]))
    assert r.status_code == 302 and r.headers["Location"].endswith("/orders/%d" % card["id"])   # no «#…»: the script puts the place back


def test_the_chat_of_the_card_never_moves_the_page():
    end = block("function chatToEnd()", "var orderChat")
    assert 'orderChat.contains(thread) && window.location.hash !== "#order-chat" && isPageBox(box)) return;' in end
    page_box = block("function isPageBox(box)", "function chatBottom")
    assert "document.scrollingElement" in page_box and 'classList.contains("main")' in page_box


def test_the_card_keeps_its_place_when_loaded_again():
    keep = block("13b) Карточка заказа", "13a) Заказ")
    assert 'document.querySelector("[data-keep-place]")' in keep and "sessionStorage.removeItem(KEY)" in keep   # another page: next time from the top
    assert "mark.path === path" in keep and 'addEventListener("pagehide"' in keep                             # the same address only
    assert '!window.location.hash || (nav && nav.type === "reload")' in keep                                   # an anchor wins, except F5
    assert keep.count("try {") == 3 and keep.count("catch (e)") == 3                                           # no storage: the card as before
    assert "scrollIntoView" not in keep and "location.hash =" not in keep and "replaceState" not in keep


def test_the_chats_list_keeps_the_place_of_its_box():
    place = block("the list keeps its place", "the order column folds away")
    assert "box = visible ? chatScroller(list) : list" in place and "all[place] = box.scrollTop" in place
    assert "box.scrollTop = mark" in place and "if (!row || shared) return;" in place                          # a narrow screen: the chat's end wins
