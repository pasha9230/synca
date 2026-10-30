"""«Заказы» of the manager of a point (the owner 2026-10-30): only the point's own sales — an order of the point's Avito account or «Из
наличия точки», never ordered from WebMIM (the lead's default) — on four stages «Новый заказ», «Заказ оформлен», «Выполнено», «Отмена /
невыкуп»; a sale on another stage of the owner's is in the nearest column with its real stage named; no inbox, «Ещё», point filter nor
WebMIM; the point moves its own sale between the four only. A phone: the stages as chips and the sections at the bottom with counters.
The owner's board is not changed."""
from __future__ import annotations

import re

import pytest

from panel_helpers import login, make_app
from test_mypoint import manager
from tires_sync import order_points as points, orders, store, users

NOW = "2026-10-30T12:00:00"
ACCOUNT = 7


@pytest.fixture(autouse=True)
def cheap_hashes(monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")


def _order(conn, stages, point_id, code, name, **fields):
    cid = orders.create_card(conn, {"client_name": name, "point_id": str(point_id)}, NOW, "o")
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (stages[code], cid))
    for key, value in fields.items():
        conn.execute("UPDATE order_card SET %s = ? WHERE id = ?" % key, (value, cid))
    return cid


@pytest.fixture
def w(tmp_path):
    app = make_app(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, NOW)
    conn.execute("INSERT OR IGNORE INTO account (id, name, feed_token, created, chats_enabled) VALUES (?, 'Акк точки', 't7', '2026-10-01', 1)",
                 (ACCOUNT,))
    alfa = points.save_point(conn, {"name": "Альфа", "manager_name": "А", "channel": "panel"})
    beta = points.save_point(conn, {"name": "Бета", "manager_name": "Б", "channel": "panel"})
    conn.execute("UPDATE point SET avito_account_id = ? WHERE id = ?", (ACCOUNT, alfa))
    s = {st["code"]: st["id"] for st in orders.list_stages(conn)}
    made = {
        "stock_new": _order(conn, s, alfa, "new", "Свой новый", deal_kind="stock"),
        "stock_talk": _order(conn, s, alfa, "talk", "Свой переговоры", deal_kind="stock"),
        "stock_at": _order(conn, s, alfa, "at_point", "Свой на точке", deal_kind="stock"),
        "stock_done": _order(conn, s, alfa, "done", "Свой выполнен", deal_kind="stock"),
        "stock_oth": _order(conn, s, alfa, "oth", "Свой на ОТХ", deal_kind="stock"),
        "account": _order(conn, s, alfa, "placed", "Из аккаунта точки"),
        "ours": _order(conn, s, alfa, "moving", "Наш едет"),
        "ours_new": _order(conn, s, alfa, "new", "Наш новый"),
        "stock_webmim": _order(conn, s, alfa, "placed", "Свой с WebMIM", deal_kind="stock", webmim_number="WM-1"),
        "other_point": _order(conn, s, beta, "new", "Чужой точки", deal_kind="stock"),
    }
    conn.execute("INSERT INTO avito_chat (account_id, chat_id, peer_id, peer_name, order_id, unread) VALUES (?, 'c1', 'p1', 'К', ?, 0)",
                 (ACCOUNT, made["account"]))
    conn.commit()
    return dict(app=app, conn=conn, alfa=alfa, made=made, stages=s, point=manager(app, conn, alfa))


def _page(client, url="/orders"):
    return client.get(url).get_data(as_text=True)


def test_own_sales_are_the_accounts_orders_and_stock_never_ordered_from_webmim(w):
    m = w["made"]
    mine = points.own_sales(w["conn"], w["alfa"])
    assert {m["stock_new"], m["stock_talk"], m["stock_at"], m["stock_done"], m["stock_oth"], m["account"]} <= mine
    assert not mine & {m["ours"], m["ours_new"], m["stock_webmim"], m["other_point"]}
    w["conn"].execute("UPDATE order_card SET webmim_id = 55 WHERE id = ?", (m["account"],))         # an id of WebMIM is enough
    assert m["account"] not in points.own_sales(w["conn"], w["alfa"])


