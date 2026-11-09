"""«Новый вид» of the client (the owner 2026-11-02, port of the prototype + the owner's edits of 2026-10-31 п. 20-29): the pages of
templates/shop2/ only when «Новый вид» of the group «Клиент» is on for the viewer (all → the client; owner_only → the owner's preview
«Каталог глазами клиента», never the client); off — the old pages exactly. The new checkout makes the same order as the old one; the
delivery (the city, the street, the house, the recipient, a word for the driver) is kept in the client's own profile only."""
from __future__ import annotations

import pytest

import re

from panel_helpers import login, post
from test_shop import make_shop, page, prices
from test_shop_cart import add, checkout, keys, second_client, shop
from test_tour import select
from tires_sync import orders, shop_cart, store, users
from tires_sync.panel import tour

DELIVERY = dict(city="Тест", street="ул. Первая", house="1", recipient="Иннокентий", recipient_phone="+7 900 222-33-44", driver_note="калитка синяя")


def new_view(conn, state="all"):
    store.set_state(conn, "ui_new.client", state)
    conn.commit()


def order_id(r):
    return int(re.search(r"/shop/orders/(\d+)", r.headers["Location"]).group(1))


# ---- off: the old pages, exactly ------------------------------------------------------------------------------------------------------

def test_off_every_page_of_the_client_is_the_old_one(tmp_path):
    app, client, conn, tg, pid = shop(tmp_path)
    for state in ("off", "owner_only"):                                    # «только у владельца»: the client still has the old pages
        new_view(conn, state)
        cat = page(client)
        assert 'class="o2 shop-o2f"' in cat and "shop2" not in cat and "ui-new" not in cat
        add(client, keys(cat)[0])
        cart = page(client, "/shop/cart")
        assert 'name="address"' in cart and "shop2" not in cart and 'name="city"' not in cart
        prof = page(client, "/shop/profile")
        assert 'name="address"' in prof and 'name="part"' not in prof and "shop2" not in prof
        assert "shop2" not in page(client, "/shop/orders")
    r = checkout(client, way="point", point_id=str(pid))
    assert r.headers["Location"].endswith("/shop/orders/%d" % order_id(r))    # no «placed» of the new view
    assert "shop2" not in page(client, r.headers["Location"])


def test_off_the_owners_preview_is_the_old_one_and_owner_only_shows_it_the_new_one(tmp_path):
    app, client, conn, _ = make_shop(tmp_path)
    owner = app.test_client()
    login(owner)
    old = page(owner, "/client-terms/c/buyer/preview")
    assert 'class="o2 shop-o2f"' in old and "shop2" not in old
    new_view(conn, "owner_only")
    html = page(owner, "/client-terms/c/buyer/preview")
    assert "shop2-cat" in html and 'name="xb"' not in html and " ui-new" in html
    assert sorted(prices(html)) == sorted(prices(old))                     # the same rows and prices
    assert 'action="/shop/cart/add"' not in html                           # the preview never buys
    assert "shop2" not in page(client)                                       # the client: still the old page


# ---- on: the new pages -----------------------------------------------------------------------------------------------------------------

def test_on_the_catalog_has_the_size_chips_one_search_no_except_and_the_photo_cell(tmp_path):
    app, client, conn, _ = make_shop(tmp_path)
    new_view(conn)
    html = page(client)
    assert "shop2-cat" in html and 'name="xb"' not in html and 'id="f-size"' not in html
    for key, values in (("w", "195 205 245"), ("h", "45 55 65"), ("d", "15 16 18")):        # the hints of the boxes (2026-11-09: as the office's)
        hints = re.search(r'<datalist id="dl-%s">(.*?)</datalist>' % key, html).group(1)
        assert re.findall(r'value="(\d+)"', hints) == values.split(), key
    assert 'name="w"' in html and 'name="q"' in html and html.count('name="q"') == 1
    assert html.count('class="c-photo"') == len(prices(html)) and "<script" not in html
    assert len(set(keys(html))) == len(prices(html))                       # «В корзину» in each row (and in its card)


