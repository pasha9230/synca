"""The size filters of the office's catalog and of the client's /shop (the owner 2026-11-09): «Ширина», «Профиль», «Диаметр» each with
several values from a list (repeated parameters, OR inside a field, AND between the fields; the old links still read); the page is not
asked anew on Tab or a key; «Недавние» (the last 5 sizes searched, per person, user_pref); the client's «Колонки» («Страна» and «Год»
apart), «Количество» of «В корзину» at most the stock and the word when more is asked."""
from __future__ import annotations

import re

import pytest

from panel_helpers import post
from test_catalog_filters import codes, setup as office
from test_shop import ROWS, make_shop, page, prices, wm
from test_shop_cart import add, keys
from tires_sync import store, user_prefs
from tires_sync.panel import client_catalog as cc


def view(conn, who, state):
    store.set_state(conn, "ui_new." + who, state)
    conn.commit()


def filters(html) -> str:
    start = html.index('class="card o2-cat-filters"')
    return html[start:html.index("</form>", start)]


def size_list(html, key) -> str:
    return re.search(r'data-msel="%s">(.*?)</div>\n?</div>' % key, filters(html), re.S).group(0)


# ---- several values: the client (both views) and the office -------------------------------------------------------------------------

@pytest.mark.parametrize("state", ["off", "all"])
def test_the_client_ticks_two_widths_and_sees_both_and_no_other(tmp_path, state):
    app, client, conn, _ = make_shop(tmp_path)          # 205/55 R16 at 13 580, 245/45 R18 at 65 190, 195/65 R15 at 3 670
    view(conn, "client", state)
    assert sorted(prices(page(client, "/shop?w=205&w=245"))) == [13580, 65190]
    assert sorted(prices(page(client, "/shop?w=205&w=245&d=18"))) == [65190]                  # AND between the fields
    assert sorted(prices(page(client, "/shop?d=15&d=16"))) == [3670, 13580]
    assert sorted(prices(page(client, "/shop?w=205"))) == [13580]                             # the old one-value links
    assert sorted(prices(page(client, "/shop?w=205+245"))) == [13580, 65190]                  # and the old «by spaces»
    html = page(client, "/shop?w=205&w=245")
    box = size_list(html, "w")
    assert 'name="w" value="205" checked' in box and 'name="w" value="245" checked' in box and 'name="w" value="195">' in box
    assert re.search(r'data-msel-n>2<', box)                                                    # the badge of how many
    chips = re.findall(r'class="msel-chip" href="([^"]+)"', box)
    assert chips == ["/shop?w=245", "/shop?w=205"]                                             # each chip: the page without it
    assert 'class="msel-clear" href="/shop"' in box                                            # the cross of the whole field
    assert "<script" not in html


def test_the_office_ticks_two_widths_and_old_links_still_read(tmp_path):
    app, owner, tok, conn = office(tmp_path)
    view(conn, "office", "all")
    get = lambda q: codes(owner.get("/catalog" + q).get_data(as_text=True))
    assert get("?w=225&w=195") == ["C1", "C3", "C5", "C7"]                                     # 225 and 195, never 205
    assert get("?w=205&w=225&h=55&d=16") == ["C1", "C2", "C6"]
    assert get("?w=225+195") == get("?w=225,195") == ["C1", "C3", "C5", "C7"] and get("?w=205") == ["C2", "C4", "C6"]
    html = owner.get("/catalog?w=225&w=195").get_data(as_text=True)
    box = size_list(html, "w")
    assert 'name="w" value="225" checked' in box and 'name="w" value="195" checked' in box and 'name="w" value="205">' in box


def test_one_reading_of_the_sizes_for_both():
    from werkzeug.datastructures import MultiDict
    from tires_sync.panel import routes_catalog
    for args in (MultiDict([("w", "245"), ("w", "255")]), MultiDict([("w", "245 255")]), MultiDict([("w", "255,245"), ("w", "245")])):
        assert cc.size_numbers(args, "w") == routes_catalog._nums(args, "w") == [245, 255]
    assert cc.size_numbers(MultiDict([("d", "R16"), ("d", "r17")]), "d") == routes_catalog._nums(MultiDict([("d", "R16 r17")]), "d") == [16, 17]
    assert routes_catalog._nums(MultiDict([("w", "abc")]), "w") is None and cc.size_numbers(MultiDict(), "w") == []