def test_the_point_sees_its_own_sales_on_four_stages_the_others_folded_with_the_real_stage(w):
    m = w["made"]
    page = _page(w["point"])
    shown = {int(x) for x in re.findall(r'data-card="(\d+)"', page)}
    assert shown == {m["stock_new"], m["stock_talk"], m["stock_at"], m["stock_done"], m["stock_oth"], m["account"]}
    names = re.findall(r'<a class="pt-chip[^"]*" href="/orders\?ost=([a-z]+)"', page)
    assert names == ["new", "placed", "done", "cancel"]
    for card, real in ((m["stock_talk"], "Переговоры"), (m["stock_at"], "Товар на точке"), (m["stock_oth"], "На ОТХ")):
        piece = page[page.index('data-card="%d"' % card):].split("</a>")[0]
        assert "data-real-stage" in piece and real in piece, card
    plain = page[page.index('data-card="%d"' % m["stock_new"]):].split("</a>")[0]
    assert "data-real-stage" not in plain


def test_the_point_has_no_inbox_more_menu_point_filter_nor_tiles(w):
    page = _page(w["point"])
    assert "Ещё ⋯" not in page and 'name="p"' not in page and "/orders/inbox" not in page and "tiles tiles--row" not in page
    assert "Продажи точки из своего наличия" in page and 'content="width=device-width' in page


def test_the_owner_board_is_unchanged(w):
    client = w["app"].test_client()
    login(client)
    page = _page(client)
    assert "Ещё ⋯" in page and 'name="p"' in page and "pt-desk" not in page and "data-point-nav" not in page
    assert 'data-card="%d"' % w["made"]["ours"] in page and "data-real-stage" not in page and 'content="width=1280' in page


def test_the_point_moves_its_own_sale_between_the_four_stages_only(w):
    m, s, conn = w["made"], w["stages"], w["conn"]
    r = w["point"].post("/orders/%d/move" % m["stock_new"], data={"csrf": "tok", "stage_id": s["talk"]})
    assert r.status_code in (302, 303)
    assert orders.get_card(conn, m["stock_new"])["stage_id"] == s["new"]
    r = w["point"].post("/orders/%d/move" % m["stock_new"], data={"csrf": "tok", "stage_id": s["talk"]}, follow_redirects=True)
    assert points.MOVE_REFUSED in r.get_data(as_text=True)
    w["point"].post("/orders/%d/move" % m["stock_talk"], data={"csrf": "tok", "stage_id": s["new"]})     # into the four: as before
    assert orders.get_card(conn, m["stock_talk"])["stage_id"] == s["new"]


def test_the_owner_still_moves_a_points_sale_to_any_stage(w):
    client = w["app"].test_client()
    login(client)
    m, s, conn = w["made"], w["stages"], w["conn"]
    client.post("/orders/%d/move" % m["stock_new"], data={"csrf": "tok", "stage_id": s["talk"]})
    assert orders.get_card(conn, m["stock_new"])["stage_id"] == s["talk"]


def test_the_phone_chips_show_the_cards_of_one_stage(w):
    m = w["made"]
    page = _page(w["point"], "/orders?ost=placed")
    phone = page[page.index("data-point-orders"):page.index('class="pt-desk"')]
    assert set(map(int, re.findall(r'data-pcard="(\d+)"', phone))) == {m["account"], m["stock_at"]}
    assert 'aria-current="page" data-ost="placed"' in phone
    first = _page(w["point"], "/orders?ost=nonsense")                                      # «Новый заказ» by default
    phone = first[first.index("data-point-orders"):first.index('class="pt-desk"')]
    assert set(map(int, re.findall(r'data-pcard="(\d+)"', phone))) == {m["stock_new"], m["stock_talk"]}


def test_the_sections_at_the_bottom_with_their_counters(w):
    for url, sec in (("/orders", "orders"), ("/my-point", "mypoint"), ("/my-point/handed", "handed")):
        page = _page(w["point"], url)
        nav = page[page.index("data-point-nav"):].split("</nav>")[0]
        assert re.findall(r'data-sec="([a-z]+)"', nav) == ["mypoint", "handed", "orders", "chats"], url
        assert re.search(r'aria-current="page" data-sec="%s"' % sec, nav), url
    counts = points_nav(w)
    assert counts["orders"] == 2                                                   # «Новый заказ» with the folded «Переговоры», as its chip
    assert counts["mypoint"] >= 1                                                  # «Наш едет» comes to the point


def points_nav(w):
    from tires_sync.panel.routes_mypoint import nav_counts
    with w["app"].app_context():
        return nav_counts(w["conn"], w["alfa"])


def test_the_owner_has_no_sections_at_the_bottom(w):
    client = w["app"].test_client()
    login(client)
    assert "data-point-nav" not in _page(client, "/my-point?p=%d" % w["alfa"])


