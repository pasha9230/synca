"""«Подключения → Google-таблица» (the owner 2026-10-23): the web app of Apps Script that writes the sheet «Заказы», its secret, the switch
and «Проверить»; and the sending after a request (order_sheet.flush in a thread of its own: the page never waits for Google)."""
from __future__ import annotations

import logging
import threading
from datetime import timedelta

from flask import flash, redirect, render_template, request

from .. import connections, order_sheet, store
from ..secretbox import SecretKeyError
from ..transport import urllib_transport
from .common import author, get_conn, login_required, now_iso
from .routes_connections import NO_KEY, TOO_SOON

log = logging.getLogger("tires_sync.sheet")
_busy = threading.Lock()
_again = threading.Event()


def kick(app) -> None:
    """After a change: send what changed, in this process, on its own connection. One sender at a time; a kick while it runs makes it look
    once more. `SHEET_RUN` (tests) runs it in place."""
    stamp = lambda: (app.config["NOW"]() + timedelta(hours=3)).isoformat(timespec="seconds")      # the order's journal is in Moscow time
    first = stamp()
    box, http = app.config.get("SECRET_BOX"), app.config.get("SHEET_HTTP") or urllib_transport

    def work():
        when = first
        _again.set()
        while _again.is_set():                                            # a kick that came while the sender ran: the sender looks once more
            if not _busy.acquire(blocking=False):
                return                                                    # the running sender sees _again after its round
            try:
                _again.clear()
                conn = store.connect(app.config["DB_PATH"])
                try:
                    order_sheet.flush(conn, box, http, when)
                except Exception:
                    log.exception("the Google table was not sent")
                finally:
                    conn.close()
            finally:
                _busy.release()
            when = stamp()
    runner = app.config.get("SHEET_RUN")
    if runner is not None:
        runner(work)
    else:
        threading.Thread(target=work, name="google-sheet", daemon=True).start()


def register(app) -> None:
    @app.after_request
    def sheet_after_change(response):
        if request.method == "POST" and response.status_code < 500 and not request.path.startswith(("/hook/", "/s/", "/static/")):
            try:
                if order_sheet.enabled(get_conn()):
                    kick(app)
            except Exception:                                             # the table never breaks the answer of a page
                log.exception("the Google table was not kicked")
        return response

    @app.route("/connections/sheet")
    @login_required
    def connections_sheet_page():
        conn = get_conn()
        box = app.config.get("SECRET_BOX")
        tabs = {"active": "sheet", "account_id": None, "supplier_id": None, "suppliers": [], "accounts": []}
        return render_template("conn_sheet.html", tabs=tabs, box_ok=box is not None, sheet=order_sheet.settings(conn),
                               sheet_secrets=connections.secret_status(conn, box, order_sheet.KIND, 0),
                               sheet_check=connections.get_check(conn, order_sheet.KIND, 0), sheet_status=order_sheet.status(conn),
                               columns=order_sheet.COLUMNS, sheet_name=order_sheet.SHEET)

    def save():
        conn = get_conn()
        url, secret, on = request.form.get("url", "").strip(), request.form.get("secret", "").strip(), request.form.get("enabled") == "1"
        before = order_sheet.settings(conn)
        try:
            order_sheet.check_url(url, on)
        except order_sheet.SheetError as err:
            flash(str(err))
            return False
        if url and url != before["secret_for"] and not secret:            # the secret never goes to a new address by itself
            flash("Для этого адреса впишите секрет (тот, что в свойствах скрипта)." + (" Заказы, уже записанные в прежнюю таблицу, в новой не "
                  "обновляются." if before["secret_for"] else ""))
            return False
        if secret:
            try:
                connections.set_secret(conn, app.config.get("SECRET_BOX"), order_sheet.KIND, 0, "secret", secret, now_iso(), author())
            except SecretKeyError:
                flash(NO_KEY)
                return False
        order_sheet.save_settings(conn, url, on, now_iso(), author(), secret_typed=bool(secret))
        return True

    @app.route("/connections/sheet", methods=["POST"])
    @login_required
    def connections_sheet():
        if save():
            flash("Настройки Google-таблицы сохранены.")
        return redirect("/connections/sheet")

    @app.route("/connections/sheet/check", methods=["POST"])
    @login_required
    def connections_sheet_check():
        conn = get_conn()
        box = app.config.get("SECRET_BOX")
        if box is None:
            flash(NO_KEY)
            return redirect("/connections/sheet")
        if "url" in request.form and not save():
            return redirect("/connections/sheet")
        if not connections.check_allowed(conn, order_sheet.KIND, 0, now_iso()):
            flash(TOO_SOON)
            return redirect("/connections/sheet")
        connections.record_check(conn, order_sheet.KIND, 0, False, "проверка выполняется…", now_iso())
        try:
            message = order_sheet.check(conn, box, app.config.get("SHEET_HTTP") or urllib_transport)
            connections.record_check(conn, order_sheet.KIND, 0, True, message, now_iso())
        except order_sheet.SheetError as err:
            connections.record_check(conn, order_sheet.KIND, 0, False, str(err), now_iso())
        except SecretKeyError:
            connections.record_check(conn, order_sheet.KIND, 0, False, "Секреты не читаются: проверьте мастер-ключ.", now_iso())
        return redirect("/connections/sheet")