def test_on_several_widths_and_radii_at_once(tmp_path):
    app, client, conn, _ = make_shop(tmp_path)
    new_view(conn)
    assert sorted(prices(page(client, "/shop?w=205&w=195"))) == [3670, 13580]
    assert sorted(prices(page(client, "/shop?d=R16&d=18"))) == [13580, 65190]
    assert prices(page(client, "/shop?w=205&h=65")) == []
    html = page(client, "/shop?w=205&d=16")
    assert "ширина 205" in html and "R16" in html and 'href="/shop?d=16"' in html     # a chip on: a link without it
    assert "Сбросить" in html


def test_on_the_form_as_a_browser_sends_it_with_every_filter_empty(tmp_path):
    app, client, conn, _ = make_shop(tmp_path)
    new_view(conn)
    html = page(client, "/shop?o=brand&w=205&s=&t=&wh=&q=&wh=0")
    assert sorted(prices(html)) == [13580] and "ширина 205" in html and "Склад 0" not in html
    html = page(client, "/shop?o=brand&s=winter&t=studded&wh=1&b=laufenn&xb=ikon+tyres&size=205/55R16&q=x")
    for label in ("Зима", "Шипы", "Склад 1", "«x»", "205/55R16"):
        assert label in html, label


def test_on_the_search_looks_in_the_season_too_off_it_does_not(tmp_path):
    app, client, conn, _ = make_shop(tmp_path)
    assert prices(page(client, "/shop?q=лето")) == []
    new_view(conn)
    assert prices(page(client, "/shop?q=лето")) == [3670]
    assert sorted(prices(page(client, "/shop?q=ikon+205/55"))) == [13580]


def test_on_the_filters_are_a_card_above_the_rows_and_an_old_folding_link_still_opens(tmp_path):
    # 2026-11-09 (the owner): the filters are one card above the rows as in the office's catalog, no side panel to fold any more
    app, client, conn, _ = make_shop(tmp_path)
    new_view(conn)
    html = page(client, "/shop?sf=0&w=205")
    assert sorted(prices(html)) == [13580] and 'class="o2 shop-o2f"' in html
    assert "is-folded" not in html and "shop2-unfold" not in html and "shop-side" not in html
    assert html.index('class="o2 shop-o2f"') < html.index('data-tour="catalog"')


def test_on_every_step_of_the_tour_still_lights_an_element(tmp_path):
    app, client, conn, _ = make_shop(tmp_path)
    new_view(conn)
    html = page(client)
    for s in tour.steps_for("client"):
        assert len(select(html, s["sel"])) == 1, s["sel"]
        if "phone" in s:
            assert len(select(html, s["phone"])) == 1, s["phone"]


def test_on_the_cart_steps_the_quantity_never_above_the_stock(tmp_path):
    app, client, conn, tg, pid = shop(tmp_path)
    new_view(conn)
    k = keys(page(client))[0]                                              # IKON 205/55 R16, stock 12
    add(client, k, qty="11")
    cart = page(client, "/shop/cart")
    assert 'name="qty" value="10"' in cart and 'name="qty" value="12"' in cart and "Первый заказ" in cart
    post(client, "/shop/cart/set", "tok", key=k, qty="12")
    cart = page(client, "/shop/cart")
    assert re.search(r'name="qty" value="13"[^>]*disabled', cart)
    post(client, "/shop/cart/set", "tok", key=k, qty="13")
    assert shop_cart.cart(conn, "buyer") == {"77001": 12}


# ---- on: the checkout makes the same order ---------------------------------------------------------------------------------------------

def _card(conn, r):
    card = orders.get_card(conn, order_id(r))
    return {k: card[k] for k in ("client_name", "client_phone", "point_id", "source", "responsible", "comment")}, \
        card["stage"]["code"], sorted((i["goods_id"], i["qty"], i["sale_unit"], i["supplier_price"]) for i in card["items"])


def test_on_a_pickup_needs_the_phone_only_and_makes_the_order_of_the_old_checkout(tmp_path):
    app, client, conn, tg, pid = shop(tmp_path)
    ks = keys(page(client))
    add(client, ks[0], qty="4")
    old = _card(conn, checkout(client, way="point", point_id=str(pid)))
    new_view(conn)
    add(client, ks[0], qty="4")
    r = post(client, "/shop/checkout", "tok", way="point", point_id=str(pid), phone="+7 900 111-22-33", comment="к пятнице")
    assert r.status_code == 302 and r.headers["Location"].endswith("?placed=1")
    assert _card(conn, r) == old
    html = page(client, r.headers["Location"])
    assert "Заказ №%d принят" % order_id(r) in html and "shop2-done" in html
    assert shop_cart.profile(conn, "buyer")["address"] == "г. Тест, ул. Первая, 1"   # a pickup keeps the address it had


