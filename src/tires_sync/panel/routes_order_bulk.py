"""«Закрыть заказы разом»: pick the cards and the stage, see what moves and what does not, confirm. See order_bulk."""
from __future__ import annotations

from flask import flash, redirect, render_template, request

from .. import order_bulk, orders, oth_sale, trash
from ..orders import OrderError
from .common import author, get_conn, has_right, login_required, safe_path
from .routes_orders import _int, check_responsible, local_iso


def _ids(values) -> list:
    out = []
    for value in values:
        for part in str(value).split(","):
            number = _int(part)
            if number:
                out.append(number)
    return out


def _back():
    """Back to «Список» with its filters (the form's `back`, a path of «Заказы» only)."""
    target = safe_path(request.form.get("back", ""))
    return redirect(target if target.startswith("/orders") else "/orders?v=list")


def _each(ids, change) -> tuple:
    """`change(card)` for every picked order still there (deleted meanwhile: skipped): (done, [refusals «№N: why»])."""
    done, refused = 0, []
    for card_id in sorted(set(ids))[:order_bulk.LIMIT]:
        try:
            card = orders.get_card(get_conn(), card_id)
        except OrderError:
            continue
        try:
            done += 1 if change(card) else 0
        except OrderError as err:
            refused.append("№%d: %s" % (card_id, err))
    return done, refused


def _said(what: str, done: int, refused: list) -> None:
    flash("%s: %d." % (what, done), "ok" if done else "info")
    if refused:
        flash("Не изменены (%d): %s" % (len(refused), "; ".join(refused[:10]) + (" …" if len(refused) > 10 else "")), "error")


def register(app) -> None:
    # the side panel of «Список» in «Новый вид» (lane port-office-orders, the prototype's variant 3): the tag, the responsible and «Удалить» of
    # the selected orders — each order exactly as its own card does it (orders.update_card, check_responsible, trash.delete_order);
    # «Перевести на этап» is «Закрыть разом»'s check above, «Excel» is /orders/export.xlsx?ids=
    @app.route("/orders/bulk/tag", methods=["POST"])
    @login_required
    def orders_bulk_tag():
        ids, tag, on = _ids(request.form.getlist("id")), " ".join(request.form.get("tag", "").split()), request.form.get("mode") != "off"
        if not ids or not tag:
            flash("Отметьте заказы и впишите тег.", "error")
            return _back()

        def change(card):
            tags = [t for t in card["tags"] if t != tag] + ([tag] if on else [])
            if tags == card["tags"]:
                return False
            return bool(orders.update_card(get_conn(), card["id"], {"tags": ",".join(tags)}, local_iso(), author()))
        done, refused = _each(ids, change)
        _said(("Поставлен тег «%s»" if on else "Снят тег «%s»") % tag, done, refused)
        return _back()

    @app.route("/orders/bulk/responsible", methods=["POST"])
    @login_required
    def orders_bulk_responsible():
        ids, login = _ids(request.form.getlist("id")), request.form.get("responsible", "").strip()
        if not ids or not login:
            flash("Отметьте заказы и выберите ответственного.", "error")
            return _back()

        def change(card):
            if card["responsible"] == login:
                return False
            check_responsible(app, get_conn(), login, card["responsible"])
            return bool(orders.update_card(get_conn(), card["id"], {"responsible": login}, local_iso(), author()))
        done, refused = _each(ids, change)
        _said("Ответственный сменён", done, refused)
        return _back()

    @app.route("/orders/bulk/delete", methods=["POST"])
    @login_required
    def orders_bulk_delete():
        if not has_right("bulk"):                                        # check_access asked «delete» (NEEDS); many at once: «bulk» too
            flash("Удалять разом может тот, у кого есть право массовых действий.", "error")
            return _back()
        ids = _ids(request.form.getlist("id"))
        if not ids:
            flash("Отметьте заказы.", "error")
            return _back()

        def change(card):
            trash.delete_order(get_conn(), card["id"], local_iso(), author(), only_if=lambda c, _card: oth_sale.why_not_delete(c, card["id"]))
            return True
        done, refused = _each(ids, change)
        _said("Удалено в «Удалённые» (вернуть можно там)", done, refused)
        return _back()

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
