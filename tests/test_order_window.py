"""The window of an order (the owner's mockup 2026-10-20): «Новый заказ» with positions, all or nothing; the same window on the card."""
from __future__ import annotations

import re
from datetime import datetime

import panel_helpers
from panel_helpers import login, make_app, post
from test_order_search import REF, UNLOAD
from tires_sync import order_engine as engine, order_points as points, order_rules as rules, order_templates as tpl, orders, store, users


def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(panel_helpers, "UNLOAD", UNLOAD)
    monkeypatch.setattr(panel_helpers, "REF_TEXT", REF)
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 10, 12, 9, 0, 0))
    client = app.test_client()
    tok = login(client)
    client.get("/orders")                                                                       # the pipeline
    return app, client, tok, store.connect(app.config["DB_PATH"])


def created(r) -> int:
    assert r.status_code == 302, r.get_data(as_text=True)[:300]
    return int(re.search(r"/orders/(\d+)$", r.headers["Location"]).group(1))


def count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM order_card").fetchone()[0]


def test_the_new_order_window_has_the_fields_in_the_order_of_the_mockup_and_the_creator_answers_by_default(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    users.add_user(conn, "worker2", "пароль-для-партнёра-1", "2026-10-15T12:00:00")
    page = client.get("/orders/new").get_data(as_text=True)
    order = ["client_name", "client_phone", "point_id", "deal_kind", "responsible", "item_query", "item_qty", "item_sale_unit", "delivery_date",
             "delivery_time", "webmim_number", "transfer_number", "comment", "special_terms", "special_amount", "receipt_needed", "avito_chat", "source"]
    at = [page.index('name="%s"' % name) for name in order]
    assert at == sorted(at)
    assert page.index('<summary>Дополнительно') < page.index('name="comment"')                   # the rare fields are folded
    assert 'name="tags"' in page and 'class="owin-head"' in page and "Закупка" not in page and "прибыль" not in page.lower()
    assert '<option value="owner" selected>' in page and 'value="worker2"' in page                 # the one who makes the order answers, another can be chosen
    assert '<option value="avito" selected>' in page and '<option value="webmim" selected>' in page


def test_an_order_is_made_with_several_positions_the_hand_price_and_the_fields_of_the_window(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    users.add_user(conn, "worker2", "пароль-для-партнёра-1", "2026-10-15T12:00:00")
    pid = points.save_point(conn, {"name": "Гамма"})
    price = client.get("/orders/items/find?q=autograph snow").get_json()["items"][0]["price"]
    cid = created(post(client, "/orders/new", tok, client_name="Антон", client_phone="+7 900 123-45-67", point_id=str(pid), deal_kind="stock",
                       responsible="worker2", tags="Основной, повторный", delivery_date="2026-10-21", delivery_time="после 18", webmim_number="5000300",
                       transfer_number="26R-1", comment="Объявление", special_terms="предоплата", special_amount="1.500", receipt_needed=["1", ""],
                       avito_chat="Ч-2346", source="repeat",
                       item_goods_id=["100", "101", ""], item_query=["Autograph Snow 3 · 205/55 R16", "", ""], item_qty=["4", "2", "4"],
                       item_sale_unit=["", "4800,50", ""]))
    c = orders.get_card(conn, cid)
    assert [(i["goods_id"], i["qty"], i["sale_unit"]) for i in c["items"]] == [("100", 4, price), ("101", 2, 4800.5)]    # the empty field row is skipped
    assert (c["client_name"], c["point_id"], c["deal_kind"], c["responsible"], c["tags"]) == ("Антон", pid, "stock", "worker2", ["Основной", "повторный"])
    assert (c["delivery_date"], c["webmim_number"], c["transfer_number"], c["special_amount"], c["receipt_needed"], c["source"]) == (
        "2026-10-21", "5000300", "26R-1", 1500.0, 1, "repeat")


def test_one_position_that_cannot_be_added_leaves_no_order_and_the_window_keeps_what_was_typed(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    for bad in ({"item_goods_id": ["100", "999"], "item_qty": ["4", "1"]},                       # a code no longer in the unload
                {"item_goods_id": ["100", "101"], "item_qty": ["4", "0"]},                       # a quantity add_item refuses: the card is already written then
                {"item_goods_id": ["100", "101"], "item_qty": ["4", "2"], "item_sale_unit": ["", "abc"]},
                {"item_query": ["ikon autograph"], "item_qty": ["1"]}):                          # two positions match the text
        r = post(client, "/orders/new", tok, client_name="Антон", comment="перезвонить вечером", **bad)
        page = r.get_data(as_text=True)
        assert r.status_code == 200 and count(conn) == 0 and conn.execute("SELECT COUNT(*) FROM order_item").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM order_event").fetchone()[0] == 0                # not even «Заказ создан»
        assert 'value="Антон"' in page and "перезвонить вечером" in page and 'class="flash flash--err"' in page
    r = post(client, "/orders/new", tok, client_name="Антон", item_goods_id=["100"] * 21, item_qty=["1"] * 21)
    assert r.status_code == 200 and "не больше 20 позиций" in r.get_data(as_text=True) and count(conn) == 0


def test_the_rules_of_the_first_stage_see_the_positions_of_a_new_order(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    new = {s["code"]: s["id"] for s in orders.list_stages(conn)}["new"]
    t = tpl.add_template(conn, "Клиенту", "client", "Ваш заказ: {items}")
    rules.add_rule(conn, new, "enter", 0, "message", t, "", "ask", None)
    cid = created(post(client, "/orders/new", tok, client_name="Антон", item_goods_id=["100"], item_query=[""], item_qty=["4"], item_sale_unit=["5000"]))
    [task] = engine.list_tasks(conn, cid)
    assert "Autograph Snow" in task["body"] and "× 4" in task["body"]
    kinds = [e["kind"] for e in reversed(orders.list_events(conn, cid))]
    assert kinds[:2] == ["created", "item"]


def test_the_older_ways_of_making_an_order_still_work(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    assert orders.get_card(conn, created(post(client, "/orders/new", tok, client_name="Иван")))["items"] == []           # the quick add of the board
    one = orders.get_card(conn, created(post(client, "/orders/new", tok, goods_id="100")))                           # the catalog's form: no contact needed
    assert [(i["goods_id"], i["qty"]) for i in one["items"]] == [("100", 1)]
    page = client.get("/orders/new?goods_id=100").get_data(as_text=True)                         # «Оформить заказ» from the catalog: the position is in the list
    assert 'name="item_goods_id" value="100"' in page and 'name="item_qty" value="1"' in page and "Autograph Snow" in page
    assert client.get("/orders/new?goods_id=NO").status_code == 200
    assert post(client, "/orders/new", tok, client_name="Иван").status_code == 302
    assert client.post("/orders/new", data={"client_name": "x"}).status_code == 400              # no CSRF token


def test_the_tags_are_plates_with_suggestions_of_the_tags_already_used(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    cid = created(post(client, "/orders/new", tok, client_name="Иван", tags="срочно, Основной"))
    created(post(client, "/orders/new", tok, client_name="Ольга", tags="срочно"))
    page = client.get("/orders/%d" % cid).get_data(as_text=True)
    head = page[page.index('class="owin-head"'):page.index('class="owin-body"')]
    assert 'data-tag="срочно"' in head and 'data-tag="Основной"' in head and 'name="tags" value="срочно, Основной"' in head
    known = re.findall(r'<option value="([^"]+)">', head[head.index("<datalist"):])
    assert known == ["срочно", "Основной"]                                                       # the most used first
    assert "<script" not in client.get("/orders/new").get_data(as_text=True).split('id="ow-known-tags"')[1].split("</datalist>")[0]
    post(client, "/orders/%d/save" % cid, tok, tags="Основной")                                 # ✕ on «срочно»: the window sends the rest
    assert orders.get_card(conn, cid)["tags"] == ["Основной"]
    post(client, "/orders/%d/save" % cid, tok, tags=", ".join("т%d" % i for i in range(11)))     # the limits of orders._clean stay
    assert orders.get_card(conn, cid)["tags"] == ["Основной"]


def test_the_card_window_saves_its_fields_moves_by_a_stage_and_keeps_the_positions_forms(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    pid = points.save_point(conn, {"name": "Гамма"})
    cid = created(post(client, "/orders/new", tok, client_name="Иван", item_goods_id=["100"], item_qty=["4"], item_sale_unit=["5875"]))
    page = client.get("/orders/%d" % cid).get_data(as_text=True)
    window = page[page.index('<section class="card owin"'):page.index("</section>", page.index('<section class="card owin"'))]
    assert '<form id="ow-main" method="post" action="/orders/%d/save"' % cid in window
    assert " шт × " in window and "23 500" in window and "Итого" in window and "Закупка за 1 шт" in window and "рибыль" not in window   # the owner 2026-10-21: the purchase is in the row
    iid = orders.get_card(conn, cid)["items"][0]["id"]
    for form in ('<form id="ow-item-%d" method="post" action="/orders/%d/items/%d/update">' % (iid, cid, iid),
                 '<form id="ow-item-del-%d" method="post" action="/orders/%d/items/%d/delete">' % (iid, cid, iid)):
        assert form in window
    main = window[window.index('<form id="ow-main"'):window.index("</form>")]
    assert main.index('<button class="sr-only"') < main.index("formaction=")                   # Enter in a field saves, it never moves the card
    assert 'name="goods_id" data-find-code' in main and 'name="qty" value="4" form="ow-item-%d"' % iid in main
    post(client, "/orders/%d/items/add" % cid, tok, goods_id="101", query="", qty="2", sale_unit="4800")          # хвост З6: a code alone (restored
    assert len(orders.get_card(conn, cid)["items"]) == 1                                                     # by the browser) adds nothing
    post(client, "/orders/%d/items/add" % cid, tok, **ICE)
    post(client, "/orders/%d/items/%d/update" % (cid, iid), tok, qty="2", sale_unit="6000")
    assert [(i["qty"], i["sale_unit"]) for i in orders.get_card(conn, cid)["items"]] == [(2, 6000.0), (2, 4800.0)]
    r = post(client, "/orders/%d/items/%d/delete" % (cid, iid), tok)
    assert r.headers["Location"].endswith("/orders/%d#items" % cid) and len(orders.get_card(conn, cid)["items"]) == 1
    assert 'formaction="/orders/%d/save" name="stage_id"' % cid in main                    # a stage of the window saves it first, then moves


def test_a_stage_of_the_window_saves_the_fields_first_then_moves(tmp_path, monkeypatch):
    """The team lead, 2026-10-20: wrong fields — nothing saved, nothing moved, the typed stays; saved fields and a refused move — the fields stay."""
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    pid = points.save_point(conn, {"name": "Гамма"})
    cid = created(post(client, "/orders/new", tok, client_name="Иван", item_goods_id=["100"], item_qty=["4"]))
    placed = {s["code"]: s["id"] for s in orders.list_stages(conn)}["placed"]
    fields = dict(client_phone="+7 900 000 00 15", point_id=str(pid), delivery_date="2026-10-14", client_name="Иван", tags="", comment="звонить после 18")
    n = len(orders.list_events(conn, cid))
    r = post(client, "/orders/%d/save" % cid, tok, stage_id=str(placed), **dict(fields, client_phone="12"))      # a wrong phone
    page = r.get_data(as_text=True)
    assert r.status_code == 200 and "Телефон клиента: от 10 до 15 цифр" in page and 'value="12"' in page and "звонить после 18" in page
    c = orders.get_card(conn, cid)
    assert c["stage"]["code"] == "new" and c["client_phone"] == "" and c["comment"] == "" and len(orders.list_events(conn, cid)) == n
    r = post(client, "/orders/%d/save" % cid, tok, stage_id=str(placed), **dict(fields, delivery_date=""))      # saved, but the stage needs a date
    c = orders.get_card(conn, cid)
    assert r.status_code == 302 and c["stage"]["code"] == "new" and c["client_phone"].startswith("+7") and c["comment"] == "звонить после 18"
    refused = [e for e in orders.list_events(conn, cid) if e["kind"] == "refused"]
    assert len(refused) == 1 and refused[0]["level"] == "red" and "дата доставки" in refused[0]["text"]
    assert "дата доставки" in client.get("/orders/%d" % cid).get_data(as_text=True)                               # the refusal is shown on the card
    post(client, "/orders/%d/save" % cid, tok, stage_id=str(placed), **fields)                                    # all there: saved and moved
    c = orders.get_card(conn, cid)
    assert c["stage"]["code"] == "placed" and c["point_id"] == pid and c["delivery_date"] == "2026-10-14"
    assert post(client, "/orders/%d/save" % cid, tok, stage_id="abc", client_name="Иван").status_code == 302 and orders.get_card(conn, cid)["stage"]["code"] == "placed"


def test_a_wrong_field_saved_by_the_button_comes_back_with_what_was_typed(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    cid = created(post(client, "/orders/new", tok, client_name="Иван", tags="срочно"))
    r = post(client, "/orders/%d/save" % cid, tok, client_name="Пётр", delivery_date="2026-13-40", tags="срочно, новый")
    page = r.get_data(as_text=True)
    assert r.status_code == 200 and "Дата доставки: формат ГГГГ-ММ-ДД" in page and 'value="Пётр"' in page and 'data-tag="новый"' in page
    assert orders.get_card(conn, cid)["client_name"] == "Иван"
    r = post(client, "/orders/%d/save" % cid, tok, tab="money", settled_point_at="bad")                           # the dates of «Продажа и расчёты»: as before
    assert r.status_code == 302 and r.headers["Location"].endswith("/orders/%d#money" % cid)


def test_the_card_shows_no_profit_and_the_purchase_only_in_the_rows(tmp_path, monkeypatch):
    """The owner 2026-10-20: «без цены закупки и прибыли»; 2026-10-21 the purchase came back to the rows of the positions (tests/test_order_purchase.py),
    the profit stays in the reports."""
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    cid = created(post(client, "/orders/new", tok, client_name="Иван", item_goods_id=["100"], item_qty=["4"], item_sale_unit=["5875"]))
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", ({s["code"]: s["id"] for s in orders.list_stages(conn)}["done"], cid))
    conn.commit()
    page = client.get("/orders/%d" % cid).get_data(as_text=True)
    body = page[page.index('<main'):] if '<main' in page else page
    assert body.lower().count("закупк") == 2 and "Закупка за 1 шт" in body and "прибыл" not in body.lower()   # the label and its aria-label of the one row
    assert "Сумма заказа <b>23\u00a0500" in body and "Записать продажу" in body


def test_a_stage_of_the_window_sends_who_answers_now_and_that_names_nobody_new(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    cid = orders.create_card(conn, dict(client_name="Иван"), "2026-10-12T12:00:00", "robot")      # nobody named: the maker answers (orders.get_card)
    page = client.get("/orders/%d" % cid).get_data(as_text=True)
    assert '<option value="robot" selected>' in page
    talk = {s["code"]: s["id"] for s in orders.list_stages(conn)}["talk"]
    post(client, "/orders/%d/save" % cid, tok, stage_id=str(talk), client_name="Иван", responsible="robot")
    assert orders.get_card(conn, cid)["stage"]["code"] == "talk"
    assert not [e for e in orders.list_events(conn, cid) if e["text"].startswith("Ответственный")]
    assert conn.execute("SELECT responsible FROM order_card WHERE id = ?", (cid,)).fetchone()[0] == ""


def test_the_cancel_and_return_fields_are_in_the_window_on_those_stages(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    s = {x["code"]: x["id"] for x in orders.list_stages(conn)}
    cid = created(post(client, "/orders/new", tok, client_name="Иван"))
    post(client, "/orders/%d/move" % cid, tok, stage_id=str(s["return"]), return_kind="client", return_reason="не подошли", return_amount="9600")
    page = client.get("/orders/%d" % cid).get_data(as_text=True)
    main = page[page.index('<form id="ow-main"'):page.index("</form>", page.index('<form id="ow-main"'))]
    assert 'value="не подошли"' in main and 'name="return_amount" value="9600"' in main and 'name="cancel_reason"' not in main   # «9600», not «9600.0» (2026-10-26)
    post(client, "/orders/%d/save" % cid, tok, return_reason="брак")
    assert orders.get_card(conn, cid)["return_reason"] == "брак"


def test_a_refused_position_rolls_the_new_card_back_so_a_later_save_of_the_same_connection_keeps_nothing(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    item = dict(goods_id="1", name="Ikon 205/55 R16", brand_key="IKON", season="winter", diameter=16, territory="", sale_unit=5000, supplier_price=4000,
                terms={}, fee_pct=0, promo_pct=0)
    try:
        orders.create_card_with_items(conn, {"client_name": "Антон"}, [dict(item, qty=4), dict(item, qty=0)], "2026-10-12T12:00:00", "owner")
    except orders.OrderError as err:
        assert "Количество" in str(err)
    else:
        raise AssertionError("a quantity of 0 must be refused")
    conn.commit()                                                                               # whatever the caller saves next
    assert count(conn) == 0 and conn.execute("SELECT COUNT(*) FROM order_item").fetchone()[0] == 0
    cid = orders.create_card_with_items(conn, {"client_name": "Антон"}, [dict(item, qty=4)], "2026-10-12T12:00:00", "owner")
    assert [i["qty"] for i in orders.get_card(conn, cid)["items"]] == [4]


def test_a_stale_window_does_not_wipe_what_the_system_wrote_after_the_page_was_opened(tmp_path, monkeypatch):
    """Review 2026-10-20: the window sends all its fields; only those the manager changed on the page are saved."""
    import html
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    cid = created(post(client, "/orders/new", tok, client_name="Иван", tags="срочно"))
    page = client.get("/orders/%d" % cid).get_data(as_text=True)
    was = html.unescape(re.search(r'name="was" value="([^"]*)"', page).group(1))
    orders.update_card(conn, cid, {"tags": "срочно, Появилось", "avito_chat": "Ч-7"}, "2026-10-12T12:30:00", "robot")   # the wait check, the chats
    talk = {s["code"]: s["id"] for s in orders.list_stages(conn)}["talk"]
    stale = dict(was=was, client_name="Иван Петров", tags="срочно", avito_chat="", client_phone="", comment="")      # what the open page still shows
    post(client, "/orders/%d/save" % cid, tok, stage_id=str(talk), **stale)
    c = orders.get_card(conn, cid)
    assert c["client_name"] == "Иван Петров" and c["tags"] == ["срочно", "Появилось"] and c["avito_chat"] == "Ч-7" and c["stage"]["code"] == "talk"
    page = client.get("/orders/%d" % cid).get_data(as_text=True)
    was = html.unescape(re.search(r'name="was" value="([^"]*)"', page).group(1))
    post(client, "/orders/%d/save" % cid, tok, was=was, client_name="Иван Петров", tags="Появилось", avito_chat="")   # what the manager did change is saved
    c = orders.get_card(conn, cid)
    assert c["tags"] == ["Появилось"] and c["avito_chat"] == ""
    post(client, "/orders/%d/save" % cid, tok, was="{not json", client_name="Иван")              # a broken mark: everything sent is saved, as before
    assert orders.get_card(conn, cid)["client_name"] == "Иван"


def test_tags_changed_on_a_stale_window_are_merged_with_the_tags_the_system_added(tmp_path, monkeypatch):
    """Review round 2: the manager's added and removed tags go over what is stored now; «Появилось» of the wait check stays."""
    import html
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    cid = created(post(client, "/orders/new", tok, client_name="Иван", tags="срочно"))

    def was():
        return html.unescape(re.search(r'name="was" value="([^"]*)"', client.get("/orders/%d" % cid).get_data(as_text=True)).group(1))
    shown = was()
    orders.update_card(conn, cid, {"tags": "срочно, Появилось"}, "2026-10-12T12:30:00", "robot")
    post(client, "/orders/%d/save" % cid, tok, was=shown, client_name="Иван", tags="срочно, новый")       # added «новый» on the old page
    assert orders.get_card(conn, cid)["tags"] == ["срочно", "Появилось", "новый"]
    shown = was()
    orders.update_card(conn, cid, {"tags": "срочно, Появилось, новый, Есть аналоги"}, "2026-10-12T12:40:00", "robot")
    post(client, "/orders/%d/save" % cid, tok, was=shown, client_name="Иван", tags="Появилось, новый")   # took «срочно» off on the old page
    assert orders.get_card(conn, cid)["tags"] == ["Появилось", "новый", "Есть аналоги"]


def test_a_tag_the_system_took_off_does_not_come_back_from_a_stale_window(tmp_path, monkeypatch):
    """Review round 3: the wait check swaps «Есть аналоги» for «Появилось»; the old page adds «новый» — the analogs tag is not brought back."""
    import html
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    cid = created(post(client, "/orders/new", tok, client_name="Иван", tags="Есть аналоги"))
    shown = html.unescape(re.search(r'name="was" value="([^"]*)"', client.get("/orders/%d" % cid).get_data(as_text=True)).group(1))
    orders.update_card(conn, cid, {"tags": "Появилось"}, "2026-10-12T12:30:00", "robot")
    post(client, "/orders/%d/save" % cid, tok, was=shown, client_name="Иван", tags="Есть аналоги, новый")
    assert orders.get_card(conn, cid)["tags"] == ["Появилось", "новый"]


ICE = dict(goods_id="101", query="IKON 195/65/15 T 91 Autograph Ice 9 · 195/65 R15", qty="2", sale_unit="4800")      # the row of «Позиции», chosen in the list


def test_save_adds_the_position_chosen_in_the_row_together_with_the_fields(tmp_path, monkeypatch):
    """The owner 2026-10-23 (order №428): a tyre chosen in «Позиции» and «Сохранить» without «+» was lost; «Сохранить» adds it with the rest."""
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    cid = created(post(client, "/orders/new", tok, client_name="Иван"))
    r = post(client, "/orders/%d/save" % cid, tok, client_name="Иван", comment="звонить после 18", **ICE)
    assert r.status_code == 302
    c = orders.get_card(conn, cid)
    assert [(i["goods_id"], i["qty"], i["sale_unit"]) for i in c["items"]] == [("101", 2, 4800.0)] and c["comment"] == "звонить после 18"
    assert "позиция добавлена" in client.get("/orders/%d" % cid).get_data(as_text=True)
    r = post(client, "/orders/%d/save" % cid, tok, client_name="Иван", comment="звонить после 18", goods_id="", query="", qty="4", sale_unit="")
    assert r.status_code == 302 and len(orders.get_card(conn, cid)["items"]) == 1                # the row is empty after the save: nothing twice
    post(client, "/orders/%d/save" % cid, tok, client_name="Иван", goods_id="", query="ICE ZERO", qty="1", sale_unit="")    # exact text: as «+»
    assert [i["goods_id"] for i in orders.get_card(conn, cid)["items"]] == ["101", "103"]
    assert "Добавлено по тексту" in client.get("/orders/%d" % cid).get_data(as_text=True)


def test_save_with_a_typed_but_not_chosen_position_keeps_the_fields_and_says_the_position_is_not_added(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    pid = points.save_point(conn, {"name": "Гамма"})
    cid = created(post(client, "/orders/new", tok, client_name="Иван", item_goods_id=["100"], item_qty=["4"]))
    placed = {s["code"]: s["id"] for s in orders.list_stages(conn)}["placed"]
    fields = dict(client_phone="+7 900 000 00 15", point_id=str(pid), delivery_date="2026-10-14", client_name="Иван", comment="звонить после 18")
    r = post(client, "/orders/%d/save" % cid, tok, stage_id=str(placed), goods_id="", query="ikon autograph", qty="1", sale_unit="", **fields)
    page = r.get_data(as_text=True)
    assert r.status_code == 200 and "Позиция не добавлена" in page and "Выберите" in page and 'value="ikon autograph"' in page
    assert "Заказ не перенесён" in page
    c = orders.get_card(conn, cid)
    assert c["comment"] == "звонить после 18" and c["point_id"] == pid and [i["goods_id"] for i in c["items"]] == ["100"]
    assert c["stage"]["code"] == "new"                                                           # the press meant both: no move without the position


def test_save_when_the_position_is_refused_says_what_was_saved_and_what_was_not(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    cid = created(post(client, "/orders/new", tok, client_name="Иван", item_goods_id=["100"], item_qty=["4"]))
    conn.execute("UPDATE order_card SET sale_ids = '[5]' WHERE id = ?", (cid,))                  # a recorded sale freezes the positions
    conn.commit()
    r = post(client, "/orders/%d/save" % cid, tok, client_name="Иван", comment="после продажи", **ICE)
    page = r.get_data(as_text=True)
    assert r.status_code == 200 and "Поля заказа сохранены" in page and "Позиция не добавлена" in page and "записана продажа" in page
    assert 'value="%s"' % ICE["query"] in page and 'value="4800"' in page
    c = orders.get_card(conn, cid)
    assert c["comment"] == "после продажи" and [i["goods_id"] for i in c["items"]] == ["100"]
    r = post(client, "/orders/%d/save" % cid, tok, client_phone="12", comment="другое", **ICE)    # wrong fields: nothing saved, the row stays typed
    page = r.get_data(as_text=True)
    assert r.status_code == 200 and "Позиция не добавлена: окно не сохранено" in page and 'value="%s"' % ICE["query"] in page
    assert orders.get_card(conn, cid)["comment"] == "после продажи" and len(orders.get_card(conn, cid)["items"]) == 1


def test_plus_adds_the_position_as_before_and_the_row_is_in_the_main_form(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    cid = created(post(client, "/orders/new", tok, client_name="Иван"))
    page = client.get("/orders/%d" % cid).get_data(as_text=True)
    main = page[page.index('<form id="ow-main"'):page.index("</form>", page.index('<form id="ow-main"'))]
    assert 'id="ow-item-add"' not in page and 'form="ow-item-add"' not in page
    assert 'name="goods_id" data-find-code' in main and 'name="query" placeholder=' in main and 'name="sale_unit" placeholder=' in main
    assert 'formaction="/orders/%d/items/add" formnovalidate' % cid in main                      # «+» skips the checks of the card's own fields
    assert re.search(r'<form id="ow-main"[^>]* data-once', main)                                   # a double press of «Сохранить» adds nothing twice
    r = post(client, "/orders/%d/items/add" % cid, tok, client_name="Пётр", comment="не сохраняется", **ICE)
    c = orders.get_card(conn, cid)
    assert r.headers["Location"].endswith("/orders/%d#items" % cid) and [i["goods_id"] for i in c["items"]] == ["101"]
    assert c["client_name"] == "Иван" and c["comment"] == ""                                     # «+» adds the position only, as before
    post(client, "/orders/%d/items/add" % cid, tok, goods_id="", query="", qty="4", sale_unit="")
    assert "Позиция не выбрана" in client.get("/orders/%d" % cid).get_data(as_text=True) and len(orders.get_card(conn, cid)["items"]) == 1


def test_a_new_order_takes_the_position_left_in_the_row_without_plus(tmp_path, monkeypatch):
    """The trap of the card is not in «Новый заказ»: its row is in the window's form, «Создать» takes it as one more position (13b of app.js too)."""
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    cid = created(post(client, "/orders/new", tok, client_name="Иван", item_goods_id=[ICE["goods_id"]], item_query=[ICE["query"]],
                       item_qty=[ICE["qty"]], item_sale_unit=[ICE["sale_unit"]]))
    assert [(i["goods_id"], i["qty"], i["sale_unit"]) for i in orders.get_card(conn, cid)["items"]] == [("101", 2, 4800.0)]
    page = client.get("/orders/new").get_data(as_text=True)
    row = page[page.index("data-item-row"):page.index("data-item-list")]
    assert 'name="item_goods_id" data-find-code' in row and 'form="' not in row


def test_a_code_without_the_text_is_not_a_choice_and_save_adds_nothing(tmp_path, monkeypatch):
    """The list fills the text with the code; a code alone is what a browser restored in the hidden field after «Назад» (review 2026-10-23)."""
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    cid = created(post(client, "/orders/new", tok, client_name="Иван"))
    r = post(client, "/orders/%d/save" % cid, tok, client_name="Иван", comment="x", goods_id="101", query="", qty="4", sale_unit="")
    assert r.status_code == 302 and orders.get_card(conn, cid)["items"] == [] and orders.get_card(conn, cid)["comment"] == "x"


OWIN_BACK_HARNESS = r"""
const fs = require("fs");
const handlers = {doc: {}, win: {}};
const anyEl = () => new Proxy(function () {}, {get: (t, k) => k === "length" ? 0 : (k === Symbol.iterator ? [][Symbol.iterator] : (k === "forEach" ? () => {} : anyEl())), apply: () => anyEl()});
const on = (box) => (t, f) => (box[t] = box[t] || []).push(f);
const attrs = () => { const a = {}; return {setAttribute: (k, v) => (a[k] = v), removeAttribute: (k) => delete a[k], has: (k) => k in a, disabled: false}; };
const windowForm = (isNew) => {
  const buttons = [attrs(), attrs()], fields = [{value: "101"}, {value: "Autograph Ice 9 · 195/65 R15"}, {value: "4800"}];
  return {dataset: {}, buttons, fields, matches: (s) => s === "form[data-once]",
    querySelector: (s) => s === "[data-item-new]" && isNew ? {} : null,
    querySelectorAll: (s) => s === "button" ? buttons : (s === "[data-find-code], [data-find-text], [data-find-price], [data-find-auto]" ? fields : [])};
};
const card = windowForm(false), fresh = windowForm(true);
let reloaded = 0, shown = card;
global.document = {addEventListener: on(handlers.doc), querySelectorAll: (s) => s === "form[data-owin]" ? [card, fresh] : [], querySelector: (s) => s === "form[data-owin]" ? shown : null, getElementById: () => null,
  body: anyEl(), documentElement: anyEl(), createElement: () => anyEl(), hidden: false, cookie: ""};
global.window = {addEventListener: on(handlers.win), location: {}, matchMedia: () => ({matches: false, addEventListener() {}}), fetch: () => new Promise(() => {}), FormData: function () {}};
const replaced = [];
global.location = {pathname: "/orders/5", reload: () => { reloaded += 1; }, replace: (u) => replaced.push(u)}; global.localStorage = {getItem: () => null, setItem() {}}; global.navigator = {};
global.MutationObserver = function () { return {observe() {}}; }; global.Event = function (t) { this.type = t; };
global.setTimeout = () => 0; global.clearTimeout = () => {};
try { eval(fs.readFileSync(process.argv[2], "utf8")); } catch (e) { /* later parts need more of a browser; the handlers are in place */ }
const submit = (form) => { const e = {target: form, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }};
  (handlers.doc.submit || []).forEach((f) => f(e)); return e; };
const out = {};
submit(card); submit(fresh);
out.blocked = submit(card).defaultPrevented && card.buttons.every((b) => b.has("aria-busy"));
(handlers.win.pageshow || []).forEach((f) => f({persisted: true}));
out.card_back = !card.dataset.sent && card.buttons.every((b) => !b.has("aria-busy")) && card.fields.every((f) => f.value === "");
out.card_sends = !submit(card).defaultPrevented;
out.new_stays = fresh.dataset.sent === "1" && fresh.fields[0].value === "101";
out.card_reloaded = reloaded === 1;                                       // the card of an order is read again (хвост З6)
shown = fresh; (handlers.win.pageshow || []).forEach((f) => f({persisted: true}));
out.new_not_reloaded = reloaded === 1;                                    // «Новый заказ» is never reloaded: what was typed stays
(handlers.win.pageshow || []).forEach((f) => f({persisted: false}));
out.fresh_load_alone = reloaded === 1;
shown = card; location.pathname = "/orders/5/save"; (handlers.win.pageshow || []).forEach((f) => f({persisted: true}));
out.post_opened_by_address = reloaded === 1 && replaced.join() === "/orders/5";   // the answer to a POST: no «resend the form?»
console.log(JSON.stringify(out));
"""


def test_after_back_the_card_window_sends_again_without_the_row_it_already_sent(tmp_path):
    """data-once on the card (2026-10-23) blocks a double press; after «Назад» the browser shows the sent window again: it must work, and the
    row it already sent is emptied so that one more «Сохранить» does not add the tyre twice. «Новый заказ» stays blocked: no second order."""
    import json
    import shutil
    import subprocess
    from pathlib import Path
    import pytest
    node = shutil.which("node")
    if not node:
        pytest.skip("no node")
    harness = tmp_path / "harness.js"
    harness.write_text(OWIN_BACK_HARNESS, encoding="utf-8")
    app_js = Path(__file__).resolve().parents[1] / "src" / "tires_sync" / "panel" / "static" / "app.js"
    result = json.loads(subprocess.run([node, str(harness), str(app_js)], capture_output=True, text=True, timeout=30, check=True).stdout.strip().splitlines()[-1])
    assert result == {"blocked": True, "card_back": True, "card_sends": True, "new_stays": True, "card_reloaded": True, "new_not_reloaded": True,
                      "fresh_load_alone": True, "post_opened_by_address": True}


def test_the_page_of_an_order_is_never_kept_by_the_browser(tmp_path, monkeypatch):
    """Хвост З6: after «Назад» the browser asks for the order again, so a tyre added by «+» before is in the list (no page from the cache)."""
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    cid = created(post(client, "/orders/new", tok, client_name="Иван"))
    assert client.get("/orders/%d" % cid).headers["Cache-Control"] == "no-store"
