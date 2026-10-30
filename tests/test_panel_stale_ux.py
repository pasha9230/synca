"""Run tails5, lane A: an old second WebMIM unload (held by the cycle and the panel since run tails4) is still seen where only viewing is
asked: its supplier's settings lists come from the raw parse of the file with the mark «только просмотр: выгрузка устарела» and stay
settable; the photo of a held ad that our cache lacks is taken from that file (as before tails4), never for a code we have not published;
the catalog shows its positions for viewing only; the readiness step checks «no positions» before «old», as the cycle does."""
from __future__ import annotations

from datetime import datetime

from werkzeug.security import generate_password_hash

from helpers import catalog_xml, commodity_xml
from panel_helpers import login, post
from test_multi_supplier_cycle import ONE, files, prepared
from test_panel_tails4 import both, old, second
from tires_sync import connections, staging, store
from tires_sync.panel import create_app, data, service
from tires_sync.panel.data import load_merged
from tires_sync.settings_store import active_settings

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64
MARK = "только просмотр: выгрузка устарела (скачана"


def app_of(tmp_path, fetch=None):
    return create_app({"TESTING": True, "SECRET_KEY": "test-secret", "PASSWORD_HASH": generate_password_hash("pw", method="pbkdf2:sha256:1000"),
                       "DB_PATH": str(tmp_path / "t.db"), "UNLOAD_PATH": str(tmp_path / "unload.xml"),
                       "REFERENCE_PATH": str(tmp_path / "reference.txt"), "FEED_ROOT": str(tmp_path / "feed"),
                       "IMG_ROOT": str(tmp_path / "img"), "PHOTO_FETCH": fetch, "NOW": datetime.utcnow})


def stale_setup(tmp_path, fetched=None, **row):
    prepared(tmp_path).close()
    files(tmp_path, ONE)
    second(tmp_path, dict(dict(NNOMMODIF="5", NREST="9", SPICTURE="https://example.test/s2-5.jpg"), **row), fetched=fetched or old())


def publish(tmp_path, account_id, *codes):
    conn = store.connect(tmp_path / "t.db")
    for code in codes:
        conn.execute("INSERT INTO published_state (account_id, goods_id, price, base_price, point, snapshot) VALUES (?, ?, 5000, 5000, 'P1', '{}')",
                     (account_id, code))
    conn.commit()
    conn.close()


def token_of(tmp_path, account_id):
    conn = store.connect(tmp_path / "t.db")
    try:
        return connections.get_account(conn, account_id)["feed_token"]
    finally:
        conn.close()


# --- 1: the supplier's settings lists --------------------------------------------------------------------------------------------

def test_the_settings_lists_of_a_stale_supplier_come_from_its_file_and_are_marked(tmp_path):
    stale_setup(tmp_path)
    conn = store.connect(tmp_path / "t.db")
    base = str(tmp_path / "unload.xml")
    assert 2 in load_merged(conn, active_settings(conn), base).unavailable        # held, as the cycle: none of its positions in the set
    brands, diameters = service.supplier_catalog(conn, 2, base)
    assert brands == {"S2:IKON TYRES"} and diameters
    assert [r["brand"] for r in service.pricing_table(conn, 2, base)] == ["S2:IKON TYRES"]
    ex = service.exclusions(conn, 2, base)
    assert ex["brand_all"] == ["IKON TYRES"] and ex["stale"].startswith(MARK)
    assert service.supplier_stale_text(conn, 1, base) == "" and service.exclusions(conn, 1, base)["stale"] == ""
    conn.close()
    client = app_of(tmp_path).test_client()
    login(client)
    for url in ("/settings/brands", "/settings/exclusions", "/settings/brands/card?supplier=2&brand=S2:IKON+TYRES"):
        page = client.get(url)
        assert page.status_code == 200 and MARK in page.get_data(as_text=True), url
    assert client.get("/settings/brands/card?supplier=1&brand=IKON+TYRES").get_data(as_text=True).count(MARK) == 0


def test_the_settings_of_a_stale_supplier_are_still_saved(tmp_path):
    stale_setup(tmp_path)
    client = app_of(tmp_path).test_client()
    tok = login(client)
    r = post(client, "/settings/pricing", tok, scope="brand", supplier="2", brand="S2:IKON TYRES", winter_small="7", winter_large="8")
    assert r.status_code == 302 and r.headers["Location"].endswith("/settings/brands#pricing-2")   # the owner 2026-10-29: stays on the page
    conn = store.connect(tmp_path / "t.db")
    assert staging.summary(conn)["changes"]["pricing"]["S2:IKON TYRES|winter"] == {"pct_small": 7.0, "pct_large": 8.0}
    conn.close()
    r = post(client, "/connections/suppliers/2/excluded", tok, brand="IKON TYRES")
    assert r.status_code == 302
    conn = store.connect(tmp_path / "t.db")
    assert "S2:IKON TYRES" in staging.summary(conn)["changes"]["excluded_brands"]
    conn.close()


