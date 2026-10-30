"""Excluded brands: a supplier's brand list that must never reach the feed."""
from __future__ import annotations

import re
from datetime import date, timedelta

import pytest

from helpers import catalog_xml, commodity_xml, make_settings, make_offer, settings_dict
from panel_helpers import login, make_app, post
from tires_sync import connections, cycle, publishing, staging, store
from tires_sync.candidates import evaluate
from tires_sync.catalog import parse_reference
from tires_sync.filters import exclusion_reason
from tires_sync.settings_store import confirm_draft, create_draft, get_active, init_settings

NOW = "2026-10-14T10:00:00"
TODAY = date(2026, 10, 14)
REF = parse_reference('<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n'
                      '<make name="Pirelli">\n<model name="Test Model"/>\n</make>\n</Shiny>')


def two_brands(n=3):
    rows = [commodity_xml(NNOMMODIF=str(i), SMNFCODE="I%d" % i) for i in range(1, n + 1)]
    rows += [commodity_xml(NNOMMODIF=str(100 + i), SMARKA="Pirelli", SMODIFNAME="PIRELLI 205/55/16 T 94 Test Model",
                           SMODIFNAME_ORIG="Pirelli 205/55/16 T 94 Test Model", SMNFCODE="P%d" % i) for i in range(1, n + 1)]
    return catalog_xml(*rows)


# --- filter ----------------------------------------------------------------------------------------------------------

def test_excluded_brand_is_filtered_before_anything_else():
    s = make_settings(excluded_brands=["IKON TYRES"])
    assert exclusion_reason(make_offer(), s) == "brand_excluded"
    assert exclusion_reason(make_offer(brand_key="PIRELLI"), s) is None
    assert exclusion_reason(make_offer(), make_settings()) is None          # empty list: as before


def test_a_manual_on_does_not_bring_an_excluded_brand_back():
    s = make_settings(excluded_brands=["IKON TYRES"], overrides={"1:100": "on"})
    ev = evaluate({"100": make_offer()}, {}, s, REF, {}, None)
    assert "100" not in ev.ok and ev.excluded["100"] == "brand_excluded"


def test_settings_reject_garbage_in_excluded_brands():
    with pytest.raises(ValueError):
        make_settings(excluded_brands="IKON")
    with pytest.raises(ValueError):
        make_settings(excluded_brands=[1])


# --- feed ------------------------------------------------------------------------------------------------------------

def feed(tmp_path, conn):
    token = connections.get_account(conn, 1)["feed_token"]
    return (tmp_path / "feed" / token / "next" / "avito_feed.xml").read_text(encoding="utf-8")


def prepared(tmp_path, **settings):
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(**settings), NOW)
    connections.create_account(conn, "A", NOW, "owner")
    connections.set_live(conn, 1, True, NOW, "owner")
    return conn


def test_excluded_brand_is_absent_from_the_feed_and_empty_list_changes_nothing(tmp_path):
    conn = prepared(tmp_path, excluded_brands=["PIRELLI"])
    cycle.run_for_accounts(conn, two_brands(), REF, TODAY, tmp_path / "feed")
    text = feed(tmp_path, conn)
    assert text.count("<Id>") == 3 and "<Id>101</Id>" not in text and "<Id>1</Id>" in text
    (tmp_path / "other").mkdir()
    conn2 = prepared(tmp_path / "other")
    cycle.run_for_accounts(conn2, two_brands(), REF, TODAY, tmp_path / "other" / "feed")
    token = connections.get_account(conn2, 1)["feed_token"]
    assert (tmp_path / "other" / "feed" / token / "next" / "avito_feed.xml").read_text(encoding="utf-8").count("<Id>") == 6


def test_excluding_a_published_brand_removes_its_ads_even_when_that_is_many(tmp_path):
    conn = prepared(tmp_path, guard_min=1, guard_fraction=0.0)
    cycle.run_for_accounts(conn, two_brands(6), REF, TODAY, tmp_path / "feed")
    publishing.publish_live_accounts(conn, tmp_path / "feed")
    assert len(store.load_published(conn, 1)) == 12
    confirm_draft(conn, create_draft(conn, {"excluded_brands": ["PIRELLI"]}, NOW), NOW, "owner")
    publishing.remember_expected(conn, get_active(conn)[0], 6)
    cycle.run_for_accounts(conn, two_brands(6), REF, TODAY + timedelta(days=1), tmp_path / "feed")
    text = feed(tmp_path, conn)
    assert text.count("<Id>") == 6 and "<Id>101</Id>" not in text      # removed, not held by the mass-removal guard


