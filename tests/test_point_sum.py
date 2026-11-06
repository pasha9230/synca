"""«Итого N ₽» of an order on the point's own screens (the owner 2026-11-07): the cards of «Моя точка» (old view and «Новый вид») and the
point's «Заказы» on a phone. The number is the order window's «Итого» (orders.totals, the sale); «На ОТХ» and «На возврат» have none (the
client is gone), an order with no positions has none; the purchase and the profit never reach the point."""
from __future__ import annotations

import re

import pytest

from test_mypoint import manager
from test_point_orders import w as orders_world  # noqa: F401  (the fixture of the point's «Заказы»)
from test_vk_commands import _card, ready
from tires_sync import orders, store

NOW = "2026-10-13T12:00:00"
SALE, COST, MARGIN = "20\u00a0000\u00a0₽", "16\u00a0000", "4\u00a0000"      # 4 × 5000, 4 × 4000, the difference (test_vk_commands._card)


def item(conn, cid, sale=5000, purchase=4000, goods="1"):
    orders.add_item(conn, cid, goods_id=goods, name="Nokian Hakka 10", brand_key="nokian", season="winter", diameter=16, territory="1", qty=4,
                    sale_unit=sale, supplier_price=purchase, terms=None, fee_pct=0, promo_pct=0, now=NOW, author="o")
    conn.commit()


def window_sum(client, cid):
    text = client.get("/orders/%d" % cid).get_data(as_text=True)
    found = re.search(r"Итого <b data-order-sum>([^<]+)</b>", text)
    assert found, cid
    return found.group(1)


def block(page, start, cid):
    at = page.index(start % cid)
    return page[at:page.find("</article>", at) if "</article>" in page[at:] else len(page)]


def no_office_money(page):
    plain = page.replace(" ", " ")
    assert COST.replace(" ", " ") not in plain and MARGIN.replace(" ", " ") not in plain
    for word in ("Закупка", "Прибыль", "Маржа", "Себестоимость"):
        assert word not in page


@pytest.fixture(params=["old", "new"])
def point(tmp_path, request):
    app, client, tok, conn, pid, moving = ready(tmp_path)
    here = _card(conn, pid, webmim="700202", code="at_point")
    empty = _card(conn, pid, webmim="700203", code="at_point")
    conn.execute("DELETE FROM order_item WHERE order_id = ?", (empty,))
    oth = _card(conn, pid, webmim="700204", code="at_point")
    orders.move_card(conn, oth, next(s["id"] for s in orders.list_stages(conn) if s["code"] == "oth"), NOW, "o")
    store.set_state(conn, "ui_new.point", "all" if request.param == "new" else "off")
    conn.commit()
    return dict(conn=conn, office=client, mine=manager(app, conn, pid), moving=moving, here=here, empty=empty, oth=oth, view=request.param)


def test_the_point_card_shows_the_sum_of_the_order_window(point):
    pages = {t: point["mine"].get("/my-point?tab=%s" % t).get_data(as_text=True) for t in ("here", "moving", "oth")}
    assert ("point2.css" in pages["here"]) == (point["view"] == "new")
    for tab, cid in (("here", point["here"]), ("moving", point["moving"])):
        card = block(pages[tab], 'data-order="%d"', cid)
        shown = re.search(r"data-point-sum>Итого <b>([^<]+)</b>", card)
        assert shown and shown.group(1) == SALE, (tab, card)
        assert shown.group(1) == window_sum(point["office"], cid)          # the one number of the order window
        assert card.index("data-tyres") < card.index("data-point-sum")     # under the tyres and the client
        assert card.index("mp-name") < card.index("data-point-sum")


def test_no_sum_on_oth_nor_without_positions_and_no_office_money(point):
    pages = {t: point["mine"].get("/my-point?tab=%s" % t).get_data(as_text=True) for t in ("here", "moving", "oth")}
    assert "data-point-sum" not in block(pages["here"], 'data-order="%d"', point["empty"])
    assert 'data-order="%d"' % point["oth"] in pages["oth"]
    assert "data-point-sum" not in pages["oth"]
    for page in pages.values():
        no_office_money(page)


def test_the_point_phone_orders_show_the_sum_of_the_order_window(orders_world):
    w, m = orders_world, orders_world["made"]
    for cid in (m["stock_new"], m["stock_at"], m["stock_oth"]):          # its own sales: the point's own tyres
        item(w["conn"], cid, goods=orders.POINT_GOODS)
    seen = {}
    for ost in ("new", "placed", "done", "cancel"):
        page = w["point"].get("/orders?ost=%s" % ost).get_data(as_text=True)
        no_office_money(page)
        phone = page[page.index("data-point-orders"):page.index("pt-desk")]
        for cid in re.findall(r'data-pcard="(\d+)"', phone):
            card = phone[phone.index('data-pcard="%s"' % cid):]
            card = card[:card.index("</a>")]
            shown = re.search(r"data-point-sum>Итого <b>([^<]+)</b>", card)
            seen[int(cid)] = shown.group(1) if shown else None
            if shown:
                assert card.index("mp-tyres") < card.index("data-point-sum")
    assert seen[m["stock_new"]] == seen[m["stock_at"]] == SALE
    assert seen[m["stock_new"]] == window_sum(w["point"], m["stock_new"])     # the one number of the order window
    assert seen[m["stock_talk"]] is None                                   # no positions: no line
    assert m["stock_oth"] in seen and seen[m["stock_oth"]] is None         # «На ОТХ», folded into a column: the client is gone, no sum
