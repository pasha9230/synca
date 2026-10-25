from __future__ import annotations

import re
from pathlib import Path

from helpers import catalog_xml, commodity_xml
from panel_helpers import login, make_app
from tires_sync.panel import service


def app_with(tmp_path, *rows):
    app = make_app(tmp_path)
    client = app.test_client()
    login(client)
    open(app.config["UNLOAD_PATH"], "wb").write(catalog_xml(*rows))
    return client


ROWS = (
    commodity_xml(NNOMMODIF="1", SMODIFNAME="IKON 205/55/16 T 94 Test Model Ш.", SMODIFNAME_ORIG="IKON 205/55/16 T 94 Test Model Ш.", NREST="12", NPRICE_DELAYED="5000", SMNFCODE="C1"),
    commodity_xml(NNOMMODIF="2", SMODIFNAME="IKON 215/60/17 T 94 Test Model", SMODIFNAME_ORIG="IKON 215/60/17 T 94 Test Model", SWIDTH="215", SHEIGHT="60", SDIAMETR="17",
                  NREST="2", NPRICE_DELAYED="7000", SMNFCODE="C2"),
    commodity_xml(NNOMMODIF="3", SSEASON="Летняя", SMARKA="Лауфенн", SMODIFNAME="LAUFENN 195/65/15 G Fit", SMODIFNAME_ORIG="LAUFENN 195/65/15 G Fit", SMODEL="G Fit",
                  SDIAMETR="15", SWIDTH="195", SHEIGHT="65", NREST="9", NPRICE_DELAYED="3000", SMNFCODE="C3"),
)


def names(html):
    return re.findall(r'class="gname">([^<]+)<', html)


def test_catalog_is_in_the_menu_and_groups_goods_by_brand_with_status(tmp_path):
    client = app_with(tmp_path, *ROWS)
    html = client.get("/catalog").get_data(as_text=True)
    assert 'href="/catalog"' in html
    assert "<details" not in html and "IKON TYRES" in html and "LAUFENN" in html
    assert ">C1<" in html and ">C2<" in html
    assert "в фиде" in html and "остаток ниже порога" in html
    assert "5\u00a0000\u00a0₽" in html and "4\u00a0810\u00a0₽" in html and "<td class=\"num\">—</td>" in html
    assert "сочетание не включено" in html


def test_status_filter_season_studs_size_and_producer(tmp_path):
    client = app_with(tmp_path, *ROWS)
    get = lambda q: client.get("/catalog" + q).get_data(as_text=True)
    assert ">C2<" not in get("?st=feed") and ">C1<" in get("?st=feed")
    only_no = get("?st=no")
    assert ">C2<" in only_no and ">C1<" not in only_no
    assert ">C3<" in get("?s=summer") and ">C1<" not in get("?s=summer")
    assert ">C1<" in get("?t=studded") and ">C2<" not in get("?t=studded")
    assert ">C2<" in get("?d=17") and ">C1<" not in get("?d=17")
    assert ">C3<" in get("?q=lauf") and ">C1<" not in get("?q=lauf")
    assert "Ничего не найдено" in get("?q=nothing")


def test_sorting_by_price_stock_name_and_diameter_with_direction(tmp_path):
    client = app_with(tmp_path, *ROWS)
    order = lambda q: re.findall(r">(C[123])<", client.get("/catalog?q=ikon" + q).get_data(as_text=True))
    assert order("&o=price") == ["C1", "C2"] and order("&o=price&r=1") == ["C2", "C1"]
    assert order("&o=stock") == ["C2", "C1"] and order("&o=stock&r=1") == ["C1", "C2"]
    assert order("&o=diameter") == ["C1", "C2"] and order("&o=diameter&r=1") == ["C2", "C1"]
    html = client.get("/catalog?o=price").get_data(as_text=True)
    assert "o=stock" in html and "o=brand" in html and "o=name" in html


def _unpriced(first_purchase, second_purchase):
    """Two summer LAUFENN positions (the combination is not on: no sale price), the purchase prices given."""
    row = lambda n, model, purchase, code: commodity_xml(
        NNOMMODIF=n, SSEASON="Летняя", SMARKA="Лауфенн", SMODIFNAME="LAUFENN 195/65/15 " + model, SMODIFNAME_ORIG="LAUFENN 195/65/15 " + model,
        SMODEL=model, SDIAMETR="15", SWIDTH="195", SHEIGHT="65", NREST="9", NPRICE_DELAYED=purchase, SMNFCODE=code)
    return row("11", "Alpha", first_purchase, "U1"), row("12", "Beta", second_purchase, "U2")


