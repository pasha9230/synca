from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from flask import flash, redirect, render_template, request

from .. import connections, store
from ..secretbox import SecretKeyError
from ..suppliers import unload_path_for
from ..transport import urllib_transport
from ..webmim_client import WebmimError
from ..fetcher import fetch_for_connection
from . import feed_update
from .common import author, get_conn, login_required, now, now_iso
from .routes_drafts import apply_publish
from .routes_main import context


def register(app) -> None:
    def preview():
        conn, settings, parsed, ref = context(app)
        return feed_update.build_preview(conn, parsed, ref, settings, now().date(), app.config.get("FEED_ROOT", ""),
                                         now=now() + timedelta(hours=3)), conn

    @app.route("/feed/update")
    @login_required
    def feed_update_page():
        data, _conn = preview()
        return render_template("feed_update.html", p=data, loaded=feed_update.unload_age(app.config["UNLOAD_PATH"]),
                               any_live=any(a["live"] for a in data["accounts"]))

    @app.route("/feed/update/fetch", methods=["POST"])
    @login_required
    def feed_update_fetch():
        conn = get_conn()
        box = app.config.get("SECRET_BOX")
        if box is None:
            flash("Мастер-ключ недоступен: выгрузку из WebMIM скачать нельзя.")
            return redirect("/feed/update")
        done, problems = [], []
        try:
            with feed_update.fetch_lock(Path(app.config["UNLOAD_PATH"]).parent):
                for supplier in connections.list_suppliers(conn):
                    sid = supplier["id"]
                    config = connections.get_config(conn, "webmim", connections.supplier_owner(sid))
                    if sid != 1 and not config.get("base_url"):
                        continue
                    try:
                        meta = fetch_for_connection(conn, box, unload_path_for(Path(app.config["UNLOAD_PATH"]), sid), now_iso(),
                                                    http=app.config.get("WEBMIM_HTTP") or urllib_transport, supplier_id=sid)
                        done.append("%s: %d товаров" % (supplier["name"], meta["items"]))
                    except (WebmimError, SecretKeyError, OSError, ValueError) as err:
                        problems.append("%s: %s" % (supplier["name"], err))
        except feed_update.FetchBusy:
            flash("Выгрузка уже скачивается: подождите минуту и обновите страницу.")
            return redirect("/feed/update")
        if done:
            flash("Выгрузка скачана. " + "; ".join(done) + ".")
        for line in problems:
            flash("Не удалось скачать выгрузку, предпросмотр построен по прежней. %s" % line)
        return redirect("/feed/update")

    @app.route("/feed/update/confirm", methods=["POST"])
    @login_required
    def feed_update_confirm():
        data, conn = preview()
        if request.form.get("rev") != data["rev"]:
            flash("Данные изменились с момента предпросмотра. Проверьте обновлённый список и подтвердите снова.")
            return redirect("/feed/update")
        if data["blocked"]:
            flash("Обновление остановлено: см. предупреждения ниже.")
            return redirect("/feed/update")
        if data["over"] and request.form.get("understand") != "1":
            flash("Исчезает много объявлений: поставьте галочку «Я понимаю».")
            return redirect("/feed/update")
        rev = data["rev"]

        def verify():
            """Runs inside the feed lock, right before the rebuild: what the owner saw must still be what will be built."""
            again, _conn = preview()
            if again["rev"] != rev or again["blocked"]:
                return "Данные изменились с момента предпросмотра: фид не обновлён. Откройте предпросмотр заново."
            return None

        lines = apply_publish(app, verify=verify, upload=request.form.get("upload") == "1")
        failed = any(line.startswith(("Фид не пересобран", "Каталог фидов", "Данные изменились")) or "фид не собран" in line for line in lines)
        store.set_state(conn, "last_publish", json.dumps({"at": now_iso(), "lines": lines}, ensure_ascii=False))
        store.write_audit(conn, now_iso(), author(), "feed_update_failed" if failed else "feed_updated_manually", {"lines": lines[:5]})
        return redirect("/publish/result")
