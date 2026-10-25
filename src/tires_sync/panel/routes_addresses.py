from __future__ import annotations

from flask import abort, flash, redirect, render_template, request

from .. import connections
from ..settings_store import active_settings
from .service import SEASON_NAMES
from .common import author, get_conn, login_required, now_iso

EXTRA_ROWS = 3


def parse_rows(form) -> list:
    rows = []
    index = 0
    while ("addr_%d" % index) in form or ("name_%d" % index) in form:
        name = form.get("name_%d" % index, "").strip()
        address_id = form.get("addr_%d" % index, "").strip()
        raw_share = form.get("share_%d" % index, "").strip().replace(",", ".")
        index += 1
        if not name and not address_id and not raw_share and not form.get("text_%d" % (index - 1), "").strip():
            continue
        try:
            share = float(raw_share) if raw_share else 0.0
        except ValueError:
            share = float("nan")
        rows.append({"name": name, "address_id": address_id, "enabled": ("on_%d" % (index - 1)) in form, "share": share,
                     "address_text": form.get("text_%d" % (index - 1), "").strip()})
    return rows


def build_form(conn, account_id, rows=None, mode=None, rest=None, errors=(), address_mode=None) -> dict:
    saved_mode, saved_rest = connections.get_point_config(conn, account_id)
    rows = connections.list_addresses(conn, account_id) if rows is None else rows
    extra = [{"name": "", "address_id": "", "enabled": True, "share": "", "address_text": ""}] * EXTRA_ROWS
    settings = active_settings(conn)
    names = {r["address_id"]: r["name"] for r in connections.list_addresses(conn, account_id)}
    rules = [dict(r, address_name=names.get(r["address_id"], r["address_id"]),
                  label="%s · %s" % (r["brand_key"], SEASON_NAMES.get(r["season"], r["season"])))
             for r in connections.list_point_rules(conn, account_id)]
    pairs = [{"key": "%s|%s" % (b, s.value), "label": "%s · %s" % (b, SEASON_NAMES.get(s.value, s.value))}
             for b, s in sorted(settings.enabled, key=lambda p: (p[0], p[1].value))]
    return {"pairs": pairs, "rules": rules, "choices": [r for r in connections.list_addresses(conn, account_id) if r["enabled"]],
            "rows": list(rows) + extra, "mode": mode or saved_mode, "rest": (saved_rest if rest is None else rest) or "",
            "address_mode": address_mode or connections.get_address_mode(conn, account_id), "errors": list(errors), "has_own": bool(connections.list_addresses(conn, account_id))}


def register(app) -> None:
    def account_or_404(account_id: int) -> dict:
        account = connections.get_account(get_conn(), account_id)
        if account is None:
            abort(404)
        return account

    def show(account, rows, mode, rest, errors, status=200, address_mode=None):
        return render_template(
            "addresses.html", account=account,
            form=build_form(get_conn(), account["id"], rows, mode, rest, errors, address_mode)), status

    @app.route("/accounts/<int:account_id>/point-rules", methods=["POST"])
    @login_required
    def point_rules(account_id):
        account_or_404(account_id)
        brand, _, season = request.form.get("pair", "").partition("|")
        action = request.form.get("action", "")
        if not brand or not season or action not in ("set", "clear"):
            abort(400)
        try:
            if action == "set":
                connections.set_point_rule(get_conn(), account_id, brand, season, request.form.get("address_id", ""),
                                           now_iso(), author())
                flash("Распределение по бренду сохранено.")
            else:
                connections.clear_point_rule(get_conn(), account_id, brand, season, now_iso(), author())
                flash("Правило для бренда снято.")
        except ValueError as err:
            flash(str(err))
        return redirect("/connections/avito/%d" % account_id)

    @app.route("/accounts/<int:account_id>/addresses")
    @login_required
    def addresses_page(account_id):
        account_or_404(account_id)
        return redirect("/connections/avito/%d" % account_id)

    @app.route("/accounts/<int:account_id>/addresses", methods=["POST"])
    @login_required
    def addresses_save(account_id):
        account = account_or_404(account_id)
        mode = request.form.get("mode", "percent")
        rest = request.form.get("rest", "").strip() or None
        address_mode = request.form.get("address_mode", "id")
        rows = parse_rows(request.form)
        try:
            connections.save_addresses(
                get_conn(), account_id, mode, rest, rows, now_iso(), author(), address_mode=address_mode)
        except ValueError as err:
            shown = [dict(r, share=("" if r["share"] != r["share"] else r["share"])) for r in rows]
            return show(account, shown, mode, rest, [str(err)], 400, address_mode)
        flash("Авито адреса сохранены.")
        return redirect("/accounts/%d/addresses" % account_id)
