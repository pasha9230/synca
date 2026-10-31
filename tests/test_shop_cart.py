"""The client's cart and orders (the owner 2026-10-30, shop_cart.py): the cart of the own catalog, the checkout to «Передано с точки» with the
manager of the clients' orders, the message in Telegram, «Мои заказы» — and one client never reaching another's cart or orders."""
from __future__ import annotations

import json
import re

from cryptography.fernet import Fernet

from http_fakes import ScriptedHttp
from panel_helpers import login, post
from test_roles import sign_in
from test_shop import make_shop, page, set_ours
from tires_sync import connections, order_points, order_notify, orders, shop_cart, store, users
from tires_sync.secretbox import SecretBox

NOW = "2026-10-07T12:00:00"
PASSWORD = "длинный-пароль-1"
TG_OK = (200, {"ok": True, "result": {}})


def keys(html):
    """The opaque keys of «В корзину» in the order of the rows, with the row's price."""
    return re.findall(r'name="key" value="([0-9a-f]{32})"', html)


def shop(tmp_path, staff_chat=True):
    """The catalog of «buyer» (IKON 205/55 R16 at 13 580, stock 12; IKON 245/45 R18 at 65 190, stock 41), a point, the manager «zaman1»
    of the clients' orders (with a Telegram chat or without)."""
    app, client, conn, _ = make_shop(tmp_path)
    box = SecretBox(Fernet.generate_key())
    app.config["SECRET_BOX"] = box
    app.config["TG_HTTP"] = tg = ScriptedHttp(*[TG_OK] * 10)
    connections.set_config(conn, "telegram", 0, {"chat_id": "1000"}, NOW, "owner")
    connections.set_secret(conn, box, "telegram", 0, "bot_token", "123456:ABCdef-TEST", NOW, "owner")
    users.add_user(conn, "zaman1", PASSWORD, NOW, role="partner")
    if staff_chat:
        order_notify.set_user_chat(conn, "zaman1", "555", NOW, "owner")
    shop_cart.set_responsible(conn, "zaman1", NOW, "owner")
    pid = order_points.save_point(conn, {"name": "Бета", "address": "ул. Шинная, 1"})
    store.set_state(conn, "public_base", "https://panel.example.test")
    conn.commit()
    return app, client, conn, tg, pid


def add(client, key, qty="2", next_="/shop"):
    return post(client, "/shop/cart/add", "tok", key=key, qty=qty, next=next_)


def checkout(client, **form):
    data = dict(phone="+7 900 111-22-33", address="г. Тест, ул. Первая, 1", way="delivery", comment="к пятнице")
    data.update(form)
    return post(client, "/shop/checkout", "tok", **data)


def second_client(app, conn, name="buyer2"):
    users.add_user(conn, name, PASSWORD, NOW, role="client")
    conn.commit()
    other = app.test_client()
    sign_in(other, conn, name)
    return other


# ---- the cart -----------------------------------------------------------------------------------------------------------------------

def test_the_catalog_puts_a_tyre_in_the_cart_by_its_opaque_key_and_never_above_the_stock(tmp_path):
    app, client, conn, tg, pid = shop(tmp_path)
    html = page(client)
    first = keys(html)[0]
    assert "77001" not in html and "SECRETCODE" not in html                          # the key is no goods id, no code
    r = add(client, first, qty="5")
    assert r.status_code == 302 and r.headers["Location"] == "/shop"
    add(client, first, qty="100")                                                      # 5 + 100 > 12 in stock: the stock
    assert shop_cart.cart(conn, "buyer") == {"77001": 12}
    cart = page(client, "/shop/cart")
    assert "13 580" in cart.replace("\xa0", " ").replace(" ", " ") and "12345" not in cart and "77001" not in cart
    assert 'href="/shop/cart"' in cart and '<b class="shop-count">1</b>' in cart


def test_a_key_of_no_position_or_without_a_price_puts_nothing(tmp_path):
    app, client, conn, tg, pid = shop(tmp_path)
    assert add(client, "0" * 32).status_code == 302 and shop_cart.cart(conn, "buyer") == {}
    from tires_sync.panel import client_catalog as cc
    set_ours(conn, {"IKON TYRES|winter": (10, 20)})                                   # Laufenn: no % of ours nor of the client
    add(client, cc.photo_token(app.secret_key, "77003"))
    assert shop_cart.cart(conn, "buyer") == {}


