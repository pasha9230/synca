"""«Звонки»: the calls through the Avito substitute numbers (read by `calls-sync`), the recordings for 7 days, «Создать заказ» from a call.
The manager of a point sees the calls of the point's account only (common.check_access, own_call); a recording needs the right «listen»."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import logging
import secrets

from flask import abort, current_app, flash, redirect, render_template, request, send_file, session

from .. import calls, order_manager as mgr, orders, store
from .common import author, can, get_conn, has_right, now_iso, own_point, point_account
from .routes_orders import from_panel, local_iso

PERIODS = {"1": "сегодня и вчера", "7": "7 дней", "30": "30 дней", "90": "90 дней"}


def records_folder() -> Path:
    return Path(current_app.config["DB_PATH"]).resolve().parent / "calls"


def may_listen(call: dict) -> bool:
    """The right «listen», and for the manager of a point a call of the point's account only."""
    if not has_right("listen") or call["record_state"] != "saved":
        return False
    return own_point() is None or call["account_id"] == point_account()


LISTEN_SAME = timedelta(minutes=10)              # the player's requests of one listening (Range, a rewind) are one record


def _journal(call_row: int, call: dict) -> None:
    """Who listened to which recording, when (no number): every request counts, the same person on the same call within 10 minutes once."""
    conn = get_conn()
    stamp = now_iso()
    last = conn.execute("SELECT at FROM audit WHERE action = 'call_listened' AND author = ? AND detail LIKE ? ORDER BY id DESC LIMIT 1",
                        (author(), '{"call": %d,%%' % call_row)).fetchone()
    try:
        recent = last is not None and datetime.fromisoformat(stamp[:19]) - datetime.fromisoformat(last[0][:19]) < LISTEN_SAME
    except ValueError:
        recent = False
    if not recent:
        store.write_audit(conn, stamp, author(), "call_listened", {"call": call_row, "account": call["account_id"], "at": call["call_at"]})
        conn.commit()


def register(app) -> None:
    @app.route("/calls")
    def calls_page():
        conn = get_conn()
        days = request.args.get("days", "7")
        days = days if days in PERIODS else "7"
        since = (datetime.fromisoformat(local_iso()) - timedelta(days=int(days))).replace(hour=0, minute=0, second=0).isoformat(timespec="seconds")
        missed = request.args.get("missed") == "1"
        phone = calls.search_digits(request.args.get("q", ""))
        mine = own_point() is not None
        accounts = calls.accounts_with_calls(conn)
        account = request.args.get("account", "")
        if mine:                                                         # the manager of a point: the point's account, whatever was asked
            account_id = point_account()
        else:
            account_id = int(account) if account.isdigit() and any(a["id"] == int(account) for a in accounts) else None
        no_account = mine and account_id is None                         # a point without an Avito account has no calls
        rows = [] if no_account else calls.list_calls(conn, account_id=account_id, missed=missed, since=since, phone=phone)
        for row in rows:
            row["may_listen"] = may_listen(row)
            if mine:                                                     # a point never sees an account's name (the owner 2026-10-29)
                row["account_name"] = ""
        handed = mgr.handed_calls(conn, [row["id"] for row in rows])
        links = calls.client_links(conn, [row["client_id"] for row in rows], account_id=account_id if mine else None, point_id=own_point())
        handoff = None
        if mine and not no_account:                                      # «Передать под заказ»: the calls of the point's account (lane B)
            typed = session.pop("handoff_call", None)
            handoff = {"action": "/calls/%d/handoff" % typed["call"] if typed else "", "token": secrets.token_hex(12), "error": bool(typed),
                       "form": typed or {}, "seasons": mgr.SEASONS, "done": None, "back": request.full_path.rstrip("?")}
        return render_template("calls.html", rows=rows, handed=handed, handoff=handoff, links=links, chat_links=can("chats"), order_links=can("orders"), accounts=accounts, account=account_id, days=days, periods=PERIODS, missed=missed,
                               q=phone, mine=mine, no_account=no_account, can_order=can("orders", "edit") and can("calls", "edit"), clients_link=can("clients"))

    @app.route("/calls/<int:call_row>/record")
    def call_record(call_row):
        call = calls.get_call(get_conn(), call_row)
        if call is None:
            abort(404)
        if not has_right("listen"):
            abort(403)
        path = calls.record_path(records_folder(), call["record_file"]) if call["record_state"] == "saved" else None
        if path is None or not path.is_file():
            abort(404)
        acting = from_panel()                                          # opened from another site: the recording only (хвост З20)
        if acting:
            _journal(call_row, call)
        if acting and own_point() is None and can("orders", "edit"):   # the staff listened: a client a point handed over from it is taken
            try:
                mgr.take_call(get_conn(), call_row, "прослушал звонок", author(), local_iso())
            except Exception:
                get_conn().rollback()
                logging.getLogger(__name__).exception("the handed-over client of call %s could not be marked taken", call_row)
        answer = send_file(str(path), mimetype="audio/mpeg", conditional=True, max_age=0)
        answer.headers["Cache-Control"] = "private, no-store"
        return answer

    @app.route("/calls/<int:call_row>/handoff", methods=["POST"])
    def call_handoff(call_row):
        """«Передать под заказ» from a call of the point's account: the same window and order_manager.hand_over as in «Чаты»."""
        conn = get_conn()
        call = calls.get_call(conn, call_row)
        if call is None:
            abort(404)
        point = own_point()
        if point is None or call["account_id"] != point_account():
            abort(403)
        form = {k: str(request.form.get(k) or "")[:600] for k in ("phone", "size", "season", "brand", "comment")}
        try:
            from .routes_vk import channel
            _send, _deliver, notify = channel(app, conn, local_iso())
        except Exception:                                                  # without the alert channel the client still goes; the order shows it
            logging.getLogger(__name__).exception("the alert channel for a handoff failed")
            notify = None
        try:
            _card_id, answer = mgr.hand_over(conn, point, {"kind": "call", "call_id": call_row}, form["phone"], mgr.handoff_fields(form),
                                             request.form.get("token"), author(), local_iso(), notify)
            from .. import point_log
            point_log.record(conn, local_iso(), author(), point, "handoff", _card_id, "из звонка")             # «История точки»
            flash(answer, "ok")
        except orders.OrderError as err:
            flash(str(err))
            session["handoff_call"] = dict(form, call=call_row)            # the window opens again with what was typed
        back = str(request.form.get("back") or "")
        back = back if back == "/calls" or (back.startswith("/calls?") and "#" not in back and "\\" not in back) else "/calls"    # only «Звонки»
        return redirect("%s#call-%d" % (back, call_row))

    @app.route("/calls/<int:call_row>/order", methods=["POST"])
    def call_create_order(call_row):
        conn = get_conn()
        if calls.get_call(conn, call_row) is None:
            abort(404)
        try:
            card_id = calls.create_order(conn, call_row, local_iso(), author(), own_point=own_point())
        except orders.OrderError as err:
            flash(str(err))
            return redirect("/calls")
        return redirect("/orders/%d" % card_id)
