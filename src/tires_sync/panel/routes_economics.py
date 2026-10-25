from __future__ import annotations

import re
from ..amounts import parse_amount
from datetime import date, timedelta
from urllib.parse import urlencode

import secrets

from flask import Response, abort, flash, redirect, render_template, request, session

from .. import connections, economics, sales as sales_store, staging, terms as terms_store
from ..settings_store import StaleDraftError, get_active
from .common import author, get_conn, local_today, login_required, now_iso, review_or_back, safe_path
from .routes_main import context
from . import economics_view, service

SEASON_FILTERS = ("winter", "summer", "all")
PAGE_SIZE = 100
PERIODS = ((7, "7 дней"), (30, "30 дней"), (90, "90 дней"), (365, "Год"))


def _int(value):
    """An integer from a query value, or None («²» and other look-alike digits are not numbers)."""
    value = str(value or "")
    return int(value) if value.isascii() and value.isdigit() else None


def _filters(args) -> dict:
    return {"season": args.get("s") if args.get("s") in SEASON_FILTERS else "", "diameter": _int(args.get("d")),
            "supplier": _int(args.get("sup"))}


def _effective(conn, settings):
    accounts = connections.list_feed_accounts(conn)
    return connections.effective_settings(conn, accounts[0]["id"], settings) if accounts else settings


def _is_day(value: str) -> bool:
    try:
        date.fromisoformat(value)
        return len(value) == 10
    except ValueError:
        return False


def _period(args, today):
    """(start, end, chosen days or 0 for custom): the last 30 days unless the owner picked something else."""
    start, end = args.get("from", ""), args.get("to", "")
    if start or end:
        return (start if _is_day(start) else ""), (end if _is_day(end) else ""), 0
    days = _int(args.get("p"))
    days = days if days in dict(PERIODS) else 30
    return (today - timedelta(days=days - 1)).isoformat(), today.isoformat(), days


def _amount(text: str) -> float:
    """«19 240», «19 240,50», «19240.5 ₽», «1.500» (a thousand, not one and a half): see amounts.parse_amount."""
    try:
        return parse_amount(text)
    except ValueError:
        raise ValueError("Выручка: нужно число, например 19240 или 19 240,50.")


def _names(conn):
    accounts = {a["id"]: connections.display_name(conn, a["id"]) for a in connections.list_accounts(conn)}
    points = []
    for account_id in accounts:
        for row in connections.list_addresses(conn, account_id):
            if row["name"] and row["name"] not in points:
                points.append(row["name"])
    return accounts, points


