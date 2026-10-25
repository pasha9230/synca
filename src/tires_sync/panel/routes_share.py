"""Links for clients: creation and management (behind the login) and the public page (no login, no cookies)."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from flask import abort, flash, make_response, redirect, render_template, request, send_file, session

from .. import share, share_photos, store
from .common import author, get_conn, login_required, now, now_iso
from .routes_main import context
from . import service


def share_root(app) -> str:
    configured = app.config.get("SHARE_ROOT")
    if configured:
        return configured
    feed_root = app.config.get("FEED_ROOT")
    return str(Path(feed_root).parent / "share") if feed_root else str(Path(app.config["DB_PATH"]).parent / "share")


def register(app) -> None:
    def snapshot(goods_ids):
        conn, settings, parsed, ref = context(app)
        return service.share_snapshot(conn, settings, parsed, ref, goods_ids)

    @app.route("/shares/new", methods=["POST"])
    @login_required
    def share_new():
        ids = [g for g in request.form.getlist("g") if g]
        if not ids:
            flash("Отметьте в каталоге позиции для ссылки.")
            return redirect("/catalog")
        items, skipped = snapshot(ids)
        return render_template("share_new.html", items=items, skipped=skipped, ids=[i for i in dict.fromkeys(ids)],
                               limit=share.MAX_ITEMS, too_many=len(items) > share.MAX_ITEMS)

    @app.route("/shares", methods=["POST"])
    @login_required
    def share_create():
        conn = get_conn()
        ids = [g for g in request.form.getlist("g") if g]
        items, skipped = snapshot(ids)
        fetch = app.config.get("PHOTO_FETCH") or share_photos.fetch
        try:
            share.purge(conn, share_root(app), now())
            token, link_id, no_photo = share.create(
                conn, share_root(app), items, request.form.get("show_price") == "1", request.form.get("note", ""), now(), author(), fetch=fetch)
        except share.ShareError as err:
            flash(str(err))
            return redirect("/catalog")
        store.write_audit(conn, now_iso(), author(), "share_created", {"link": link_id, "items": len(items),
                                                                     "price": request.form.get("show_price") == "1"})
        # post-redirect-get: a refresh or «back» must not create a second link; the address is shown once and then forgotten
        session["share_created"] = {"url": request.host_url.rstrip("/") + "/s/" + token, "count": len(items), "no_photo": no_photo,
                                    "skipped": [list(pair) for pair in skipped], "show_price": request.form.get("show_price") == "1"}
        return redirect("/shares/created")

    @app.route("/shares/created")
    @login_required
    def share_created():
        shown = session.pop("share_created", None)
        if not shown:
            flash("Адрес ссылки показывается один раз, при создании. Потеряли его — отзовите ссылку и создайте новую.")
            return redirect("/shares")
        response = make_response(render_template("share_created.html", **shown))
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/shares")
    @login_required
    def share_list():
        conn = get_conn()
        share.purge(conn, share_root(app), now())
        return render_template("shares.html", links=share.list_links(conn, now()), ttl=share.TTL_DAYS)

    @app.route("/shares/<int:link_id>/revoke", methods=["POST"])
    @login_required
    def share_revoke(link_id):
        conn = get_conn()
        if share.revoke(conn, link_id, now()):
            store.write_audit(conn, now_iso(), author(), "share_revoked", {"link": link_id})
            flash("Ссылка отозвана: по ней больше ничего не открывается.")
        return redirect("/shares")

    @app.route("/shares/<int:link_id>/price", methods=["POST"])
    @login_required
    def share_price(link_id):
        conn = get_conn()
        show = request.form.get("show_price") == "1"
        if share.set_show_price(conn, link_id, show, now()):
            store.write_audit(conn, now_iso(), author(), "share_price_toggled", {"link": link_id, "show": show})
            flash("Цена теперь %s." % ("показывается" if show else "скрыта"))
        return redirect("/shares")

    # ---- public: no login, no session, nothing but the snapshot ----
    @app.route("/s/<token>")
    def share_public(token):
        conn = get_conn()
        link = share.find(conn, token, now())
        if link is None:
            return render_template("share_gone.html"), 404
        share.count_view(conn, link["id"], now())
        link["created_label"] = (datetime.fromisoformat(link["created_at"]) + timedelta(hours=3)).strftime("%d.%m.%Y %H:%M")
        return render_template("share_public.html", link=link, items=share.items_of(conn, link["id"]), token=token)

    @app.route("/s/<token>/p/<name>")
    def share_photo(token, name):
        conn = get_conn()
        link = share.find(conn, token, now())
        if link is None or not share.photo_of(conn, link["id"], name):
            abort(404)
        path = share.photo_path(share_root(app), link["id"], name)
        if path is None:
            abort(404)
        mimetype = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(name.rsplit(".", 1)[-1])
        if mimetype is None:
            abort(404)
        return send_file(path, mimetype=mimetype, max_age=0)
