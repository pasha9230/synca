"""«Не дошло клиенту» in the order card (unsent.py, the owner 2026-10-31): the block's rows for the template and «Отправил вручную». Section
«Заказы»: «менять» to mark it; a manager of a point by the orders of its own point only (common.check_access: `card_id` of the address).
Nothing is sent from here."""
from __future__ import annotations

from flask import abort, flash, redirect

from .. import unsent
from .common import author, get_conn, login_required
from .routes_orders import local_iso


def register(app) -> None:
    def unsent_for(order_id) -> list:
        try:
            return unsent.of_order(get_conn(), order_id)
        except Exception:                                  # the block never takes the card down
            get_conn().rollback()
            app.logger.exception("the unsent messages of order %s could not be read", order_id)
            return []

    app.jinja_env.globals["unsent_for"] = unsent_for

    @app.route("/orders/<int:card_id>/unsent/<kind>/<int:item_id>", methods=["POST"])
    @login_required
    def order_unsent_by_hand(card_id, kind, item_id):
        if kind not in ("send", "task"):
            abort(404)
        try:
            unsent.sent_by_hand(get_conn(), card_id, kind, item_id, local_iso(), author())
            flash("Отмечено: сообщение клиенту отправлено вручную.", "info")
        except unsent.UnsentError as err:
            flash(str(err), "error")
        return redirect("/orders/%d" % card_id)
