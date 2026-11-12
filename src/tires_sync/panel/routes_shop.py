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

from flask import Response, abort, current_app, flash, g, make_response, redirect, render_template, request

from .. import order_manager, order_points, orders, oth_stock, photo_cache, shop_cart, store, tochki_feed
from ..settings_store import active_settings
from . import client_catalog as cc, shop_photos
from ..suppliers import supplier_of_id
from .. import users
from .common import current_user, get_conn, now, safe_path
from .routes_design import ui_new
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


def catalog(app, conn, login: str, base: str, photos: bool, cart: bool = False, new: bool = False, fview: int = 1) -> dict:
    """What the catalog page of the client `login` shows (the client's own /shop, and the owner's preview «глазами клиента» at `base`):
    the same rows, filters and prices. `photos=False`: no photo links (the owner's preview; /shop/photo is the client's own). `cart`: each
    row gets `key` (the opaque token of «В корзину»), the client's own page only. `new` («Новый вид» of the client): the width, the
    height and the radius by several values, the wider search, the chips of the filters on, the filters folded (`sf=0`). `fview` 2: the
    block of the filters «Вид 2» (the glued lists, the brands, the mixed-width sets: _view2)."""
    if not login:
        raise ValueError("the catalog of nobody")                 # never the person signed in by default
    items = _visible(app, conn, _offers(app, conn).offers.values(), login)
    f = cc.filters_of(request.args, {i.brand for i in items}, wide=new)
    both = cc.both_warehouses(conn, login)             # one warehouse on: no filter and no column «Склад» (the owner 2026-11-10)
    if not both:
        f.warehouse = 0                               # a `wh` of an old link or of the kept filters: not a filter any more
    found = cc.apply(items, f)
    total = len(found)
    pages = max(1, -(-total // cc.PAGE))
    page = min(f.page, pages)
    shots = _photos(app, conn) if photos else None
    rows = [cc.row(i, "/shop/photo/%s" % cc.photo_token(app.secret_key, i.offer.goods_id) if shots and shots.ready(i.offer) else None)
            for i in found[(page - 1) * cc.PAGE: page * cc.PAGE]]
    if cart:
        have = shop_cart.cart(conn, login)
        rows = [dict(r, key=cc.photo_token(app.secret_key, i.offer.goods_id), in_cart=have.get(i.offer.goods_id, 0))
                for r, i in zip(rows, found[(page - 1) * cc.PAGE: page * cc.PAGE])]
    keep = [(k, v) for k, v in request.args.items(multi=True) if k in cc.PARAMS and k not in ("o", "r", "p")]
    sort_links = {}
    for key in cc.SORTS:
        flip = f.sort == key and not f.descending
        sort_links[key] = base + "?" + urlencode(keep + [("o", key)] + ([("r", "1")] if flip else []))
    here = [(k, v) for k, v in request.args.items(multi=True) if k in cc.PARAMS and k != "p"]
    view = "tiles" if request.args.get("v") == "tiles" else "table"            # «Таблица» / «Плитка» (the prototype), a link, no script
    plain = [(k, v) for k, v in here if k != "v"]
    view_links = {"table": base + ("?" + urlencode(plain) if plain else ""), "tiles": base + "?" + urlencode(plain + [("v", "tiles")])}
    shown = sorted({1, pages} | set(range(max(1, page - 4), min(pages, page + 4) + 1)))       # a window of pages, not hundreds
    pager = {n: base + "?" + urlencode(here + [("p", n)]) for n in shown} if pages > 1 else {}
    out = dict(rows=rows, brands=cc.brand_list(items), f=f, total=total, page=page, pages=pages, sort_links=sort_links, pager=pager,
               seasons=cc.SEASONS, empty=not items, base=base, tochki_hidden=tochki_stale(app, conn), cart=cart, view=view, view_links=view_links,
               size={k: " ".join(map(str, v)) for k, v in (("w", f.widths), ("h", f.heights), ("d", f.diameters))},
               size_options=cc.size_values(items), filters_reset=_reset_link(base, here),
               pick=cc.size_pick(base, here, {"w": f.widths, "h": f.heights, "d": f.diameters}, cc.size_values(items)),
               recent=[], hidden=[] if both else ["wh"], columns=cc.columns_of(both), wh_both=both)
    if new:
        out.update(_new_catalog(base, f, here, items))
    out.update(fview=fview, mixed=None)
    if fview == 2:
        out.update(_view2(app, conn, login, base, f, here, items, shots, cart))
    return out


def _view2(app, conn, login: str, base: str, f, here: list, items, shots, cart: bool) -> dict:
    """«Вид 2» of the block of the filters (the owner 2026-11-09…10): the three glued lists of any size, «Производитель», the
    mixed-width sets. With «Подбор разношироких шин» (`mixed=1`) the rows are the sets (a front and a rear tyre of one model, 2 of
    each by default), then the tyres of each axis in no set; no pages."""
    out = dict(pick2=cc.size_pick2(base, here, {"w": f.widths, "h": f.heights, "d": f.diameters}, cc.size_values(items)),
               brand_pick=cc.brand_pick(base, here, items, f.brands), mixed=None, any_sizes=cc.ANY_SIZES)
    if request.args.get("mixed") != "1":
        return out
    axes = {p: cc.axis_of(request.args, p) for p, _ in cc.AXES}
    out.update(axes=[(p, label, axes[p]) for p, label in cc.AXES], recent=[], rows=[], pager={}, pages=1, page=1)

    def line(i):
        r = cc.row(i, "/shop/photo/%s" % cc.photo_token(app.secret_key, i.offer.goods_id) if shots and shots.ready(i.offer) else None,
                   qty=cc.QTY_AXIS)
        if cart:
            r.update(key=cc.photo_token(app.secret_key, i.offer.goods_id), in_cart=have.get(i.offer.goods_id, 0))
        return r
    have = shop_cart.cart(conn, login) if cart else {}
    if not any(v is not None for a in axes.values() for v in a):
        out.update(mixed={"asked": False, "kits": [], "front": [], "rear": [], "more": False, "only_more": False},
                   total=0)
        return out
    sets = cc.mixed_sets(items, f, axes["f"], axes["r"])
    cut = cc.KITS_MAX
    out.update(total=len(sets["kits"][:cut]), mixed={
        "asked": True, "kits": [(line(a), line(b)) for a, b in sets["kits"][:cut]], "front": [line(i) for i in sets["front"][:cut]],
        "rear": [line(i) for i in sets["rear"][:cut]], "more": len(sets["kits"]) > cut,
        "only_more": any(len(sets[k]) > cut for k in ("front", "rear")),
        "front_unset": sets["front_unset"], "rear_unset": sets["rear_unset"]})
    return out


def _reset_link(base: str, here: list) -> str:
    """«Сбросить» of the filters: the view, the sort and the folding stay, every filter goes."""
    reset = [kv for kv in here if kv[0] in ("v", "sf", "o", "r")]
    return base + ("?" + urlencode(reset) if reset else "")


def _new_catalog(base: str, f, here: list, items) -> dict:
    """The parts of the catalog only «Новый вид» draws: the chips of the filters on (each a link without
    it), «Сбросить»."""
    labels = dict(cc.brand_list(items))
    num = lambda v: str(v).strip().upper().lstrip("R")
    of = lambda v, chosen: [t for t in (num(x) for x in re.split(r"[\s,;]+", str(v))) if t.isdigit() and int(t) in chosen]   # «205 215» typed
    acts = {"w": lambda v: of(v, f.widths) and "ширина %s" % " ".join(of(v, f.widths)),
            "h": lambda v: of(v, f.heights) and "профиль %s" % " ".join(of(v, f.heights)),
            "d": lambda v: of(v, f.diameters) and " ".join("R" + t for t in of(v, f.diameters)),
            "s": lambda v: v == f.season and cc.SEASONS[v], "t": lambda v: v == f.studs and {"studded": "Шипы", "friction": "Без шипов"}[v],
            "wh": lambda v: f.warehouse and v == str(f.warehouse) and "Склад %s" % v,
            "b": lambda v: cc.bare_brand(v) in f.brands and labels[cc.bare_brand(v)],
            "xb": lambda v: cc.bare_brand(v) in f.without and "кроме %s" % labels[cc.bare_brand(v)],
            "q": lambda v: v.strip() and "«%s»" % v.strip()[:40], "size": lambda v: v.strip() and v.strip()[:40]}
    chips = []
    for k, v in dict.fromkeys(here):                                             # a value given twice: one chip, its cross drops both
        label = acts[k](v) if k in acts and str(v).strip() else None             # «Все» of a filter sends it empty
        if label:
            rest = [kv for kv in here if kv != (k, v)]
            chips.append((label, base + ("?" + urlencode(rest) if rest else "")))
    return dict(new=True, chips=chips, reset_link=_reset_link(base, here))


def _page(template: str, conn, login: str, **values):
    """A page of the client: the frame shows the cart's count; never kept by a cache. «Новый вид» of the client on for this viewer: the
    page of shop2/ (the old one stays exactly as it was with it off)."""
    if ui_new():
        template = "shop2/" + template
        values.setdefault("orders_count", shop_cart.open_orders(conn, login))      # the digit on «Мои заказы» (the prototype)
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
             "max": min(int(l.item.offer.stock), shop_cart.MAX_QTY) if l.item is not None else 0,
             "row": cc.row(l.item, None) if l.item is not None else None,
             "name": shop_cart._client_name(l.item) if l.item is not None else "Шина больше не продаётся",
             "sum": l.qty * l.item.price if l.item is not None else 0} for l in lines]
    tests = oth_stock._test_points(conn)                       # a point of the tests is never offered to a client
    points = [{"id": p["id"], "name": p["name"], "address": p.get("address") or "", "hours": p.get("hours") or ""}
              for p in order_points.list_points(conn) if p["id"] not in tests]
    return {"lines": lines, "rows": rows, "total": shop_cart.total(lines), "profile": shop_cart.profile(conn, login), "points": points,
            "error": "", "form": {}, "wh_both": cc.both_warehouses(conn, login)}


