"""The block of the filters of the office's catalog as the catalog of MIM (lane office-catalog-mim, the owner 2026-11-11; the client's
«Вид 2» the sample): three glued fields of ANY size with chips, typed digits narrow the list, Tab takes the typed value and goes on
(static/office_catalog.js), the rows asked anew without «Показать»; «Производитель» with «Только / Кроме»; the season with «Всесезонные»;
«Импортные / Отечественные» by the country, «В наличии от N шт.», «Цена от / до»; «Разноширокие» — «Комплект N», then each axis alone;
winter with studs as one icon. The old view of the office and the client's catalog stay as they were."""
from __future__ import annotations

import logging
import os
import re
import threading
from pathlib import Path

import pytest
from werkzeug.datastructures import MultiDict

from helpers import catalog_xml, commodity_xml
from test_catalog_filters import codes, setup as office
from test_catalog_multi import filters, view
from tires_sync.panel import client_catalog as cc, routes_catalog

JS = Path(__file__).resolve().parents[1] / "src" / "tires_sync" / "panel" / "static" / "office_catalog.js"


def tire(code, w, h, d, model="Test Model", country="Россия", season="Зимняя", rest="12", price="5000", name="IKON", studs=False):
    full = "%s %d/%d/%d T 94 %s%s" % (name, w, h, d, model, " Ш." if studs else "")
    return commodity_xml(NNOMMODIF=code[1:], SMNFCODE=code, SMODEL=model, SBRANDCOUNTRY=country, SSEASON=season, SWIDTH=str(w), SHEIGHT=str(h),
                         SDIAMETR=str(d), NREST=rest, NPRICE_DELAYED=price, SMODIFNAME=full, SMODIFNAME_ORIG=full)


ROWS = (tire("C1", 225, 45, 18), tire("C2", 255, 40, 18), tire("C3", 205, 55, 16, country="Корея", season="Всесезонная", rest="4", price="3000"),
        tire("C4", 215, 55, 16, country="", season="Летняя", rest="6", price="9000"), tire("C5", 225, 45, 18, model="Other Model"),
        tire("C6", 245, 40, 18, model="Other Model"))


def catalog(tmp_path, rows=ROWS, state="all"):
    app, owner, tok, conn = office(tmp_path)
    open(app.config["UNLOAD_PATH"], "wb").write(catalog_xml(*rows))
    view(conn, "office", state)
    return app, owner, conn


def get(owner, q=""):
    return owner.get("/catalog" + q).get_data(as_text=True)


# ---- the server: the new filters --------------------------------------------------------------------------------------------------

def test_all_season_import_domestic_stock_and_price(tmp_path):
    app, owner, conn = catalog(tmp_path)
    assert codes(get(owner, "?s=all")) == ["C3"]                                        # «Всесезонные» understood
    assert codes(get(owner, "?s=summer")) == ["C4"]
    assert codes(get(owner, "?c=dom")) == ["C1", "C2", "C5", "C6"]                       # Россия: «Отечественные»
    assert codes(get(owner, "?c=imp")) == ["C3"]                                         # another country; no country: neither
    assert codes(get(owner, "?sn=6")) == ["C1", "C2", "C4", "C5", "C6"] and codes(get(owner, "?sn=12")) == ["C1", "C2", "C5", "C6"]
    assert codes(get(owner, "?pf=4000&pt=6000")) == ["C1", "C2", "C5", "C6"]                # C3, C4: no price of ours (season off)
    assert codes(get(owner, "?c=x&sn=abc&pf=-1")) == ["C1", "C2", "C3", "C4", "C5", "C6"]    # a value not understood is no filter


def test_the_price_range_is_of_our_price(tmp_path):
    app, owner, conn = catalog(tmp_path, (tire("C1", 205, 55, 16, price="3000"), tire("C2", 215, 55, 16, price="5000"),
                                          tire("C3", 225, 55, 16, price="9000")))
    assert codes(get(owner, "?pt=4000")) == ["C1"] and codes(get(owner, "?pf=8000")) == ["C3"]
    assert codes(get(owner, "?pf=4000&pt=8000")) == ["C2"] and codes(get(owner, "?pf=1&pt=99999")) == ["C1", "C2", "C3"]


def test_origin_of_a_position():
    class O:
        def __init__(self, country):
            self.country = country
    assert [routes_catalog.origin_of(O(c)) for c in ("Россия", " россия ", "Russia", "Корея", "", None)] == ["dom", "dom", "dom", "imp", "", ""]


