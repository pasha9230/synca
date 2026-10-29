from __future__ import annotations

import re
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET
from pathlib import Path

from flask import abort, flash, redirect, render_template, request

from .. import avito_stock, connections, feed as feed_module, order_notify, orders, users as panel_users
from ..avito import AvitoAuthError, AvitoClient, AvitoError
from ..secretbox import SecretKeyError
from ..settings_store import StaleDraftError, active_settings, get_active
from .. import staging
from .service import supplier_catalog as service_catalog
from . import service
from ..publish import count_ads
from ..notify import NotifyError, send_message
from ..transport import urllib_transport
from ..webmim_client import WebmimClient, WebmimError
from .. import reliability, share_photos, tochki_feed, webmim_orders
from ..tochki_client import TochkiClient, TochkiError
from .routes_addresses import build_form
from .common import author, get_conn, login_required, now_iso, review_or_back, safe_path

WEBMIM_DEFAULTS = {"base_url": "https://mimtest.svrauto.ru", "mode": "login", "login": ""}


def public_address(url: str) -> bool:
    """The address of WebMIM leads to a public server only: https, a plain host name, every address of it outside our network (as the photos)."""
    try:
        share_photos.resolve_public(url)
    except (share_photos.PhotoError, ValueError):            # ValueError: a broken port («:abc», «:99999»)
        return False
    return True
NO_KEY = "Мастер-ключ недоступен: секреты не сохраняются и не читаются."
TOO_SOON = "Проверку можно запускать не чаще раза в 10 секунд."


def _save_secrets(app, kind, owner_id, pairs) -> None:
    conn = get_conn()
    for name, value in pairs:
        if not value or not value.strip():
            continue
        try:
            connections.set_secret(
                conn, app.config.get("SECRET_BOX"), kind, owner_id, name, value, now_iso(), author())
        except SecretKeyError:
            flash(NO_KEY)
            return
        except ValueError as err:
            flash(str(err))


def _tab_label(conn, account_id: int) -> str:
    label = connections.display_name(conn, account_id)
    name = connections.get_avito_identity(conn, account_id)["name"]
    return "%s · %s" % (label, name) if name else label


