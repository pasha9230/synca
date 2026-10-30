"""The client's catalog /shop (build 2 of the role «Клиент», plan client-role §4.3-§6, §9.2, §9.3, §13): a white list of fields, the price
from the purchase by the client's % of the brand by the diameter, «Склад 1/2», 41 of 4tochki as «40+», the filters, and the photos through
our server: nothing of the supplier (its price, our retail price, its codes, its photo address, its name) reaches the client."""
from __future__ import annotations

import io
import json
import re

import pytest

from helpers import catalog_xml, commodity_xml, make_offer
from panel_helpers import login, make_app
from tires_sync import connections, share_photos, store, users
from tires_sync.models import Season
from tires_sync.panel import client_catalog as cc, service, shop_photos
from tires_sync.panel.data import load_merged, load_reference
from tires_sync.settings_store import active_settings

NOW = "2026-10-29T12:00:00"


def picture(color, size=(1600, 1200), kind="JPEG"):
    """A real picture with the supplier's marks in its metadata (EXIF Artist and Copyright, an XMP packet, a comment)."""
    from PIL import Image
    image = Image.new("RGB", size, color)
    exif = Image.Exif()
    exif[0x013B] = "4tochki supplier-one"
    exif[0x8298] = "(c) WebMIM cdn.supplier-one.test"
    out = io.BytesIO()
    if kind == "JPEG":
        image.save(out, "JPEG", exif=exif.tobytes(), comment=b"4tochki WebMIM", xmp=b"<x:xmpmeta>supplier-one.test 4tochki</x:xmpmeta>")
    else:
        image.save(out, kind, exif=exif.tobytes())
    return out.getvalue()


JPEG = picture((0, 0, 255))
OWN_JPEG = picture((255, 0, 0))


def color_of(data):
    from PIL import Image
    with Image.open(io.BytesIO(data)) as image:
        assert image.format == "JPEG" and max(image.size) <= shop_photos.MAX_SIDE
        return image.convert("RGB").getpixel((5, 5))
FETCHED = []
TOKEN_RE = re.compile(r"/shop/photo/([0-9a-f]{32})")