def test_on_a_delivery_needs_the_address_the_recipient_and_keeps_them(tmp_path):
    app, client, conn, tg, pid = shop(tmp_path)
    new_view(conn)
    add(client, keys(page(client))[0], qty="2")
    r = post(client, "/shop/checkout", "tok", way="delivery", phone="+7 900 111-22-33", **dict(DELIVERY, recipient=""))
    body = r.get_data(as_text=True)
    assert r.status_code == 400 and "Получатель: впишите" in body and 'shop2-dlv-recipient is-err' in body and shop_cart.cart(conn, "buyer")
    assert post(client, "/shop/checkout", "tok", way="delivery", phone="+7 900 111-22-33", **dict(DELIVERY, recipient_phone="12")).status_code == 400
    assert post(client, "/shop/checkout", "tok", way="", phone="+7 900 111-22-33").status_code == 400
    assert post(client, "/shop/checkout", "tok", way="point", point_id="999", phone="+7 900 111-22-33").status_code == 400
    assert post(client, "/shop/checkout", "tok", way="point", point_id=str(pid), phone="").status_code == 400
    assert conn.execute("SELECT COUNT(*) FROM shop_order").fetchone()[0] == 0
    r = post(client, "/shop/checkout", "tok", way="delivery", phone="+7 900 111-22-33", comment="к пятнице", **DELIVERY)
    assert r.status_code == 302
    fields, stage, items = _card(conn, r)
    assert stage == "from_point" and fields["source"] == "shop" and fields["point_id"] in ("", None, 0) and fields["client_phone"] == "+79001112233"
    assert fields["comment"] == "Доставка: Тест, ул. Первая, д. 1. Получатель: Иннокентий, +79002223344. Водителю: калитка синяя. к пятнице"
    kept = shop_cart.delivery(conn, "buyer")
    assert kept["saved"] and kept["recipient"] == "Иннокентий" and kept["driver_note"] == "калитка синяя"
    assert "Доставка: Тест, ул. Первая, д. 1" in page(client, "/shop/orders")
    add(client, keys(page(client))[0])
    cart = page(client, "/shop/cart")
    assert "Получатель: Иннокентий" in cart and "Изменить адрес" in cart and "Изменить телефон" in cart and "Первый заказ" not in cart


# ---- on: the profile ------------------------------------------------------------------------------------------------------------------

def test_on_the_profile_keeps_the_contacts_and_the_delivery_apart(tmp_path):
    app, client, conn, _ = make_shop(tmp_path)
    new_view(conn)
    html = page(client, "/shop/profile")
    assert 'name="part" value="contacts"' in html and 'name="part" value="delivery"' in html and 'name="driver_note"' in html
    assert 'action="/account/password"' in html
    r = post(client, "/shop/profile", "tok", part="contacts", name="ООО Шины", phone="+7 900 111-22-33")
    assert r.status_code == 302
    assert shop_cart.profile(conn, "buyer")["phone"] == "+79001112233" and users.get_role(conn, "buyer")["display_name"] == "ООО Шины"
    r = post(client, "/shop/profile", "tok", part="delivery", **DELIVERY)
    assert r.status_code == 302 and r.headers["Location"].endswith("#delivery")
    assert shop_cart.delivery(conn, "buyer")["address"] == "Тест, ул. Первая, д. 1"
    assert shop_cart.profile(conn, "buyer") == {"phone": "+79001112233", "address": "Тест, ул. Первая, д. 1"}   # the old line follows
    r = post(client, "/shop/profile", "tok", part="delivery", **dict(DELIVERY, city="", recipient="Пётр"))
    assert r.status_code == 400 and "Город: впишите" in r.get_data(as_text=True)
    assert shop_cart.delivery(conn, "buyer")["recipient"] == "Иннокентий"                          # a refusal changes nothing
    r = post(client, "/shop/profile", "tok", part="contacts", name="Другое", phone="12")
    assert r.status_code == 400 and users.get_role(conn, "buyer")["display_name"] == "ООО Шины"
    assert post(client, "/shop/profile", "tok", part="other").status_code == 400


