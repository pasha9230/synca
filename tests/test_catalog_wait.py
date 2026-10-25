"""«В лист ожидания» from the catalog (the owner 2026-10-24): the window with the client's name and phone, the size and the model of the
position; the order is made by the code of the waiting list (order_wait), right in «Ожидает наличие»; the rights of a new order."""
from __future__ import annotations

import re
import sqlite3

import pytest

from helpers import catalog_xml, commodity_xml, make_offer
from panel_helpers import login, make_app, post
from tests.test_roles import cheap_hashes, person  # noqa: F401  (the fixture makes the hashes of the test users cheap)
from tires_sync import catalog_archive, order_points as points, order_wait, orders, store
from tires_sync.webmim import ParseResult

ROWS = (
    commodity_xml(NNOMMODIF="1", SMODIFNAME="IKON 205/55/16 T 94 Test Model", SMODIFNAME_ORIG="IKON 205/55/16 T 94 Test Model", NREST="2",
                  NPRICE_DELAYED="5000", SMNFCODE="C1"),
    commodity_xml(NNOMMODIF="2", SSEASON="", SMODIFNAME="IKON 215/60/17 T 94 Other", SMODIFNAME_ORIG="IKON 215/60/17 T 94 Other", SMODEL="Other",
                  SWIDTH="215", SHEIGHT="60", SDIAMETR="17", NREST="0", NPRICE_DELAYED="7000", SMNFCODE="C2"),
)
NAME, PHONE = "Пётр", "+7 900 555-44-33"


def owner_app(tmp_path):
    app = make_app(tmp_path)
    open(app.config["UNLOAD_PATH"], "wb").write(catalog_xml(*ROWS))
    client = app.test_client()
    tok = login(client)
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, "2026-10-25T10:00:00")
    return app, client, tok, conn


def cards(conn):
    return [r[0] for r in conn.execute("SELECT id FROM order_card ORDER BY id")]


def send(client, tok, goods_id="1", **fields):
    data = dict(goods_id=goods_id, next="/catalog?q=ikon", client_name=NAME, client_phone=PHONE, qty="4")
    data.update(fields)
    return post(client, "/catalog/wait", tok, **data)


def test_every_position_has_the_button_and_it_opens_the_window_of_the_position(tmp_path):
    app, client, tok, conn = owner_app(tmp_path)
    html = client.get("/catalog?q=ikon").get_data(as_text=True)
    assert 'href="/catalog?q=ikon&amp;wait=1"' in html and "В лист ожидания" in html and 'id="wait-window"' not in html
    html = client.get("/catalog?q=ikon&wait=1").get_data(as_text=True)
    window = html[html.index('id="wait-window"'):]
    assert "modal-backdrop is-open" in html and 'action="/catalog/wait"' in window
    assert "205/55 R16" in window and "Test Model" in window and "зима" in window
    assert 'name="client_name"' in window and 'name="client_phone"' in window and 'name="point_id"' in window and 'name="comment"' in window
    assert 'name="next" value="/catalog?q=ikon"' in window                       # the window goes back to the page without itself
    assert "Сейчас в наличии 2 шт." in window
    assert 'name="season"' not in window                                          # the position has its season
    other = client.get("/catalog?wait=2").get_data(as_text=True)
    assert 'name="season" required' in other and "Сейчас в наличии" not in other
    table = client.get("/catalog/table?q=ikon&wait=1").get_data(as_text=True)    # the table keeps its links without the window
    assert 'href="/catalog?q=ikon&amp;wait=1"' in table and "wait=1&amp;wait" not in table


