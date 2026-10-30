"""«Каталог для клиента» (/shop): the pages of a client (a buyer, the owner 2026-10-29), in its own small frame (shop_base.html: no menu,
no search, no bell, no counters, no scripts). The rows are built by client_catalog from a white list of fields; the photos go through
/shop/photo/<our opaque name> (shop_photos), only of a position the client may see. Both answers: `no-store` for the page, a strict CSP
(panel/__init__.security_headers). The cart, the checkout and «Мои заказы» (/shop/cart, /shop/orders; shop_cart.py) are forms and pages of
the server, no script either; a position travels as our opaque token (the one of its photo), never as the goods id."""
from __future__ import annotations

import hmac
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlsplit

from flask import Response, abort, current_app, flash, make_response, redirect, render_template, request

from .. import order_manager, order_points, orders, oth_stock, photo_cache, shop_cart, store, tochki_feed
from ..settings_store import active_settings
from . import client_catalog as cc, shop_photos
from ..suppliers import supplier_of_id
from .. import users
from .common import current_user, get_conn, now, safe_path
from .routes_orders import local_iso
from .data import load_merged, load_reference

TOKEN = re.compile(r"^[0-9a-f]{32}$")
_tokens: dict = {}             # (id of the set of positions, the key) -> (the set, {our photo name: goods id}); one set at a time


def _offers(app, conn):
    """Every position of today's loads of both suppliers, in a feed or not (§13 В8): what the catalog shows before its filters."""
    parsed = load_merged(conn, active_settings(conn), app.config["UNLOAD_PATH"])
    view, _ = tochki_feed.catalog_view(conn, parsed, app.config["UNLOAD_PATH"])
    return view


TOCHKI_MAX_AGE = timedelta(hours=6)           # «Склад 2» older than this is not shown to a client (the owner 2026-10-29)
FUTURE_SLACK = timedelta(minutes=10)           # a load time further ahead than this (a clock gone wrong) counts as unknown
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
    if fetched is not None and timedelta(0) - FUTURE_SLACK <= now() - fetched <= TOCHKI_MAX_AGE:      # a time ahead of ours: unknown
        return False
    marked = conn.execute("INSERT INTO app_state VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value "
                          "WHERE value != excluded.value", (STALE_KEY, stamp)).rowcount          # two requests at once: one record
    if marked == 1:
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


def catalog(app, conn, login: str, base: str, photos: bool, cart: bool = False) -> dict:
    """What the catalog page of the client `login` shows (the client's own /shop, and the owner's preview «глазами клиента» at `base`):
    the same rows, filters and prices. `photos=False`: no photo links (the owner's preview; /shop/photo is the client's own). `cart`: each
    row gets `key` (the opaque token of «В корзину»), the client's own page only."""
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
    if cart:
        rows = [dict(r, key=cc.photo_token(app.secret_key, i.offer.goods_id)) for r, i in zip(rows, found[(page - 1) * cc.PAGE: page * cc.PAGE])]
    keep = [(k, v) for k, v in request.args.items(multi=True) if k in cc.PARAMS and k not in ("o", "r", "p")]
    sort_links = {}
    for key in cc.SORTS:
        flip = f.sort == key and not f.descending
        sort_links[key] = base + "?" + urlencode(keep + [("o", key)] + ([("r", "1")] if flip else []))
    here = [(k, v) for k, v in request.args.items(multi=True) if k in cc.PARAMS and k != "p"]
    shown = sorted({1, pages} | set(range(max(1, page - 4), min(pages, page + 4) + 1)))       # a window of pages, not hundreds
    pager = {n: base + "?" + urlencode(here + [("p", n)]) for n in shown} if pages > 1 else {}
    return dict(rows=rows, brands=cc.brand_list(items), f=f, total=total, page=page, pages=pages, sort_links=sort_links, pager=pager,
                seasons=cc.SEASONS, empty=not items, base=base, tochki_hidden=tochki_stale(app, conn), cart=cart)