def test_on_an_address_of_before_is_offered_in_the_street(tmp_path):
    app, client, conn, _ = make_shop(tmp_path)
    shop_cart.save_profile(conn, "buyer", "+7 900 111-22-33", "г. Тест, ул. Старая, 5", "2026-10-30T10:00:00")
    new_view(conn)
    assert 'name="street" maxlength="120" placeholder="Улица" value="г. Тест, ул. Старая, 5"' in page(client, "/shop/profile")


def test_on_one_client_never_sees_another_delivery(tmp_path):
    app, client, conn, _ = make_shop(tmp_path)
    new_view(conn)
    post(client, "/shop/profile", "tok", part="delivery", **DELIVERY)
    other = second_client(app, conn)
    html = page(other, "/shop/profile")
    assert "Иннокентий" not in html and "калитка синяя" not in html and "Первая" not in html
    post(other, "/shop/profile", "tok", part="delivery", **dict(DELIVERY, recipient="Пётр"))
    assert shop_cart.delivery(conn, "buyer")["recipient"] == "Иннокентий" and shop_cart.delivery(conn, "buyer2")["recipient"] == "Пётр"


def test_on_the_staff_still_has_no_page_of_the_client(tmp_path):
    app, client, conn, _ = make_shop(tmp_path)
    new_view(conn)
    owner = app.test_client()
    login(owner)
    for url in ("/shop", "/shop/cart", "/shop/orders", "/shop/profile"):
        assert owner.get(url).status_code == 403, url
    assert post(owner, "/shop/profile", "tok", part="delivery", **DELIVERY).status_code == 403
    assert post(owner, "/shop/checkout", "tok", way="point", phone="+7 900 111-22-33").status_code == 403


# ---- the review of 2026-11-02 ---------------------------------------------------------------------------------------------------------

@pytest.mark.usefixtures("telegram_for_everything")       # the mechanics of Telegram: the setting of critical.py off (test_critical.py: the rule)
def test_on_a_failed_save_of_the_delivery_never_silences_the_order(tmp_path, monkeypatch):
    app, client, conn, tg, pid = shop(tmp_path)
    new_view(conn)
    add(client, keys(page(client))[0])

    def broken(*a, **kw):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(shop_cart, "save_delivery", broken)
    r = post(client, "/shop/checkout", "tok", way="delivery", phone="+7 900 111-22-33", **DELIVERY)
    assert r.status_code == 302 and conn.execute("SELECT COUNT(*) FROM shop_order").fetchone()[0] == 1
    assert any("sendMessage" in c[1] for c in tg.calls)                                # the manager is told all the same


def test_on_an_address_changed_in_the_old_view_is_not_offered_as_kept(tmp_path):
    app, client, conn, _ = make_shop(tmp_path)
    new_view(conn)
    post(client, "/shop/profile", "tok", part="delivery", **DELIVERY)
    new_view(conn, "off")
    shop_cart.save_profile(conn, "buyer", "+7 900 111-22-33", "г. Новый, ул. Другая, 7", "2026-11-02T12:00:00")
    kept = shop_cart.delivery(conn, "buyer")
    assert not kept["saved"] and kept["street"] == "г. Новый, ул. Другая, 7" and kept["city"] == "" and kept["house"] == ""
    assert kept["recipient"] == "Иннокентий"                                          # the recipient stays offered


def test_on_a_chip_given_twice_is_one_and_its_cross_drops_it(tmp_path):
    app, client, conn, _ = make_shop(tmp_path)
    new_view(conn)
    html = page(client, "/shop?w=205&w=205&d=16")
    assert html.count(">ширина 205<") == 1 and 'href="/shop?d=16"' in html


def test_on_the_fields_of_the_delivery_say_the_limits_of_the_server(tmp_path):
    app, client, conn, _ = make_shop(tmp_path)
    new_view(conn)
    html = page(client, "/shop/profile")
    for key, limit in shop_cart.DELIVERY_LIMITS.items():
        assert re.search(r'name="%s" maxlength="%d"' % (key, limit), html), key