@pytest.mark.parametrize("who", ["office", "client"])
def test_no_update_on_tab_or_a_key(tmp_path, who):
    """Tab goes from «Ширина» to «Профиль» to «Диаметр»: the row is never sent on a change (app.js data-autosubmit), the page is asked
    anew by «Показать» of a list, by Enter or by a chip."""
    if who == "office":
        app, owner, tok, conn = office(tmp_path)
        view(conn, "office", "all")
        html = owner.get("/catalog").get_data(as_text=True)
    else:
        app, owner, conn, _ = make_shop(tmp_path)
        view(conn, "client", "all")
        html = page(owner)
    box = filters(html)
    assert 'class="o2-frow o2-frow--1" data-no-autosubmit' in box
    assert box.count('<details class="msel-box" data-no-autosubmit>') == 3
    assert [m for m in re.findall(r'data-msel="(\w)"', box)] == ["w", "h", "d"]                    # in this order: Tab goes so
    assert box.count('class="btn btn--primary btn--sm msel-go"') == 3
    assert 'list="dl-' not in box and "onkeyup" not in box and "oninput" not in box


def test_the_script_of_the_office_does_not_send_on_keys():
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "src" / "tires_sync" / "panel" / "static" / "app.js").read_text(encoding="utf-8")
    part = js[js.index("details.msel-box"):js.index("// 2)")]
    assert "keyup" not in part and '"input"' not in part and 'addEventListener("toggle"' in part and "mselChanged" in part


# ---- «Недавние» ---------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("state", ["off", "all"])
def test_recent_sizes_of_the_client_are_kept_and_put_back(tmp_path, state):
    app, client, conn, _ = make_shop(tmp_path)
    view(conn, "client", state)
    assert "Недавние" not in page(client)
    page(client, "/shop?w=205&h=55&d=16&s=winter")
    page(client, "/shop?size=245/45R18")
    page(client, "/shop?w=205&w=245")                                                   # two widths: no one size, nothing kept
    assert user_prefs.get(conn, "buyer", cc.RECENT_KEY) == [[245, 45, 18], [205, 55, 16]]
    html = page(client, "/shop?s=winter")
    chips = re.findall(r'class="msel-chip o2-recent-chip" href="([^"]+)">([^<]+)<', html)
    assert [c[1] for c in chips] == ["245/45 R18", "205/55 R16"]
    url = chips[1][0].replace("&amp;", "&")
    assert url == "/shop?s=winter&w=205&h=55&d=16"                                       # the other filters stay, the size goes in
    assert sorted(prices(page(client, url))) == [13580]
    for n in range(6):                                                                   # five at most, the latest first, each once
        page(client, "/shop?w=%d&h=55&d=16" % (200 + n))
    assert user_prefs.get(conn, "buyer", cc.RECENT_KEY) == [[205, 55, 16], [204, 55, 16], [203, 55, 16], [202, 55, 16], [201, 55, 16]]


def test_recent_sizes_are_each_persons_own_and_the_preview_keeps_none(tmp_path):
    app, client, conn, _ = make_shop(tmp_path)
    page(client, "/shop?w=205&h=55&d=16")
    (tmp_path / "office").mkdir()
    app2, owner, tok, conn2 = office(tmp_path / "office")
    view(conn2, "office", "all")
    owner.get("/catalog?w=225&h=45&d=18")
    html = owner.get("/catalog").get_data(as_text=True)
    assert re.findall(r'o2-recent-chip" href="[^"]+">([^<]+)<', html) == ["225/45 R18"]
    assert user_prefs.get(conn, "buyer", cc.RECENT_KEY) == [[205, 55, 16]]
    assert cc.remember_sizes(conn, "", [(1, 2, 3)], "x") == [] and cc.recent_sizes(conn, "nobody") == []
    user_prefs.put(conn, "buyer", cc.RECENT_KEY, [[205, 55], "junk", [0, 1, 2], [205, 55, 16]], "x")       # a value that no longer reads
    assert cc.recent_sizes(conn, "buyer") == [(205, 55, 16)]


