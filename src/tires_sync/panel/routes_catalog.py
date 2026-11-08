from __future__ import annotations

import dataclasses
import json
import re
import sqlite3
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

from flask import abort, current_app, flash, jsonify, redirect, render_template, request
from werkzeug.datastructures import MultiDict

from .. import catalog_archive, connections, market, own_bulk, order_oth, order_points, order_wait, orders, staging, store, tochki_feed
from ..config import own_price_value, settings_from_dict
from ..pricing import position_price
from ..settings_store import StaleDraftError, get_active
from .common import author, can, can_post, get_conn, has_right, login_required, now, now_iso, remember_page, return_page, safe_path
from .routes_design import office_page
from .data import load_merged, stale_unload
from ..webmim import ParseResult
from ..settings_store import active_settings
from .routes_main import context
from .routes_orders import local_iso
from . import client_catalog, routes_market, service


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


def _own_value(text: str) -> dict:
    """What the cell «Наша цена» was given (the owner 2026-10-30): «6990» a sale price in ₽, «+5 %» / «−5 %» / «5%» a percent to the
    purchase, as the own price of the card."""
    text = str(text or "").replace(" ", "").replace("\u00a0", "").replace("\u202f", "").replace(",", ".").replace("−", "-").replace("–", "-").replace("₽", "")
    if text.endswith("%"):
        number = text[:-1]
        if re.fullmatch(r"[+\-]?[0-9]+(\.[0-9]+)?", number):
            return {"pct": float(number) + 0.0}                        # «−0 %» is 0
    elif re.fullmatch(r"[0-9]+", text):
        return {"price": int(text)}
    raise ValueError("Введите цену в рублях (6990) или процент к закупке (+5 % или −5 %).")


PAGE_SIZES = (10, 50, 100, 200, 500)
MARKET_SHOWN = False             # «Рынок» of the catalog (the owner 2026-10-30: not before the parser itself): the column, «Дороже рынка», «Конкуренты»
BULK_PRICE_LIMIT = 1000          # positions of one press of «Своя цена выбранным» (the owner 2026-10-30)
BULK_PCT_MAX = 50                # a markup or a markdown of one press, % to the purchase (the owner 2026-10-30)
BULK_RUB_MAX = 50000             # a markup or a markdown of one press in ₽ (a typo of a zero more is refused; «Применить» asks of the rest)
BULK_ROWS_SHOWN = 500            # rows of «было → стало» of one press listed (the risky ones first); the rest are counted


def _ids_fp(ids) -> str:
    """The positions a filter found, in short: «все по фильтру» takes them again only if the filter finds the very same ones."""
    import hashlib
    return hashlib.sha1("\n".join(sorted(ids)).encode("utf-8")).hexdigest()[:16]
PAGE_SIZE = 100


MAX_BRANDS = 50          # as many values of one parameter as page_prefs keeps: a remembered choice is the one shown


def _view_filters(args) -> dict:
    """The filters of the page's view beyond the size fields: whole sizes («205/55R16 225/45R18», the client catalog's reading), the brands
    shown and the brands left out. The view only: nothing here touches the feed."""
    sizes, unknown = client_catalog.parse_sizes((args.get("size") or "")[:client_catalog.MAX_TEXT])
    pick = lambda name: [b for b in dict.fromkeys(v.strip() for v in args.getlist(name)) if b][:MAX_BRANDS] if hasattr(args, "getlist") else []
    return {"sizes": sizes, "unknown": unknown, "brands": pick("b"), "without": pick("xb")}


def _view_only(args) -> dict:
    """The keywords of service.filter_offers among _view_filters."""
    return {k: v for k, v in _view_filters(args).items() if k != "unknown"}


def _pairs(args, drop=()) -> list:
    """The parameters of the address as (name, value) pairs, every value of a repeated one (the brands) kept, `drop` left out."""
    items = args.items(multi=True) if hasattr(args, "getlist") else args.items()
    return [(k, v) for k, v in items if k not in drop]


def _page_size(args) -> int:
    n = _num(args, "n")
    return n if n in PAGE_SIZES else PAGE_SIZE


def _gone_view(conn, raw, args, season, studs, live, ref=None) -> list:
    """The positions the supplier took away that fit the owner's exclusions and the size, season, studs and supplier filters of the page."""
    known = {o.goods_id: (o, info) for o, info in catalog_archive.gone_offers(conn) if o.goods_id not in live}      # the page may hold a fresher unload than the last cycle
    if not known:
        return []
    shown = service.filter_offers(service.hide_excluded(service._Filtered({g: o for g, (o, _) in known.items()}, {}), raw, conn, ref),
                                  _nums(args, "w"), _nums(args, "h"), _nums(args, "d"), season, studs, _num(args, "sup"), **_view_only(args))
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


