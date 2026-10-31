"""The client in the new design (the owner 2026-10-30, the prototype renderClientDesk / renderClient / clProfile): the sections
«Каталог», «Корзина», «Мои заказы», «Профиль»; «Таблица» / «Плитка» by a link; «Профиль» (the name, the phone, the address of
delivery, the password) — one client's own only, the strict frame of /shop (no script) on every page of the client."""
from __future__ import annotations

import re

from panel_helpers import login, post
from test_shop import make_shop, page, prices
from test_shop_cart import add, checkout, keys, second_client
from tires_sync import shop_cart, users

STRICT = "default-src 'none'"


def test_the_four_sections_on_every_page_of_the_client(tmp_path):
    app, client, conn, _ = make_shop(tmp_path)
    for url, here in (("/shop", "/shop"), ("/shop/cart", "/shop/cart"), ("/shop/orders", "/shop/orders"), ("/shop/profile", "/shop/profile")):
        html = page(client, url)
        nav = html[html.index('class="shop-nav"'):].split("</nav>")[0]
        assert re.findall(r'<a href="(/shop[a-z/]*)"', nav) == ["/shop", "/shop/cart", "/shop/orders", "/shop/profile"], url
        assert '<a href="%s" class="is-active" aria-current="page"' % here in nav, url


def test_the_tiles_are_a_link_and_the_links_keep_them(tmp_path):
    app, client, conn, _ = make_shop(tmp_path)
    table = page(client)
    assert 'class="table shop-table"' in table and "shop-tiles" not in table
    tiles = page(client, "/shop?v=tiles&o=price")
    assert 'class="shop-tiles"' in tiles and "shop-table" not in tiles
    assert sorted(prices(tiles)) == sorted(prices(table))                              # the same rows and prices
    assert 'name="v" value="tiles"' in tiles                                           # the filters keep the view
    sorts = re.findall(r'href="(/shop\?[^"]*o=size[^"]*)"', tiles)
    assert sorts and all("v=tiles" in s for s in sorts)
    assert len(keys(tiles)) == len(prices(tiles))                                      # «В корзину» in each tile


def test_a_row_in_the_cart_says_so(tmp_path):
    app, client, conn, _ = make_shop(tmp_path)
    add(client, keys(page(client))[0], qty="4")
    html = page(client)
    assert "в корзине: 4 шт." in html and ">Ещё</button>" in html


def test_the_profile_keeps_the_name_the_phone_and_the_address_and_the_checkout_offers_them(tmp_path):
    app, client, conn, _ = make_shop(tmp_path)
    html = page(client, "/shop/profile")
    assert 'action="/shop/profile"' in html and 'action="/account/password"' in html and "телефон не указан" in html
    r = post(client, "/shop/profile", "tok", name="ООО Шины", phone="+7 900 111-22-33", address="г. Тест, ул. Первая, 1")
    assert r.status_code == 302 and r.headers["Location"].endswith("/shop/profile")
    assert shop_cart.profile(conn, "buyer")["address"] == "г. Тест, ул. Первая, 1" and users.get_role(conn, "buyer")["display_name"] == "ООО Шины"
    add(client, keys(page(client))[0])
    cart = page(client, "/shop/cart")
    assert 'value="г. Тест, ул. Первая, 1"' in cart and "9001112233" in re.sub(r"\D", "", cart)
    r = post(client, "/shop/profile", "tok", name="ООО Шины", phone="12", address="г. Тест, ул. Первая, 1")
    assert r.status_code == 400 and "Телефон" in r.get_data(as_text=True)
    assert "9001112233" in shop_cart.profile(conn, "buyer")["phone"].replace(" ", "").replace("-", "")    # a refusal changes nothing


def test_one_client_never_reaches_another_profile_nor_orders(tmp_path):
    app, client, conn, _ = make_shop(tmp_path)
    post(client, "/shop/profile", "tok", name="Первый", phone="+7 900 111-22-33", address="г. Тест, ул. Первая, 1")
    add(client, keys(page(client))[0])
    order = int(checkout(client).headers["Location"].rsplit("/", 1)[1])
    other = second_client(app, conn)
    html = page(other, "/shop/profile")
    assert "Первая" not in html and "Первый" not in html and "111-22-33" not in html
    assert other.get("/shop/orders/%d" % order).status_code == 404 and "№ %d" % order not in page(other, "/shop/orders")
    post(other, "/shop/profile", "tok", name="Второй", phone="+7 900 333-44-55", address="г. Тест, ул. Вторая, 2")
    assert shop_cart.profile(conn, "buyer")["address"] == "г. Тест, ул. Первая, 1"
    assert users.get_role(conn, "buyer")["display_name"] == "Первый"


def test_the_staff_and_the_point_have_no_profile_of_the_client(tmp_path):
    app, client, conn, _ = make_shop(tmp_path)
    owner = app.test_client()
    login(owner)
    assert owner.get("/shop/profile").status_code == 403
    assert post(owner, "/shop/profile", "tok", name="x", phone="+7 900 111-22-33", address="г. Тест, ул. 1").status_code == 403