def test_the_chat_moves_its_own_sale_between_the_four_only_and_shows_those(w):
    m, s, conn = w["made"], w["stages"], w["conn"]
    chat = conn.execute("SELECT id FROM avito_chat WHERE order_id = ?", (m["account"],)).fetchone()[0]
    page = _page(w["point"], "/chats/%d" % chat)
    select = page[page.index('name="stage_id"'):].split("</select>")[0]
    assert re.findall(r'<option value="(\d+)"', select) == [str(s[c]) for c in ("new", "placed", "done", "cancel")]
    w["point"].post("/chats/%d/stage" % chat, data={"csrf": "tok", "stage_id": s["moving"]})
    assert orders.get_card(conn, m["account"])["stage_id"] == s["placed"]


def test_own_sales_are_not_on_the_workplace_and_its_presses_refuse_them(w):
    m, conn = w["made"], w["conn"]
    page = "".join(_page(w["point"], "/my-point?tab=" + t) for t in ("here", "moving", "oth", "return"))
    assert 'id="order-%d"' % m["ours"] in page
    for own in (m["account"], m["stock_at"], m["stock_oth"]):
        assert 'id="order-%d"' % own not in page, own
    r = w["point"].post("/my-point/%d/issued" % m["stock_at"], data={"csrf": "tok", "token": "a" * 24}, follow_redirects=True)
    assert "продажа точки" in r.get_data(as_text=True) and orders.get_card(conn, m["stock_at"])["stage"]["code"] == "at_point"
    assert points_nav(w)["mypoint"] == 2                                      # «Наш едет» and «Свой с WebMIM» (ours) only


def test_the_phone_card_moves_forward_and_comes_back_to_its_stage(w):
    m, s, conn = w["made"], w["stages"], w["conn"]
    page = _page(w["point"], "/orders?ost=new")
    card = page[page.index('data-pcard="%d"' % m["stock_new"]):].split('class="mp-card pt-ocard"')[0]
    assert re.findall(r'data-move="([a-z]+)"', card) == ["placed", "done"] and "#finish" in card
    conn.execute("UPDATE order_card SET client_phone = '+79001112233', delivery_date = '2026-11-02' WHERE id = ?", (m["stock_new"],))
    conn.execute("INSERT INTO order_item (order_id, goods_id, name, qty, supplier_price, sale_unit) VALUES (?, 'G1', 'Шина', 4, 1000, 1500)", (m["stock_new"],))
    conn.commit()
    r = w["point"].post("/orders/%d/move" % m["stock_new"], data={"csrf": "tok", "stage_id": s["placed"], "ost": "new"})
    assert r.headers["Location"].endswith("/orders?ost=new")
    assert orders.get_card(conn, m["stock_new"])["stage"]["code"] == "placed"


def test_a_stage_the_owner_added_stands_by_its_kind(w):
    conn = w["conn"]
    extra = orders.add_stage(conn, "Своё ожидание", "#123456", "open", "", None)
    cid = _order(conn, w["stages"], w["alfa"], "new", "На своём этапе", deal_kind="stock")
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (extra, cid))
    conn.commit()
    page = _page(w["point"], "/orders?ost=new")
    phone = page[page.index("data-point-orders"):page.index('class="pt-desk"')]
    assert 'data-pcard="%d"' % cid in phone and "Своё ожидание" in phone


def test_the_cancel_window_of_an_own_sale_has_no_return(w):
    assert "Оформить возврат" not in _page(w["point"], "/orders/%d" % w["made"]["stock_new"])


# --- «Продажа точки»: the point edits its own sale (the owner 2026-10-30) -------------------------------------------------------------

def test_the_point_edits_the_client_the_date_and_the_comment_of_its_own_sale(w):
    m, conn = w["made"], w["conn"]
    page = _page(w["point"], "/orders/%d" % m["stock_new"])
    assert "data-point-sale" in page and 'action="/orders/%d/point-sale"' % m["stock_new"] in page
    r = w["point"].post("/orders/%d/point-sale" % m["stock_new"], data={"csrf": "tok", "client_name": "Пётр", "client_phone": "+7 900 111-22-33",
                                                                         "delivery_date": "2026-11-02", "comment": "к пятнице",
                                                                         "point_id": "999", "webmim_number": "777", "deal_kind": "webmim"})
    assert r.status_code == 302
    card = orders.get_card(conn, m["stock_new"])
    assert (card["client_name"], card["delivery_date"], card["comment"]) == ("Пётр", "2026-11-02", "к пятнице") and "900 111-22-33" in card["client_phone"]
    assert card["point_id"] == w["alfa"] and not card["webmim_number"] and card["deal_kind"] == "stock"     # nothing else of the order