# ---- the owner's answers of 2026-11-02 -------------------------------------------------------------------------------------------------

def test_on_minus_stops_at_one_and_delete_drops_the_tyre(tmp_path):
    app, client, conn, tg, pid = shop(tmp_path)
    new_view(conn)
    k = keys(page(client))[0]
    add(client, k, qty="1")
    cart = page(client, "/shop/cart")
    assert re.search(r'name="qty" value="0"[^>]*disabled', cart) and ">Удалить</button>" in cart and "Убрать</button>" not in cart
    add(client, k, qty="1")
    assert not re.search(r'name="qty" value="1"[^>]*disabled', page(client, "/shop/cart"))      # at two «−» works
    post(client, "/shop/cart/set", "tok", key=k, drop="1")
    assert shop_cart.cart(conn, "buyer") == {}
    new_view(conn, "off")
    add(client, k, qty="1")
    old = page(client, "/shop/cart")
    assert ">Убрать</button>" in old and 'aria-label="Меньше"' not in old                       # the old cart as it was


def test_on_a_tyre_opens_its_card_with_the_big_photo_and_the_buy(tmp_path):
    app, client, conn, _ = make_shop(tmp_path)
    new_view(conn)
    html = page(client)
    n = len(prices(html))
    assert n and all('href="#tire-%d"' % i in html and 'id="tire-%d"' % i in html and 'id="row-%d"' % i in html for i in range(1, n + 1))
    card = html[html.index('id="tire-1"'):].split('class="shop2-sheet"')[0]
    assert "Размер" in card and "Наличие" in card and 'action="/shop/cart/add"' in card and 'href="#row-1"' in card
    assert "<script" not in html
    tiles = page(client, "/shop?v=tiles")
    assert 'href="#tire-1"' in tiles and 'id="tire-1"' in tiles
    owner = app.test_client()
    login(owner)
    new_view(conn, "owner_only")
    preview = page(owner, "/client-terms/c/buyer/preview")
    assert 'id="tire-1"' in preview and 'action="/shop/cart/add"' not in preview                # the preview never buys


def test_on_my_orders_shows_how_many_are_open(tmp_path):
    app, client, conn, tg, pid = shop(tmp_path)
    ks = keys(page(client))
    add(client, ks[0])
    checkout(client, way="point", point_id=str(pid))
    nav = lambda html: html[html.index('class="shop-nav"'):].split("</nav>")[0]
    assert "Заказы в работе" not in nav(page(client))                                       # off: the old header
    new_view(conn)
    add(client, ks[0])
    r = post(client, "/shop/checkout", "tok", way="point", point_id=str(pid), phone="+7 900 111-22-33")
    assert re.search(r'Мои заказы</span> <b class="shop-count" title="Заказы в работе">2</b>', nav(page(client)))
    conn.execute("INSERT INTO order_stage (code, name, position, kind) VALUES ('done', 'Выполнено', 99, 'won')")
    conn.execute("UPDATE order_card SET stage_id = (SELECT id FROM order_stage WHERE code = 'done') WHERE id = ?", (order_id(r),))
    conn.commit()
    assert '"Заказы в работе">1</b>' in nav(page(client, "/shop/orders"))                   # «Выполнен» is not in the digit
    conn.execute("INSERT INTO order_stage (code, name, position, kind) VALUES ('', 'Свой закрытый', 98, 'won')")
    conn.execute("UPDATE order_card SET stage_id = (SELECT id FROM order_stage WHERE name = 'Свой закрытый') WHERE id = 1")
    conn.commit()
    assert shop_cart.open_orders(conn, "buyer") == 0                                          # a stage of the owner: by its kind
    conn.execute("UPDATE order_card SET stage_id = (SELECT id FROM order_stage WHERE code = 'from_point') WHERE id = 1")
    conn.execute("UPDATE order_card SET sale_ids = 'not json' WHERE id = 1")                 # an order the card cannot read
    conn.commit()
    assert shop_cart.open_orders(conn, "buyer") == 1                                          # the count never reads the card
