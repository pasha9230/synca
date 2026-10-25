"""The inquiries of the clients in the panel: their status next to the chats, the calls and the clients, «в работе» when the staff open a
chat, a client card or the recording of a call, and «Перезвонить» (a form, «Сделано», «Отменить»). Nothing here writes to Avito or a client."""
from __future__ import annotations

import logging

from flask import abort, flash, g, redirect, request

from .. import inquiry
from .common import SECTION_OF, author, get_conn, login_required, safe_path
from .routes_alerts import may_take
from .routes_orders import from_panel, local_iso

log = logging.getLogger(__name__)
OPENED = {"chats_page": ("chat_row", "client_chat", "открыл чат"), "client_page": ("client_id", None, "открыл карточку клиента"),
          "call_record": ("call_row", "client_call", "прослушал звонок")}


def _back(default: str = "/alerts") -> str:
    """Where a form returns: a path of the panel only."""
    return safe_path(str(request.form.get("back") or "").strip()[:300]) or default


def _opened(response):
    """A chat, a client card or a recording opened by somebody who works the orders: «новое» becomes «в работе». Opened from another
    site (an <img> or a link on a foreign page), the page is shown and the status stays (from_panel, хвост З20)."""
    try:
        place = OPENED.get(request.endpoint or "")
        if place is None or request.method != "GET" or response.status_code not in (200, 206) or not may_take() or not from_panel():
            return response
        key, table, how = place
        value = (request.view_args or {}).get(key)
        if not value:
            return response
        conn = get_conn()
        if table:
            row = conn.execute("SELECT client_id FROM %s WHERE %s = ?" % (table, key), (value,)).fetchone()
            value = row[0] if row else None
        if value:
            inquiry.touch(conn, value, local_iso(), author(), how)
    except Exception:                                                        # noqa: BLE001  (an opened page must open)
        log.exception("the inquiry of an opened page could not be marked")
    return response


def _cached(name: str, compute):
    cache = g.setdefault("inquiry_cache", {})
    if name not in cache:
        cache[name] = compute()
    return cache[name]


def register(app) -> None:
    app.after_request(_opened)
    SECTION_OF.update(callback_new="orders", callback_close="orders")        # «менять» in «Заказы» (common.py is lane A's this run)

    def inquiry_chats(chat_rows) -> dict:
        rows = list(chat_rows or [])
        return _cached("chats:%s" % ",".join(map(str, rows)), lambda: inquiry.of_chats(get_conn(), rows))

    def inquiry_calls(call_rows) -> dict:
        rows = list(call_rows or [])
        return _cached("calls:%s" % ",".join(map(str, rows)), lambda: inquiry.of_calls(get_conn(), rows))

    def inquiry_client(client_id):
        return inquiry.statuses(get_conn(), [client_id]).get(int(client_id)) if client_id else None

    def callbacks_for(client_id=None, order_id=None, chat_row=None) -> list:
        return inquiry.open_callbacks(get_conn(), client_id=client_id, order_id=order_id, chat_row=chat_row)

    app.jinja_env.globals.update(inquiry_chats=inquiry_chats, inquiry_calls=inquiry_calls, inquiry_client=inquiry_client,
                                 callbacks_for=callbacks_for, may_callback=may_take)

    def _int(name):
        try:
            return int(request.form.get(name) or 0) or None
        except ValueError:
            return None

    @app.route("/callbacks/new", methods=["POST"])
    @login_required
    def callback_new():
        if not may_take():
            abort(403)
        due = "%sT%s" % (str(request.form.get("date") or "").strip(), str(request.form.get("time") or "").strip())
        try:
            inquiry.add_callback(get_conn(), due, request.form.get("note", ""), local_iso(), author(),
                                 client_id=_int("client_id"), order_id=_int("order_id"), chat_row=_int("chat_row"))
            flash("Задача «Перезвонить» поставлена.", "info")
        except inquiry.InquiryError as err:
            get_conn().rollback()
            flash(str(err), "error")
        return redirect(_back("/"))

    @app.route("/callbacks/<int:task_id>/<how>", methods=["POST"])
    @login_required
    def callback_close(task_id, how):
        if how not in ("done", "cancel"):
            abort(404)
        if not may_take():
            abort(403)
        try:
            inquiry.close_callback(get_conn(), task_id, how, local_iso(), author())
        except inquiry.InquiryError as err:
            flash(str(err), "error")
        return redirect(_back())
