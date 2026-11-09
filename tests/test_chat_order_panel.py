"""The order of a chat in a panel on the right (the owner 2026-11-04; office «Новый вид»): above the messages one line of the order, «Открыть заказ»
opens the panel with what the block held, the chat stays beside it. The old view, the point's view and the forms' routes do not change."""
from __future__ import annotations

from pathlib import Path

import tires_sync
from panel_helpers import post
from test_office_new_view import cheap_hashes, flag, person, w  # noqa: F401 (the fixtures)
from tires_sync import chat_deals as deals, orders, users
from test_own_price import NOW
from test_roles import sign_in

PANEL = Path(tires_sync.__file__).parent / "panel"


def chat_page(w, client=None):
    return (client or w["owner"]).get("/chats/%d" % w["chat"]).get_data(as_text=True)


def order_of(w):
    return w["conn"].execute("SELECT id FROM order_card WHERE avito_chat = ? ORDER BY id DESC", (deals.ref(w["chat"]),)).fetchone()[0]


def set_comment(w, text):
    w["conn"].execute("UPDATE order_card SET comment = ? WHERE id = ?", (text, order_of(w)))
    w["conn"].commit()


def add_position(w, sale_unit=15300):
    orders.add_item(w["conn"], order_of(w), goods_id="100", name="Ikon Test Model 205/55 R16", brand_key="IKON TYRES", season="winter", diameter=16,
                    territory="1", qty=4, sale_unit=sale_unit, supplier_price=5000, terms=None, fee_pct=0, promo_pct=0, now=NOW, author="o")
    w["conn"].commit()


def between(html, start, end):
    return html.split(start, 1)[1].split(end, 1)[0]


def test_off_the_old_block_stays_as_it_was(w):
    set_comment(w, "Объявление: Ikon https://www.avito.ru/x")
    for state in ("off", "owner_only"):
        flag(w, state)
        client = w["owner"] if state == "off" else person(w, "chat1")
        html = chat_page(w, client)
        assert "data-order-panel" not in html and "o2-order-line" not in html
        assert 'class="card chats-deal"' in html and "data-deal-fold" in html and "data-deal-unfold" in html
    flag(w, "off")
    assert "Объявление: Ikon https://www.avito.ru/x" in chat_page(w)            # the old block keeps the whole comment


def test_one_line_above_the_messages_and_the_order_in_the_panel(w):
    flag(w, "all")
    add_position(w)
    html = chat_page(w)
    oid = order_of(w)
    line = between(html, '<div class="o2-order-line">', "</div>")
    assert "<b>Заказ №%d</b> · Новый заказ · <b>61\xa0200\xa0₽</b>" % oid in line
    assert '<a class="btn btn--secondary btn--xs" href="/orders/%d" data-order-panel-open aria-controls="order-panel">Открыть заказ</a>' % oid in line
    head = between(html, 'class="o2-cl-sub">', "</div>")
    assert "Заказ №" not in head                                               # the line says it once
    above = html.split('class="chat-thread o2-cl-msgs"', 1)[0].split('<section class="chats-main', 1)[1]
    assert "chats-deal" not in above and "data-item-find" not in above and "Клиент" + " ждёт" not in above
    panel = html.split('id="order-panel"', 1)[1].split("</aside>", 1)[0]
    assert html.index('class="chat-composer') < html.index('id="order-panel"')  # the panel is no part of the column of messages
    for part in ("<dt>Клиент</dt>", "<dt>Телефон</dt>", "<dt>Точка</dt>", "<dt>Доставка</dt>", "<dt>Позиции</dt>", "<dt>Сумма</dt>", "data-item-find",
                 'action="/chats/%d/items/add"' % w["chat"], 'action="/chats/%d/wait"' % w["chat"], "Клиент ждёт эту шину", "Задачи",
                 'action="/chats/%d/stage"' % w["chat"], 'id="o2-stage"', 'href="/orders/%d">Открыть полностью' % oid, "data-order-panel-close"):
        assert part in panel, part
    assert "data-deal-fold" not in html and "data-deal-unfold" not in html
    menu = between(html, '<details class="menu o2-cl-more">', "</details>")
    assert "/stage" not in menu and "/assign" in menu                          # one selector of the stage: in the panel


def test_the_ad_line_is_compact_and_what_it_held_is_in_the_panel(w):
    flag(w, "all")
    html = chat_page(w)
    assert 'class="chat-product o2-cl-product"' in html
    ad = between(html, 'class="chat-product o2-cl-product">', "</div>")
    assert "Объявление:" in ad and "В каталоге" in ad and "Артикул" not in ad and "у поставщика" not in ad


def test_the_comment_of_the_panel_has_no_automatic_line(w):
    flag(w, "all")
    set_comment(w, "Объявление: Ikon 205/55 R16 https://www.avito.ru/x. Перезвонить после обеда")
    panel = between(chat_page(w), 'id="order-panel"', "</aside>")
    assert '<dt>Комментарий</dt><dd class="small">Перезвонить после обеда</dd>' in panel and "https://www.avito.ru/x" not in panel
    set_comment(w, "Объявление: Ikon 205/55 R16 https://www.avito.ru/x")       # nothing of a person: no row at all
    assert "<dt>Комментарий</dt>" not in chat_page(w)
    set_comment(w, "Клиент сам приедет")
    assert '<dd class="small">Клиент сам приедет</dd>' in chat_page(w)