def test_the_cart_changes_and_empties(tmp_path):
    app, client, conn, tg, pid = shop(tmp_path)
    k = keys(page(client))[0]
    add(client, k, qty="4")
    assert post(client, "/shop/cart/set", "tok", key=k, qty="3").status_code == 302 and shop_cart.cart(conn, "buyer") == {"77001": 3}
    post(client, "/shop/cart/set", "tok", key=k, drop="1")
    assert shop_cart.cart(conn, "buyer") == {} and "Корзина пуста" in page(client, "/shop/cart")


# ---- the checkout ------------------------------------------------------------------------------------------------------------------

def test_the_order_lands_on_from_point_with_the_manager_the_clients_price_and_the_source(tmp_path):
    app, client, conn, tg, pid = shop(tmp_path)
    ks = keys(page(client))
    add(client, ks[0], qty="4")
    add(client, ks[1], qty="2")
    r = checkout(client, way="point", point_id=str(pid))
    assert r.status_code == 302
    card_id = int(r.headers["Location"].rsplit("/", 1)[1])
    card = orders.get_card(conn, card_id)
    assert card["stage"]["code"] == "from_point" and card["responsible"] == "zaman1" and card["source"] == "shop"
    assert card["point_id"] == pid and card["client_phone"] == "+79001112233" and "Самовывоз: Бета" in card["comment"]
    assert sorted((i["goods_id"], i["qty"], i["sale_unit"]) for i in card["items"]) == [("77001", 4, 13580), ("77002", 2, 65190)]
    assert all(i["fee_pct"] == 0 and i["supplier_price"] > 0 for i in card["items"])  # the unit economy has the purchase; no fee of Avito
    assert shop_cart.cart(conn, "buyer") == {} and shop_cart.profile(conn, "buyer")["address"] == "г. Тест, ул. Первая, 1"
    mine = page(client, r.headers["Location"])
    assert "Заказ №%d принят, менеджер свяжется" % card_id in mine and "Принят" in mine
    for secret in ("12345", "54321", "77001", "SECRETCODE", "zaman1", "Передано с точки", "supplier"):
        assert secret not in mine, secret


def test_the_contacts_are_needed_at_the_first_order_and_kept_for_the_next(tmp_path):
    app, client, conn, tg, pid = shop(tmp_path)
    add(client, keys(page(client))[0])
    r = checkout(client, phone="", address="")
    assert r.status_code == 400 and "Телефон" in r.get_data(as_text=True) and shop_cart.cart(conn, "buyer")
    assert checkout(client, address="ул").status_code == 400                           # too short an address
    assert checkout(client, way="").status_code == 400                                 # neither the point nor the delivery
    assert checkout(client, way="point", point_id="999").status_code == 400            # no such point
    assert not conn.execute("SELECT COUNT(*) FROM shop_order").fetchone()[0]
    assert checkout(client).status_code == 302
    add(client, keys(page(client))[0])
    cart = page(client, "/shop/cart")
    assert 'value="+79001112233"' in cart and 'value="г. Тест, ул. Первая, 1"' in cart
    assert checkout(client).status_code == 302                                         # a second open order of the same phone: allowed
    assert conn.execute("SELECT COUNT(*) FROM shop_order").fetchone()[0] == 2
    add(client, keys(page(client))[0])
    assert checkout(client, phone="8 900 111 22 44").status_code == 302                # the phone changed at the checkout: kept
    assert shop_cart.profile(conn, "buyer")["phone"] == "+79001112244"


def test_a_second_press_makes_one_order(tmp_path):
    app, client, conn, tg, pid = shop(tmp_path)
    add(client, keys(page(client))[0])
    assert checkout(client).status_code == 302
    r = checkout(client)
    assert r.status_code == 302 and r.headers["Location"] == "/shop/orders"                # the order is there: no error, its list
    assert "оформленный заказ" in page(client, "/shop/orders")
    assert conn.execute("SELECT COUNT(*) FROM shop_order").fetchone()[0] == 1


