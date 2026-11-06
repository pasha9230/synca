"""«Автосообщение клиенту» in the order card and in the chat of an order (order_autosend, the owner 2026-11-05/06): set, change, cancel.
Section «Заказы», the right «send» (who may write to clients). A manager of a point never sees it: the endpoints are not in POINT_PAGES
(common.check_access answers 403) and the block is empty for them. Nothing is sent from here: the minute's sync sends it at its time."""
from __future__ import annotations

from flask import flash, redirect, request

from .. import order_autosend
from .common import author, can_post, get_conn, login_required, own_point
from .routes_orders import local_iso

ENDPOINTS = ("order_autosend_add", "order_autosend_update", "order_autosend_cancel")


def register(app) -> None:
    def autosend_for(order_id) -> dict:
        """The block's values: the order's auto messages and the default texts; nothing for a point or who may not set them."""
        if own_point() is not None or not can_post("order_autosend_add"):
            return {}
        conn = get_conn()
        try:
            return {"rows": order_autosend.of_order(conn, order_id), "kinds": order_autosend.KINDS,
                    "texts": {k: order_autosend.default_text(conn, k) for k in order_autosend.KINDS}}
        except Exception:                                  # the block never takes the page down
            conn.rollback()
            app.logger.exception("the auto messages of order %s could not be read", order_id)
            return {}

    app.jinja_env.globals["autosend_for"] = autosend_for

    def back(card_id):
        chat = request.form.get("chat", "")
        if chat.isascii() and chat.isdigit():
            return redirect("/chats/%d#autosend" % int(chat))
        return redirect("/orders/%d#autosend" % card_id)

    def done(card_id, action, said):
        try:
            action()
            flash(said, "info")
        except (order_autosend.AutosendError, ValueError) as err:
            flash(str(err), "error")
        return back(card_id)

    @app.route("/orders/<int:card_id>/autosend", methods=["POST"])
    @login_required
    def order_autosend_add(card_id):
        form = request.form
        return done(card_id, lambda: order_autosend.add(get_conn(), card_id, form.get("kind", ""), form.get("due_at", ""), form.get("text", ""),
                                                        local_iso(), author()), "Автосообщение поставлено.")

    @app.route("/orders/<int:card_id>/autosend/<int:autosend_id>/update", methods=["POST"])
    @login_required
    def order_autosend_update(card_id, autosend_id):
        form = request.form
        return done(card_id, lambda: order_autosend.update(get_conn(), autosend_id, form.get("due_at", ""), form.get("text", ""), local_iso(),
                                                           author(), order_id=card_id), "Автосообщение изменено.")

    @app.route("/orders/<int:card_id>/autosend/<int:autosend_id>/cancel", methods=["POST"])
    @login_required
    def order_autosend_cancel(card_id, autosend_id):
        return done(card_id, lambda: order_autosend.cancel(get_conn(), autosend_id, local_iso(), author(), order_id=card_id), "Автосообщение отменено.")