def test_any_size_is_in_the_lists_and_a_size_not_in_stock_finds_nothing(tmp_path):
    app, owner, conn = catalog(tmp_path)
    html = get(owner, "?w=125")
    box = filters(html)
    w = re.search(r'data-oc-ms data-name="w">(.*?)</ul></div></div>', box, re.S).group(1)
    assert all('data-v="%d"' % v in w for v in cc.ANY_SIZES["w"]) and 'data-v="255"' in w            # any size and the stock's
    assert 'name="w" value="125"' in w and 'aria-selected="true">125<' in w                            # the chosen one: a chip, ticked
    assert codes(html) == [] and "Ничего не найдено" in html
    d = re.search(r'data-oc-ms data-name="d" data-prefix="R">(.*?)</ul></div></div>', box, re.S).group(1)
    assert 'data-v="10">R10<' in d and 'data-v="24">R24<' in d


def test_the_block_has_three_glued_fields_in_tab_order_and_no_show_button(tmp_path):
    app, owner, conn = catalog(tmp_path)
    html = get(owner, "?w=225&w=255&h=40")
    box = filters(html)
    assert 'data-oc' in box and "data-autosubmit" not in box and "<details" not in box
    glue = re.search(r'<div class="oc-glue" role="group" aria-label="Размер">(.*?)</div>\{?', box, re.S)
    assert glue and re.findall(r'id="f-(\w)" name="\w"', box) == ["w", "h", "d"]                      # Tab: ширина → профиль → диаметр
    assert re.findall(r'data-oc-n>(\d+)<', box)[:2] == ["2", "1"]                                        # the counters
    assert box.count('data-oc-x tabindex="-1"') == 3                                                     # a cross never takes Tab
    assert re.sub(r"<noscript>.*?</noscript>", "", box, flags=re.S).count(">Показать<") == 0 and "<noscript>" in box
    assert "office_catalog.js" in html and "office_catalog.css" in html and "shop_v2.js" not in html


def test_the_brands_with_only_and_except(tmp_path):
    app, owner, conn = catalog(tmp_path)
    html = get(owner, "?bm=x&xb=IKON+TYRES")
    box = filters(html)
    assert 'name="bm" value="x" checked' in box and 'data-name="xb" data-text' in box
    assert 'class="oc-ms-chip oc-ms-chip--x"><input type="hidden" name="xb" value="IKON TYRES"' in box
    assert codes(html) == []
    html = get(owner, "?b=IKON+TYRES&sn=12")
    assert 'name="b" value="IKON TYRES"' in filters(html) and 'name="bm" value="" checked' in filters(html)
    assert codes(html) == ["C1", "C2", "C5", "C6"]
    url = re.search(r'name="b" value="IKON TYRES"><span>[^<]*</span><a class="oc-ms-x" href="([^"]+)"', filters(html)).group(1)
    assert "b=" not in url.replace("&amp;", "&").split("?")[1] and "sn=12" in url                     # the cross: the page without it


def test_mixed_width_sets_then_each_axis(tmp_path):
    app, owner, conn = catalog(tmp_path)
    html = get(owner, "?mixed=1&fw=225&rw=255")
    table = html.split('id="items"')[1]
    heads = re.findall(r'scope="colgroup">([^<]+)<', table)
    assert heads == ["Комплект 1", "Только передняя ось", "Только задняя ось"]
    kit = table[table.index("Комплект 1"):table.index("Только передняя ось")]
    assert re.findall(r'o2-kit-axis">(\w+)<', kit) == ["Перед", "Зад"] and kit.index(">C1<") < kit.index(">C2<")
    front = table[table.index("Только передняя ось"):table.index("Только задняя ось")]
    assert re.findall(r">(C\d)<", front) == ["C5"]                                              # Other Model has no 255 rear
    assert "Комплектов 1 · только передняя 1 · только задняя 0" in html
    box = filters(html)
    assert 'name="mixed" value="1" checked' in box and 'id="f-fw" name="fw" value="225"' in box and 'id="f-rw" name="rw" value="255"' in box
    assert 'id="f-w"' not in box and 'name="size"' not in box                                    # the axes stand for the size fields


def test_mixed_partial_axes_are_any_and_no_axis_asks(tmp_path):
    app, owner, conn = catalog(tmp_path)
    table = get(owner, "?mixed=1&fd=18").split('id="items"')[1]                       # the front R18, the rear any: a set of each model
    kits = re.findall(r"Комплект \d", table)
    assert kits == ["Комплект 1", "Комплект 2"]
    rear = table[table.index("Только задняя ось"):]
    assert "Укажите размер задней оси" in rear and not re.findall(r">(C\d)<", rear)    # the rear not given: not the whole catalog
    assert "Укажите размеры осей" in get(owner, "?mixed=1")
    assert "Укажите размеры осей" in get(owner, "?mixed=1&w=225")                      # the size fields are not read in this mode
    assert codes(get(owner, "?mixed=1&fw=225&rw=255&c=imp")) == []                      # the other filters act on both axes