# ---- the client's «Колонки» and «Количество» ---------------------------------------------------------------------------------------

def head(html) -> list:
    return re.findall(r"<th[^>]*>([^<]*)</th>", html.split('class="table shop-table')[1].split("</thead>")[0])


@pytest.mark.parametrize("state", ["off", "all"])
def test_country_and_year_are_two_columns_and_the_client_hides_and_keeps_them(tmp_path, state):
    app, client, conn, _ = make_shop(tmp_path, warehouses=(1, 2))        # both warehouses on (the owner 2026-11-10: else no «Склад»)
    view(conn, "client", state)
    html = page(client)
    names = head(html)
    assert "Страна" in names and "Год" in names and "Страна, год" not in names
    assert "Колонки" in html and html.count('name="show"') == 6
    assert post(client, "/shop/columns", "tok", show="season").status_code == 302      # only «Сезон» stays shown
    client.post("/shop/columns", data={"csrf": "tok", "show": ["season", "price"]}, headers={"Referer": "http://localhost/shop?w=205"})
    assert user_prefs.get(conn, "buyer", cc.COLUMNS_KEY) == ["country", "year", "wh", "stock"]
    html = page(client)
    names = head(html)
    assert "Сезон" in names and "Цена за шт." in names and "Шина" in names and "Количество" in names
    assert not {"Страна", "Год", "Склад", "Остаток"} & set(names)
    assert 'class="c-country"' not in html and 'class="c-stock num"' not in html and 'class="c-tire"' in html
    assert 'name="show" value="year">' in html and 'name="show" value="season" checked' in html
    response = client.post("/shop/columns", data={"csrf": "tok"}, headers={"Referer": "http://localhost/shop?w=205"})
    assert response.headers["Location"].endswith("/shop?w=205")                           # back to the page it came from
    names = head(page(client))
    assert "Шина" in names and "Количество" in names and "Сезон" not in names and "Цена за шт." not in names


def test_the_columns_are_the_clients_only(tmp_path):
    app, owner, tok, conn = office(tmp_path)
    assert owner.post("/shop/columns", data={"csrf": tok, "show": ["season"]}).status_code in (302, 403)
    assert user_prefs.get(conn, "owner", cc.COLUMNS_KEY) is None


@pytest.mark.parametrize("state", ["off", "all"])
def test_the_quantity_is_at_most_the_stock(tmp_path, state):
    few = wm(NNOMMODIF="77009", SMODIFNAME="IKON 215/60/16 T 95 Test Model", SWIDTH="215", SHEIGHT="60", NPRICE_DELAYED="9000", NREST="3")
    app, client, conn, _ = make_shop(tmp_path, rows=ROWS + (few,))
    view(conn, "client", state)
    html = page(client, "/shop?o=price")                   # 195/65 R15 (9 in stock), 215/60 R16 (3), 205/55 R16 (12), 245/45 R18 (41)
    found = re.findall(r'name="qty" value="(\d+)" min="1" max="(\d+)"', html.split('class="shop2-sheet"')[0])
    assert found == [("4", "9"), ("3", "3"), ("4", "12"), ("4", "41")]
    assert 'max="100"' not in html


def test_more_than_the_stock_puts_the_stock_and_says_so(tmp_path):
    app, client, conn, _ = make_shop(tmp_path)
    key = keys(page(client, "/shop?w=205"))[0]                                             # 205/55 R16, 12 in stock
    add(client, key, qty="50")
    html = page(client, "/shop")
    assert "В наличии 12 шт.: столько и положили." in html
    from tires_sync import shop_cart
    assert list(shop_cart.cart(conn, "buyer").values()) == [12]
    add(client, key, qty="1")                                                              # the cart holds all of them already
    assert "В наличии 12 шт.: столько и положили." in page(client)
    assert list(shop_cart.cart(conn, "buyer").values()) == [12]


def test_within_the_stock_the_usual_word(tmp_path):
    app, client, conn, _ = make_shop(tmp_path)
    key = keys(page(client, "/shop?w=205"))[0]
    add(client, key, qty="4")
    html = page(client)
    assert "В корзине: 4 шт." in html and "В наличии" not in html
