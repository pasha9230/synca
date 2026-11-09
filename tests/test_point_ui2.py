"""The owner 2026-11-09 (the screenshots of a point's «Рабочее место»): the stage and its number on the tab only (no line «На ОТХ 5» under
the tabs); the plaques with their day («на точке с 07.11», «на ОТХ с 31.10»); «На ОТХ»: no line «На ОТХ с … · срока продажи нет», each
position with «Кол-во: N шт.» and «Цена:» — the order's own price × the pieces, never Avito's nor the purchase; «Подробнее»: the client's
phone, «Номер перемещения: … от …», a block of each position (Товар, Код, Кол-во, Цена ед., Сумма) and «Резерв: Хранение шин до …»,
no line of what is not there. Both views of the point."""
from __future__ import annotations

import re

import pytest

from test_mypoint import manager
from test_point_new_view import flag
from test_vk_commands import SENT, _card, _stage, ready
from tires_sync import order_templates as tpl, orders
from tires_sync.panel import routes_mypoint

NBSP = " "
HERE_AT, OTH_AT = "2026-11-07T10:00:00", "2026-10-31T09:00:00"


@pytest.fixture(params=["old", "new"])
def view(request):
    return request.param


def setup(tmp_path, view):
    app, owner, tok, conn, pid, card = ready(tmp_path, SENT)                # card: «Перемещение на точку»
    if view == "new":
        flag(conn, "all")
    return app, conn, pid, card


def to_stage(conn, card, code, at):
    assert orders.move_card(conn, card, _stage(conn, code), at, "o")
    conn.commit()


def page(client, tab):
    return client.get("/my-point?tab=" + tab).get_data(as_text=True)


def article(page_text, card):
    return re.search(r'<article class="mp-card[^"]*" id="order-%d".*?</article>' % card, page_text, re.S).group(0)


def more(page_text, card):
    """«Подробнее» without the line of an order not from the point's own Avito account (test_point_label: kept as it was)."""
    return re.sub(r"<p [^>]*data-origin>.*?</p>", "", article(page_text, card).split('<details class="mp-more">', 1)[1], flags=re.S)


def text(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).replace(NBSP, " ").strip()


def money(n):
    return "{:,.0f}".format(n).replace(",", " ") + " ₽"


# --- 1. the stage once: on its tab ------------------------------------------------------------------------------------------------

def test_the_stage_and_its_number_are_on_the_tab_only(tmp_path, view):
    app, conn, pid, card = setup(tmp_path, view)
    here = _card(conn, pid, webmim="700100", code="at_point")
    oth = _card(conn, pid, webmim="700200", code="at_point")
    to_stage(conn, oth, "oth", OTH_AT)
    mine = manager(app, conn, pid)
    for tab, name in (("moving", "Едут"), ("here", "На точке"), ("oth", "На ОТХ"), ("issued", "Выдано за 30 дней")):
        shown = page(mine, tab)
        head = re.search(r'<h2 id="mp-orders-h"([^>]*)>(.*?)</h2>', shown, re.S)
        assert head.group(2) == name and re.search(r'class="(p2-sr|sr-only)"', head.group(1)), tab   # for a screen reader only
        assert re.search(r'data-tab-count="%s">1<' % tab, shown) or tab == "issued"                   # the number: on the tab
        lists = shown[shown.index('<div class="mp-list">'):]
        assert "p2-count" not in lists and "mp-count" not in lists, tab
    assert here and card


# --- 2. the plaques with their day ------------------------------------------------------------------------------------------------

def test_the_plaques_say_since_when_the_order_is_at_the_point_or_on_oth(tmp_path, view):
    app, conn, pid, card = setup(tmp_path, view)
    to_stage(conn, card, "at_point", HERE_AT)
    oth = _card(conn, pid, webmim="700200", code="at_point")
    to_stage(conn, oth, "oth", OTH_AT)
    going = _card(conn, pid, webmim="700300", code="moving")
    mine = manager(app, conn, pid)
    tag = lambda shown, c: text(re.search(r"<span [^>]*data-stage-tag>(.*?)</span>", article(shown, c), re.S).group(1))
    assert re.fullmatch(r"на точке с 07\.11(\.2026)?", tag(page(mine, "here"), card))
    assert re.fullmatch(r"на ОТХ с 31\.10(\.2026)?", tag(page(mine, "oth"), oth))
    assert tag(page(mine, "moving"), going) == "едет на точку"                                    # on the way: no day asked


# --- 3. «На ОТХ»: the positions with the order's price ----------------------------------------------------------------------------