def test_the_point_types_its_own_tyre_changes_it_and_removes_it_and_the_sum_follows(w):
    m, conn = w["made"], w["conn"]
    page = _page(w["point"], "/orders/%d" % m["stock_new"])
    block = page[page.index("data-point-sale"):page.index("</section>", page.index("data-point-sale"))]
    assert "data-point-tyre" in block and "data-item-find" not in block                    # typed, no catalog (the owner 2026-10-31)
    w["point"].post("/orders/%d/items/add" % m["stock_new"], data={"csrf": "tok", "name": "Nokian Hakka 10 205/55 R16", "qty": "4",
                                                                    "sale_unit": "5000", "goods_id": "101"})
    [item] = orders.get_card(conn, m["stock_new"])["items"]
    assert (item["goods_id"], item["name"], item["qty"], item["sale_unit"], item["supplier_price"]) == (orders.POINT_GOODS, "Nokian Hakka 10 205/55 R16", 4, 5000, 0)
    assert "20 000" in _page(w["point"], "/orders/%d" % m["stock_new"]).replace("\xa0", " ")
    w["point"].post("/orders/%d/items/%d/update" % (m["stock_new"], item["id"]), data={"csrf": "tok", "qty": "2", "sale_unit": "5100",
                                                                                      "supplier_price": "1"})
    item = orders.get_card(conn, m["stock_new"])["items"][0]
    assert (item["qty"], item["sale_unit"], item["supplier_price"]) == (2, 5100, 0)                # the purchase is not the point's
    page = _page(w["point"], "/orders/%d" % m["stock_new"])
    assert "data-buy" not in page and "Закупка" not in page and "10 200" in page.replace("\xa0", " ")
    w["point"].post("/orders/%d/items/%d/delete" % (m["stock_new"], item["id"]), data={"csrf": "tok"})
    assert orders.get_card(conn, m["stock_new"])["items"] == []


def test_the_point_has_no_catalog_nor_its_suggestions(w):
    assert w["point"].get("/catalog").status_code == 403
    assert w["point"].get("/orders/items/find?order=%d&q=205/55" % w["made"]["stock_new"]).status_code == 403
    assert 'href="/catalog"' not in _page(w["point"], "/orders")


@pytest.mark.parametrize("which", ["ours", "stock_webmim", "other_point"])
def test_the_point_edits_no_order_but_its_own_sale(w, which):
    m, conn = w["made"], w["conn"]
    cid = m[which]
    before = orders._row(conn, cid)["client_name"]
    assert w["point"].post("/orders/%d/point-sale" % cid, data={"csrf": "tok", "client_name": "Взлом"}).status_code == 403
    assert w["point"].post("/orders/%d/items/add" % cid, data={"csrf": "tok", "goods_id": "x", "query": "x", "qty": "1"}).status_code == 403
    assert orders._row(conn, cid)["client_name"] == before
    if which != "other_point":
        assert "data-point-sale" not in _page(w["point"], "/orders/%d" % cid)


def test_the_point_still_cannot_save_the_whole_window(w):
    m = w["made"]
    assert w["point"].post("/orders/%d/save" % m["stock_new"], data={"csrf": "tok", "client_name": "x"}).status_code == 403
    r = w["point"].post("/orders/%d/items/add" % m["stock_new"], data={"csrf": "tok", "was": "1", "point_id": "999", "query": ""})
    assert r.status_code == 302 and orders._row(w["conn"], m["stock_new"])["point_id"] == w["alfa"]       # «+» of the window: not the window's save


def test_the_move_without_tyres_phone_or_date_is_refused_plainly(w):
    m, s = w["made"], w["stages"]
    r = w["point"].post("/orders/%d/move" % m["stock_new"], data={"csrf": "tok", "stage_id": s["placed"]}, follow_redirects=True)
    assert "заполните: позиции, дата доставки, телефон клиента" in r.get_data(as_text=True)


def test_the_point_cannot_make_our_order_its_own_sale_by_linking_its_chat(w):
    m, conn = w["made"], w["conn"]
    chat = conn.execute("SELECT id FROM avito_chat WHERE order_id = ?", (m["account"],)).fetchone()[0]
    r = w["point"].post("/chats/%d/link" % chat, data={"csrf": "tok", "order_id": str(m["ours_new"])}, follow_redirects=True)
    assert "привязать к нему чат может только он" in r.get_data(as_text=True)
    assert m["ours_new"] not in points.own_sales(conn, w["alfa"])
    assert w["point"].post("/orders/%d/point-sale" % m["ours_new"], data={"csrf": "tok", "client_name": "x"}).status_code == 403


