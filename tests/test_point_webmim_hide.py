"""WebMIM не показывается менеджеру точки (решение владельца 30.10, lane point-webmim-hide): в окне и карточке заказа, которые открывает
точка (старый и «Новый вид», телефон, «Работать как точка»), нет слов WebMIM / MIM / МИМ, номера, ID, статуса заказа WebMIM и даты счёта;
доставка (дата, точка, номер перемещения) остаётся — точке она нужна, чтобы принять и выдать шины. На доске и в «Списке» точки нет
названия «№ MIM» у заказа на ОТХ и колонок MIM, в выгрузке Excel нет колонок WebMIM, в журнале заказа нет записей про WebMIM, в подсказках
«Рабочего места» нет слова WebMIM. Маршруты и формы WebMIM точке закрыты (403), её форма своей продажи номер WebMIM не пишет. Офис видит всё
как раньше. Номер заказа на «Рабочем месте» («№ …») остаётся: это номер заказа для точки (message_texts order_label, им пользуются команды
ВК), подписи WebMIM у него нет."""
from __future__ import annotations

import io
import re

import pytest

from panel_helpers import login, post
from test_mypoint import manager
from test_point_orders import w as orders_world  # noqa: F401  (the fixture of the point's «Заказы»)
from test_vk_commands import _card, ready
from tires_sync import orders, store

NUMBER, WID, STATUS, TRANSFER = "5284060", "W-777", "Отгружен-статус", "ПМ-55"
WORDS = re.compile(r"WebMIM|\bMIM\b|МИМ")          # as a person reads them; the lower-case codes of the forms (value="webmim") are not shown


def visible(page):
    """The page as a person reads it: no scripts (the shared app.js names its endpoints), no HTML comments."""
    return re.sub(r"<script.*?</script>|<!--.*?-->", "", page, flags=re.S)


def assert_no_webmim(page, where, numbers=True):
    text = visible(page)
    found = WORDS.search(text)
    assert not found, (where, text[max(0, found.start() - 120):found.end() + 120])
    if numbers:
        for value in (NUMBER, WID, STATUS):
            assert value not in text, (where, value)


@pytest.fixture(params=["old", "new"])
def world(tmp_path, request):
    app, office, tok, conn, pid, moving = ready(tmp_path)
    here = _card(conn, pid, webmim=NUMBER, code="at_point")
    conn.execute("UPDATE order_card SET webmim_id = ?, webmim_status = ?, transfer_number = ? WHERE id = ?", (WID, STATUS, TRANSFER, here))
    orders.add_event(conn, here, "2026-10-13T12:00:00", "o", "field", "Номер заказа WebMIM: %s" % NUMBER)
    store.set_state(conn, "ui_new.point", "all" if request.param == "new" else "off")
    conn.commit()
    return dict(app=app, office=office, tok=tok, conn=conn, pid=pid, here=here, point=manager(app, conn, pid), view=request.param)


def test_the_order_card_the_point_opens_has_no_webmim_but_keeps_the_delivery(world):
    for url in ("/orders/%d" % world["here"], "/orders/%d?tab=webmim" % world["here"]):
        response = world["point"].get(url)
        assert response.status_code == 200, url
        page = response.get_data(as_text=True)
        assert_no_webmim(page, url)
        assert 'name="delivery_date"' in page and "2026-10-15" in page            # the delivery stays
        assert TRANSFER in page                                                    # the transfer: the point receives the tyres by it
        assert "Доставка" in page


def test_the_office_still_sees_webmim_in_the_same_card(world):
    page = world["office"].get("/orders/%d" % world["here"]).get_data(as_text=True)
    assert "Доставка и WebMIM" in page and "Номер заказа WebMIM" in page and "ID заказа WebMIM" in page and "Статус WebMIM" in page
    assert NUMBER in page and WID in page and STATUS in page
    assert "Номер заказа WebMIM: %s" % NUMBER in page                             # the journal record


def test_working_as_a_point_hides_webmim_too(world):
    owner = world["app"].test_client()
    login(owner)
    assert post(owner, "/as-point", "tok", point_id=str(world["pid"])).status_code == 302
    page = owner.get("/orders/%d" % world["here"]).get_data(as_text=True)
    assert_no_webmim(page, "as point")


def test_the_workplace_has_the_order_number_but_no_word_webmim(world):
    for tab in ("here", "moving", "oth"):
        page = world["point"].get("/my-point?tab=%s" % tab).get_data(as_text=True)
        assert_no_webmim(page, tab, numbers=False)
    assert "№ %s" % NUMBER in world["point"].get("/my-point?tab=here").get_data(as_text=True)    # the order's number for the point (order_label)


def test_the_webmim_routes_are_closed_to_the_point(world):
    m, cid = world["point"], world["here"]
    assert m.get("/orders/webmim").status_code == 403
    item = world["conn"].execute("SELECT id FROM order_item WHERE order_id = ?", (cid,)).fetchone()[0]
    for url, fields in (("/orders/%d/webmim/link" % cid, {"order": "1"}), ("/orders/%d/webmim/create" % cid, {}),
                        ("/orders/%d/webmim/resolve" % cid, {"found": "1", "webmim_id": "1"}),
                        ("/orders/%d/items/%d/mim" % (cid, item), {"webmim_number": "1"}),
                        ("/orders/%d/items/%d/mim-extra" % (cid, item), {"webmim_number": "1"})):
        assert post(m, url, "tok", **fields).status_code in (403, 404, 405), url
    assert orders.get_card(world["conn"], cid)["webmim_number"] == NUMBER


@pytest.mark.parametrize("view", ["old", "new"])
def test_the_point_board_list_and_file_have_no_webmim(orders_world, view):
    w, m = orders_world, orders_world["made"]
    store.set_state(w["conn"], "ui_new.point", "all" if view == "new" else "off")
    w["conn"].commit()
    for url in ("/orders?v=board", "/orders?v=list", "/orders?ost=cancel"):
        page = w["point"].get(url).get_data(as_text=True)
        assert_no_webmim(page, url, numbers=False)
    assert 'data-card="%d"' % m["stock_oth"] in w["point"].get("/orders?v=board").get_data(as_text=True)      # the order on ОТХ is still there
    response = w["point"].get("/orders/export.xlsx")
    if response.status_code == 200 and response.mimetype.endswith("sheet"):
        from openpyxl import load_workbook
        head = [c.value for c in next(load_workbook(io.BytesIO(response.data)).active.iter_rows(max_row=1))]
        assert not any(WORDS.search(str(h or "")) for h in head), head
    office = w["app"].test_client()
    login(office)
    assert "№ заказа MIM" in office.get("/orders?v=list").get_data(as_text=True)


def test_the_point_s_own_sale_form_never_writes_webmim(orders_world):
    w, m = orders_world, orders_world["made"]
    cid = m["stock_new"]
    post(w["point"], "/orders/%d/point-sale" % cid, "tok", client_name="Свой новый", webmim_number="999", webmim_id="888", webmim_status="x")
    card = orders.get_card(w["conn"], cid)
    assert not card["webmim_number"] and not card["webmim_id"] and not card["webmim_status"]
