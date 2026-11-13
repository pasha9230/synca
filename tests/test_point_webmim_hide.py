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

from panel_helpers import assert_card_closed_to_point, login, post
from test_mypoint import manager
from test_point_orders import w as orders_world  # noqa: F401  (the fixture of the point's «Заказы»)
from test_vk_commands import _card, ready
from tires_sync import orders, store

NUMBER, WID, STATUS, TRANSFER = "5284060", "W-777", "Отгружен-статус", "ПМ-55"
WORDS = re.compile(r"WebMIM|\bMIM\b|\bМИМ\b")          # as a person reads them; the lower-case codes of the forms (value="webmim") are not shown


def visible(page):
    """The page as a person reads it: no scripts (the shared app.js names its endpoints), no HTML comments."""
    return re.sub(r"<script.*?</script>|<!--.*?-->", "", page, flags=re.S)


def assert_no_webmim(page, where, numbers=True, named=False):
    """`named`: the order's window of a point names the order «№ <WebMIM number>» (the owner 08.11: «Точка: заказ виден по № MIM, без
    внутреннего номера») — that name only; no word «WebMIM», no id, no status, no number anywhere else."""
    text = visible(page)
    if named:
        assert "№ %s" % NUMBER in text, where
        text = text.replace("№ %s" % NUMBER, "№ —")
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
    return dict(app=app, office=office, tok=tok, conn=conn, pid=pid, here=here, moving=moving, point=manager(app, conn, pid), view=request.param)


def test_the_order_card_the_point_opens_has_no_webmim_but_keeps_the_delivery(world):
    """The owner 13.11: a point opens no card of our order — the card of it is a redirect to «Рабочее место» searching the order (its WebMIM
    number, the number the point knows the order by), with no data of the order in the answer; the page it lands on has no WebMIM word, ID or
    status (the delivery of the order is the cards' there)."""
    for url in ("/orders/%d" % world["here"], "/orders/%d?tab=webmim" % world["here"]):
        response = world["point"].get(url)
        assert response.status_code == 302, url
        assert response.headers["Location"] == "/my-point?q=%s" % NUMBER, url
        body = response.get_data(as_text=True)
        assert 'name="delivery_date"' not in body and "Доставка" not in body and TRANSFER not in body, url     # not a card
        assert not WORDS.search(visible(body)) and WID not in body and STATUS not in body, url
    page = world["point"].get("/my-point?q=%s" % NUMBER).get_data(as_text=True)
    assert_no_webmim(page, "my-point?q=", numbers=False)


def test_the_office_still_sees_webmim_in_the_same_card(world):
    page = world["office"].get("/orders/%d" % world["here"]).get_data(as_text=True)
    assert "Доставка и WebMIM" in page and "Номер заказа WebMIM" in page and "ID заказа WebMIM" in page and "Статус WebMIM" in page
    assert NUMBER in page and WID in page and STATUS in page
    assert "Номер заказа WebMIM: %s" % NUMBER in page                             # the journal record


def test_working_as_a_point_hides_webmim_too(world):
    owner = world["app"].test_client()
    login(owner)
    assert post(owner, "/as-point", "tok", point_id=str(world["pid"])).status_code == 302
    assert_card_closed_to_point(owner, world["here"], query=NUMBER, hidden=(WID, STATUS, TRANSFER))   # the owner 13.11: working as a point, no card of our order
    assert_no_webmim(owner.get("/my-point?q=%s" % NUMBER).get_data(as_text=True), "as point", numbers=False)


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
                        ("/orders/%d/webmim/resolve" % cid, {"found": "1", "webmim_id": "1"}), ("/orders/%d/webmim/cabinet" % cid, {}),
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


def test_an_alert_about_webmim_is_not_the_points_and_opening_the_order_still_reads_the_journal(world):
    """Review: the journal hides the records of WebMIM from the point; the alerts must not show them either. Since the owner 13.11 the point
    opens no card of our order (redirect to «Рабочее место»), so the card does not read the journal for it; the office still has the record."""
    conn, m, cid = world["conn"], world["point"], world["here"]
    orders.add_event(conn, cid, "2026-10-13T12:05:00", "система", "webmim", "Создан заказ WebMIM № %s" % NUMBER, level="green")
    conn.commit()
    assert_no_webmim(m.get("/alerts").get_data(as_text=True), "alerts")
    feed = m.get("/alerts/feed").get_json()
    assert not any(WORDS.search(str(i.get("text") or "")) for i in feed["items"]), feed
    assert_card_closed_to_point(m, cid, query=NUMBER, hidden=("Создан заказ WebMIM",))   # the owner 13.11: the point opens no card of our order any more
    office = world["office"]
    assert "Создан заказ WebMIM" in office.get("/orders/%d" % cid).get_data(as_text=True)     # the office still has it


def test_the_empty_search_hint_has_no_webmim_for_the_point(world):
    assert_no_webmim(world["point"].get("/search").get_data(as_text=True), "search", numbers=False)
    assert "WebMIM" in world["office"].get("/search").get_data(as_text=True)


def test_a_client_s_words_with_mimo_stay_the_point_s(world):
    """Review: only the whole words WebMIM / MIM / МИМ are hidden; «проеду мимо», «Мимоза» of a client stay in the journal and the alerts."""
    from tires_sync.panel.routes_orders import point_no_webmim
    conn, cid = world["conn"], world["here"]
    orders.add_event(conn, cid, "2026-10-13T12:06:00", "o", "field", "Комментарий: проеду мимо в 5, Мимоза")
    conn.commit()
    assert_card_closed_to_point(world["point"], cid, query=NUMBER)             # the owner 13.11: no card of our order for the point
    assert "проеду мимо в 5" in world["office"].get("/orders/%d" % cid).get_data(as_text=True)     # the office reads the journal with the client's words
    for text in ("Клиент написал: проеду мимо в 5", "Мимоза", "пантомима"):
        assert not point_no_webmim({"kind": "chat", "text": text}), text
    for text in ("Создан заказ WebMIM № 1", "№ MIM 1", "Заказать в МИМ"):
        assert point_no_webmim({"kind": "field", "text": text}), text
    assert point_no_webmim({"kind": "webmim", "text": "что угодно"})


def test_a_merged_order_names_its_sources_by_the_point_s_order_number_without_webmim(world):
    """A merged order names its sources by the number the point knows them by (order_label: «Рабочее место», VK, the journal's «[№…]»),
    the same everywhere in the card; no word WebMIM, no ID, no status (review 2026-11-07: one number per order in one card). Since the owner 13.11 the point opens no card of our order: «Рабочее место» names them."""
    from tires_sync import order_merge
    conn, pid = world["conn"], world["pid"]
    second = _card(conn, pid, webmim="5284061", code="at_point")
    stage = next(s["id"] for s in orders.list_stages(conn) if s["code"] == "at_point")
    merged = order_merge.merge(conn, [world["moving"], world["here"], second], stage, "2026-10-13T12:30:00", "o", client_from=world["here"])
    conn.commit()
    assert_card_closed_to_point(world["point"], merged, hidden=(WID, STATUS))   # the owner 13.11: no card of our order for the point
    page = visible(world["point"].get("/my-point?q=%d" % merged).get_data(as_text=True))     # «Рабочее место» names the sources the same way
    assert "Объединён из" in page
    found = WORDS.search(page)
    assert not found, page[max(0, found.start() - 120):found.end() + 120]
    assert WID not in page and STATUS not in page
    banner = page[page.index("Объединён из"):]
    banner = banner[:banner.index("</div>")]
    assert "№%s" % NUMBER in banner and "№5284061" in banner                     # the point's order numbers, as its journal names them