def test_a_fresh_second_unload_has_no_mark(tmp_path):
    stale_setup(tmp_path, fetched=datetime.utcnow().isoformat(timespec="seconds"))
    client = app_of(tmp_path).test_client()
    login(client)
    assert MARK not in client.get("/settings/brands").get_data(as_text=True)
    assert MARK not in client.get("/settings/exclusions").get_data(as_text=True)


# --- 2: the photo of a held ad ---------------------------------------------------------------------------------------------------

def test_the_photo_of_a_held_ad_comes_from_the_file_when_the_cache_lacks_it(tmp_path):
    calls = []

    def fetch(url):
        calls.append(url)
        return JPEG, "jpg"
    stale_setup(tmp_path)
    publish(tmp_path, 2, "s2-5")
    client = app_of(tmp_path, fetch).test_client()
    got = client.get("/img/%s/s2-5.jpg" % token_of(tmp_path, 2))
    assert got.status_code == 200 and got.data == JPEG and calls == ["https://example.test/s2-5.jpg"]
    assert client.get("/img/%s/s2-5.jpg" % token_of(tmp_path, 2)).data == JPEG and len(calls) == 1      # from our disk afterwards


def test_a_set_of_four_held_ad_gets_the_photo_of_its_single_code(tmp_path):
    stale_setup(tmp_path)
    publish(tmp_path, 2, "s2-5-x4")
    client = app_of(tmp_path, lambda url: (JPEG, "jpg")).test_client()
    assert client.get("/img/%s/s2-5.jpg" % token_of(tmp_path, 2)).status_code == 200


def test_no_photo_of_an_old_file_for_a_code_not_published_in_this_account(tmp_path):
    calls = []

    def fetch(url):
        calls.append(url)
        return JPEG, "jpg"
    stale_setup(tmp_path)
    publish(tmp_path, 1, "s2-5")                                                   # published in the other account only
    client = app_of(tmp_path, fetch).test_client()
    assert client.get("/img/%s/s2-5.jpg" % token_of(tmp_path, 2)).status_code == 404
    assert client.get("/img/%s/s2-6.jpg" % token_of(tmp_path, 1)).status_code == 404     # not in the file
    publish(tmp_path, 1, "s2-6")
    assert client.get("/img/%s/s2-6.jpg" % token_of(tmp_path, 1)).status_code == 404     # published, but not in the file
    assert calls == [] and not ((tmp_path / "img").exists() and list((tmp_path / "img").glob("s2-*")))


def test_no_photo_from_the_file_when_it_has_no_picture_or_the_supplier_is_not_held(tmp_path):
    stale_setup(tmp_path, SPICTURE="")
    publish(tmp_path, 2, "s2-5")
    client = app_of(tmp_path, lambda url: (JPEG, "jpg")).test_client()
    assert client.get("/img/%s/s2-5.jpg" % token_of(tmp_path, 2)).status_code == 404
    second(tmp_path, dict(NNOMMODIF="5"), fetched=datetime.utcnow().isoformat(timespec="seconds"))   # fresh: the set has it, as before
    assert client.get("/img/%s/s2-5.jpg" % token_of(tmp_path, 2)).status_code == 200


def test_the_held_photo_leaves_the_panel_set_as_the_cycle_has_it(tmp_path):
    stale_setup(tmp_path)
    publish(tmp_path, 2, "s2-5")
    app_of(tmp_path, lambda url: (JPEG, "jpg")).test_client().get("/img/%s/s2-5.jpg" % token_of(tmp_path, 2))
    in_panel, in_cycle = both(tmp_path)
    assert set(in_panel.offers) == set(in_cycle.offers) == {"5"} and in_panel.stocks == in_cycle.stocks


# --- 3: the catalog --------------------------------------------------------------------------------------------------------------

def test_the_catalog_shows_the_positions_of_a_stale_unload_for_viewing_only(tmp_path):
    stale_setup(tmp_path)
    second(tmp_path, dict(NNOMMODIF="5", NREST="9"), dict(NNOMMODIF="6", NREST="8"), fetched=old())
    publish(tmp_path, 2, "s2-5")
    client = app_of(tmp_path).test_client()
    login(client)
    html = client.get("/catalog").get_data(as_text=True)
    assert html.count("data-stale-unload>выгрузка устарела") == 2                  # both rows, marked
    assert "объявление держится как было" in html and "): не публикуется" in html
    for trace in ('value="s2-', ':s2-', "goods_id=s2-", "wait=s2-"):     # no checkbox, stage, sale/order, wait
        assert trace not in html, trace
    assert 'data-preview="s2-5"' in html and client.get("/catalog/preview/s2-5").status_code == 200   # the owner 2026-10-29: a preview for every row
    assert ':5"' in html and "goods_id=5" in html                                 # the owner's role does see the actions of own positions
    assert "нет у поставщика" not in html
    feed_only = client.get("/catalog?st=feed").get_data(as_text=True)
    assert feed_only.count("data-stale-unload>") == 1 and "объявление держится как было" in feed_only
    assert "data-stale-unload>" not in client.get("/catalog?st=gone").get_data(as_text=True)
    assert "data-stale-unload>" not in client.get("/catalog?q=nothing-like-it").get_data(as_text=True)
    assert client.get("/catalog?sup=2").get_data(as_text=True).count("data-stale-unload>выгрузка") == 2
    in_panel, in_cycle = both(tmp_path)
    assert set(in_panel.offers) == set(in_cycle.offers) == {"5"}                    # still never published from the panel


