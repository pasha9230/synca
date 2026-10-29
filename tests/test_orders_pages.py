"""Pages of the Orders section."""
from __future__ import annotations

import re
from datetime import datetime

from panel_helpers import login, make_app, post
from test_roles import sign_in
from tires_sync import order_points as points, orders, store, users

CHOSEN = "шина из списка"          # a choice in the list fills the text of the row too (хвост З6: a code alone adds nothing)


def setup(tmp_path, **cfg):
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 10, 12, 9, 0, 0), **cfg)          # 12:00 Moscow
    client = app.test_client()
    tok = login(client)
    return app, client, tok, store.connect(app.config["DB_PATH"])


def test_points_page_saves_edits_deletes_and_makes_a_code(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    assert app.test_client().get("/connections/points").status_code == 302
    assert 'href="/connections/points"' in client.get("/connections/webmim").get_data(as_text=True)
    post(client, "/connections/points/save", tok, name="Гамма", manager_name="Алексей", manager_phone="+7 900 000 00 15", channel="vk")
    html = client.get("/connections/points").get_data(as_text=True)
    assert "Гамма" in html and "Алексей" in html and "ВК" in html
    pid = points.list_points(conn)[0]["id"]
    post(client, "/connections/points/save", tok, id=str(pid), name="Гамма", manager_name="Сергей", channel="telegram")
    assert points.get_point(conn, pid)["manager_name"] == "Сергей"
    post(client, "/connections/points/save", tok, name="", channel="vk")                                # refused, the list is unchanged
    assert len(points.list_points(conn)) == 1
    post(client, "/connections/points/%d/code" % pid, tok)
    assert len(points.get_point(conn, pid)["bind_code"]) == 8
    assert points.get_point(conn, pid)["bind_code"] in client.get("/connections/points").get_data(as_text=True)
    post(client, "/connections/points/%d/delete" % pid, tok)
    assert points.list_points(conn) == [] and post(client, "/connections/points/999/delete", tok).status_code in (302, 404)
    assert client.post("/connections/points/save", data={"name": "x"}).status_code == 400            # no CSRF token


def test_the_menu_has_the_orders_entry(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    assert 'href="/orders"' in client.get("/").get_data(as_text=True)


def stages(conn):
    return {s["code"]: s["id"] for s in orders.list_stages(conn)}


def make_card(client, tok, **fields):
    r = post(client, "/orders/new", tok, **dict({"client_name": "Иван"}, **fields))
    assert r.status_code == 302
    return int(re.search(r"/orders/(\d+)", r.headers["Location"]).group(1))


def test_the_board_and_the_list_show_the_cards_and_filters_work(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    assert app.test_client().get("/orders").status_code == 302
    html = client.get("/orders").get_data(as_text=True)
    assert "Новый заказ" in html and "Переговоры" in html and "Возврат" in html and "Заказов пока нет" in html          # seeds the pipeline
    cid = make_card(client, tok, client_name="Иван", tags="срочно")
    make_card(client, tok, client_name="Ольга")
    html = client.get("/orders").get_data(as_text=True)
    assert "Иван" in html and "Ольга" in html and "№%d" % cid in html and 'data-card="%d"' % cid in html
    assert "Иван" in client.get("/orders?q=иван").get_data(as_text=True) and "Ольга" not in client.get("/orders?q=иван").get_data(as_text=True)
    assert "Ольга" not in client.get("/orders?t=срочно").get_data(as_text=True)
    listing = client.get("/orders?v=list").get_data(as_text=True)
    assert "<table" in listing and "Иван" in listing and "Ольга" in listing
    assert client.get("/orders?p=abc&v=zzz").status_code == 200


def test_new_order_needs_a_contact_and_escapes_what_is_typed(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    r = post(client, "/orders/new", tok, comment="без контактов")
    page = r.get_data(as_text=True)                                                             # the window comes back with what was typed
    assert r.status_code == 200 and "Укажите хотя бы имя, телефон или чат клиента" in page and "без контактов" in page
    assert conn.execute("SELECT COUNT(*) FROM order_card").fetchone()[0] == 0
    make_card(client, tok, client_name="<script>alert(1)</script>")
    html = client.get("/orders").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html
    assert client.get("/orders/new").status_code == 200


def test_quick_add_takes_a_phone_typed_into_the_name_field(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    cid = make_card(client, tok, client_name="+7 (900) 000-00-15")
    card = orders.get_card(conn, cid)
    assert card["client_phone"].startswith("+7") and card["client_name"] == ""


def test_moving_by_post_obeys_the_required_data_and_answers_json_for_the_board(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    cid = make_card(client, tok)
    s = stages(conn)
    r = client.post("/orders/%d/move" % cid, data={"csrf": tok, "stage_id": str(s["placed"])}, headers={"X-Requested-With": "fetch"})
    assert r.status_code == 200 and r.get_json()["ok"] is False and "позиции" in r.get_json()["message"]
    assert orders.get_card(conn, cid)["stage"]["code"] == "new"                              # a forged move changed nothing
    r = client.post("/orders/%d/move" % cid, data={"csrf": tok, "stage_id": str(s["talk"])}, headers={"X-Requested-With": "fetch"})
    assert r.get_json()["ok"] is True and orders.get_card(conn, cid)["stage"]["code"] == "talk"
    n = len(orders.list_events(conn, cid))
    client.post("/orders/%d/move" % cid, data={"csrf": tok, "stage_id": str(s["talk"])}, headers={"X-Requested-With": "fetch"})
    assert len(orders.list_events(conn, cid)) == n                                           # a double drop writes nothing
    assert client.post("/orders/%d/move" % cid, data={"csrf": tok, "stage_id": "abc"}, headers={"X-Requested-With": "fetch"}).get_json()["ok"] is False
    assert client.post("/orders/999/move", data={"csrf": tok, "stage_id": str(s["talk"])}).status_code == 404


def test_the_card_page_has_the_stage_strip_the_order_window_and_the_feed(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    cid = make_card(client, tok, client_name="Иван")
    assert app.test_client().get("/orders/%d" % cid).status_code == 302 and client.get("/orders/999").status_code == 404
    html = client.get("/orders/%d" % cid).get_data(as_text=True)
    for part in ("Заказ №%d" % cid, "Новый заказ", "Имя клиента", "Позиции", "Доставка и WebMIM", "Дополнительно", "Продажа и расчёты", "Лента событий",
                 "Заказ создан на этапе"):
        assert part in html
    assert '<nav class="tabs">' not in html                                                     # one window instead of the tabs (the owner, 2026-10-20)
    assert "Иван" in client.get("/orders/%d?tab=items" % cid).get_data(as_text=True)
    assert client.get("/orders/%d?tab=zzz" % cid).status_code == 200


def test_saving_fields_notes_and_the_move_from_the_card(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    pid = points.save_point(conn, {"name": "Гамма"})
    cid = make_card(client, tok)
    post(client, "/orders/%d/save" % cid, tok, client_phone="+7 900 000 00 15", point_id=str(pid), delivery_date="2026-10-14", comment="<b>x</b>")
    c = orders.get_card(conn, cid)
    assert c["client_phone"].startswith("+7") and c["point"]["name"] == "Гамма" and c["delivery_date"] == "2026-10-14"
    assert "<b>x</b>" not in client.get("/orders/%d" % cid).get_data(as_text=True)
    post(client, "/orders/%d/save" % cid, tok, client_phone="12")                                  # refused: nothing changes
    assert orders.get_card(conn, cid)["client_phone"].startswith("+7")
    post(client, "/orders/%d/note" % cid, tok, text="Звонил клиент")
    assert "Звонил клиент" in client.get("/orders/%d" % cid).get_data(as_text=True)
    post(client, "/orders/%d/note" % cid, tok, text="")
    assert len([e for e in orders.list_events(conn, cid) if e["kind"] == "note"]) == 1
    r = post(client, "/orders/%d/move" % cid, tok, stage_id=str(stages(conn)["placed"]))
    assert "позиции" in client.get("/orders/%d" % cid).get_data(as_text=True)                      # the refusal is shown on the card
    assert orders.get_card(conn, cid)["stage"]["code"] == "new" and r.status_code == 302


def test_positions_come_from_the_catalog_with_the_money_of_the_moment(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    cid = make_card(client, tok)
    goods = re.findall(r'name="g" value="([^"]+)"', client.get("/catalog").get_data(as_text=True))[0]
    post(client, "/orders/%d/items/add" % cid, tok, query=CHOSEN, goods_id=goods, qty="4")
    c = orders.get_card(conn, cid)
    it = c["items"][0]
    assert it["goods_id"] == goods and it["qty"] == 4 and it["supplier_price"] > 0 and it["sale_unit"] > 0 and it["money"]["margin"] is not None
    html = client.get("/orders/%d?tab=items" % cid).get_data(as_text=True)
    assert it["name"] in html and "Итого" in html
    post(client, "/orders/%d/items/%d/update" % (cid, it["id"]), tok, qty="2", sale_unit="5000")
    assert orders.get_card(conn, cid)["items"][0]["qty"] == 2 and orders.get_card(conn, cid)["items"][0]["sale_unit"] == 5000.0
    post(client, "/orders/%d/items/add" % cid, tok, query=CHOSEN, goods_id="no-such-goods", qty="1")
    post(client, "/orders/%d/items/add" % cid, tok, query=CHOSEN, goods_id=goods, qty="0")
    assert len(orders.get_card(conn, cid)["items"]) == 1                                           # unknown goods and bad quantity add nothing
    post(client, "/orders/%d/items/%d/delete" % (cid, it["id"]), tok)
    assert orders.get_card(conn, cid)["items"] == []
    created = make_card(client, tok, client_name="", goods_id=goods)                              # «Оформить заказ» from the catalog: a position alone is enough
    assert len(orders.get_card(conn, created)["items"]) == 1


def test_the_sale_button_works_once_and_a_return_offers_the_reversal(tmp_path):
    from tires_sync import sales
    app, client, tok, conn = setup(tmp_path)
    cid = make_card(client, tok)
    goods = re.findall(r'name="g" value="([^"]+)"', client.get("/catalog").get_data(as_text=True))[0]
    post(client, "/orders/%d/items/add" % cid, tok, query=CHOSEN, goods_id=goods, qty="4")
    post(client, "/orders/%d/sale" % cid, tok)                                                     # not on the successful stage: refused
    assert orders.get_card(conn, cid)["sale_ids"] == []
    pid = points.save_point(conn, {"name": "Гамма"})
    post(client, "/orders/%d/save" % cid, tok, client_phone="+7 900 000 00 15", point_id=str(pid), delivery_date="2026-10-14")
    post(client, "/orders/%d/move" % cid, tok, stage_id=str(stages(conn)["done"]))
    post(client, "/orders/%d/sale" % cid, tok)
    post(client, "/orders/%d/sale" % cid, tok)                                                     # a second press
    day = "2026-10-12"
    assert len(sales.list_sales(conn, day, day)) == 1 and len(orders.get_card(conn, cid)["sale_ids"]) == 1
    post(client, "/orders/%d/sale/reverse" % cid, tok)
    assert sales.summarize(sales.list_sales(conn, day, day))["pieces"] == 0 and orders.get_card(conn, cid)["sale_ids"] == []


def test_the_pipeline_settings_add_rename_move_and_delete_stages(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    assert app.test_client().get("/orders/settings").status_code == 302
    html = client.get("/orders/settings").get_data(as_text=True)
    assert "Новый заказ" in html and "На ОТХ" in html and "Системный" in html
    post(client, "/orders/settings/stage/add", tok, name="Ждём ответа", color="#7F56D9", kind="open", hint="Позвоните", limit_hours="48")
    mine = [s for s in orders.list_stages(conn) if s["name"] == "Ждём ответа"][0]
    post(client, "/orders/settings/stage/%d/update" % mine["id"], tok, name="Ждём клиента", color="#F79009", kind="open", hint="", limit_hours="24")
    assert orders.get_stage(conn, mine["id"])["name"] == "Ждём клиента"
    post(client, "/orders/settings/stage/%d/shift" % mine["id"], tok, dir="up")
    assert [s["name"] for s in orders.list_stages(conn)][-2] == "Ждём клиента"
    post(client, "/orders/settings/stage/%d/delete" % mine["id"], tok)
    assert "Ждём клиента" not in [s["name"] for s in orders.list_stages(conn)]


def test_settings_refuse_bad_input_and_protect_system_stages_and_stages_with_cards(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    client.get("/orders")                                                                           # seeds the pipeline
    before = len(orders.list_stages(conn))
    post(client, "/orders/settings/stage/add", tok, name="", color="#7F56D9", kind="open")
    post(client, "/orders/settings/stage/add", tok, name="x", color="red", kind="open")
    assert len(orders.list_stages(conn)) == before
    system = orders.list_stages(conn)[0]["id"]
    post(client, "/orders/settings/stage/%d/delete" % system, tok)
    assert len(orders.list_stages(conn)) == before
    post(client, "/orders/settings/stage/add", tok, name="Мой", color="#7F56D9", kind="open")
    mine = orders.list_stages(conn)[-1]["id"]
    conn.execute("INSERT INTO order_card (stage_id, created_at, created_by, entered_at, updated_at) VALUES (?, 'n','o','n','n')", (mine,))
    conn.commit()
    post(client, "/orders/settings/stage/%d/delete" % mine, tok)
    assert orders.get_stage(conn, mine)["name"] == "Мой"
    assert post(client, "/orders/settings/stage/999/delete", tok).status_code in (302, 404)


def test_cancel_and_return_are_reachable_from_the_card_form(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    s = stages(conn) if False else None
    cid = make_card(client, tok)
    s = stages(conn)
    html = client.get("/orders/%d" % cid).get_data(as_text=True)
    assert 'name="cancel_reason"' in html and 'name="return_reason"' in html and 'name="return_kind"' in html           # on a card in «Новый заказ»
    assert 'name="stage_id" value="%d"' % s["cancel"] in html and 'name="stage_id" value="%d"' % s["return"] in html
    post(client, "/orders/%d/move" % cid, tok, stage_id=str(s["cancel"]), cancel_reason="клиент передумал")
    assert orders.get_card(conn, cid)["stage"]["code"] == "cancel" and orders.get_card(conn, cid)["cancel_reason"] == "клиент передумал"
    other = make_card(client, tok, client_name="Ольга")
    post(client, "/orders/%d/move" % other, tok, stage_id=str(s["return"]), return_kind="client", return_reason="не подошли", return_amount="9600")
    assert orders.get_card(conn, other)["stage"]["code"] == "return" and orders.get_card(conn, other)["return_amount"] == 9600.0


def test_cancel_and_return_open_as_a_window_over_the_card(tmp_path):
    """The owner 2026-10-23: «Отменить заказ или оформить возврат…» did nothing to see — the block was at the very bottom of the card. The
    item of «Ещё» opens the window «Отмена или возврат» over the card: one markup, the same forms to /move; no block left below."""
    app, client, tok, conn = setup(tmp_path)
    cid = make_card(client, tok)
    s = stages(conn)
    page = client.get("/orders/%d" % cid).get_data(as_text=True)
    menu = page[page.index('<details class="menu owin-menu">'):page.index("</details>", page.index('<details class="menu owin-menu">'))]
    assert 'data-open-modal="finish-window"' in menu and "Отменить заказ или оформить возврат…" in menu
    win = page[page.index('id="finish-window"'):]
    win = win[:win.index('</div></div></div>')]
    assert 'class="modal-backdrop' in page[page.rindex("<div", 0, page.index('id="finish-window"')):page.index('id="finish-window"')]
    assert win.count('action="/orders/%d/move"' % cid) == 2 and 'name="stage_id" value="%d"' % s["cancel"] in win and 'name="stage_id" value="%d"' % s["return"] in win
    assert all('name="%s"' % f in win for f in ("cancel_reason", "return_kind", "return_reason", "return_amount")) and "data-close-modal" in win
    assert page.count('name="cancel_reason"') == 1 and "ocard-finish" not in page                         # the forms are drawn once: in the window
    post(client, "/orders/%d/move" % cid, tok, stage_id=str(s["cancel"]), cancel_reason="клиент передумал")
    assert orders.get_card(conn, cid)["stage"]["code"] == "cancel"
    page = client.get("/orders/%d" % cid).get_data(as_text=True)
    assert 'id="finish-window"' not in page and "data-open-modal=\"finish-window\"" not in page               # a cancelled order has neither


def test_a_point_moves_but_does_not_save_and_still_has_the_cancel_window(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    pid = points.save_point(conn, {"name": "Гамма"})
    cid = make_card(client, tok)
    post(client, "/orders/%d/save" % cid, tok, point_id=str(pid))
    users.add_user(conn, "gamma", "пароль-для-точки-1", "2026-10-15T12:00:00", role="point", point_id=pid)
    sign_in(client, conn, "gamma")
    page = client.get("/orders/%d" % cid).get_data(as_text=True)
    assert '<fieldset disabled' in page and 'data-open-modal="finish-window"' in page and 'id="finish-window"' in page
    assert '<a class="menu-item menu-item--danger" href="#finish"' in page                  # a link, not a button: a disabled fieldset does not switch it off


def test_the_sale_button_is_guarded_against_a_double_press_in_the_browser(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    cid = make_card(client, tok)
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (stages(conn)["done"], cid))             # on the successful stage
    conn.commit()
    html = client.get("/orders/%d" % cid).get_data(as_text=True)
    form = re.search(r'<form[^>]*action="/orders/%d/sale"[^>]*>' % cid, html).group(0)
    assert "data-once" in form                                                                       # app.js guards a form, not a button


def test_a_bad_position_from_the_catalog_does_not_leave_an_empty_card_behind(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    before = conn.execute("SELECT COUNT(*) FROM order_card").fetchone()[0]
    r = post(client, "/orders/new", tok, client_name="", goods_id="NO-SUCH-GOODS")
    page = r.get_data(as_text=True)                                                             # the window comes back with the position in its list
    assert r.status_code == 200 and "Этой позиции нет в выгрузке поставщика" in page and 'name="item_goods_id" value="NO-SUCH-GOODS"' in page
    assert conn.execute("SELECT COUNT(*) FROM order_card").fetchone()[0] == before


def test_the_bind_code_message_does_not_promise_a_command_that_does_not_exist(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    pid = points.save_point(conn, {"name": "Гамма"})
    post(client, "/connections/points/%d/code" % pid, tok)
    html = client.get("/connections/points").get_data(as_text=True)
    assert "привязка КОД" not in html and "Код создан" in html


def test_a_crafted_post_cannot_change_the_kind_of_a_system_stage_but_the_form_can_rename_it(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    client.get("/orders")
    done = orders.get_stage(conn, stages(conn)["done"])
    post(client, "/orders/settings/stage/%d/update" % done["id"], tok, name="Готово", color=done["color"], kind="open", hint="", limit_hours="0")
    assert orders.get_stage(conn, done["id"])["name"] == "Выполнено" and orders.get_stage(conn, done["id"])["kind"] == "won"
    html = client.get("/orders/settings").get_data(as_text=True)
    assert 'name="kind" value="won"' in html                                                          # the form carries the fixed kind
    post(client, "/orders/settings/stage/%d/update" % done["id"], tok, name="Готово", color=done["color"], kind="won", hint="", limit_hours="0")
    assert orders.get_stage(conn, done["id"])["name"] == "Готово"


def test_deleting_the_last_position_of_a_placed_order_is_a_message_not_a_server_error(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    cid = make_card(client, tok)
    goods = re.findall(r'name="g" value="([^"]+)"', client.get("/catalog").get_data(as_text=True))[0]
    post(client, "/orders/%d/items/add" % cid, tok, query=CHOSEN, goods_id=goods, qty="4")
    pid = points.save_point(conn, {"name": "Гамма"})
    post(client, "/orders/%d/save" % cid, tok, client_phone="+7 900 000 00 15", point_id=str(pid), delivery_date="2026-10-14")
    post(client, "/orders/%d/move" % cid, tok, stage_id=str(stages(conn)["placed"]))
    iid = orders.get_card(conn, cid)["items"][0]["id"]
    r = post(client, "/orders/%d/items/%d/delete" % (cid, iid), tok)
    assert r.status_code == 302 and len(orders.get_card(conn, cid)["items"]) == 1
    assert "нельзя убрать" in client.get("/orders/%d?tab=items" % cid).get_data(as_text=True)


def test_the_reversal_is_always_offered_when_a_sale_exists_and_a_hanging_record_can_be_released(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    cid = make_card(client, tok)
    conn.execute("UPDATE order_card SET stage_id = ?, sale_ids = '[5]' WHERE id = ?", (stages(conn)["done"], cid))
    conn.commit()
    html = client.get("/orders/%d" % cid).get_data(as_text=True)
    assert "Сторнировать продажу" in html and "Продажа записана" in html                              # no return_kind needed
    conn.execute("UPDATE order_card SET sale_ids = '[-1]' WHERE id = ?", (cid,))
    conn.commit()
    html = client.get("/orders/%d" % cid).get_data(as_text=True)
    assert "Запись продажи не завершена" in html and "Освободить" in html


def test_a_price_with_kopeks_is_not_rounded_by_the_position_form(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    cid = make_card(client, tok)
    goods = re.findall(r'name="g" value="([^"]+)"', client.get("/catalog").get_data(as_text=True))[0]
    post(client, "/orders/%d/items/add" % cid, tok, query=CHOSEN, goods_id=goods, qty="1", sale_unit="1234,50")
    assert 'name="sale_unit" value="1234.5"' in client.get("/orders/%d?tab=items" % cid).get_data(as_text=True)


def test_the_card_saves_the_sheet_fields_and_the_new_order_form_has_the_source(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    assert 'name="source"' in client.get("/orders/new").get_data(as_text=True)
    post(client, "/orders/new", tok, client_name="Иван", source="avito")
    cid = conn.execute("SELECT id FROM order_card").fetchone()[0]
    page = client.get("/orders/%d" % cid).get_data(as_text=True)
    window = page[page.index('<form id="ow-main"'):page.index("</form>", page.index('<form id="ow-main"'))]
    for name in ("source", "deal_kind", "transfer_number"):
        assert 'name="%s"' % name in window
    money = page[page.index('id="money"'):]                                                     # the dates of the settlements are in «Продажа и расчёты» below
    for name in ("settled_point_at", "codes_written_off_at", "settled_supplier_at"):
        assert 'name="%s"' % name in money and 'name="%s"' % name not in window
    post(client, "/orders/%d/save" % cid, tok, tab="main", source="pvz", deal_kind="stock", settled_point_at="2026-10-14")
    card = orders.get_card(conn, cid)
    assert (card["source"], card["deal_kind"], card["settled_point_at"]) == ("pvz", "stock", "2026-10-14")
    post(client, "/orders/%d/save" % cid, tok, tab="main", source="bogus")                                  # refused, nothing changes
    assert orders.get_card(conn, cid)["source"] == "pvz"


def test_the_funnel_settings_show_one_collapsible_row_per_stage_and_deleting_a_point_asks_again(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    page = client.get("/orders/settings").get_data(as_text=True)
    assert page.count('<details class="stage-block"') == len(orders.list_stages(conn)) and "Заказов на этапе" in page
    post(client, "/connections/points/save", tok, name="Гамма", channel="vk")
    points_page = client.get("/connections/points").get_data(as_text=True)
    assert "Точно удалить?" in points_page                                                                # a second look before the deletion


def test_a_card_with_our_chat_shows_a_link_to_it(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    orders.ensure_pipeline(conn, "2026-10-12T12:00:00")
    cid = orders.create_card(conn, dict(client_name="Иван", avito_chat="Ч-7"), "2026-10-12T12:00:00", "o")
    assert 'href="/chats/7"' in client.get("/orders/%d" % cid).get_data(as_text=True)


def test_the_card_shows_the_stages_the_summary_and_hides_the_rare_actions_in_the_more_menu(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    pid = points.save_point(conn, {"name": "Гамма"})
    cid = make_card(client, tok, client_name="Иван")
    post(client, "/orders/%d/save" % cid, tok, point_id=str(pid), delivery_date="2026-10-21", delivery_time="после 18", webmim_number="1234567")
    page = client.get("/orders/%d" % cid).get_data(as_text=True)
    funnel = page[page.index('class="owin-stages"'):page.index('class="owin-note"')]
    assert '<span class="owin-stage is-current" aria-current="step">Новый заказ · 0 дн.</span>' in funnel and "Выполнено" in funnel and "Отмена" not in funnel
    assert 'formaction="/orders/%d/save" name="stage_id" value="%d"' % (cid, stages(conn)["placed"]) in funnel        # a click on a stage saves the window and moves
    window = page[page.index('<form id="ow-main"'):page.index("</form>", page.index('<form id="ow-main"'))]
    assert 'value="%d" selected>Гамма' % pid in window and 'value="2026-10-21"' in window and 'value="после 18"' in window and 'value="1234567"' in window
    menu = page[page.index('<details class="menu owin-menu">'):page.index("</details>", page.index('<details class="menu owin-menu">'))]
    assert 'form="ow-delete"' in menu and 'data-open-modal="finish-window"' in menu and '<form id="ow-delete" method="post" action="/orders/%d/delete"' % cid in page
    assert 'id="finish-window"' in page and 'value="%d"' % orders.get_stage(conn, [s for s in orders.list_stages(conn) if s["code"] == "cancel"][0]["id"])["id"] in page
    cancel = [s for s in orders.list_stages(conn) if s["code"] == "cancel"][0]["id"]
    post(client, "/orders/%d/move" % cid, tok, stage_id=str(cancel), cancel_reason="передумал")
    page = client.get("/orders/%d" % cid).get_data(as_text=True)
    assert '<span class="owin-stage is-stopped" aria-current="step">Отмена / невыкуп · 0 дн.</span>' in page and "is-done" not in page and 'id="finish-window"' not in page


def test_special_terms_and_the_receipt_show_on_the_collapsed_extra_fields(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    cid = make_card(client, tok, client_name="Иван")

    def summary():
        page = client.get("/orders/%d" % cid).get_data(as_text=True)
        return page[page.index('<details class="owin-extra">'):page.index("</summary>", page.index('<details class="owin-extra">'))]
    assert "Особые условия" not in summary() and "товарный чек" not in summary()
    post(client, "/orders/%d/save" % cid, tok, special_terms="перевод на карту", special_amount="5000", receipt_needed=["1", ""])
    shown = summary()                                                                            # seen without opening «Дополнительно»
    assert "Особые условия: 5" in shown and 'title="перевод на карту"' in shown and "Нужен товарный чек" in shown


def test_a_refused_move_on_the_order_page_is_shown_as_an_error_and_a_done_one_as_a_note(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    orders.ensure_pipeline(conn, "2026-10-12T12:00:00")
    cid = orders.create_card(conn, dict(client_name="Алексей"), "2026-10-12T12:00:00", "o")
    stage = {s["code"]: s["id"] for s in orders.list_stages(conn)}
    page = client.post("/orders/%d/move" % cid, data={"csrf": tok, "stage_id": str(stage["moving"])}, follow_redirects=True).get_data(as_text=True)
    assert 'class="flash flash--err" role="alert"' in page and "заполните: позиции, точка, дата доставки, телефон клиента" in page
    page = client.post("/orders/%d/move" % cid, data={"csrf": tok, "stage_id": str(stage["talk"])}, follow_redirects=True).get_data(as_text=True)
    assert "Заказ перенесён." in page and "flash--err" not in page