def _page(template: str, conn, login: str, **values):
    """A page of the client: the frame shows the cart's count; never kept by a cache."""
    response = make_response(render_template(template, cart_count=len(shop_cart.cart(conn, login)), **values))
    response.headers["Cache-Control"] = "private, no-store"
    return response


def _back_to_catalog() -> str:
    """The catalog page «В корзину» was pressed on (its filters and page), by the Referer of our own host; never written into a page."""
    parts = urlsplit(request.headers.get("Referer", ""))
    if parts.path != "/shop" or (parts.netloc and parts.netloc != request.host):
        return ""
    return safe_path("/shop" + ("?" + parts.query if parts.query else ""))


def _cart_goods(app, conn, login: str, token: str):
    """The goods id of the login's own cart behind the token; None for anything else."""
    if not TOKEN.match(str(token or "")):
        return None
    for gid in shop_cart.cart(conn, login):
        if hmac.compare_digest(cc.photo_token(app.secret_key, gid), token):
            return gid
    return None


def _cart_view(app, conn, login: str) -> dict:
    """The cart as the client may buy it now (the price and the stock of today), the contacts kept and the points to pick up at."""
    view = _offers(app, conn)

    def visible_of(gids):
        offers = [view.offers[g] for g in gids if g in view.offers]
        return {i.offer.goods_id: i for i in _visible(app, conn, offers, login)}
    lines = shop_cart.lines(conn, login, visible_of)
    rows = [{"key": cc.photo_token(app.secret_key, l.goods_id), "qty": l.qty, "ok": l.item is not None,
             "row": cc.row(l.item, None) if l.item is not None else None,
             "name": shop_cart._client_name(l.item) if l.item is not None else "Шина больше не продаётся",
             "sum": l.qty * l.item.price if l.item is not None else 0} for l in lines]
    tests = oth_stock._test_points(conn)                       # a point of the tests is never offered to a client
    points = [{"id": p["id"], "name": p["name"], "address": p.get("address") or "", "hours": p.get("hours") or ""}
              for p in order_points.list_points(conn) if p["id"] not in tests]
    return {"lines": lines, "rows": rows, "total": shop_cart.total(lines), "profile": shop_cart.profile(conn, login), "points": points,
            "error": "", "form": {}}


def _after_order(app, conn, card_id: int, name: str, stamp: str) -> None:
    """After the order is saved: the rules of its stage and the message to its manager (Telegram, the path of every alert). A failure here
    never takes the order back; deliver writes in the order's journal what was not sent."""
    try:
        orders._fire_rules(conn, card_id, stamp, ())
    except Exception:
        current_app.logger.exception("the rules of the client's order %s failed", card_id)
    try:
        from . import routes_vk
        _send, _deliver, notify = routes_vk.channel(app, conn, stamp)
        card = orders.get_card(conn, card_id)
        base = (store.get_state(conn, "public_base") or "").rstrip("/")      # never the Host the client's browser sent
        text = shop_cart.message(card, name, sum(i["qty"] * i["sale_unit"] for i in card["items"]), base + "/orders/%d" % card_id if base else "")
        notify(order_manager.staff_card(conn, card), text)
    except Exception:
        current_app.logger.exception("the message about the client's order %s failed", card_id)
        orders.add_event(conn, card_id, stamp, "оповещение", "notify", "Оповещение не отправлено: канал оповещений недоступен")
        conn.commit()