def test_every_page_of_the_client_has_the_strict_frame(tmp_path):
    app, client, conn, _ = make_shop(tmp_path)
    for url in ("/shop", "/shop?v=tiles", "/shop/cart", "/shop/orders", "/shop/profile"):
        response = client.get(url)
        csp = response.headers["Content-Security-Policy"]
        assert STRICT in csp and "script-src" not in csp and "<script" not in response.get_data(as_text=True), url
    r = post(client, "/shop/profile", "tok", name="x", phone="1", address="г")
    assert STRICT in r.headers["Content-Security-Policy"]


def test_the_password_of_the_profile_comes_back_to_it(tmp_path):
    app, client, conn, _ = make_shop(tmp_path)
    r = post(client, "/account/password", "tok", old="длинный-пароль-1", new="новый-длинный-1", new2="другой-длинный")
    assert r.headers["Location"].endswith("/shop/profile#password")
    r = post(client, "/account/password", "tok", old="длинный-пароль-1", new="новый-длинный-1", new2="новый-длинный-1")
    assert r.headers["Location"].endswith("/shop/profile")
    assert users.verify(conn, "buyer", "новый-длинный-1")


def test_a_client_names_itself_only(tmp_path):
    app, client, conn, _ = make_shop(tmp_path)
    users.add_user(conn, "boss", "длинный-пароль-1", "2026-10-07T12:00:00", role="owner", display_name="Босс")
    import pytest
    with pytest.raises(users.UserError):
        users.set_own_name(conn, "boss", "Другое", "2026-10-07T12:00:00")
    with pytest.raises(users.UserError):
        users.set_own_name(conn, "buyer", "   ", "2026-10-07T12:00:00")
    assert users.get_role(conn, "boss")["display_name"] == "Босс"


def test_a_refused_profile_changes_neither_the_name_nor_the_contacts(tmp_path):
    app, client, conn, _ = make_shop(tmp_path)
    post(client, "/shop/profile", "tok", name="Первый", phone="+7 900 111-22-33", address="г. Тест, ул. Первая, 1")
    r = post(client, "/shop/profile", "tok", name="Новое имя", phone="12", address="г. Тест, ул. Первая, 1")
    assert r.status_code == 400 and users.get_role(conn, "buyer")["display_name"] == "Первый"
    r = post(client, "/shop/profile", "tok", name="  ", phone="+7 900 333-44-55", address="г. Тест, ул. Вторая, 2")
    assert r.status_code == 400 and shop_cart.profile(conn, "buyer")["address"] == "г. Тест, ул. Первая, 1"


def test_the_point_has_no_profile_of_the_client(tmp_path):
    from tires_sync import order_points, orders
    from test_mypoint import manager
    app, client, conn, _ = make_shop(tmp_path)
    orders.ensure_pipeline(conn, "2026-10-07T12:00:00")
    pid = order_points.save_point(conn, {"name": "Альфа", "manager_name": "А", "channel": "panel"})
    point = manager(app, conn, pid)
    assert point.get("/shop/profile").status_code == 403
    assert post(point, "/shop/profile", "tok", name="x", phone="+7 900 111-22-33", address="г. Тест, ул. 1").status_code == 403


def test_the_owners_preview_has_the_new_design_and_no_cart(tmp_path):
    app, client, conn, _ = make_shop(tmp_path)
    owner = app.test_client()
    login(owner)
    html = page(owner, "/client-terms/c/buyer/preview")
    assert "/static/shop.css" in html and 'class="shop-cat"' in html and "/shop/cart/add" not in html and 3670 in prices(html)


def test_nothing_priced_is_a_catalog_not_set_up(tmp_path):
    app, client, conn, _ = make_shop(tmp_path, role=(None, None), rows_of=(("laufenn", 5, 0, 5, 0),))
    html = page(client)
    assert "Ассортимент ещё не настроен" in html and prices(html) == []


def test_the_journal_says_the_old_and_the_new_name(tmp_path):
    import json
    from tires_sync.panel import texts
    app, client, conn, _ = make_shop(tmp_path)
    post(client, "/shop/profile", "tok", name="Первый", phone="+7 900 111-22-33", address="г. Тест, ул. Первая, 1")
    post(client, "/shop/profile", "tok", name="Второй", phone="+7 900 111-22-33", address="г. Тест, ул. Первая, 1")
    detail = json.loads(conn.execute("SELECT detail FROM audit WHERE action = 'client_own_name' ORDER BY id DESC").fetchone()[0])
    assert (detail["from"], detail["to"]) == ("Первый", "Второй")
    line = texts.audit_text({"action": "client_own_name", "detail": detail, "author": "buyer", "at": "2026-10-07T12:00:00"})
    assert "Первый" in line and "Второй" in line and "buyer" in line
