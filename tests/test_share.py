"""Links for clients: a snapshot behind an unguessable token, 7 days, revocable, nothing but photo, name, characteristics and (optionally) the selling price."""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import pytest

from helpers import catalog_xml, commodity_xml
from panel_helpers import login, make_app, post
from tires_sync import share, share_photos, store

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64
ROWS = (
    commodity_xml(NNOMMODIF="1", SMODIFNAME="IKON 205/55/16 T 94 Test Model Ш.", NREST="12", NPRICE_DELAYED="5000", SMNFCODE="SECRETCODE1"),
    commodity_xml(NNOMMODIF="2", SMODIFNAME="IKON 215/60/17 T 94 Test Model", SWIDTH="215", SHEIGHT="60", SDIAMETR="17",
                  NREST="2", NPRICE_DELAYED="7000", SMNFCODE="SECRETCODE2"),
    commodity_xml(NNOMMODIF="3", SSEASON="Летняя", SMARKA="Лауфенн", SMODIFNAME="LAUFENN 195/65/15 G Fit", SMODEL="G Fit",
                  SDIAMETR="15", SWIDTH="195", SHEIGHT="65", NREST="9", NPRICE_DELAYED="3000", SMNFCODE="SECRETCODE3"),
)
FETCHED = []


def fake_fetch(url):
    FETCHED.append(url)
    if "bad" in url:
        raise share_photos.PhotoError("не картинка")
    return JPEG, "jpg"


def setup(tmp_path, rows=ROWS, **cfg):
    FETCHED.clear()
    app = make_app(tmp_path, SHARE_ROOT=str(tmp_path / "share"), PHOTO_FETCH=fake_fetch, **cfg)
    client = app.test_client()
    tok = login(client)
    open(app.config["UNLOAD_PATH"], "wb").write(catalog_xml(*rows))
    return app, client, tok


def goods_ids(client):
    return re.findall(r'name="g" value="([^"]+)"', client.get("/catalog").get_data(as_text=True))


def create(client, tok, ids, **extra):
    """Creates a link and returns the page that shows its address (the redirect target)."""
    data = {"g": ids, "csrf": tok}
    data.update(extra)
    response = client.post("/shares", data=data)
    assert response.status_code == 302, response.get_data(as_text=True)
    if not response.headers["Location"].endswith("/shares/created"):
        return response
    return client.get("/shares/created")


def token_of(response):
    return re.search(r"/s/([A-Za-z0-9_-]{20,})", response.get_data(as_text=True)).group(1)


def test_a_link_shows_the_snapshot_and_nothing_a_client_must_not_see(tmp_path):
    app, client, tok = setup(tmp_path)
    ids = goods_ids(client)[:2]
    assert len(ids) == 2
    preview = client.post("/shares/new", data={"g": ids, "csrf": tok}).get_data(as_text=True)
    assert "Ссылка для клиента" in preview and "4 810 ₽" in preview
    made = create(client, tok, ids, show_price="1", note="для Audi")
    assert made.status_code == 200 and made.headers["Cache-Control"] == "no-store"
    token = token_of(made)
    anon = app.test_client()                                    # no session at all
    page = anon.get("/s/" + token)
    html = page.get_data(as_text=True)
    assert page.status_code == 200 and "для Audi" in html and "4 810 ₽" in html and "205/55 R16" in html
    for forbidden in ("5 000", "SECRETCODE", " шт", "в наличии", "под заказ", "Цена поставщика", "goods", "csrf", "/catalog"):
        assert forbidden not in html, forbidden
    assert "Set-Cookie" not in page.headers
    assert page.headers["X-Robots-Tag"].startswith("noindex") and page.headers["Referrer-Policy"] == "no-referrer"
    assert "default-src 'none'" in page.headers["Content-Security-Policy"] and "no-store" in page.headers["Cache-Control"]
    assert 'name="robots" content="noindex' in html
    photo = re.search(r'src="(/s/[^"]+/p/[^"]+)"', html).group(1)
    image = anon.get(photo)
    assert image.status_code == 200 and image.mimetype == "image/jpeg" and image.data == JPEG and "Set-Cookie" not in image.headers


