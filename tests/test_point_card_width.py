"""The card of an order on a point's phone (the owner 2026-11-06: 537 px at a width of 375, the table of the positions did not fit): the page
never scrolls sideways at 375 px, in the old view and «Новый вид» of the point, the window's positions and «Продажа точки» alike. A browser
test: skipped where Playwright or its Chromium is not installed."""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime

import pytest

from panel_helpers import make_app
from test_mypoint import manager
from tires_sync import order_points as points, orders, store, users

sync_api = pytest.importorskip("playwright.sync_api")

NOW = "2026-11-05T12:00:00"
LONG = "Nokian Hakka Hakkapeliitta 10 SUV 235/65 R17 108T XL шип"
CHROMIUM = "/opt/pw-browsers/chromium"                   # the preinstalled browser of the cloud sessions; else Playwright's own


@pytest.fixture
def served(tmp_path, monkeypatch):
    from werkzeug.serving import make_server
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 11, 5, 9, 0, 0))
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, NOW)
    pid = points.save_point(conn, {"name": "Альфа", "manager_name": "А", "channel": "panel"})
    ours = orders.create_card(conn, {"client_name": "Анна", "client_phone": "+7 900 000 00 01", "point_id": str(pid)}, NOW, "o")
    sale = orders.create_card(conn, {"client_name": "Борис", "point_id": str(pid), "deal_kind": "stock"}, NOW, "o")
    for cid, goods in ((ours, "100"), (ours, "101"), (sale, orders.POINT_GOODS)):
        orders.add_item(conn, cid, goods_id=goods, name=LONG, brand_key="nokian", season="winter", diameter=17, territory="1", qty=4,
                        sale_unit=123456, supplier_price=100000, terms=None, fee_pct=0, promo_pct=0, now=NOW, author="o")
    conn.commit()
    client = manager(app, conn, pid)
    client.get("/")
    cookie = client.get_cookie("session").value
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield dict(conn=conn, cookie=cookie, base="http://127.0.0.1:%d" % server.server_port, ours=ours, sale=sale, pid=pid)
    server.shutdown()


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch(executable_path=CHROMIUM) if os.path.exists(CHROMIUM) else p.chromium.launch()
        except Exception as err:                                            # noqa: BLE001 - no browser on this machine
            pytest.skip("no Chromium: %s" % err)
        yield b
        b.close()


@pytest.mark.parametrize("view", ["off", "all"])
def test_the_card_of_an_order_fits_a_phone_of_375(served, browser, view):
    store.set_state(served["conn"], "ui_new.point", view)
    served["conn"].commit()
    assert served["sale"] in points.own_sales(served["conn"], served["pid"])            # «Продажа точки» is on its page
    ctx = browser.new_context(viewport={"width": 375, "height": 800})
    ctx.add_cookies([{"name": "session", "value": served["cookie"], "domain": "127.0.0.1", "path": "/"}])
    page = ctx.new_page()
    try:
        for cid in (served["ours"], served["sale"]):
            assert page.goto("%s/orders/%d" % (served["base"], cid)).status == 200
            assert page.locator(".owin-items").count() >= 1
            if cid == served["sale"]:
                assert page.locator("[data-point-sale] .owin-items").count() == 1
            assert page.evaluate("document.documentElement.scrollWidth") <= 375, (view, cid)
            for box in page.locator(".owin-items").all():
                assert box.bounding_box()["x"] + box.bounding_box()["width"] <= 375, (view, cid)
            assert page.locator(".owin-items input[name=qty]").first.is_visible()
    finally:
        ctx.close()
