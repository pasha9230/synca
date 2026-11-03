"""«Что нового» (the owner 2026-11-02, tires_sync.whatsnew): the page of every role — the office's menu, the point's, the client's frame;
the dot on its menu item while an update for the role is unread; opening the page reads it. No popup."""
from __future__ import annotations

import logging
from pathlib import Path

from flask import current_app, g, make_response, render_template

from .. import users, whatsnew
from .common import current_user, get_conn, now_iso
from .routes_design import point_page

log = logging.getLogger(__name__)


def updates() -> tuple:
    """The shipped updates; a test gives its own folder by WHATSNEW_DIR (read anew then)."""
    folder = current_app.config.get("WHATSNEW_DIR")
    return tuple(whatsnew.load(Path(folder))) if folder else whatsnew.shipped()


def whatsnew_dot() -> bool:
    """Once per request: a point's page draws it twice (the menu and the phone's sections)."""
    if "whatsnew_dot" not in g:
        user = current_user()
        g.whatsnew_dot = whatsnew.has_new(get_conn(), user["login"], user["role"], updates())
    return g.whatsnew_dot


def register(app) -> None:
    app.jinja_env.globals["whatsnew_dot"] = whatsnew_dot

    @app.route("/whats-new")
    def whatsnew_page():
        conn, user = get_conn(), current_user()
        role, shown = user["role"], updates()
        values = {"updates": whatsnew.for_role(shown, role), "sees_all": role in whatsnew.SEES_ALL, "role_names": users.ROLES,
                  "point": role == "point", "client": role == "client"}
        if role == "client":
            from .. import shop_cart
            values["cart_count"] = len(shop_cart.cart(conn, user["login"]))
        try:                                                                 # before the menu is drawn: no dot on the page that reads it
            whatsnew.mark_seen(conn, user["login"], role, shown, now_iso())
            g.whatsnew_dot = False
        except Exception:                                                    # a busy database: the page still opens, the dot stays
            log.exception("whatsnew: the mark of %s not saved", user["login"])
        response = make_response(render_template(point_page("whatsnew.html"), **values))
        response.headers["Cache-Control"] = "private, no-store"
        return response