def register(app) -> None:
    def tabs(conn, active, account_id=None, supplier_id=None):
        tochki = tochki_feed.config(conn)["supplier_id"]                 # the row of 4 точки has its own tab (the owner 2026-10-21)
        return {"active": active, "account_id": account_id, "supplier_id": supplier_id, "suppliers": [
            {"id": sp["id"], "label": sp["name"], "url": "/connections/webmim/%d" % sp["id"]}
            for sp in connections.list_suppliers(conn) if sp["id"] != tochki], "accounts": [
            {"id": a["id"], "label": _tab_label(conn, a["id"]), "url": "/connections/avito/%d" % a["id"]}
            for a in connections.list_accounts(conn)], "services": reliability.status(conn)}

    @app.route("/connections")
    @login_required
    def connections_page():
        return redirect("/connections/webmim")

    def _supplier_or_404(supplier_id: int) -> dict:
        supplier = connections.get_supplier(get_conn(), supplier_id)
        if supplier is None:
            abort(404)
        return supplier

    def supplier_catalog(conn, supplier_id):
        return service_catalog(conn, supplier_id, app.config["UNLOAD_PATH"])

    def _split(key):
        match = re.match(r"S(\d+):(.*)", key)
        return (int(match.group(1)), match.group(2)) if match else (1, key)

    @app.route("/connections/suppliers/<int:supplier_id>/excluded", methods=["POST"])
    @login_required
    def connections_excluded_brand(supplier_id):
        conn = get_conn()
        _supplier_or_404(supplier_id)
        back = "/settings/exclusions#excluded-%d" % supplier_id
        asked = safe_path(request.form.get("next", ""))
        if urlsplit(asked).path == "/catalog":                     # «Бренды в фиде» of the catalog: the same change, back to the catalog
            back = asked
        try:
            if request.form.get("remove"):
                sid, brand = _split(request.form["remove"])
                staging.set_brand_excluded(conn, brand, False, now_iso(), supplier=sid)
            else:
                from ..webmim import brand_key
                typed = request.form.get("brand", "")
                brands, _ = supplier_catalog(conn, supplier_id)
                key = ("S%d:" % supplier_id if supplier_id != 1 else "") + brand_key(typed)
                if key not in brands:
                    raise ValueError("У этого поставщика нет бренда «%s»: выберите из списка." % typed.strip())
                staging.set_brand_excluded(conn, typed, True, now_iso(), supplier=supplier_id)
        except (ValueError, StaleDraftError) as err:
            flash(str(err))
            return redirect(back)
        return review_or_back(conn, back)

    @app.route("/connections/suppliers/<int:supplier_id>/excluded-bulk", methods=["POST"])
    @login_required
    def connections_excluded_brands_bulk(supplier_id):
        conn = get_conn()
        _supplier_or_404(supplier_id)
        back = "/settings/exclusions#excluded-%d" % supplier_id
        try:
            ticked, listed = set(request.form.getlist("excluded")), set(request.form.getlist("listed"))
            prefix = "S%d:" % supplier_id if supplier_id != 1 else ""
            mine = lambda key: key.startswith(prefix) if prefix else not (key.startswith("S") and ":" in key.split("|")[0])
            brands, _ = supplier_catalog(conn, supplier_id)
            staged = staging.summary(conn)
            raw = get_active(conn)[1]
            effective = set((raw.get("excluded_brands") if staged["stale"] else staged["changes"].get("excluded_brands", raw.get("excluded_brands"))) or [])
            current = {b[len(prefix):] for b in effective if mine(b)}
            known = {b[len(prefix):] for b in brands if mine(b)} | current
            unknown = sorted((ticked | listed) - known)
            if unknown:
                raise ValueError("У этого поставщика нет бренда «%s»: обновите страницу и выберите из списка." % unknown[0])
            staging.set_brands_excluded(conn, sorted(ticked - current), sorted((listed - ticked) & current), now_iso(), supplier=supplier_id)
        except (ValueError, StaleDraftError) as err:
            flash(str(err))
            return redirect(back)
        return review_or_back(conn, back)

    @app.route("/connections/suppliers/<int:supplier_id>/excluded-sizes", methods=["POST"])
    @login_required
    def connections_excluded_size(supplier_id):
        conn = get_conn()
        _supplier_or_404(supplier_id)
        back = "/settings/exclusions#excluded-sizes-%d" % supplier_id
        try:
            if request.form.get("remove"):
                rule = request.form["remove"]
                sid, rest = _split(rule)
                brand, _, diameter = rest.rpartition("|")
                staging.set_size_excluded(conn, None if brand == "*" else brand, int(diameter), False, now_iso(), supplier=sid)
            else:
                from ..webmim import brand_key
                brand = request.form.get("brand", "").strip()
                raw_d = request.form.get("diameter", "")
                diameter = int(raw_d) if raw_d.isascii() and raw_d.isdigit() else None
                brands, diameters = supplier_catalog(conn, supplier_id)
                if diameter is None or diameter not in diameters:
                    raise ValueError("У этого поставщика нет радиуса «%s»: выберите из списка." % raw_d)
                if brand and ("S%d:" % supplier_id if supplier_id != 1 else "") + brand_key(brand) not in brands:
                    raise ValueError("У этого поставщика нет бренда «%s»: выберите из списка." % brand)
                staging.set_size_excluded(conn, brand or None, diameter, True, now_iso(), supplier=supplier_id)
        except (ValueError, StaleDraftError) as err:
            flash(str(err))
            return redirect(back)
        return review_or_back(conn, back)

    @app.route("/connections/webmim")
    @app.route("/connections/webmim/<int:supplier_id>")
    @login_required
    def connections_webmim_page(supplier_id=1):
        conn = get_conn()
        supplier = _supplier_or_404(supplier_id)
        if supplier_id == tochki_feed.config(conn)["supplier_id"]:       # the owner 2026-10-21: 4 точки are not in the WebMIM section
            return redirect("/connections/tochki")
        owner = connections.supplier_owner(supplier_id)
        box = app.config.get("SECRET_BOX")
        return render_template(
            "conn_webmim.html", tabs=tabs(conn, "webmim", supplier_id=supplier_id), box_ok=box is not None,
            supplier=supplier,
            webmim=dict(WEBMIM_DEFAULTS, **connections.get_config(conn, "webmim", owner)),
            webmim_secrets=connections.secret_status(conn, box, "webmim", owner),
            webmim_check=connections.get_check(conn, "webmim", owner),
            orders_check=connections.get_check(conn, "webmim_orders", owner),
            orders_cfg=dict({"server": webmim_orders.DEFAULT_SERVER, "login": "", "auth": "login"}, **connections.get_config(conn, "webmim_orders", owner)),
            orders_secrets=connections.secret_status(conn, box, "webmim_orders", owner),
            orders_servers=[(key, webmim_orders.SERVER_NAMES[key]) for key in webmim_orders.SERVERS])

    @app.route("/connections/tochki")
    @login_required
    def connections_tochki_page():
        conn = get_conn()
        box = app.config.get("SECRET_BOX")
        return render_template(
            "conn_tochki.html", tabs=tabs(conn, "tochki"), box_ok=box is not None,
            tochki=dict({"login": ""}, **connections.get_config(conn, "tochki", 0)),
            tochki_cfg=tochki_feed.config(conn), tochki_choices=connections.tochki_supplier_choices(conn),
            tochki_load=tochki_feed.read_state(app.config["UNLOAD_PATH"]),
            tochki_secrets=connections.secret_status(conn, box, "tochki", 0),
            tochki_check=connections.get_check(conn, "tochki", 0))

    def save_tochki() -> None:
        conn = get_conn()
        raw = request.form.get("supplier_id", "").strip()
        try:
            connections.set_tochki_config(conn, request.form.get("login", "").strip()[:100], int(raw) if raw.isdigit() else None,
                                          now_iso(), author())
        except ValueError as err:
            flash(str(err))
            return                                         # nothing of the form is kept, the password neither
        _save_secrets(app, "tochki", 0, [("password", request.form.get("password", ""))])

    @app.route("/connections/tochki", methods=["POST"])
    @login_required
    def connections_tochki():
        save_tochki()
        flash("Настройки 4 точек сохранены.")
        return redirect("/connections/tochki")

    @app.route("/connections/tochki/check", methods=["POST"])
    @login_required
    def connections_tochki_check():
        """Do the login and password open the API? Ping and the warehouse list: reads only."""
        conn = get_conn()
        back = "/connections/tochki"
        box = app.config.get("SECRET_BOX")
        if box is None:
            flash(NO_KEY)
            return redirect(back)
        if request.form.get("login") is not None:
            save_tochki()
        if not connections.check_allowed(conn, "tochki", 0, now_iso()):
            flash(TOO_SOON)
            return redirect(back)
        config = connections.get_config(conn, "tochki", 0)
        connections.record_check(conn, "tochki", 0, False, "проверка выполняется…", now_iso())
        try:
            password = connections.get_secret(conn, box, "tochki", 0, "password") or ""
            client = TochkiClient(config.get("login", ""), password, http=app.config.get("TOCHKI_HTTP") or urllib_transport)
            with reliability.probe():                      # the owner's check goes even while the service rests, and its result counts
                message = client.check()
            try:
                (Path(app.config["DB_PATH"]).parent / "tochki.sample.xml").write_text(client.sample(), encoding="utf-8")
                message += "; образец ответов сохранён"
            except (OSError, TochkiError):
                pass
            connections.record_check(conn, "tochki", 0, True, message, now_iso())
        except OSError:
            connections.record_check(conn, "tochki", 0, False, "Сервер недоступен.", now_iso())
        except TochkiError as err:
            connections.record_check(conn, "tochki", 0, False, str(err), now_iso())
        except SecretKeyError:
            connections.record_check(conn, "tochki", 0, False, "Секреты не читаются: проверьте мастер-ключ.", now_iso())
        return redirect(back)

    @app.route("/connections/tochki/sample")
    @login_required
    def connections_tochki_sample():
        """The saved raw answers of the supplier (no login or password in them), to write the catalog loader from."""
        path = Path(app.config["DB_PATH"]).parent / "tochki.sample.xml"
        if not path.exists():
            abort(404)
        return app.response_class(path.read_bytes(), mimetype="text/plain; charset=utf-8")

    @app.route("/connections/suppliers", methods=["POST"])
    @login_required
    def connections_add_supplier():
        try:
            new_id = connections.create_supplier(get_conn(), request.form.get("name", ""), now_iso(), author())
            flash("Поставщик добавлен.")
            return redirect("/connections/webmim/%d" % new_id)
        except ValueError as err:
            flash(str(err))
        return redirect("/connections/webmim")

    @app.route("/connections/telegram")
    @login_required
    def connections_telegram_page():
        conn = get_conn()
        box = app.config.get("SECRET_BOX")
        chats = order_notify.user_chats(conn)
        return render_template(
            "conn_telegram.html", tabs=tabs(conn, "telegram"), box_ok=box is not None,
            telegram=connections.get_config(conn, "telegram", 0),
            telegram_secrets=connections.secret_status(conn, box, "telegram", 0),
            telegram_check=connections.get_check(conn, "telegram", 0),
            user_chats=chats, panel_users=[u["login"] for u in panel_users.list_users(conn)
                                           if u["role"] != "client" or u["login"] in chats])     # a client: only its stale chat, to be cleared

    @app.route("/connections/avito")
    @login_required
    def connections_avito_page():
        conn = get_conn()
        accounts = connections.list_accounts(conn)
        if accounts:
            return redirect("/connections/avito/%d" % accounts[0]["id"])
        return render_template("conn_account_none.html", tabs=tabs(conn, "avito"), box_ok=app.config.get("SECRET_BOX") is not None)

    @app.route("/connections/avito/<int:account_id>")
    @login_required
    def connections_account_page(account_id):
        conn = get_conn()
        account = _account_or_404(account_id)
        box = app.config.get("SECRET_BOX")
        identity = connections.get_avito_identity(conn, account_id)
        return render_template(
            "conn_account.html", tabs=tabs(conn, "avito", account_id), box_ok=box is not None, entity=connections.card_view(conn, account), a=dict(
                account, label=connections.display_name(conn, account_id), identity=identity,
                feed_url="%sfeed/%s/avito_feed.xml" % (request.url_root, account["feed_token"]),
                secrets=connections.secret_status(conn, box, "avito", account_id),
                check=connections.get_check(conn, "avito", account_id), form=build_form(conn, account_id),
                pricing=connections.get_account_pricing(conn, account_id), own_photos=connections.account_own_photos(conn, account_id), exclude_types=connections.get_excluded_types(conn, account_id),
                contact_name=connections.get_contact_name(conn, account_id),
                brand_delta_text="\n".join("%s: %s" % (k, ("%g" % v)) for k, v in sorted(connections.get_brand_deltas(conn, account_id).items())),
                title_template=connections.get_title_template(conn, account_id), photo_base=connections.get_photo_base(conn, account_id),
                title_fields=feed_module.TITLE_FIELDS, photo_style=connections.get_photo_style(conn, account_id),
                stock=avito_stock.config(conn, account_id), stock_summary=avito_stock.summary(conn, account_id)),
            phones=service.phone_reconciliation(conn, app.config.get("FEED_ROOT")), tire_types=connections.TIRE_TYPES)

    def save_webmim(supplier_id, owner):
        """Saves the WebMIM form; returns False (after flashing why) when it is invalid."""
        conn = get_conn()
        base_url = request.form.get("base_url", "").strip()
        mode = request.form.get("mode", "")
        if supplier_id == 1 or base_url:
            if not base_url.startswith("https://"):
                flash("Адрес WebMIM должен начинаться с https://.")
                return False
            if not public_address(base_url):                  # the panel would ask our own network (127.0.0.1, 10.x) on behalf of the form
                flash("Адрес WebMIM должен вести на сервер в интернете: адрес не найден или ведёт во внутреннюю сеть.")
                return False
        if mode not in ("login", "apikey"):
            flash("Неизвестный режим подключения.")
            return False
        previous = connections.get_config(conn, "webmim", owner).get("base_url")
        config = {"base_url": base_url, "mode": mode, "login": request.form.get("login", "").strip()}
        if supplier_id != 1:
            territory = request.form.get("territory_rn", "").strip()
            if territory and not (territory.isascii() and territory.isdigit()):
                flash("Территория: только цифры (номер из выгрузки поставщика).")
                return False
            config["territory_rn"] = territory
        connections.set_config(conn, "webmim", owner, config, now_iso(), author())
        if previous and previous != base_url:
            connections.clear_secrets(conn, "webmim", owner, now_iso(), author())
            flash("Адрес сервера изменён: сохранённые пароль и API-ключ удалены, введите их заново.")
        _save_secrets(app, "webmim", owner, [
            ("password", request.form.get("password", "")), ("api_key", request.form.get("api_key", "")),
        ])
        return True

    @app.route("/connections/webmim", methods=["POST"])
    @login_required
    def connections_webmim():
        supplier_id = int(request.form.get("supplier", "1") or 1) if request.form.get("supplier", "1").isascii() and request.form.get("supplier", "1").isdigit() else 1
        _supplier_or_404(supplier_id)
        if save_webmim(supplier_id, connections.supplier_owner(supplier_id)):
            flash("Настройки WebMIM сохранены.")
        return redirect("/connections/webmim/%d" % supplier_id)

    @app.route("/connections/webmim/check", methods=["POST"])
    @login_required
    def connections_webmim_check():
        conn = get_conn()
        supplier_id = int(request.form.get("supplier", "1")) if request.form.get("supplier", "1").isascii() and request.form.get("supplier", "1").isdigit() else 1
        _supplier_or_404(supplier_id)
        owner = connections.supplier_owner(supplier_id)
        back = "/connections/webmim/%d" % supplier_id
        box = app.config.get("SECRET_BOX")
        if box is None:
            flash(NO_KEY)
            return redirect(back)
        if request.form.get("base_url") is not None and not save_webmim(supplier_id, owner):
            return redirect(back)
        if not connections.check_allowed(conn, "webmim", owner, now_iso()):
            flash(TOO_SOON)
            return redirect(back)
        config = dict(WEBMIM_DEFAULTS, **connections.get_config(conn, "webmim", owner))
        connections.record_check(conn, "webmim", owner, False, "проверка выполняется…", now_iso())
        try:
            password = connections.get_secret(conn, box, "webmim", owner, "password") or ""
            api_key = connections.get_secret(conn, box, "webmim", owner, "api_key") or ""
            if config["mode"] == "login" and not password:
                raise WebmimError("Пароль не задан.")
            if config["mode"] == "apikey" and not api_key:
                raise WebmimError("API-ключ не задан.")
            client = WebmimClient(
                config["base_url"], mode=config["mode"], login=config.get("login", ""),
                password=password, api_key=api_key,
                http=app.config.get("WEBMIM_HTTP") or urllib_transport,
            )
            with reliability.probe():                      # the owner's check goes even while the service rests, and its result counts
                message = client.check(connections.supplier_territory(conn, supplier_id, active_settings(conn)))
            connections.record_check(conn, "webmim", owner, True, message, now_iso())
        except OSError:
            connections.record_check(conn, "webmim", owner, False, "Сервер недоступен.", now_iso())
        except WebmimError as err:
            connections.record_check(conn, "webmim", owner, False, str(err), now_iso())
        except SecretKeyError:
            connections.record_check(conn, "webmim", owner, False, "Секреты не читаются: проверьте мастер-ключ.", now_iso())
        except ValueError:
            connections.record_check(conn, "webmim", owner, False, "Неверные настройки подключения.", now_iso())
        return redirect(back)

    def _orders_supplier():
        raw = request.form.get("supplier", "1")
        supplier_id = int(raw) if raw.isascii() and raw.isdigit() else 1
        _supplier_or_404(supplier_id)
        return supplier_id, connections.supplier_owner(supplier_id)

    @app.route("/connections/webmim/orders", methods=["POST"])
    @login_required
    def connections_webmim_orders():
        """The API user for orders: which server, the login and the password (the password is kept only when typed)."""
        conn = get_conn()
        supplier_id, owner = _orders_supplier()
        server = request.form.get("server", "")
        login_name = request.form.get("login", "").strip()
        auth = "login"                                          # the only way in; the form no longer sends a choice
        if server not in webmim_orders.SERVERS:
            flash("Неизвестный сервер: выберите тестовый или боевой.")
        elif len(login_name) > 100 or any(ch.isspace() for ch in login_name):
            flash("Логин: без пробелов, не длиннее 100 знаков.")
        else:
            connections.set_config(conn, "webmim_orders", owner, {"server": server, "login": login_name, "auth": auth}, now_iso(), author())
            _save_secrets(app, "webmim_orders", owner, [("password", request.form.get("password", ""))])
            flash("Настройки заказов сохранены.")
        return redirect("/connections/webmim/%d" % supplier_id + "#orders")

    @app.route("/connections/webmim/orders-check", methods=["POST"])
    @login_required
    def connections_webmim_orders_check():
        """Can the API user work with orders on the chosen server? Reads the token, the version and the dictionaries; creates nothing."""
        conn = get_conn()
        supplier_id, owner = _orders_supplier()
        back = "/connections/webmim/%d" % supplier_id + "#orders"
        box = app.config.get("SECRET_BOX")
        if box is None:
            flash(NO_KEY)
            return redirect(back)
        if not connections.check_allowed(conn, "webmim_orders", owner, now_iso()):
            flash(TOO_SOON)
            return redirect(back)
        config = connections.get_config(conn, "webmim_orders", owner)
        server = config.get("server") if config.get("server") in webmim_orders.SERVERS else webmim_orders.DEFAULT_SERVER
        connections.record_check(conn, "webmim_orders", owner, False, "проверка выполняется…", now_iso())
        client = None
        try:
            http = app.config.get("WEBMIM_HTTP") or urllib_transport
            password = connections.get_secret(conn, box, "webmim_orders", owner, "password") or ""
            client = webmim_orders.OrdersClient(webmim_orders.SERVERS[server], config.get("login", ""), password, http=http)
            with reliability.probe():                      # the owner's check goes even while the service rests, and its result counts
                version = client.version()
            dictionaries = client.dictionaries()
            message = "%s: вход — %s; версия API %s; %s" % (webmim_orders.SERVER_NAMES[server].split(" ")[0], client.method, version,
                                                         webmim_orders.summarize(dictionaries))
            document = None
            try:
                document = client.openapi()
            except webmim_orders.OrdersError:
                document = None
            if document:
                from pathlib import Path
                (Path(app.config["DB_PATH"]).parent / "webmim.api.v2.openapi.yaml").write_bytes(document)
                message += "; описание API сохранено"
            connections.record_check(conn, "webmim_orders", owner, True, message, now_iso())
        except OSError:
            connections.record_check(conn, "webmim_orders", owner, False, "Сервер недоступен.", now_iso())
        except webmim_orders.OrdersError as err:
            connections.record_check(conn, "webmim_orders", owner, False, str(err), now_iso())
        except SecretKeyError:
            connections.record_check(conn, "webmim_orders", owner, False, "Секреты не читаются: проверьте мастер-ключ.", now_iso())
        except ValueError:
            connections.record_check(conn, "webmim_orders", owner, False, "Неверные настройки подключения.", now_iso())
        finally:
            if client is not None:
                client.close()
        return redirect(back)

    @app.route("/connections/telegram/user-chat", methods=["POST"])
    @login_required
    def connections_telegram_user_chat():
        conn = get_conn()
        try:
            login = request.form.get("login", "")
            known = {u["login"]: u["role"] for u in panel_users.list_users(conn)}
            if login not in known or (known[login] == "client" and request.form.get("chat_id", "").strip()):   # a client: only cleared
                raise orders.OrderError("Такого пользователя панели нет." if login not in known else "Клиенту чат Telegram не привязывается.")
            order_notify.set_user_chat(conn, login, request.form.get("chat_id", ""), now_iso(), author())
            flash("Чат Telegram сохранён.")
        except orders.OrderError as err:
            flash(str(err))
        return redirect("/connections/telegram")

    @app.route("/connections/telegram", methods=["POST"])
    @login_required
    def connections_telegram():
        conn = get_conn()
        chat_id = request.form.get("chat_id", "").strip()
        connections.set_config(conn, "telegram", 0, {"chat_id": chat_id}, now_iso(), author())
        _save_secrets(app, "telegram", 0, [("bot_token", request.form.get("bot_token", ""))])
        flash("Настройки Telegram сохранены.")
        return redirect("/connections/telegram")

    @app.route("/connections/telegram/test", methods=["POST"])
    @login_required
    def connections_telegram_test():
        conn = get_conn()
        box = app.config.get("SECRET_BOX")
        if box is None:
            flash(NO_KEY)
            return redirect("/connections/telegram")
        if "chat_id" in request.form:
            connections.set_config(conn, "telegram", 0, {"chat_id": request.form["chat_id"].strip()}, now_iso(), author())
            _save_secrets(app, "telegram", 0, [("bot_token", request.form.get("bot_token", ""))])
        if not connections.check_allowed(conn, "telegram", 0, now_iso()):
            flash(TOO_SOON)
            return redirect("/connections/telegram")
        connections.record_check(conn, "telegram", 0, False, "проверка выполняется…", now_iso())
        try:
            token = connections.get_secret(conn, box, "telegram", 0, "bot_token") or ""
            chat_id = connections.get_config(conn, "telegram", 0).get("chat_id", "")
            send_message(token, chat_id, "Tires Sync: тестовое сообщение.", http=app.config.get("TELEGRAM_HTTP") or urllib_transport)
            connections.record_check(conn, "telegram", 0, True, "сообщение отправлено", now_iso())
        except NotifyError as err:
            connections.record_check(conn, "telegram", 0, False, str(err), now_iso())
        except SecretKeyError:
            connections.record_check(conn, "telegram", 0, False, "Секреты не читаются: проверьте мастер-ключ.", now_iso())
        return redirect("/connections/telegram")

    @app.route("/connections/accounts", methods=["POST"])
    @login_required
    def connections_add_account():
        try:
            new_id = connections.create_account(get_conn(), request.form.get("name", ""), now_iso(), author())
            flash("Аккаунт добавлен.")
            return redirect("/connections/avito/%d" % new_id)
        except ValueError as err:
            flash(str(err))
        return redirect("/connections/avito")

    def _account_or_404(account_id: int) -> dict:
        account = connections.get_account(get_conn(), account_id)
        if account is None:
            abort(404)
        return account

    @app.route("/connections/accounts/<int:account_id>/modes", methods=["POST"])
    @login_required
    def account_modes(account_id):
        conn = get_conn()
        if connections.get_account(conn, account_id) is None:
            abort(404)
        try:
            connections.set_modes(conn, account_id, bool(request.form.get("chats_enabled")), bool(request.form.get("feed_enabled")), now_iso(), author())
            connections.set_chats_send(conn, account_id, bool(request.form.get("chats_send")) and bool(request.form.get("chats_enabled")), now_iso(), author())
            flash("Режимы аккаунта сохранены.")
        except ValueError as err:
            flash(str(err))
        return redirect("/connections/avito/%d" % account_id)

    @app.route("/connections/accounts/<int:account_id>/phone-source", methods=["POST"])
    @login_required
    def account_phone_source(account_id):
        _account_or_404(account_id)
        try:
            connections.set_phone_source(get_conn(), account_id, "file" if request.form.get("keep_file") else "cabinet", now_iso(), author())
            flash("Источник телефона сохранён.")
        except ValueError as err:
            flash(str(err))
        return redirect("/connections/avito/%d" % account_id)

    @app.route("/connections/accounts/<int:account_id>/exclude-types", methods=["POST"])
    @login_required
    def account_exclude_types(account_id):
        _account_or_404(account_id)
        try:
            connections.set_excluded_types(get_conn(), account_id, request.form.getlist("exclude_types"), now_iso(), author())
            flash("Типы шин, которые не публикуются в этом аккаунте, сохранены. Применятся при ближайшем цикле.")
        except ValueError as err:
            flash(str(err))
        return redirect("/connections/avito/%d" % account_id)

    @app.route("/connections/accounts/<int:account_id>/contact-name", methods=["POST"])
    @login_required
    def account_contact_name(account_id):
        _account_or_404(account_id)
        try:
            connections.set_contact_name(get_conn(), account_id, request.form.get("contact_name", ""), now_iso(), author())
            flash("Имя контактного лица сохранено. Попадёт в объявления при ближайшем цикле.")
        except ValueError as err:
            flash(str(err))
        return redirect("/connections/avito/%d" % account_id)

    @app.route("/connections/accounts/<int:account_id>/phone", methods=["POST"])
    @login_required
    def account_phone(account_id):
        _account_or_404(account_id)
        try:
            connections.pin_phone(get_conn(), account_id, request.form.get("phone", "").strip(), now_iso(), author())
            flash("Номер для объявлений сохранён.")
        except ValueError as err:
            flash(str(err))
        return redirect("/connections/avito/%d" % account_id)

    @app.route("/connections/accounts/<int:account_id>/live", methods=["GET", "POST"])
    @login_required
    def connections_account_live(account_id):
        account = _account_or_404(account_id)
        if request.method == "GET":
            ads = None
            root = app.config.get("FEED_ROOT")
            if root:
                path = Path(root) / account["feed_token"] / "next" / "avito_feed.xml"
                try:
                    ads = count_ads(path.read_bytes())
                except (OSError, ET.ParseError):
                    ads = None
            return render_template(
                "live.html", account=account, ads=ads,
                has_addresses=bool(connections.list_addresses(get_conn(), account_id)))
        action = request.form.get("action")
        if action not in ("enable", "disable"):
            abort(400)
        if action == "enable" and not request.form.get("understand"):
            flash("Отметьте подтверждение, чтобы включить публикацию.")
            return redirect("/connections/accounts/%d/live" % account_id)
        connections.set_live(get_conn(), account_id, action == "enable", now_iso(), author())
        flash("Публикация включена." if action == "enable" else "Публикация выключена.")
        if action == "enable":
            from .routes_drafts import publish_now, publish_summary
            lines = publish_now(app)
            if lines is not None:
                flash(publish_summary(lines))
        return redirect("/connections/avito/%d" % account_id)

    @app.route("/connections/accounts/<int:account_id>/pricing", methods=["POST"])
    @login_required
    def connections_account_pricing(account_id):
        _account_or_404(account_id)
        raw_delta = request.form.get("delta", "").strip().replace(",", ".") or "0"
        raw_stock = request.form.get("min_stock", "").strip()
        try:
            delta = float(raw_delta)
            stock = int(raw_stock) if raw_stock else None
            connections.set_account_pricing(get_conn(), account_id, delta, stock, now_iso(), author())
            flash("Цены и остатки аккаунта сохранены. Применятся при ближайшем цикле.")
        except ValueError as err:
            flash(str(err) if "Поправка" in str(err) or "Порог" in str(err) else "Введите числа: поправка в процентных пунктах, порог целым числом.")
        return redirect("/connections/avito/%d" % account_id)

    @app.route("/connections/accounts/<int:account_id>/own-photos", methods=["POST"])
    @login_required
    def connections_account_own_photos(account_id):
        _account_or_404(account_id)
        on = request.form.get("on") == "1"
        connections.set_account_own_photos(get_conn(), account_id, on, now_iso(), author())
        get_conn().commit()
        flash("Свои фото шин в этом аккаунте %s. Применится при ближайшем цикле." % ("включены" if on else "выключены"))
        return redirect("/connections/avito/%d" % account_id)

    @app.route("/connections/accounts/<int:account_id>/stock", methods=["POST"])
    @login_required
    def connections_account_stock(account_id):
        _account_or_404(account_id)
        try:
            cfg = avito_stock.save_config(get_conn(), account_id, request.form.get("mode", ""), request.form.get("codes", ""),
                                          request.form.get("cap", ""), now_iso(), author())
            flash("Остатки на Авито: %s. Применится после ближайшего цикла." % avito_stock.MODE_LABELS[cfg["mode"]])
        except ValueError as err:
            flash(str(err))
        return redirect("/connections/avito/%d#stock" % account_id)

    @app.route("/connections/accounts/<int:account_id>/brand-pricing", methods=["POST"])
    @login_required
    def connections_account_brand_pricing(account_id):
        _account_or_404(account_id)
        try:
            deltas = connections.parse_brand_deltas(request.form.get("brands", ""))
            known = connections.known_brands(active_settings(get_conn()))
            unknown = [b for b in deltas if connections._clean_brand(b) not in known]
            if unknown:
                raise ValueError("Бренд не найден среди брендов с правилом цены: %s. Доступны: %s." % (", ".join(unknown), ", ".join(known)))
            connections.set_brand_deltas(get_conn(), account_id, deltas, now_iso(), author())
            flash("Наценка по брендам сохранена. Применится при ближайшем цикле.")
        except ValueError as err:
            flash(str(err))
        return redirect("/connections/avito/%d" % account_id)

    @app.route("/connections/accounts/<int:account_id>/title", methods=["POST"])
    @login_required
    def connections_account_title(account_id):
        _account_or_404(account_id)
        try:
            connections.set_title_template(get_conn(), account_id, request.form.get("template", ""), now_iso(), author())
            flash("Шаблон заголовка сохранён. Он действует для новых объявлений: уже размещённые названия не меняются.")
        except ValueError as err:
            flash(str(err))
        return redirect("/connections/avito/%d" % account_id)

    @app.route("/connections/accounts/<int:account_id>/photo-base", methods=["POST"])
    @login_required
    def connections_account_photo_base(account_id):
        _account_or_404(account_id)
        try:
            connections.set_photo_base(get_conn(), account_id, request.form.get("base", ""), now_iso(), author())
            flash("Адрес фото аккаунта сохранён. Ссылки в фиде поменяются при ближайшем цикле.")
        except ValueError as err:
            flash(str(err))
        return redirect("/connections/avito/%d" % account_id)

    @app.route("/connections/accounts/<int:account_id>/photo-style", methods=["POST"])
    @login_required
    def connections_account_photo_style(account_id):
        _account_or_404(account_id)
        try:
            style = connections.set_photo_style(get_conn(), account_id, {
                "label": request.form.get("label", ""), "corner": request.form.get("corner", ""), "border": request.form.get("border", ""),
                "crop": request.form.get("crop", ""), "quality": request.form.get("quality", "")}, now_iso(), author())
            flash("Вид фото аккаунта сохранён." if style else "Обработка фото аккаунта выключена: отдаётся картинка поставщика.")
        except ValueError as err:
            flash(str(err))
        return redirect("/connections/avito/%d" % account_id)

    def _save_keys(account_id) -> None:
        """The keys of an account, from either form that takes them (save, check)."""
        _save_secrets(app, "avito", account_id, [
            ("client_id", request.form.get("client_id", "")), ("client_secret", request.form.get("client_secret", "")),
        ])
        if request.form.get("client_id", "").strip() or request.form.get("client_secret", "").strip():
            connections.forget_identity(get_conn(), account_id)               # new keys may open another cabinet: id and phone are read again at the check

    @app.route("/connections/accounts/<int:account_id>/keys", methods=["POST"])
    @login_required
    def connections_account_keys(account_id):
        _account_or_404(account_id)
        _save_keys(account_id)
        flash("Ключи аккаунта сохранены.")
        return redirect("/connections/avito/%d" % account_id)

    @app.route("/connections/accounts/<int:account_id>/check", methods=["POST"])
    @login_required
    def connections_account_check(account_id):
        _account_or_404(account_id)
        conn = get_conn()
        box = app.config.get("SECRET_BOX")
        if box is None:
            flash(NO_KEY)
            return redirect("/connections/avito/%d" % account_id)
        if "client_id" in request.form:
            _save_keys(account_id)
        if not connections.check_allowed(conn, "avito", account_id, now_iso()):
            flash(TOO_SOON)
            return redirect("/connections/avito/%d" % account_id)
        connections.record_check(conn, "avito", account_id, False, "проверка выполняется…", now_iso())
        try:
            client_id = connections.get_secret(conn, box, "avito", account_id, "client_id")
            client_secret = connections.get_secret(conn, box, "avito", account_id, "client_secret")
            if not client_id or not client_secret:
                connections.record_check(conn, "avito", account_id, False, "Ключи аккаунта не заданы.", now_iso())
                return redirect("/connections/avito/%d" % account_id)
            client = AvitoClient(client_id, client_secret, http=app.config.get("AVITO_HTTP") or urllib_transport)
            try:
                with reliability.probe():                  # the owner's check goes even while the service rests, and its result counts
                    me = client.get_self(retry=False)
                if me.get("id"):
                    connections.set_avito_identity(conn, account_id, me["id"], me.get("name"), me.get("phone"), me.get("phones"))
            except AvitoError as err:
                if err.status not in (403, 404):
                    raise
            client.get_profile(retry=False)
            connections.record_check(
                conn, "avito", account_id, True, "ключи верны, профиль автозагрузки доступен", now_iso())
        except OSError:
            connections.record_check(conn, "avito", account_id, False, "Сервер недоступен.", now_iso())
        except AvitoAuthError:
            connections.record_check(conn, "avito", account_id, False, "ключи отклонены Авито.", now_iso())
        except AvitoError as err:
            if err.status == 403:
                connections.record_check(
                    conn, "avito", account_id, True,
                    "ключи верны, но профиль автозагрузки недоступен (включите автозагрузку в кабинете).", now_iso())
            else:
                connections.record_check(
                    conn, "avito", account_id, False, "Авито ответил ошибкой (код %s)." % err.status, now_iso())
        except SecretKeyError:
            connections.record_check(conn, "avito", account_id, False, "Секреты не читаются: проверьте мастер-ключ.", now_iso())
        return redirect("/connections/avito/%d" % account_id)