def test_the_window_makes_an_order_in_the_waiting_stage_with_the_line_of_the_position_and_the_journal(tmp_path):
    app, client, tok, conn = owner_app(tmp_path)
    point = points.save_point(conn, dict(name="Бета", manager_name="Алексей", channel="vk"))
    orders.set_default_responsible(conn, "olya")                                  # the responsible of the orders the waiting list leaves tasks to
    r = send(client, tok, point_id=str(point), comment="перезвонить вечером", qty="2", any_size="1")
    assert r.status_code == 302 and r.headers["Location"].endswith("/catalog?q=ikon")
    [cid] = cards(conn)
    card = orders.get_card(conn, cid)
    assert card["stage"]["code"] == "wait" and card["client_name"] == NAME and card["client_phone"] == PHONE
    assert card["point_id"] == point and card["comment"] == "перезвонить вечером" and card["items"] == []
    assert card["responsible"] == "olya" and card["created_by"] == "owner"
    [line] = card["wants"]
    assert (line["brand"], line["model"], line["width"], line["profile"], line["radius"], line["season"], line["studded"], line["qty"]) == (
        "IKON TYRES", "Test Model", 205, 55, 16, "winter", "no", 2)
    assert line["any_size"] and not line["any_brand"] and line["code"] == ""
    texts = [e["text"] for e in orders.list_events(conn, cid)]
    assert "Заказ создан на этапе «Ожидает наличие»" in texts
    assert any(t.startswith("Что нужно клиенту: IKON TYRES Test Model 205/55 R16") for t in texts)
    assert "Из каталога: IKON 205/55/16 T 94 Test Model (код C1)" in texts
    assert "Клиент добавлен в лист ожидания: заказ №%d" % cid in client.get("/catalog?q=ikon").get_data(as_text=True)


@pytest.mark.parametrize("fields, error", [
    ({"client_name": ""}, "Укажите имя клиента."),
    ({"client_phone": ""}, "Укажите телефон клиента."),
    ({"client_phone": "12345"}, "Телефон клиента: от 10 до 15 цифр."),
    ({"qty": "0"}, "Количество: число от 1 до 100."),
    ({"qty": "101"}, "Количество: число от 1 до 100."),
    ({"point_id": "x"}, "Точка выбрана неверно."),
])
def test_a_wrong_window_makes_nothing_and_comes_back_with_what_was_typed(tmp_path, fields, error):
    app, client, tok, conn = owner_app(tmp_path)
    r = send(client, tok, comment="мой текст", **fields)
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and error in html and 'id="wait-window"' in html
    assert "мой текст" in html and 'name="next" value="/catalog?q=ikon"' in html
    if fields.get("client_name") != "":
        assert 'value="%s"' % NAME in html
    assert cards(conn) == [] and conn.execute("SELECT COUNT(*) FROM order_want").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM order_event").fetchone()[0] == 0


def test_a_position_without_a_season_needs_one_chosen_in_the_window(tmp_path):
    app, client, tok, conn = owner_app(tmp_path)
    html = send(client, tok, goods_id="2").get_data(as_text=True)
    assert "Выберите сезон: у этой позиции он не указан." in html and cards(conn) == []
    send(client, tok, goods_id="2", season="summer")
    [cid] = cards(conn)
    assert orders.get_card(conn, cid)["wants"][0]["season"] == "summer"


def test_the_same_client_and_tire_again_makes_no_second_order_but_another_tire_or_phone_does(tmp_path):
    app, client, tok, conn = owner_app(tmp_path)
    send(client, tok)
    send(client, tok, client_phone="8 (900) 555 44 33")                          # the same number typed otherwise
    assert len(cards(conn)) == 1
    assert "Клиент уже ждёт эту шину: заказ №%d" % cards(conn)[0] in client.get("/catalog").get_data(as_text=True)
    send(client, tok, comment="перезвонить")
    assert "точку и комментарий из окна допишите в этом заказе" in client.get("/catalog").get_data(as_text=True)
    send(client, tok, qty="2")                                                    # another line (two, not four): a new order
    send(client, tok, client_phone="+7 900 555-44-00")
    assert len(cards(conn)) == 3


def test_a_position_the_supplier_took_away_can_be_waited_for(tmp_path):
    app, client, tok, conn = owner_app(tmp_path)
    old = make_offer(goods_id="900", code="GONE1", name="IKON 175/70/13 T 82 Old Model", name_orig="IKON 175/70/13 T 82 Old Model",
                     model_raw="Old Model", width=175, height=70, diameter=13, stock=8, base_price=3300.0)
    catalog_archive.record(conn, ParseResult(offers={"900": old, "901": make_offer(goods_id="901", code="K")}), "2026-10-10T10:00:00")
    catalog_archive.record(conn, ParseResult(offers={"901": make_offer(goods_id="901", code="K")}), "2026-10-15T10:00:00")
    html = client.get("/catalog?st=gone").get_data(as_text=True)
    assert 'wait=900"' in html and "нет у поставщика" in html
    assert "Сейчас в наличии" not in client.get("/catalog?wait=900").get_data(as_text=True)
    send(client, tok, goods_id="900")
    [cid] = cards(conn)
    line = orders.get_card(conn, cid)["wants"][0]
    assert (line["width"], line["profile"], line["radius"], line["model"]) == (175, 70, 13, "Old Model")


