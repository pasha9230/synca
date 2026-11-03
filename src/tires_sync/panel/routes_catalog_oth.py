"""«Товар и фид» → «На ОТХ»: the tyres of the orders the clients refused, kept by us (order_oth). The mark «Вернуть поставщику» is ours only.
«Свой остаток (ОТХ)» of the catalog card (oth_manual, the owner 2026-11-03): the owner and the partner, from the office."""
from __future__ import annotations

from urllib.parse import quote

from flask import abort, flash, redirect, render_template, request

from .. import catalog_archive, connections, oth_manual, oth_stock, order_oth, store
from ..settings_store import active_settings
from .common import author, can_post, get_conn, has_right, login_required, now_iso


def manual_block(goods_id: str) -> dict:
    """What the card shows of «Свой остаток (ОТХ)»: the rows, the points one may enter, the log and, per feed account, whether the position
    goes to Avito on it (a position the supplier has no more: only with an own price and something to build the ad from)."""
    from flask import current_app
    from .routes_main import context
    conn = get_conn()
    goods_id = str(goods_id or "")
    rows = oth_manual.rows(conn, goods_id)
    test = oth_stock._test_points(conn)
    points = [{"id": r[0], "name": r[1]} for r in conn.execute("SELECT id, name FROM point ORDER BY name, id") if r[0] not in test]
    parsed = context(current_app)[2]
    in_unload = goods_id in (getattr(parsed, "offers", {}) or {})
    status = []
    if rows and not in_unload:
        general = active_settings(conn)
        offer = catalog_archive.last_offer(conn, goods_id)
        for account in connections.list_feed_accounts(conn):
            eff = connections.effective_settings(conn, account["id"], general)
            price = oth_manual.gone_price(conn, goods_id, eff)
            saved = store.get_snapshot(conn, goods_id, account["id"]) or (store.retired_ad_of(conn, account["id"], goods_id) or (None, None))[1]
            if price is None:
                text, ok = "нужна своя цена: без неё позиция, которой нет у поставщика, на Авито не уходит", False
            elif offer is None and not saved:
                text, ok = "не из чего собрать объявление: позиции нет ни в архиве каталога, ни среди сохранённых объявлений", False
            else:
                text, ok = ("уйдёт на Авито по своей цене" + (" %d ₽" % price if has_right("money") else "")
                            + (" (из архива каталога)" if offer is not None else " (по сохранённому объявлению)")), True
            status.append({"account": connections.display_name(conn, account["id"]), "text": text, "ok": ok})
    return {"rows": rows, "points": points, "log": oth_manual.log_of(conn, goods_id, 20), "in_unload": in_unload, "status": status,
            "can_edit": can_post("catalog_oth_manual")}


def _known(conn, goods_id: str) -> bool:
    """A position of the catalog: in the unload, in the archive, or with manual pieces already."""
    from flask import current_app
    from .routes_main import context
    return bool(goods_id) and (goods_id in (getattr(context(current_app)[2], "offers", {}) or {})
                               or conn.execute("SELECT 1 FROM catalog_item WHERE goods_id = ?", (goods_id,)).fetchone() is not None
                               or conn.execute("SELECT 1 FROM oth_manual WHERE goods_id = ?", (goods_id,)).fetchone() is not None)


def register(app) -> None:
    app.jinja_env.globals["oth_manual_block"] = manual_block

    @app.route("/catalog/item/<goods_id>/oth-manual", methods=["POST"])
    @login_required
    def catalog_oth_manual(goods_id):
        """Add, change or remove (0) the manual pieces of a position at a point. The rights: common.NEEDS (the owner and the partner)."""
        conn = get_conn()
        if not _known(conn, goods_id):
            abort(404)
        try:
            point = int(request.form.get("point") or 0)
            qty = 0 if request.form.get("action") == "remove" else oth_manual.pieces_of(request.form.get("qty"))
            before = oth_manual.set_qty(conn, goods_id, point, qty, now_iso(), author(), request.form.get("note") or "")
            flash("Свой остаток: %d → %d шт." % (before, qty) if before != qty else "Свой остаток не изменился.")
        except oth_manual.ManualError as err:
            flash(str(err))
        except ValueError:
            flash("Неизвестная точка.")
        return redirect("/catalog/item/" + quote(goods_id, safe=""))

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