def test_the_point_cannot_reach_an_item_of_another_order_nor_ours(w):
    m, conn = w["made"], w["conn"]
    conn.execute("INSERT INTO order_item (order_id, goods_id, name, qty, supplier_price, sale_unit) VALUES (?, 'G1', 'Наша шина', 4, 1000, 1500)",
                 (m["ours_new"],))
    conn.commit()
    item = orders.get_card(conn, m["ours_new"])["items"][0]["id"]
    assert w["point"].post("/orders/%d/items/%d/update" % (m["stock_new"], item), data={"csrf": "tok", "qty": "1"}).status_code == 404
    assert w["point"].post("/orders/%d/items/%d/delete" % (m["stock_new"], item), data={"csrf": "tok"}).status_code == 404
    assert w["point"].post("/orders/%d/items/%d/update" % (m["ours_new"], item), data={"csrf": "tok", "qty": "1"}).status_code == 403
    assert w["point"].post("/orders/%d/items/%d/delete" % (m["ours_new"], item), data={"csrf": "tok"}).status_code == 403
    assert orders.get_card(conn, m["ours_new"])["items"][0]["qty"] == 4


def test_the_point_phone_of_its_sale_joins_no_client_of_the_crm(w):
    from tires_sync import clients
    m, conn = w["made"], w["conn"]
    w["point"].post("/orders/%d/point-sale" % m["stock_new"], data={"csrf": "tok", "client_phone": "+7 900 555-66-77"})
    assert clients.client_of_order(conn, m["stock_new"]) is None


def test_a_typed_tyre_makes_a_sale_of_the_account_one_of_the_points_stock_and_a_refusal_changes_nothing(w):
    m, conn = w["made"], w["conn"]
    assert orders._row(conn, m["account"])["deal_kind"] != "stock"
    w["point"].post("/orders/%d/items/add" % m["account"], data={"csrf": "tok", "name": "Шина с полки", "qty": "1"})       # no price
    assert orders.get_card(conn, m["account"])["items"] == [] and orders._row(conn, m["account"])["deal_kind"] != "stock"
    w["point"].post("/orders/%d/items/add" % m["account"], data={"csrf": "tok", "name": "Шина с полки", "qty": "1", "sale_unit": "3000"})
    assert [i["goods_id"] for i in orders.get_card(conn, m["account"])["items"]] == [orders.POINT_GOODS]
    assert orders._row(conn, m["account"])["deal_kind"] == "stock" and m["account"] in points.own_sales(conn, w["alfa"])

def test_the_sale_block_never_names_the_account_and_keeps_the_comment_it_did_not_change(w):
    m, conn = w["made"], w["conn"]
    conn.execute("UPDATE order_card SET comment = 'Звонок на «Акк точки»' WHERE id = ?", (m["stock_new"],))
    conn.commit()
    page = _page(w["point"], "/orders/%d" % m["stock_new"])
    block = page[page.index("data-point-sale"):page.index("</section>", page.index("data-point-sale"))]
    assert "Акк точки" not in block
    shown = re.search(r'<textarea class="input" name="comment"[^>]*>([^<]*)</textarea>', block).group(1)
    w["point"].post("/orders/%d/point-sale" % m["stock_new"], data={"csrf": "tok", "client_name": "Новое имя", "comment": shown})
    row = orders._row(conn, m["stock_new"])
    assert row["client_name"] == "Новое имя" and row["comment"] == "Звонок на «Акк точки»"


def test_the_points_move_of_its_own_sale_sends_no_can_pick_up(w, monkeypatch):
    from tires_sync import order_manager
    m, s, conn = w["made"], w["stages"], w["conn"]
    called = []
    monkeypatch.setattr(order_manager, "tell_client_arrived", lambda *a, **k: called.append(a) or (False, ""))
    conn.execute("UPDATE order_card SET client_phone = '+79001112233', delivery_date = '2026-11-02' WHERE id = ?", (m["stock_new"],))
    conn.execute("INSERT INTO order_item (order_id, goods_id, name, qty, supplier_price, sale_unit) VALUES (?, 'point', 'Шина', 4, 0, 1500)",
                 (m["stock_new"],))
    conn.commit()
    for code in ("placed", "done"):
        w["point"].post("/orders/%d/move" % m["stock_new"], data={"csrf": "tok", "stage_id": s[code]})
        assert orders.get_card(conn, m["stock_new"])["stage"]["code"] == code
    w["point"].post("/orders/%d/move" % m["stock_new"], data={"csrf": "tok", "stage_id": s["at_point"]})     # not the point's for its sale
    assert orders.get_card(conn, m["stock_new"])["stage"]["code"] == "done" and called == []


