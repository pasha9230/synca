"""«Вид 2» of the client's block of the filters (/shop, the owner 2026-11-09…10, by the catalog of the supplier the client knows): the switch
«Вид 1 / Вид 2» kept for the client; three glued lists of any size with chips and counters, «Производитель» of the client's own brands,
no «Показать» (the rows are asked anew by static/shop_v2.js); the mixed-width sets (front / rear, 2 of each, one model); the icons of a
row. «Вид 1» and the office's catalog stay as they were."""
from __future__ import annotations

import re

import pytest

from panel_helpers import post
from test_catalog_multi import office, view
from test_shop import NOW, make_shop, page, tochki_row
from tires_sync import user_prefs
from tires_sync.panel import client_catalog as cc


def found(html) -> int:
    return int(re.search(r"[Нн]айдено (\d+)", html).group(1))


def block(html) -> str:
    start = html.index('class="card shop-v2"')
    return html[start:html.index("</section>", start)]


def to_view2(client):
    r = post(client, "/shop/filters-view", "tok", fv="2")
    assert r.status_code == 302 and r.headers["Location"].endswith("/shop")


def results(html) -> str:
    return html[html.index('class="shop-results'):]


@pytest.mark.parametrize("state", ["off", "all"])
def test_the_switch_of_the_view_is_kept_for_the_client(tmp_path, state):
    app, client, conn, _ = make_shop(tmp_path)
    view(conn, "client", state)
    first = client.get("/shop")
    html = first.get_data(as_text=True)
    assert 'action="/shop/filters-view"' in html and 'value="1" class="is-active" aria-pressed="true">Вид 1' in html
    assert "data-fv2" not in html and "<script" not in html and "script-src" not in first.headers["Content-Security-Policy"]
    to_view2(client)
    assert user_prefs.get(conn, "buyer", cc.VIEW_KEY) == 2
    for url in ("/shop", "/shop?s=winter", "/shop/cart", "/shop"):                    # kept on a reload and across the pages
        response = client.get(url)
        assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "data-fv2" in html and 'value="2" class="is-active" aria-pressed="true">Вид 2' in html
    assert '<script src="/static/shop_v2.js' in html and html.count("<script") == 1
    csp = response.headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "connect-src 'self'" in csp and "default-src 'none'" in csp and "unsafe" not in csp.split("style-src")[0]
    assert client.get("/static/shop_v2.js").status_code == 200
    r = post(client, "/shop/filters-view", "tok", fv="1")
    assert r.status_code == 302 and "data-fv2" not in page(client) and user_prefs.get(conn, "buyer", cc.VIEW_KEY) == 1
    assert post(client, "/shop/filters-view", "bad", fv="2").status_code in (400, 403)                # the CSRF of every form
    assert user_prefs.get(conn, "buyer", cc.VIEW_KEY) == 1


def test_another_client_keeps_its_own_view(tmp_path):
    app, client, conn, _ = make_shop(tmp_path)
    to_view2(client)
    assert cc.filters_view(conn, "buyer") == 2 and cc.filters_view(conn, "someone") == 1 and cc.filters_view(conn, "") == 1


