"""The base of the new design (the owner 2026-11-02, base-plan): «Новый вид» — per role group (клиент, точка, офис) off / at the owner's
only / at everybody's, kept in app_state ui_new.<group>; body.ui-new switches the style «Строгий» on. And «Работать как точка» for the
owner and the partner: the topbar's list of points, the session's as_point, «Выйти» (the rules of the mode are in common.current_user)."""
from __future__ import annotations

from flask import abort, flash, redirect, render_template, request, session

from .. import order_points as points, store
from .common import AS_POINT_ROLES, acting_point, author, current_user, end_as_point, get_conn, now_iso

UI_GROUPS = {"client": "Клиент", "point": "Точка", "office": "Офис: партнёр, менеджер чатов, наблюдатель"}
UI_STATES = {"off": "Выкл", "owner_only": "Только у владельца", "all": "У всех"}
UI_KEY = "ui_new.%s"
VIEWERS = frozenset({"owner", "partner"})            # «только у владельца»: the owner and the partner see it


def ui_group(role: str) -> str:
    return role if role in ("client", "point") else "office"


def ui_state(conn, group: str) -> str:
    value = store.get_state(conn, UI_KEY % group)
    return value if value in UI_STATES else "off"


def ui_new() -> bool:
    """body.ui-new for this page: the state of the role looked at is «у всех», or «только у владельца» and the person is the owner or
    the partner. The role looked at: the person's own (as a point: the point's), the client's in «Каталог глазами клиента»."""
    user = current_user()
    if not user["login"]:
        return False
    group = "client" if request.endpoint == "users_client_preview" else ui_group(user["role"])
    state = ui_state(get_conn(), group)
    return state == "all" or (state == "owner_only" and user["real_role"] in VIEWERS)


def point_new() -> bool:
    """The point's screens of the new design (lane port-point, the prototype's role «Точка»): a point's manager with «Точка» on for them, or
    the owner and the partner working as a point with «только у владельца» / «у всех». Off: the pages exactly as before."""
    return current_user()["role"] == "point" and ui_new()


def point_page(template: str) -> str:
    """The template of a point's page: point2/<template> while point_new(), the old one otherwise (the same values, the same forms)."""
    return "point2/" + template if point_new() else template


def as_point_choices() -> list:
    """The points of the topbar's «Работать как точка»: the owner and the partner only, never while working as a point."""
    user = current_user()
    if user["real_role"] not in AS_POINT_ROLES or user["as_point"] is not None:
        return []
    return points.list_points(get_conn())


def register(app) -> None:
    app.jinja_env.globals.update(ui_new=ui_new, point_new=point_new, as_point_choices=as_point_choices)

    @app.route("/ui-new", methods=["GET", "POST"])
    def ui_new_page():
        conn = get_conn()
        if request.method == "POST":
            wanted = {group: request.form.get(group, "") for group in UI_GROUPS}
            if any(state not in UI_STATES for state in wanted.values()):     # all checked before any is written
                abort(400)
            changed = []
            for group, new in wanted.items():
                old = ui_state(conn, group)
                if new != old:
                    store.set_state(conn, UI_KEY % group, new)
                    store.write_audit(conn, now_iso(), author(), "ui_new", {"group": group, "from": old, "to": new})
                    changed.append(group)
            flash("Сохранено." if changed else "Изменений нет.", "ok" if changed else "info")
            return redirect("/ui-new")
        return render_template("ui_new.html", groups=UI_GROUPS, states=UI_STATES, current={g: ui_state(conn, g) for g in UI_GROUPS})

    @app.route("/as-point", methods=["POST"])
    def as_point_start():
        user = current_user()
        if user["real_role"] not in AS_POINT_ROLES:                     # check_access refused it already; twice for a right
            abort(403)
        if user["as_point"] is not None:                                # one point at a time: «Выйти» first
            abort(409)
        point = acting_point(get_conn(), user["real_role"], request.form.get("point_id", ""))
        if point is None:
            abort(400)
        session["as_point"] = point["id"]
        store.write_audit(get_conn(), now_iso(), user["login"], "as_point_start", {"point": point["id"], "name": point["name"]})
        return redirect("/my-point")

    @app.route("/as-point/stop", methods=["POST"])
    def as_point_stop():
        if current_user()["real_role"] not in AS_POINT_ROLES:
            abort(403)
        end_as_point(current_user()["login"], "exit")
        return redirect("/")