def test_the_sets_of_the_office_and_of_the_client_are_paired_alike():
    from types import SimpleNamespace as N
    o = lambda w, h, d, model="M": N(width=w, height=h, diameter=d, model_raw=model, studded=False, runflat=False, season=N(value="winter"))
    offers = [o(225, 45, 18), o(255, 40, 18), o(245, 40, 18), o(225, 45, 18, "X")]
    rows = [{"goods_id": str(k), "brand": "a", "size": "%d/%d R%d" % (x.width, x.height, x.diameter)} for k, x in enumerate(offers)]
    by = {str(k): x for k, x in enumerate(offers)}
    office = routes_catalog._mixed(rows, MultiDict([("fw", "225"), ("rd", "18")]), by)
    client = cc.pair_axes([cc.Visible(x, 1, 1, "a", "A") for x in offers], lambda i: (i.offer.width, i.offer.height, i.offer.diameter),
                          cc._model_key, (225, None, None), (None, None, 18))
    assert [(a["goods_id"], b["goods_id"]) for a, b in office["kits"]] == [(str(offers.index(a.offer)), str(offers.index(b.offer)))
                                                                         for a, b in client["kits"]] == [("0", "1"), ("0", "2")]


# ---- the icons, the old view, the client --------------------------------------------------------------------------------------------

def test_winter_with_studs_is_one_icon(tmp_path):
    app, owner, conn = catalog(tmp_path, (tire("C1", 205, 55, 16, studs=True), tire("C2", 205, 55, 16, model="Other Model")))
    table = get(owner).split('id="items"')[1]
    assert table.count("o2-season--wstud") == 1 and 'class="o2-spike"' in table and "o2-season--stud" not in table
    assert 'title="Зима, шипы"' in filters(get(owner))                                        # the filter «Шипы» draws the same icon


def test_the_old_view_of_the_office_is_kept(tmp_path):
    app, owner, conn = catalog(tmp_path, state="off")
    html = get(owner, "?w=225")
    assert "office_catalog.js" not in html and "oc-ms" not in html and codes(html) == ["C1", "C5"]


# ---- the script --------------------------------------------------------------------------------------------------------------------

def test_the_script_narrows_by_one_function_and_sends_a_second_later():
    js = JS.read_text(encoding="utf-8")
    assert js.count("function matches(") == 1 and "matches(textOf(o), t, text)" in js
    assert "value.indexOf(typed) === 0" in js                                                    # as MIM: the values starting with the typed
    assert "DELAY = 1000" in js and "history.replaceState" in js and 'e.key === "Tab"' in js
    assert "keyup" not in js and "eval(" not in js and "innerHTML = html" not in js


sync_api = None
try:
    from playwright import sync_api                                                            # noqa: E402 - optional, a browser test
except ImportError:                                                                           # pragma: no cover
    pass
CHROMIUM = "/opt/pw-browsers/chromium"


@pytest.fixture
def served(tmp_path):
    if sync_api is None:
        pytest.skip("no Playwright")
    from werkzeug.serving import make_server
    app, owner, conn = catalog(tmp_path)
    owner.get("/catalog")
    cookie = owner.get_cookie("session").value
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield dict(cookie=cookie, base="http://127.0.0.1:%d" % server.server_port)
    server.shutdown()


def test_tab_takes_the_typed_values_and_the_rows_come_without_a_reload(served):
    """In a browser: «2» narrows the widths to those starting with 2; «205» Tab «55» Tab «16» Tab: three chips, the focus went
    ширина → профиль → диаметр; a second later the rows and the address are the size's, the page never reloaded."""
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch(executable_path=CHROMIUM) if os.path.exists(CHROMIUM) else p.chromium.launch()
        except Exception as err:                                                              # noqa: BLE001 - no browser on this machine
            pytest.skip("no Chromium: %s" % err)
        try:
            ctx = b.new_context()
            ctx.add_cookies([{"name": "session", "value": served["cookie"], "domain": "127.0.0.1", "path": "/"}])
            page = ctx.new_page()
            page.goto(served["base"] + "/catalog")
            page.evaluate("window.__same = 1")
            page.click("#f-w")
            page.keyboard.type("2")
            shown = page.eval_on_selector_all("#oc-w-list [role=option]:not([hidden])", "l => l.map(x => x.textContent)")
            assert shown and all(v.startswith("2") for v in shown) and shown == sorted(shown, key=int)
            page.keyboard.type("05")
            page.keyboard.press("Tab")
            assert page.evaluate("document.activeElement.id") == "f-h"
            page.keyboard.type("55")
            page.keyboard.press("Tab")
            assert page.evaluate("document.activeElement.id") == "f-d"
            page.keyboard.type("16")
            page.keyboard.press("Tab")
            chips = page.eval_on_selector_all(".oc-ms-chip input", "l => l.map(x => x.name + '=' + x.value)")
            assert chips == ["w=205", "h=55", "d=16"]
            page.locator("#items [data-found]", has_text="Найдено 1 ").wait_for(timeout=5000)
            assert "w=205" in page.url and "h=55" in page.url and "d=16" in page.url
            assert page.evaluate("window.__same") == 1                                                # no reload
            assert ">C3<" in page.inner_html("#items")
        finally:
            b.close()
