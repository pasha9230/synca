"""The sum of a return is pre-filled when it is empty (the owner 2026-10-26): «возврат поставщику» — the purchase from the supplier,
«клиент вернул нам» — the sale. The manager types or keeps it and saves: opening the page saves nothing, a typed or a saved sum is never
replaced, and who has no right «money» (a point too) gets no purchase in the page at all."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from panel_helpers import login, make_app, post
from test_roles import sign_in
from tires_sync import order_points as points, orders, store, users

NOW = "2026-10-12T09:00:00"
COST, SALE = "15670", "20492"          # 4 × 3917.5 and 4 × 5123: numbers seen nowhere else on the page
COST_SPELLINGS = ("15670", "15 670", "15 670", "3917", "3 917", "3 917")


def setup(tmp_path, deal_kind="webmim"):
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 10, 12, 9, 0, 0))
    client = app.test_client()
    tok = login(client)
    client.get("/orders")                                                                           # seeds the pipeline
    conn = store.connect(app.config["DB_PATH"])
    pid = points.save_point(conn, {"name": "Гамма"})
    cid = orders.create_card(conn, dict(client_name="Иван", client_phone="+7 900 111 22 33", point_id=pid, deal_kind=deal_kind), NOW, "o")
    orders.add_item(conn, cid, goods_id="1", name="Nokian 205/55 R16", brand_key="nokian", season="winter", diameter=16, territory="1",
                    qty=4, sale_unit=5123, supplier_price=3917.5, terms=None, fee_pct=0, promo_pct=0, now=NOW, author="o")
    return app, client, tok, conn, pid, cid


def stage(conn, cid, code, **fields):
    """Puts the card on a stage as stored (the move itself asks for the sum: here the sum is still empty)."""
    sid = {s["code"]: s["id"] for s in orders.list_stages(conn)}[code]
    sets = ", ".join(["stage_id = ?"] + ["%s = ?" % k for k in fields])
    conn.execute("UPDATE order_card SET %s WHERE id = ?" % sets, (sid, *fields.values(), cid))
    conn.commit()


def amount_input(page, start=0) -> str:
    return re.search(r'<input[^>]*name="return_amount"[^>]*>', page[start:]).group(0)


def value(tag) -> str:
    return re.search(r'value="([^"]*)"', tag).group(1)


def finish(page) -> str:
    return page[page.index('id="finish-window"'):]


def stored_amount(conn, cid) -> float:
    return float(orders._row(conn, cid)["return_amount"] or 0)


def attr(tag, name) -> str:
    m = re.search(r'%s="([^"]*)"' % name, tag)
    return m.group(1) if m else ""


def was_of(page) -> str:
    return re.search(r'name="was" value="([^"]*)"', page).group(1).replace("&#34;", '"').replace("&quot;", '"')


def test_the_order_window_suggests_the_purchase_or_the_sale_greyed_and_never_as_a_value(tmp_path):
    """Every button of the order window («Отвязать», a stage, «+») sends the whole form: a value put in the field would be saved
    without «Сохранить» (review 1). The window shows the suggestion as the placeholder only."""
    app, client, tok, conn, pid, cid = setup(tmp_path)
    stage(conn, cid, "return", return_kind="supplier")
    tag = amount_input(client.get("/orders/%d" % cid).get_data(as_text=True))
    assert value(tag) == "" and attr(tag, "placeholder") == COST and 'data-return-amount="hint"' in tag and 'autocomplete="off"' in tag
    stage(conn, cid, "return", return_kind="client")
    tag = amount_input(client.get("/orders/%d" % cid).get_data(as_text=True))
    assert value(tag) == "" and attr(tag, "placeholder") == SALE
    assert stored_amount(conn, cid) == 0                                                            # opening the page saves nothing


def test_no_kind_chosen_leaves_the_sum_empty_with_both_suggestions_for_the_switch(tmp_path):
    app, client, tok, conn, pid, cid = setup(tmp_path)
    stage(conn, cid, "return")
    tag = amount_input(client.get("/orders/%d" % cid).get_data(as_text=True))
    assert value(tag) == "" and attr(tag, "placeholder") == ""
    assert 'data-suggest-supplier="%s"' % COST in tag and 'data-suggest-client="%s"' % SALE in tag


def test_a_saved_sum_is_never_replaced(tmp_path):
    app, client, tok, conn, pid, cid = setup(tmp_path)
    stage(conn, cid, "return", return_kind="supplier", return_amount=9600)
    tag = amount_input(client.get("/orders/%d" % cid).get_data(as_text=True))
    assert value(tag) == "9600" and "data-return-amount" not in tag and "data-suggest" not in tag and "placeholder" not in tag
    assert stored_amount(conn, cid) == 9600


def test_a_typed_sum_comes_back_as_typed_after_a_refused_save(tmp_path):
    app, client, tok, conn, pid, cid = setup(tmp_path)
    stage(conn, cid, "return", return_kind="supplier")
    page = client.get("/orders/%d" % cid).get_data(as_text=True)
    r = post(client, "/orders/%d/save" % cid, tok, was=was_of(page), return_kind="supplier", return_amount="777", delivery_date="не дата")
    tag = amount_input(r.get_data(as_text=True))
    assert value(tag) == "777" and "data-return-amount" not in tag and stored_amount(conn, cid) == 0


def test_a_wrong_typed_sum_comes_back_as_typed_and_is_not_replaced_by_the_suggestion(tmp_path):
    app, client, tok, conn, pid, cid = setup(tmp_path)
    stage(conn, cid, "return", return_kind="supplier")
    page = client.get("/orders/%d" % cid).get_data(as_text=True)
    r = post(client, "/orders/%d/save" % cid, tok, was=was_of(page), return_kind="supplier", return_amount="abc")
    assert r.status_code == 200 and "Сумма возврата: число." in r.get_data(as_text=True)
    tag = amount_input(r.get_data(as_text=True))
    assert value(tag) == "abc" and "data-return-amount" not in tag and stored_amount(conn, cid) == 0


def test_a_button_of_the_window_that_is_not_the_save_writes_no_sum(tmp_path):
    """What the browser sends from the window as drawn (the field empty, the suggestion only greyed) by «Отвязать» or a stage."""
    app, client, tok, conn, pid, cid = setup(tmp_path)
    stage(conn, cid, "return", return_kind="supplier", return_reason="брак")
    page = client.get("/orders/%d" % cid).get_data(as_text=True)
    post(client, "/orders/%d/save" % cid, tok, was=was_of(page), return_kind="supplier", return_reason="брак",
         return_amount=value(amount_input(page)), chat_unlink="999")
    assert stored_amount(conn, cid) == 0
    post(client, "/orders/%d/save" % cid, tok, was=was_of(page), return_kind="supplier", return_reason="брак", return_amount=COST)
    assert stored_amount(conn, cid) == float(COST)                                                  # typed or accepted and saved: kept


def test_the_window_of_a_return_fills_by_the_kind_and_a_return_made_with_it_keeps_the_sum(tmp_path):
    app, client, tok, conn, pid, cid = setup(tmp_path)
    tag = amount_input(finish(client.get("/orders/%d" % cid).get_data(as_text=True)))
    assert 'value="' not in tag and "data-auto" in tag and 'autocomplete="off"' in tag and 'data-return-amount' in tag and "hint" not in tag
    assert 'data-suggest-supplier="%s"' % COST in tag and 'data-suggest-client="%s"' % SALE in tag
    s = {s["code"]: s["id"] for s in orders.list_stages(conn)}
    post(client, "/orders/%d/move" % cid, tok, stage_id=str(s["return"]), return_kind="client", return_reason="не подошли", return_amount=SALE)
    c = orders.get_card(conn, cid)
    assert c["stage"]["code"] == "return" and c["return_amount"] == float(SALE)


def test_the_window_of_a_return_fills_its_empty_field_after_an_earlier_return_too(tmp_path):
    """The order came back from «Возврат» with the sum stored (review 1): the field of the window is empty on the screen, so it is filled."""
    app, client, tok, conn, pid, cid = setup(tmp_path)
    stage(conn, cid, "talk", return_kind="client", return_amount=9600)
    tag = amount_input(finish(client.get("/orders/%d" % cid).get_data(as_text=True)))
    assert "data-auto" in tag and 'data-suggest-client="%s"' % SALE in tag


def test_a_purchase_not_typed_for_every_position_gives_no_supplier_suggestion(tmp_path):
    app, client, tok, conn, pid, cid = setup(tmp_path, deal_kind="stock")
    orders.add_item(conn, cid, goods_id=orders.POINT_GOODS, name="Шина точки 205/55 R16", brand_key="", season="winter", diameter=16, territory="",
                    qty=1, sale_unit=1000, supplier_price=0, terms=None, fee_pct=0, promo_pct=0, now=NOW, author="o")
    stage(conn, cid, "return", return_kind="supplier")
    tag = amount_input(client.get("/orders/%d" % cid).get_data(as_text=True))
    assert value(tag) == "" and attr(tag, "placeholder") == "" and "data-suggest-supplier" not in tag and 'data-suggest-client="21492"' in tag


@pytest.mark.parametrize("role", ["point", "chats", "viewer"])
def test_without_the_right_money_the_page_has_no_purchase_at_all(tmp_path, role):
    app, client, tok, conn, pid, cid = setup(tmp_path)
    users.add_user(conn, "who", "пароль-для-проверки-1", "2026-10-15T12:00:00", role=role, point_id=pid if role == "point" else None)
    sign_in(client, conn, "who")
    for kind in ("supplier", "client", ""):
        stage(conn, cid, "return", return_kind=kind)
        page = client.get("/orders/%d" % cid).get_data(as_text=True)
        assert "data-suggest-supplier" not in page and not any(s in page for s in COST_SPELLINGS), (role, kind)
        tag = amount_input(page)
        assert value(tag) == "" and attr(tag, "placeholder") == (SALE if kind == "client" else "")
    stage(conn, cid, "new")
    page = client.get("/orders/%d" % cid).get_data(as_text=True)
    assert "data-suggest-supplier" not in page and not any(s in page for s in COST_SPELLINGS), role


JS_HARNESS = r"""
const fs = require("fs");
const listeners = {};
const kind = {value: "", addEventListener(t, f) { (listeners["kind_" + t] = listeners["kind_" + t] || []).push(f); }};
const attrs = {"data-auto": "", "data-suggest-client": "20492", "data-suggest-supplier": "15670", "data-return-amount": process.argv[3]};
const amount = {value: "", placeholder: "", form: {elements: {namedItem: (n) => n === "return_kind" ? kind : null}},
  hasAttribute: (k) => k in attrs, getAttribute: (k) => (k in attrs ? attrs[k] : null), removeAttribute: (k) => { delete attrs[k]; },
  setAttribute: (k, v) => { attrs[k] = String(v); },
  addEventListener(t, f) { (listeners["amount_" + t] = listeners["amount_" + t] || []).push(f); }};
