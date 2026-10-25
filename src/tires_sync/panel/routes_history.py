from __future__ import annotations

from datetime import timedelta

from flask import flash, redirect, render_template, request

from .. import store
from ..publishing import remember_expected
from ..settings_store import list_versions, rollback, rollback_needs_strong
from .common import author, get_conn, login_required, now, now_iso
from .routes_main import context
from . import service


def register(app) -> None:
    register_ads(app)

    @app.route("/history")
    @login_required
    def history():
        conn = get_conn()
        kind = request.args.get("f") if request.args.get("f") in ("mine", "system", "errors") else ""
        chips = [{"label": label, "on": key == kind, "url": "/history" + ("?f=" + key if key else "")}
                 for key, label in (("", "Все"), ("mine", "Мои действия"), ("system", "Система"), ("errors", "Ошибки"))]
        return render_template("history.html", versions=list_versions(conn), chips=chips, kind=kind,
                               audit=store.list_audit(conn, 50, kind=kind, author=author()),
                               last_publish=service.last_publish(conn))

    @app.route("/history/rollback/<int:version>", methods=["POST"])
    @login_required
    def history_rollback(version):
        if request.form.get("confirm") != "on":
            flash("Откат не выполнен: отметьте подтверждение.")
            return redirect("/history")
        try:
            conn, _settings, parsed, ref = context(app)
            effect = service.rollback_effect(conn, version, parsed, ref, now().date(), now() + timedelta(hours=3))
            if effect["over"] and request.form.get("understand") != "on":
                flash("Откат уберёт %d объявлений, это больше порога защиты (%d). Повторите с отметкой «Я понимаю»." % (
                    effect["gone"], effect["limit"]))
                return redirect("/history")
            if rollback_needs_strong(get_conn(), version) and request.form.get("understand") != "on":
                flash("В этой версии есть проценты выше текущего лимита. Откат возможен только с отметкой «Я понимаю».")
                return redirect("/history")
            new_version = rollback(get_conn(), version, now_iso(), author())
            if effect["gone"]:
                remember_expected(get_conn(), new_version, effect["gone_ads"])
            flash("Откат выполнен: создана новая версия настроек.", "ok")
        except ValueError as err:
            flash(str(err))
        return redirect("/history")


EVENT_LABELS = {"published": "Опубликованы", "waiting": "Ждали или удержаны", "returned": "Вернулись", "removed": "Сняты"}
EVENT_VIEW = {"published": ("Опубликована", "ok"), "waiting": ("Ждёт", "warn"), "returned": ("Вернулась", "ok"), "removed": ("Снята", "neutral")}
PAGE = 100


def _why(event: str, reason: str) -> str:
    from .feed_update import reason_text
    if not reason:
        return ""
    head, _, tail = reason.partition(":")
    if head == "cart_wait":
        return "Пропала днём: ждём утреннего наполнения каталога (%s)" % reason_text(tail)
    if head == "guard":
        return "Удержана защитой от массового снятия (%s)" % reason_text(tail)
    return reason_text(reason)


def register_ads(app) -> None:
    from datetime import timedelta
    from urllib.parse import urlencode

    @app.route("/history/ads")
    @login_required
    def history_ads():
        conn = get_conn()
        kind = request.args.get("f") if request.args.get("f") in EVENT_LABELS else ""
        q = request.args.get("q", "").strip()[:80]
        try:
            page = max(int(request.args.get("pg", "1")), 1)
        except ValueError:
            page = 1
        where, args = [], []
        if kind:
            where.append("event = ?")
            args.append(kind)
        if q:
            conn.create_function("fold", 1, lambda text: (text or "").casefold())
            where.append("(fold(name) LIKE ? ESCAPE '\\' OR fold(goods_id) LIKE ? ESCAPE '\\')")
            like = "%" + q.casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            args += [like, like]
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        total = conn.execute("SELECT COUNT(*) FROM ad_event" + clause, args).fetchone()[0]
        pages = max(1, -(-total // PAGE))
        page = min(page, pages)
        rows = []
        for at, goods_id, name, event, reason in conn.execute(
                "SELECT at, goods_id, name, event, reason FROM ad_event" + clause + " ORDER BY id DESC LIMIT ? OFFSET ?", args + [PAGE, (page - 1) * PAGE]):
            label, tone = EVENT_VIEW.get(event, (event, "neutral"))
            rows.append({"at": at, "goods_id": goods_id, "name": name, "label": label, "tone": tone, "why": _why(event, reason)})
        border = (now() + timedelta(hours=3) - timedelta(days=7)).isoformat(timespec="seconds")
        week = {key: 0 for key in EVENT_LABELS}
        for event, count in conn.execute("SELECT event, COUNT(*) FROM ad_event WHERE at >= ? GROUP BY event", (border,)):
            week[event] = count
        first = conn.execute("SELECT MIN(at) FROM ad_event").fetchone()[0]
        base = {k: v for k, v in (("f", kind), ("q", q)) if v}
        pager = {n: "/history/ads?" + urlencode(dict(base, pg=n)) for n in range(1, pages + 1)} if pages > 1 else {}
        return render_template("history_ads.html", rows=rows, kind=kind, q=q, labels=EVENT_LABELS, week=week, page=page, pager=pager,
                               since=first[:10] if first else "")

    @app.route("/history/catalog")
    @login_required
    def history_catalog():
        from .. import catalog_probe
        conn = get_conn()
        days = 3
        since = (now() + timedelta(hours=3) - timedelta(days=days)).isoformat(timespec="seconds")
        probes = catalog_probe.list_probes(conn, since)
        summary = catalog_probe.refill_summary(catalog_probe.list_probes(conn, (now() + timedelta(hours=3) - timedelta(days=14)).isoformat(timespec="seconds")))
        return render_template("history_catalog.html", probes=list(reversed(probes)), summary=list(reversed(summary))[:7], days=days)
