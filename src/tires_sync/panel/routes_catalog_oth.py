"""«Товар и фид» → «На ОТХ»: the tyres of the orders the clients refused, kept by us (order_oth). The mark «Вернуть поставщику» is ours only.
«Свой остаток (ОТХ)» of the catalog card (oth_manual, the owner 2026-11-03): the owner and the partner, from the office."""
from __future__ import annotations

from urllib.parse import quote

from flask import abort, flash, redirect, render_template, request

from .. import connections, oth_manual, oth_stock, order_oth, store
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
        for account in connections.list_feed_accounts(conn):
            found = account_status(conn, goods_id, account["id"], parsed, has_right("money"))
            if found is not None:
                status.append({"account": connections.display_name(conn, account["id"]), "text": found[1], "ok": found[0]})
    return {"rows": rows, "points": points, "log": oth_manual.log_of(conn, goods_id, 20), "in_unload": in_unload, "status": status,
            "can_edit": can_post("catalog_oth_manual")}


def account_status(conn, goods_id: str, account_id: int, parsed, money: bool = True):
    """(goes to Avito, why) of a position on «Свой остаток» in one feed account, by the cycle's own rules (sync._manual_gone, the Id guard
    of sync._oth_for_account); None: the cycle does not take it for gone (in the unload, a broken row, its supplier's file failed)."""
    from .. import mirror_feed, sync, tochki_feed
    from ..overrides import override_key
    eff = connections.effective_settings(conn, account_id, active_settings(conn))
    total, by_orders = oth_stock.pieces(conn).get(goods_id, 0), oth_stock.pieces(conn, manual=False)
    tochki = tochki_feed.config(conn)["supplier_id"]
    left_out = frozenset(connections.get_excluded_types(conn, account_id))
    offers, prices, drop, no_price = sync._manual_gone(conn, parsed, {goods_id: total}, by_orders, eff,
                                                      tochki if tochki_feed.trial(conn, account_id) is None else None, left_out)
    if goods_id in no_price:
        own = (eff.own_prices or {}).get(goods_id)
        return False, ("своя цена в процентах к закупке, а последней закупки в архиве каталога нет: поставьте готовую цену" if own
                       else "нужна своя цена: без неё позиция, которой нет у поставщика, на Авито не уходит")
    if goods_id in drop:
        return False, "позиция снята настройкой аккаунта (исключённый тип шин или 4 точки вне пробы)"
    if goods_id not in prices:
        return None
    if eff.overrides.get(override_key(goods_id)) == "off":
        return False, "на позиции стоит ручное «выкл»"
    offer = offers.get(goods_id)
    published = goods_id in store.load_published(conn, account_id)
    before = store.retired_ad_of(conn, account_id, goods_id)
    if offer is None and not (published or before):
        return False, "не из чего собрать объявление: позиции нет ни в архиве каталога, ни среди сохранённых объявлений"
    if offer is None and not published and (not sync._removed_for_stock(conn, account_id, goods_id)
                                            or sync._off_by_the_owner(goods_id, before[1], eff, left_out, True)):
        return False, "сохранённое объявление снято настройкой (не по остатку) — так оно и остаётся снятым"
    if offer is not None and not published and not before:
        return False, "раньше не публиковалась в этом аккаунте — на Авито не уходит (новый Id не выдаём)"
    if offer is not None and not published and before:
        id_map = dict(mirror_feed.adopted_maps(conn, account_id)[0])
        if mirror_feed.alias_scheme(conn, account_id) and goods_id not in id_map:
            id_map[goods_id] = mirror_feed.alias_id(account_id, goods_id)
        suffix = "" if account_id == connections.PRIMARY_ACCOUNT else "_a%d" % account_id
        now_id = sync._final_id(goods_id, id_map, suffix, mirror_feed.reissued_own(conn, account_id), account_id)
        if sync.REISSUE_MARK.sub("", before[0]) != sync.REISSUE_MARK.sub("", now_id):
            return False, "прежний Id объявления (%s) не совпадает с нынешним: новый Id не выдаём" % before[0]
    return True, ("уйдёт на Авито по своей цене" + (" %d ₽" % prices[goods_id] if money else "")
                  + (" (из архива каталога)" if offer is not None else " (по сохранённому объявлению)")
                  + "; лимит возвратов ОТХ и «одна шина — одно объявление» действуют")


def manual_list(conn) -> list:
    """The rows of «Свой остаток» for the page «На ОТХ» (no test point), with the name and code of the position as the catalog knows them."""
    from .. import catalog_archive
    from flask import current_app
    from .routes_main import context
    test = oth_stock._test_points(conn)
    offers = getattr(context(current_app)[2], "offers", {}) or {}
    out = []
    for r in oth_manual.rows(conn):
        if r["point_id"] in test:
            continue
        offer = offers.get(r["goods_id"]) or catalog_archive.last_offer(conn, r["goods_id"])
        out.append(dict(r, name=(getattr(offer, "name_orig", "") or getattr(offer, "name", "")) if offer is not None else r["goods_id"],
                        code=getattr(offer, "code", "") if offer is not None else "", in_unload=r["goods_id"] in offers,
                        url="/catalog/item/" + quote(r["goods_id"], safe="")))
    return out


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
            note = request.form.get("note")
            before = oth_manual.set_qty(conn, goods_id, point, qty, now_iso(), author(),
                                        note if note and note.strip() else None)        # no note typed: the row's note stays
            flash("Свой остаток: %d → %d шт." % (before, qty) if before != qty else "Свой остаток не изменился.")
        except oth_manual.ManualError as err:
            flash(str(err))
        except ValueError:
            flash("Неизвестная точка.")
        return redirect("/catalog/item/" + quote(goods_id, safe=""))

    @app.route("/oth")
    @login_required
    def oth_page():
        """The orders' tyres on «На ОТХ» and «Свой остаток» (the owner 2026-11-03: its own rows, each opening the card to change it);
        `?point=<id>` shows one point (both lists)."""
        conn = get_conn()
        data = order_oth.stock(conn)
        own = manual_list(conn)
        test = oth_stock._test_points(conn)
        places = {(r["point_id"], r["point"] or "") for r in data["stock"] + data["returned"] + own if r["point_id"] and r["point_id"] not in test}
        point = request.args.get("point", type=int)
        if point not in {p[0] for p in places}:                       # an unknown point, or one with nothing left: the whole page
            point = None
        if point:
            data = {k: [r for r in rows if r["point_id"] == point] for k, rows in data.items()}
            own = [r for r in own if r["point_id"] == point]
        return render_template("oth.html", manual=own, places=sorted(places, key=lambda p: (p[1].casefold(), p[0])), point=point, **data)

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
