"""«Убрать пустые карточки из «Нового заказа»»: the preview with the rule, then one button. See order_cleanup."""
from __future__ import annotations

from flask import flash, redirect, render_template, request

from .. import order_cleanup
from ..orders import OrderError
from .common import author, get_conn, login_required
from .routes_orders import local_iso

SHOWN = 100


def register(app) -> None:
    @app.route("/orders/cleanup")
    @login_required
    def orders_cleanup_page():
        plan = order_cleanup.preview(get_conn(), local_iso())
        return render_template("orders_cleanup.html", plan=plan, shown=plan["remove"][:SHOWN], reasons=order_cleanup.REASONS,
                               fresh_days=order_cleanup.FRESH_DAYS)

    @app.route("/orders/cleanup", methods=["POST"])
    @login_required
    def orders_cleanup_run():
        text = request.form.get("expected", "").strip()
        if not text:
            flash("Откройте страницу ещё раз и проверьте список.")
            return redirect("/orders/cleanup")
        try:
            done = order_cleanup.run(get_conn(), app.config["DB_PATH"], local_iso(), author(), text)
        except OrderError as err:
            flash(str(err))
            return redirect("/orders/cleanup")
        flash("Убрано карточек: %d%s%s. Перед этим снята копия базы %s. Вернуть любую можно в «Удалённых», чаты на месте." % (
            done["removed"], " (%d изменили по ходу, они остались)" % done["skipped"] if done["skipped"] else "",
            "; база была занята, %d не убраны, нажмите ещё раз" % done["left"] if done["left"] else "", done["backup"]))
        return redirect("/deleted?k=order")