@pytest.fixture(autouse=True)
def cheap_hashes_and_clean_photos(monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")
    FETCHED.clear()
    shop_photos.drain()
    shop_photos.forget()
    yield
    shop_photos.drain()
    shop_photos.forget()


def fake_fetch(url):
    FETCHED.append(url)
    if "broken" in url:
        raise share_photos.PhotoError("не картинка")
    return JPEG, "jpg"


def wm(**kw):
    """A WebMIM row whose original name is its name (the default original name is of another size)."""
    return commodity_xml(SMODIFNAME_ORIG=kw.get("SMODIFNAME", "IKON 205/55/16 T 94 Test Model"), **kw)


ROWS = (
    wm(NNOMMODIF="77001", SMODIFNAME="IKON 205/55/16 T 94 Test Model", NPRICE_DELAYED="12345", NREST="12",
                  SMNFCODE="SECRETCODE1", SPICTURE="https://cdn.supplier-one.test/p/77001.jpg"),
    wm(NNOMMODIF="77002", SMODIFNAME="IKON 245/45/18 T 96 Test Model", SWIDTH="245", SHEIGHT="45", SDIAMETR="18",
                  NPRICE_DELAYED="54321", NREST="41", SMNFCODE="SECRETCODE2", SPICTURE="https://cdn.supplier-one.test/p/77002.jpg"),
    wm(NNOMMODIF="77003", SSEASON="Летняя", SMARKA="Лауфенн", SMODIFNAME="LAUFENN 195/65/15 G Fit", SMODEL="G Fit",
                  SWIDTH="195", SHEIGHT="65", SDIAMETR="15", NPRICE_DELAYED="3333", NREST="9", SMNFCODE="SECRETCODE3",
                  SPICTURE="https://cdn.supplier-one.test/p/77003.jpg"),
    wm(NNOMMODIF="77004", SMODIFNAME="IKON 205/55/16 T 94 Test Model Zero", NPRICE_DELAYED="0", NREST="5", SMNFCODE="SECRETCODE4"),
    wm(NNOMMODIF="77005", SMODIFNAME="IKON 195/65/15 T 91 Test Model", SWIDTH="195", SHEIGHT="65", SDIAMETR="15",
                  NPRICE_DELAYED="4444", NREST="0", SMNFCODE="SECRETCODE5"),
)


def tochki_row(code, rest=41, price=7001.0):
    return {"code": code, "name": "Ikon Tyres Test Model 205/55 R16 94T", "marka": "Ikon Tyres", "model": "Test Model", "season": "w",
            "thorn": True, "type": "car", "quality": 0, "img_big_my": "", "img_big_pish": "https://img.4tochki.test/%s.jpg" % code,
            "price": price, "rest": rest, "width": "205", "height": "55", "diameter": "16", "load_index": "94", "speed_index": "T",
            "puncture": "", "strengthening": False, "omolog": "", "card": True}


def make_shop(tmp_path, rows=ROWS, tochki=(), role=(10, 20), brands=(("IKON TYRES", None, None),)):
    """An app with the rows of WebMIM, 4tochki (switch off: shown in the catalog only) and a client «buyer» with the role's conditions."""
    app = make_app(tmp_path, PHOTO_FETCH=fake_fetch)
    open(app.config["UNLOAD_PATH"], "wb").write(catalog_xml(*rows))
    conn = store.connect(app.config["DB_PATH"])
    sid = None
    if tochki:
        sid = connections.create_supplier(conn, "4Точки", NOW, "o")
        connections.set_config(conn, "tochki", 0, {"login": "api-user", "supplier_id": sid, "feed": False}, NOW, "o")
        (tmp_path / "tochki-snapshot.json").write_text(json.dumps({"version": 1, "fetched_at": "2026-10-07T11:00:00",   # an hour before the app's clock
                                                                   "warehouse_id": 7, "items": list(tochki)}), encoding="utf-8")
    users.add_user(conn, "buyer", "длинный-пароль-1", NOW, role="client")
    conn.execute("UPDATE client_terms SET pct_small = ?, pct_large = ? WHERE subject = '*'", role)
    conn.executemany("INSERT INTO client_brand (subject, brand_key, pct_small, pct_large) VALUES ('*', ?, ?, ?)", brands)
    conn.commit()
    client = app.test_client()
    with client.session_transaction() as session:
        session["user"] = "buyer"
        session["epoch"] = store.get_session_epoch(conn, "buyer")
        session["csrf"] = "tok"
    return app, client, conn, sid


def page(client, url="/shop"):
    response = client.get(url)
    assert response.status_code == 200, url
    return response.get_data(as_text=True)


def prices(html):
    return [int(re.sub(r"\D", "", p)) for p in re.findall(r'<b class="strong">([\d\s\u00a0\u202f]+)₽</b>', html)]


def listing(html):
    """The part of the page after the filters (the filters echo what the client typed)."""
    return html[html.index("Найдено"):] if "Найдено" in html else html


# --- §9.2 leaks ------------------------------------------------------------------------------------------------------------------------

def test_the_page_has_nothing_of_the_supplier_nor_the_purchase_nor_our_retail_price(tmp_path):
    app, client, conn, sid = make_shop(tmp_path, tochki=[tochki_row("T1")])
    settings = active_settings(conn)
    parsed = load_merged(conn, settings, app.config["UNLOAD_PATH"])
    retail = {g: c.price for g, c in service.evaluation(conn, settings, parsed, load_reference(app.config["REFERENCE_PATH"])).ok.items()}
    assert retail.get("77001") and retail.get("77002")                       # our retail prices exist: they must not be on the page
    html = page(client)
    shop_photos.drain()
    html += page(client)
    assert sorted(prices(html)) == sorted([13580, 65190, 7710, 3670] * 2)      # 12345×1.10, 54321×1.20 (R18), 7001×1.10, Laufenn 3333×1.10
    lowered = html.lower()
    for leak in ["12345", "12 345", "12 345", "54321", "54 321", "54 321", "7001", "7 001", "7 001", "secretcode", "77001",
                 "77002", "s%d-t1" % sid, "supplier-one", "4tochki", "webmim", "4 точки", "4точки", "mim", "4т", "/img/"] + \
            ["%d" % retail["77001"], "{:,}".format(int(retail["77001"])).replace(",", " ")]:
        assert leak.lower() not in lowered, leak
    assert "Склад 1" in html and "Склад 2" in html


def test_the_row_has_exactly_the_white_list_of_fields(tmp_path):
    app, client, conn, sid = make_shop(tmp_path, tochki=[tochki_row("T1")])
    parsed = load_merged(conn, active_settings(conn), app.config["UNLOAD_PATH"])
    from tires_sync import tochki_feed
    view, _ = tochki_feed.catalog_view(conn, parsed, app.config["UNLOAD_PATH"])
    items = cc.visible(conn, "buyer", view.offers.values(), sid)
    rows = {r["warehouse"] + r["size"]: r for r in (cc.row(i, None) for i in items)}
    assert all(tuple(r) == cc.FIELDS for r in rows.values())
    assert cc.FIELDS == ("brand", "model", "name", "size", "season", "studs", "load", "speed", "runflat", "country", "year", "stock",
                         "warehouse", "price", "photo")
    assert rows["Склад 2205/55 R16"]["stock"] == "40+" and rows["Склад 2205/55 R16"]["studs"] is True     # 4tochki 41 = «more than 40»
    assert rows["Склад 1245/45 R18"]["stock"] == "41"                          # WebMIM's 41 is a number
    assert rows["Склад 1205/55 R16"]["stock"] == "12" and rows["Склад 1205/55 R16"]["price"] == 13580


def test_every_brand_of_the_suppliers_is_shown_and_a_brand_without_its_own_pct_has_the_roles_general(tmp_path):
    """The owner 2026-10-30: every brand the suppliers have (WebMIM and 4 Точки), not only the set; a brand without its own % is priced
    by the general % of the role «Клиент» (up to R17 / from R18). Laufenn is in no set: 3333 × 1.10."""
    app, client, conn, _ = make_shop(tmp_path)
    for url in ("/shop", "/shop?q=Laufenn", "/shop?q=G+Fit", "/shop?b=laufenn", "/shop?b=LAUFENN", "/shop?size=195/65R15"):
        html = page(client, url)
        assert "laufenn" in listing(html).lower() and 3670 in prices(html), url
    assert 'value="laufenn"' in page(client)                                  # in the brand ticks
    assert 3670 not in prices(page(client, "/shop?xb=laufenn"))               # «кроме брендов» works for it
    assert "Найдено 0" in page(client, "/shop?q=SECRETCODE1")                 # the supplier's article is not searched
    assert "Найдено 0" in page(client, "/shop?q=77001")                       # nor the goods id


def test_no_purchase_no_stock_or_no_markup_hides_the_position(tmp_path):
    app, client, conn, _ = make_shop(tmp_path)
    html = page(client)
    assert "Найдено 3" in html                                                 # 77004 (purchase 0) and 77005 (stock 0) are not shown
    conn.execute("UPDATE client_terms SET pct_large = NULL WHERE subject = '*'")
    conn.commit()
    html = page(client)
    assert "Найдено 2" in html and sorted(prices(html)) == [3670, 13580]       # R18 without a % is hidden, never at the purchase
    conn.execute("UPDATE client_terms SET pct_small = NULL WHERE subject = '*'")
    conn.commit()
    html = page(client)
    assert "Ассортимент ещё не настроен" in html and prices(html) == []        # nothing has a price: as nothing set up


def test_an_empty_role_shows_an_empty_catalog(tmp_path):
    app, client, conn, _ = make_shop(tmp_path, role=(None, None), brands=())
    html = page(client)
    assert "Ассортимент ещё не настроен" in html and prices(html) == [] and "<table" not in html


def test_the_headers_of_the_page(tmp_path):
    app, client, conn, _ = make_shop(tmp_path)
    response = client.get("/shop")
    assert response.headers["Cache-Control"] == "private, no-store"
    csp = response.headers["Content-Security-Policy"]
    assert "img-src 'self';" in csp and "https:" not in csp and "script-src" not in csp and "default-src 'none'" in csp
    assert "<script" not in response.get_data(as_text=True)


# --- §9.3 price ------------------------------------------------------------------------------------------------------------------------

def terms_conn(tmp_path):
    conn = store.connect(tmp_path / "terms.db")
    conn.execute("UPDATE client_terms SET pct_small = 10, pct_large = 20 WHERE subject = '*'")
    conn.execute("INSERT INTO client_brand VALUES ('*', 'IKON TYRES', NULL, NULL)")
    conn.execute("INSERT INTO client_brand VALUES ('*', 'CORDIANT', 5, 7)")
    return conn


def price(conn, login="buyer", **kw):
    return cc.client_price(cc.terms_of(conn, login), make_offer(**kw))


@pytest.mark.parametrize("base, diameter, expected", [
    (7001, 16, 7710),        # 7701.1 -> up
    (7000, 16, 7700),        # an even ten does not grow
    (7000, 17, 7700),        # R17 is «up to R17»
    (7000, 18, 8400),        # R18 takes the large %
    (12345, 22, 14820),      # 14814 -> up
])
def test_the_price_is_the_purchase_plus_the_percent_by_the_diameter_up_to_tens(tmp_path, base, diameter, expected):
    assert price(terms_conn(tmp_path), base_price=base, diameter=diameter) == expected


def test_the_profile_over_the_role_the_brand_over_the_general_and_nothing_means_hidden(tmp_path):
    """The order (the owner 2026-10-29): the profile's % of the brand, the profile's general %, the role's % of the brand, the role's general %."""
    conn = terms_conn(tmp_path)
    conn.execute("UPDATE client_brand SET pct_small = 15, pct_large = 15 WHERE brand_key = 'CORDIANT'")
    assert price(conn, base_price=1000, diameter=16, brand_key="CORDIANT") == 1150                 # the role's % of the brand
    assert price(conn, base_price=1000, diameter=18, brand_key="S2:Cordiant") == 1150              # one brand at both warehouses
    assert price(conn, base_price=1000, diameter=16) == 1100                                       # the role's general %
    conn.execute("INSERT INTO client_terms (subject, pct_small, pct_large) VALUES ('buyer', 18, NULL)")
    assert price(conn, base_price=1000, diameter=16, brand_key="CORDIANT") == 1180                 # the example: role Cordiant 15, profile 18 -> 18
    assert price(conn, base_price=1000, diameter=18, brand_key="CORDIANT") == 1150                 # NULL in the profile: the role's brand %
    assert price(conn, base_price=1000, diameter=16) == 1180 and price(conn, base_price=1000, diameter=18) == 1200
    conn.execute("INSERT INTO client_brand VALUES ('buyer', 'CORDIANT', 12, NULL)")                 # the profile's % of the brand: first
    assert price(conn, base_price=1000, diameter=16, brand_key="CORDIANT") == 1120
    assert price(conn, base_price=1000, diameter=16) == 1180                                       # brands_mode 'inherit': the role's set
    conn.execute("UPDATE client_terms SET brands_mode = 'own' WHERE subject = 'buyer'")
    assert price(conn, base_price=1000, diameter=16) == 1180                                       # an own set without IKON: shown at
                                                                                                    # the general % (the owner 2026-10-30)
    assert price(conn, base_price=1000, diameter=16, brand_key="CORDIANT") == 1120
    conn.execute("UPDATE client_terms SET pct_small = NULL, pct_large = NULL")
    conn.execute("UPDATE client_brand SET pct_small = NULL, pct_large = NULL")
    assert price(conn, base_price=1000, diameter=16, brand_key="CORDIANT") is None                 # no % anywhere: hidden
    assert price(conn, "other", base_price=1000, diameter=16) is None


def test_a_broken_first_percent_never_falls_to_the_next(tmp_path):
    conn = terms_conn(tmp_path)
    conn.execute("INSERT INTO client_terms (subject, pct_small, pct_large) VALUES ('buyer', -1, NULL)")
    assert price(conn, base_price=1000, diameter=16) is None and price(conn, base_price=1000, diameter=18) == 1200


@pytest.mark.parametrize("pct", [-5, 300.01, float("nan"), float("inf")])
def test_a_broken_percent_hides_the_position(tmp_path, pct):
    conn = terms_conn(tmp_path)
    conn.execute("UPDATE client_terms SET pct_small = ? WHERE subject = '*'", (pct, ))
    assert price(conn, base_price=1000, diameter=16) is None
    assert price(conn, base_price=1000, diameter=18) == 1200


def test_zero_percent_only_when_set_and_no_purchase_is_hidden(tmp_path):
    conn = terms_conn(tmp_path)
    conn.execute("UPDATE client_terms SET pct_small = 0 WHERE subject = '*'")
    assert price(conn, base_price=1001, diameter=16) == 1010
    assert price(conn, base_price=0, diameter=16) is None and price(conn, base_price=-5, diameter=16) is None


# --- §6 filters -----------------------------------------------------------------------------------------------------------------------

def test_the_sizes_line(tmp_path):
    assert cc.parse_sizes("205/55R16 195/65 r15 205/55/16 225/45ZR17 235/40 Р18 junk 225 45 17") == (
        [(205, 55, 16), (195, 65, 15), (225, 45, 17), (235, 40, 18)], ["junk", "225", "45", "17"])
    assert cc.parse_sizes("") == ([], [])
    assert len(cc.parse_sizes("205/55R16 " * 200)[0]) == 1                     # the line is cut at 120 signs, a repeat counts once


def many_rows(n):
    return [wm(NNOMMODIF=str(80000 + k), SMODIFNAME="IKON %d/55/16 T 94 Test Model" % (150 + k), SWIDTH=str(150 + k),
                          NPRICE_DELAYED=str(1000 + k * 10), NREST=str(1 + k % 7), SMNFCODE="C%d" % k) for k in range(n)]


def test_the_filters_the_sort_and_the_pages(tmp_path):
    app, client, conn, sid = make_shop(tmp_path, tochki=[tochki_row("T1", rest=3)],
                                       brands=(("IKON TYRES", None, None), ("LAUFENN", None, None)))
    assert "Найдено 4" in page(client)
    html = page(client, "/shop?size=205/55R16+245/45R18")
    assert "Найдено 3" in html and "195/65 R15" not in html
    assert "Найдено 1" in page(client, "/shop?size=195/65R15") and "Найдено 1" in page(client, "/shop?b=laufenn")
    assert "Найдено 3" in page(client, "/shop?xb=laufenn") and "Найдено 0" in page(client, "/shop?b=laufenn&xb=laufenn")
    assert "Найдено 4" in page(client, "/shop?b=laufenn&b=ikon+tyres")
    assert "Найдено 1" in page(client, "/shop?s=summer") and "Найдено 3" in page(client, "/shop?s=winter")
    assert "Найдено 1" in page(client, "/shop?t=studded") and "Найдено 3" in page(client, "/shop?t=friction")
    assert "Найдено 1" in page(client, "/shop?wh=2") and "Найдено 3" in page(client, "/shop?wh=1")
    assert "Найдено 1" in page(client, "/shop?q=G+Fit") and "Найдено 2" in page(client, "/shop?q=205/55")
    assert "Не понял: abc" in page(client, "/shop?size=205/55R16+abc")
    assert prices(page(client, "/shop?o=price")) == sorted(prices(page(client, "/shop?o=price")))
    assert prices(page(client, "/shop?o=price&r=1")) == sorted(prices(page(client, "/shop?o=price")), reverse=True)
    for junk in ("/shop?o=goods_id", "/shop?p=-3", "/shop?p=x", "/shop?wh=3", "/shop?s=%00", "/shop?b=" + "x" * 5000):
        page(client, junk)


def test_pages_of_fifty(tmp_path):
    app, client, conn, _ = make_shop(tmp_path, rows=many_rows(60))
    first, second = page(client), page(client, "/shop?p=2")
    assert len(prices(first)) == 50 and len(prices(second)) == 10 and "страница 2 из 2" in second
    assert len(prices(page(client, "/shop?p=99"))) == 10                       # past the end: the last page


# --- the photos through our server ---------------------------------------------------------------------------------------------------

def tokens(html):
    return TOKEN_RE.findall(html)


def own_photo(app, conn, goods_id, data=None):
    """Our own photo (an approved three-quarter frame of a set sent to the feed) for the position `goods_id`; returns its sha."""
    from pathlib import Path
    from tires_sync import photo_library
    import hashlib
    data = data or OWN_JPEG
    offer = load_merged(conn, active_settings(conn), app.config["UNLOAD_PATH"]).offers[goods_id]
    key = photo_library.set_key_for(offer).text()
    sha = hashlib.sha256(data).hexdigest()
    root = Path(app.config["PHOTOS_ROOT"])
    root.mkdir(parents=True, exist_ok=True)
    (root / ("%s.jpg" % sha)).write_bytes(data)
    pid = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM photo").fetchone()[0]
    conn.execute("INSERT INTO photo (id, sha256, ext, source, created) VALUES (?, ?, 'jpg', 'gen', ?)", (pid, sha, NOW))
    conn.execute("INSERT INTO photo_set (key, brand_key, model_key, season, studded, size_class, state, in_feed) "
                 "VALUES (?, 'ikon tyres', 'x', 'winter', 0, 'r15_18', 'ready', 1)", (key, ))
    conn.execute("INSERT INTO set_frame (set_key, kind, photo_id, status, created) VALUES (?, 'three_quarter', ?, 'approved', ?)", (key, pid, NOW))
    conn.commit()
    return sha


def cc_tochki_gid(app, conn):
    from tires_sync.panel.routes_shop import _offers
    with app.test_request_context():
        return next(g for g in _offers(app, conn).offers if g.startswith("s"))


def test_the_photo_comes_through_our_server_under_an_opaque_name(tmp_path):
    app, client, conn, sid = make_shop(tmp_path, tochki=[tochki_row("T1")])
    own_photo(app, conn, "77001", JPEG)                                       # our photo, with marks in its metadata
    assert tokens(page(client)) == []                                          # not drawn yet: no link
    shop_photos.drain()
    found = tokens(page(client))
    ours = {cc.photo_token(app.secret_key, g) for g in ("77001", "77002")}     # one set of ours: both sizes of the model
    assert set(found) == ours and cc.photo_token(app.secret_key, cc_tochki_gid(app, conn)) not in found   # 4tochki's: a placeholder
    for token in found:
        response = client.get("/shop/photo/" + token)
        data = response.get_data()
        assert response.status_code == 200 and response.mimetype == "image/jpeg" and color_of(data)[2] > 200
        for mark in (b"4tochki", b"supplier-one", b"WebMIM", b"Exif", b"xmpmeta", b"http"):                # drawn anew: no metadata
            assert mark not in data, mark
        assert response.headers["Cache-Control"] == "private, max-age=86400" and "Location" not in response.headers
        assert "ETag" not in response.headers and "Last-Modified" not in response.headers
        text = " ".join("%s: %s" % kv for kv in response.headers.items()).lower()
        for leak in ("supplier-one", "4tochki", "77001", "77002", "t1.jpg", "s%d-" % sid):
            assert leak not in text, leak
    assert FETCHED == []                                                       # no supplier is ever asked

def test_the_photo_of_a_position_out_of_the_set_or_any_address_is_never_served(tmp_path):
    app, client, conn, sid = make_shop(tmp_path)
    page(client)
    shop_photos.drain()
    outside = cc.photo_token(app.secret_key, "77003")                         # Laufenn: not of the client's brands
    hidden = cc.photo_token(app.secret_key, "77005")                          # no stock
    for token in (outside, hidden, "0" * 32, "77001", "..%2f..%2fetc%2fpasswd", "https:%2f%2fexample.com%2fa.jpg",
                  cc.photo_token("other-secret", "77001")):
        assert client.get("/shop/photo/" + token).status_code == 404, token
    assert client.get("/shop/photo/https://example.com/a.jpg").status_code == 404
    assert client.get("/shop/photo?url=https://example.com/a.jpg").status_code in (403, 404)
    assert not any("77003" in u or "example.com" in u for u in FETCHED)
    conn.execute("DELETE FROM client_brand")                                  # the set changed: the old link is closed at once
    conn.commit()
    assert client.get("/shop/photo/" + cc.photo_token(app.secret_key, "77001")).status_code == 404


def test_the_photo_is_the_clients_only(tmp_path):
    app, client, conn, _ = make_shop(tmp_path)
    own_photo(app, conn, "77001")
    page(client)
    shop_photos.drain()
    token = cc.photo_token(app.secret_key, "77001")
    assert client.get("/shop/photo/" + token).status_code == 200
    owner = app.test_client()
    login(owner)
    assert owner.get("/shop/photo/" + token).status_code == 403
    assert app.test_client().get("/shop/photo/" + token).status_code == 302   # signed out: to the login page


def test_our_own_photo_comes_first(tmp_path):
    app, client, conn, _ = make_shop(tmp_path)
    sha = own_photo(app, conn, "77001")
    html = page(client)
    token = cc.photo_token(app.secret_key, "77001")
    assert token not in tokens(html)                                           # being made
    shop_photos.drain()
    html = page(client)
    assert token in tokens(html)                                               # made from our own photo
    own = client.get("/shop/photo/" + token).get_data()
    assert color_of(own)[0] > 200 and b"4tochki" not in own
    shop_photos.drain()
    assert "https://cdn.supplier-one.test/p/77001.jpg" not in FETCHED
    assert sha not in html                                                     # our library's name is not shown either


def test_a_position_without_our_photo_is_a_stub_and_the_supplier_is_never_asked(tmp_path):
    """The owner 2026-10-29: the supplier's photos carry its watermarks, the client must not know the supplier: only our own photos."""
    app, client, conn, _ = make_shop(tmp_path, tochki=[tochki_row("T1")])
    page(client)
    shop_photos.drain()
    html = page(client)
    shop_photos.drain()
    assert tokens(html) == [] and html.count("нет фото") == 4 and FETCHED == []     # Laufenn too: every brand (2026-10-30)
    for gid in ("77001", "77002"):
        assert client.get("/shop/photo/" + cc.photo_token(app.secret_key, gid)).status_code == 404
    assert FETCHED == []

def test_no_season_label_of_unknown_and_the_search_fields_have_no_code():
    item = cc.Visible(make_offer(code="ART123", name="IKON ART123 205/55/16", name_orig="", season=Season.UNKNOWN), 100, 1, "ikon tyres", "Ikon")
    out = cc.row(item, None)
    assert out["season"] == "" and "ART123" not in out["name"]
    assert not any("art123" in f or "100" == f for f in cc._search_fields(item)[0])


def test_a_supplier_s_copy_kept_for_img_or_made_before_is_never_served(tmp_path):
    app, client, conn, _ = make_shop(tmp_path)
    from tires_sync import photo_cache
    kept = photo_cache.root(app.config["DB_PATH"], app.config.get("IMG_ROOT"))
    (kept / "shop").mkdir(parents=True, exist_ok=True)
    (kept / "77001.png").write_bytes(picture((0, 0, 255), kind="PNG"))         # the copy /img keeps
    (kept / "shop" / "77002.jpg").write_bytes(JPEG)                            # a supplier's picture drawn by the build before
    html = page(client)
    shop_photos.drain()
    assert tokens(page(client)) == [] and tokens(html) == []
    for gid in ("77001", "77002"):
        assert client.get("/shop/photo/" + cc.photo_token(app.secret_key, gid)).status_code == 404
    assert FETCHED == []

def test_a_huge_or_false_picture_is_refused():
    with pytest.raises(share_photos.PhotoError):
        shop_photos.clean(b"\xff\xd8\xff\xe0 not really a jpeg")
    with pytest.raises(share_photos.PhotoError):
        shop_photos.clean(picture((1, 2, 3), size=(8000, 6000)))           # more than MAX_PIXELS: not decoded


def test_the_links_carry_only_the_filters_and_a_window_of_pages(tmp_path):
    app, client, conn, _ = make_shop(tmp_path, rows=many_rows(60) * 1)
    html = page(client, "/shop?evil=%3Cscript%3E&q=Test&p=2")
    assert "evil" not in html and "script" not in html.lower().replace("<script", "")
    assert 'href="/shop?q=Test&amp;p=1"' in html


def test_a_turned_phone_photo_stands_up_and_a_transparent_palette_is_on_white():
    from PIL import Image
    image = Image.new("RGB", (400, 200), (0, 0, 255))
    exif = Image.Exif()
    exif[0x0112] = 6                                                           # «turn 90°»: the picture is shown 200 × 400
    out = io.BytesIO()
    image.save(out, "JPEG", exif=exif.tobytes())
    with Image.open(io.BytesIO(shop_photos.clean(out.getvalue()))) as made:
        assert made.size == (200, 400)
    palette = Image.new("P", (50, 50), 0)
    palette.putpalette([0, 0, 0] * 256)
    out = io.BytesIO()
    palette.save(out, "PNG", transparency=0)
    assert color_of(shop_photos.clean(out.getvalue())) == (255, 255, 255)


def test_the_frame_of_the_client_is_for_a_phone_too_and_the_staff_css_is_untouched(tmp_path):
    from pathlib import Path
    app, client, conn, sid = make_shop(tmp_path, tochki=[tochki_row("T1")])
    html = page(client)
    assert '<body class="shop">' in html and "/static/shop.css" in html and 'class="shop-top"' in html
    static = Path(__file__).resolve().parents[1] / "src" / "tires_sync" / "panel" / "static"
    css = (static / "shop.css").read_text(encoding="utf-8")
    assert "body.shop{min-width:0}" in css and "@media (max-width:720px)" in css
    bare = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    selectors = [sel.strip() for block in re.findall(r"([^{}]+)\{[^{}]*\}", re.sub(r"@media[^{]*\{", "", bare)) for sel in block.split(",")]
    assert selectors and all(re.match(r"(body)?\.shop", sel) for sel in selectors), [s for s in selectors if not re.match(r"(body)?\.shop", s)]
    assert "min-width:1280px" in (static / "panel.css").read_text(encoding="utf-8")       # the staff pages stay as they are
    assert client.get("/static/shop.css").status_code == 200
    assert "Зима · шип" in html                                               # 4tochki T1: winter, studded
    assert "Ikon Tyres Test Model 205/55 R16" not in html                     # the supplier's name is not repeated
    assert all('id="f-%s"' % n in html and 'for="f-%s"' % n in html for n in ("size", "q", "s", "t", "wh"))


@pytest.mark.parametrize("fetched, shown", [("2026-10-07T07:00:00", True), ("2026-10-07T06:00:00", True), ("2026-10-07T05:59:00", False),
                                            ("2026-10-07T09:00:00+03:00", True), ("2026-10-07T08:59:00+03:00", False), ("2026-10-07T12:09:00", True),
                                            ("2026-10-07T15:00:00", False), ("", False), ("not a time", False)])
def test_an_old_snapshot_of_4tochki_hides_the_second_warehouse_and_warns_once(tmp_path, fetched, shown):
    """The owner 2026-10-29: the hourly cycle refreshes the snapshot in a feed or not; one older than 6 hours (the app's clock is
    2026-10-07 12:00 UTC) means the refresh fails: «Склад 2» is not shown, the journal says it once per snapshot."""
    app, client, conn, sid = make_shop(tmp_path, tochki=[tochki_row("T1")])
    path = tmp_path / "tochki-snapshot.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["fetched_at"] = fetched
    path.write_text(json.dumps(data), encoding="utf-8")
    html = page(client)
    page(client)
    warned = conn.execute("SELECT author, detail FROM audit WHERE action = 'shop_tochki_stale'").fetchall()
    assert len(warned) == (0 if shown else 1) and all(a == "system" for a, _ in warned)
    owner = app.test_client()
    login(owner)
    preview = page(owner, "/client-terms/c/buyer/preview")
    rows_of = lambda text: text[text.index("Найдено "):]
    assert ("«Склад 2» скрыт" in preview) is not shown
    assert ("Склад 2</td>" in rows_of(preview)) is shown and ("Склад 2</td>" in rows_of(html)) is shown
    assert "скрыт" not in html and "4 Точ" not in html                                  # the client is never told why
