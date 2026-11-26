"""«Уведомления точек» and «Сводки» of a point (the owner 2026-11-25; tires_sync.point_notify has the rules).

The point: /my-point/notify switches its own notifications (off: who does it, a name, is required), /my-point/notify/ask counts one ask
«Разрешите уведомления» (notify.js shows it only when the server says yes), /my-point/notify/answer records what the browser answered,
/my-point/notices is «Сводки» (the week, the return today, the office's tasks with «Прочитал» / «Выполнено», the alerts of the point).
Its own point only (own_point); nothing of the purchase or the prices.

The office: /points/notify (section «Заказы»: to look «смотреть», to switch «менять» — never a point, the observer only looks) lists the
points with their state, switches them, and shows the history of the switches and of the points' actions on «Сводки», by point and days."""
from __future__ import annotations

from datetime import date

from flask import abort, current_app, flash, g, jsonify, redirect, render_template, request

from .. import order_points as points, order_report, point_notify as pn, point_summary as ps, tasks, users
from . import income_view
from .common import author, can, current_user, get_conn, has_right, login_required, own_point, safe_path
from .routes_design import point_page
from .routes_orders import local_iso

POINT_OFFICE = "Офис"


def _day(value) -> str:
    text = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return ""


def _back(default: str) -> str:
    return safe_path(str(request.form.get("back") or "").strip()[:300]) or default


def _real_point() -> bool:
    """A manager of a point signed in as itself (not the owner or the partner working as a point): only it is asked for the browser."""
    user = current_user()
    return user["role"] == "point" and user["as_point"] is None


def notice_marks(point_id=None) -> dict:
    """The marks of the button «Сводки» (point_notify.marks) of `point_id`, the person's own point by default; once per request."""
    pid = own_point() if point_id is None else point_id
    cache = g.setdefault("notice_marks", {})
    if pid not in cache:
        cache[pid] = pn.marks(get_conn(), pid, local_iso()[:10])
    return cache[pid]


def _compare(conn, point_id, today: str, issued) -> dict:
    """«Другие точки» of «Сводки»: every point's counts of the last week or this month (?period=week), the names and the numbers only."""
    period = "week" if request.args.get("period") == "week" else "month"
    start, end = pn.last_week(today) if period == "week" else (ps.month_start(today), today)
    found = {}

    def issued_of(pid):
        if pid not in found:
            found[pid] = issued(conn, pid, today)
        return found[pid]
    rows = ps.compare(conn, points.list_points(conn), start, end, issued_of, point_id)
    return {"period": period, "cmp_start": start, "cmp_end": end, "cmp_rows": rows, "month_name": ps.month_name(today)}


