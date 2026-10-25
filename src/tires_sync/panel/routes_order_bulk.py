"""«Закрыть заказы разом»: pick the cards and the stage, see what moves and what does not, confirm. See order_bulk."""
from __future__ import annotations

from flask import flash, redirect, render_template, request

from .. import order_bulk, orders
from ..orders import OrderError
from .common import author, get_conn, login_required
from .routes_orders import _int, local_iso


def _ids(values) -> list:
    out = []
    for value in values:
        for part in str(value).split(","):
            number = _int(part)
            if number:
                out.append(number)
    return out


def register(app) -> None:
    @app.route("/orders/bulk")
    @login_required
    def orders_bulk_page():
        conn = get_conn()
        everything = request.args.get("from") == "all"                       # only «Новый заказ» unless all are asked for on purpose
        from_stage = None if everything else (_int(request.args.get("from")) or orders._first_stage(conn)["id"])
        cards = order_bulk.open_cards(conn, from_stage)
        targets = order_bulk.targets(conn)
        to = _int(request.args.get("to")) or next((s["id"] for s in targets if s["code"] == "cancel"), None)
        return render_template("orders_bulk.html", cards=cards[:order_bulk.LIMIT], more=max(len(cards) - order_bulk.LIMIT, 0),
                               stages=[s for s in orders.list_stages(conn) if s["kind"] == "open"], from_stage=from_stage, targets=targets, to=to, runs=order_bulk.runs(conn),
                               reason=request.args.get("reason") or order_bulk.DEFAULT_REASON)

    @app.route("/orders/bulk/check", methods=["POST"])
    @login_required
    def orders_bulk_check():
        conn = get_conn()
        ids, to, reason = _ids(request.form.getlist("id")), _int(request.form.get("to")), request.form.get("reason", "").strip()[:200]
        if not ids or not to:
            flash("Отметьте карточки и выберите этап.")
            return redirect("/orders/bulk")
        try:
            plan = order_bulk.check(conn, ids, to, reason)
        except OrderError as err:
            flash(str(err))
            return redirect("/orders/bulk")
        return render_template("orders_bulk_check.html", plan=plan, reason=reason, ids=",".join(str(c["id"]) for c in plan["ok"]))

    @app.route("/orders/bulk/run", methods=["POST"])
    @login_required
    def orders_bulk_run():
        conn = get_conn()
        ids, to = _ids([request.form.get("ids", "")]), _int(request.form.get("to"))
        try:
            done = order_bulk.run(conn, app.config["DB_PATH"], ids, to, request.form.get("reason", ""), request.form.get("expected", ""),
                                  local_iso(), author())
        except OrderError as err:
            flash(str(err))
            return redirect("/orders/bulk")
        flash("Переведено в «%s»: %d%s%s. Перед этим снята копия базы %s." % (
            done["stage"], done["moved"], " (%d изменили по ходу, они остались)" % done["skipped"] if done["skipped"] else "",
            "; база была занята, %d не переведены, повторите" % done["left"] if done["left"] else "", done["backup"]))
        return redirect("/orders?v=list")

    @app.route("/orders/bulk/undo/<int:audit_id>")
    @login_required
    def orders_bulk_undo_page(audit_id):
        try:
            plan = order_bulk.undo_check(get_conn(), audit_id)
        except OrderError as err:
            flash(str(err))
            return redirect("/orders/bulk")
        return render_template("orders_bulk_undo.html", plan=plan)

    @app.route("/orders/bulk/undo/<int:audit_id>", methods=["POST"])
    @login_required
    def orders_bulk_undo_run(audit_id):
        try:
            done = order_bulk.undo(get_conn(), app.config["DB_PATH"], audit_id, _ids(request.form.getlist("id")), local_iso(), author())
        except OrderError as err:
            flash(str(err))
            return redirect("/orders/bulk/undo/%d" % audit_id)
        flash("Возвращено на прежние этапы: %d%s%s. Перед этим снята копия базы %s." % (
            done["back"], " (%d не вернулись: их меняли по ходу)" % done["skipped"] if done["skipped"] else "",
            "; база была занята, %d не возвращены, повторите" % done["left"] if done["left"] else "", done["backup"]))
        return redirect("/orders?v=list")
