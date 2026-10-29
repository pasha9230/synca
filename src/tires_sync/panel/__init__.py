from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from werkzeug.routing import IntegerConverter
from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

from ..secretbox import load_box
from .common import can, can_post, check_access, check_csrf, close_conn, csrf_token, current_user, has_right, is_agent_path

REQUIRED = ("SECRET_KEY", "PASSWORD_HASH", "DB_PATH", "UNLOAD_PATH", "REFERENCE_PATH")


def _chat_unread(conn) -> int:
    """The menu counter of «Чаты»: the whole panel's but the points' accounts (the owner 2026-10-26); for the manager of a point, the chats
    of its account only (none without one)."""
    from .. import chats as chats_store
    from .common import own_point, point_account
    if own_point() is None:
        return chats_store.unread_total(conn, without_points=True)
    account = point_account()
    if account is None:
        return 0
    return conn.execute("SELECT COUNT(*) FROM avito_chat c JOIN account a ON a.id = c.account_id WHERE a.chats_enabled = 1 AND c.unread > 0 "
                        "AND c.status = 'open' AND c.account_id = ?", (account,)).fetchone()[0]


class SafeInt(IntegerConverter):
    regex = r"\d{1,15}"


def create_app(config: dict) -> Flask:
    app = Flask(__name__)
    app.url_map.converters["int"] = SafeInt                              # a number in an address has at most 15 digits: more is a 404, not a database error
    app.config.update(config)
    missing = [key for key in REQUIRED if not app.config.get(key)]
    if missing:
        raise RuntimeError("missing panel config: %s" % ", ".join(missing))
    app.config.setdefault("NOW", datetime.utcnow)
    app.config.setdefault("MAX_CONTENT_LENGTH", 2 * 1024 * 1024)       # uploads are tables of a few hundred rows
    app.config.setdefault("AUTHOR", "owner")
    app.config.setdefault("MAX_LOGIN_FAILURES", 5)
    app.config.setdefault("LOGIN_WINDOW_SECONDS", 900)
    app.config.setdefault("PHOTOS_ROOT", Path(app.config["DB_PATH"]).resolve().parent / "photos")
    app.config.setdefault("SECRET_BOX", load_box(app.config.get("SECRET_KEY_FILE")))
    from .. import reliability                                      # the breaker of the outside services keeps its state in this base
    reliability.bind(app.config["DB_PATH"], reliability.owner_alert(lambda: app.config.get("SECRET_BOX")))
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
    from flask import request as _request
    from flask.sessions import SecureCookieSessionInterface

    class QuietPublicSessions(SecureCookieSessionInterface):
        """The public client page never sets or refreshes a cookie, whatever a template or a handler does with the session."""

        def save_session(self, app, session, response):
            if _request.path.startswith(("/s/", "/hook/")) or is_agent_path(_request.path):
                return None
            return super().save_session(app, session, response)

    app.session_interface = QuietPublicSessions()
    app.config.setdefault("PERMANENT_SESSION_LIFETIME", timedelta(hours=12))
    if int(app.config.get("TRUST_PROXY", 0) or 0):
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=int(app.config["TRUST_PROXY"]), x_proto=1)     # nginx says https: links are https
    app.jinja_env.globals["csrf_token"] = csrf_token
    app.jinja_env.globals.update(can=can, can_post=can_post, has_right=has_right, current_user=current_user)
    from .. import users as _roles
    app.jinja_env.filters["role_label"] = lambda key: _roles.ROLES.get(key, key)

    def account_label(account_id):
        from .common import get_conn
        from .. import connections
        return connections.display_name(get_conn(), account_id)

    app.jinja_env.globals["account_label"] = account_label
    app.jinja_env.globals["brand_label"] = lambda key: __import__("re").sub(r"^S\d+:", "", key or "")
    from .. import connections as _connections
    def _number(value):
        """A finite number, or None for an empty or broken value."""
        if value is None or isinstance(value, bool) or (isinstance(value, str) and not value.strip()):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if number == number and abs(number) != float("inf") else None

    def money(value):
        """12345.6 -> «12 346 ₽» with no-break spaces; «—» on empty."""
        number = _number(value)
        if number is None:
            return "—"
        return ("−" if round(number) < 0 else "") + "{:,.0f}".format(abs(number)).replace(",", "\u00a0") + "\u00a0₽"

    def money_delta(value):
        """A change of money: «+1 200 ₽», «−1 200 ₽» (a real minus), «0 ₽»; «—» on empty."""
        number = _number(value)
        if number is None:
            return "—"
        sign = "+" if round(number) > 0 else ("−" if round(number) < 0 else "")
        return sign + "{:,.0f}".format(abs(number)).replace(",", "\u00a0") + "\u00a0₽"

    def pct(value, digits=1):
        """12.345 -> «12,3» (the sign % stays in the template); «—» on empty."""
        number = _number(value)
        if number is None:
            return "—"
        text = "%.*f" % (digits, number)
        return (text.lstrip("-") if float(text) == 0 else text).replace(".", ",").replace("-", "−")         # no «−0,0»

    def phone(value):
        """+7 900 111-22-33 for a Russian number; the text as it is otherwise; «—» on empty."""
        text = "" if value is None else str(value).strip()
        return _connections.format_phone(text) if text else "—"

    # Dates: everything is shown in Moscow time by one rule (docs/design.md §7). The base keeps two clocks: Moscow (orders, chats, the
    # points, the life of positions: local_iso / moscow_now / store.moscow_iso / Avito's stamps) and UTC (the panel's now_iso, the cycle,
    # the commands' cli._now, the connections; never the server's own zone): a UTC field is shown
    # through the «_utc» filters, which add three hours. Tables: «20.10 12:00» (the year when not this one); journals and hints:
    # «20.10.2026 12:00:05»; a date alone: «20.10.2026» / «20.10». Empty: «—».
    def _stamp(value):
        text = "" if value is None else str(value).strip()
        for size, pattern in ((19, "%Y-%m-%dT%H:%M:%S"), (16, "%Y-%m-%dT%H:%M"), (10, "%Y-%m-%d")):
            try:
                return datetime.strptime(text[:size].replace(" ", "T"), pattern), size
            except ValueError:
                continue
        return None, 0

    def _today():
        from .common import now
        return (now() + timedelta(hours=3)).date()                       # the panel clock is UTC, the stamps are shown in Moscow time

    def _shown(value, shift, full):
        at, size = _stamp(value)
        if at is None:
            return "—" if value is None or not str(value).strip() else str(value)
        if size == 10:                                                   # a date with no time is a calendar day: never shifted
            return at.strftime("%d.%m.%Y" if full or at.year != _today().year else "%d.%m")
        at += timedelta(hours=shift)
        if full:
            return at.strftime("%d.%m.%Y %H:%M:%S" if size == 19 else "%d.%m.%Y %H:%M")
        return at.strftime("%d.%m %H:%M" if at.year == _today().year else "%d.%m.%Y %H:%M")

    def dt(value):
        """A Moscow stamp in full: 2026-10-20T12:00:05 -> 20.10.2026 12:00:05."""
        return _shown(value, 0, True)

    def dt_utc(value):
        """A UTC stamp in full, in Moscow time: 2026-10-20T21:30:00 -> 21.10.2026 00:30:00."""
        return _shown(value, 3, True)

    def short_dt(value):
        """A Moscow stamp in a table: 20.10 12:00 (the year only when it is not this one)."""
        return _shown(value, 0, False)

    def short_dt_utc(value):
        """A UTC stamp in a table, in Moscow time."""
        return _shown(value, 3, False)

    def day(value):
        """A calendar day: 2026-10-20 -> 20.10 (the year only when it is not this one); «—» on empty."""
        at, _size = _stamp(value)
        if at is None:
            return "—" if value is None or not str(value).strip() else str(value)
        return at.strftime("%d.%m" if at.year == _today().year else "%d.%m.%Y")

    MOSCOW_AUDIT = frozenset({"order_rate", "order_delete", "order_restore", "chat_delete", "chat_restore", "chat_assign", "orders_bulk_move",
                              "orders_bulk_undo", "orders_cleanup_new", "mirror_adopt", "mirror_reissue", "mirror_reissue_own",
                              "mirror_unreissue_own", "ids_rebound", "id_scheme", "clients_linked"})

    def audit_dt(entry):
        """The time of a record of the journal of actions in Moscow time. The journal is written by both clocks (the panel's own actions
        in UTC; the orders, the chats and the identifier commands in Moscow time): which one is told by the action and its author.
        A sale recorded from an order is written in Moscow time but cannot be told from a sale typed by hand: both are shown as UTC."""
        action, author = entry.get("action", ""), str(entry.get("author") or "")
        moscow = (action in MOSCOW_AUDIT or (action == "ids_bound" and author.startswith("ids-"))
                  or (action == "account_live" and author == "mirror-go-live"))
        return dt(entry.get("at")) if moscow else dt_utc(entry.get("at"))

    months = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря")

    def hm(value):
        """A Moscow stamp: 2026-10-07T15:46:50 -> 15:46"""
        at, size = _stamp(value)
        return at.strftime("%H:%M") if at and size > 10 else ""

    def day_label(value):
        """The day over the messages of a chat: Сегодня, Вчера, 7 октября (2025)."""
        at, _size = _stamp(value)
        if at is None:
            return ""
        today = _today()
        if at.date() == today:
            return "Сегодня"
        if at.date() == today - timedelta(days=1):
            return "Вчера"
        return "%d %s%s" % (at.day, months[at.month - 1], "" if at.year == today.year else " %d" % at.year)

    app.jinja_env.filters.update(money=money, money_delta=money_delta, phone=phone)
    app.jinja_env.filters.update(dt=dt, dt_utc=dt_utc, short_dt=short_dt, short_dt_utc=short_dt_utc, day=day, hm=hm, day_label=day_label)
    app.jinja_env.globals["audit_dt"] = audit_dt

    def chat_client(chat_row, order_id=None):
        """The client card of a chat (5A): by the chat, else by its order; None when there is none or the base cannot say."""
        from .common import get_conn
        from .. import clients
        try:
            conn = get_conn()
            return clients.client_of_chat(conn, chat_row) or (clients.client_of_order(conn, order_id) if order_id else None)
        except Exception:
            return None

    app.jinja_env.globals["chat_client"] = chat_client
    from . import texts
    app.jinja_env.filters["pct"] = pct
    app.jinja_env.globals.update(run_text=texts.run_text, audit_text=texts.audit_text, author_label=texts.author_label, draft_status=texts.draft_status)
    def _visible_new_count(conn) -> int:
        """The menu counter of new positions: only goods of brands that are on for publication."""
        from .. import new_filter, store
        from ..settings_store import active_settings
        try:
            goods = store.new_since(conn, store.get_new_marker(conn))["goods"]
            return len(new_filter.visible(active_settings(conn), goods))
        except Exception:
            return store.count_new_since(conn, store.get_new_marker(conn))

    class LazyNav:
        """Menu counters are computed on first use, so fragments rendered without the menu cost nothing."""

        def __init__(self):
            self._data = None

        def _load(self):
            if self._data is None:
                from .common import get_conn
                from .. import chats as chats_store, staging, store
                conn = get_conn()
                staged = staging.summary(conn)
                pairs = store.get_state(conn, "nav_pending_pairs")
                if pairs is None:
                    from .routes_main import context
                    from . import service
                    try:
                        _, settings, parsed, ref = context(app)
                        pairs = str(service.unmatched_models(conn, parsed, ref)[0])
                        store.set_state(conn, "nav_pending_pairs", pairs)
                    except Exception:
                        pairs = "0"
                from .service import draft_rev
                self._data = {
                    "staged": staged["count"] if not staged["stale"] else 0, "stale": staged["stale"],
                    "draft_id": staged["draft_id"], "rev": draft_rev(staged["changes"]),
                    "new_count": _visible_new_count(conn),
                    "pending_pairs": int(pairs) if pairs.isdigit() else 0,
                    "chat_unread": _chat_unread(conn)}
                from flask import session
                from .routes_alerts import nav_counts
                self._data.update(nav_counts(conn, session["user"]))
            return self._data

        def __getattr__(self, name):
            return self._load().get(name)

    @app.context_processor
    def navigation():
        from flask import session
        if not session.get("user"):
            return {"nav": {}}
        from .page_prefs import state
        return {"nav": LazyNav(), "page_filter": state}

    from .. import users as _users
    _users.warm()
    app.teardown_appcontext(close_conn)
    app.before_request(check_csrf)
    app.before_request(check_access)                                   # after the CSRF check: one right per page (common.SECTION_OF)
    from . import page_prefs
    app.before_request(page_prefs.remember)                            # after the right: the person's own filters of a list page

    @app.after_request
    def security_headers(response):
        from flask import request
        if request.path.startswith("/s/"):
            # the public client page: no cookies, no indexing, no referrer, nothing but our own pictures and styles
            response.headers.pop("Set-Cookie", None)
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
            response.headers["Cache-Control"] = "private, no-store"
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; img-src 'self'; style-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
            return response
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; "
            "frame-ancestors 'none'")
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if request.endpoint in ("shop_page", "shop_photo"):
            # the client's catalog: no scripts, no picture from outside (a supplier's host is never asked by the client's browser), not kept
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; img-src 'self'; style-src 'self' 'unsafe-inline'; form-action 'self'; base-uri 'none'; "
                "frame-ancestors 'none'")
            response.headers.setdefault("Cache-Control", "private, no-store")
        return response

    from . import routes, routes_addresses, routes_alerts, routes_catalog, routes_connections, routes_description, routes_drafts, routes_economics, routes_feedupdate, routes_history, routes_funnel, routes_chats, routes_health, routes_img, routes_main, routes_mappings, routes_mypoint, routes_order_bulk, routes_order_cleanup, routes_orders, routes_pairs, routes_agent, routes_photos, routes_points, routes_share, routes_users, routes_vk, routes_clients, routes_analytics, routes_avito_webhook, routes_search, routes_calls, routes_sheet, routes_inquiry, routes_shop, routes_client_terms

    routes.register(app)
    routes_main.register(app)
    routes_mappings.register(app)
    routes_connections.register(app)
    routes_description.register(app)
    routes_addresses.register(app)
    routes_catalog.register(app)
    routes_pairs.register(app)
    routes_drafts.register(app)
    routes_feedupdate.register(app)
    routes_history.register(app)
    routes_economics.register(app)
    routes_funnel.register(app)
    routes_share.register(app)
    routes_vk.register(app)
    routes_points.register(app)
    routes_orders.register(app)
    routes_order_cleanup.register(app)
    routes_order_bulk.register(app)
    routes_chats.register(app)
    routes_alerts.register(app)
    routes_img.register(app)
    routes_photos.register(app)
    routes_agent.register(app)
    routes_health.register(app)
    routes_users.register(app)
    routes_clients.register(app)
    routes_calls.register(app)
    routes_analytics.register(app)
    routes_mypoint.register(app)
    routes_avito_webhook.register(app)
    routes_search.register(app)
    routes_sheet.register(app)
    routes_inquiry.register(app)
    routes_shop.register(app)
    routes_client_terms.register(app)

    from flask import render_template

    from ..settings_store import SettingsNotInitialized

    @app.errorhandler(OverflowError)
    def too_big_number(_err):                                            # a number typed or sent that no database column can hold
        return "Слишком большое число в запросе.", 400

    @app.errorhandler(413)
    def too_big_body(_err):                                              # a file over MAX_CONTENT_LENGTH: a plain page, not the bare error of Flask
        return render_template("message.html", text="Файл слишком большой: вернитесь назад и выберите файл поменьше."), 413

    def _forget_stale_session():                                         # a page of an error never draws the menu of a session that is over
        from flask import session
        from .common import session_valid
        if session.get("user") and not session_valid():
            session.clear()

    @app.errorhandler(403)
    def forbidden(_err):
        _forget_stale_session()
        return render_template("error.html", nav={}, code=403, title="Нет доступа",
                               text="У вас нет прав на эту страницу или действие. Если они нужны, спросите владельца."), 403

    @app.errorhandler(404)
    def not_found(_err):
        _forget_stale_session()
        return render_template("error.html", nav={}, code=404, title="Страница не найдена", text="Такой страницы нет: возможно, её удалили или ссылка неточная."), 404

    @app.errorhandler(500)
    def server_error(_err):
        text = "Что-то пошло не так. Попробуйте ещё раз; если повторится, напишите владельцу."
        try:
            return render_template("error.html", nav={}, code=500, title="Ошибка на сервере", text=text), 500
        except Exception:                                                # the page itself failed (the database, the menu): plain text
            return text, 500

    @app.errorhandler(SettingsNotInitialized)
    def not_initialized(error):
        return render_template("message.html", text=str(error)), 503

    return app
