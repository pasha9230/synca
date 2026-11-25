"""ТЗ №89 и №90 (the owner 2026-11-25).

№89 «Карточка заказа едина независимо откуда в неё попадаешь»: the card is the one page /orders/<id>, and what it shows follows from the order
(its stage, its chat, its record of storage) and the person's role, never from where the person came from — the chat's «Открыть полностью»,
the list, CRM, «На ОТХ», a new order made by hand or «Новый заказ этому клиенту». «Новый вид» lost the block of a record of storage
(«Вернуть заказ клиенту»): it is there again, as in the old card.

№90 «Бесшовное обновление»: a filter, a tab, a button or F5 that load the same page again keep its place (app.js 13d), as the card does
(13c, ТЗ №87). The browser is not run here: the script is read as text, the pages through the app."""
from __future__ import annotations

import re

import pytest

from panel_helpers import post
from test_order_chat_window import page, window
from test_oth_sale import panel
from test_oth_storage import MOVED, at_point, record
from test_order_reserve import stage
from tires_sync import orders, store

OTH_SPLIT = True
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
