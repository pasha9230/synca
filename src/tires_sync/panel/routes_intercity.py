"""«Доставка в другой город» in the order card (order_intercity, the owner 2026-11-02): the checkbox, the city, the transport company, the
track number and the PDF labels. Section «Заказы»: the office writes them (owner, partner, the chats' manager: «менять» and not a point —
common.POINT_PAGES leaves the writing out); the labels are downloaded by whoever sees the order, a manager of a point by its own orders only
(common.check_access: `card_id` of the address). Nothing is sent from here."""
from __future__ import annotations

from urllib.parse import quote

from flask import Response, abort, flash, redirect, request

from .. import order_intercity as intercity, orders
from .common import author, get_conn, login_required
from .routes_orders import card_or_404, local_iso


def register(app) -> None:
    def intercity_labels(order_id) -> list:
        try:
            return intercity.labels(get_conn(), order_id)
        except Exception:                                  # the block never takes the card down
            get_conn().rollback()
            app.logger.exception("the labels of order %s could not be read", order_id)
            return []

    app.jinja_env.globals["intercity_labels"] = intercity_labels
    app.jinja_env.globals["intercity_carriers"] = intercity.CARRIERS
    app.jinja_env.globals["intercity_ids"] = lambda: intercity.enabled_ids(get_conn())     # the tag «В другой город» of the list of orders

    def back(card_id):
        return redirect("/orders/%d#intercity" % card_id)

    @app.route("/orders/<int:card_id>/intercity", methods=["POST"])
    @login_required
    def order_intercity_save(card_id):
        conn = get_conn()
        card_or_404(conn, card_id)
        try:
            changed = intercity.save(conn, card_id, request.form, local_iso(), author())
        except orders.OrderError as err:
            flash(str(err), "error")
            return back(card_id)
        flash("Сохранено: %s." % ", ".join(changed) if changed else "Ничего не изменилось.", "ok" if changed else "info")
        return back(card_id)

    @app.route("/orders/<int:card_id>/labels", methods=["POST"])
    @login_required
    def order_label_upload(card_id):
        conn = get_conn()
        card_or_404(conn, card_id)
        upload = request.files.get("file")
        data = upload.read(intercity.LABEL_MAX + 1) if upload else b""
        try:
            intercity.add_label(conn, card_id, upload.filename if upload else "", data, local_iso(), author())
        except orders.OrderError as err:
            flash(str(err), "error")
            return back(card_id)
        flash("Этикетки загружены.", "ok")
        return back(card_id)

    @app.route("/orders/<int:card_id>/labels/<int:label_id>/delete", methods=["POST"])
    @login_required
    def order_label_delete(card_id, label_id):
        conn = get_conn()
        try:
            intercity.delete_label(conn, card_id, label_id, local_iso(), author())
        except orders.OrderError as err:
            flash(str(err), "error")
            return back(card_id)
        flash("Файл этикеток удалён.", "ok")
        return back(card_id)

    @app.route("/orders/<int:card_id>/labels/<int:label_id>")
    @login_required
    def order_label_file(card_id, label_id):
        try:
            name, data = intercity.label_file(get_conn(), card_id, label_id)
        except orders.OrderError:
            abort(404)
        return Response(data, mimetype="application/pdf", headers={
            "Content-Disposition": "attachment; filename=\"labels-%d.pdf\"; filename*=UTF-8''%s" % (label_id, quote(name)),
            "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})