def _cart_new(conn, login: str) -> dict:
    """What the cart of «Новый вид» draws besides the old one's: the kept delivery, and whether this is the client's first order (the
    owner 2026-10-31 п. 26: the steps of the first order are shown on the cart)."""
    first = conn.execute("SELECT 1 FROM shop_order WHERE login = ? LIMIT 1", (login,)).fetchone() is None
    return {"delivery": shop_cart.delivery(conn, login), "first_order": first}


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


def _tell_cancel(app, conn, card: dict, name: str, stamp: str) -> None:
    """The client cancelled the order: its manager is told the way of a new order (_after_order); a failure never undoes the cancel."""
    try:
        from . import routes_vk
        _send, _deliver, notify = routes_vk.channel(app, conn, stamp)
        base = (store.get_state(conn, "public_base") or "").rstrip("/")
        notify(order_manager.staff_card(conn, orders.get_card(conn, card["id"])),
               shop_cart.cancel_message(card, name, base + "/orders/%d" % card["id"] if base else ""))
    except Exception:
        current_app.logger.exception("the message about the cancel of the client's order %s failed", card["id"])
        orders.add_event(conn, card["id"], stamp, "оповещение", "notify", "Оповещение не отправлено: канал оповещений недоступен")
        conn.commit()


def register(app) -> None:
    @app.route("/shop")
    def shop_page():
        conn, login = get_conn(), current_user()["login"]
        fview = cc.filters_view(conn, login)
        values = catalog(app, conn, login, "/shop", photos=True, cart=True, new=ui_new(), fview=fview)
        if fview == 2:
            g.shop_script = True                    # «Вид 2» updates the rows by itself: its own script of /static (the CSP lets only it)
        f = values["f"]                             # «Недавние» and «Колонки» of the client's own page only (never the owner's preview)
        recent = cc.remember_sizes(conn, login, cc.searched_sizes(f.widths, f.heights, f.diameters, f.sizes), now().isoformat(timespec="seconds"))
        here = [(k, v) for k, v in request.args.items(multi=True) if k in cc.PARAMS]
        values.update(recent=[] if values.get("mixed") is not None else cc.recent_links("/shop", here, recent), hidden=cc.hidden_columns(conn, login) + ([] if values["wh_both"] else ["wh"]))
        return _page("shop.html", conn, login, **values)

    @app.route("/shop/filters-view", methods=["POST"])
    def shop_filters_view():
        """«Вид 1 / Вид 2» of the block of the filters: kept for the client (user_pref shop.fview); back to the catalog page it came from."""
        conn, login = get_conn(), current_user()["login"]
        cc.save_filters_view(conn, login, request.form.get("fv", ""), now().isoformat(timespec="seconds"))
        return redirect(_back_to_catalog() or "/shop")

    @app.route("/shop/columns", methods=["POST"])
    def shop_columns():
        """«Колонки» of the client's table: the columns shown, kept for the client (user_pref); back to the catalog page it came from."""
        conn, login = get_conn(), current_user()["login"]
        cc.save_columns(conn, login, request.form.getlist("show"), now().isoformat(timespec="seconds"))
        return redirect(_back_to_catalog() or "/shop")

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
            before = shop_cart.cart(conn, login).get(found[0].offer.goods_id, 0)
            qty = shop_cart.put(conn, login, found[0], request.form.get("qty", "1"), now().isoformat(timespec="seconds"), add=True)
            asked = before + shop_cart._qty(request.form.get("qty", "1"))
            if qty < asked and qty >= int(found[0].offer.stock):                  # more asked than the stock: the cart holds the stock
                flash("В наличии %d шт.: столько и положили." % qty, "info")
            else:
                flash("В корзине: %d шт. %s." % (qty, shop_cart._client_name(found[0])), "ok")
        except shop_cart.CartError as err:
            flash(str(err), "error")
        return redirect(back or "/shop")

    @app.route("/shop/cart")
    def shop_cart_page():
        conn, login = get_conn(), current_user()["login"]
        return _page("shop_cart.html", conn, login, **_cart_view(app, conn, login), **(_cart_new(conn, login) if ui_new() else {}))

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
        if ui_new():
            return _checkout_new(conn, login, f, view)
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

    def _checkout_new(conn, login: str, f, view):
        """The checkout of «Новый вид» (the owner 2026-10-31 п. 27, 29): at a point — the phone only (no address); a delivery — the
        city, the street, the house, the recipient and the phone of the recipient (the word for the driver if any), kept for the next
        order. The order itself is made as by the old checkout (shop_cart.place): the same stage, fields, positions and prices."""
        try:
            phone = shop_cart.clean_phone(f.get("phone", ""))
            point, kept, extra, address = None, None, "", view["profile"]["address"]
            if f.get("way") == "point":
                point = next((p for p in view["points"] if str(p["id"]) == f.get("point_id", "")), None)
                if point is None:
                    raise shop_cart.CartError("Выберите точку самовывоза.", "point_id")
            elif f.get("way") == "delivery":
                kept = shop_cart.clean_delivery(f)
                address, extra = shop_cart.delivery_address(kept), shop_cart.delivery_note(kept)
            else:
                raise shop_cart.CartError("Выберите: забрать на точке или доставка.", "way")
            stamp = local_iso()
            name = users.get_role(conn, login)["display_name"] or login
            seen = f.get("total", "")
            card_id = shop_cart.place(conn, login, name, view["lines"], {"phone": phone, "address": address}, point, f.get("comment", ""),
                                      stamp, expected_total=int(seen) if seen.isdigit() else None, extra=extra)
        except (shop_cart.CartError, orders.OrderError) as err:
            return _page("shop_cart.html", conn, login, **dict(view, error=str(err), form=f), error_field=getattr(err, "field", ""),
                         **_cart_new(conn, login)), 400
        _after_order(app, conn, card_id, name, stamp)                   # the order is saved: its rules and its manager first
        if kept is not None:
            try:
                shop_cart.save_delivery(conn, login, kept, stamp)
            except Exception:                                          # only the next order's offer is lost, never the order
                current_app.logger.exception("the delivery of %s was not kept", login)
        return redirect("/shop/orders/%d?placed=1" % card_id)              # the page says «Заказ №… принят» itself (no flash twice)

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
        return _page("shop_order.html", conn, login, order=order, confirm=request.args.get("cancel") == "1", placed=request.args.get("placed") == "1")

    @app.route("/shop/orders/<order_id>/cancel", methods=["POST"])
    def shop_order_cancel(order_id):
        """«Отменить заказ» (the owner 2026-10-31): the client's own order while it is early (shop_cart.cancel); its manager is told the
        way of a new order. Nothing goes to the client here: only what the rules of «Отмена» send anyway."""
        conn, login = get_conn(), current_user()["login"]
        if shop_cart.my_order(conn, login, order_id) is None:
            abort(404)
        if request.form.get("confirm") != "1":
            return redirect("/shop/orders/%s?cancel=1#cancel" % int(order_id))
        stamp = local_iso()
        try:
            card = shop_cart.cancel(conn, login, order_id, stamp)
        except shop_cart.CartError as err:
            flash(str(err), "error")
            return redirect("/shop/orders/%d" % int(order_id))
        _tell_cancel(app, conn, card, users.get_role(conn, login)["display_name"] or login, stamp)
        flash("Заказ №%d отменён." % card["id"], "ok")
        return redirect("/shop/orders/%d" % card["id"])

    @app.route("/shop/profile")
    def shop_profile_page():
        conn, login = get_conn(), current_user()["login"]
        return _page("shop_profile.html", conn, login, profile=shop_cart.profile(conn, login), name=users.get_role(conn, login)["display_name"],
                     form={}, error=None, **({"delivery": shop_cart.delivery(conn, login)} if ui_new() else {}))

    def _profile_new(conn, login: str, f):
        """«Профиль» of «Новый вид»: «Контакты» (the name, the phone) and «Адрес доставки» (the fields of the delivery) are two forms
        (`part`); each checks all its fields before it writes any."""
        part = f.get("part", "")
        try:
            stamp = local_iso()
            if part == "contacts":
                shop_cart.clean_phone(f.get("phone", ""))
                if not " ".join(f.get("name", "").split()):
                    raise users.UserError("Название или имя: впишите его.")
                if " ".join(f.get("name", "").split()) != users.get_role(conn, login)["display_name"]:
                    users.set_own_name(conn, login, f.get("name", ""), stamp)
                shop_cart.save_phone(conn, login, f.get("phone", ""), stamp)
            elif part == "delivery":
                shop_cart.save_delivery(conn, login, shop_cart.clean_delivery(f), stamp)
            else:
                abort(400)
        except (shop_cart.CartError, users.UserError) as err:
            return _page("shop_profile.html", conn, login, profile=shop_cart.profile(conn, login), name=users.get_role(conn, login)["display_name"],
                         form=f, error=str(err), error_field=getattr(err, "field", ""), part=part, delivery=shop_cart.delivery(conn, login)), 400
        flash("Адрес доставки сохранён." if part == "delivery" else "Контакты сохранены.", "ok")
        return redirect("/shop/profile" + ("#delivery" if part == "delivery" else ""))

    @app.route("/shop/profile", methods=["POST"])
    def shop_profile_save():
        """«Профиль» (the owner 2026-10-30): the name, the phone and the address of delivery; the password is /account/password's."""
        conn, login = get_conn(), current_user()["login"]
        f = request.form
        if ui_new():
            return _profile_new(conn, login, f)
        try:
            stamp = local_iso()
            shop_cart.clean_contacts(f.get("phone", ""), f.get("address", ""))   # all checked first: a refusal changes nothing (the review)
            if not " ".join(f.get("name", "").split()):
                raise users.UserError("Название или имя: впишите его.")
            if " ".join(f.get("name", "").split()) != users.get_role(conn, login)["display_name"]:
                users.set_own_name(conn, login, f.get("name", ""), stamp)
            shop_cart.save_profile(conn, login, f.get("phone", ""), f.get("address", ""), stamp)
        except (shop_cart.CartError, users.UserError) as err:
            return _page("shop_profile.html", conn, login, profile=shop_cart.profile(conn, login), name=users.get_role(conn, login)["display_name"],
                         form=f, error=str(err)), 400
        flash("Профиль сохранён.", "ok")
        return redirect("/shop/profile")

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