def test_a_position_added_from_the_panel_lands_in_the_order_and_the_line(w):
    flag(w, "all")
    oid = order_of(w)
    form = between(chat_page(w), '<form id="ci-add"', "</form>")
    assert 'action="/chats/%d/items/add"' % w["chat"] in form and 'name="order_id" value="%d"' % oid in form
    r = post(w["owner"], "/chats/%d/items/add" % w["chat"], w["tok"], order_id=str(oid), goods_id="100", query="Ikon", qty="2", sale_unit="5000", f="", acc="", q="")
    assert r.status_code == 302 and r.headers["Location"].endswith("/chats/%d" % w["chat"])
    html = chat_page(w)
    assert "× 2" in between(html, 'id="order-panel"', "</aside>") and "<b>10\xa0000\xa0₽</b>" in between(html, '<div class="o2-order-line">', "</div>")


def test_a_chat_without_an_order_creates_it_and_the_panel_opens(w):
    flag(w, "all")
    assert post(w["owner"], "/chats/%d/unlink" % w["chat"], w["tok"], f="", acc="", q="").status_code == 302
    html = chat_page(w)
    line = between(html, '<div class="o2-order-line">', "</div>")
    assert "Без заказа" in line and "Панель заказа" in line and "data-order-panel-open" in line
    assert 'action="/chats/%d/create-deal" data-order-panel-after' % w["chat"] in line and ">Создать заказ<" in line
    panel = between(html, 'id="order-panel"', "</aside>")
    assert "Чат не привязан к заказу" in panel and 'action="/chats/%d/link"' % w["chat"] in panel and "Клиент ждёт эту шину" in panel
    assert post(w["owner"], "/chats/%d/create-deal" % w["chat"], w["tok"], f="", acc="", q="").status_code == 302
    assert "Заказ №%d" % order_of(w) in between(chat_page(w), '<div class="o2-order-line">', "</div>")


def test_rights_cut_the_forms_of_the_panel_not_the_view(w):
    flag(w, "all")
    users.add_user(w["conn"], "look1", "x" * 12, NOW, role="viewer")
    w["conn"].commit()
    viewer = w["app"].test_client()
    sign_in(viewer, w["conn"], "look1")
    html = chat_page(w, viewer)
    assert "Открыть заказ" in between(html, '<div class="o2-order-line">', "</div>")
    panel = between(html, 'id="order-panel"', "</aside>")
    assert "<dt>Клиент</dt>" in panel and "/items/add" not in panel and "/stage" not in panel and "/wait" not in panel
    assert '<span class="badge badge--brand">Новый заказ</span>' in panel


def test_a_point_never_gets_the_panel(w):
    flag(w, "all")
    w["conn"].execute("UPDATE point SET avito_account_id = ? WHERE id = ?", (w["accounts"][0], w["pid"]))         # the chat is of the point's account: it opens
    w["conn"].commit()
    r = person(w, "pt1").get("/chats/%d" % w["chat"])
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and 'class="chat-thread' in html
    assert "data-order-panel" not in html and "o2-order-line" not in html and "office2-pages.css" not in html


def test_the_script_and_the_style_keep_their_contract():
    js = (PANEL / "static" / "app.js").read_text(encoding="utf-8")
    block = js.split("«Чаты» of the new view: the order in a panel", 1)[1].split("// 11) Заказ: поиск позиции", 1)[0]
    assert 'MARK = "#order"' in block and "history.pushState" in block and "history.back()" in block and 'addEventListener("popstate", sync)' in block
    assert '".modal-backdrop.is-open, details.menu[open], details.msel-box[open], #bell-pop:not([hidden])"' in block and 'closest(".topbar")' in block and '.suggest:not([hidden])' in block            # Esc: what is on top closes first
    assert "form && (isOpen() ||" in block                                                                            # a reply to the client keeps the panel open
    assert "if (live && !held)" in js                                                                                 # held back: the next poll asks again
    assert "sessionStorage" in block and "localStorage" not in block                                                  # no state is kept beyond the tab
    assert '[".chats-main .card-head", ".o2-order-line", ".chats-deal"]' in js                                        # the live refill follows the line
    assert 'mine.closest("[data-order-panel]") && mine.querySelector("details[open], .o2-toggle:checked")' in js      # and leaves what is half typed in the panel
    css = (PANEL / "static" / "office2-pages.css").read_text(encoding="utf-8")
    assert ".o2 .chats-layout.is-deal-folded .o2-cl-panel{display:flex}" in css    # a «Свернуть» remembered from the old view never hides the panel
    assert "z-index:25;" in css.split(".o2-order-panel{position:fixed", 1)[1].split("}", 1)[0]    # under the top bar's bell and search (30), under the windows (50)
    assert ".o2-panel-open .o2-cl-list{display:none}" in css                       # narrow screens: the chat beside the panel keeps the room
    assert ".o2-order-panel{top:0;left:0;width:auto;" in css                       # the phone: the whole screen


def test_a_chat_without_an_order_keeps_the_code_and_the_stock_of_the_ad_in_the_panel():
    html = (PANEL / "templates" / "office2" / "chats.html").read_text(encoding="utf-8")
    branches = html.split('<h3 id="order-panel-h">')
    assert "{% if current.product %}" in branches[1] and "{% if current.product %}" in branches[2]        # with an order and without