def test_an_oth_card_has_each_position_its_pieces_and_the_orders_price_and_no_line_of_the_term(tmp_path, view):
    app, conn, pid, card = setup(tmp_path, view)
    oth = _card(conn, pid, webmim="700200", code="at_point")                    # Nokian × 4 at 5 000 (the purchase 4 000)
    orders.add_item(conn, oth, goods_id="2", name="Ikon Character Ice 8", brand_key="ikon", season="winter", diameter=15, territory="1", qty=2,
                    sale_unit=5800, supplier_price=4100, terms=None, fee_pct=0, promo_pct=0, now=HERE_AT, author="o")
    to_stage(conn, oth, "oth", OTH_AT)
    shown = page(manager(app, conn, pid), "oth")
    top = article(shown, oth).split('<details class="mp-more">')[0]
    assert "срока продажи нет" not in top and "На ОТХ с" not in top                               # the day is on the plaque
    assert top.count("data-oth-item") == 2                                                          # a block of each position
    assert ("Nokian Hakkapeliitta 10 Кол-во: 4 шт. Цена: %s Ikon Character Ice 8 Кол-во: 2 шт. Цена: %s" % (money(20000), money(11600))) in text(top)
    assert money(16000) not in text(top) and money(8200) not in text(top)                          # never the purchase
    assert "Татьяна" not in top and "Итого" not in top                                             # the client is gone (2026-10-30)


def test_an_oth_card_takes_the_price_of_the_orders_position(tmp_path, view):
    app, conn, pid, card = setup(tmp_path, view)
    oth = _card(conn, pid, webmim="700200", code="at_point")
    item = orders.get_card(conn, oth)["items"][0]["id"]
    orders.update_item(conn, item, 4, 5200, HERE_AT, "o")                                 # the price of the order's position, × its pieces
    to_stage(conn, oth, "oth", OTH_AT)
    top = text(article(page(manager(app, conn, pid), "oth"), oth))
    assert "Цена: %s" % money(20800) in top


# --- 4. «Подробнее» ---------------------------------------------------------------------------------------------------------------

def test_more_has_the_phone_the_transfer_each_position_and_the_reserve_and_nothing_else(tmp_path, view):
    app, conn, pid, card = setup(tmp_path, view)
    orders.update_card(conn, card, dict(transfer_number="26R-1552920"), HERE_AT, "o")
    orders.add_item(conn, card, goods_id="TS78036", name="Ikon Character Ice 8 XL", brand_key="ikon", season="winter", diameter=15, territory="1",
                    qty=4, sale_unit=5800, supplier_price=4100, terms=None, fee_pct=0, promo_pct=0, now=HERE_AT, author="o")
    to_stage(conn, card, "at_point", HERE_AT)
    orders.update_card(conn, card, {"reserve_until": "2026-11-11"}, HERE_AT, "o")
    conn.commit()
    shown = page(manager(app, conn, pid), "here")
    body = more(shown, card)
    day = routes_mypoint._order_day(conn, orders.get_card(conn, card))
    assert day and day == tpl.values(conn, orders.get_card(conn, card))["order_date"]     # the day of the point's message «оформлен на перемещение … от …»
    code = orders.get_card(conn, card)["items"][0]
    first = code.get("code") or code["goods_id"]
    assert text(body) == ("Подробнее Телефон клиента +7 900 111-22-33 Номер перемещения: 26R-1552920 от %s "
                          "Товар Nokian Hakkapeliitta 10 Код %s Кол-во 4 шт Цена ед. %s Сумма %s "
                          "Товар Ikon Character Ice 8 XL Код TS78036 Кол-во 4 шт Цена ед. %s Сумма %s "
                          "Резерв: Хранение шин до 11.11.2026") % (day, first, money(5000), money(20000), money(5800), money(23200))
    assert 'href="tel:+79001112233"' in body
    assert "Перемещение" not in text(body).replace("Номер перемещения", "")                     # no second transfer, no «Доставка»


def test_more_drops_the_lines_it_has_nothing_for(tmp_path, view):
    app, conn, pid, card = setup(tmp_path, view)                                        # on the way: no transfer number, no reserve yet
    oth = _card(conn, pid, webmim="700200", code="at_point")
    to_stage(conn, oth, "oth", OTH_AT)
    mine = manager(app, conn, pid)
    going = text(more(page(mine, "moving"), card))
    assert "Номер перемещения" not in going and "Перемещение: ещё нет" not in going and "Резерв" not in going
    assert going.startswith("Подробнее Телефон клиента") and "Товар Nokian Hakkapeliitta 10" in going
    on_oth = text(more(page(mine, "oth"), oth))
    assert "Телефон" not in on_oth and "+7 900" not in on_oth and "Резерв" not in on_oth          # «На ОТХ»: the client is gone
    assert on_oth == "Подробнее Товар Nokian Hakkapeliitta 10 Код 1 Кол-во 4 шт Цена ед. %s Сумма %s" % (money(5000), money(20000))


def test_point_lines_skip_a_position_given_on_and_a_tyre_of_the_point_has_no_code():
    card = {"items": [{"name": "A", "goods_id": "7", "code": "C7", "qty": 2, "sale_unit": 1000},
                      {"name": "B", "goods_id": "8", "code": "", "qty": 0, "sale_unit": 900},
                      {"name": "C", "goods_id": orders.POINT_GOODS, "code": "", "qty": 1, "sale_unit": 0}]}
    assert routes_mypoint.point_lines(card) == [{"name": "A", "code": "C7", "qty": 2, "unit": 1000.0, "sum": 2000.0},
                                                {"name": "C", "code": "", "qty": 1, "unit": None, "sum": None}]
