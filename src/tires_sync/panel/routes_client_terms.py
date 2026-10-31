"""The conditions of the clients in «Пользователи» (the owner, plan client-role §8 builds 3-4; since 2026-10-31 a client's own % of a brand,
kept as a shift from ours: client_terms): the list of the clients, a client's brands; the owner's preview of a client's catalog.
Section «users»: changing is the owner's (EDIT), the partner looks. The conditions are written by client_terms; how they act is client_catalog's (not changed here)."""
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


def rows_of(choices, ours: dict, data: dict, typed=None, prefix: str = "") -> list:
    """The brands of a form (client_terms): per diameter the field (its mode and what it shows), our % now beside it and the client's %
    by every one of ours. `typed`: a refused form comes back with what was typed (the fields only)."""
    sent = {}
    if typed:
        for i, key in enumerate(typed.getlist(prefix + "bk")[:ct.MAX_BRANDS]):
            sent[key] = {d: typed.get("%s%s_%d" % (prefix, d[0], i), "")[:12] for d, _label in ct.DIAMETERS}
    out = []
    for key, label in choices:
        mine, row = ours.get(key, []), data.get(key, ct.EMPTY)
        cells = {}
        for diameter, _dlabel in ct.DIAMETERS:
            mode, our = ct.mode_of(mine, diameter)
            absolute = row[1] if diameter == "small" else row[3]
            cells[diameter] = {
                "mode": mode, "mark": ("abs:%g" % (row[0] if diameter == "small" else row[2])) if absolute and mode != "abs"
                else "pct:%g" % our if mode == "pct" else mode,
                "value": sent[key][diameter] if key in sent else ct.field_value(row, diameter, mine),
                "absolute": absolute and mode != "abs",                      # kept absolute: we had no % then, we have one now
                "totals": [{"label": o["label"], "our": o[diameter], "client": ct.client_pct(row, diameter, o[diameter])} for o in mine],
                "own": ct.client_pct(row, diameter, None)}
        out.append({"key": key, "label": label, "ours": mine, "cells": cells, "set": key in data})
    return out


def parse_form(form, prefix: str = "") -> dict:
    """{bare key: Row} of a sent form, read against our % now (client_terms.parse_rows)."""
    return ct.parse_rows(form, prefix, ct.active_ours(get_conn()))


def _client_or_404(conn, login: str) -> str:
    login = users.normalize(login)
    if not users.exists(conn, login) or users.get_role(conn, login)["role"] != "client":
        abort(404)
    return login


def register(app) -> None:
    @app.route("/client-terms")
    def users_clients_page():
        conn = get_conn()
        clients = [dict(u, terms=ct.rows(conn, u["login"])) for u in users.list_users(conn) if u["role"] == "client"]
        return render_template("users_clients.html", clients=clients, describe=ct.describe)

    def client_page(conn, login, status=200, typed=None):
        own, ours = ct.rows(conn, login), ct.active_ours(conn)
        rows = rows_of(brand_choices(app, conn, own), ours, own, typed)
        return render_template("users_client.html", login=login, info=users.get_role(conn, login), rows=rows, typed=typed), status

    @app.route("/client-terms/c/<login>")
    def users_client_page(login):
        conn = get_conn()
        return client_page(conn, _client_or_404(conn, login))

    @app.route("/client-terms/c/<login>", methods=["POST"])
    def users_client_save(login):
        conn = get_conn()
        login = _client_or_404(conn, login)
        try:
            data = parse_form(request.form)
            keys = {cc.bare_brand(k) for k in request.form.getlist("bk")}
            ct.save_rows(conn, login, dict({k: ct.EMPTY for k in keys if k}, **data), now_iso(), author())   # an emptied brand: as ours
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