def register(app) -> None:
    @app.route("/shop")
    def shop_page():
        conn, login = get_conn(), current_user()["login"]
        return _page("shop.html", conn, login, **catalog(app, conn, login, "/shop", photos=True, cart=True))

    @app.route("/shop/cart/add", methods=["POST"])
    def shop_cart_add():
        conn, login = get_conn(), current_user()["login"]
        view = _offers(app, conn)
        gid = _goods_of(view, request.form.get("key", ""))
        found = _visible(app, conn, [view.offers[gid]], login) if gid is not None else []
        back = _back_to_catalog()
        if not found:
            flash("Этой шины уже нет в наличии.", "error")
            return redirect(back or "/shop")
        try:
            qty = shop_cart.put(conn, login, found[0], request.form.get("qty", "1"), now().isoformat(timespec="seconds"), add=True)
            flash("В корзине: %d шт. %s." % (qty, shop_cart._client_name(found[0])), "ok")
        except shop_cart.CartError as err:
            flash(str(err), "error")
        return redirect(back or "/shop")

    @app.route("/shop/cart")
    def shop_cart_page():
        conn, login = get_conn(), current_user()["login"]
        return _page("shop_cart.html", conn, login, **_cart_view(app, conn, login))

    @app.route("/shop/cart/set", methods=["POST"])
    def shop_cart_set():
        conn, login = get_conn(), current_user()["login"]
        gid = _cart_goods(app, conn, login, request.form.get("key", ""))
        if gid is None:
            abort(404)
        if request.form.get("drop") or request.form.get("qty", "").strip() in ("", "0"):
            shop_cart.drop(conn, login, gid)
            return redirect("/shop/cart")
        view = _offers(app, conn)
        found = _visible(app, conn, [view.offers[gid]], login) if gid in view.offers else []
        if not found:
            flash("Этой шины уже нет в наличии: уберите её из корзины.", "error")
            return redirect("/shop/cart")
        try:
            qty = shop_cart.put(conn, login, found[0], request.form.get("qty", ""), now().isoformat(timespec="seconds"))
            if str(qty) != request.form.get("qty", "").strip():
                flash("В наличии %d шт.: столько и оставили." % qty, "info")
        except shop_cart.CartError as err:
            flash(str(err), "error")
        return redirect("/shop/cart")

    @app.route("/shop/checkout", methods=["POST"])
    def shop_checkout():
        conn, login = get_conn(), current_user()["login"]
        f = request.form
        view = _cart_view(app, conn, login)
        if not view["lines"]:                                    # a second press after the order: it is there, not an error
            flash("Корзина пуста: оформленный заказ — в «Мои заказы».", "info")
            return redirect("/shop/orders")
        try:
            contacts = shop_cart.clean_contacts(f.get("phone", ""), f.get("address", ""))
            point = None
            if f.get("way") == "point":
                point = next((p for p in view["points"] if str(p["id"]) == f.get("point_id", "")), None)
                if point is None:
                    raise shop_cart.CartError("Выберите точку самовывоза.")
            elif f.get("way") != "delivery":
                raise shop_cart.CartError("Выберите: самовывоз с точки или доставка.")
            stamp = local_iso()
            name = users.get_role(conn, login)["display_name"] or login
            seen = f.get("total", "")
            card_id = shop_cart.place(conn, login, name, view["lines"], contacts, point, f.get("comment", ""), stamp,
                                      expected_total=int(seen) if seen.isdigit() else None)
        except (shop_cart.CartError, orders.OrderError) as err:
            return _page("shop_cart.html", conn, login, **dict(view, error=str(err), form=f)), 400
        _after_order(app, conn, card_id, name, stamp)
        flash("Заказ №%d принят, менеджер свяжется с вами." % card_id, "ok")
        return redirect("/shop/orders/%d" % card_id)

    @app.route("/shop/orders")
    def shop_orders_page():
        conn, login = get_conn(), current_user()["login"]
        return _page("shop_orders.html", conn, login, orders_list=shop_cart.my_orders(conn, login))

    @app.route("/shop/orders/<order_id>")
    def shop_order_page(order_id):
        conn, login = get_conn(), current_user()["login"]
        order = shop_cart.my_order(conn, login, order_id)
        if order is None:
            abort(404)                                          # another client's, not of the catalog, or no such: the same answer
        return _page("shop_order.html", conn, login, order=order)

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