def test_the_price_is_taken_anew_at_the_checkout_and_a_tyre_no_longer_sold_stops_it(tmp_path):
    app, client, conn, tg, pid = shop(tmp_path)
    ks = keys(page(client))
    add(client, ks[0])
    set_ours(conn, {"IKON TYRES|winter": (20, 20), "LAUFENN|summer": (10, 20)})       # our % changed after the cart
    r = checkout(client, price="1")                                                   # a price in the form means nothing
    card = orders.get_card(conn, int(r.headers["Location"].rsplit("/", 1)[1]))
    assert [i["sale_unit"] for i in card["items"]] == [14820]                         # 12345 + 20 %, rounded to 10 as the catalog
    add(client, ks[1])
    set_ours(conn, {})
    r = checkout(client)
    assert r.status_code == 400 and "нет в наличии" in r.get_data(as_text=True)
    assert conn.execute("SELECT COUNT(*) FROM shop_order").fetchone()[0] == 1


def test_above_the_stock_the_order_is_refused_whole(tmp_path):
    app, client, conn, tg, pid = shop(tmp_path)
    add(client, keys(page(client))[0])
    conn.execute("UPDATE shop_cart SET qty = 50")                                     # more than the 12 of today
    conn.commit()
    r = checkout(client)
    assert r.status_code == 400 and "в наличии 12 шт." in r.get_data(as_text=True)
    assert not conn.execute("SELECT COUNT(*) FROM shop_order").fetchone()[0] and shop_cart.cart(conn, "buyer") == {"77001": 50}


# ---- the message --------------------------------------------------------------------------------------------------------------------

def test_the_manager_gets_the_order_in_telegram(tmp_path):
    app, client, conn, tg, pid = shop(tmp_path)
    add(client, keys(page(client))[0], qty="2")
    r = checkout(client)
    card_id = int(r.headers["Location"].rsplit("/", 1)[1])
    sent = [c for c in tg.calls if "sendMessage" in c[1]]
    assert len(sent) == 1
    body = json.loads(sent[0][3])
    assert body["chat_id"] == "555"
    body = body["text"]
    assert "Заказ №%d из каталога клиента" % card_id in body and "https://panel.example.test/orders/%d" % card_id in body and "27 160" in body
    journal = [e["text"] for e in orders.list_events(conn, card_id)]
    assert any("Оповещение отправлено" in t for t in journal)


def test_without_a_chat_the_order_is_made_and_the_journal_says_so(tmp_path):
    app, client, conn, tg, pid = shop(tmp_path, staff_chat=False)
    add(client, keys(page(client))[0])
    r = checkout(client)
    assert r.status_code == 302
    card_id = int(r.headers["Location"].rsplit("/", 1)[1])
    assert any("Оповещение не отправлено" in e["text"] and "Telegram" in e["text"] for e in orders.list_events(conn, card_id))


# ---- one client, one cart, own orders ---------------------------------------------------------------------------------------------

def test_a_client_never_reaches_another_clients_cart_nor_orders(tmp_path):
    app, client, conn, tg, pid = shop(tmp_path)
    k = keys(page(client))[0]
    add(client, k, qty="3")
    r = checkout(client)
    mine_id = int(r.headers["Location"].rsplit("/", 1)[1])
    add(client, k, qty="1")
    other = second_client(app, conn)
    assert other.get("/shop/orders/%d" % mine_id).status_code == 404
    assert "№ %d" % mine_id not in page(other, "/shop/orders")
    assert post(other, "/shop/cart/set", "tok", key=k, drop="1").status_code == 404  # the key of a tyre not in the own cart
    assert shop_cart.cart(conn, "buyer") == {"77001": 1}
    staff_order = orders.create_card(conn, {"client_name": "Иван", "client_phone": "+7 900 222 33 44"}, NOW, "owner")
    assert client.get("/shop/orders/%d" % staff_order).status_code == 404             # an order not of the catalog
    assert client.get("/shop/orders/abc").status_code == 404


def test_the_staff_and_the_point_have_no_cart(tmp_path):
    app, client, conn, tg, pid = shop(tmp_path)
    owner = app.test_client()
    login(owner)
    for url in ("/shop/cart", "/shop/orders"):
        assert owner.get(url).status_code == 403
    assert post(owner, "/shop/checkout", "tok").status_code == 403
    users.add_user(conn, "pt1", PASSWORD, NOW, role="point", point_id=pid)
    conn.commit()
    pt = app.test_client()
    sign_in(pt, conn, "pt1")
    assert pt.get("/shop/cart").status_code == 403