@pytest.mark.parametrize("state", ["off", "all"])
def test_view2_has_three_glued_lists_chips_counters_and_the_clients_brands(tmp_path, state):
    app, client, conn, sid = make_shop(tmp_path, tochki=[tochki_row("T1", rest=3)])
    view(conn, "client", state)
    to_view2(client)
    html = page(client, "/shop?w=205&w=245&d=16&b=ikon+tyres&b=nobody")
    box = block(html)
    glue = box[box.index('class="shop-ms-glue"'):box.index('class="shop-ms shop-ms--brand"')]
    assert re.findall(r'class="shop-ms" data-ms data-name="(\w)"', glue) == ["w", "h", "d"]          # glued, in the order of Tab
    for label in ("Ширина", "Профиль", "Диаметр", "Производитель", "Подбор разношироких шин", "Все", "Лето", "Зима", "Шипы", "Без шипов",
                  "Сбросить фильтры"):
        assert label in box, label
    assert re.findall(r'<input type="hidden" name="w" value="(\d+)">', box) == ["205", "245"]          # the chips, each with its cross
    assert box.count('aria-label="Убрать ширина') == 2 and 'aria-label="Убрать диаметр R16"' in box
    assert re.findall(r'data-ms-n(?: hidden)?>(\d+)</b>', box) == ["2", "0", "1", "1"]                  # w, h, d, brands
    width = box[box.index('id="ms-w-list"'):box.index("</ul>", box.index('id="ms-w-list"'))]
    assert 'data-v="135"' in width and 'data-v="335"' in width                                       # any size, not only of the stock
    brands = box[box.index('id="ms-b-list"'):box.index("</ul>", box.index('id="ms-b-list"'))]
    assert sorted(re.findall(r'data-v="([^"]+)"', brands)) == ["ikon tyres", "laufenn"]               # the client's own brands only
    assert '<input type="hidden" name="b" value="ikon tyres">' in box and 'value="nobody"' not in box and ">nobody<" not in box
    assert "Показать" not in box and "msel-go" not in box and "<details" not in box
    for word in ("Подбор по авто", "Шинный калькулятор", "Происхождение", "Отечественная", "В наличии", "Все фильтры"):
        assert word not in html, word
    assert 'class="shop-v2-reset"' in box and '<path d="M8 12h8"/>' in box       # «Сбросить» with its icon
    assert "WebMIM" not in html and "4Точки" not in html and "мим" not in html.lower()
    assert found(html) == 2                                                                          # IKON 205/55 R16 of both warehouses


def test_view2_the_brands_narrow_the_rows_and_an_empty_list_is_every_brand(tmp_path):
    app, client, conn, _ = make_shop(tmp_path)
    to_view2(client)
    assert found(page(client)) == 3 and found(page(client, "/shop?b=laufenn")) == 1 and found(page(client, "/shop?b=ikon+tyres")) == 2


def test_a_size_not_in_stock_can_be_chosen_and_finds_nothing(tmp_path):
    app, client, conn, _ = make_shop(tmp_path)
    to_view2(client)
    html = page(client, "/shop?w=135")
    assert found(html) == 0 and "Товары не найдены" in html
    assert '<input type="hidden" name="w" value="135">' in block(html)
    assert "Товары не найдены" not in page(client)


def test_recent_sizes_on_top_of_view2(tmp_path):
    app, client, conn, _ = make_shop(tmp_path)
    to_view2(client)
    page(client, "/shop?w=205&h=55&d=16")
    box = block(page(client))
    assert 'class="shop-v2-recent"' in box and box.index("shop-v2-recent") < box.index("shop-ms-glue") and "205/55 R16" in box


MIXED = "/shop?mixed=1&fw=205&fh=55&fd=16&rw=245&rh=45&rd=18"


def groups(html) -> dict:
    """The groups of the mixed rows: the title of the band -> the sizes of its rows in order (and the quantities)."""
    out = {}
    for body in re.findall(r'<tbody class="shop-kit".*?</tbody>', html, flags=re.S):
        title = re.search(r'scope="colgroup">([^<]+)</th>', body).group(1)
        out[title] = [(size, qty) for size, qty in re.findall(r'<td class="c-size">(?:<span class="shop-kit-axis">\w+</span> )?([\d/ R]+)</td>'
                                                               r'.*?name="qty" value="(\d+)"', body, flags=re.S)]
        if "Нет подходящих" in body:
            out[title] = "нет"
    return out


