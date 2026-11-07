"""«Письмо поставщику» (supplier_mail, the owner 2026-11-07): the button's letter for the order card on «На возврат» and the return task of
«Задачи», the press noted in the order's journal, «Почта для возвратов» of the supplier's settings. Section «Заказы» to press, «Настройки» to
set the address. A manager of a point never sees it: the endpoints are not in POINT_PAGES (common.check_access answers 403) and the letter is
empty for them. Nothing is sent from here."""
from __future__ import annotations

from flask import flash, redirect, request

from .. import orders, supplier_mail
from .common import author, can_post, get_conn, login_required, own_point
from .routes_orders import local_iso


def register(app) -> None:
    def supplier_mail_for(order_id) -> dict:
        """The letter of an order on «На возврат» ({} for a point, for who may not press it, for another stage)."""
        if own_point() is not None or not can_post("order_supplier_mail") or not order_id:
            return {}
        conn = get_conn()
        try:
            card = orders.get_card(conn, int(order_id))
            return supplier_mail.draft(conn, card) if card["stage"]["code"] == supplier_mail.STAGE else {}
        except Exception:                                  # the button never takes the page down
            conn.rollback()
            app.logger.exception("the supplier letter of order %s could not be made", order_id)
            return {}

    app.jinja_env.globals["supplier_mail_for"] = supplier_mail_for

    @app.route("/orders/<int:card_id>/supplier-mail", methods=["POST"])
    @login_required
    def order_supplier_mail(card_id):
        """The press (app.js, data-mail-log, beside the «mailto:» link that opens the letter): a record of the journal, nothing else."""
        try:
            why = supplier_mail.note_press(get_conn(), card_id, local_iso(), author())
        except orders.OrderError:
            return "", 404
        return ("", 409) if why else ("", 204)

    @app.route("/connections/suppliers/<int:supplier_id>/email", methods=["POST"])
    @login_required
    def connections_supplier_email(supplier_id):
        from .. import connections
        if connections.get_supplier(get_conn(), supplier_id) is None:
            return "", 404
        try:
            supplier_mail.set_email(get_conn(), supplier_id, request.form.get("return_email", ""), local_iso(), author())
            flash("Почта для возвратов сохранена.")
        except ValueError as err:
            flash(str(err))
        return redirect("/connections/webmim/%d#return-email" % supplier_id)