def test_sorting_by_price_never_follows_the_purchase_price(tmp_path):
    """The purchase price is money: positions without a sale price go last, by name, whatever they cost to buy (both directions, both views)."""
    for n, purchases in enumerate((("9000", "1000"), ("1000", "9000"))):
        (tmp_path / str(n)).mkdir()
        client = app_with(tmp_path / str(n), ROWS[0], *_unpriced(*purchases))
        order = lambda q: re.findall(r">([CU][12])<", client.get("/catalog?o=price" + q).get_data(as_text=True))
        assert order("") == ["C1", "U1", "U2"], purchases
        assert order("&r=1") == ["C1", "U1", "U2"], purchases


def test_flat_and_grouped_price_sort_put_the_unpriced_last_by_brand_and_name():
    item = lambda brand, name, price, purchase: {"brand": brand, "name": name, "price": price, "supplier_price": purchase}
    groups = [{"items": [item("B", "b2", None, 1), item("B", "b1", None, 9), item("B", "b0", 500, 1)]},
              {"items": [item("A", "a1", None, 5), item("A", "a0", 700, 1)]}]
    flat = lambda desc: [i["name"] for i in service.flat_items(groups, "price", desc)]
    assert flat(False) == ["b0", "a0", "a1", "b1", "b2"]
    assert flat(True) == ["a0", "b0", "a1", "b1", "b2"]


def test_page_explains_percents_and_has_segmented_filters_that_submit_themselves(tmp_path):
    html = app_with(tmp_path, *ROWS).get("/catalog").get_data(as_text=True)
    assert "Бренды и наценки" in html and "Сопоставления" in html
    assert html.count('class="seg"') == 2
    for label in ("Все", "Лето", "Зима", "Шипы", "Без шипов", "В фиде", "Не в фиде", "Нет пары"):
        assert label in html
    assert 'name="s" value="summer"' in html and 'name="t" value="studded"' in html and '<option value="feed"' in html
    assert 'aria-label="Ширина"' in html and 'aria-label="Профиль"' in html and 'aria-label="Диаметр"' in html
    assert 'placeholder="Размер, название, артикул"' in html                                          # the search is not only by the maker any more
    assert "data-autosubmit" in html and "onchange=" not in html


def test_winter_split_shows_studded_and_friction_models_with_icons(tmp_path):
    html = app_with(tmp_path, *ROWS).get("/catalog").get_data(as_text=True)
    assert 'title="Шипы"' in html and 'title="Зима · шипы"' in html
    assert '<span class="season" title="Зима"><span class="ico-snow">' in html


def test_status_filters_for_unmatched_models_and_real_problems(tmp_path):
    client = app_with(tmp_path, *ROWS)
    get = lambda q: client.get("/catalog" + q).get_data(as_text=True)
    assert "Нет пары" in get("") and "Проблемы" in get("")
    assert ">C1<" not in get("?st=problem") and "Ничего не найдено" in get("?st=problem")
    assert "Ничего не найдено" in get("?st=nopair")           # every model of this data set has its pair


def test_supplier_price_column_can_be_hidden_on_screen_only(tmp_path):
    client = app_with(tmp_path, *ROWS)
    html = client.get("/catalog").get_data(as_text=True)
    assert html.count('class="num col-sp"') == 4  # header + three rows
    assert "Цена поставщика" in html and 'data-toggle-sp' in html and 'aria-pressed="false"' in html
    table = client.get("/catalog/table").get_data(as_text=True)
    assert 'data-toggle-sp' in table and 'class="num col-sp"' in table
    css = (Path(__file__).resolve().parents[1] / "src/tires_sync/panel/static/panel.css").read_text(encoding="utf-8")
    assert "body.hide-sp .col-sp{display:none}" in css


