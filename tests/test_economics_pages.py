from __future__ import annotations

import re

from helpers import catalog_xml, commodity_xml
from panel_helpers import login, make_app, post
from tires_sync import store, terms

NOW = "2026-10-09T10:00:00"
ROWS = (
    commodity_xml(NNOMMODIF="1", SMNFCODE="C1", NREST="10"),                                                             # Ikon winter 16, price 5000
    commodity_xml(NNOMMODIF="2", SMNFCODE="C2", NREST="3", SDIAMETR="19", SWIDTH="235", SHEIGHT="45"),                  # Ikon winter 19
    commodity_xml(NNOMMODIF="3", SSEASON="Летняя", SMARKA="Лауфенн", SMODIFNAME="LAUFENN 195/65/15 G Fit", SMODEL="G Fit",
                  SDIAMETR="15", SWIDTH="195", SHEIGHT="65", SMNFCODE="C3", NREST="8"),                                  # Laufenn summer: not in feed
)


def setup(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    tok = login(client)
    open(app.config["UNLOAD_PATH"], "wb").write(catalog_xml(*ROWS))
    conn = store.connect(app.config["DB_PATH"])
    terms.save_term(conn, {"brand_key": "IKON TYRES", "season": "winter", "size_class": "all", "markdown_pct": -3.82, "discount_pct": 0,
                           "bonus_pct": 7, "monthly_pct": 10, "valid_from": "2026-09-01"}, NOW, "owner")
    return app, client, tok, conn


def test_overview_shows_totals_and_a_row_per_brand(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    html = client.get("/economics").get_data(as_text=True)
    assert "Юнит-экономика" in html and 'href="/economics/brand?brand=IKON%20TYRES"' in html
    laufenn = html.split("По брендам")[1].split("/economics/brand?brand=LAUFENN")[1].split("</tr>")[0]
    assert "не публикуется" in laufenn and "≈" not in laufenn                          # not in the feed and no terms or rule: no promise either
    assert re.search(r"Прибыль на остатках", html) and "Условия" in html


def test_the_position_money_follows_the_owners_formula(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    html = client.get("/economics/brand?brand=IKON%20TYRES").get_data(as_text=True)
    # supplier price 5000, sale 4810 (-3.82 %), bonus 7 % + monthly 10 % of the price = 850 back: margin 660
    assert "660" in html and "13,2" in html and "4 810" in html and "5 000" in html
    assert "×4" in html or "Комплект" in html
    assert client.get("/economics/brand?brand=NOBODY").status_code == 404
    assert app.test_client().get("/economics").status_code == 302


def test_totals_count_only_what_is_in_the_feed_with_stock(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    html = client.get("/economics").get_data(as_text=True)
    # Ikon: 10 pcs at 660 and 3 pcs of R19: 5000 → 4810/… ; profit text appears as rubles with a thin space
    assert "Объявлений в фиде" in html and "остаток 10 шт." in html            # R19 has 3 pcs: below the threshold of 4, not in the feed
    from tires_sync.panel import economics_view
    from tires_sync.panel.routes_main import context
    assert economics_view.summarize([])["profit"] == 0


def test_terms_page_adds_deletes_imports_and_derives_the_total(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    r = post(client, "/economics/terms/save", tok, brand_key="LAUFENN", season="summer", size_class="all", markdown_pct="-8,5",
             discount_pct="0", bonus_pct="13", monthly_pct="10", valid_from="2026-09-01")
    assert r.status_code == 302
    html = client.get("/economics/terms").get_data(as_text=True)
    assert "Условия сохранены" in html and "14,50 %" in html                       # -8.5 + 13 + 10, the owner's own example
    post(client, "/economics/terms/save", tok, brand_key="LAUFENN", season="spring", size_class="all", valid_from="2026-09-01")
    assert "Сезон" in client.get("/economics/terms").get_data(as_text=True)
    tid = [t for t in terms.list_terms(conn) if t["brand_key"] == "LAUFENN"][0]["id"]
    post(client, "/economics/terms/delete", tok, id=str(tid))
    assert all(t["brand_key"] != "LAUFENN" for t in terms.list_terms(conn))
    from io import BytesIO
    csv_text = "Бренд;Сезон;Диаметр;Уценка;Скидка;Бонус\nIkon;лето;все радиусы;-2;;5\n"
    r = client.post("/economics/terms/import", data={"csrf": tok, "valid_from": "2026-09-01",
                                                    "file": (BytesIO(csv_text.encode("utf-8")), "t.csv")}, content_type="multipart/form-data")
    assert r.status_code == 302 and any(t["brand_key"] == "IKON TYRES" and t["season"] == "summer" for t in terms.list_terms(conn))
    assert "Загружено строк: 1" in client.get("/economics/terms").get_data(as_text=True)


def test_costs_of_avito_change_the_margin(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    post(client, "/economics/params", tok, fee_pct="5", promo_pct="2")
    assert terms.get_params(conn) == {"fee_pct": 5.0, "promo_pct": 2.0}
    html = client.get("/economics/brand?brand=IKON%20TYRES").get_data(as_text=True)
    assert "336" in html or "337" in html                                       # 660 - 7 % of 4810 (= 336.7)
    post(client, "/economics/params", tok, fee_pct="90", promo_pct="0")
    assert "допустимо от 0 до 50" in client.get("/economics/terms").get_data(as_text=True)


def test_apply_pricing_sends_the_markdown_column_through_the_confirmation(tmp_path):
    from tires_sync import staging
    app, client, tok, conn = setup(tmp_path)
    terms.save_term(conn, {"brand_key": "LAUFENN", "season": "summer", "size_class": "small", "markdown_pct": -8.5, "bonus_pct": 13,
                           "valid_from": "2026-09-01"}, NOW, "owner")
    terms.save_term(conn, {"brand_key": "LAUFENN", "season": "summer", "size_class": "large", "markdown_pct": -9.2, "bonus_pct": 13,
                           "valid_from": "2026-09-01"}, NOW, "owner")
    r = post(client, "/economics/terms/apply-pricing", tok, day="2026-10-09")
    assert r.headers["Location"].endswith("/draft/1")
    pricing = staging.summary(conn)["changes"]["pricing"]
    assert pricing["LAUFENN|summer"] == {"pct_small": -8.5, "pct_large": -9.2}
    assert pricing["IKON TYRES|winter"] == {"pct_small": -3.82, "pct_large": -3.82} or "IKON TYRES|winter" in pricing


def test_snapshots_record_one_row_per_brand_per_day_and_draw_a_series(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    from tires_sync.panel import economics_view
    brand = lambda profit, ads: {"brand": "IKON TYRES", "ads": ads, "stock": 13, "profit": profit, "sale_total": 1000.0, "price_total": 5000.0, "no_terms": 0}
    terms.record_snapshot(conn, "2026-10-05", [brand(100.0, 2)])
    terms.record_snapshot(conn, "2026-10-06", [brand(150.0, 3)])
    terms.record_snapshot(conn, "2026-10-06", [brand(200.0, 3)])                  # a second run on the same day replaces the first
    series = terms.snapshot_series(conn, "", 30, "2026-10-09")
    assert [(p["day"], p["profit"], p["ads"]) for p in series] == [("2026-10-05", 100.0, 2), ("2026-10-06", 200.0, 3)]
    assert series[-1]["margin_pct"] == 4.0
    chart = economics_view.chart(series, "profit")
    assert chart["low"] == 100.0 and chart["high"] == 200.0 and chart["points"].count(",") == 2
    assert economics_view.chart(series[:1], "profit") is None
    assert "polyline" in client.get("/economics").get_data(as_text=True)


def test_a_brand_that_is_off_shows_what_it_would_bring_if_it_were_on(tmp_path):
    from tires_sync import staging
    app, client, tok, conn = setup(tmp_path)
    terms.save_term(conn, {"brand_key": "LAUFENN", "season": "summer", "size_class": "all", "markdown_pct": -3, "bonus_pct": 6,
                           "valid_from": "2026-09-01"}, NOW, "owner")
    staging.set_brand_state(conn, [("LAUFENN", "summer", -3.0, -3.0, False)], NOW)       # a price rule exists, the season is off
    from tires_sync.settings_store import confirm_draft
    confirm_draft(conn, staging.summary(conn)["draft_id"], NOW, "owner")
    html = client.get("/economics").get_data(as_text=True)
    row = html.split("/economics/brand?brand=LAUFENN")[1].split("</tr>")[0]
    assert "не публикуется" in row and "≈" in row
    page = client.get("/economics/brand?brand=LAUFENN").get_data(as_text=True)
    assert "Если опубликовать весь бренд" in page


# --- sales: the fact ------------------------------------------------------------------------------------------------------

def test_a_sale_is_recorded_from_the_form_and_shows_up_in_the_facts(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    page = client.get("/economics/sales/new?goods_id=1").get_data(as_text=True)
    assert "Продано:" in page and 'name="sale_total"' in page and 'value="4810"' in page.replace(" ", "") or "4810" in page
    r = post(client, "/economics/sales/add", tok, goods_id="1", ad_kind="single", pieces="4", sale_total="19 240", day="2026-10-07",
             account_id="", point="Бета", note="тест")
    assert r.status_code == 302 and r.headers["Location"].endswith("/economics/sales")
    sale = __import__("tires_sync.sales", fromlist=["sales"]).list_sales(conn)[0]
    # 4 tires, supplier price 5000, bonus 7 % + monthly 10 % of the price come back: 19240 - 20000 + 3400 = 2640
    assert (sale["pieces"], sale["sale_total"], sale["margin_total"], sale["point"]) == (4, 19240.0, 2640.0, "Бета")
    html = client.get("/economics/sales?from=2026-10-01&to=2026-10-31").get_data(as_text=True)
    assert "Продажа записана" in html and "2 640" in html and "Бета" in html and "4 шт." in html


def test_wrong_sales_are_explained_and_nothing_is_saved(tmp_path):
    from tires_sync import sales
    app, client, tok, conn = setup(tmp_path)
    for bad in ({"pieces": "0"}, {"sale_total": "abc"}, {"day": "2030-01-01"}, {"ad_kind": "set4", "pieces": "3"}):
        data = dict(goods_id="1", ad_kind="single", pieces="2", sale_total="9000", day="2026-10-07", account_id="", point="", note="")
        data.update(bad)
        r = post(client, "/economics/sales/add", tok, **data)
        assert r.headers["Location"].endswith("/economics/sales/new?goods_id=1")
    assert sales.list_sales(conn) == []
    r = post(client, "/economics/sales/add", tok, goods_id="nope", ad_kind="single", pieces="1", sale_total="1")
    assert r.status_code == 302 and r.headers["Location"].endswith("/economics/sales")
    assert "уже нет в выгрузке" in client.get("/economics/sales").get_data(as_text=True)
    assert client.get("/economics/sales/new?goods_id=nope").status_code == 302


def test_reversal_removes_a_sale_from_the_totals_but_not_from_the_journal(tmp_path):
    from tires_sync import sales
    app, client, tok, conn = setup(tmp_path)
    post(client, "/economics/sales/add", tok, goods_id="1", ad_kind="single", pieces="4", sale_total="19240", day="2026-10-07")
    sid = sales.list_sales(conn)[0]["id"]
    r = post(client, "/economics/sales/reverse", tok, id=str(sid), next="/economics/sales")
    assert r.headers["Location"].endswith("/economics/sales") and sales.list_sales(conn) == []
    assert "сторнирована" in client.get("/economics/sales").get_data(as_text=True)
    post(client, "/economics/sales/reverse", tok, id=str(sid), next="//evil.example")
    assert "уже сторнирована" in client.get("/economics/sales").get_data(as_text=True)
    assert [e["action"] for e in store.list_audit(conn, 5)][:2] == ["sale_reversed", "sale_added"]


def test_the_sales_page_filters_by_period_brand_and_exports_csv(tmp_path):
    from tires_sync import sales
    app, client, tok, conn = setup(tmp_path)
    post(client, "/economics/sales/add", tok, goods_id="1", ad_kind="single", pieces="4", sale_total="19240", day="2026-10-07", point="Бета")
    post(client, "/economics/sales/add", tok, goods_id="3", ad_kind="single", pieces="1", sale_total="3000", day="2026-09-01")
    html = client.get("/economics/sales?from=2026-10-01&to=2026-10-31").get_data(as_text=True)
    assert "Gislaved" not in html and "IKON" in html.upper()
    only = client.get("/economics/sales?from=2026-01-01&to=2026-12-31&b=LAUFENN").get_data(as_text=True)
    assert "3\u00a0000" in only and "Бета" not in only
    body = client.get("/economics/sales/export.csv?from=2026-01-01&to=2026-12-31").get_data().decode("utf-8-sig")
    assert body.count("\n") == 3 and body.startswith("Дата;")
    for gran in ("day", "week", "month"):
        assert client.get("/economics/sales?g=%s&from=2026-01-01&to=2026-12-31" % gran).status_code == 200
    assert client.get("/economics/sales?from=bad&to=%3Cscript%3E").status_code == 200


def test_sold_buttons_and_facts_are_wired_into_the_catalog_the_overview_and_the_brand_page(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    assert 'href="/economics/sales/new?goods_id=1"' in client.get("/catalog").get_data(as_text=True)
    post(client, "/economics/sales/add", tok, goods_id="1", ad_kind="single", pieces="4", sale_total="19240", day="2026-10-05")
    overview = client.get("/economics").get_data(as_text=True)
    assert "Факт за 30 дней" in overview and "2 640" in overview
    brand = client.get("/economics/brand?brand=IKON%20TYRES").get_data(as_text=True)
    assert "4 шт. ·" in brand and 'href="/economics/sales/new?goods_id=1"' in brand


def test_a_saved_term_is_edited_from_the_terms_page(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    tid = terms.list_terms(conn)[0]["id"]
    html = client.get("/economics/terms").get_data(as_text=True)
    assert "/economics/terms?edit=%d" % tid in html                                  # the row has an «Изменить» link
    html = client.get("/economics/terms?edit=%d" % tid).get_data(as_text=True)
    assert "Изменение условий" in html and 'value="-3.82"' in html and 'name="id" value="%d"' % tid in html
    r = post(client, "/economics/terms/update", tok, id=str(tid), brand_key="IKON TYRES", season="winter", size_class="all",
             markdown_pct="-4,5", discount_pct="0", bonus_pct="8", monthly_pct="10", valid_from="2026-09-01")
    assert r.status_code == 302
    changed = terms.list_terms(conn)[0]
    assert (changed["id"], changed["markdown_pct"], changed["bonus_pct"]) == (tid, -4.5, 8.0)
    assert "Условия изменены" in client.get("/economics/terms").get_data(as_text=True)


def test_a_wrong_edit_is_explained_and_an_unknown_edit_link_is_just_the_plain_page(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    tid = terms.list_terms(conn)[0]["id"]
    post(client, "/economics/terms/update", tok, id=str(tid), brand_key="IKON TYRES", season="winter", size_class="all", bonus_pct="80",
         valid_from="2026-09-01")
    assert terms.list_terms(conn)[0]["bonus_pct"] == 7.0
    assert "Бонус" in client.get("/economics/terms").get_data(as_text=True)
    assert "Изменение условий" not in client.get("/economics/terms?edit=99999").get_data(as_text=True)
    assert "Изменение условий" not in client.get("/economics/terms?edit=abc").get_data(as_text=True)
