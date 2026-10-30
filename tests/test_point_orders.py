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
