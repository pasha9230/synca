from __future__ import annotations

from datetime import datetime, timedelta
from urllib.parse import urlencode

from flask import flash, redirect, render_template, request

from .. import analytics, daily_summary, new_filter, order_alerts, staging, store, tochki_feed
from ..settings_store import active_settings, get_active
from .common import author, can, current_user, get_conn, has_right, login_required, now, now_iso
from .data import load_merged, load_reference
from . import service


def context(app):
    conn = get_conn()
    settings = active_settings(conn)
    parsed = load_merged(conn, settings, app.config["UNLOAD_PATH"])
    ref = load_reference(app.config["REFERENCE_PATH"])
    return conn, settings, parsed, ref


def catalog_scope(app, conn, parsed):
    """The settings and positions the catalog page shows with no filters (as `routes_catalog.build` makes them)."""
    from .routes_catalog import _view_settings
    _, raw = get_active(conn)
    staged = staging.summary(conn)
    effective = _view_settings(conn, raw, {} if staged["stale"] else staged["changes"])
    view, _ = tochki_feed.catalog_view(conn, parsed, app.config["UNLOAD_PATH"])
    return effective, service.hide_excluded(view, raw, conn, load_reference(app.config["REFERENCE_PATH"]))      # 4tochki: worked brands only


def register(app) -> None:
    def _feed_data(conn, settings, parsed, ref):
        return service.dashboard_data(conn, settings, parsed, ref, app.config["UNLOAD_PATH"], app.config.get("FEED_ROOT", ""),
                                      now().replace(tzinfo=None), catalog=catalog_scope(app, conn, parsed))

    @app.route("/")
    @login_required
    def index():
        """The day dashboard: the figures of the day (analytics.day_numbers), what needs attention (the feed, the orders, the chats, the
        alerts) and the person's own work; the state of the feeds is on «Фиды»."""
        conn, settings, parsed, ref = context(app)
        local = (now() + timedelta(hours=3)).replace(tzinfo=None)                  # Moscow, as the times of the base
        day = analytics.day_numbers(conn, local, with_money=has_right("money"), without_points=True)   # the staff's page: not the points' chats
        login = current_user()["login"]
        extra = []
        try:
            reds = order_alerts.counts(conn, login).get("red", 0)
        except Exception:                                                 # a broken counter never breaks the page (as the menu's nav_counts)
            reds = 0
        if reds and can("alerts"):
            extra.append({"level": "bad", "title": "Красные уведомления: %d" % reds, "detail": "Сбой или срок: кто-то должен разобраться.",
                          "href": "/alerts", "action": "Открыть"})
        if day["orders_overdue"] and can("orders"):
            extra.append({"level": "warn", "title": "Просроченные заказы: %d" % day["orders_overdue"], "detail": "Заказы дольше срока своего этапа.",
                          "href": "/orders", "action": "Открыть"})
        if day["waiting_long"] and can("chats"):
            extra.append({"level": "bad", "title": "Клиенты ждут ответа дольше %d минут: %d" % (analytics.WAIT_ALERT_MINUTES, len(day["waiting_long"])),
                          "detail": "Дольше всех — %d мин." % day["waiting_long"][0]["minutes"], "href": "/chats", "action": "Ответить"})
        return render_template("dashboard.html", d=_feed_data(conn, settings, parsed, ref), day=day, mine=analytics.my_day(conn, login, local),
                               extra=extra, summary_on=daily_summary.is_on(conn))

    @app.route("/feeds")
    @login_required
    def feeds_page():
        """The feeds on one page: the accounts, the last run, the readiness, what needs attention about them."""
        conn, settings, parsed, ref = context(app)
        d = _feed_data(conn, settings, parsed, ref)
        return render_template("feeds.html", d=d, feed_attention=d["attention"])

    @app.route("/summary/toggle", methods=["POST"])
    @login_required
    def summary_toggle():
        on = request.form.get("on") == "1"
        daily_summary.set_on(get_conn(), on, now_iso(), author())
        flash("Утренняя сводка включена: придёт после 9:00 МСК." if on else "Утренняя сводка выключена.")
        return redirect("/")

    @app.route("/new")
    @login_required
    def new_positions():
        conn, settings, parsed, _ref = context(app)
        run = store.last_run(conn)
        days = request.args.get("days", "")
        days = int(days) if days.isascii() and days.isdigit() and 0 < int(days) <= 3650 else None
        if days:
            cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat(timespec="seconds")
            after = store.run_before(conn, cutoff)
        else:
            after = store.get_new_marker(conn)
        report = store.new_since(conn, after) if run else None
        if report:
            report["goods"] = new_filter.visible(settings, report["goods"])                   # goods of brands that are off for publication are not shown
        for g in (report or {}).get("goods", ()):                                  # rows saved earlier hold the supplier's Russian spelling
            offer = parsed.offers.get(g["goods_id"])
            if offer is not None and offer.name_orig:
                g["name"] = offer.name_orig
        runs = conn.execute("SELECT COUNT(*) FROM run").fetchone()[0]
        return render_template("new_positions.html", run=run, report=report, days=days, only_baseline=runs <= 1)

    @app.route("/new/seen", methods=["POST"])
    @login_required
    def new_positions_seen():
        conn = get_conn()
        run = store.last_run(conn)
        if run:
            store.mark_new_seen(conn, run["id"])
            flash("Отмечено: новые позиции до этого запуска просмотрены.")
        return redirect("/new")
