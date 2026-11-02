"""«Задачи» of the panel (the owner 2026-10-31; tasks.py): the page with «Мне», «Поставил я», «Все» (the office only), the filters «Кому»,
«Срок», «Тип»; «Новая задача» with or without an order; «Выполнено» with what came of it and «Отменить»; the block «Задачи» of an order,
a chat and a client card (the same form). Section «Заказы»: to look — «смотреть», to set or close — «менять» (so never the observer nor the
client). A manager of a point is checked here, on the server, not by the buttons only: it sees and closes only the tasks set to itself,
and sets a task only to itself, of an order of its own point or of none (never a chat nor a client)."""
from __future__ import annotations

from flask import abort, flash, redirect, render_template, request

from .. import order_notify, tasks, users
from .common import author, can, current_user, get_conn, login_required, own_point, safe_path
from .routes_alerts import may_take
from .routes_orders import local_iso

TABS = {"mine": "Мне", "by_me": "Поставил я", "all": "Все"}
DUES = {"": "Все сроки", "overdue": "Просроченные", "today": "До конца сегодня", "week": "До конца недели"}


def _back(default: str = "/tasks") -> str:
    return safe_path(str(request.form.get("back") or "").strip()[:300]) or default


def is_point() -> bool:
    return own_point() is not None


def may_set() -> bool:
    """Who sets and closes tasks: «менять» in «Заказы» (the office and a point; never the observer nor the client)."""
    return can("orders", "edit")


def _owner(conn) -> str:
    return order_notify.get_owner_login(conn)


def _main(app) -> str:
    return app.config.get("AUTHOR", "")


def _int(name):
    try:
        return int(request.form.get(name) or 0) or None
    except ValueError:
        return None


def _card_point(conn, order_id):
    row = conn.execute("SELECT point_id FROM order_card WHERE id = ?", (order_id,)).fetchone()
    try:
        return int(row[0]) if row and str(row[0] or "").strip() else None
    except ValueError:
        return None


def _of_point(conn, task, point_id) -> bool:
    """The task is one the owner or the partner working as the point sees (tasks.listing as_point): of an order of that point or of nothing."""
    if task["chat_row"]:
        return False
    if task["order_id"]:
        return _card_point(conn, task["order_id"]) == point_id
    return not task["client_id"]


def register(app) -> None:
    def tasks_for(client_id=None, order_id=None, chat_row=None) -> list:
        """The open tasks of the place for the one who looks: a point only its own; late ones known, the names of the assignees."""
        conn, now = get_conn(), local_iso()
        found = tasks.of_place(conn, client_id=client_id, order_id=order_id, chat_row=chat_row,
                               only=current_user()["login"] if is_point() else None)
        for t in found:
            t["late"] = t["due_at"] < now[:16]
            t["assignee_name"] = users.person_name(conn, t["assignee"], _main(app))
        return found

    def task_people() -> list:
        conn = get_conn()
        if is_point():
            me = current_user()["login"]
            return [{"login": me, "name": users.person_name(conn, me), "group": "Себе"}]
        return tasks.assignees(conn, _owner(conn), _main(app))

    def task_default(order_id=None, chat_row=None) -> str:
        conn = get_conn()
        me = current_user()["login"]
        return me if is_point() else tasks.default_assignee(conn, _owner(conn), me, order_id, chat_row)

    app.jinja_env.globals.update(tasks_for=tasks_for, may_task=may_set, task_people=task_people, task_default=task_default,
                                 task_kinds=tasks.KINDS)

    @app.route("/tasks")
    @login_required
    def tasks_page():
        conn, me = get_conn(), current_user()["login"]
        point = is_point()
        office = may_take()
        tab = request.args.get("tab", "mine")
        if tab not in TABS or (tab == "all" and not office):
            tab = "mine"
        to = request.args.get("to", "")[:40] if not point else ""
        due = request.args.get("due", "")
        due = due if due in DUES else ""
        kind = request.args.get("kind", "")
        kind = kind if kind in tasks.KINDS else ""
        rows = tasks.listing(conn, tab=tab, login=me, owner_login=_owner(conn), now=local_iso(), point=point, to=to, due=due, kind=kind,
                             as_point=current_user()["as_point"])
        from .. import inquiry
        for t in rows:                                    # whom to call: the name and the phone, as the reminder had them
            t["who"], t["phone"] = inquiry._who_called(conn, t)
        names = {}
        for t in rows:
            for login in (t["assignee"], t["created_by"]):
                if login not in names:
                    names[login] = users.person_name(conn, login, _main(app))
        tabs = {k: v for k, v in TABS.items() if office or k != "all"}
        return render_template("tasks.html", rows=rows, names=names, tab=tab, tabs=tabs, to=to, due=due, kind=kind, dues=DUES,
                               people=task_people(), point=point)

    @app.route("/tasks/new", methods=["POST"])
    @login_required
    def task_new():
        if not may_set():
            abort(403)
        conn, me = get_conn(), current_user()["login"]
        f = request.form
        order_id, chat_row, client_id = _int("order_id"), _int("chat_row"), _int("client_id")
        assignee = f.get("assignee", "").strip()
        if is_point():                                   # the point: to itself only, of an order of its own point or of none
            if chat_row or client_id or (assignee and assignee != me):
                abort(403)
            if order_id and _card_point(conn, order_id) != own_point():
                abort(403)
            assignee, allowed = me, {me}
        else:
            if not may_take():
                abort(403)
            allowed = {p["login"] for p in tasks.assignees(conn, _owner(conn), _main(app))}
            role = users.get_role(conn, assignee) if assignee else None
            if role and role["role"] == "point":          # a point's manager (the lead's review): an order of its own point or none — never
                if chat_row or client_id or (order_id and _card_point(conn, order_id) != role["point_id"]):   # a chat nor a client
                    flash("Менеджеру точки можно поставить задачу только по заказу его точки или без заказа.", "error")
                    return redirect(_back())
        due = "%sT%s" % (str(f.get("date") or "").strip(), str(f.get("time") or "").strip())
        try:
            tasks.add(conn, kind=f.get("kind", "callback"), title=f.get("title", ""), due_at=due, note=f.get("note", ""), now=local_iso(),
                      author=me, assignee=assignee or None, owner_login=_owner(conn), client_id=client_id, order_id=order_id,
                      chat_row=chat_row, allowed=allowed, journal_author=author())
            flash("Задача поставлена.", "info")
        except tasks.TaskError as err:
            conn.rollback()
            flash(str(err), "error")
        return redirect(_back())

    @app.route("/tasks/<int:task_id>/<how>", methods=["POST"])
    @login_required
    def task_close(task_id, how):
        if how not in ("done", "cancel"):
            abort(404)
        if not may_set():
            abort(403)
        conn, me = get_conn(), current_user()["login"]
        try:
            task = tasks.get(conn, task_id)
        except tasks.TaskError:
            abort(404)
        if is_point() and task["assignee"] != me:        # a point closes only its own: another's is refused, never even looked at
            abort(403)
        if current_user()["as_point"] is not None and not _of_point(conn, task, own_point()):    # the owner as a point: of that point only
            abort(403)
        if not is_point() and not may_take():
            abort(403)
        try:
            tasks.close(conn, task_id, how, local_iso(), me, request.form.get("result", ""), journal_author=author())
            flash("Задача выполнена." if how == "done" else "Задача отменена.", "info")
        except tasks.TaskError as err:
            flash(str(err), "error")
        return redirect(_back())
