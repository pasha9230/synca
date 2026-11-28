"""«Подключения → Google-таблица» (the owner 2026-10-23): the web app of Apps Script that writes the sheet «Заказы», its secret, the switch
and «Проверить»; and the sending after a request (order_sheet.flush in a thread of its own: the page never waits for Google)."""
from __future__ import annotations

import json
import logging
import threading
from datetime import timedelta

from flask import abort, flash, redirect, render_template, request

from .. import connections, order_sheet, store
from ..secretbox import SecretKeyError
from ..transport import urllib_transport
from .common import author, current_user, get_conn, login_required, now_iso
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
                               columns=order_sheet.ASKED + order_sheet.OPTIONAL, sheet_name=order_sheet.SHEET, repair=repair_last(conn))

    def save():
        conn = get_conn()
        url, secret, on = request.form.get("url", "").strip(), request.form.get("secret", "").strip(), request.form.get("enabled") == "1"
        before = order_sheet.settings(conn)
        keep_from = request.form.get("keep_from") if "keep_from" in request.form else None
        try:
            order_sheet.check_url(url, on)
            if keep_from is not None:
                keep_from = order_sheet.check_day(keep_from)
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
        order_sheet.save_settings(conn, url, on, now_iso(), author(), secret_typed=bool(secret), keep_from=keep_from)
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

    @app.route("/connections/sheet/repair", methods=["POST"])
    @login_required
    def connections_sheet_repair():
        """«Строки, стёртые вручную» (the owner 28.11): order_sheet.repair_blank in a thread of its own (it asks the sheet about every row
        written); «dry»: only the list. The owner only. The result is shown on the page (repair_last)."""
        if current_user()["role"] != "owner":
            abort(403)
        box = app.config.get("SECRET_BOX")
        if box is None:
            flash(NO_KEY)
            return redirect("/connections/sheet")
        dry, who = request.form.get("dry") == "1", author()
        moscow = (app.config["NOW"]() + timedelta(hours=3)).isoformat(timespec="seconds")      # the order's journal is in Moscow time
        http = app.config.get("SHEET_HTTP") or urllib_transport
        conn = get_conn()
        if not store.claim_state(conn, REPAIR_KEY + ":run", moscow, (app.config["NOW"]() + timedelta(hours=3) - timedelta(minutes=15)).isoformat(timespec="seconds")):
            flash("Проверка пустых строк уже идёт: обновите страницу через минуту.")
            return redirect("/connections/sheet#repair")
        conn.commit()

        def work():
            db = store.connect(app.config["DB_PATH"])
            try:
                try:
                    done, error = order_sheet.repair_blank(db, box, http, moscow, author=who, dry_run=dry), ""
                except order_sheet.SheetError as err:
                    done, error = [], str(err)
                except Exception as err:                                  # noqa: BLE001: the page shows it, never a silent stop
                    log.exception("the Google table: the repair of the wiped rows")
                    done, error = [], "сбой панели (%s): сообщите разработчику." % type(err).__name__
                store.set_state(db, REPAIR_KEY, json.dumps({"at": moscow, "dry": dry, "error": error, "count": len(done),
                                                            "rows": done[:REPAIR_SHOWN]}, ensure_ascii=False))
                store.set_state(db, REPAIR_KEY + ":run", "")
                db.commit()
            finally:
                db.close()
        runner = app.config.get("SHEET_RUN")
        if runner is not None:
            runner(work)
        else:
            threading.Thread(target=work, name="google-sheet-repair", daemon=True).start()
        flash("Проверка пустых строк запущена: результат появится ниже через минуту (обновите страницу).")
        return redirect("/connections/sheet#repair")


REPAIR_KEY = "sheet_repair_last"
REPAIR_SHOWN = 300


def repair_last(conn):
    """The last result of «Строки, стёртые вручную»: {"at", "dry", "error", "count", "rows"}, or None; «running» while one goes."""
    raw = store.get_state(conn, REPAIR_KEY)
    try:
        last = json.loads(raw) if raw else None
    except ValueError:
        last = None
    if last is not None or store.get_state(conn, REPAIR_KEY + ":run"):
        last = dict(last or {}, running=bool(store.get_state(conn, REPAIR_KEY + ":run")))
    return last
