"""«Дата доставки» saved by itself, then «Оформить в MIM» at once, without F5 (the owner 2026-11-11). After the date is stored app.js reads
the order page again (X-Requested-With: fetch) and puts its «Заказ поставщику», its window «Подтверждение» and «Оформить в WebMIM» in place
of the old ones: the new date and the new fingerprint `seen`. The check of the press stays: a `seen` of before the change is refused."""
from __future__ import annotations

import re
from pathlib import Path

from test_order_autosave import autosave
from test_order_supplier import owner_client
from test_webmim_create import card, panel  # noqa: F401 (panel: the fixture)
from panel_helpers import post
from tires_sync import order_supplier, orders

APP_JS = Path(__file__).resolve().parents[1] / "src" / "tires_sync" / "panel" / "static" / "app.js"
SEEN = re.compile(r'name="seen" value="([0-9a-f]+)"')


def again(client, cid):
    """The page as app.js reads it after the date is saved."""
    return client.get("/orders/%d" % cid, headers={"X-Requested-With": "fetch", "Sec-Fetch-Site": "same-origin"}).get_data(as_text=True)


def test_a_date_typed_into_an_empty_one_opens_the_window_with_it(panel):
    app, conn, fake = panel
    cid = card(conn, delivery_date="")
    client = owner_client(app)
    html = client.get("/orders/%d" % cid).get_data(as_text=True)
    assert "Не указана дата доставки" in html and 'id="supplier-window"' not in html     # the button shut, no window
    assert autosave(client, cid, "tok", "delivery_date", "2026-10-08").get_json() == {"ok": True, "value": "2026-10-08"}
    html = again(client, cid)
    assert "Не указана дата доставки" not in html and 'id="supplier-window"' in html and 'id="supplier"' in html
    assert "Дата доставки</dt><dd>08.10.2026</dd>" in html
    view = order_supplier.view(conn, orders.get_card(conn, cid), None, "2026-10-07T15:00:00")
    assert SEEN.search(html).group(1) == view["fingerprint"]
    assert fake.calls == []                                                              # reading the page asks WebMIM nothing


def test_a_changed_date_gives_the_window_the_new_date_and_the_new_seen(panel):
    app, conn, fake = panel
    cid = card(conn, delivery_date="2026-10-08")
    client = owner_client(app)
    old = SEEN.search(client.get("/orders/%d" % cid).get_data(as_text=True)).group(1)
    assert autosave(client, cid, "tok", "delivery_date", "2026-10-09").get_json()["ok"] is True
    html = again(client, cid)
    new = SEEN.search(html).group(1)
    assert new != old and "Дата доставки</dt><dd>09.10.2026</dd>" in html


def test_the_old_seen_is_still_refused(panel):
    # the window opened before the date changed and not read again: the press is refused, nothing goes to WebMIM
    app, conn, fake = panel
    cid = card(conn, delivery_date="2026-10-08")
    client = owner_client(app)
    old = SEEN.search(client.get("/orders/%d" % cid).get_data(as_text=True)).group(1)
    assert autosave(client, cid, "tok", "delivery_date", "2026-10-09").get_json()["ok"] is True
    post(client, "/orders/%d/webmim/create" % cid, "tok", seen=old)
    assert "изменились после открытия окна" in client.get("/orders/%d" % cid).get_data(as_text=True)
    assert not fake.posts() and orders._row(conn, cid)["webmim_id"] == ""


def test_the_parts_app_js_puts_in_place_are_on_the_page(panel):
    app, conn, fake = panel
    cid = card(conn, delivery_date="2026-10-08")
    html = again(owner_client(app), cid)
    # the window comes right after the block (app.js puts the new one there when there was none)
    assert re.search(r'id="supplier".*?</section>\s*<div class="modal-backdrop send-window" id="supplier-window"', html, re.S)
    js = APP_JS.read_text(encoding="utf-8")
    for part in ('getElementById("supplier")', 'getElementById("supplier-window")', 'querySelector("[data-owin-wm]")'):
        assert part in js, part


def test_the_date_is_saved_whole_once_after_the_typing():
    # Chrome: «25» typed over «20» is «02», then «25», each a whole date and a «change»; the date is saved 2 s after the last change or on
    # leaving the field, only whole (a half typed date is "" with badInput, a year typed by keys passes 0002, 0020, 0202)
    js = APP_JS.read_text(encoding="utf-8")
    part = js[js.index('var isDate = field.type === "date"'):js.index("// 13a)")]
    assert "field.validity.badInput) return false" in part and "+m[1] >= 2020 && +m[1] <= 2100" in part
    branch = part[part.index("} else if (isDate) {"):part.index("} else {", part.index("} else if (isDate) {"))]
    assert 'addEventListener("change", later)' in branch and 'addEventListener("input", later)' in branch
    assert 'addEventListener("change", save)' not in branch and "setTimeout(save, 2000)" in branch
    assert "save(true)" in branch                                                        # leaving the field: at once
    assert "if (isDate) supplierBusy(true)" in part and "supplierRefresh(" in part        # «Подтвердить» shut until the block is read again
