from __future__ import annotations

import re
import sqlite3
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

from flask import abort, current_app, flash, jsonify, redirect, render_template, request

from .. import catalog_archive, connections, order_oth, order_points, order_wait, orders, staging, store, tochki_feed
from ..config import settings_from_dict
from ..settings_store import StaleDraftError, get_active
from .common import author, can, can_post, get_conn, has_right, login_required, now, now_iso, remember_page, return_page, safe_path
from .data import load_merged, stale_unload
from ..settings_store import active_settings
from .routes_main import context
from .routes_orders import local_iso
from . import service


def _num(args, name):
    value = args.get(name, "")
    return int(value) if value.isascii() and value.isdigit() else None


MAX_SIZE_VALUES = 20


def _nums(args, name):
    """«205, 215» or «205 215» -> [205, 215]; anything that is not a number is ignored; None when nothing is left."""
    found = []
    for token in re.split(r"[\s,;]+", args.get(name, "")):
        if token.isascii() and token.isdigit() and int(token) not in found:
            found.append(int(token))
    return found[:MAX_SIZE_VALUES] or None


def _back(default="/catalog"):
    return safe_path(request.form.get("next", "")) or default


def _in_place() -> bool:
    return request.headers.get("X-Requested-With") == "fetch"


def _state(conn) -> dict:
    staged = staging.summary(conn)
    return {"staged": 0 if staged["stale"] else staged["count"], "stale": staged["stale"], "review_url": "/catalog/review"}


def _view_settings(conn, raw, changes):
    """Settings as the first account sees them (its own price delta and stock threshold): what the catalog shows."""
    effective = settings_from_dict({**raw, **changes}, enforce_pct_limit=False)
    accounts = connections.list_feed_accounts(conn)
    return connections.effective_settings(conn, accounts[0]["id"], effective) if accounts else effective


PAGE_SIZES = (10, 50, 100, 200, 500)
PAGE_SIZE = 100


def _page_size(args) -> int:
    n = _num(args, "n")
    return n if n in PAGE_SIZES else PAGE_SIZE


def _gone_view(conn, raw, args, season, studs, live, ref=None) -> list:
    """The positions the supplier took away that fit the owner's exclusions and the size, season, studs and supplier filters of the page."""
    known = {o.goods_id: (o, info) for o, info in catalog_archive.gone_offers(conn) if o.goods_id not in live}      # the page may hold a fresher unload than the last cycle
    if not known:
        return []
    shown = service.filter_offers(service.hide_excluded(service._Filtered({g: o for g, (o, _) in known.items()}, {}), raw, conn, ref),
                                  _nums(args, "w"), _nums(args, "h"), _nums(args, "d"), season, studs, _num(args, "sup"))
    return [known[g] for g in shown.offers]


def _held_offers(conn, settings, base) -> list:
    """(position, load time) of every secondary WebMIM unload the cycle holds as old (`data.stale_unload`): its positions are not in the
    panel's set (no price, never published from the panel), the catalog shows them for viewing only."""
    out = []
    for supplier in connections.list_suppliers(conn):
        held = stale_unload(conn, settings, base, supplier["id"])
        if held is not None:
            out += [(offer, held[1]) for offer in held[0].offers.values()]
    return out


def _held_view(conn, raw, args, season, studs, held, ref=None) -> list:
    """The held positions that fit the owner's exclusions, the filters of the page and its search (as the catalog's own positions)."""
    if not held:
        return []
    known = {o.goods_id: (o, at) for o, at in held}
    shown = service.filter_offers(service.hide_excluded(service._Filtered({g: o for g, (o, _) in known.items()}, {}), raw, conn, ref),
                                  _nums(args, "w"), _nums(args, "h"), _nums(args, "d"), season, studs, _num(args, "sup"))
    terms = service.search_terms(args.get("q", ""))
    if terms:
        from ..catalog import resolve_brand
        brand_map = store.load_brand_map(conn)
        shown = service._Filtered({g: o for g, o in shown.offers.items()
                                   if service.offer_matches(o, (resolve_brand(o.brand_key, ref, brand_map) or "").lower(), terms)})
    return [known[g] for g in shown.offers]


