"""The users of the panel (the owner only): the list, the roles, «on/off», one-time invitations; a person's own password; the invitation page."""
from __future__ import annotations

from flask import flash, redirect, render_template, request, session

from .. import client_terms, order_points as points, store, users
from .common import author, current_user, get_conn, login_blocked, login_failed, now_iso


def _user_field(message: str) -> str:
    """The field a refusal of users.* is about, for the message under it: «login», «password» or ''."""
    if message.startswith(("Логин", "Пользователь «")):
        return "login"
    if message.startswith("Пароль"):
        return "password"
    return ""


def register(app) -> None:
    def _point(value):
        try:
            return int(value) if str(value or "").strip() else None
        except ValueError:
            return None

    @app.route("/users")
    def users_page():
        from .routes_client_terms import brand_choices
        conn = get_conn()
        return render_template("users.html", users_list=users.list_users(conn), invites=users.list_invites(conn, now_iso()),
                               points={p["id"]: p["name"] for p in points.list_points(conn)}, roles=users.ROLES, sections=users.SECTIONS,
                               rights=users.RIGHTS, matrix=users.MATRIX, link=session.pop("invite_link", None),
                               invite_brands=brand_choices(app, conn, client_terms.read(conn, client_terms.ROLE)["brands"]),
                               describe=client_terms.describe, shop_resp=_shop_resp(conn))

    def _shop_resp(conn):
        """«Ответственный за заказы клиентов»: the owner's choice, who gets them now, the staff to choose from."""
        from .. import shop_cart
        staff = [u for u in users.list_users(conn) if u["active"] and u["role"] not in ("client", "point")]
        return {"chosen": shop_cart.responsible_setting(conn), "now": shop_cart.responsible(conn), "staff": staff}

    @app.route("/users/shop-responsible", methods=["POST"])
    def users_shop_responsible():
        from .. import shop_cart
        try:
            shop_cart.set_responsible(get_conn(), request.form.get("login", ""), now_iso(), author())
            flash("Ответственный за заказы клиентов сохранён.", "ok")
        except shop_cart.CartError as err:
            flash(str(err), "error")
        return redirect("/users")

    @app.route("/users/invite", methods=["POST"])
    def users_invite():
        conn = get_conn()
        role, f = request.form.get("role", ""), request.form
        try:
            terms = client_terms.invite_terms(f.get("c_small", ""), f.get("c_large", ""), f.get("c_mode", "inherit"),
                                              f.getlist("c_brand")) if role == "client" else ""
            token = users.create_invite(conn, role, _point(f.get("point_id")), f.get("display_name", ""), now_iso(), author(), terms)
        except client_terms.TermsError as err:
            flash(str(err), "field:" + err.field if err.field else "error")
            return redirect("/users")
        except users.UserError as err:
            flash(str(err))
            return redirect("/users")
        session["invite_link"] = request.host_url.rstrip("/") + "/invite/" + token          # shown once on the next page, never kept
        return redirect("/users")

    @app.route("/users/<login>/role", methods=["POST"])
    def users_role(login):
        try:
            users.set_role(get_conn(), login, request.form.get("role", ""), _point(request.form.get("point_id")), request.form.get("display_name", ""),
                           now_iso(), author())
            flash("Роль сохранена.", "ok")
        except users.UserError as err:
            flash(str(err))
        return redirect("/users")

    @app.route("/users/<login>/active", methods=["POST"])
    def users_active(login):
        on = request.form.get("active") == "1"
        try:
            users.set_active(get_conn(), login, on, now_iso(), author())
            flash("Пользователь включён." if on else "Пользователь выключен: войти он больше не может, история осталась.")
        except users.UserError as err:
            flash(str(err))
        return redirect("/users")

    @app.route("/users/invites/<int:invite_id>/revoke", methods=["POST"])
    def users_invite_revoke(invite_id):
        try:
            users.revoke_invite(get_conn(), invite_id, now_iso(), author())
            flash("Приглашение отозвано.", "ok")
        except users.UserError as err:
            flash(str(err))
        return redirect("/users")

    @app.route("/invite/<token>", methods=["GET", "POST"])
    def invite_page(token):
        """Open: the person sets their login and password from the one-time link. A wrong token sent in its form counts as a failed login."""
        conn = get_conn()
        if login_blocked(conn):
            return render_template("invite.html", invite=None, error="Слишком много попыток. Подождите и повторите."), 429
        invite = users.find_invite(conn, token, now_iso())
        if invite is None:
            if request.method == "POST":                      # a sent form; an opened link (<img> of a foreign page) never closes the address
                login_failed(conn)
            return render_template("invite.html", invite=None, error="Ссылка недействительна: она уже использована, отозвана или истекла."), 404
        if request.method == "POST":
            if request.form.get("password", "") != request.form.get("password2", ""):
                return render_template("invite.html", invite=invite, error="Пароли не совпали.", error_field="password2", login=request.form.get("login", "")), 400
            try:
                login = users.accept_invite(conn, token, request.form.get("login", ""), request.form.get("password", ""), now_iso())
            except users.UserError as err:
                return render_template("invite.html", invite=invite, error=str(err), error_field=_user_field(str(err)), login=request.form.get("login", "")), 400
            session.clear()
            flash("Готово: войдите с логином «%s» и своим паролем." % login, "ok")
            return redirect("/login")
        return render_template("invite.html", invite=invite, error=None, login="")

    @app.route("/account/password")
    def account_password_page():
        return render_template("account_password.html", shared=current_user()["login"] == app.config["AUTHOR"])

    @app.route("/account/password", methods=["POST"])
    def account_password():
        login = current_user()["login"]
        if login == app.config["AUTHOR"]:
            flash("Общий пароль владельца меняется на сервере (panel.env), не здесь.")
            return redirect("/account/password")
        if request.form.get("new", "") != request.form.get("new2", ""):
            flash("Новые пароли не совпали.", "field:new2")
            return redirect("/account/password")
        conn = get_conn()
        if login_blocked(conn):                                       # a stolen session never guesses the current password
            flash("Слишком много попыток. Подождите и повторите.", "field:old")
            return redirect("/account/password")
        try:
            users.change_own_password(conn, login, request.form.get("old", ""), request.form.get("new", ""), now_iso())
        except users.UserError as err:
            wrong = isinstance(err, users.WrongPassword)
            if wrong:
                login_failed(conn)
            flash(str(err), {"login": "error", "password": "field:new"}.get(_user_field(str(err)), "field:old" if wrong else "error"))
            return redirect("/account/password")
        session["epoch"] = store.get_session_epoch(conn, login)          # this session stays, the others end
        flash("Пароль сменён. Остальные открытые входы закрыты.", "ok")
        return redirect("/")