def test_page_size_switch_limits_rows_keeps_filters_and_ignores_bad_values(tmp_path):
    client = app_with(tmp_path, *ROWS)
    assert len(names(client.get("/catalog?n=10").get_data(as_text=True))) == 3
    for bad in ("7", "abc", "1000", "0", "-5"):
        assert len(names(client.get("/catalog?n=" + bad).get_data(as_text=True))) == 3
    html = client.get("/catalog?n=10&s=winter&q=IKON").get_data(as_text=True)
    assert 'class="is-active" aria-current="true">10<' in html
    for n in (10, 50, 100, 200, 500):
        assert re.search(r'href="/catalog\?[^"]*n=%d[^"]*"' % n, html)
    assert "s=winter" in re.search(r'href="(/catalog\?[^"]*n=50[^"]*)"', html).group(1)
    assert 'name="n" value="10"' in html


def test_page_size_paginates(tmp_path):
    rows = tuple(
        commodity_xml(NNOMMODIF=str(100 + k), SMODIFNAME="IKON 205/55/16 T 94 Test Model %d" % k, NREST="12",
                      NPRICE_DELAYED="5000", SMNFCODE="X%d" % k) for k in range(12))
    client = app_with(tmp_path, *rows)
    first = client.get("/catalog?n=10").get_data(as_text=True)
    assert len(names(first)) == 10 and "страница 1 из 2" in first
    assert len(names(client.get("/catalog?n=10&p=2").get_data(as_text=True))) == 2
    assert "страница" not in client.get("/catalog?n=50").get_data(as_text=True)


def test_size_filter_accepts_several_values_per_field(tmp_path):
    client = app_with(tmp_path, *ROWS)
    html = client.get("/catalog?w=205,215").get_data(as_text=True)
    assert len(names(html)) == 2 and ">C1<" in html and ">C2<" in html and ">C3<" not in html
    assert len(names(client.get("/catalog?w=205+215&d=16;17").get_data(as_text=True))) == 2
    assert len(names(client.get("/catalog?w=205,215&d=17").get_data(as_text=True))) == 1  # fields combine with "and"
    assert len(names(client.get("/catalog?d=15,17").get_data(as_text=True))) == 2
    assert len(names(client.get("/catalog?w=205,%20%20215,205").get_data(as_text=True))) == 2  # spaces and repeats
    assert len(names(client.get("/catalog?w=abc").get_data(as_text=True))) == 3  # not a number: no filter
    assert len(names(client.get("/catalog?w=999").get_data(as_text=True))) == 0
    html = client.get("/catalog?w=205,215&h=55").get_data(as_text=True)
    assert 'name="w" value="205, 215"' in html and 'name="h" value="55"' in html
    assert len(names(client.get("/catalog/table?w=205,215").get_data(as_text=True))) == 2


def test_size_filter_keeps_the_single_value_form_and_caps_the_list(tmp_path):
    from tires_sync.panel import service
    from tires_sync.panel.routes_catalog import MAX_SIZE_VALUES, _nums
    assert _nums({"w": "205"}, "w") == [205] and _nums({}, "w") is None and _nums({"w": " ,; "}, "w") is None
    assert len(_nums({"w": ",".join(str(i) for i in range(100, 150))}, "w")) == MAX_SIZE_VALUES

    class Offer:
        def __init__(self, w):
            self.width, self.height, self.diameter = w, 55, 16
    parsed = type("P", (), {"offers": {"a": Offer(205), "b": Offer(215)}})()
    assert set(service.filter_offers(parsed, 205).offers) == {"a"}
    assert set(service.filter_offers(parsed, [205, 215]).offers) == {"a", "b"}
    assert service.filter_offers(parsed, []) is parsed


def test_the_search_finds_by_size_by_part_of_the_name_by_article_and_by_several_words(tmp_path):
    client = app_with(tmp_path, *ROWS)
    found = lambda q: sorted(re.findall(r">(C[123])<", client.get("/catalog?q=" + q).get_data(as_text=True)))
    assert found("205/55") == ["C1"] and found("205 55 16") == ["C1"] and found("20555r16") == ["C1"] and found("205/55/16") == ["C1"]
    assert found("215/60/17") == ["C2"] and found("215%2F60%20R17") == ["C2"] and found("195 65 15") == ["C3"]
    assert found("test model") == ["C1", "C2"] and found("g fit") == ["C3"] and found("TEST") == ["C1", "C2"]
    assert found("c2") == ["C2"] and found("ikon%20215") == ["C2"] and found("ikon%20g%20fit") == []
    assert found("lauf") == ["C3"] and found("nothing") == [] and found("") == ["C1", "C2", "C3"]
    assert found("  205   55  ") == ["C1"]                                                              # spaces do not matter