def test_the_mixed_sets_are_pairs_of_one_model_then_each_axis_alone(tmp_path):
    app, client, conn, _ = make_shop(tmp_path, tochki=[tochki_row("T1", rest=1)])
    to_view2(client)
    html = page(client, MIXED)
    box = block(html)
    assert "shop-v2-recent" not in box and 'name="mixed" value="1" checked' in box
    assert "Передняя ось" in box and "Задняя ось" in box
    for name, value in (("fw", "205"), ("fh", "55"), ("fd", "16"), ("rw", "245"), ("rh", "45"), ("rd", "18")):
        assert 'name="%s" value="%s"' % (name, value) in box, name
    assert 'data-name="w"' not in box and "data-ms-chips" not in box.split("Производитель")[0]   # one value per field, no chips
    got = groups(results(html))
    # IKON Test Model winter: 205/55 R16 of «Склад 1» in front, 245/45 R18 behind, 2 of each; the 4tochki one is studded: another model
    assert got == {"Комплект 1": [("205/55 R16", "2"), ("245/45 R18", "2")], "Только передняя ось": [("205/55 R16", "1")],
                   "Только задняя ось": []}
    assert found(html) == 1 and html.count("data-kit") == 1
    assert "Сумма" not in results(html) and "за комплект" not in results(html)
    assert len(re.findall(r'action="/shop/cart/add"', results(html))) == 3                          # each row its own cart


def test_one_front_tyre_may_be_in_several_sets_and_no_pair_says_so(tmp_path):
    app, client, conn, _ = make_shop(tmp_path)
    to_view2(client)
    html = page(client, "/shop?mixed=1&fw=205&fh=55&fd=16&rd=18")                                   # the rear: any width of R18
    assert groups(results(html))["Комплект 1"] == [("205/55 R16", "2"), ("245/45 R18", "2")]
    html = page(client, "/shop?mixed=1&fw=205&rw=195")                                              # another brand behind
    got = groups(results(html))
    assert got["Комплект"] == "нет" and got["Только передняя ось"] == [("205/55 R16", "2")] and got["Только задняя ось"] == [("195/65 R15", "2")]
    assert "Нет подходящих" in html and found(html) == 0
    assert "Укажите размеры осей" in page(client, "/shop?mixed=1")


def test_mixed_sets_pair_by_brand_model_season_and_studs():
    class O:
        def __init__(self, w, h, d, model="M", season="winter", studded=False):
            from types import SimpleNamespace
            self.width, self.height, self.diameter, self.model_raw, self.studded = w, h, d, model, studded
            self.season = SimpleNamespace(value=season)
            self.stock = 4
    item = lambda o, brand="a": cc.Visible(o, 100, 1, brand, brand.upper())
    f225, f245, r255 = item(O(225, 45, 18)), item(O(245, 40, 18)), item(O(255, 35, 18))
    other = [item(O(255, 35, 19), "b"), item(O(255, 35, 19, model="N")), item(O(255, 35, 19, season="summer")), item(O(255, 35, 19, studded=True))]
    f = cc.filters_of(_Args(), set())
    sets = cc.mixed_sets([f225, f245, r255] + other, f, (None, None, None), (255, 35, None))
    assert [(a.offer.width, b.offer.width) for a, b in sets["kits"]] == [(225, 255), (245, 255)]      # one rear in two sets
    assert {id(i) for i in sets["rear"]} == set()                 # alone and fitting both axes: once, in «Только передняя»
    assert {id(i) for i in sets["front"]} == {id(i) for i in other}  # another brand, model, season or studs: never a pair
    assert r255 not in sets["front"]                              # in a set: in no «Только …», and a pair is never of one size


class _Args(dict):
    def getlist(self, name):
        return []


@pytest.mark.parametrize("state", ["off", "all"])
def test_the_icons_of_the_client(tmp_path, state):
    app, client, conn, _ = make_shop(tmp_path, tochki=[tochki_row("T1")])
    view(conn, "client", state)
    html = page(client)
    head = html[html.index('class="shop-top"'):html.index("</header>")]
    assert 'data-ico title="Корзина"' in head and 'data-ico title="Профиль"' in head and '<span class="sr-only">Выйти</span>' in head
    rows = results(html)
    assert 'title="Зима, без шипов"' in rows and 'title="Зима, шипы"' in rows and 'class="shop-spike"' in rows and 'title="Лето"' in rows
    assert 'class="btn btn--primary btn--sm shop-buy-btn" title="В корзину"' in rows and '<span class="shop-buy-t">В корзину</span>' in rows
    assert 'title="Таблица"' in rows and 'title="Плитка"' in rows and "shop-sort-i" in rows and "↑" not in rows and "↓" not in rows
    to_view2(client)
    assert '<span class="sr-only">Найти</span>' in block(page(client))                                # the loupe of the search


