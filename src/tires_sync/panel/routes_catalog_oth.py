"""«Товар и фид» → «На ОТХ»: the tyres of the orders the clients refused, kept by us (order_oth). The mark «Вернуть поставщику» is ours only."""
from __future__ import annotations

from flask import flash, redirect, render_template, request

from .. import order_oth
from .common import author, get_conn, login_required, now_iso


def register(app) -> None:
    @app.route("/oth")
    @login_required
    def oth_page():
        return render_template("oth.html", **order_oth.stock(get_conn()))

    @app.route("/oth/return", methods=["POST"])
    @login_required
    def oth_return():
        conn = get_conn()
        try:
            item_id = int(request.form.get("item") or 0)
            qty = request.form.get("qty")                                 # no field (the older form): every piece left, or every piece returned;
                                                                          # an empty field is a wrong number, never «all»
            if request.form.get("undo"):
                order_oth.unmark_returned(conn, item_id, now_iso(), author(), qty)
                flash("Отметка «возвращено поставщику» снята: шины снова на ОТХ.")
            else:
                order_oth.mark_returned(conn, item_id, now_iso(), author(), qty)
                flash("Отмечено как возвращённое поставщику. В WebMIM ничего не отправлено.")
        except (order_oth.OthError, ValueError) as err:
            flash(str(err) if isinstance(err, order_oth.OthError) else "Неизвестная позиция.")
        return redirect("/oth")
