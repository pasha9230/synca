"""The conditions of the clients in «Пользователи» (the owner, plan client-role §8 builds 3-4): the role's (every client without their own)
and a client's own over them; the owner's preview of a client's catalog. Section «users»: changing is the owner's (EDIT), the partner looks.
The conditions are written by client_terms; how they act is client_catalog's (not changed here)."""
from __future__ import annotations

from flask import abort, flash, make_response, redirect, render_template, request

from .. import client_terms as ct, store, users
from ..catalog import resolve_brand
from . import client_catalog as cc, routes_shop
from .common import author, get_conn, now_iso
from .data import load_reference


def brand_choices(app, conn, *known) -> list:
    """[(bare key, label)] of the brands of today's loads of both warehouses and of the conditions already written (never lost from the
    form), by the label."""
    ref, brand_map = load_reference(app.config["REFERENCE_PATH"]), store.load_brand_map(conn)
    labels = {}
    for offer in routes_shop._offers(app, conn).offers.values():
        key = cc.bare_brand(offer.brand_key)
        if key and key not in labels:
            labels[key] = resolve_brand(offer.brand_key, ref, brand_map) or (offer.brand_raw or "").strip() or key.upper()
    for keys in known:
        for key in keys:
            labels.setdefault(key, key.upper())
    return sorted(labels.items(), key=lambda kv: (kv[1].casefold(), kv[0]))


def _fmt(value) -> str:
    return "" if value is None else "%g" % value


def _rows(choices, brands: dict) -> list:
    return [{"key": key, "label": label, "on": key in brands, "small": _fmt(brands.get(key, (None, None))[0]),
             "large": _fmt(brands.get(key, (None, None))[1])} for key, label in choices]


def _typed_rows(form, choices) -> list:
    """The rows as they were sent (a refused form comes back with what was typed)."""
    labels = dict(choices)
    keys = form.getlist("bk")[:ct.MAX_BRANDS]
    return [{"key": key, "label": labels.get(key, key.upper()), "on": form.get("on_%d" % i) == "1",
             "small": form.get("s_%d" % i, "")[:12], "large": form.get("l_%d" % i, "")[:12]} for i, key in enumerate(keys)]


def _parse(form, role: bool):
    """(small, large, mode, {key: (small, large)}) of a sent form; a wrong % is refused naming its field. The role and a client's own list:
    the ticked brands (with their % if typed); «как у роли»: only the brands given a % of their own (the set is the role's)."""
    small, large = ct.parse_pct(form.get("small", ""), "small"), ct.parse_pct(form.get("large", ""), "large")
    mode = "own" if role else form.get("mode", "")
    if mode not in ct.MODES:
        raise ct.TermsError("Бренды: «как у роли» или «свой список».", "mode")
    keys = form.getlist("bk")
    if len(keys) > ct.MAX_BRANDS:
        raise ct.TermsError("Слишком много брендов: не больше %d." % ct.MAX_BRANDS)
    brands = {}
    for i, key in enumerate(keys):
        pct = (ct.parse_pct(form.get("s_%d" % i, ""), "s_%d" % i), ct.parse_pct(form.get("l_%d" % i, ""), "l_%d" % i))
        if (form.get("on_%d" % i) == "1") if mode == "own" else pct != (None, None):
            brands[key] = pct
    return small, large, mode, brands


def _client_or_404(conn, login: str) -> str:
    login = users.normalize(login)
    if not users.exists(conn, login) or users.get_role(conn, login)["role"] != "client":
        abort(404)
    return login


def register(app) -> None:
    def role_page(conn, status=200, typed=None):
        role = ct.read(conn, ct.ROLE)
        choices = brand_choices(app, conn, role["brands"])
        clients = [dict(u, terms=ct.read(conn, u["login"])) for u in users.list_users(conn) if u["role"] == "client"]
        return render_template("users_clients.html", role=role, rows=_typed_rows(typed, choices) if typed else _rows(choices, role["brands"]),
                               typed=typed, clients=clients, describe=ct.describe, fmt=_fmt), status

    @app.route("/client-terms")
    def users_clients_page():
        return role_page(get_conn())

    @app.route("/client-terms/role", methods=["POST"])
    def users_clients_role_save():
        conn = get_conn()
        try:
            ct.save(conn, ct.ROLE, *_parse(request.form, role=True), now=now_iso(), author=author())
        except ct.TermsError as err:
            flash(str(err), "field:" + err.field if err.field else "error")
            return role_page(conn, 400, request.form)
        flash("Условия роли «Клиент» сохранены: действуют для всех клиентов без своих условий.", "ok")
        return redirect("/client-terms")

    def client_page(conn, login, status=200, typed=None):
        own, role = ct.read(conn, login), ct.read(conn, ct.ROLE)
        choices = brand_choices(app, conn, own["brands"], role["brands"])
        rows = _typed_rows(typed, choices) if typed else _rows(choices, own["brands"])
        for r in rows:                                                # what the role gives, beside the client's own
            r["role_on"], r["role_pct"] = r["key"] in role["brands"], role["brands"].get(r["key"], (None, None))
            if not typed and own["mode"] != "own":
                r["on"] = r["role_on"]                                # «как у роли»: the ticks show the role's set (a switch to own starts from it)
        return render_template("users_client.html", login=login, info=users.get_role(conn, login), own=own, role=role, rows=rows, typed=typed,
                               fmt=_fmt), status

    @app.route("/client-terms/c/<login>")
    def users_client_page(login):
        conn = get_conn()
        return client_page(conn, _client_or_404(conn, login))

    @app.route("/client-terms/c/<login>", methods=["POST"])
    def users_client_save(login):
        conn = get_conn()
        login = _client_or_404(conn, login)
        try:
            ct.save(conn, login, *_parse(request.form, role=False), now=now_iso(), author=author())
        except ct.TermsError as err:
            flash(str(err), "field:" + err.field if err.field else "error")
            return client_page(conn, login, 400, request.form)
        flash("Условия клиента %s сохранены." % login, "ok")
        return redirect("/client-terms/c/" + login)

    @app.route("/client-terms/c/<login>/preview")
    def users_client_preview(login):
        """«Каталог глазами клиента»: the very rows of the client's /shop (client_catalog with the client's conditions), without photos."""
        conn = get_conn()
        login = _client_or_404(conn, login)
        page = routes_shop.catalog(app, conn, login, "/client-terms/c/%s/preview" % login, photos=False)
        response = make_response(render_template("users_client_preview.html", login=login, info=users.get_role(conn, login), **page))
        response.headers["Cache-Control"] = "private, no-store"
        return response
