from __future__ import annotations

import time
from datetime import datetime

import pytest

from helpers import catalog_xml, commodity_xml
from panel_helpers import login, make_app, post
from test_economics_pages import NOW, ROWS, setup
from tires_sync import sales, store, terms

OK_FORM = dict(goods_id="1", ad_kind="single", pieces="4", sale_total="19240", day="2026-10-05", account_id="", point="", note="")


# --- sales: the supplier price of the day, Moscow's today, forgiving numbers -------------------------------------------------

def test_a_back_dated_sale_takes_the_price_the_owner_types_not_todays(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    post(client, "/economics/sales/add", tok, **dict(OK_FORM, supplier_price="4800"))
    row = sales.list_sales(conn)[0]
    assert row["price_total"] == 4800.0 * 4 and row["margin_total"] == pytest.approx(19240.0 - 4800 * 4 + 4800 * 4 * 0.17, abs=0.01)   # bonus 7 % + monthly 10 % of that price
    page = client.get("/economics/sales/new?goods_id=1").get_data(as_text=True)
    assert 'name="supplier_price"' in page and 'value="5000"' in page


def test_today_is_moscows_today_not_the_utc_day(tmp_path):
    app = make_app(tmp_path)
    app.config["NOW"] = lambda: datetime(2026, 10, 7, 22, 30, 0)             # 01:30 on the 8th in Moscow
    client = app.test_client()
    tok = login(client)
    open(app.config["UNLOAD_PATH"], "wb").write(catalog_xml(*ROWS))
    conn = store.connect(app.config["DB_PATH"])
    assert 'value="2026-10-08"' in client.get("/economics/sales/new?goods_id=1").get_data(as_text=True)
    r = post(client, "/economics/sales/add", tok, **dict(OK_FORM, day="2026-10-08"))
    assert r.headers["Location"].endswith("/economics/sales") and len(sales.list_sales(conn)) == 1


@pytest.mark.parametrize("text,value", [("19240", 19240.0), ("19 240", 19240.0), ("19 240", 19240.0), ("19 240,50", 19240.5),
                                        ("19.240,50", 19240.5), ("19240.5 ₽", 19240.5)])
def test_revenue_is_read_in_every_russian_way(text, value):
    from tires_sync.panel.routes_economics import _amount
    assert _amount(text) == value


def test_bad_revenue_and_dates_and_accounts_are_explained_in_russian(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    for bad, word in ((dict(sale_total="abc"), "нужно число"), (dict(sale_total="₽"), "нужно число"), (dict(day="0001-01-01"), "слишком давняя"),
                      (dict(account_id="999"), "Такого аккаунта")):
        post(client, "/economics/sales/add", tok, **dict(OK_FORM, **bad))
        assert word in client.get("/economics/sales/new?goods_id=1").get_data(as_text=True)
    assert sales.list_sales(conn) == []


@pytest.mark.parametrize("url", ["/economics/sales?a=%C2%B2", "/economics/sales?p=%C2%B2", "/economics/sales/export.csv?a=%C2%B2",
                                 "/economics?d=%C2%B2&sup=%C2%B2", "/economics/brand?brand=IKON%20TYRES&p=%C2%B2"])
def test_look_alike_digits_never_crash_a_page(tmp_path, url):
    app, client, tok, conn = setup(tmp_path)
    assert client.get(url).status_code == 200


def test_odd_ids_in_posts_are_explained_not_crashed(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    assert post(client, "/economics/sales/reverse", tok, id="²").status_code == 302
    assert "Такой продажи нет" in client.get("/economics/sales").get_data(as_text=True)
    post(client, "/economics/terms/delete", tok, id="²")
    assert "Таких условий уже нет" in client.get("/economics/terms").get_data(as_text=True)


def test_reversed_sales_are_shown_on_request_and_marked(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    post(client, "/economics/sales/add", tok, **OK_FORM)
    sid = sales.list_sales(conn)[0]["id"]
    post(client, "/economics/sales/reverse", tok, id=str(sid))
    assert "сторнирована</span>" not in client.get("/economics/sales?from=2026-01-01&to=2026-12-31").get_data(as_text=True)
    shown = client.get("/economics/sales?from=2026-01-01&to=2026-12-31&rev=1").get_data(as_text=True)
    assert "сторнирована</span>" in shown and "Продано</span><span class=\"tile-value\">0 шт." in shown          # still not in the totals


def test_the_csv_is_made_for_russian_excel(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    post(client, "/economics/sales/add", tok, **dict(OK_FORM, sale_total="19240,5"))
    body = client.get("/economics/sales/export.csv?from=2026-01-01&to=2026-12-31").get_data().decode("utf-8-sig")
    line = body.split("\n")[1]
    assert "19240,50" in line and "зима" in line and "одиночное" in line and "вручную" in line and "19240.5" not in line


def test_period_links_keep_the_other_filters_and_the_window_stays_sliding(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    html = client.get("/economics/sales?p=7&b=IKON%20TYRES&g=week").get_data(as_text=True)
    assert 'href="/economics/sales?p=30&amp;b=IKON+TYRES&amp;g=week"' in html
    assert 'name="p" value="7"' in html and 'name="from"' not in html           # a preset period sends no fixed dates
    custom = client.get("/economics/sales?from=2026-09-01&to=2026-09-30").get_data(as_text=True)
    assert 'name="from"' in custom and 'name="p"' not in custom


# --- terms: tolerant numbers, sizes, brands, replacement ----------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("до R17 вкл.", "small"), ("от R15 до R17 вкл.", "small"), ("выше R17", "large"), ("R18+", "large"), ("R18-R22", "large"), ("≥R18", "large"),
    ("R18 и более", "large"), ("R16", "small"), ("R20", "large"), ("все радиусы", "all"), ("small", "small"), ("large", "large"),
    ("от R15 до R22", "all"), ("", None), ("что-то", None)])
def test_sizes_of_the_table_are_understood(text, expected):
    assert terms._size(text) == expected


def test_percent_signs_minus_signs_and_blank_markdown(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    csv_text = "Бренд;Сезон;Диаметр;Уценка;Скидка;Бонус\nIKON;зима;все радиусы;-8,50%;;13 %\nIKON;лето;все радиусы;−5;;7\nIKON;зима;до R17;;;7\n"
    done, errors = terms.import_csv(conn, csv_text, "2026-09-01", ["IKON TYRES"], "2026-10-09T10:00:00", "owner")
    assert done == 2 and len(errors) == 1 and "не заполнена уценка" in errors[0]
    got = {(t["season"]): t for t in terms.list_terms(conn)}
    assert got["winter"]["markdown_pct"] == -8.5 and got["winter"]["bonus_pct"] == 13.0 and got["summer"]["markdown_pct"] == -5.0


def test_a_new_import_replaces_the_whole_date_in_one_step_and_a_failed_one_changes_nothing(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    first = "Бренд;Сезон;Диаметр;Уценка\nIKON;зима;все радиусы;-5\nIKON;лето;все радиусы;-4\n"
    terms.import_csv(conn, first, "2026-09-01", ["IKON TYRES"], "2026-10-09T10:00:00", "owner")
    terms.import_csv(conn, "Бренд;Сезон;Диаметр;Уценка\nIKON;зима;все радиусы;-6\n", "2026-09-01", ["IKON TYRES"], "2026-10-09T11:00:00", "owner")
    assert [(t["season"], t["markdown_pct"]) for t in terms.list_terms(conn)] == [("winter", -6.0)]      # summer of the old file is gone
    done, errors = terms.import_csv(conn, "Бренд;Сезон;Диаметр;Уценка\nNobody;зима;все радиусы;-6\n", "2026-09-01", ["IKON TYRES"], "2026-10-09T12:00:00", "owner")
    assert done == 0 and errors and len(terms.list_terms(conn)) == 1                                       # nothing valid: nothing replaced
    assert [e["action"] for e in store.list_audit(conn, 10)] == ["terms_imported", "terms_imported"]


def test_brand_names_are_matched_without_the_noise_words_and_across_suppliers():
    keys = ["IKON TYRES", "S2:IKON TYRES", "PIRELLI", "PIRELLI FORMULA", "S2:PIRELLI FORMULA"]
    assert terms.resolve_brand("Tyres", keys) is None
    assert terms.resolve_brand("Ikon Tyres", keys) == "IKON TYRES"
    assert terms.resolve_brand("Ikon", keys) == "IKON TYRES"                       # the first supplier's key wins over S2:
    assert terms.resolve_brand("Formula", keys) == "PIRELLI FORMULA"
    assert terms.resolve_brand("Pirelli", keys) == "PIRELLI"
    assert terms.resolve_brand("", keys) is None and terms.resolve_brand("Nope", keys) is None


def test_brand_keys_are_stored_in_capitals_and_a_missing_term_cannot_be_deleted(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    terms.save_term(conn, {"brand_key": " ikon   tyres ", "season": "winter", "size_class": "all", "markdown_pct": "-5",
                           "valid_from": "2026-09-01"}, "2026-10-09T10:00:00", "owner")
    assert terms.list_terms(conn)[0]["brand_key"] == "IKON TYRES"
    with pytest.raises(ValueError):
        terms.delete_term(conn, 999, "2026-10-09T10:00:00", "owner")
    terms.delete_term(conn, terms.list_terms(conn)[0]["id"], "2026-10-09T10:00:00", "owner")
    assert store.list_audit(conn, 1)[0]["detail"]["brand_key"] == "IKON TYRES"


def test_apply_pricing_refuses_a_bad_day_and_the_journal_speaks_russian(tmp_path):
    app, client, tok, conn = setup(tmp_path)
    post(client, "/economics/terms/apply-pricing", tok, day="yesterday")
    assert "ГГГГ-ММ-ДД" in client.get("/economics/terms").get_data(as_text=True)
    post(client, "/economics/sales/add", tok, **OK_FORM)
    post(client, "/economics/params", tok, fee_pct="5", promo_pct="1")
    history = client.get("/history").get_data(as_text=True)
    assert "Записана продажа: 4 шт." in history and "Расходы Авито: комиссия 5.0 %" in history and "Условия поставщика: IKON TYRES" in history
    assert "sale_added" not in history and "term_saved" not in history


# --- dynamics and views --------------------------------------------------------------------------------------------------------

def test_a_flat_series_is_drawn_through_the_middle():
    from tires_sync.panel import economics_view
    chart = economics_view.chart([{"day": "d1", "v": 5}, {"day": "d2", "v": 5}], "v")
    assert chart["points"] == "8.0,70.0 632.0,70.0"


def test_a_manually_added_position_counts_in_what_the_brand_would_bring():
    from tires_sync.panel import economics_view
    row = {"unit": {"margin": 100.0}, "scode": "manual_on", "stock": 5, "supplier_price": 1000.0}
    assert economics_view.if_enabled([row])["profit"] == 500.0


def test_the_brand_page_is_paged_and_light_even_for_a_huge_brand(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    login(client)
    rows = [commodity_xml(NNOMMODIF=str(i), SMODEL="Weird %d model" % (i % 300), SMNFCODE="C%d" % i, SWIDTH=str(175 + (i % 8) * 10),
                          SDIAMETR=str(15 + i % 5)) for i in range(1850)]
    open(app.config["UNLOAD_PATH"], "wb").write(catalog_xml(*rows))
    started = time.time()
    html = client.get("/economics/brand?brand=IKON%20TYRES").get_data(as_text=True)
    assert time.time() - started < 4.0
    assert html.count('class="gname"') == 100 and 'class="pager"' in html and len(html) < 400000
    assert client.get("/economics/brand?brand=IKON%20TYRES&p=19").get_data(as_text=True).count('class="gname"') == 50


def test_the_overview_says_which_account_the_prices_are_for(tmp_path):
    from tires_sync import connections
    app, client, tok, conn = setup(tmp_path)
    connections.create_account(conn, "Первый", NOW, "owner")
    connections.create_account(conn, "Второй", NOW, "owner")
    html = client.get("/economics").get_data(as_text=True)
    assert "Цены и объявления посчитаны для аккаунта" in html and "Первый" in html
    assert "график по всему каталогу" in html


def test_a_manual_sale_is_written_once_however_often_the_form_is_sent(tmp_path):
    import re
    app, client, tok, conn = setup(tmp_path)
    page = client.get("/economics/sales/new?goods_id=%s" % OK_FORM["goods_id"]).get_data(as_text=True)
    token = re.search(r'name="sale_token" value="([0-9a-f]+)"', page).group(1)
    for _ in range(3):
        client.post("/economics/sales/add", data=dict(OK_FORM, csrf=tok, sale_token=token))
    assert len(sales.list_sales(conn)) == 1
    client.post("/economics/sales/add", data=dict(OK_FORM, csrf=tok, sale_token="made-up"))              # a token nobody was given
    assert len(sales.list_sales(conn)) == 1