def register(app) -> None:
    app.jinja_env.globals["notice_marks"] = notice_marks
    app.jinja_env.globals["returns_mark"] = pn.RETURNS_MARK

    @app.route("/my-point/notify", methods=["POST"])
    @login_required
    def mypoint_notify():
        """«Выключить» / «Включить» of «Рабочее место»: the point's own notifications; off needs the name of who does it."""
        point_id = own_point()
        if point_id is None:
            abort(403)
        on = request.form.get("on") == "1"
        conn = get_conn()
        try:
            pn.switch(conn, point_id, on, local_iso(), author(), request.form.get("who", ""), request.form.get("reason", ""), by_point=True)
            flash("Уведомления включены." if on else "Уведомления выключены. Включить их снова можно здесь же.", "info")
        except pn.NotifyError as err:
            conn.rollback()
            flash(str(err), "error")
        return redirect(_back("/my-point"))

    @app.route("/my-point/notify/ask", methods=["POST"])
    @login_required
    def mypoint_notify_ask():
        """notify.js is about to show «Разрешите уведомления»: yes while the point's notifications are on and the person has asks left today."""
        point_id = own_point()
        if point_id is None or not _real_point():
            return jsonify({"ok": False, "left": 0})
        conn = get_conn()
        if not pn.enabled(conn, point_id):
            return jsonify({"ok": False, "left": 0})
        now = local_iso()
        done = pn.ask(conn, current_user()["login"], now)
        return jsonify({"ok": done, "left": pn.asks_left(conn, current_user()["login"], now)})

    @app.route("/my-point/notify/answer", methods=["POST"])
    @login_required
    def mypoint_notify_answer():
        """What the browser answered the ask (granted / denied): one record per person and answer a day."""
        point_id = own_point()
        answer = {"granted": "allowed", "denied": "refused"}.get(request.form.get("permission", ""))
        if point_id is None or not _real_point() or answer is None:
            return jsonify({"ok": False})
        conn, now, who = get_conn(), local_iso(), author()
        if conn.execute("SELECT 1 FROM point_event WHERE point_id = ? AND kind = ? AND author = ? AND at >= ?", (point_id, answer, who, now[:10])).fetchone() is None:
            pn.record(conn, now, point_id, answer, who)
        return jsonify({"ok": True})

    @app.route("/my-point/notices")
    @login_required
    def mypoint_notices():
        """«Сводки»: the last week, the return today, the office's tasks, the state of the notifications and the alerts of the point."""
        from .routes_mypoint import ALERTS_READ, _issued, _point_id
        conn = get_conn()
        point_id = _point_id(conn)
        try:
            point = points.get_point(conn, point_id) if point_id is not None else None
        except points.PointError:
            abort(403)
        now = local_iso()
        today = now[:10]
        compare_on = ps.compare_shown(conn)
        tab = "others" if request.args.get("tab") == "others" and compare_on else "mine"
        values = {"point": point, "choose": own_point() is None, "points": points.list_points(conn) if own_point() is None else [],
                  "user": current_user(), "week": None, "returns": [], "office_tasks": [], "alerts": [], "notify": pn.state(conn, point_id),
                  "can_switch": own_point() is not None, "tab": tab, "compare_on": compare_on, "act": {"late": [], "stale": []},
                  "late_days": ps.LATE_DAYS, "stale_days": ps.STALE_DAYS, "on_time_days": ps.ON_TIME_DAYS}
        if point is not None and tab == "others":
            return render_template(point_page("mypoint_notices.html"), **values, **_compare(conn, point_id, today, _issued))
        if point is not None:
            start, end = pn.last_week(today)
            first = ps.month_start(today)
            issued = _issued(conn, point_id, today)
            week = dict(ps.counts(conn, point_id, start, end, issued), start=start, end=end)
            month = dict(ps.counts(conn, point_id, first, today, issued), start=first, end=today, name=ps.month_name(today))
            show_money = order_report.income_shown(conn) if own_point() is not None else has_right("money")
            kpi = rubles = None
            if show_money or today >= order_report.KPI_FROM:                    # one count of the report: the KPI and the sums
                report = order_report.build(conn, min(start, first), today)
                if today >= order_report.KPI_FROM:
                    kpi = income_view._kpi(report, conn, point_id, today[:7], today)
                if show_money:
                    rubles = ps.money(conn, point_id, {"week": (start, end), "month": (first, today)}, report)
            values.update(month=month, kpi=kpi, rubles=rubles, kpi_from=order_report.KPI_FROM, show_money=show_money,
                          act=ps.to_act(conn, point_id, today))
            found = pn.office_tasks(conn, point_id, today)
            own = set(tasks.point_managers(conn, point_id))
            main = current_app.config.get("AUTHOR", "")
            for t in found:
                name = users.person_name(conn, t["created_by"], main)
                t["from"] = POINT_OFFICE if name == t["created_by"] and t["created_by"] not in own else name     # never an office login
                t["late"] = t["due_at"] < now[:16]
            alerts = [n for n in points.notes(conn, point_id, limit=ALERTS_READ) if n["kind"] == "alert"][:points.NOTES_SHOWN] if points.in_panel(point) else []
            if alerts and own_point() is not None:                              # the alerts opened here are seen (as ?notes=1 of «Рабочее место»)
                try:
                    points.mark_seen(conn, point_id, max(n["at"] for n in alerts))
                except Exception:                                                # noqa: BLE001 - a busy base: the page still opens
                    conn.rollback()
            values.update(week=week, returns=pn.returns_today(conn, point_id, today), office_tasks=found, alerts=alerts)
        return render_template(point_page("mypoint_notices.html"), **values)

    @app.route("/my-point/notices/task/<int:task_id>/<how>", methods=["POST"])
    @login_required
    def mypoint_notice_task(task_id, how):
        """«Прочитал» / «Выполнено» of a task of the office on «Сводки»: its own point's only; both are recorded."""
        if how not in ("read", "done"):
            abort(404)
        point_id = own_point()
        if point_id is None:
            abort(403)
        conn, now = get_conn(), local_iso()
        task = pn.office_task(conn, point_id, task_id, now[:10])
        if task is None:                                                        # another point's, the point's own or closed: not here
            abort(404)
        if how == "read":
            if pn.mark_read(conn, point_id, task, now, author()):
                flash("Отмечено: задача прочитана.", "info")
        else:
            try:
                tasks.close(conn, task_id, "done", now, current_user()["login"], request.form.get("result", ""), journal_author=author())
                pn.record(conn, now, point_id, "task_done", author(), text=pn.task_text(task), task_id=task_id)
                flash("Задача выполнена.", "info")
            except tasks.TaskError as err:
                flash(str(err), "error")
        return redirect("/my-point/notices")

    @app.route("/points/notify", methods=["GET", "POST"])
    @login_required
    def points_notify():
        """«Уведомления точек» of the office: the state of every point, «Выключить» / «Включить», the history with its filters."""
        if own_point() is not None:
            abort(403)
        conn = get_conn()
        if request.method == "POST" and request.form.get("what") == "compare":     # «Другие точки» in «Сводки»: the owner's own switch
            if current_user()["role"] != "owner":
                abort(403)
            on = request.form.get("on") == "1"
            if ps.set_compare_shown(conn, on, local_iso(), author()):
                flash("Точки видят вкладку «Другие точки» в «Сводках»." if on else "Вкладка «Другие точки» скрыта от точек.", "info")
            return redirect("/points/notify")
        if request.method == "POST":
            try:
                point_id = int(request.form.get("p") or 0)
            except ValueError:
                abort(400)
            on = request.form.get("on") == "1"
            try:
                pn.switch(conn, point_id, on, local_iso(), author(), request.form.get("who", "") or users.person_name(conn, current_user()["login"],
                          current_app.config.get("AUTHOR", "")), request.form.get("reason", ""))
                flash("Уведомления точки включены." if on else "Уведомления точки выключены.", "info")
            except pn.NotifyError as err:
                conn.rollback()
                flash(str(err), "error")
            return redirect(_back("/points/notify"))
        known = points.list_points(conn)
        wanted = str(request.args.get("p") or "")
        point_id = int(wanted) if wanted.isascii() and wanted.isdigit() and int(wanted) in {p["id"] for p in known} else None
        since, until = _day(request.args.get("from")), _day(request.args.get("to"))
        kind = request.args.get("kind", "")
        kind = kind if kind in pn.KINDS else ""
        states = [dict(p, notify=pn.state(conn, p["id"])) for p in known]
        return render_template("points_notify.html", states=states, points=known, point_id=point_id, since=since, until=until, kind=kind,
                               kinds=pn.KINDS, rows=pn.history(conn, point_id, since, until, kind), may_switch=can("orders", "edit"),
                               compare_on=ps.compare_shown(conn), compare_switch=current_user()["role"] == "owner")