def test_a_manual_rule_on_a_held_position_is_not_a_rule_without_a_position(tmp_path):
    stale_setup(tmp_path)
    conn = store.connect(tmp_path / "t.db")
    staging.set_override(conn, "s2-5", "on", "2026-10-25T10:00:00")              # held: its file has it
    staging.set_override(conn, "s2-77", "on", "2026-10-25T10:00:00")             # in no file at all
    conn.close()
    client = app_of(tmp_path).test_client()
    login(client)
    html = client.get("/settings/exclusions").get_data(as_text=True)              # moved from the catalog (the owner 2026-10-30)
    orphans = html[html.index("Ручные правила без позиции"):]
    assert "s2-77" in orphans and "s2-5" not in orphans


# --- 4: readiness in the order of the cycle --------------------------------------------------------------------------------------

def step_of(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    settings = active_settings(conn)
    steps = service.readiness(conn, tmp_path / "unload.xml", load_merged(conn, settings, tmp_path / "unload.xml"), {}, settings)
    conn.close()
    return next(s for s in steps if s["title"] == "Поставщик «Второй»")


def test_readiness_takes_an_old_empty_or_broken_file_as_without_positions_as_the_cycle(tmp_path):
    prepared(tmp_path).close()
    files(tmp_path, ONE)
    (tmp_path / "unload-2.xml").write_bytes(catalog_xml(commodity_xml(NNOMMODIF="5", TERRITORY_RN="9999")))       # no row of its territory
    (tmp_path / "unload-2.xml.meta.json").write_text('{"fetched_at": "%s"}' % old(), encoding="utf-8")
    step = step_of(tmp_path)
    assert step["level"] == "bad" and step["detail"] == "в выгрузке нет позиций нужной территории"
    (tmp_path / "unload-2.xml").write_bytes(b"<broken")
    step = step_of(tmp_path)
    assert step["level"] == "bad" and step["detail"] == "в выгрузке нет позиций нужной территории"
    second(tmp_path, dict(NNOMMODIF="5"), fetched=old())
    step = step_of(tmp_path)
    assert step["level"] == "warn" and "выгрузка устарела (скачана" in step["detail"]


def test_stale_unload_decides_as_load_merged(tmp_path):
    stale_setup(tmp_path)
    conn = store.connect(tmp_path / "t.db")
    settings = active_settings(conn)
    held = data.stale_unload(conn, settings, tmp_path / "unload.xml", 2)
    assert held is not None and set(held[0].offers) == {"s2-5"}
    assert data.stale_unload(conn, settings, tmp_path / "unload.xml", 1) is None
    second(tmp_path, dict(NNOMMODIF="5"), fetched=datetime.utcnow().isoformat(timespec="seconds"))
    assert data.stale_unload(conn, settings, tmp_path / "unload.xml", 2) is None and "s2-5" in load_merged(conn, settings, tmp_path / "unload.xml").offers
    conn.close()


def test_stale_unload_is_none_for_the_4tochki_row_with_its_feed_on(tmp_path, monkeypatch):
    from tires_sync import tochki_feed
    stale_setup(tmp_path)
    monkeypatch.setattr(tochki_feed, "config", lambda conn: {"login": "x", "supplier_id": 2, "feed": True, "auto": False})
    conn = store.connect(tmp_path / "t.db")
    assert data.stale_unload(conn, active_settings(conn), tmp_path / "unload.xml", 2) is None     # its own load, never a WebMIM file
    conn.close()


def test_no_photo_for_a_code_of_the_first_supplier_whose_file_is_missing(tmp_path):
    stale_setup(tmp_path)
    publish(tmp_path, 2, "5")
    (tmp_path / "unload.xml").unlink()
    client = app_of(tmp_path, lambda url: (JPEG, "jpg")).test_client()
    assert client.get("/img/%s/5.jpg" % token_of(tmp_path, 2)).status_code == 404


def test_the_supplier_tab_counts_the_held_positions_it_shows(tmp_path):
    import re
    stale_setup(tmp_path)
    second(tmp_path, dict(NNOMMODIF="5"), dict(NNOMMODIF="6"), fetched=old())
    client = app_of(tmp_path).test_client()
    login(client)

    def tab(html):
        return int(re.search(r'>Второй <span class="badge badge--neutral">(\d+)<', html).group(1))
    html = client.get("/catalog").get_data(as_text=True)
    assert tab(html) == html.count("data-stale-unload>выгрузка") == 2
    from tires_sync.settings_store import confirm_draft, create_draft
    conn = store.connect(tmp_path / "t.db")
    confirm_draft(conn, create_draft(conn, {"excluded_brands": ["S2:IKON TYRES"]}, "2026-10-25T10:00:00"), "2026-10-25T10:00:00", "owner")
    conn.close()
    html = client.get("/catalog").get_data(as_text=True)
    assert tab(html) == html.count("data-stale-unload>выгрузка") == 0                # an excluded brand: neither shown nor counted