def _locate(conn, settings, parsed, goods_id: str, base):
    """Where the card of a position is built from: (positions holding it, the day the supplier took it away or "") or None. Every position the
    catalog lists has a card and «Просмотр» (the owner 2026-10-29): the panel's set, 4tochki before its switch, an old second unload, the archive."""
    if goods_id in parsed.offers:
        return parsed, ""
    view, only = tochki_feed.catalog_view(conn, parsed, base)
    if goods_id in only:
        return view, ""
    for offer, _ in _held_offers(conn, settings, base):
        if offer.goods_id == goods_id:
            return ParseResult(offers={goods_id: offer}), ""
    offer = catalog_archive.gone_offer(conn, goods_id)
    if offer is not None:
        return ParseResult(offers={goods_id: offer}), (catalog_archive.item_info(conn, goods_id) or {}).get("gone_since") or ""
    return None


def orphan_rules(app, conn) -> list:
    """«Ручные правила без позиции» of «Настройки → Исключения» (the owner 2026-10-30; it was a block of the catalog): the manual rules
    (on/off, applied or in the draft) of positions the supplier lists no more, with what the archive knows of each — the last name, since when
    the supplier has it not — and who set it and when (the confirm of the settings that brought it in). A position of an old second unload
    held for viewing is still there: not listed."""
    from ..catalog_archive import gone_offer, item_info
    from ..suppliers import supplier_of_id                              # the key is always «1:<code>» (override_key): the code names its supplier
    _conn, settings, parsed, _ref = context(app)
    effective = settings_from_dict(staging.effective_raw(conn), enforce_pct_limit=False)
    held = {o.goods_id for o, _ in _held_offers(conn, settings, app.config["UNLOAD_PATH"])}
    suppliers = {str(s["id"]): s["name"] for s in connections.list_suppliers(conn)}
    confirms = [(at, who, json.loads(detail or "{}").get("changes", {}).get("overrides")) for at, who, detail in conn.execute(
        "SELECT at, author, detail FROM audit WHERE action = 'settings_confirm' ORDER BY id")]
    out = []
    for key, mode in sorted(effective.overrides.items()):
        supplier, _, goods_id = key.partition(":") if ":" in key else ("1", "", key)
        if goods_id in parsed.offers or goods_id in held:
            continue
        gone, info = gone_offer(conn, goods_id), item_info(conn, goods_id)
        set_at = set_by = ""
        before = None
        for at, who, rules in confirms:                                 # the confirm that brought the rule as it is now
            if not isinstance(rules, dict):
                continue
            if rules.get(key) == mode and before != mode:
                set_at, set_by = at, who
            before = rules.get(key)
        out.append({"goods_id": goods_id, "mode": mode, "name": (gone.name_orig or gone.name) if gone else "",
                    "supplier": suppliers.get(str(supplier_of_id(goods_id)), "поставщик %s" % supplier_of_id(goods_id)), "gone_since": (info or {}).get("gone_since") or "",
                    "set_at": set_at, "set_by": set_by, "pending": key not in (settings.overrides or {})})
    return out


def _held_view(conn, raw, args, season, studs, held, ref=None) -> list:
    """The held positions that fit the owner's exclusions, the filters of the page and its search (as the catalog's own positions)."""
    if not held:
        return []
    known = {o.goods_id: (o, at) for o, at in held}
    shown = service.filter_offers(service.hide_excluded(service._Filtered({g: o for g, (o, _) in known.items()}, {}), raw, conn, ref),
                                  _nums(args, "w"), _nums(args, "h"), _nums(args, "d"), season, studs, _num(args, "sup"), **_view_only(args))
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


def _market_cell(price, snapshot) -> dict:
    """The column «Рынок» of a row: «мин / медиана» and our place; empty without a snapshot."""
    if not snapshot:
        return {}
    at = market.moscow(snapshot["run_at"]) + (" (%s)" % snapshot["note"] if snapshot["note"] else "")
    if snapshot["status"] != "ok":
        return {"status": snapshot["status"], "text": "мало данных", "at": at}
    return {"status": "ok", "min": snapshot["min"], "median": snapshot["median"], "count": snapshot["count"], "at": at,
            "place": market.place(price, snapshot), "dearer": market.dearer(price, snapshot)}