def test_view1_is_kept_as_it_was(tmp_path):
    """The guard of «Вид 1»: the block of the office's look, «Показать», no script, the strict frame."""
    app, client, conn, _ = make_shop(tmp_path)
    response = client.get("/shop?w=205")
    html = response.get_data(as_text=True)
    box = html[html.index('class="o2 shop-o2f"'):html.index("</form>", html.index('class="o2 shop-o2f"'))]
    assert box.count('class="o2-frow') == 2 and box.count('<details class="msel-box" data-no-autosubmit>') == 3 and "Показать" in box
    assert "shop-ms" not in html and "data-fv2" not in html and "<script" not in html and 'name="mixed"' not in html
    assert page(client, "/shop?mixed=1&fw=205&rw=245").count("Комплект") == 0                    # the sets are of «Вид 2» only
    assert "script-src" not in response.headers["Content-Security-Policy"]


def test_the_catalog_of_the_office_is_untouched(tmp_path):
    app, owner, tok, conn = office(tmp_path)
    for state in ("off", "all"):
        view(conn, "office", state)
        html = owner.get("/catalog?w=225").get_data(as_text=True)
        assert "shop-ms" not in html and "shop_v2.js" not in html and "Вид 2" not in html and "filters-view" not in html


# The review of «Вид 2» (2026-11-10): the sets are of one real model, RunFlat with RunFlat, ordered pairs with no mirror, the rear radius
# of the front, the counters and the columns of the client.

class _O:
    def __init__(self, w, h, d, model="M", season="winter", studded=False, runflat=False):
        from types import SimpleNamespace
        self.width, self.height, self.diameter, self.model_raw, self.studded, self.runflat = w, h, d, model, studded, runflat
        self.season = SimpleNamespace(value=season)
        self.stock = 4


def _item(o, brand="a", warehouse=1):
    return cc.Visible(o, 100, warehouse, brand, brand.upper())


def _sets(items, front, rear):
    return cc.mixed_sets(items, cc.filters_of(_Args(), set()), front, rear)


def _widths(sets):
    return [(a.offer.width, b.offer.width) for a, b in sets["kits"]]


def test_positions_without_a_model_are_never_paired():
    f, r = _item(_O(225, 45, 18, model="")), _item(_O(255, 35, 18, model="  "))
    sets = _sets([f, r], (225, None, None), (255, None, None))
    assert sets["kits"] == [] and sets["front"] == [f] and sets["rear"] == [r]


def test_runflat_pairs_only_with_runflat():
    f, r, rf_f, rf_r = (_item(_O(225, 45, 18)), _item(_O(255, 35, 18, runflat=True)), _item(_O(225, 45, 18, runflat=True)),
                        _item(_O(255, 35, 18, runflat=True)))
    assert _sets([f, r], (225, None, None), (255, None, None))["kits"] == []
    assert _widths(_sets([rf_f, rf_r], (225, None, None), (255, None, None))) == [(225, 255)]


def test_overlapping_axes_give_one_ordered_pair_and_each_tyre_once():
    a, b, c = _item(_O(225, 45, 18)), _item(_O(255, 35, 18)), _item(_O(235, 40, 18, model="N"))
    sets = _sets([b, a, c], (None, None, 18), (None, None, 18))
    assert _widths(sets) == [(225, 255)]                          # no (255, 225): the narrower in front, no mirror
    assert sets["front"] == [c] and sets["rear"] == []             # alone: once, never in both «Только …»
    assert all(x is not y for x, y in sets["kits"])


def test_a_tyre_in_a_set_is_in_no_only_list(tmp_path):
    app, client, conn, _ = make_shop(tmp_path)
    to_view2(client)
    html = page(client, "/shop?mixed=1&rw=245&rh=45&rd=18")                                         # the front: any
    got = groups(results(html))
    assert got["Комплект 1"] == [("205/55 R16", "2"), ("245/45 R18", "2")] and found(html) == 1
    assert ("245/45 R18", "2") not in got["Только передняя ось"] and got["Только задняя ось"] == []