const node = () => ({classList: {add() {}, remove() {}, toggle() {}, contains: () => false}, setAttribute() {}, getAttribute: () => null, appendChild() {},
  querySelector: () => null, querySelectorAll: () => [], addEventListener() {}, style: {}});
global.document = {addEventListener() {}, querySelectorAll: (s) => (s === "[data-return-amount]" ? [amount] : []), querySelector: () => null,
  getElementById: () => null, body: node(), documentElement: node(), createElement: node, hidden: false, cookie: ""};
global.window = {addEventListener() {}, location: {}, matchMedia: () => ({matches: false, addEventListener() {}}), fetch: () => new Promise(() => {}), FormData: function () {}};
global.location = {}; global.localStorage = {getItem: () => null, setItem() {}}; global.sessionStorage = global.localStorage; global.navigator = {};
global.MutationObserver = function () { return {observe() {}}; }; global.Event = function (t) { this.type = t; };
global.setTimeout = () => 0; global.clearTimeout = () => {}; global.setInterval = () => 0;
let failed = null;
try { eval(fs.readFileSync(process.argv[2], "utf8")); } catch (e) { failed = e.message; }
const fire = (who, t) => (listeners[who + "_" + t] || []).forEach((f) => f({target: who === "kind" ? kind : amount}));
const out = {failed};
kind.value = "supplier"; fire("kind", "change"); out.supplier = [amount.value, amount.placeholder];
kind.value = "client"; fire("kind", "change"); out.client = [amount.value, amount.placeholder];
kind.value = ""; fire("kind", "change"); out.none = [amount.value, amount.placeholder];
kind.value = "supplier"; fire("kind", "change");
amount.value = "9000"; fire("amount", "input");
kind.value = "client"; fire("kind", "change"); out.typed_kept = amount.value;
amount.value = ""; fire("amount", "input");
kind.value = "client"; fire("kind", "change"); out.emptied_follows = amount.value;
console.log(JSON.stringify(out));
"""


def test_the_switch_of_the_kind_switches_the_suggestion_and_never_a_typed_sum(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("no node")
    harness = tmp_path / "return_prefill.js"
    harness.write_text(JS_HARNESS, encoding="utf-8")
    js = Path(__file__).resolve().parents[1] / "src" / "tires_sync" / "panel" / "static" / "app.js"
    run = lambda mode: json.loads(subprocess.run([node, str(harness), str(js), mode], capture_output=True, text=True, timeout=30,
                                                 check=True).stdout.strip().splitlines()[-1])
    assert run("") == {"failed": None, "supplier": [COST, ""], "client": [SALE, ""], "none": ["", ""], "typed_kept": "9000", "emptied_follows": SALE}
    hint = run("hint")                                                                              # the order window: greyed only, never a value
    assert hint == {"failed": None, "supplier": ["", COST], "client": ["", SALE], "none": ["", ""], "typed_kept": "9000", "emptied_follows": ""}