def test_the_price_can_be_hidden_and_shown_again_at_once(tmp_path):
    app, client, tok = setup(tmp_path)
    token = token_of(create(client, tok, goods_ids(client)[:1], show_price=""))
    anon = app.test_client()
    assert "₽" not in anon.get("/s/" + token).get_data(as_text=True)
    link_id = store.connect(app.config["DB_PATH"]).execute("SELECT id FROM share_link").fetchone()[0]
    post(client, "/shares/%d/price" % link_id, tok, show_price="1")
    assert "4 810 ₽" in anon.get("/s/" + token).get_data(as_text=True)
    post(client, "/shares/%d/price" % link_id, tok, show_price="0")
    assert "₽" not in anon.get("/s/" + token).get_data(as_text=True)


def test_unknown_expired_and_revoked_links_look_exactly_the_same(tmp_path):
    app, client, tok = setup(tmp_path)
    ids = goods_ids(client)[:1]
    revoked, expiring = token_of(create(client, tok, ids)), token_of(create(client, tok, ids))
    anon = app.test_client()
    unknown = anon.get("/s/" + "x" * 32)
    assert unknown.status_code == 404
    link_id = store.connect(app.config["DB_PATH"]).execute("SELECT id FROM share_link ORDER BY id").fetchone()[0]
    post(client, "/shares/%d/revoke" % link_id, tok)
    gone = anon.get("/s/" + revoked)
    assert gone.status_code == 404 and gone.data == unknown.data
    assert anon.get("/s/" + expiring).status_code == 200
    app.config["NOW"] = lambda: datetime(2026, 10, 14, 12, 0, 1)     # created 2026-10-07 12:00 + 7 days
    late = anon.get("/s/" + expiring)
    assert late.status_code == 404 and late.data == unknown.data


def test_only_the_hash_of_the_token_is_stored_and_the_token_is_long_and_random(tmp_path):
    app, client, tok = setup(tmp_path)
    ids = goods_ids(client)[:1]
    one, two = token_of(create(client, tok, ids)), token_of(create(client, tok, ids))
    assert one != two and len(one) >= 32
    dump = "\n".join(str(row) for row in store.connect(app.config["DB_PATH"]).execute("SELECT * FROM share_link"))
    audit = str(list(store.connect(app.config["DB_PATH"]).execute("SELECT * FROM audit")))
    assert one not in dump and two not in dump and one not in audit
    assert share.token_hash(one) in dump


def test_the_snapshot_does_not_change_when_the_prices_change_later(tmp_path):
    app, client, tok = setup(tmp_path)
    token = token_of(create(client, tok, goods_ids(client)[:1], show_price="1"))
    open(app.config["UNLOAD_PATH"], "wb").write(catalog_xml(
        commodity_xml(NNOMMODIF="1", SMODIFNAME="IKON 205/55/16 T 94 Test Model Ш.", NREST="0", NPRICE_DELAYED="9000", SMNFCODE="SECRETCODE1")))
    html = app.test_client().get("/s/" + token).get_data(as_text=True)
    assert "4 810 ₽" in html and "9 000" not in html and "8 " not in html


def test_positions_without_a_selling_price_are_left_out_and_said_so(tmp_path):
    app, client, tok = setup(tmp_path)
    ids = goods_ids(client)
    preview = client.post("/shares/new", data={"g": ids, "csrf": tok}).get_data(as_text=True)
    assert "Не попадут в ссылку" in preview and "не рассчитана цена продажи" in preview


def test_photos_that_fail_are_skipped_and_counted(tmp_path):
    rows = (commodity_xml(NNOMMODIF="1", SMODIFNAME="IKON 205/55/16 T 94 Test Model Ш.", NREST="12", SPICTURE="https://bad.example/1.jpg"),
            commodity_xml(NNOMMODIF="2", SMODIFNAME="IKON 215/60/17 T 94 Test Model", SWIDTH="215", SHEIGHT="60", SDIAMETR="17", NREST="5",
                          SPICTURE="https://ok.example/2.jpg"))
    app, client, tok = setup(tmp_path, rows)
    made = create(client, tok, goods_ids(client), show_price="1")
    assert "Без фото: 1" in made.get_data(as_text=True)
    html = app.test_client().get("/s/" + token_of(made)).get_data(as_text=True)
    assert html.count("<img") == 1 and "нет фото" in html


def test_a_photo_is_reachable_only_through_its_own_link_and_never_by_path_tricks(tmp_path):
    app, client, tok = setup(tmp_path)
    ids = goods_ids(client)[:1]
    one, two = token_of(create(client, tok, ids)), token_of(create(client, tok, ids))
    anon = app.test_client()
    name = re.search(r"/p/([^\"]+)\"", anon.get("/s/" + one).get_data(as_text=True)).group(1)
    assert anon.get("/s/%s/p/%s" % (one, name)).status_code == 200
    assert anon.get("/s/%s/p/%s" % (two, name)).status_code == 404            # another link
    for trick in ("..%2f..%2fstate.db", "..%5c..%5cx", ".hidden", "../1/" + name, "%2e%2e/" + name):
        assert anon.get("/s/%s/p/%s" % (one, trick)).status_code == 404, trick
    assert anon.get("/s/%s/p/%s" % ("x" * 32, name)).status_code == 404