def _item(conn, cid, goods="G1", qty=4):
    conn.execute("INSERT INTO order_item (order_id, goods_id, name, qty, supplier_price, sale_unit) VALUES (?, ?, 'Шина', ?, ?, 1500)",
                 (cid, goods, qty, 0 if goods == orders.POINT_GOODS else 1000))
    conn.commit()


def test_an_order_placed_with_the_supplier_is_never_the_points_own_sale(w):
    """Review 2026-10-31: the number of 4 Точки («Оформил вручную»), or «Заказ оформлен» and later with the supplier's tyres (placed by
    hand without a number) — the tyres are coming: «Рабочее место»'s, not the point's to edit."""
    m, conn = w["made"], w["conn"]
    conn.execute("UPDATE order_card SET supplier_number = '4T-77' WHERE id = ?", (m["stock_new"],))
    _item(conn, m["account"])                                                      # «Заказ оформлен» with the supplier's tyre
    _item(conn, m["stock_done"])                                                   # «Выполнено»: closed, stays as it was
    _item(conn, m["stock_at"], goods=orders.POINT_GOODS)                           # the point's own tyre: still its sale
    mine = points.own_sales(conn, w["alfa"])
    assert m["stock_new"] not in mine and m["account"] not in mine
    assert m["stock_at"] in mine and m["stock_talk"] in mine and m["stock_done"] in mine
    assert w["point"].post("/orders/%d/items/add" % m["account"], data={"csrf": "tok", "name": "x", "qty": "1", "sale_unit": "1"}).status_code == 403
    assert 'id="order-%d"' % m["account"] in _page(w["point"], "/my-point?tab=moving") + _page(w["point"], "/my-point?tab=here")


def test_a_typed_tyre_never_turns_a_sale_with_the_suppliers_tyres_into_the_points_stock(w):
    m, conn = w["made"], w["conn"]
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (w["stages"]["new"], m["account"]))
    _item(conn, m["account"])                                                      # the manager put the supplier's tyre, not placed yet
    assert m["account"] in points.own_sales(conn, w["alfa"])
    r = w["point"].post("/orders/%d/items/add" % m["account"], data={"csrf": "tok", "name": "Шина с полки", "qty": "1", "sale_unit": "3000"},
                        follow_redirects=True)
    assert "заказ ведёт менеджер" in r.get_data(as_text=True)
    assert orders._row(conn, m["account"])["deal_kind"] != "stock" and len(orders.get_card(conn, m["account"])["items"]) == 1


def test_a_merged_order_is_never_the_points_own_sale(w):
    """Review 2026-10-31: a merge is the office's — neither its sources nor the merged order are the point's to edit or move."""
    m, conn = w["made"], w["conn"]
    merge = conn.execute("INSERT INTO order_merge (into_order, at, author) VALUES (?, ?, 'o')", (m["stock_new"], NOW)).lastrowid
    conn.execute("INSERT INTO order_merge_source (merge_id, order_id, stage_id) VALUES (?, ?, ?)", (merge, m["stock_talk"], w["stages"]["talk"]))
    conn.commit()
    mine = points.own_sales(conn, w["alfa"])
    assert m["stock_new"] not in mine and m["stock_talk"] not in mine and m["stock_done"] in mine
    assert w["point"].post("/orders/%d/point-sale" % m["stock_new"], data={"csrf": "tok", "client_name": "x"}).status_code == 403
    conn.execute("UPDATE order_merge SET undone_at = ? WHERE id = ?", (NOW, merge))                 # undone: the point's again
    conn.commit()
    assert {m["stock_new"], m["stock_talk"]} <= points.own_sales(conn, w["alfa"])


def test_the_point_cancelling_its_own_sale_on_the_point_is_not_asked_about_oth(w):
    m, s, conn = w["made"], w["stages"], w["conn"]
    r = w["point"].post("/orders/%d/move" % m["stock_at"], data={"csrf": "tok", "stage_id": s["cancel"], "cancel_kind": "отменил клиент"})
    assert "#oth-offer" not in r.headers["Location"]
    assert orders.get_card(conn, m["stock_at"])["stage"]["code"] == "cancel"
