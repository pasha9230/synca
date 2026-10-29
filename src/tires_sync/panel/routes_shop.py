"""«Каталог для клиента» (/shop): the only page of a client (a buyer, the owner 2026-10-29), in its own small frame (shop_base.html: no menu,
no search, no bell, no counters, no scripts). The rows are built by client_catalog from a white list of fields; the photos go through
/shop/photo/<our opaque name> (shop_photos), only of a position the client may see. Both answers: `no-store` for the page, a strict CSP
(panel/__init__.security_headers)."""
from __future__ import annotations

import hmac
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from flask import Response, abort, current_app, make_response, render_template, request

from .. import photo_cache, store, tochki_feed
from ..settings_store import active_settings
from . import client_catalog as cc, shop_photos
from ..suppliers import supplier_of_id
from .common import current_user, get_conn, now
from .data import load_merged, load_reference

TOKEN = re.compile(r"^[0-9a-f]{32}$")
_tokens: dict = {}             # (id of the set of positions, the key) -> (the set, {our photo name: goods id}); one set at a time


def _offers(app, conn):
    """Every position of today's loads of both suppliers, in a feed or not (§13 В8): what the catalog shows before its filters."""
    parsed = load_merged(conn, active_settings(conn), app.config["UNLOAD_PATH"])
    view, _ = tochki_feed.catalog_view(conn, parsed, app.config["UNLOAD_PATH"])
    return view


TOCHKI_MAX_AGE = timedelta(hours=6)           # «Склад 2» older than this is not shown to a client (the owner 2026-10-29)
STALE_KEY = "shop_tochki_stale_warned"         # the snapshot already written in the journal as stale: once per snapshot


def tochki_stale(app, conn) -> bool:
    """The 4tochki snapshot is older than TOCHKI_MAX_AGE (or its time is unknown): the hourly cycle refreshes it, in a feed or not
    (cli._load_tochki); an old one means the refresh fails. The first request that sees it writes a warning in the journal."""
    sid = tochki_feed.config(conn)["supplier_id"]
    if not sid:
        return False
    loaded = tochki_feed.load_snapshot(app.config["UNLOAD_PATH"], sid)
    if loaded is None:
        return False                                  # no snapshot: no position of «Склад 2» at all
    stamp = str(loaded[1].get("fetched_at") or "")
    try:
        fetched = datetime.fromisoformat(stamp)
        if fetched.tzinfo is not None:                # the load writes UTC without a zone; one with a zone is compared in UTC
            fetched = fetched.astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        fetched = None
    if fetched is not None and now() - fetched <= TOCHKI_MAX_AGE:
        return False
    if (conn.execute("SELECT value FROM app_state WHERE key = ?", (STALE_KEY,)).fetchone() or [None])[0] != stamp:
        conn.execute("INSERT OR REPLACE INTO app_state VALUES (?, ?)", (STALE_KEY, stamp))
        store.write_audit(conn, now().isoformat(timespec="seconds"), "system", "shop_tochki_stale", {"fetched_at": stamp})
    return True


def _visible(app, conn, offers, login: str):
    sid = tochki_feed.config(conn)["supplier_id"]
    if sid and tochki_stale(app, conn):               # an old snapshot: «Склад 2» is hidden, never sold from stale stock
        offers = [o for o in offers if supplier_of_id(o.goods_id) != sid]
    return cc.visible(conn, login, offers, tochki_feed.config(conn)["supplier_id"],
                      load_reference(app.config["REFERENCE_PATH"]), store.load_brand_map(conn))


def _photos(app, conn):
    return shop_photos.Photos(conn, app.config["PHOTOS_ROOT"], photo_cache.root(app.config["DB_PATH"], app.config.get("IMG_ROOT")),
                              app.config.get("PHOTO_FETCH"))


def _goods_of(view, token: str):
    key = (id(view), current_app.secret_key)
    found = _tokens.get(key)
    if found is None:
        found = (view, {cc.photo_token(current_app.secret_key, gid): gid for gid in view.offers})
        _tokens.clear()
        _tokens[key] = found
    return found[1].get(token)


def catalog(app, conn, login: str, base: str, photos: bool) -> dict:
    """What the catalog page of the client `login` shows (the client's own /shop, and the owner's preview «глазами клиента» at `base`):
    the same rows, filters and prices. `photos=False`: no photo links (the owner's preview; /shop/photo is the client's own)."""
    if not login:
        raise ValueError("the catalog of nobody")                 # never the person signed in by default
    items = _visible(app, conn, _offers(app, conn).offers.values(), login)
    f = cc.filters_of(request.args, {i.brand for i in items})
    found = cc.apply(items, f)
    total = len(found)
    pages = max(1, -(-total // cc.PAGE))
    page = min(f.page, pages)
    shots = _photos(app, conn) if photos else None
    rows = [cc.row(i, "/shop/photo/%s" % cc.photo_token(app.secret_key, i.offer.goods_id) if shots and shots.ready(i.offer) else None)
            for i in found[(page - 1) * cc.PAGE: page * cc.PAGE]]
    keep = [(k, v) for k, v in request.args.items(multi=True) if k in cc.PARAMS and k not in ("o", "r", "p")]
    sort_links = {}
    for key in cc.SORTS:
        flip = f.sort == key and not f.descending
        sort_links[key] = base + "?" + urlencode(keep + [("o", key)] + ([("r", "1")] if flip else []))
    here = [(k, v) for k, v in request.args.items(multi=True) if k in cc.PARAMS and k != "p"]
    shown = sorted({1, pages} | set(range(max(1, page - 4), min(pages, page + 4) + 1)))       # a window of pages, not hundreds
    pager = {n: base + "?" + urlencode(here + [("p", n)]) for n in shown} if pages > 1 else {}
    return dict(rows=rows, brands=cc.brand_list(items), f=f, total=total, page=page, pages=pages, sort_links=sort_links, pager=pager,
                seasons=cc.SEASONS, empty=not items, base=base, tochki_hidden=tochki_stale(app, conn))


def register(app) -> None:
    @app.route("/shop")
    def shop_page():
        response = make_response(render_template("shop.html", **catalog(app, get_conn(), current_user()["login"], "/shop", photos=True)))
        response.headers["Cache-Control"] = "private, no-store"
        return response

    @app.route("/shop/photo/<token>")
    def shop_photo(token):
        if not TOKEN.match(token):
            abort(404)
        conn = get_conn()
        view = _offers(app, conn)
        gid = _goods_of(view, token)
        offer = view.offers.get(gid) if gid is not None and hmac.compare_digest(cc.photo_token(app.secret_key, gid), token) else None
        if offer is None or not _visible(app, conn, [offer], current_user()["login"]):              # only a position of the client's own set
            abort(404)
        photos = _photos(app, conn)
        path = photos.path(offer)
        if path is None:
            photos.ready(offer)
            abort(404)
        response = Response(path.read_bytes(), mimetype="image/jpeg")         # a small picture drawn anew; no ETag or Last-Modified
        response.headers["Cache-Control"] = "private, max-age=86400"           # (Werkzeug's ETag of a file hashes its path: the goods id)
        return response
