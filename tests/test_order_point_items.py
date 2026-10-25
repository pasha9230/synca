"""Tyres «из наличия точки» that the supplier's unload has not (the owner 2026-10-23, «починить возможность добавлять шины в наличии»): a deal of the
point's stock takes a position typed by its name, chosen in the list as «шина с точки». Its purchase is not invented: none until typed by hand, and
no sale is recorded without it."""
from __future__ import annotations

import pytest

from test_order_window import ICE, created, setup
from panel_helpers import post
from tires_sync import orders, order_templates as tpl, sales, webmim_create
from datetime import date

NAME = "Nokian Hakkapeliitta 9 205/55 R16 зимние шипы"
POINT = dict(goods_id=orders.POINT_GOODS, query=NAME, qty="4", sale_unit="7.500")


def stock_card(client, tok, **more) -> int:
    return created(post(client, "/orders/new", tok, client_name="Иван", deal_kind="stock", **more))


def done(conn, cid):
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", ({s["code"]: s["id"] for s in orders.list_stages(conn)}["done"], cid))
    conn.commit()


def test_the_list_offers_a_point_tyre_only_for_a_deal_of_the_point_stock(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    plain = client.get("/orders/items/find?q=hakkapeliitta 9").get_json()["items"]
    assert plain == []                                                                             # not in the unload: as before
    found = client.get("/orders/items/find?q=hakkapeliitta  9&deal=stock").get_json()["items"]
    assert found == [{"goods_id": "point", "name": "hakkapeliitta 9", "point": True}]
    both = client.get("/orders/items/find?q=autograph snow&deal=stock").get_json()["items"]
    assert [i["goods_id"] for i in both][-1] == "point" and len(both) >= 2 and not both[0].get("point")    # the unload first, the point's tyre last
    assert client.get("/orders/items/find?q=autograph snow&deal=webmim").get_json()["items"] == client.get("/orders/items/find?q=autograph snow").get_json()["items"]


def test_plus_and_save_add_a_point_tyre_to_a_deal_of_the_point_stock_without_a_purchase(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    cid = stock_card(client, tok)
    r = post(client, "/orders/%d/items/add" % cid, tok, **POINT)
    assert r.status_code == 302
    post(client, "/orders/%d/save" % cid, tok, client_name="Иван", deal_kind="stock", goods_id="point", query="Cordiant Snow Cross 2 185/65 R15", qty="2", sale_unit="5000")
    items = orders.get_card(conn, cid)["items"]
    assert [(i["goods_id"], i["name"], i["qty"], i["sale_unit"], i["supplier_price"], i["purchase_manual"], i["code"]) for i in items] == [
        ("point", NAME, 4, 7500.0, 0.0, 1, ""), ("point", "Cordiant Snow Cross 2 185/65 R15", 2, 5000.0, 0.0, 1, "")]
    assert items[0]["season"] == "winter" and items[0]["brand_key"] == "" and items[0]["terms"] == {}     # the season by the name; no supplier's terms
    page = client.get("/orders/%d" % cid).get_data(as_text=True)
    assert "с точки · нет в выгрузке" in page and ">point<" not in page and 'placeholder="не указана"' in page
    assert "Закупку поправили вручную" not in page                                               # nobody typed it yet (review 2026-10-23)
    assert "Добавлено: %s × 4 по 7500 ₽" % NAME in [e["text"] for e in orders.list_events(conn, cid)]


def test_a_new_order_of_the_point_stock_takes_a_point_tyre_with_one_of_the_unload(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    cid = stock_card(client, tok, item_goods_id=["point", ICE["goods_id"]], item_query=[NAME, ICE["query"]], item_qty=["4", "2"], item_sale_unit=["7500", "4800"])
    assert [(i["goods_id"], i["supplier_price"] > 0) for i in orders.get_card(conn, cid)["items"]] == [("point", False), ("101", True)]


def test_a_point_tyre_is_refused_in_a_deal_through_webmim_and_without_a_price(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    cid = created(post(client, "/orders/new", tok, client_name="Иван"))                         # through WebMIM by default
    post(client, "/orders/%d/items/add" % cid, tok, **POINT)
    assert orders.get_card(conn, cid)["items"] == [] and "только в сделку «Из наличия точки»" in client.get("/orders/%d" % cid).get_data(as_text=True)
    r = post(client, "/orders/new", tok, client_name="Пётр", deal_kind="webmim", item_goods_id=["point"], item_query=[NAME], item_qty=["4"], item_sale_unit=["7500"])
    assert r.status_code == 200 and "только в сделку «Из наличия точки»" in r.get_data(as_text=True)
    stock = stock_card(client, tok)
    post(client, "/orders/%d/items/add" % stock, tok, **dict(POINT, sale_unit=""))
    assert orders.get_card(conn, stock)["items"] == [] and "впишите цену за шт." in client.get("/orders/%d" % stock).get_data(as_text=True)
    post(client, "/orders/%d/items/add" % stock, tok, **dict(POINT, query=" "))
    assert orders.get_card(conn, stock)["items"] == []
    with pytest.raises(orders.OrderError):                                                        # the core keeps the rule too
        orders.add_item(conn, cid, goods_id="point", name=NAME, brand_key="", season="all", diameter=0, territory="", qty=1, sale_unit=100,
                        supplier_price=0, terms={}, fee_pct=0, promo_pct=0, now="2026-10-12T09:00:00", author="o")
    with pytest.raises(orders.OrderError):                                                        # a purchase of 0 is the point tyre's only
        orders.add_item(conn, stock, goods_id="101", name="x", brand_key="IKON", season="all", diameter=15, territory="1", qty=1, sale_unit=100,
                        supplier_price=0, terms={}, fee_pct=0, promo_pct=0, now="2026-10-12T09:00:00", author="o")


def test_a_text_the_unload_has_not_tells_how_to_add_it_in_a_deal_of_the_point_stock(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    stock = stock_card(client, tok)
    post(client, "/orders/%d/items/add" % stock, tok, goods_id="", query=NAME, qty="4", sale_unit="7500")
    page = client.get("/orders/%d" % stock).get_data(as_text=True)
    assert orders.get_card(conn, stock)["items"] == [] and "строкой «шина с точки»" in page                  # nothing added by a guess: chosen in the list only
    plain = created(post(client, "/orders/new", tok, client_name="Пётр"))
    post(client, "/orders/%d/items/add" % plain, tok, goods_id="", query=NAME, qty="4", sale_unit="7500")
    assert "строкой «шина с точки»" not in client.get("/orders/%d" % plain).get_data(as_text=True)


def test_no_sale_without_the_purchase_and_the_typed_one_is_the_sale_cost(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    cid = stock_card(client, tok)
    post(client, "/orders/%d/items/add" % cid, tok, **POINT)
    done(conn, cid)
    with pytest.raises(orders.OrderError, match="Не указана закупка: «%s»" % NAME):
        orders.record_sales(conn, cid, "2026-10-12", "2026-10-12T09:00:00", "o", "2026-10-12")
    assert orders.get_card(conn, cid)["sale_ids"] == [] and sales.list_sales(conn) == []
    item = orders.get_card(conn, cid)["items"][0]
    orders.update_item(conn, item["id"], 4, 7500, "2026-10-12T09:00:00", "o", supplier_price="6000")
    assert orders.refresh_purchase(conn, cid, {"point": 1.0}, "unload", "2026-10-12T09:00:00", "o") == 0      # a hand purchase: WebMIM keeps off it
    orders.record_sales(conn, cid, "2026-10-12", "2026-10-12T09:00:00", "o", "2026-10-12")
    sale = sales.list_sales(conn)[0]
    assert (sale["goods_id"], sale["pieces"], sale["sale_total"], sale["cost_total"], sale["brand_key"]) == ("point", 4, 30000.0, 24000.0, "-")


def test_the_message_has_no_code_line_for_a_point_tyre_and_webmim_names_it_if_the_deal_changes(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    cid = stock_card(client, tok)
    post(client, "/orders/%d/items/add" % cid, tok, **POINT)
    post(client, "/orders/%d/items/add" % cid, tok, **ICE)
    full = tpl.values(conn, orders.get_card(conn, cid))["items_full"]
    assert "Товар %s\nКол-во 4 шт\nЦена ед. 7 500 ₽\nСумма 30 000 ₽" % NAME in full and "point" not in full
    assert "Код TS1\nКол-во 2 шт" in full                                             # the unload's position keeps its line
    orders.update_card(conn, cid, {"deal_kind": "webmim"}, "2026-10-12T09:00:00", "o")
    assert "У позиции «%s» нет кода WebMIM." % NAME in webmim_create.problems(conn, orders.get_card(conn, cid), date(2026, 10, 12))


SUGGEST_HARNESS = r"""
const fs = require("fs");
const anyEl = () => new Proxy(function () {}, {get: (t, k) => k === "length" ? 0 : (k === Symbol.iterator ? [][Symbol.iterator] : (k === "forEach" ? () => {} : anyEl())), apply: () => anyEl()});
const el = () => { const e = {children: [], attrs: {}, on: {}, value: "", textContent: "", className: "", hidden: true,
  setAttribute(k, v) { this.attrs[k] = v; }, getAttribute(k) { return this.attrs[k]; }, appendChild(c) { this.children.push(c); return c; },
  addEventListener(t, f) { (this.on[t] = this.on[t] || []).push(f); }, focus() {}}; 
  Object.defineProperty(e, "innerHTML", {set(v) { e.children = []; }, get() { return ""; }}); return e; };
const input = el(), code = el(), list = el(), price = el(), kind = {value: process.argv[3]};
const form = {querySelector: (s) => s === 'select[name="deal_kind"]' ? kind : null};
const row = {querySelector: (s) => s === "[data-find-price]" ? price : null};
const wrap = {getAttribute: () => "/orders/items/find", querySelector: (s) => ({"[data-find-text]": input, "[data-find-code]": code, ".suggest": list})[s] || null,
  closest: (s) => s === "form" ? form : (s === "[data-item-row]" ? row : null)};
const timers = [], urls = [];
global.document = {addEventListener() {}, querySelectorAll: (s) => s === "[data-item-find]" ? [wrap] : [], querySelector: () => null, getElementById: () => null,
  body: anyEl(), documentElement: anyEl(), createElement: () => el(), hidden: false, cookie: ""};
global.window = {addEventListener() {}, location: {}, matchMedia: () => ({matches: false, addEventListener() {}}), FormData: function () {}};
global.location = {}; global.localStorage = {getItem: () => null, setItem() {}}; global.navigator = {};
global.MutationObserver = function () { return {observe() {}}; }; global.Event = function (t) { this.type = t; };
global.setTimeout = (f) => { timers.push(f); return timers.length; }; global.clearTimeout = () => {};
global.fetch = (url) => { urls.push(url); return Promise.resolve({ok: true, redirected: false, json: () => Promise.resolve({items: [
  {goods_id: "101", name: "Autograph Ice 9", size: "195/65 R15", stock: 8, price: 4800}, {goods_id: "point", name: "hakka 9", point: true}]})}); };
try { eval(fs.readFileSync(process.argv[2], "utf8")); } catch (e) { console.error("stopped:", e.message); }
const choose = (i) => { const li = list.children[i < 0 ? list.children.length + i : i]; (li.on.mousedown || []).forEach((f) => f({preventDefault() {}})); };
const after = process.argv[4] === "after";
input.value = "hakka 9"; (input.on.input || []).forEach((f) => f({})); timers.splice(0).forEach((f) => f());
setImmediate(() => setImmediate(() => {
  if (after) { choose(0); (input.on.focus || []).forEach((f) => f({})); }      // an unload position first: its price came into the field
  setImmediate(() => setImmediate(() => finish()));
}));
function finish() {
  const last = list.children[list.children.length - 1];
  const out = {url: urls[0], label: last && last.children.map((c) => c.textContent)};
  (last.on.mousedown || []).forEach((f) => f({preventDefault() {}}));
  out.code = code.value; out.text = input.value; out.price = price.value;
  console.log(JSON.stringify(out));
}
"""


def test_the_list_of_the_window_asks_with_the_deal_and_a_point_tyre_is_chosen_with_its_code_and_no_price(tmp_path):
    """app.js «подсказки позиции»: the deal of the same window goes with the query; the point's line keeps the text, sets the code «point» and
    leaves the price to the manager."""
    import json
    import shutil
    import subprocess
    from pathlib import Path
    node = shutil.which("node")
    if not node:
        pytest.skip("no node")
    harness = tmp_path / "harness.js"
    harness.write_text(SUGGEST_HARNESS, encoding="utf-8")
    app_js = Path(__file__).resolve().parents[1] / "src" / "tires_sync" / "panel" / "static" / "app.js"
    run = lambda deal, *more: json.loads(subprocess.run([node, str(harness), str(app_js), deal, *more], capture_output=True, text=True, timeout=30, check=True).stdout.strip().splitlines()[-1])
    stock = run("stock")
    assert stock == {"url": "/orders/items/find?q=hakka%209&deal=stock", "label": ["«hakka 9»", "шина с точки, нет в выгрузке WebMIM", "впишите цену"],
                     "code": "point", "text": "hakka 9", "price": ""}
    assert run("webmim")["url"] == "/orders/items/find?q=hakka%209"                             # another deal: the query of before
    after = run("stock", "after")                                                                # review 2026-10-23: the ad's price must not go with it
    assert (after["code"], after["price"]) == ("point", "")