def test_the_pages_of_the_cart_keep_the_strict_frame(tmp_path):
    app, client, conn, tg, pid = shop(tmp_path)
    for url in ("/shop/cart", "/shop/orders"):
        response = client.get(url)
        assert "script-src" not in response.headers["Content-Security-Policy"] and "<script" not in response.get_data(as_text=True)
        assert response.headers["Cache-Control"] == "private, no-store"


# ---- the owner's choices ------------------------------------------------------------------------------------------------------------

def test_the_manager_of_the_clients_orders_is_the_owners_choice_and_the_page_says_when_none(tmp_path):
    app, client, conn, tg, pid = shop(tmp_path)
    owner = app.test_client()
    login(owner)
    assert 'option value="zaman1" selected' in page(owner, "/users") and "data-shop-resp-hint" not in page(owner, "/users")
    assert post(owner, "/users/shop-responsible", "tok", login="").status_code == 302
    assert shop_cart.responsible(conn) == "owner" and "data-shop-resp-hint" in page(owner, "/users")     # no default either: the owner
    for wrong in ("buyer", "no-such-person"):
        post(owner, "/users/shop-responsible", "tok", login=wrong)
        assert shop_cart.responsible_setting(conn) == ""
    post(owner, "/users/shop-responsible", "tok", login="zaman1")
    users.set_active(conn, "zaman1", False, NOW, "owner")
    assert shop_cart.responsible(conn) == "owner"                                      # switched off: the default (the owner), the hint
    assert "data-shop-resp-hint" in page(owner, "/users")
    assert post(client, "/users/shop-responsible", "tok", login="").status_code == 403


def test_the_invitation_shows_the_point_and_the_terms_by_role_and_keeps_a_point_for_a_manager_of_a_point_only(tmp_path):
    app, client, conn, tg, pid = shop(tmp_path)
    owner = app.test_client()
    login(owner)
    html = page(owner, "/users")
    assert 'class="row inv-form"' in html and 'class="field inv-point"' in html and 'class="field inv-terms"' in html
    assert ".inv-form:has(select[name=role] option[value=point]:checked) .inv-point" in html
    for role in ("client", "viewer", "partner"):
        post(owner, "/users/invite", "tok", role=role, point_id=str(pid), display_name="Тест " + role)
    post(owner, "/users/invite", "tok", role="point", point_id=str(pid), display_name="Тест точка")
    kept = dict(conn.execute("SELECT role, point_id FROM panel_invite").fetchall())
    assert kept == {"client": None, "viewer": None, "partner": None, "point": pid}


def test_a_cart_changed_in_another_tab_after_the_page_is_not_ordered(tmp_path):
    app, client, conn, tg, pid = shop(tmp_path)
    add(client, keys(page(client))[0], qty="2")
    view_lines = shop_cart.lines(conn, "buyer", lambda gids: {})                     # what one tab read (the item is checked below)
    conn.execute("UPDATE shop_cart SET qty = 5")                                       # the other tab changed it
    conn.commit()
    from tires_sync.panel import client_catalog as cc
    item = cc.Visible(type("O", (), {"stock": 12, "goods_id": "77001"})(), 13580, 1, "ikon", "Ikon")
    stale = [shop_cart.Line(l.goods_id, l.qty, item) for l in view_lines]
    try:
        shop_cart.place(conn, "buyer", "Покупатель", stale, {"phone": "+79001112233", "address": "ул. Первая, 1"}, None, "", NOW)
        raise AssertionError("ordered a cart that changed")
    except shop_cart.CartError as err:
        assert "изменилась" in str(err)
    assert shop_cart.cart(conn, "buyer") == {"77001": 5} and not conn.execute("SELECT COUNT(*) FROM shop_order").fetchone()[0]


