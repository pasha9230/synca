from __future__ import annotations

from flask import redirect, render_template, request, session
from werkzeug.security import check_password_hash

from .. import store, users
from .common import epoch_login, get_conn, login_blocked, login_failed, now_iso, session_valid, shared_mark


def register(app) -> None:
    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            conn = get_conn()
            ip = request.remote_addr or "?"
            if login_blocked(conn):
                return render_template("login.html", error="Слишком много попыток. Подождите и повторите."), 429
            typed = request.form.get("login", "").strip().lower()
            password = request.form.get("password", "")
            if not typed or typed == app.config["AUTHOR"]:
                # the shared owner password (panel.env)
                who = app.config["AUTHOR"] if check_password_hash(app.config["PASSWORD_HASH"], password) else None
            else:
                who = users.verify(conn, typed, password)
            if who:
                store.write_audit(conn, now_iso(), who, "login", {"ip": ip})
                session.clear()
                session.permanent = True
                session["user"] = who
                session["epoch"] = store.get_session_epoch(conn, epoch_login(conn, who))
                if who == app.config["AUTHOR"]:
                    session["pw"] = shared_mark()
                return redirect("/")              # the failures of the address stay: they end with the window, not with one's own login
            login_failed(conn)
            store.write_audit(conn, now_iso(), "system", "login_failed", {"login": typed[:40], "ip": ip})     # never the password
            return render_template("login.html", error="Неверный логин или пароль."), 401
        if session_valid():
            return redirect("/")
        return render_template("login.html", error=None)

    @app.route("/logout", methods=["POST"])
    def logout():
        if session_valid():              # only a signed-in user ends their own sessions
            conn = get_conn()
            store.bump_session_epoch(conn, epoch_login(conn, session["user"]))
        session.clear()
        return redirect("/login")
