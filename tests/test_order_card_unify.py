"""ТЗ №89 и №90 (the owner 2026-11-25).

№89 «Карточка заказа едина независимо откуда в неё попадаешь»: the card is the one page /orders/<id>, and what it shows follows from the order
(its stage, its chat, its record of storage) and the person's role, never from where the person came from — the chat's «Открыть полностью»,
the list, CRM, «На ОТХ», a new order made by hand or «Новый заказ этому клиенту». «Новый вид» lost the block of a record of storage
(«Вернуть заказ клиенту»): it is there again, as in the old card.

№90 «Бесшовное обновление»: a filter, a tab, a button or F5 that load the same page again keep its place (app.js 13d), as the card does
(13c, ТЗ №87). The browser is not run here: the script is read as text, the pages through the app."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from panel_helpers import post
from test_order_chat_window import page, window
from test_oth_sale import panel
from test_oth_storage import MOVED, at_point, record
from test_order_reserve import stage
from tires_sync import orders, store

OTH_SPLIT = True
JS = (Path(__file__).resolve().parents[1] / "src" / "tires_sync" / "panel" / "static" / "app.js").read_text(encoding="utf-8")
# what only an order with a chat of Avito has: the column «Чат Авито» and «Позиция из объявления»
CHAT_BOUND = {"chats-live", "chats-offline", "order-chat", "ow-ad-position"}


def blocks(html):
    """The parts of the card: the ids of its blocks and windows (numbers of rows left out) and the headings of its sections."""
    body = html.split("<main", 1)[-1]
    ids = {i for i in re.findall(r'\sid="([a-zA-Z][\w-]*?)(?:-\d+)?"', body)} - CHAT_BOUND
    heads = {re.sub(r"\s*·\s*заказ.*|\s*по заказу.*|\s*\(\d+\)", "", re.sub("<[^>]+>", "", h)).strip()
             for h in re.findall(r"<(?:h3|summary)[^>]*>(.*?)</(?:h3|summary)>", body, re.S)} - {"Чаты Авито"}
    return ids, heads


def same_page(html):
    """The page without the tokens of the forms (csrf, the receipt's and the reply's own: new on every page)."""
    return re.sub(r'name="csrf" value="[^"]*"|data-csrf="[^"]*"|value="[0-9a-f]{16,}"', "", html)


@pytest.mark.parametrize("view", ["old", "new"])
def test_the_card_is_the_same_wherever_it_is_opened_from(tmp_path, view):
    app, client, tok, conn, tanya = page(tmp_path, view)
    card = tanya["order_id"]
    window(client, card)                                                   # «взяли в работу» once: the journal is the same after it
    shown = {same_page(client.get("/orders/%d" % card, headers={"Referer": "http://localhost" + ref}).get_data(as_text=True))
             for ref in ("/chats/%d" % tanya["id"], "/orders", "/orders?v=list", "/clients", "/oth", "/my-point", "/catalog")}
    assert len(shown) == 1


@pytest.mark.parametrize("view", ["old", "new"])
def test_an_order_made_by_hand_or_for_the_same_client_has_the_card_of_an_order_made_in_the_chat(tmp_path, view):
    app, client, tok, conn, tanya = page(tmp_path, view)
    chat_order = tanya["order_id"]
    r = post(client, "/orders/new", tok, client_name="Пётр", client_phone="+7 900 555-44-33", source="avito", deal_kind="webmim")
    assert r.status_code == 302
    by_hand = int(r.headers["Location"].rsplit("/", 1)[1])
    first = blocks(window(client, chat_order))
    assert "wants" in first[0] and "Что нужно клиенту" in first[1]          # the part «Что нужно клиенту» → «Ожидает наличие», in each
    assert blocks(window(client, by_hand)) == first
    r = post(client, "/orders/%d/again" % chat_order, tok)                  # «Новый заказ этому клиенту»: both orders are in one chain now
    assert r.status_code == 302
    again = int(r.headers["Location"].split("#")[0].rsplit("/", 1)[1])
    assert len({chat_order, by_hand, again}) == 3
    for other in (by_hand, again):
        assert orders.get_card(conn, other)["stage"]["code"] == orders.get_card(conn, chat_order)["stage"]["code"]
    chained = blocks(window(client, chat_order))
    assert chained[0] - first[0] == {"ow-chain-unlink"}                     # the order decides: «Отвязать» only in a chain
    assert blocks(window(client, again)) == chained


@pytest.mark.parametrize("view", ["old", "new"])
def test_the_record_of_storage_shows_in_both_views(tmp_path, view):
    app, owner, tok, conn = panel(tmp_path)
    store.set_state(conn, "ui_new.office", "all" if view == "new" else "off")
    conn.commit()
    cid = at_point(conn)
    assert orders.move_card(conn, cid, stage(conn, "oth"), MOVED, "o", changes={"cancel_reason": "отказ"})
    x = record(conn, cid)
    held = owner.get("/orders/%d" % x).get_data(as_text=True)
    assert ("o2-card" in held) == (view == "new")
    assert "data-oth-storage" in held and "Запись хранения: шины без клиента" in held and 'action="/orders/%d/storage-back"' % x in held
    cancelled = owner.get("/orders/%d" % cid).get_data(as_text=True)
    assert "Заказ отменён: клиент отказался" in cancelled and "Вернуть заказ клиенту" in cancelled


# ---- ТЗ №90: the place of a list page -----------------------------------------------------------------------------------------------

def place():
    return JS[JS.index("13d) Списки и разделы"):JS.index("13b) Новый заказ")]


def test_the_place_is_kept_only_on_the_same_page_after_a_press_on_it():
    keep = place()
    assert 'path = window.location.pathname' in keep and "mark.path !== path" in keep                        # the address without the filters
    assert '!(mark.keep || reload)' in keep and 'nav.type === "reload"' in keep                               # a press here or F5, never a visit
    assert 'addEventListener("submit", function () { armed = true; }, true)' in keep                          # a form, a button
    assert 'form[data-autosubmit], details.msel-box' in keep                                                  # a filter applied at once (form.submit())
    assert "samePage(" in keep and 'a.closest(".pager")' in keep                                              # a link here, not another page of the list
    assert "sessionStorage.removeItem(KEY)" in keep and 'addEventListener("pagehide"' in keep


def test_the_card_the_chats_an_anchor_and_a_field_error_keep_their_own_ways():
    keep = place()
    assert 'document.querySelector("[data-keep-place], [data-no-keep-place], .chats-list")' in keep           # 13c, «Чаты»: their own place
    assert '(window.location.hash && !reload) || document.querySelector("[aria-invalid=true]")' in keep       # the anchor, the field (5a)
    assert keep.count("try {") == 3 and keep.count("catch (e)") == 3                                          # no storage: the page as before
    assert "scrollIntoView" not in keep and "location.hash =" not in keep and "replaceState" not in keep
    assert JS.index("13c) Карточка заказа") < JS.index("13d) Списки и разделы")
    assert JS.index('field.scrollIntoView({block: "center"});') < JS.index("13d) Списки и разделы")         # the field's error is marked first


def test_the_answer_of_a_press_is_seen_where_the_page_stays():
    keep = place()
    assert '.page > .msgs > .flash' in keep and "pageToast(note.textContent.trim()" in keep          # the toast of 10), not a copy
    assert "pageToast = toast;" in JS and JS.index("pageToast = toast;") < JS.index("13d) Списки и разделы")


@pytest.mark.parametrize("view", ["old", "new"])
def test_the_list_pages_take_the_common_place(tmp_path, view):
    app, owner, tok, conn = panel(tmp_path)
    store.set_state(conn, "ui_new.office", "all" if view == "new" else "off")
    conn.commit()
    for url in ("/orders", "/orders?v=list", "/clients", "/oth", "/catalog", "/orders/report"):
        r = owner.get(url)
        assert r.status_code == 200, url
        html = r.get_data(as_text=True)
        assert "app.js" in html and "data-keep-place" not in html and "data-no-keep-place" not in html, url
        assert re.search(r'<div class="page[^"]*">\s*<div class="msgs"', html), url                       # where 13d reads the answer