def test_an_unknown_position_or_a_foreign_return_address_is_refused_quietly(tmp_path):
    app, client, tok, conn = owner_app(tmp_path)
    r = send(client, tok, goods_id="nope")
    assert r.status_code == 302 and cards(conn) == []
    assert "Позиции нет в каталоге" in client.get("/catalog").get_data(as_text=True)
    r = send(client, tok, next="https://evil.example/x")
    assert r.headers["Location"].endswith("/catalog")
    r = send(client, tok, next="/orders", client_phone="+7 900 555-44-01")
    assert r.headers["Location"].endswith("/catalog")


def test_the_rights_are_those_of_a_new_order(tmp_path):
    app, client, tok, conn = owner_app(tmp_path)
    viewer, _ = person(app, "viewer")
    html = viewer.get("/catalog").get_data(as_text=True)
    assert "В лист ожидания" not in html and 'id="wait-window"' not in viewer.get("/catalog?wait=1").get_data(as_text=True)
    assert send(viewer, "tok").status_code == 403
    manager, _ = person(app, "chats")                                             # «Заказы: менять», the catalog to look at
    assert "В лист ожидания" in manager.get("/catalog").get_data(as_text=True)
    assert send(manager, "tok").status_code == 302 and len(cards(conn)) == 1
    pid = points.save_point(conn, dict(name="Бета", manager_name="Алексей", channel="vk"))
    point, _ = person(app, "point", point_id=pid)
    assert send(point, "tok", client_phone="+7 900 555-44-02").status_code == 403 and len(cards(conn)) == 1


def test_a_line_that_fails_leaves_no_order_behind(tmp_path, monkeypatch):
    app, client, tok, conn = owner_app(tmp_path)

    def broken(*args, **kwargs):
        raise orders.OrderError("сбой записи строки")
    monkeypatch.setattr(order_wait, "add_want", broken)
    html = send(client, tok).get_data(as_text=True)
    assert "сбой записи строки" in html and cards(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM order_event").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM client").fetchone()[0] == 0           # the client the order joined is undone with it
    monkeypatch.undo()
    send(client, tok)                                                                 # the same window without the failure: the client is made
    assert len(cards(conn)) == 1 and conn.execute("SELECT COUNT(*) FROM client").fetchone()[0] == 1


def test_a_second_press_that_starts_while_the_first_writes_finds_its_order(tmp_path, monkeypatch):
    """Two tabs at once: the search for the waiting order runs inside a BEGIN IMMEDIATE (the write lock taken first), so the second press
    waits for the first and sees its order; a plain deferred BEGIN would not close the race."""
    app, client, tok, conn = owner_app(tmp_path)
    ran, seen = [], []
    real_search, real_wait = order_wait.waiting_card, order_wait.wait_from_catalog

    def search(c, phone, clean):
        seen.append((c.in_transaction, [s.strip().upper() for s in ran][-1:] == ["BEGIN IMMEDIATE"]))
        return real_search(c, phone, clean)

    def traced(c, *args, **kwargs):
        c.set_trace_callback(ran.append)                                            # every statement of the press, in order
        try:
            return real_wait(c, *args, **kwargs)
        finally:
            c.set_trace_callback(None)
    monkeypatch.setattr(order_wait, "waiting_card", search)
    monkeypatch.setattr(order_wait, "wait_from_catalog", traced)
    send(client, tok)
    send(client, tok)
    assert seen == [(True, True), (True, True)] and len(cards(conn)) == 1