def register(app) -> None:
    @app.route("/prices/purchase", methods=["GET"])
    @app.route("/prices/purchase/<int:alert_id>", methods=["GET"])
    @login_required
    def purchase_changes_page(alert_id=None):
        """«Закупка изменилась» (purchase_alert, the owner 2026-11-06): the whole list of one alert (the latest without a number); who may see
        the purchase only (the right «money»). Read-only."""
        from .. import purchase_alert
        if not has_right("money"):
            abort(403)
        conn = get_conn()
        alert = purchase_alert.get(conn, alert_id)
        if alert_id is not None and alert is None:
            abort(404)
        return render_template("purchase_changes.html", alert=alert, recent=purchase_alert.recent(conn), pct=purchase_alert.pct_text)

    @app.route("/prices/changes", methods=["GET"])
    @login_required
    def price_changes_page():
        """«Цена поставщика изменилась» (price_guard, the owner 2026-10-30): the falls of 5 % and more waiting, the owner's role only."""
        from .. import price_guard
        from .common import current_user
        if current_user()["role"] != "owner":
            abort(403)
        conn, settings, parsed, _ref = context(app)
        eff = connections.effective_settings(conn, connections.PRIMARY_ACCOUNT, settings)
        from ..pricing import position_price
        import dataclasses
        rows = []
        for r in price_guard.waiting(conn):
            offer = parsed.offers.get(r["goods_id"])
            was = position_price(dataclasses.replace(offer, base_price=float(r["base_price"])), eff) if offer is not None else None
            will = position_price(dataclasses.replace(offer, base_price=r["now"]), eff) if offer is not None else None
            rows.append(dict(r, name=offer.name if offer is not None else r["goods_id"], was=was, will=will))
        return render_template("price_changes.html", rows=rows, rev=price_guard.rev(conn), waits=price_guard.WAITS)

    @app.route("/prices/changes", methods=["POST"])
    @login_required
    def price_changes():
        """«Принять» / «Оставить прежнюю» for the positions ticked; a page older than a change does nothing."""
        from .. import price_guard
        from .common import current_user
        if current_user()["role"] != "owner":
            abort(403)
        conn = get_conn()
        picked = [g for g in request.form.getlist("goods") if g][:2000]
        action = request.form.get("action")
        if request.form.get("rev") != price_guard.rev(conn):
            flash("Список уже изменился: обновите страницу и отметьте снова.", "error")
        elif not picked or action not in ("accept", "keep"):
            flash("Отметьте позиции и выберите «Принять» или «Оставить прежнюю».", "error")
        elif action == "accept":
            flash("Принято: %d поз. Новая цена уйдёт на Авито %s." % (price_guard.accept(conn, picked, local_iso(), author()), price_guard.WAITS), "ok")
        else:
            flash("Оставлена прежняя цена: %d поз. Спросим снова, если поставщик снизит ещё на 5 %%." % price_guard.keep(conn, picked, local_iso(), author()),
                  "ok")
        return redirect("/prices/changes")

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
                                         _num(args, "sup"), **_view_only(args))
        held_all = _held_offers(conn, settings, app.config["UNLOAD_PATH"])                    # an old second unload: shown, never published
        held_ids = {o.goods_id for o, _ in held_all}
        gone = _gone_view(conn, raw, args, season, studs, set(parsed.offers) | held_ids, ref) if status in ("all", "gone") else []
        capped = tochki_feed.config(conn)["supplier_id"]
        groups = service.catalog_groups(
            conn, effective, filtered, ref, args.get("q", ""), "all" if status in ("nopair", "problem") else status, sort, descending, base_raw=raw,
            gone=gone, view_only=view_only, capped_supplier=capped, twins_from=parsed)
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
        own_only = args.get("own") == "1" and has_right("money")
        if own_only:                                                  # «Ручные цены» (the owner 2026-11-03): an own price in an account, applied or in the draft
            feed_ids = {a["id"] for a in connections.list_feed_accounts(conn)}      # an account without a feed: its own prices reach no Avito
            keyed = {k.partition(":")[2] for k in set(raw.get("price_overrides") or {}) | set({**raw, **changes}.get("price_overrides") or {})
                     if k.partition(":")[0].isdigit() and int(k.partition(":")[0]) in feed_ids}
            items = [i for i in items if i["goods_id"] in keyed]
        market_on = MARKET_SHOWN and has_right("money")               # the competitors' prices: the owner's, as the purchase (market.py)
        snaps = market.latest(conn) if market_on else {}
        market_over = market_on and args.get("mk") == "over"
        if market_over:                                               # «Дороже рынка»: our price above the median of enough data
            items = [i for i in items if market.dearer(i["price"], snaps.get(i["goods_id"]))]
        total = len(items)
        size = _page_size(args)
        pages = max(1, -(-total // size))
        page = min(max(_num(args, "p") or 1, 1), pages)
        shown = items[(page - 1) * size: page * size]
        if not has_right("money"):                                    # the supplier's price is a purchase price: not even in the HTML
            shown = [dict(i, supplier_price=None, purchase=None) for i in shown]
        accounts = connections.list_feed_accounts(conn)
        if accounts:                                                  # the cell «Наша цена»: the base own price, the first account's (base_own_prices)
            now_own, was_own = {**raw, **changes}.get("price_overrides") or {}, raw.get("price_overrides") or {}
            labels = {a["id"]: connections.display_name(conn, a["id"]) for a in accounts} if own_only else {}
            for i in shown:
                keys = ["%d:%s" % (a["id"], i["goods_id"]) for a in accounts]
                own = now_own.get(keys[0])
                i.update(own_here=own, own_text=service.own_price_text(own) if own else "", own_some=any(now_own.get(k) for k in keys),
                         own_pending=any(now_own.get(k) != was_own.get(k) for k in keys),
                         own_cleared=any(was_own.get(k) and not now_own.get(k) for k in keys),
                         own_where=[{"account": labels[a["id"]], "text": service.own_price_text(now_own.get(k)),
                                     "state": "" if now_own.get(k) == was_own.get(k) else "pending" if now_own.get(k) else "cleared"}
                                    for a, k in zip(accounts, keys) if now_own.get(k) or was_own.get(k)] if own_only else [])
                offer = parsed.offers.get(i["goods_id"])
                if i["own_some"] and offer is not None and i.get("purchase"):     # «Вернуть цену по настройкам фида»: what the first account gets back
                    i["brand_price"] = position_price(dataclasses.replace(offer, base_price=float(i["purchase"])),
                                                      dataclasses.replace(effective, own_prices={}))
            if len(accounts) > 1 and can_post("catalog_own_price_inline"):   # the cell's line «Шины СПб 6 990 · Шина-Колесо 7 130»
                shifts = {}
                for i in shown:
                    if i.get("brand", "") not in shifts:
                        shifts[i.get("brand", "")] = json.dumps([[connections.display_name(conn, a["id"]), shift]
                                                         for a, shift in service.account_shifts(conn, i.get("brand", ""), accounts)], ensure_ascii=False)
                    i["own_shifts"] = shifts[i.get("brand", "")]
        on_oth = order_oth.goods_on_oth(conn)                        # our own tyres of refused orders: a mark only, the feed is not touched
        shown = [dict(i, oth=on_oth.get(i["goods_id"])) for i in shown]
        if market_on:
            shown = [dict(i, market=_market_cell(i["price"], snaps.get(i["goods_id"]))) for i in shown]
        return dict(conn=conn, parsed=parsed, effective=effective, staged=staged, items=shown, held_ids=held_ids,
                    all_ids=[i["goods_id"] for i in items],                # every position of the filter, every page («все по фильтру»)
                    market_on=market_on, market_over=market_over, market_stopped=market_on and routes_market.stopped(conn),
                    page_size=size, total=total, brands=len({i["brand"] for i in items}), page=page, pages=pages, season=season or "",
                    studs=studs or "", status=status, sort=sort, descending=descending, own_only=own_only)

    def table_context(args, next_url=None):
        args = MultiDict(_pairs(args, ("wait",)))                        # the window «В лист ожидания» is no filter of the table
        b = build(args)
        base = _pairs(args, ("o", "r", "p"))
        links = {}
        for key in service.SORT_KEYS:
            flip = b["sort"] == key and not b["descending"]
            links[key] = "/catalog?" + urlencode(base + [("o", key)] + ([("r", "1")] if flip else []))
        keep = _pairs(args, ("p", "n"))
        size_links = {n: "/catalog?" + urlencode(keep + [("n", n)]) for n in PAGE_SIZES}
        keep = _pairs(args, ("p",))
        pager = {p: "/catalog?" + urlencode(keep + [("p", p)]) for p in range(1, b["pages"] + 1)} if b["pages"] > 1 else {}
        b.update(links=links, pager=pager, size_links=size_links, next_url=next_url or request.full_path.rstrip("?"), sup=_num(args, "sup"),
                 show_marks=_num(args, "sup") is None and len(connections.list_suppliers(b["conn"])) > 1,
                 table_url="/catalog/table?" + urlencode(_pairs(args)),
                 filter_query=urlencode(_pairs(args, ("p", "n", "o", "r"))),     # what «все по фильтру» takes again when pressed
                 feed_accounts=connections.list_feed_accounts(b["conn"]) if can_post("catalog_own_price_bulk") else [],
                 page_url=("/catalog?" + urlencode(_pairs(args))).rstrip("?"),     # where the cell «Наша цена» comes back to
                 bulk_limit=BULK_PRICE_LIMIT, bulk_pct_max=BULK_PCT_MAX, bulk_rub_max=BULK_RUB_MAX, filter_fp=_ids_fp(b["all_ids"]),
                 wait_url="/catalog?" + urlencode(_pairs(args) + [("wait", "")]))     # + the position: the page opens with its window
        return b

    def _item_path(goods_id):
        return "/catalog/item/" + quote(goods_id, safe="")

    def _wait_offer(conn, goods_id):
        """The position of the window «В лист ожидания»: today's unload (4tochki shown too), else a position the supplier took away."""
        _conn, _settings, parsed, _ref = context(app)
        parsed, _view_only = tochki_feed.catalog_view(conn, parsed, app.config["UNLOAD_PATH"])
        held = next((o for o, _ in _held_offers(conn, _settings, app.config["UNLOAD_PATH"]) if o.goods_id == goods_id), None) \
            if goods_id not in parsed.offers else None                # an old second unload: its card has the button too
        return parsed.offers.get(goods_id) or held or catalog_archive.gone_offer(conn, goods_id)

    def _wait_window(conn, goods_id, values: dict, error: str, back: str, offer=None):
        offer = offer or (_wait_offer(conn, goods_id) if goods_id else None)
        if offer is None:
            return None
        return {"goods_id": goods_id, "offer": offer, "line": order_wait.line_from_offer(offer), "typed": values, "error": error,
                "points": order_points.list_points(conn), "next": back, "seasons": order_wait.SEASON_NAMES, "studs": order_wait.STUD_NAMES}

    def catalog_page(args, wait=None, next_url=None):
        """The catalog; `wait`: the window «В лист ожидания» already made (a refused form comes back with what was typed)."""
        if "wait" in args and next_url is None:                           # the page without its window: where the window goes back to
            next_url = ("/catalog?" + urlencode(_pairs(args, ("wait",)))).rstrip("?")
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
            plain = _pairs(args, ("sup", "p", "wait"))
            # the tabs count what the filters find (п. 9 of the audit, 2026-11-08): «Все» = «Найдено» on the tab «Все», each supplier's tab =
            # «Найдено» on it; on a supplier's tab the filters are counted once more without the supplier
            every = b["all_ids"] if sup is None else build(MultiDict(_pairs(args, ("sup", "p", "wait"))))["all_ids"]
            counts = {}
            for gid in every:
                sid = service.supplier_of_id(gid)
                counts[sid] = counts.get(sid, 0) + 1
            supplier_tabs = [{"label": "Все", "on": sup is None, "url": "/catalog?" + urlencode(plain), "count": sum(counts.values())}] + [
                {"label": sp["name"], "on": sup == sp["id"], "url": "/catalog?" + urlencode(plain + [("sup", sp["id"])]),
                 "count": counts.get(sp["id"], 0)} for sp in suppliers]
        view = _view_filters(args)
        counts = {}
        for offer in b["parsed"].offers.values():                            # the brands on view (after the owner's exclusions), before the filters
            counts[offer.brand_key] = counts.get(offer.brand_key, 0) + 1
        names = {sp["id"]: sp["name"] for sp in suppliers}

        def label(key):                                                   # one brand at two suppliers: two rows, named apart
            match = re.match(r"^S(\d+):(.*)$", key)
            return match.group(2) + " · " + names.get(int(match.group(1)), match.group(1)) if match and len(names) > 1 else re.sub(r"^S\d+:", "", key)
        shown = dict(counts)
        for key in view["brands"] + view["without"]:                          # a brand chosen that has only gone or held positions: kept in the form
            shown.setdefault(key, 0)
        brand_choices = sorted(({"key": k, "label": label(k), "count": n} for k, n in shown.items()), key=lambda c: (c["label"].casefold(), c["key"]))
        return render_template(
            office_page("catalog.html"), size={k: " ".join(map(str, _nums(args, k) or ())) for k in ("w", "h", "d")}, q=args.get("q", ""),
            size_text=(args.get("size") or "")[:client_catalog.MAX_TEXT], size_unknown=view["unknown"], brand_choices=brand_choices, chosen_brands=view["brands"],
            without_brands=view["without"], feed_brands=_feed_brands(conn, counts, suppliers) if can_post("connections_excluded_brand") else None,
            size_options=service.size_options(b["parsed"]), supplier_tabs=supplier_tabs, wait=wait, **b)

    def _feed_brands(conn, counts, suppliers):
        """«Бренды в фиде» of the owner: every brand on view and every brand taken off, with what one button does to it. The change is the
        one of «Настройки → Исключения» (excluded_brands of the supplier): into the working draft, applied with «Применить» like the others."""
        active = set(get_active(conn)[1].get("excluded_brands") or [])
        pending = set(staging.effective_raw(conn).get("excluded_brands") or [])
        names = {sp["id"]: sp["name"] for sp in suppliers}
        rows = []
        for key in sorted(set(counts) | active | pending, key=lambda k: (re.sub(r"^S\d+:", "", k).casefold(), k)):
            match = re.match(r"^S(\d+):(.*)$", key)
            sid, bare = (int(match.group(1)), match.group(2)) if match else (1, key)
            if sid not in names:
                continue
            off = key in pending
            state = ("снят с фида" if key in active else "снятие не применено") if off else ("возврат не применён" if key in active else "")
            rows.append({"key": key, "bare": bare, "supplier": sid, "supplier_name": names[sid] if len(names) > 1 else "",
                         "count": counts.get(key, 0), "off": off, "state": state})
        return rows

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
            return catalog_page(MultiDict(parse_qsl(urlsplit(back).query)), wait=window, next_url=back)
        if made:
            flash("Клиент добавлен в лист ожидания: заказ №%d, этап «Ожидает наличие»." % card_id, "ok")
        else:
            flash("Клиент уже ждёт эту шину: заказ №%d. Второй заказ не создан%s." % (
                card_id, "; точку и комментарий из окна допишите в этом заказе" if (fields["point_id"] or fields["comment"].strip()) else ""))
        return redirect(back)

    @app.route("/catalog/table")
    @login_required
    def catalog_table():
        return render_template(office_page("_catalog_table.html"), **table_context(request.args))

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
                skipped = []
                if mode != "clear":                                       # a position the supplier took away (ticked for its own price) gets no
                    offers = context(app)[2].offers                       # manual rule: it would outlive its return (the owner 2026-11-03)
                    skipped = [g for g in dict.fromkeys(ids) if g not in offers and catalog_archive.gone_offer(conn, g) is not None]
                    ids = [g for g in ids if g not in skipped]
                if skipped and not ids:
                    raise ValueError("Выбраны только позиции, которых нет у поставщика: ручное правило им не ставится, ничего не записано.")
                staging.set_overrides(conn, ids, None if mode == "clear" else mode, now_iso())
                message = "%s позиций: %d." % ({"on": "Добавлено вручную", "off": "Снято", "clear": "Возвращено"}[mode], len(set(ids)))
                if skipped:
                    message += " Пропущено, нет у поставщика: %d." % len(skipped)
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
        found = _locate(conn, settings, parsed, goods_id, app.config["UNLOAD_PATH"])
        data = service.catalog_preview(conn, effective, found[0], ref, goods_id, now().date(), found[1]) if found else None
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
        found = _locate(conn, settings, parsed, goods_id, app.config["UNLOAD_PATH"])
        data = service.catalog_item_view(conn, effective, found[0], ref, goods_id, now().date(), has_right("money"), can("orders"),
                                         found[1]) if found else None
        if data is None:
            abort(404)
        gone = None if goods_id in parsed.offers else catalog_archive.gone_offer(conn, goods_id) if found[1] else None
        if has_right("money") and (goods_id in parsed.offers or gone is not None):   # the own prices: the owner and the partner see them, the owner
            general = settings_from_dict({**raw, **({} if staged["stale"] else staged["changes"])}, enforce_pct_limit=False)   # sets them: the panel's
            offer = parsed.offers.get(goods_id) or gone                                 # set and a position the supplier took away (its last purchase)
            data["own_prices"] = service.own_price_rows(conn, general, settings_from_dict(raw, enforce_pct_limit=False), offer, ref, parsed.offers)
            data["purchase"] = offer.base_price
            data["own_gone"] = gone is not None
        elif has_right("money"):                                      # shown for viewing only (4tochki before its switch, an old second unload):
            data["own_closed"] = True                                 # not in the unload prices are counted from — no own price, said why
            data["own_never"] = catalog_archive.item_info(conn, goods_id) is None
        if has_right("money") and catalog_archive.price_tracked(conn, goods_id):  # the history of the supplier's purchase: who may
            data["price_history"], data["price_history_since"] = catalog_archive.price_history(conn, goods_id)   # see the purchase; of every
                                                                      # supplier the cycle saw (4tochki shown for viewing only too)
        if MARKET_SHOWN and has_right("money"):                       # «Конкуренты»: the last snapshot of the competitors' prices (market.py)
            snapshot = market.latest(conn, [goods_id]).get(goods_id)
            data["market"] = {"snapshot": snapshot, "listings": market.listings_of(conn, snapshot["id"]) if snapshot else [],
                              "at": market.moscow(snapshot["run_at"]) if snapshot else ""}
        if wait is None and request.args.get("wait") and can_post("catalog_wait"):
            wait = _wait_window(conn, goods_id, {}, "", _item_path(goods_id), parsed.offers.get(goods_id))
        return render_template(office_page("catalog_item.html"), entity=data, wait=wait, wait_url=_item_path(goods_id) + "?wait=1")

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
            if value is not None:                                                     # the reset also for a position gone
                offers, _purchases, gone, _other = _bulk_positions(conn, [goods_id])     # the supplier took it away: a sale price always, a percent
                if goods_id not in offers:                                               # of its last purchase only (the owner 2026-11-03); never
                    _c, settings, parsed, _r = context(app)                              # a position shown for viewing only, nor one never in
                    if _locate(conn, settings, parsed, goods_id, app.config["UNLOAD_PATH"]) is not None:     # the unload (the owner 2026-11-05)
                        raise ValueError("Такой позиции нет в выгрузке, по которой считаются цены: она показана только для просмотра. "
                                         "Свою цену ей поставить нельзя.")
                    if catalog_archive.item_info(conn, goods_id) is not None:            # known, not marked gone yet (a collapse not believed,
                        raise ValueError("Позиции сейчас нет в выгрузке: свою цену можно будет поставить, когда она вернётся "   # the file
                                         "или каталог отметит её пропавшей у поставщика.")                                       # unusable)
                    raise ValueError("Такой позиции нет в выгрузке и не было в ней раньше: свою цену поставить нельзя.")
                if goods_id in gone and "pct" in value and not offers[goods_id].base_price > 0:
                    raise ValueError("Позиции нет у поставщика, и последняя закупка неизвестна: процент не от чего считать. "
                                     "Поставьте цену продажи в рублях.")
            staging.set_own_price(conn, goods_id, accounts, value, now_iso())
        except (StaleDraftError, ValueError) as err:
            flash(str(err))
            return redirect(_item_path(goods_id))
        flash(("Своя цена" if value else "Своя цена снята — цена по настройкам фида —") + " в черновике: подтвердите изменения, чтобы они попали в фид.")
        return redirect(_item_path(goods_id))

    @app.route("/catalog/own-price/inline", methods=["POST"])
    @login_required
    def catalog_own_price_inline():
        """The base own price from the cell «Наша цена» of the catalog (the owner 2026-10-30: «менять базовую цену»): one edit for every feed
        account, each with its correction over the first one's (service.base_own_prices), or «Сбросить» on all of them. The same working draft
        as the card (staging.set_own_price, applied with «Применить» and its confirmations); whom: as the card (common.NEEDS)."""
        conn = get_conn()
        form = request.form
        back = _back()
        goods_id = str(form.get("g") or "").strip()
        accounts = connections.list_feed_accounts(conn)
        try:
            if not goods_id or not accounts:
                raise ValueError("Не понял, какую позицию менять: обновите страницу.")
            offer = context(app)[2].offers.get(goods_id)
            if form.get("do") == "clear":                                 # «do», not «action»: that name hides form.action
                value, each = None, {}
            else:
                value = _own_value(form.get("value"))
                if offer is None:
                    raise ValueError("Такой позиции нет в выгрузке.")
                each = service.base_own_prices(conn, offer, value)
                for account in accounts:                                  # a shift past the bounds: said with the account and its value
                    try:
                        own_price_value(each[account["id"]])
                    except ValueError as err:
                        raise ValueError("%s: со своей поправкой выходит %s. %s" % (
                            connections.display_name(conn, account["id"]), service.own_price_text(each[account["id"]]), err))
            if value is None:
                staging.set_own_price(conn, goods_id, [a["id"] for a in accounts], None, now_iso())
            else:
                staging.set_own_price_each(conn, goods_id, each, now_iso())     # one change: all the accounts or none
        except (StaleDraftError, ValueError) as err:
            if _in_place():
                return jsonify(ok=False, message=str(err), **_state(conn))
            flash(str(err))
            return redirect(back)
        if value is None:
            message = "Своя цена снята на всех аккаунтах — цена по настройкам фида: %s." % _names(conn, [goods_id])[0]
        else:
            names = {a["id"]: connections.display_name(conn, a["id"]) for a in accounts}
            message = "Своя цена %s · %s (%s)." % (service.own_price_text(value), _names(conn, [goods_id])[0],
                                                  "; ".join("%s: %s" % (names[a], service.own_price_text(v)) for a, v in each.items()))
        message += " Пока это черновик: подтвердите изменения, чтобы они попали в фид."
        if _in_place():
            return jsonify(ok=True, message=message, **_state(conn))
        flash(message)
        return redirect(back)

    def _bulk_positions(conn, found):
        """The positions a press of «Своя цена выбранным» works on: the panel's set and those the supplier took away (the archive: the last
        purchase seen), never one shown for viewing only (4tochki before its switch, an old second unload). (offers, purchases — the anchor of
        price_guard the feed counts from —, the ids gone from the supplier, the ids skipped)."""
        from .. import price_guard
        _conn, settings, parsed, _ref = context(app)
        missing = [g for g in found if g not in parsed.offers]
        archive = {}
        if missing:
            base = app.config["UNLOAD_PATH"]
            shown_only = set(tochki_feed.catalog_view(conn, parsed, base)[1]) | {o.goods_id for o, _ in _held_offers(conn, settings, base)}
            archive = {g: catalog_archive.gone_offer(conn, g) for g in missing if g not in shown_only}
        offers = {g: parsed.offers.get(g) or archive.get(g) for g in found}
        other = [g for g, o in offers.items() if o is None]
        offers = {g: o for g, o in offers.items() if o is not None}
        guard = price_guard.load(conn)
        purchases = {g: price_guard.base_for(guard, g, float(o.base_price))[0] for g, o in offers.items()}
        return offers, purchases, {g for g in offers if g not in parsed.offers}, other

    def _bulk_op(form):
        """What «Своя цена выбранным» does: None takes the own prices off («Сбросить к настройкам фида»), else {"base", "unit", "delta"}
        (own_bulk.plan); no base nor unit sent is the press of before: % to the purchase."""
        if form.get("action") == "clear":
            return None
        base, unit, side = form.get("base") or "purchase", form.get("unit") or "pct", form.get("side")
        if base not in own_bulk.BASES or unit not in own_bulk.UNITS or side not in ("up", "down"):
            raise ValueError("Не понял, что сделать: обновите страницу и выберите ещё раз.")
        text = str(form.get("value") or "").replace(" ", "").replace(" ", "").replace(",", ".")
        if unit == "pct":
            if not re.fullmatch(r"\d+(\.\d+)?", text) or not 0 < float(text) <= BULK_PCT_MAX:
                raise ValueError("Наценка или уценка: процент %sбольше 0 и не больше %d." % ("к закупке " if base == "purchase" else "", BULK_PCT_MAX))
            delta = float(text)
        else:
            if not re.fullmatch(r"\d+", text) or not 0 < int(text) <= BULK_RUB_MAX:
                raise ValueError("Наценка или уценка в рублях: целое число больше 0 и не больше %d ₽." % BULK_RUB_MAX)
            delta = int(text)
        return {"base": base, "unit": unit, "delta": delta if side == "up" else -delta}

    def _op_text(op) -> str:
        if op is None:
            return "Своя цена снята — цена по настройкам фида"
        if op["base"] == "purchase" and op["unit"] == "pct":
            return "Своя цена %s" % service.own_price_text({"pct": op["delta"]})
        amount = ("%+g %%" % op["delta"]).replace(".", ",") if op["unit"] == "pct" else "%+d ₽" % op["delta"]
        return "Своя цена %s %s" % (amount.replace("-", "−"), "от закупки" if op["base"] == "purchase" else "от своей цены")

    @app.route("/catalog/own-price/bulk", methods=["POST"])
    @login_required
    def catalog_own_price_bulk():
        """«Своя цена выбранным» (the owner 2026-10-30, 2026-11-03): the positions ticked, or all the filter finds, in the accounts ticked —
        from the purchase or from their own price, in % or in ₽, up or down (own_bulk.plan), or «Сбросить к настройкам фида». The first press
        shows «было → стало» of every position and account; «В черновик» there writes just that (its mark `plan`: changed since — nothing
        written) into the working draft («Применить» with its confirmations). Whom: as the own price of a position (common.NEEDS). A position
        the supplier took away goes too (its last purchase); one shown for viewing only is skipped."""
        conn = get_conn()
        form = request.form
        back = _back()
        feed = {a["id"] for a in connections.list_feed_accounts(conn)}
        try:
            accounts = [int(a) for a in form.getlist("a") if str(a).isdigit()]
            if not accounts or any(a not in feed for a in accounts):
                raise ValueError("Отметьте аккаунты, где поставить цену.")
            if form.get("all") == "1":                                   # the filter again, as it stands: never more than was shown
                found = build(MultiDict(parse_qsl(form.get("filter", ""), keep_blank_values=True)))["all_ids"]
                if _ids_fp(found) != form.get("fp", ""):
                    raise ValueError("По фильтру сейчас другие позиции (%d), чем были на странице: обновите страницу и повторите." % len(found))
            else:
                found = form.getlist("g")
            offers, purchases, gone, other = _bulk_positions(conn, sorted({str(g) for g in found if str(g or "").strip()}))
            if not offers:
                raise ValueError("Среди выбранных нет позиций фида." if other else "Отметьте позиции.")
            if len(offers) > BULK_PRICE_LIMIT:
                raise ValueError("Не больше %d позиций за раз: сузьте фильтр." % BULK_PRICE_LIMIT)
            op = _bulk_op(form)
            redo = form.get("redo") == "1"
            if form.get("step") != "draft":
                if staging.summary(conn)["stale"]:
                    raise StaleDraftError("Рабочий черновик устарел: настройки изменились. Отмените его и повторите.")
                result = own_bulk.plan(staging.effective_raw(conn).get("price_overrides") or {}, purchases, accounts, op, redo)
                if result["set"]:
                    raw = staging.effective_raw(conn)
                    general = settings_from_dict(raw, enforce_pct_limit=False)
                    rows = own_bulk.rows(conn, general, offers, purchases, result["set"], gone, float(get_active(conn)[1]["pct_limit"]))
                    return render_template("catalog_own_bulk.html", rows=rows, shown=BULK_ROWS_SHOWN, result=result, op_text=_op_text(op),
                                           plan=own_bulk.fingerprint(result["set"]), positions=len({r["goods_id"] for r in rows}),
                                           gone=len(gone), other=len(other), skips=own_bulk.SKIPS, back=back, own_price_text=service.own_price_text,
                                           fields=[(k, v) for k, v in form.items(multi=True) if k not in ("csrf", "step", "plan")])
                done = result
            else:
                done = {}

                def planner(prices):
                    result = own_bulk.plan(prices, purchases, accounts, op, redo)
                    if own_bulk.fingerprint(result["set"]) != form.get("plan", ""):
                        raise staging.DraftChangedError("Цены изменились после просмотра «было → стало» (другая вкладка, другой человек или "
                                                        "новая выгрузка): ничего не записано, посмотрите ещё раз.")
                    done.update(result)
                    return result["set"]

                staging.set_own_price_plan(conn, planner, now_iso())
        except (StaleDraftError, ValueError) as err:
            flash(str(err))
            return redirect(back)
        parts = ["%s в черновике: %d (позиция × аккаунт)." % (_op_text(op), len(done["set"]))] if done["set"] else ["Ничего не изменилось."]
        if done.get("kept"):
            parts.append("Уже была своя цена, не тронуто: %d (позиция × аккаунт; «изменить и их» — чтобы поменять)." % len(done["kept"]))
        for why, keys in sorted((done.get("skipped") or {}).items()):
            parts.append("Пропущено, %s: %d (позиция × аккаунт)." % (own_bulk.SKIPS[why], len(keys)))
        if other:
            parts.append("Пропущено, не позиции фида (нет в выгрузке или только для просмотра): %d." % len(other))
        flash(" ".join(parts) + (" Подтвердите изменения, чтобы они попали в фид." if done["set"] else ""))
        return redirect(back)

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
