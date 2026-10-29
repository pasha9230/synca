"""«Каталог для клиента» (/shop): the only page of a client (a buyer, the owner 2026-10-29), in its own small frame (shop_base.html: no menu,
no search, no bell, no counters, no scripts). The rows are built by client_catalog from a white list of fields; the photos go through
/shop/photo/<our opaque name> (shop_photos), only of a position the client may see. Both answers: `no-store` for the page, a strict CSP
(panel/__init__.security_headers)."""
from __future__ import annotations

import hmac
import re
from urllib.parse import urlencode

from flask import Response, abort, current_app, make_response, render_template, request

from .. import photo_cache, store, tochki_feed
from ..settings_store import active_settings
from . import client_catalog as cc, shop_photos
from .common import current_user, get_conn
from .data import load_merged, load_reference

TOKEN = re.compile(r"^[0-9a-f]{32}$")
_tokens: dict = {}             # (id of the set of positions, the key) -> (the set, {our photo name: goods id}); one set at a time


def _offers(app, conn):
    """Every position of today's loads of both suppliers, in a feed or not (§13 В8): what the catalog shows before its filters."""
    parsed = load_merged(conn, active_settings(conn), app.config["UNLOAD_PATH"])
    view, _ = tochki_feed.catalog_view(conn, parsed, app.config["UNLOAD_PATH"])
    return view


def _visible(app, conn, offers):
    return cc.visible(conn, current_user()["login"], offers, tochki_feed.config(conn)["supplier_id"],
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


def register(app) -> None:
    @app.route("/shop")
    def shop_page():
        conn = get_conn()
        items = _visible(app, conn, _offers(app, conn).offers.values())
        f = cc.filters_of(request.args, {i.brand for i in items})
        found = cc.apply(items, f)
        total = len(found)
        pages = max(1, -(-total // cc.PAGE))
        page = min(f.page, pages)
        photos = _photos(app, conn)
        rows = [cc.row(i, "/shop/photo/%s" % cc.photo_token(app.secret_key, i.offer.goods_id) if photos.ready(i.offer) else None)
                for i in found[(page - 1) * cc.PAGE: page * cc.PAGE]]
        keep = [(k, v) for k, v in request.args.items(multi=True) if k in cc.PARAMS and k not in ("o", "r", "p")]
        sort_links = {}
        for key in cc.SORTS:
            flip = f.sort == key and not f.descending
            sort_links[key] = "/shop?" + urlencode(keep + [("o", key)] + ([("r", "1")] if flip else []))
        here = [(k, v) for k, v in request.args.items(multi=True) if k in cc.PARAMS and k != "p"]
        shown = sorted({1, pages} | set(range(max(1, page - 4), min(pages, page + 4) + 1)))       # a window of pages, not hundreds
        pager = {n: "/shop?" + urlencode(here + [("p", n)]) for n in shown} if pages > 1 else {}
        response = make_response(render_template(
            "shop.html", rows=rows, brands=cc.brand_list(items), f=f, total=total, page=page, pages=pages, sort_links=sort_links,
            pager=pager, seasons=cc.SEASONS, empty=not items))
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
        if offer is None or not _visible(app, conn, [offer]):              # only a position of the client's own set
            abort(404)
        photos = _photos(app, conn)
        path = photos.path(offer)
        if path is None:
            photos.ready(offer)
            abort(404)
        response = Response(path.read_bytes(), mimetype="image/jpeg")         # a small picture drawn anew; no ETag or Last-Modified
        response.headers["Cache-Control"] = "private, max-age=86400"           # (Werkzeug's ETag of a file hashes its path: the goods id)
        return response