def test_the_limits_for_positions_and_for_active_links(tmp_path):
    app, client, tok = setup(tmp_path)
    conn = store.connect(app.config["DB_PATH"])
    now = datetime(2026, 10, 7, 12, 0, 0)
    item = {"name": "n", "brand": "b", "model": "m", "size": "1/2 R3", "price": 1000}
    with pytest.raises(share.ShareError):
        share.create(conn, tmp_path / "s", [item] * (share.MAX_ITEMS + 1), True, "", now, "t")
    with pytest.raises(share.ShareError):
        share.create(conn, tmp_path / "s", [], True, "", now, "t")
    for _ in range(share.MAX_ACTIVE):
        share.create(conn, tmp_path / "s", [item], True, "", now, "t")
    with pytest.raises(share.ShareError):
        share.create(conn, tmp_path / "s", [item], True, "", now, "t")
    assert conn.execute("SELECT COUNT(*) FROM share_link").fetchone()[0] == share.MAX_ACTIVE      # the refused one left nothing behind


def test_a_snapshot_keeps_only_the_allowed_fields(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    token, link_id, _ = share.create(conn, tmp_path / "s", [{"name": "n", "price": 1, "supplier_price": 999, "stock": 12, "code": "C"}],
                                     True, "", datetime(2026, 10, 7), "t")
    stored = conn.execute("SELECT data FROM share_item").fetchone()[0]
    assert "999" not in stored and "stock" not in stored and '"C"' not in stored and "supplier" not in stored


def test_expired_and_revoked_links_are_purged_with_their_photos_after_a_day(tmp_path):
    root = tmp_path / "s"
    conn = store.connect(str(tmp_path / "x.db"))
    t0 = datetime(2026, 10, 7, 12, 0, 0)
    item = {"name": "n", "price": 1, "image": "https://ok.example/p.jpg"}
    _, expired_id, _ = share.create(conn, root, [item], True, "", t0, "t", fetch=lambda u: (JPEG, "jpg"))
    _, revoked_id, _ = share.create(conn, root, [item], True, "", t0, "t", fetch=lambda u: (JPEG, "jpg"))
    share.revoke(conn, revoked_id, datetime(2026, 10, 8, 0, 0, 0))
    assert (root / str(expired_id)).exists()
    assert share.purge(conn, root, datetime(2026, 10, 8, 12, 0, 0)) == 0               # revoked 12 hours ago, the other one is still valid
    assert share.purge(conn, root, datetime(2026, 10, 14, 13, 0, 0)) == 1              # the revoked one is a day old, the expired one ended an hour ago
    assert share.purge(conn, root, datetime(2026, 10, 15, 12, 30, 0)) == 1
    assert not (root / str(expired_id)).exists() and not (root / str(revoked_id)).exists()
    assert conn.execute("SELECT COUNT(*) FROM share_link").fetchone()[0] == 0 and conn.execute("SELECT COUNT(*) FROM share_item").fetchone()[0] == 0


def test_photo_validation_accepts_real_small_images_only():
    assert share_photos.validate(JPEG)[1] == "jpg"
    assert share_photos.validate(b"\x89PNG\r\n\x1a\n" + b"0" * 20)[1] == "png"
    assert share_photos.validate(b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"0" * 20)[1] == "webp"
    for bad in (b"", b"<html><script>alert(1)</script></html>", b"GIF89a" + b"0" * 20, b"MZ" + b"0" * 20, JPEG + b"0" * share_photos.MAX_PHOTO_BYTES):
        with pytest.raises(share_photos.PhotoError):
            share_photos.validate(bad)


@pytest.mark.parametrize("url", [
    "http://example.com/a.jpg", "ftp://example.com/a.jpg", "file:///etc/passwd", "https://user:pw@example.com/a.jpg", "",
    "https://127.0.0.1/a.jpg", "https://localhost/a.jpg", "https://10.0.0.5/a.jpg", "https://192.168.1.1/a.jpg",
    "https://169.254.169.254/latest/meta-data", "https://[::1]/a.jpg"])
def test_photo_addresses_that_could_reach_our_own_network_are_refused(url):
    with pytest.raises(share_photos.PhotoError):
        share_photos.check_url(url)


def test_management_pages_need_a_login_and_the_catalog_offers_the_button(tmp_path):
    app, client, tok = setup(tmp_path)
    assert app.test_client().get("/shares").status_code == 302
    assert app.test_client().post("/shares", data={"g": ["1"]}).status_code == 400            # no csrf
    html = client.get("/catalog").get_data(as_text=True)
    assert 'formaction="/shares/new"' in html and "Ссылка для клиента" in html
    assert 'href="/shares"' in html
    assert "Ссылок ещё нет" in client.get("/shares").get_data(as_text=True)
    ids = goods_ids(client)[:1]
    create(client, tok, ids, note="подборка")
    listing = client.get("/shares").get_data(as_text=True)
    assert "подборка" in listing and "Действует" in listing and "Отозвать" in listing
    assert "share_created" in [r[0] for r in store.connect(app.config["DB_PATH"]).execute("SELECT action FROM audit")]


def test_the_public_page_never_sets_a_cookie_even_if_a_handler_touches_the_session(tmp_path, monkeypatch):
    from flask import session as flask_session
    from tires_sync.panel import common
    app, client, tok = setup(tmp_path)
    monkeypatch.setattr(common, "OPEN", common.OPEN | {"probe"})   # open by name, as the real client pages (no prefix opens a page)

    @app.route("/s/_probe")
    def probe():
        flask_session["x"] = "1"
        return "ok"

    response = client.get("/s/_probe")                       # the owner is logged in here: the cookie would be refreshed
    assert response.status_code == 200 and "Set-Cookie" not in response.headers
    assert client.get("/catalog").status_code == 200


def test_creation_does_not_hold_a_database_write_lock_while_photos_download(tmp_path):
    import sqlite3
    from tires_sync import store as st
    db = str(tmp_path / "x.db")
    conn = st.connect(db)
    seen = []

    def slow_fetch(url):
        other = sqlite3.connect(db, timeout=0.2)
        try:
            other.execute("INSERT OR REPLACE INTO app_state VALUES ('probe', '1')")
            other.commit()
            seen.append("free")
        except sqlite3.OperationalError:
            seen.append("locked")
        finally:
            other.close()
        return JPEG, "jpg"

    share.create(conn, tmp_path / "s", [{"name": "n", "price": 1, "image": "https://ok.example/p.jpg"}], True, "", datetime(2026, 10, 7), "t", fetch=slow_fetch)
    assert seen == ["free"]


def test_a_failed_creation_leaves_no_rows_and_no_files(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    root = tmp_path / "s"
    calls = []

    def fetch(url):
        calls.append(url)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return JPEG, "jpg"

    items = [{"name": "a", "price": 1, "image": "https://ok.example/1.jpg"}, {"name": "b", "price": 1, "image": "https://ok.example/2.jpg"}]
    with pytest.raises(KeyboardInterrupt):
        share.create(conn, root, items, True, "", datetime(2026, 10, 7), "t", fetch=fetch)
    assert conn.execute("SELECT COUNT(*) FROM share_link").fetchone()[0] == 0 and conn.execute("SELECT COUNT(*) FROM share_item").fetchone()[0] == 0
    assert not root.exists() or list(root.iterdir()) == []


def test_slow_photo_servers_are_cut_off_by_the_total_deadline():
    import time

    class Trickle:
        def read(self, n):
            time.sleep(0.01)
            return b"0"

    with pytest.raises(share_photos.PhotoError):
        share_photos._read_bounded(Trickle(), time.monotonic() + 0.05)

    class Endless:
        def read(self, n):
            return b"0" * n

    assert len(share_photos._read_bounded(Endless(), time.monotonic() + 5)) == share_photos.MAX_PHOTO_BYTES + 1


def test_the_price_switch_works_only_on_a_live_link_and_stale_folders_are_purged(tmp_path):
    import os
    conn = store.connect(str(tmp_path / "x.db"))
    root = tmp_path / "s"
    t0 = datetime(2026, 10, 7, 12, 0, 0)
    _, live, _ = share.create(conn, root, [{"name": "n", "price": 1}], True, "", t0, "t")
    _, dead, _ = share.create(conn, root, [{"name": "n", "price": 1}], True, "", t0, "t")
    share.revoke(conn, dead, t0)
    assert share.set_show_price(conn, live, False, t0) is True
    assert share.set_show_price(conn, dead, False, t0) is False
    assert share.set_show_price(conn, live, False, datetime(2026, 10, 20)) is False          # expired
    old, fresh = root / "999", root / ".tmp-abc"
    for folder in (old, fresh):
        folder.mkdir(parents=True)
    os.utime(old, (0, 0))
    share.purge(conn, root, t0)
    assert not old.exists() and fresh.exists()                                              # an hour-old orphan goes, a running creation stays


def test_the_stored_links_cap_stops_revoke_and_create_cycles(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    t0 = datetime(2026, 10, 7, 12, 0, 0)
    item = [{"name": "n", "price": 1}]
    for _ in range(share.MAX_ACTIVE):
        share.create(conn, tmp_path / "s", item, True, "", t0, "t")
    conn.execute("UPDATE share_link SET revoked_at = '2026-10-07T12:00:00'")             # revoked, their photos still on disk for a day
    conn.commit()
    for _ in range(share.MAX_STORED - share.MAX_ACTIVE):
        share.create(conn, tmp_path / "s", item, True, "", t0, "t")
    with pytest.raises(share.ShareError):
        share.create(conn, tmp_path / "s", item, True, "", t0, "t")


def test_refreshing_the_result_page_does_not_create_a_second_link_nor_show_the_address_again(tmp_path):
    app, client, tok = setup(tmp_path)
    first = create(client, tok, goods_ids(client)[:1])
    assert "/s/" in first.get_data(as_text=True) and first.headers["Cache-Control"] == "no-store"
    again = client.get("/shares/created")                      # a refresh
    assert again.status_code == 302 and again.headers["Location"].endswith("/shares")
    assert "/s/" not in client.get("/shares").get_data(as_text=True)
    assert store.connect(app.config["DB_PATH"]).execute("SELECT COUNT(*) FROM share_link").fetchone()[0] == 1
    assert app.test_client().get("/shares/created").status_code == 302                       # no login, no page


def test_the_access_log_hides_the_token():
    from tires_sync.panel.gunicorn_logger import mask
    assert mask('GET /s/AbC_123-xyz/p/photo.jpg HTTP/1.1') == 'GET /s/***/p/photo.jpg HTTP/1.1'
    assert mask("/s/AbC_123-xyz") == "/s/***"
    assert mask("https://panel.example/s/TOKEN123?x=1") == "https://panel.example/s/***?x=1"
    assert mask("GET /catalog?s=winter HTTP/1.1") == "GET /catalog?s=winter HTTP/1.1"
    assert mask("/shares/3/revoke") == "/shares/3/revoke"


def test_photos_are_fetched_from_the_address_that_was_checked(monkeypatch):
    seen = {}

    class Fake:
        status = 200

        def __init__(self, data):
            self.data = data

        def read(self, n):
            chunk, self.data = self.data[:n], self.data[n:]
            return chunk

    class FakeConnection:
        def __init__(self, host, port, ip, timeout):
            seen.update(host=host, port=port, ip=ip)

        def request(self, method, target, headers=None):
            seen["target"] = target

        def getresponse(self):
            return Fake(JPEG)

        def close(self):
            seen["closed"] = True

    monkeypatch.setattr(share_photos, "_PinnedHTTPS", FakeConnection)
    monkeypatch.setattr(share_photos.socket, "getaddrinfo", lambda host, port, proto=0: [(2, 1, 6, "", ("93.184.216.34", port))])
    assert share_photos.fetch("https://cdn.example.org/a/b.jpg?v=2") == (JPEG, "jpg")
    assert seen == {"host": "cdn.example.org", "port": 443, "ip": "93.184.216.34", "target": "/a/b.jpg?v=2", "closed": True}
    # a host that answers with a private address is refused before any connection is made
    seen.clear()
    monkeypatch.setattr(share_photos.socket, "getaddrinfo", lambda host, port, proto=0: [(2, 1, 6, "", ("93.184.216.34", port)), (2, 1, 6, "", ("10.0.0.7", port))])
    with pytest.raises(share_photos.PhotoError):
        share_photos.fetch("https://mixed.example.org/a.jpg")
    assert seen == {}

    class Redirect(Fake):
        status = 302

    FakeConnection.getresponse = lambda self: Redirect(b"")
    monkeypatch.setattr(share_photos.socket, "getaddrinfo", lambda host, port, proto=0: [(2, 1, 6, "", ("93.184.216.34", port))])
    with pytest.raises(share_photos.PhotoError):
        share_photos.fetch("https://cdn.example.org/moved.jpg")           # a redirect is never followed