def register(app) -> None:
    def rows_for(args):
        conn, settings, parsed, ref = context(app)
        parsed = service.hide_excluded(parsed, get_active(conn)[1])
        day = local_today().isoformat()
        params = terms_store.get_params(conn)
        flt = _filters(args)
        return conn, economics_view.positions(conn, _effective(conn, settings), parsed, ref, day, params, flt), params, flt

    def keep(args, names=("s", "d", "sup")):
        return {k: v for k, v in args.items() if k in names and v}

    # ---- overview -------------------------------------------------------------------------------------------------------
    @app.route("/economics")
    @login_required
    def economics_page():
        conn, rows, params, flt = rows_for(request.args)
        brands = economics_view.by_brand(rows)
        pick = request.args.get("b", "")
        today = local_today()
        series = terms_store.snapshot_series(conn, pick, 30, today.isoformat())
        fact_rows = sales_store.list_sales(conn, (today - timedelta(days=29)).isoformat(), today.isoformat())
        accounts = connections.list_feed_accounts(conn)
        return render_template(
            "economics.html", total=economics_view.summarize(rows), brands=brands, params=params, series=series, flt=flt,
            sizes=sorted({r["diameter"] for r in rows}), pick=pick, query=urlencode(keep(request.args)),
            fact=sales_store.summarize(fact_rows), fact_by_brand={r["key"]: r for r in sales_store.breakdown(fact_rows, "brand")},
            chart_profit=economics_view.chart(series, "profit"), chart_ads=economics_view.chart(series, "ads"),
            chart_margin=economics_view.chart(series, "margin_pct"), has_terms=bool(terms_store.list_terms(conn)),
            first_account=connections.display_name(conn, accounts[0]["id"]) if accounts else "", many_accounts=len(accounts) > 1)

    # ---- one brand ------------------------------------------------------------------------------------------------------
    @app.route("/economics/brand")
    @login_required
    def economics_brand():
        brand = request.args.get("brand", "")
        conn, rows, params, flt = rows_for(request.args)
        mine = [r for r in rows if r["brand"] == brand]
        if not mine:
            abort(404)
        order = request.args.get("o", "profit")
        far = float("inf")
        keys = {"profit": lambda r: -(r["unit"]["margin"] * r["stock"]) if r["unit"] else far,
                "margin": lambda r: -(r["unit"]["margin"]) if r["unit"] else far,
                "pct": lambda r: -(r["unit"]["margin_pct_price"]) if r["unit"] else far,
                "stock": lambda r: -r["stock"], "name": lambda r: r["name"].lower()}
        order = order if order in keys else "profit"
        mine.sort(key=lambda r: (r["state"] != "feed", keys[order](r), r["name"].lower()))
        total_rows = len(mine)
        pages = max(1, -(-total_rows // PAGE_SIZE))
        page = min(max(_int(request.args.get("p")) or 1, 1), pages)
        today = local_today()
        fact_rows = sales_store.list_sales(conn, (today - timedelta(days=29)).isoformat(), today.isoformat(), brand)
        fact = {}
        for sale in fact_rows:
            item = fact.setdefault(sale["goods_id"], {"pieces": 0, "margin": 0.0})
            item["pieces"] += sale["pieces"]
            item["margin"] = round(item["margin"] + sale["margin_total"], 2)
        base = {k: v for k, v in request.args.items() if k not in ("p",) and v}
        pager = {n: "/economics/brand?" + urlencode(dict(base, p=n)) for n in range(1, pages + 1)} if pages > 1 else {}
        return render_template(
            "economics_brand.html", brand=brand, rows=mine[(page - 1) * PAGE_SIZE: page * PAGE_SIZE], total=economics_view.summarize(mine),
            potential=economics_view.if_enabled(mine), params=params, flt=flt, sizes=sorted({r["diameter"] for r in mine}), order=order,
            query=urlencode(keep(request.args)), fact=fact, fact_total=sales_store.summarize(fact_rows), pager=pager, page=page, pages=pages,
            total_rows=total_rows)

    # ---- supplier terms -------------------------------------------------------------------------------------------------
    @app.route("/economics/terms")
    @login_required
    def economics_terms():
        conn, settings, parsed, ref = context(app)
        keys = sorted({o.brand_key for o in parsed.offers.values()} | {t["brand_key"] for t in terms_store.list_terms(conn)})
        listing = terms_store.list_terms(conn)
        listing.sort(key=lambda t: (t["brand_key"], t["season"], t["size_class"], t["valid_from"]))
        for t in listing:
            t["total"] = round(t["markdown_pct"] + t["discount_pct"] + t["bonus_pct"] + t["monthly_pct"], 2)
        today = local_today().isoformat()
        editing = None
        edit_id = _int(request.args.get("edit"))
        if edit_id:
            editing = next((t for t in listing if t["id"] == edit_id), None)
        return render_template("economics_terms.html", terms=listing, brand_keys=keys, params=terms_store.get_params(conn), today=today,
                               editing=editing)

    def back_to_terms():
        return redirect("/economics/terms")

    @app.route("/economics/terms/save", methods=["POST"])
    @login_required
    def economics_terms_save():
        form = request.form
        try:
            terms_store.save_term(get_conn(), {k: form.get(k, "") for k in (
                "brand_key", "season", "size_class", "markdown_pct", "discount_pct", "bonus_pct", "monthly_pct", "valid_from")},
                                  now_iso(), author())
            flash("Условия сохранены.")
        except ValueError as err:
            flash(str(err))
        return back_to_terms()

    @app.route("/economics/terms/update", methods=["POST"])
    @login_required
    def economics_terms_update():
        form = request.form
        term_id = _int(form.get("id")) or 0
        try:
            terms_store.update_term(get_conn(), term_id, {k: form.get(k, "") for k in (
                "brand_key", "season", "size_class", "markdown_pct", "discount_pct", "bonus_pct", "monthly_pct", "valid_from")},
                                    now_iso(), author())
            flash("Условия изменены.")
        except ValueError as err:
            flash(str(err))
            return redirect("/economics/terms?edit=%d" % term_id if term_id else "/economics/terms")
        return back_to_terms()

    @app.route("/economics/terms/delete", methods=["POST"])
    @login_required
    def economics_terms_delete():
        try:
            terms_store.delete_term(get_conn(), _int(request.form.get("id")) or 0, now_iso(), author())
            flash("Условия удалены.")
        except ValueError as err:
            flash(str(err))
        return back_to_terms()

    @app.route("/economics/params", methods=["POST"])
    @login_required
    def economics_params():
        try:
            terms_store.set_params(get_conn(), request.form.get("fee_pct", ""), request.form.get("promo_pct", ""), now_iso(), author())
            flash("Расходы Авито сохранены.")
        except ValueError as err:
            flash(str(err))
        return back_to_terms()

    @app.route("/economics/terms/import", methods=["POST"])
    @login_required
    def economics_terms_import():
        upload = request.files.get("file")
        if upload is None or not upload.filename:
            flash("Выберите файл CSV.")
            return back_to_terms()
        raw = upload.read(1_000_001)
        if len(raw) > 1_000_000:
            flash("Файл слишком большой (не более 1 МБ).")
            return back_to_terms()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            flash("Сохраните файл как CSV UTF-8.")
            return back_to_terms()
        conn, settings, parsed, ref = context(app)
        keys = sorted({o.brand_key for o in parsed.offers.values()})
        try:
            done, errors = terms_store.import_csv(conn, text, request.form.get("valid_from", ""), keys, now_iso(), author())
        except ValueError as err:
            flash(str(err))
            return back_to_terms()
        message = "Загружено строк: %d (условия на эту дату заменены), ошибок: %d." % (done, len(errors)) if done else \
            "Ничего не загружено, ошибок: %d (прежние условия не тронуты)." % len(errors)
        if errors:
            message += " " + "; ".join(errors[:5])
        flash(message)
        return back_to_terms()

    @app.route("/economics/terms/apply-pricing", methods=["POST"])
    @login_required
    def economics_apply_pricing():
        conn = get_conn()
        day = request.form.get("day") or local_today().isoformat()
        if not _is_day(day):
            flash("Дата: нужен формат ГГГГ-ММ-ДД.")
            return back_to_terms()
        rules, missing = terms_store.pricing_from_terms(terms_store.list_terms(conn), day)
        if not rules:
            flash("Нечего применять: нужны уценки и для «до R17», и для «выше R17» (или «все радиусы») на эту дату.")
            return back_to_terms()
        try:
            staging.set_brand_state(conn, [(key.split("|")[0], key.split("|")[1], rule["pct_small"], rule["pct_large"], None)
                                           for key, rule in sorted(rules.items())], now_iso())
        except (ValueError, StaleDraftError) as err:
            flash(str(err))
            return back_to_terms()
        if missing:
            flash("Пропущено без полной пары размеров или ещё не действует: %s." % ", ".join(sorted(missing)))
        return review_or_back(conn, "/economics/terms")

    # ---- sales ----------------------------------------------------------------------------------------------------------
    def filtered(conn, args, reversed_too=False):
        start, end, days = _period(args, local_today())
        rows = sales_store.list_sales(conn, start, end, args.get("b", ""), _int(args.get("a")), args.get("pt", ""),
                                      include_reversed=reversed_too)
        return rows, start, end, days

    @app.route("/economics/sales")
    @login_required
    def economics_sales():
        conn = get_conn()
        show_reversed = request.args.get("rev") == "1"
        rows, start, end, days = filtered(conn, request.args, show_reversed)
        live = [r for r in rows if not r["reversed_at"]]
        gran = request.args.get("g") if request.args.get("g") in ("day", "week", "month") else "day"
        accounts, points = _names(conn)
        series = sales_store.series(live, gran)
        rest = {k: v for k, v in request.args.items() if k in ("b", "a", "pt", "g", "rev") and v}
        keep_query = {k: v for k, v in request.args.items() if k in ("from", "to", "p", "b", "a", "pt", "g", "rev") and v}
        brands = sorted({r["brand_key"] for r in sales_store.list_sales(conn)})
        season_name = {"winter": "зима", "summer": "лето", "all": "всесезонные"}
        breakdowns = [(title, head, [dict(r, label=label(r["key"])) for r in sales_store.breakdown(live, by)]) for title, head, by, label in (
            ("По сезону", "Сезон", "season", lambda k: season_name.get(k, k)),
            ("По радиусу", "Радиус", "radius", lambda k: k),
            ("По бренду", "Бренд", "brand", lambda k: re.sub(r"^S\d+:", "", k)),
            ("По аккаунту Авито", "Аккаунт", "account", lambda k: accounts.get(int(k), "—") if k.isdigit() else "не указан"),
            ("По точке", "Точка", "point", lambda k: k or "не указана"))]
        return render_template(
            "economics_sales.html", rows=rows, total=sales_store.summarize(live), series=series, gran=gran,
            chart_margin=economics_view.chart([{"day": p["key"], "m": p["margin"]} for p in series], "m"),
            chart_pieces=economics_view.chart([{"day": p["key"], "m": p["pieces"]} for p in series], "m"),
            breakdowns=breakdowns, accounts=accounts, points=points, brands=brands, start=start, end=end, days=days, periods=PERIODS,
            args=request.args, query=urlencode(keep_query), rest=urlencode(rest), show_reversed=show_reversed)

    @app.route("/economics/sales/export.csv")
    @login_required
    def economics_sales_export():
        conn = get_conn()
        rows, _, _, _ = filtered(conn, request.args)
        accounts, _ = _names(conn)
        return Response(sales_store.export_csv(rows, accounts), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=sales.csv"})

    def one_position(conn, settings, parsed, ref, offer):
        """The money of a single position, without building the whole catalog."""
        only = service._Filtered({offer.goods_id: offer}, getattr(parsed, "issues", {}))
        rows = economics_view.positions(conn, _effective(conn, settings), only, ref, local_today().isoformat(), terms_store.get_params(conn))
        return rows[0] if rows else None

    @app.route("/economics/sales/new")
    @login_required
    def economics_sale_new():
        conn, settings, parsed, ref = context(app)
        offer = parsed.offers.get(request.args.get("goods_id", ""))
        if offer is None:
            flash("Этой позиции уже нет в выгрузке поставщика: продажу записать нельзя. Запишите её, пока позиция есть в каталоге.")
            return redirect("/economics/sales")
        accounts, points = _names(conn)
        tokens = session.get("sale_tokens", [])
        token = secrets.token_hex(8)                                                  # one sale per opened form: a second press finds it spent
        session["sale_tokens"] = (tokens + [token])[-8:]
        return render_template("economics_sale_new.html", row=one_position(conn, settings, parsed, ref, offer), offer=offer,
                               accounts=accounts, points=points, today=local_today().isoformat(), sale_token=token)

    @app.route("/economics/sales/add", methods=["POST"])
    @login_required
    def economics_sale_add():
        conn, settings, parsed, ref = context(app)
        form = request.form
        token = form.get("sale_token", "")
        tokens = list(session.get("sale_tokens", []))
        if not token or token not in tokens:
            flash("Эта продажа уже записана или форма устарела: откройте форму заново.")
            return redirect("/economics/sales")
        tokens.remove(token)                                                          # spent before anything is written: a double press stops here
        session["sale_tokens"] = tokens
        offer = parsed.offers.get(form.get("goods_id", ""))
        if offer is None:
            flash("Этой позиции уже нет в выгрузке поставщика: продажу записать нельзя.")
            return redirect("/economics/sales")
        back = "/economics/sales/new?goods_id=%s" % offer.goods_id
        try:
            pieces = _int(form.get("pieces")) or 0
            total = _amount(form.get("sale_total", ""))
            price = _amount(form.get("supplier_price", "")) if form.get("supplier_price", "").strip() else float(offer.base_price)
            account = _int(form.get("account_id"))
            if account is not None and account not in {a["id"] for a in connections.list_accounts(conn)}:
                raise ValueError("Такого аккаунта Авито нет.")
            day = form.get("day", "") or local_today().isoformat()
            params = terms_store.get_params(conn)
            season = economics_view.season_of(offer)
            found = economics.pick_terms(terms_store.list_terms(conn), offer.brand_key, season, offer.diameter, day)
            sales_store.add_sale(
                conn, day=day, goods_id=offer.goods_id, brand_key=offer.brand_key, season=season, diameter=offer.diameter,
                name=offer.name_orig or offer.name, ad_kind=form.get("ad_kind", ""), pieces=pieces, sale_total=total,
                supplier_price=price, terms=found, fee_pct=params["fee_pct"], promo_pct=params["promo_pct"], account_id=account,
                point=form.get("point", ""), note=form.get("note", ""), now=now_iso(), author=author(), today=local_today().isoformat())
        except ValueError as err:
            flash(str(err))
            return redirect(back)
        flash("Продажа записана.")
        return redirect("/economics/sales")

    @app.route("/economics/sales/reverse", methods=["POST"])
    @login_required
    def economics_sale_reverse():
        try:
            sales_store.reverse_sale(get_conn(), _int(request.form.get("id")) or 0, now_iso(), author())
            flash("Продажа сторнирована: в итогах её больше нет, в журнале она осталась.")
        except ValueError as err:
            flash(str(err))
        return redirect(safe_path(request.form.get("next", "")) or "/economics/sales")