def test_a_base_busy_too_long_brings_the_window_back_with_what_was_typed(tmp_path, monkeypatch):
    app, client, tok, conn = owner_app(tmp_path)

    def busy(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(order_wait, "wait_from_catalog", busy)
    r = send(client, tok)
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and "База сейчас занята" in html and 'value="%s"' % NAME in html and cards(conn) == []


def test_a_position_without_a_size_says_so_in_the_window(tmp_path):
    app, client, tok, conn = owner_app(tmp_path)
    offer = make_offer(goods_id="900", code="NOSIZE", name="IKON Без размера", width=0, height=0, diameter=0, stock=0)
    catalog_archive.record(conn, ParseResult(offers={"900": offer, "901": make_offer(goods_id="901", code="K")}), "2026-10-10T10:00:00")
    catalog_archive.record(conn, ParseResult(offers={"901": make_offer(goods_id="901", code="K")}), "2026-10-15T10:00:00")
    html = client.get("/catalog?wait=900").get_data(as_text=True)
    assert "размер не указан" in html and "None" not in html[html.index('id="wait-window"'):html.index('id="pv"')]
    assert "У позиции не указан размер" in send(client, tok, goods_id="900").get_data(as_text=True) and cards(conn) == []


def test_the_check_of_the_cycle_finds_the_order_from_the_catalog(tmp_path):
    app, client, tok, conn = owner_app(tmp_path)
    send(client, tok, qty="2")
    [cid] = cards(conn)
    offer = make_offer(goods_id="1", code="C1", brand_key="IKON TYRES", model_raw="Test Model", width=205, height=55, diameter=16, stock=2)
    found = order_wait.check(conn, [offer], "2026-10-25T12:00:00")
    assert found["exact"] == [cid]
    assert re.search(order_wait.TAG_EXACT, ",".join(orders.get_card(conn, cid)["tags"]))


# The page of a position (run «match2», lane small-tails): the same button, the same window, the same POST, back to the page.
def test_the_page_of_a_position_has_the_button_and_opens_the_same_window(tmp_path):
    app, client, tok, conn = owner_app(tmp_path)
    html = client.get("/catalog/item/1").get_data(as_text=True)
    assert 'href="/catalog/item/1?wait=1"' in html and "В лист ожидания" in html and 'id="wait-window"' not in html
    html = client.get("/catalog/item/1?wait=1").get_data(as_text=True)
    window = html[html.index('id="wait-window"'):]
    assert 'action="/catalog/wait"' in window and 'name="goods_id" value="1"' in window and "205/55 R16" in window
    assert 'name="next" value="/catalog/item/1"' in window and 'href="/catalog/item/1" aria-label="Закрыть"' in window
    assert "Сейчас в наличии 2 шт." in window


def test_the_window_of_the_page_of_a_position_makes_the_order_and_goes_back_to_the_page(tmp_path):
    app, client, tok, conn = owner_app(tmp_path)
    r = send(client, tok, next="/catalog/item/1")
    assert r.status_code == 302 and r.headers["Location"].endswith("/catalog/item/1") and len(cards(conn)) == 1
    assert "Клиент добавлен в лист ожидания" in client.get("/catalog/item/1").get_data(as_text=True)


def test_a_refused_window_comes_back_on_the_page_of_the_position_with_what_was_typed(tmp_path):
    app, client, tok, conn = owner_app(tmp_path)
    r = send(client, tok, next="/catalog/item/1", client_phone="12345", comment="мой текст")
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and "Телефон клиента: от 10 до 15 цифр." in html and 'id="wait-window"' in html
    assert "entity-page" in html and 'name="next" value="/catalog/item/1"' in html and "мой текст" in html and 'value="%s"' % NAME in html
    assert cards(conn) == []


def test_the_page_of_another_position_is_no_return_address(tmp_path):
    app, client, tok, conn = owner_app(tmp_path)
    r = send(client, tok, next="/catalog/item/2")                                 # the window of position 1 never goes back to position 2
    assert r.headers["Location"].endswith("/catalog")
    r = send(client, tok, next="/catalog/item/1?x=1", client_phone="+7 900 555-44-01")
    assert r.headers["Location"].endswith("/catalog")


def test_the_page_of_a_position_without_the_right_has_no_button_and_no_window(tmp_path):
    app, client, tok, conn = owner_app(tmp_path)
    viewer, _ = person(app, "viewer")
    html = viewer.get("/catalog/item/1?wait=1").get_data(as_text=True)
    assert "entity-page" in html and "В лист ожидания" not in html and 'id="wait-window"' not in html
