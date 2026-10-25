from __future__ import annotations

from flask import abort, flash, redirect, render_template, request

from .. import connections, store
from ..description import (
    ALLOWED, DEFAULT_TEMPLATE, PLACEHOLDER_HELP, render_description, validate_template,
)
from .common import author, get_conn, login_required, now_iso
from .routes_main import context
from . import service


def register(app) -> None:
    def show(account: dict, text: str, errors: list, status: int = 200):
        conn, settings, parsed, ref = context(app)
        sample = service.description_sample(conn, settings, parsed, ref)
        preview = None
        if sample is not None and not errors:
            preview = render_description(text, sample, sample.price, store.load_old_model_names(conn))
        return render_template(
            "description.html", account=account, text=text, errors=errors, preview=preview, sample=sample,
            placeholders=[(name, PLACEHOLDER_HELP[name]) for name in ALLOWED],
            custom=account["description_template"] is not None,
        ), status

    def account_or_404(account_id: int) -> dict:
        account = connections.get_account(get_conn(), account_id)
        if account is None:
            abort(404)
        return account

    @app.route("/accounts/<int:account_id>/description")
    @login_required
    def description_page(account_id):
        account = account_or_404(account_id)
        return show(account, account["description_template"] or DEFAULT_TEMPLATE, [])

    @app.route("/accounts/<int:account_id>/description", methods=["POST"])
    @login_required
    def description_save(account_id):
        account = account_or_404(account_id)
        action = request.form.get("action", "")
        text = request.form.get("template", "").replace("\r\n", "\n").replace("\r", "\n")
        if action == "reset":
            connections.set_description_template(get_conn(), account_id, None, now_iso(), author())
            flash("Шаблон сброшен к стандартному.")
            return redirect("/accounts/%d/description" % account_id)
        if action not in ("save", "preview"):
            abort(400)
        errors = validate_template(text)
        if errors:
            return show(account, text, errors, 400)
        if action == "preview":
            return show(account, text, [])
        connections.set_description_template(get_conn(), account_id, text, now_iso(), author())
        flash("Шаблон сохранён.")
        return redirect("/accounts/%d/description" % account_id)