def test_an_empty_field_of_an_axis_is_any_value_as_in_mim(tmp_path):
    """The owner 2026-11-10: an empty field is any value (no radius taken from the front, no width asked for the rear)."""
    app, client, conn, _ = make_shop(tmp_path)
    to_view2(client)
    html = page(client, "/shop?mixed=1&fw=205&fh=55&fd=16")
    assert "Укажите ширину задней оси" not in html
    got = groups(results(html))
    assert ("205/55 R16", "2") in [row for k, rows in got.items() if k.startswith("Комплект") and rows != "нет" for row in rows] \
        or got["Только передняя ось"] == [("205/55 R16", "2")]


def test_an_empty_rear_pairs_any_size_of_the_model_and_the_sets_stop_past_the_limit(monkeypatch):
    many = [_item(_O(225, 45, 18))] + [_item(_O(255, 35 + k, 18)) for k in range(10)]
    sets = _sets(many, (225, None, None), (None, None, None))
    assert len(sets["kits"]) == 10 and all(a is many[0] for a, _ in sets["kits"]) and sets["front"] == []
    monkeypatch.setattr(cc, "KITS_MAX", 2)
    assert len(_sets(many, (225, None, None), (255, None, None))["kits"]) == 3                       # KITS_MAX + 1: «more» is known


def test_the_note_of_the_cut_is_of_the_sets_or_of_the_only_lists(tmp_path, monkeypatch):
    app, client, conn, _ = make_shop(tmp_path, tochki=[tochki_row("T1", rest=3)])
    to_view2(client)
    monkeypatch.setattr(cc, "KITS_MAX", 1)
    html = results(page(client, "/shop?mixed=1&rw=245&rh=45&rd=18"))                               # 1 set, 2 fronts alone
    assert "Показаны первые" not in html and "Без пары показаны не все шины" in html


@pytest.mark.parametrize("both", [True, False])
def test_the_sets_keep_the_clients_columns_and_the_warehouse(tmp_path, both):
    app, client, conn, _ = make_shop(tmp_path, tochki=[tochki_row("T1", rest=3)], warehouses=(1, 2) if both else (1,))
    to_view2(client)
    html = results(page(client, MIXED))
    assert "<th>Сезон</th>" in html and 'class="c-season"' in html
    assert ("<th>Склад</th>" in html and '<td class="c-wh">Склад 1</td>' in html) == both and ('class="c-wh"' in html) == both
    user_prefs.put(conn, "buyer", cc.COLUMNS_KEY, ["season", "wh"], NOW)
    html = results(page(client, MIXED))
    assert "<th>Сезон</th>" not in html and 'class="c-season"' not in html and 'class="c-wh"' not in html
    assert groups(html)["Комплект 1"] == [("205/55 R16", "2"), ("245/45 R18", "2")]


def test_view2_has_all_season_and_a_link_with_it_keeps_it(tmp_path):
    app, client, conn, _ = make_shop(tmp_path)
    to_view2(client)
    box = block(page(client, "/shop?s=all"))
    assert 'name="s" value="all" checked' in box and 'title="Всесезонные"' in box and 'name="s" value="" checked' not in box


def test_no_set_of_warehouse_2_when_it_is_off_for_the_client(tmp_path):
    rear = dict(tochki_row("T9", rest=6), thorn=False, width="255", height="40", diameter="18", name="Ikon Tyres Test Model 255/40 R18 99T")
    url = "/shop?mixed=1&fw=205&fh=55&fd=16&rw=255&rh=40&rd=18"
    (tmp_path / "on").mkdir()
    (tmp_path / "off").mkdir()
    app, client, conn, _ = make_shop(tmp_path / "on", tochki=[rear], warehouses=(1, 2))
    to_view2(client)
    assert groups(results(page(client, url)))["Комплект 1"] == [("205/55 R16", "2"), ("255/40 R18", "2")]  # both on: a set of both
    app, client, conn, _ = make_shop(tmp_path / "off", tochki=[rear], warehouses=(1,))
    to_view2(client)
    html = results(page(client, url))
    got = groups(html)
    assert got["Комплект"] == "нет" and got["Только задняя ось"] == [] and "255/40" not in html
