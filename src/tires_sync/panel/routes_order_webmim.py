"""«Заказы WebMIM» in the order card (order_webmim, the owner 2026-11-07): the parts of the order by source, the numbers typed for one position
«с ОТХ» and the second order of WebMIM of the pieces added. Section «Заказы», «менять». A manager of a point sees the block read-only (numbers
only: nothing of money is in it); the forms are not theirs (not in POINT_PAGES: common.check_access answers 403). Nothing goes to WebMIM."""
from __future__ import annotations

from flask import abort, flash, redirect, request

from .. import order_webmim, orders
from .common import author, get_conn, login_required
from .routes_orders import local_iso


def register(app) -> None:
    def webmim_parts_for(card_id) -> list:
        conn = get_conn()
        try:
            return order_webmim.parts(conn, orders.get_card(conn, int(card_id)), take_snapshot=True, now=local_iso())
        except Exception:                                  # the block never takes the card down
            conn.rollback()
            app.logger.exception("«Заказы WebMIM» of order %s could not be read", card_id)
            return []

    app.jinja_env.globals["webmim_parts_for"] = webmim_parts_for

    def done(card_id, action, said):
        try:
            action()
            flash(said)
        except orders.OrderError as err:
            get_conn().rollback()
            flash(str(err))
        return redirect("/orders/%d#webmim-parts" % card_id)

    @app.route("/orders/<int:card_id>/items/<int:item_id>/mim", methods=["POST"])
    @login_required
    def order_item_mim(card_id, item_id):
        if conn_card(card_id) is None:
            abort(404)
        return done(card_id, lambda: order_webmim.set_item_numbers(get_conn(), card_id, item_id, request.form.get("webmim_number", ""),
                                                                    request.form.get("transfer_number", ""), local_iso(), author()),
                    "Номера строки сохранены.")

    @app.route("/orders/<int:card_id>/items/<int:item_id>/mim-extra", methods=["POST"])
    @login_required
    def order_item_mim_extra(card_id, item_id):
        if conn_card(card_id) is None:
            abort(404)
        return done(card_id, lambda: order_webmim.set_extra_numbers(get_conn(), card_id, item_id, request.form.get("webmim_number", ""),
                                                                     request.form.get("transfer_number", ""), local_iso(), author()),
                    "Номера второго заказа WebMIM сохранены.")

    def conn_card(card_id):
        try:
            return orders.get_card(get_conn(), card_id)
        except orders.OrderError:
            return None