# --- staging and the panel ---------------------------------------------------------------------------------------------

def test_staging_normalizes_and_deduplicates(tmp_path):
    conn = prepared(tmp_path)
    staging.set_brand_excluded(conn, "  pirelli ", True, NOW)
    staging.set_brand_excluded(conn, "PIRELLI", True, NOW)
    assert staging.summary(conn)["changes"]["excluded_brands"] == ["PIRELLI"]
    assert staging.summary(conn)["count"] == 1
    staging.set_brand_excluded(conn, "Pirelli", False, NOW)
    assert staging.summary(conn)["draft_id"] is None             # back to the active state: nothing pending


def test_a_second_suppliers_brand_is_kept_apart_from_the_first(tmp_path):
    conn = prepared(tmp_path)
    staging.set_brand_excluded(conn, "ikon tyres", True, NOW, supplier=2)
    assert staging.summary(conn)["changes"]["excluded_brands"] == ["S2:IKON TYRES"]
    staging.set_brand_excluded(conn, "ikon tyres", True, NOW, supplier=1)
    assert staging.summary(conn)["changes"]["excluded_brands"] == ["IKON TYRES", "S2:IKON TYRES"]


def panel(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    tok = login(client)
    return app, client, tok, store.connect(app.config["DB_PATH"])


def test_supplier_page_has_the_block_with_choices_from_the_suppliers_brands(tmp_path):
    app, client, tok, conn = panel(tmp_path)
    html = client.get("/settings/exclusions").get_data(as_text=True)
    assert "Исключаемые бренды" in html and "Товары этих брендов не будут передаваться в выгрузку." in html
    boxes = re.findall(r'name="excluded" value="([^"]+)"', html.split("Исключаемые бренды")[1])
    assert "IKON TYRES" in boxes and "LAUFENN" in boxes                         # every brand of the supplier is a row to tick


def test_bulk_exclusion_takes_off_many_brands_in_one_pass_and_puts_them_into_one_draft(tmp_path):
    app, client, tok, conn = panel(tmp_path)
    listed = ["IKON TYRES", "LAUFENN", "НЕИЗВЕСТНЫЙ"]
    r = post(client, "/connections/suppliers/1/excluded-bulk", tok, excluded=["IKON TYRES", "LAUFENN"], listed=listed)
    assert r.status_code == 302 and r.headers["Location"].endswith("/settings/exclusions#excluded-1")  # the owner 2026-10-29: the change stays on its page (it waits in the draft); it went straight to /draft/N
    assert staging.summary(conn)["changes"]["excluded_brands"] == ["IKON TYRES", "LAUFENN"]
    html = client.get("/settings/exclusions").get_data(as_text=True)
    assert "не подтверждено" in html.split("Исключаемые бренды")[1]
    review = client.get("/catalog/review", follow_redirects=True).get_data(as_text=True)
    assert "IKON TYRES" in review and "LAUFENN" in review


def test_bulk_exclusion_also_returns_brands_that_were_unticked_and_leaves_unlisted_ones_alone(tmp_path):
    app, client, tok, conn = panel(tmp_path)
    staging.set_brand_excluded(conn, "IKON TYRES", True, NOW)
    staging.set_brand_excluded(conn, "LAUFENN", True, NOW)
    post(client, "/connections/suppliers/1/excluded-bulk", tok, excluded=["LAUFENN", "НЕИЗВЕСТНЫЙ"], listed=["IKON TYRES", "LAUFENN", "НЕИЗВЕСТНЫЙ"])
    assert staging.summary(conn)["changes"]["excluded_brands"] == ["LAUFENN", "НЕИЗВЕСТНЫЙ"]            # IKON ticked off, the third ticked on
    post(client, "/connections/suppliers/1/excluded-bulk", tok, excluded=[], listed=["IKON TYRES"])   # LAUFENN is not listed: stays
    assert staging.summary(conn)["changes"]["excluded_brands"] == ["LAUFENN", "НЕИЗВЕСТНЫЙ"]


def test_bulk_exclusion_refuses_an_unknown_brand_and_an_unknown_supplier_and_says_so_when_nothing_changes(tmp_path):
    app, client, tok, conn = panel(tmp_path)
    post(client, "/connections/suppliers/1/excluded-bulk", tok, excluded=["IKON TYRES", "NoSuchBrand"], listed=["IKON TYRES", "NoSuchBrand"])
    assert staging.summary(conn)["draft_id"] is None
    assert post(client, "/connections/suppliers/99/excluded-bulk", tok, excluded=["IKON TYRES"], listed=["IKON TYRES"]).status_code == 404
    post(client, "/connections/suppliers/1/excluded-bulk", tok, excluded=[], listed=["IKON TYRES"])
    assert "Изменений нет" in client.get("/settings/exclusions").get_data(as_text=True)


def test_add_and_remove_through_the_panel_and_the_review_screen(tmp_path):
    app, client, tok, conn = panel(tmp_path)
    r = post(client, "/connections/suppliers/1/excluded", tok, brand="Ikon Tyres")
    assert r.status_code == 302
    assert staging.summary(conn)["changes"]["excluded_brands"] == ["IKON TYRES"]
    html = client.get("/settings/exclusions").get_data(as_text=True)
    assert "IKON TYRES" in html.split("Исключаемые бренды")[1] and "не подтверждено" in html
    again = post(client, "/connections/suppliers/1/excluded", tok, brand="IKON TYRES")           # duplicate
    assert staging.summary(conn)["changes"]["excluded_brands"] == ["IKON TYRES"]
    review = client.get("/catalog/review", follow_redirects=True).get_data(as_text=True)
    assert "Исключить бренд" in review and "IKON TYRES" in review
    post(client, "/connections/suppliers/1/excluded", tok, remove="IKON TYRES")
    assert staging.summary(conn)["draft_id"] is None


def test_unknown_brand_and_unknown_supplier_are_refused(tmp_path):
    app, client, tok, conn = panel(tmp_path)
    post(client, "/connections/suppliers/1/excluded", tok, brand="NoSuchBrand")
    assert staging.summary(conn)["draft_id"] is None
    assert post(client, "/connections/suppliers/99/excluded", tok, brand="IKON TYRES").status_code == 404


def test_confirmed_exclusion_disappears_from_the_catalog(tmp_path):
    app, client, tok, conn = panel(tmp_path)
    connections.create_account(conn, "A", NOW, "owner")
    post(client, "/connections/suppliers/1/excluded", tok, brand="IKON TYRES")
    d = staging.summary(conn)["draft_id"]
    post(client, "/draft/%d/confirm" % d, tok)
    assert get_active(conn)[1]["excluded_brands"] == ["IKON TYRES"]
    page = client.get("/catalog").get_data(as_text=True)
    assert "IKON TYRES" not in page.split('id="items"')[1]                  # its positions are gone from the list
    assert 'data-brand-on="IKON TYRES"' in page                             # «Бренды в фиде» of the owner offers it back (2026-10-30)
    assert "Test Model" not in client.get("/catalog/rows?brand=IKON+TYRES").get_data(as_text=True)


def test_confirmed_excluded_radius_hides_only_that_radius_from_the_catalog(tmp_path):
    from tires_sync.panel import service
    raw = {"excluded_brands": [], "excluded_sizes": ["IKON TYRES|16", "S2:*|17"]}
    assert service.excluded_by_owner(raw, make_offer(diameter=16))
    assert not service.excluded_by_owner(raw, make_offer(diameter=15))
    assert not service.excluded_by_owner(raw, make_offer(brand_key="PIRELLI", diameter=16))
    assert service.excluded_by_owner(raw, make_offer(brand_key="S2:PIRELLI", diameter=17))
    assert not service.excluded_by_owner(raw, make_offer(brand_key="PIRELLI", diameter=17))


# --- excluded sizes: a brand (or every brand) at a given radius -----------------------------------------------------------

def test_size_filter_by_brand_and_radius_and_for_every_brand():
    ikon16 = make_offer(diameter=16)
    s = make_settings(excluded_sizes=["IKON TYRES|16"])
    assert exclusion_reason(ikon16, s) == "size_excluded"
    assert exclusion_reason(make_offer(diameter=17), s) is None                      # another radius stays
    assert exclusion_reason(make_offer(brand_key="PIRELLI", diameter=16), s) is None   # another brand stays
    every = make_settings(excluded_sizes=["*|16"])
    assert exclusion_reason(ikon16, every) == "size_excluded"
    assert exclusion_reason(make_offer(brand_key="S2:IKON TYRES", diameter=16), every) != "size_excluded"   # the rule is the first supplier's
    second = make_settings(excluded_sizes=["S2:*|16"])
    assert exclusion_reason(make_offer(brand_key="S2:IKON TYRES", diameter=16), second) == "size_excluded"
    assert exclusion_reason(ikon16, second) != "size_excluded"
    assert exclusion_reason(ikon16, make_settings()) is None                           # empty list: as before


@pytest.mark.parametrize("bad", ["IKON|", "IKON|abc", "|16", "IKON", 16])
def test_settings_reject_garbage_in_excluded_sizes(bad):
    with pytest.raises(ValueError):
        make_settings(excluded_sizes=[bad])


def test_a_manual_on_does_not_bring_an_excluded_size_back():
    s = make_settings(excluded_sizes=["*|16"], overrides={"1:100": "on"})
    ev = evaluate({"100": make_offer(diameter=16)}, {}, s, REF, {}, None)
    assert "100" not in ev.ok and ev.excluded["100"] == "size_excluded"


def test_excluded_size_is_absent_from_the_feed(tmp_path):
    conn = prepared(tmp_path, excluded_sizes=["PIRELLI|16"])
    cycle.run_for_accounts(conn, two_brands(), REF, TODAY, tmp_path / "feed")
    text = feed(tmp_path, conn)
    assert text.count("<Id>") == 3 and "<Id>101</Id>" not in text


def test_staging_of_sizes_normalizes_deduplicates_and_separates_suppliers(tmp_path):
    conn = prepared(tmp_path)
    staging.set_size_excluded(conn, " pirelli ", 16, True, NOW)
    staging.set_size_excluded(conn, "PIRELLI", 16, True, NOW)
    staging.set_size_excluded(conn, None, 15, True, NOW, supplier=2)
    assert staging.summary(conn)["changes"]["excluded_sizes"] == ["PIRELLI|16", "S2:*|15"]
    staging.set_size_excluded(conn, "pirelli", 16, False, NOW)
    staging.set_size_excluded(conn, None, 15, False, NOW, supplier=2)
    assert staging.summary(conn)["draft_id"] is None
    with pytest.raises(ValueError):
        staging.set_size_excluded(conn, "PIRELLI", 99, True, NOW)


def test_panel_adds_and_removes_a_size_exclusion_and_shows_it_in_the_review(tmp_path):
    app, client, tok, conn = panel(tmp_path)
    html = client.get("/settings/exclusions").get_data(as_text=True)
    assert "Исключаемые радиусы" in html
    assert post(client, "/connections/suppliers/1/excluded-sizes", tok, brand="Laufenn", diameter="16").status_code == 302
    post(client, "/connections/suppliers/1/excluded-sizes", tok, brand="", diameter="16")
    assert staging.summary(conn)["changes"]["excluded_sizes"] == ["*|16", "LAUFENN|16"]
    page = client.get("/settings/exclusions").get_data(as_text=True).split("Исключаемые радиусы")[1]
    assert "LAUFENN" in page and "R16" in page and "Все бренды" in page
    review = client.get("/catalog/review", follow_redirects=True).get_data(as_text=True)
    assert "Исключить размер" in review and "R16" in review
    post(client, "/connections/suppliers/1/excluded-sizes", tok, remove="LAUFENN|16")
    post(client, "/connections/suppliers/1/excluded-sizes", tok, remove="*|16")
    assert staging.summary(conn)["draft_id"] is None


def test_panel_refuses_a_radius_the_supplier_does_not_have_and_a_brand_it_does_not_have(tmp_path):
    app, client, tok, conn = panel(tmp_path)
    post(client, "/connections/suppliers/1/excluded-sizes", tok, brand="Laufenn", diameter="99")
    post(client, "/connections/suppliers/1/excluded-sizes", tok, brand="Nobody", diameter="16")
    assert staging.summary(conn)["draft_id"] is None
    assert post(client, "/connections/suppliers/99/excluded-sizes", tok, brand="", diameter="16").status_code == 404


# --- the owner stays on the page where the work was done ---------------------------------------------------------------

def test_confirming_returns_to_the_page_where_the_change_was_made(tmp_path):
    app, client, tok, conn = panel(tmp_path)
    post(client, "/connections/suppliers/1/excluded-sizes", tok, brand="Laufenn", diameter="16")
    r = client.get("/catalog/review", headers={"Referer": "http://localhost/settings"})
    draft = r.headers["Location"]
    done = post(client, draft + "/confirm", tok)
    assert done.headers["Location"].endswith("/settings")


def test_cancelling_a_draft_returns_to_the_page_where_the_change_was_made(tmp_path):
    app, client, tok, conn = panel(tmp_path)
    post(client, "/connections/suppliers/1/excluded", tok, brand="Laufenn")
    draft = client.get("/catalog/review", headers={"Referer": "http://localhost/settings"}).headers["Location"]
    r = post(client, draft + "/cancel", tok)
    assert r.headers["Location"].endswith("/settings")


def test_cancel_all_from_any_page_stays_on_that_page(tmp_path):
    app, client, tok, conn = panel(tmp_path)
    post(client, "/connections/suppliers/1/excluded", tok, brand="Laufenn")
    html = client.get("/settings/exclusions").get_data(as_text=True)
    assert 'name="next" value="/settings/exclusions"' in html
    r = post(client, "/catalog/cancel", tok, next="/settings")
    assert r.headers["Location"].endswith("/settings")


@pytest.mark.parametrize("referer", ["http://evil.example/x", "//evil.example", "http://localhost//evil.example", "http://localhost/login"])
def test_the_return_page_cannot_be_hijacked(tmp_path, referer):
    app, client, tok, conn = panel(tmp_path)
    post(client, "/connections/suppliers/1/excluded", tok, brand="Laufenn")
    draft = client.get("/catalog/review", headers={"Referer": referer}).headers["Location"]
    location = post(client, draft + "/confirm", tok).headers["Location"]
    assert "evil" not in location and "login" not in location       # the page we remembered (or home), never the Referer's
    assert location.endswith("/settings/exclusions#excluded-1") or location in ("/", "http://localhost/")


def test_settings_form_returns_to_settings_after_confirming(tmp_path):
    app, client, tok, conn = panel(tmp_path)
    r = post(client, "/settings/draft", tok, min_stock="5")
    assert r.headers["Location"].endswith("/settings")  # the owner 2026-10-29: the change stays on its page (it waits in the draft); it went straight to /draft/N
    draft = client.get("/catalog/review", headers={"Referer": "http://localhost/settings"}).headers["Location"]     # «Подробнее» of the bar
    assert "/draft/" in draft
    done = post(client, draft + "/confirm", tok)
    assert done.headers["Location"].endswith("/settings")


def test_publish_result_page_offers_the_way_back(tmp_path):
    app, client, tok, conn = panel(tmp_path)
    store.set_state(conn, "last_publish", '{"at": "x", "lines": ["Опубликовано: A"]}')
    with client.session_transaction() as s:
        s["return_to"] = "/settings"
    html = client.get("/publish/result").get_data(as_text=True)
    assert 'href="/settings"' in html


def test_exclusion_blocks_live_in_settings_not_in_the_supplier_connection(tmp_path):
    app, client, tok, conn = panel(tmp_path)
    assert "Исключаемые бренды" not in client.get("/connections/webmim/1").get_data(as_text=True)
    page = client.get("/settings/exclusions").get_data(as_text=True)
    assert "Исключаемые бренды" in page and "Исключаемые радиусы" in page