def _held_item(offer, fetched, published: bool, capped_supplier) -> dict:
    """A row of a held position: what the old file says, no price of ours, nothing to do with it here."""
    when = service._when(fetched.isoformat(timespec="seconds"))
    reason = "выгрузка устарела (скачана %s): %s" % (when, "объявление держится как было" if published else "не публикуется")
    return {"goods_id": offer.goods_id, "name": offer.name_orig or offer.name, "code": offer.code, "image": offer.image,
            "kind": service.tire_kind(offer.season.value, offer.studded), "supplier_price": offer.base_price, "price": None,
            "stock": offer.stock, "diameter": offer.diameter, "brand": offer.brand_key, "model": " ".join(offer.model_raw.split()),
            "size": "%d/%d R%d" % (offer.width, offer.height, offer.diameter),
            "car_type": offer.car_type.upper() if offer.car_type.upper() in ("C", "LT") else "",
            "state": "feed" if published else "no", "reason": reason, "scode": "stale", "manual": None, "problem": False,
            "published": published, "pending": False, "view_only": True,
            "supplier_mark": service.supplier_mark(service.supplier_of(offer), capped_supplier)}


def register(app) -> None:
    from . import routes_catalog_oth                                  # «Товар и фид» → «На ОТХ»
    routes_catalog_oth.register(app)

    def build(args):
        conn, settings, parsed, ref = context(app)
        _, raw = get_active(conn)
        staged = staging.summary(conn)
        changes = {} if staged["stale"] else staged["changes"]
        effective = _view_settings(conn, raw, changes)
        season = args.get("s") if args.get("s") in ("summer", "winter") else None
        studs = args.get("t") if args.get("t") in ("studded", "friction") else None
        status = args.get("st") if args.get("st") in ("feed", "no", "nopair", "problem", "gone") else "all"
        sort = args.get("o") if args.get("o") in service.SORT_KEYS else "brand"
        descending = args.get("r") == "1"
        parsed, view_only = tochki_feed.catalog_view(conn, parsed, app.config["UNLOAD_PATH"])      # 4tochki before its switch: shown, never published
        parsed = service.hide_excluded(parsed, raw, conn, ref)                                    # 4tochki: worked brands only
        filtered = service.filter_offers(parsed, _nums(args, "w"), _nums(args, "h"), _nums(args, "d"), season, studs,
                                         _num(args, "sup"))
        held_all = _held_offers(conn, settings, app.config["UNLOAD_PATH"])                    # an old second unload: shown, never published
        held_ids = {o.goods_id for o, _ in held_all}
        gone = _gone_view(conn, raw, args, season, studs, set(parsed.offers) | held_ids, ref) if status in ("all", "gone") else []
        capped = tochki_feed.config(conn)["supplier_id"]
        groups = service.catalog_groups(
            conn, effective, filtered, ref, args.get("q", ""), "all" if status in ("nopair", "problem") else status, sort, descending, base_raw=raw,
            gone=gone, view_only=view_only, capped_supplier=capped, twins_from=parsed)
        held_shown = {o.goods_id for o, _ in _held_view(conn, raw, {}, None, None, held_all, ref)}   # after the exclusions: the tab counts
        held = []
        if status in ("all", "feed", "no"):
            published = {re.sub(r"-x\d+$", "", g) for acc in connections.list_feed_accounts(conn) for g in store.load_published(conn, acc["id"])}
            held = [_held_item(o, at, o.goods_id in published, capped) for o, at in _held_view(conn, raw, args, season, studs, held_all, ref)]
            held = [i for i in held if status == "all" or i["state"] == status]
        items = service.flat_items(groups + [{"items": held}], sort, descending)
        if status == "nopair":
            items = [i for i in items if i["scode"] == "model_needs_mapping"]
        elif status == "problem":
            items = [i for i in items if i["problem"]]
        total = len(items)
        size = _page_size(args)
        pages = max(1, -(-total // size))
        page = min(max(_num(args, "p") or 1, 1), pages)
        shown = items[(page - 1) * size: page * size]
        if not has_right("money"):                                    # the supplier's price is a purchase price: not even in the HTML
            shown = [dict(i, supplier_price=None) for i in shown]
        on_oth = order_oth.goods_on_oth(conn)                        # our own tyres of refused orders: a mark only, the feed is not touched
        shown = [dict(i, oth=on_oth.get(i["goods_id"])) for i in shown]
        return dict(conn=conn, parsed=parsed, effective=effective, staged=staged, items=shown, held_ids=held_ids, held_shown=held_shown,
                    page_size=size, total=total, brands=len({i["brand"] for i in items}), page=page, pages=pages, season=season or "",
                    studs=studs or "", status=status, sort=sort, descending=descending)

    def table_context(args, next_url=None):
        args = {k: v for k, v in args.items() if k != "wait"}           # the window «В лист ожидания» is no filter of the table
        b = build(args)
        base = {k: v for k, v in args.items() if k not in ("o", "r", "p")}
        links = {}
        for key in service.SORT_KEYS:
            flip = b["sort"] == key and not b["descending"]
            links[key] = "/catalog?" + urlencode(dict(base, o=key, **({"r": "1"} if flip else {})))
        keep = {k: v for k, v in args.items() if k != "p"}
        size_links = {n: "/catalog?" + urlencode(dict(keep, n=n)) for n in PAGE_SIZES}
        pager = {p: "/catalog?" + urlencode(dict(keep, p=p)) for p in range(1, b["pages"] + 1)} if b["pages"] > 1 else {}
        b.update(links=links, pager=pager, size_links=size_links, next_url=next_url or request.full_path.rstrip("?"), sup=_num(args, "sup"),
                 show_marks=_num(args, "sup") is None and len(connections.list_suppliers(b["conn"])) > 1,
                 orphans=[o for o in service.orphan_overrides(b["effective"], b["parsed"]) if o["goods_id"] not in b["held_ids"]],   # held: still there
                 table_url="/catalog/table?" + urlencode(dict(args.items())),
                 wait_url="/catalog?" + urlencode(dict(args, wait="")))     # + the position: the page opens with its window
        return b

    def _item_path(goods_id):
        return "/catalog/item/" + quote(goods_id, safe="")

    def _wait_offer(conn, goods_id):
        """The position of the window «В лист ожидания»: today's unload (4tochki shown too), else a position the supplier took away."""
        _conn, _settings, parsed, _ref = context(app)
        parsed, _view_only = tochki_feed.catalog_view(conn, parsed, app.config["UNLOAD_PATH"])
        return parsed.offers.get(goods_id) or catalog_archive.gone_offer(conn, goods_id)

    def _wait_window(conn, goods_id, values: dict, error: str, back: str, offer=None):
        offer = offer or (_wait_offer(conn, goods_id) if goods_id else None)
        if offer is None:
            return None
        return {"goods_id": goods_id, "offer": offer, "line": order_wait.line_from_offer(offer), "typed": values, "error": error,
                "points": order_points.list_points(conn), "next": back, "seasons": order_wait.SEASON_NAMES, "studs": order_wait.STUD_NAMES}

    def catalog_page(args, wait=None, next_url=None):
        """The catalog; `wait`: the window «В лист ожидания» already made (a refused form comes back with what was typed)."""
        if "wait" in args and next_url is None:                           # the page without its window: where the window goes back to
            next_url = ("/catalog?" + urlencode({k: v for k, v in args.items() if k != "wait"})).rstrip("?")
        b = table_context(args, next_url)
        conn = b["conn"]
        if wait is None and (args.get("wait") or "").strip() and can_post("catalog_wait"):
            gid = args["wait"].strip()                                    # the page read the unload already: one more read only when it hides it
            wait = _wait_window(conn, gid, {}, "", b["next_url"], b["parsed"].offers.get(gid) or catalog_archive.gone_offer(conn, gid))
            if wait is None:
                flash("Позиции нет в каталоге: обновите страницу.", "error")
        suppliers = connections.list_suppliers(conn)
        sup = _num(args, "sup")
        supplier_tabs = []
        if len(suppliers) > 1:
            plain = {k: v for k, v in args.items() if k not in ("sup", "p", "wait")}
            counts = {}
            for gid in set(b["parsed"].offers) | b["held_shown"]:
                sid = service.supplier_of_id(gid)
                counts[sid] = counts.get(sid, 0) + 1
            supplier_tabs = [{"label": "Все", "on": sup is None, "url": "/catalog?" + urlencode(plain), "count": sum(counts.values())}] + [
                {"label": sp["name"], "on": sup == sp["id"], "url": "/catalog?" + urlencode(dict(plain, sup=sp["id"])),
                 "count": counts.get(sp["id"], 0)} for sp in suppliers]
        return render_template(
            "catalog.html", size={k: ", ".join(map(str, _nums(args, k) or ())) for k in ("w", "h", "d")}, q=args.get("q", ""),
            size_options=service.size_options(b["parsed"]), supplier_tabs=supplier_tabs, wait=wait, **b)

    @app.route("/catalog")
    @login_required
    def catalog():
        return catalog_page(request.args)

    @app.route("/catalog/wait", methods=["POST"])
    @login_required
    def catalog_wait():
        """«В лист ожидания»: a new order in «Ожидает наличие» for the position (order_wait.wait_from_catalog). A refused window comes back
        with what was typed; the client's name and phone go only into the order, never into the address or the session."""
        conn = get_conn()
        form = request.form
        goods_id = (form.get("goods_id") or "").strip()
        back = safe_path(form.get("next", "")) or "/catalog"
        on_item = bool(goods_id) and back == _item_path(goods_id)            # the page of this position, or the catalog: nowhere else
        if urlsplit(back).path != "/catalog" and not on_item:
            back = "/catalog"
        offer = _wait_offer(conn, goods_id) if goods_id else None
        if offer is None:
            flash("Позиции нет в каталоге: обновите страницу.", "error")
            return redirect(back)
        fields = {k: form.get(k, "") for k in ("client_name", "client_phone", "qty", "season", "point_id", "comment", "any_brand", "any_size")}
        stamp = local_iso()
        try:
            orders.ensure_pipeline(conn, stamp)
            card_id, made = order_wait.wait_from_catalog(conn, offer, fields, stamp, author())
        except (orders.OrderError, sqlite3.OperationalError) as err:     # a base busy longer than its wait: nothing written, the window stays
            text = str(err) if isinstance(err, orders.OrderError) else "База сейчас занята: ничего не записано, нажмите «Добавить» ещё раз."
            window = _wait_window(conn, goods_id, fields, text, back, offer)
            if on_item:
                return item_page(goods_id, wait=window)
            return catalog_page(dict(parse_qsl(urlsplit(back).query)), wait=window, next_url=back)
        if made:
            flash("Клиент добавлен в лист ожидания: заказ №%d, этап «Ожидает наличие»." % card_id, "ok")
        else:
            flash("Клиент уже ждёт эту шину: заказ №%d. Второй заказ не создан%s." % (
                card_id, "; точку и комментарий из окна допишите в этом заказе" if (fields["point_id"] or fields["comment"].strip()) else ""))
        return redirect(back)

    @app.route("/catalog/table")
    @login_required
    def catalog_table():
        return render_template("_catalog_table.html", **table_context(request.args))

    def _names(conn, ids):
        parsed = load_merged(conn, active_settings(conn), current_app.config["UNLOAD_PATH"])
        return [(parsed.offers[g].name_orig or parsed.offers[g].name) if g in parsed.offers else g for g in ids]

    @app.route("/catalog/stage", methods=["POST"])
    @login_required
    def catalog_stage():
        conn = get_conn()
        form = request.form
        message = ""
        try:
            if form.get("bulk"):
                mode = form["bulk"]
                if mode not in ("on", "off", "clear"):
                    raise ValueError("Неизвестное действие.")
                ids = form.getlist("g")
                staging.set_overrides(conn, ids, None if mode == "clear" else mode, now_iso())
                message = "%s позиций: %d." % ({"on": "Добавлено вручную", "off": "Снято", "clear": "Возвращено"}[mode], len(set(ids)))
            elif form.get("op"):
                mode, _, goods_id = form["op"].partition(":")
                if mode not in ("on", "off", "clear") or not goods_id:
                    raise ValueError("Неизвестное действие.")
                staging.set_override(conn, goods_id, None if mode == "clear" else mode, now_iso())
                name = _names(conn, [goods_id])[0]
                message = "%s: %s." % ({"on": "Добавлено вручную", "off": "Снято", "clear": "Правило убрано"}[mode], name)
            else:
                raise ValueError("Неизвестное действие.")
        except (StaleDraftError, ValueError) as err:
            if _in_place():
                return jsonify(ok=False, message=str(err), **_state(conn))
            flash(str(err))
            return redirect(_back())
        if _in_place():
            return jsonify(ok=True, message=message + " Пока это черновик: подтвердите изменения, чтобы они попали в фид.",
                           **_state(conn))
        return redirect(_back())

    @app.route("/catalog/preview/<goods_id>")
    @login_required
    def catalog_preview(goods_id):
        conn, settings, parsed, ref = context(app)
        _, raw = get_active(conn)
        staged = staging.summary(conn)
        changes = {} if staged["stale"] else staged["changes"]
        effective = _view_settings(conn, raw, changes)
        data = service.catalog_preview(conn, effective, parsed, ref, goods_id, now().date())
        if data is None:
            abort(404)
        return render_template("catalog_preview.html", p=data)

    def item_page(goods_id, wait=None):
        """The page of one position; `wait`: the window «В лист ожидания» already made (a refused form comes back with what was typed),
        else `?wait=1` opens it empty (the same window and the same POST as the catalog's)."""
        conn, settings, parsed, ref = context(app)
        _, raw = get_active(conn)
        staged = staging.summary(conn)
        effective = _view_settings(conn, raw, {} if staged["stale"] else staged["changes"])
        data = service.catalog_item_view(conn, effective, parsed, ref, goods_id, now().date(), has_right("money"), can("orders"))
        if data is None:
            abort(404)
        if has_right("money"):                                       # the own prices: the owner and the partner see them, the owner sets them
            general = settings_from_dict({**raw, **({} if staged["stale"] else staged["changes"])}, enforce_pct_limit=False)
            data["own_prices"] = service.own_price_rows(conn, general, settings_from_dict(raw, enforce_pct_limit=False), parsed.offers[goods_id])
            data["purchase"] = parsed.offers[goods_id].base_price
        if wait is None and request.args.get("wait") and can_post("catalog_wait"):
            wait = _wait_window(conn, goods_id, {}, "", _item_path(goods_id), parsed.offers.get(goods_id))
        return render_template("catalog_item.html", entity=data, wait=wait, wait_url=_item_path(goods_id) + "?wait=1")

    @app.route("/catalog/item/<goods_id>/own-price", methods=["POST"])
    @login_required
    def catalog_own_price(goods_id):
        """The own price of a position in the accounts ticked: into the working draft (applied with «Применить» like every change). The owner only
        (the section «catalog» to change it and the right «money»; common.SECTION_OF, NEEDS)."""
        conn = get_conn()
        form = request.form
        feed = {a["id"] for a in connections.list_feed_accounts(conn)}
        try:
            accounts = [int(a) for a in form.getlist("a") if str(a).isdigit()]
            if not accounts or any(a not in feed for a in accounts):
                raise ValueError("Отметьте аккаунты, где поставить цену.")
            if form.get("action") == "clear":
                value = None
            else:
                kind = form.get("kind")
                text = str(form.get("value") or "").replace(" ", "").replace("\u00a0", "").replace(",", ".").replace("−", "-")
                if kind not in ("pct", "price") or not re.fullmatch(r"[+\-]?\d+(\.\d+)?", text):
                    raise ValueError("Введите процент к закупке или цену продажи числом.")
                value = {"pct": float(text)} if kind == "pct" else {"price": float(text)}
            if value is not None and goods_id not in context(app)[2].offers:          # «Как у бренда» also for a position gone
                raise ValueError("Такой позиции нет в выгрузке.")
            staging.set_own_price(conn, goods_id, accounts, value, now_iso())
        except (StaleDraftError, ValueError) as err:
            flash(str(err))
            return redirect(_item_path(goods_id))
        flash(("Своя цена" if value else "«Как у бренда»") + " в черновике: подтвердите изменения, чтобы они попали в фид.")
        return redirect(_item_path(goods_id))

    @app.route("/catalog/item/<goods_id>")
    @login_required
    def catalog_item_page(goods_id):
        """The page of one position (the one look of the cards); the catalog section only: a point never opens it (common.SECTION_OF)."""
        return item_page(goods_id)

    @app.route("/catalog/cancel", methods=["POST"])
    @login_required
    def catalog_cancel():
        staging.cancel_all(get_conn(), now_iso(), author())
        flash("Неподтверждённые изменения отменены.")
        return redirect(_back())

    @app.route("/catalog/review")
    @login_required
    def catalog_review():
        staged = staging.summary(get_conn())
        if staged["draft_id"] is None:
            flash("Неподтверждённых изменений нет.")
            return redirect(return_page("/catalog"))
        remember_page()
        return redirect("/draft/%d" % staged["draft_id"])