def test_a_word_of_the_search_never_matches_across_the_border_of_two_fields(tmp_path):
    client = app_with(tmp_path, *ROWS)
    found = lambda q: sorted(re.findall(r">(C[123])<", client.get("/catalog?q=" + q).get_data(as_text=True)))
    assert found("c1205") == [] and found("1620") == []                    # the article «C1» and the size «205/55/16» are two fields, not one string
    assert found("c1") == ["C1"] and found("c2") == ["C2"]                                          # inside one field it still finds


def test_a_position_the_supplier_took_away_stays_in_the_catalog_with_stock_zero_and_the_date(tmp_path):
    from helpers import make_offer
    from panel_helpers import make_app
    from tires_sync import catalog_archive, store
    from tires_sync.webmim import ParseResult
    app = make_app(tmp_path)
    client = app.test_client()
    login(client)
    open(app.config["UNLOAD_PATH"], "wb").write(catalog_xml(*ROWS))
    conn = store.connect(app.config["DB_PATH"])
    old = make_offer(goods_id="900", code="GONE1", name="IKON 175/70/13 T 82 Old Model", name_orig="IKON 175/70/13 T 82 Old Model", model_raw="Old Model",
                     width=175, height=70, diameter=13, stock=8, base_price=3300.0)
    catalog_archive.record(conn, ParseResult(offers={"900": old, "901": make_offer(goods_id="901", code="K")}), "2026-10-10T10:00:00")
    catalog_archive.record(conn, ParseResult(offers={"901": make_offer(goods_id="901", code="K")}), "2026-10-15T10:00:00")
    get = lambda q: client.get("/catalog" + q).get_data(as_text=True)
    page = get("")
    assert ">GONE1<" in page and "нет у поставщика с 15.10.2026" in page and ">C1<" in page             # shown next to the live ones, with the date
    only = get("?st=gone")
    assert ">GONE1<" in only and ">C1<" not in only and ">C2<" not in only
    assert ">GONE1<" not in get("?st=feed") and ">GONE1<" not in get("?st=no")                          # it is neither in the feed nor «not in the feed»
    assert ">GONE1<" in get("?q=175/70") and ">GONE1<" in get("?q=old%20model") and ">GONE1<" in get("?d=13")
    assert ">GONE1<" not in get("?d=16") and ">GONE1<" not in get("?s=summer") and ">GONE1<" not in get("?q=lauf")
    assert "op\" value=\"on:900" not in page and "goods_id=900" not in page                          # nothing can be sold, ordered or added from it


def test_a_gone_position_that_came_back_in_a_fresher_unload_is_shown_once_and_a_gone_row_has_no_checkbox_or_preview(tmp_path):
    from helpers import make_offer
    from panel_helpers import make_app
    from tires_sync import catalog_archive, store
    from tires_sync.webmim import ParseResult
    app = make_app(tmp_path)
    client = app.test_client()
    login(client)
    open(app.config["UNLOAD_PATH"], "wb").write(catalog_xml(*ROWS))
    conn = store.connect(app.config["DB_PATH"])
    old = make_offer(goods_id="900", code="GONE1", name="IKON 175/70/13 T 82 Old Model", name_orig="IKON 175/70/13 T 82 Old Model", model_raw="Old Model",
                     width=175, height=70, diameter=13)
    same = make_offer(goods_id="1", code="C1")                                            # «1» is a live position of the page: the archive marked it gone in the past
    catalog_archive.record(conn, ParseResult(offers={"900": old, "1": same, "901": make_offer(goods_id="901", code="K")}), "2026-10-10T10:00:00")
    catalog_archive.record(conn, ParseResult(offers={"901": make_offer(goods_id="901", code="K")}), "2026-10-15T10:00:00")
    page = client.get("/catalog").get_data(as_text=True)
    assert page.count(">C1<") == 1 and page.count(">GONE1<") == 1
    gone_row = page.split(">GONE1<")[1].split("</tr>")[0]
    assert 'name="g"' not in page.split(">GONE1<")[0].rsplit("<tr", 1)[1] and "data-preview" not in gone_row