def test_a_failure_while_writing_the_order_keeps_the_cart(tmp_path, monkeypatch):
    app, client, conn, tg, pid = shop(tmp_path)
    add(client, keys(page(client))[0], qty="3")
    conn.execute("DELETE FROM order_stage WHERE code = 'from_point'")                 # the stage is made on the first order
    conn.commit()

    def broken(*args, **kwargs):
        raise orders.OrderError("Позиция не указана.")
    monkeypatch.setattr(orders, "add_item", broken)
    r = checkout(client)
    assert r.status_code == 400
    assert shop_cart.cart(conn, "buyer") == {"77001": 3} and not conn.execute("SELECT COUNT(*) FROM order_card").fetchone()[0]


def test_the_link_in_the_message_is_never_the_host_the_client_sent(tmp_path):
    app, client, conn, tg, pid = shop(tmp_path)
    add(client, keys(page(client))[0])
    card_id = int(checkout(client).headers["Location"].rsplit("/", 1)[1])
    from tires_sync.panel import routes_shop
    with app.test_request_context("/shop/checkout", base_url="http://evil.example"):          # a Host the client's browser chose
        routes_shop._after_order(app, conn, card_id, "Покупатель", NOW)
    body = json.loads([c for c in tg.calls if "sendMessage" in c[1]][-1][3])["text"]
    assert "evil.example" not in body and "https://panel.example.test/orders/%d" % card_id in body
    store.set_state(conn, "public_base", "")
    conn.commit()
    add(client, keys(page(client))[0])
    checkout(client)
    body = json.loads([c for c in tg.calls if "sendMessage" in c[1]][-1][3])["text"]
    assert "/orders/" not in body and "localhost" not in body                          # no address known: the number only


def test_a_price_changed_since_the_cart_was_shown_is_not_ordered_silently(tmp_path):
    app, client, conn, tg, pid = shop(tmp_path)
    add(client, keys(page(client))[0], qty="2")
    seen = re.search(r'name="total" value="(\d+)"', page(client, "/shop/cart")).group(1)
    assert seen == "27160"
    set_ours(conn, {"IKON TYRES|winter": (20, 20), "LAUFENN|summer": (10, 20)})
    r = checkout(client, total=seen)
    assert r.status_code == 400 and "Цены изменились" in r.get_data(as_text=True) and shop_cart.cart(conn, "buyer")
    assert checkout(client, total=re.search(r'name="total" value="(\d+)"', page(client, "/shop/cart")).group(1)).status_code == 302


def test_a_line_holds_at_most_what_an_order_line_does(tmp_path):
    app, client, conn, tg, pid = shop(tmp_path)
    k = keys(page(client))[1]                                                         # 41 in stock
    add(client, k, qty="150")
    assert shop_cart.cart(conn, "buyer") == {}
    add(client, k, qty="0")
    assert shop_cart.cart(conn, "buyer") == {}
    assert shop_cart.MAX_QTY == 100 and 'max="100"' in page(client)


def test_without_any_choice_the_order_is_the_owners_never_the_clients(tmp_path):
    app, client, conn, tg, pid = shop(tmp_path)
    shop_cart.set_responsible(conn, "", NOW, "owner")
    add(client, keys(page(client))[0])
    r = checkout(client)
    card = orders.get_card(conn, int(r.headers["Location"].rsplit("/", 1)[1]))
    assert card["responsible"] == order_notify.get_owner_login(conn) != "buyer"


def test_a_point_of_the_tests_is_not_offered(tmp_path):
    app, client, conn, tg, pid = shop(tmp_path)
    order_points.save_point(conn, {"name": "Тестовая точка"})
    conn.commit()
    add(client, keys(page(client))[0])
    html = page(client, "/shop/cart")
    assert "Бета" in html and "Тестовая точка" not in html


def test_a_position_a_manager_added_shows_no_code_to_the_client(tmp_path):
    app, client, conn, tg, pid = shop(tmp_path)
    add(client, keys(page(client))[0])
    card_id = int(checkout(client).headers["Location"].rsplit("/", 1)[1])
    orders.add_item(conn, card_id, goods_id="77002", code="SECRETCODE2", name="IKON 245/45/18 SECRETCODE2 Test Model", brand_key="ikon",
                    season="winter", diameter=18, territory="1", qty=1, sale_unit=65190, supplier_price=54321, terms={}, fee_pct=0,
                    promo_pct=0, now=NOW, author="zaman1")
    html = page(client, "/shop/orders/%d" % card_id)
    assert "SECRETCODE" not in html and "54321" not in html and "IKON 245/45/18 Test Model" in html
