"""«Уведомления»: the open order alerts of the current panel user and «Разобрался» on red ones."""
from __future__ import annotations

import logging
import re

from flask import abort, current_app, flash, jsonify, redirect, render_template, request, session

from .. import order_alerts, order_engine, order_manager as mgr, order_oth, order_reserve as reserve, orders, store
from .common import can, get_conn, login_required, own_point, point_account
from .routes_orders import from_panel, local_iso

log = logging.getLogger(__name__)


def grouped(conn, login: str) -> list:
    """The rows of the page, newest first: `order_alerts.page_rows` (one source with the bell, its counter and the dots by the order numbers),
    at the panel's local time, so that old green alerts are left out."""
    now = local_iso()
    rows = order_alerts.page_rows(conn, login, now)
    point = own_point()
    if point is None:
        return rows
    account = point_account()                                     # the manager of a point: the orders of the point, the chats of its account
    ids = {r["order_id"] for r in rows if r["order_id"] is not None}
    marks = ",".join("?" * len(ids))
    ours = {r[0] for r in conn.execute("SELECT id FROM order_card WHERE point_id = ? AND id IN (%s)" % marks, [point] + sorted(ids))} if ids else set()
    quiet = {r[0] for r in conn.execute(                          # an order with a chat of another account: not the client's words (2026-10-22)
        "SELECT DISTINCT order_id FROM avito_chat WHERE order_id IN (%s) AND (? IS NULL OR account_id != ?)" % marks,
        sorted(ids) + [account, account])} if ids else set()
    chats_of = {r[0] for r in conn.execute("SELECT id FROM avito_chat WHERE account_id = ?", (account,))} if account is not None else set()
    foreign = quiet | set(orders.origin_labels(conn, point, ours))     # not the point's own order: the client's texts named only (as in the card)
    rows = [r for r in rows if not is_handoff(r) and not is_return_ask(r) and not is_handoff_cancel(r)]   # «Клиент хочет заказать шины…» is for the staff: the point sees it on
                                                                  # «Моя точка»; «заполните сумму возврата» too: the point has its green row
    rows = [order_engine.for_point(r) if r["order_id"] in foreign else r
            for r in rows if (r["order_id"] in ours and not (r["order_id"] in quiet and orders.client_words(r))
                              if r["order_id"] is not None else r.get("chat_row") in chats_of)]
    rows = [dict(r, text=orders.without_account_names(conn, r.get("text"))) for r in rows]      # never an account's name (2026-10-29)
    return sorted(point_rows(conn, point, now) + rows, key=lambda r: r["at"] or "", reverse=True)


def point_rows(conn, point_id, now: str) -> list:
    """The rows of the bell of the point itself (lane 3; the owner 2026-10-26: a point hears of its news in the panel): the open question
    «Клиент забрал?» of each order there (order_reserve.question_open), until the point answers or its working day ends. Computed, never
    written: the staff and the other points never get them (grouped adds them for the manager of this point only). The id is the key of the
    question in /my-point/feed (its stay at the point), so that notify.js rings for the two once. And «возврат отмечен» (green) of an order
    the point gave back in this stay with no amount yet: it stays «На точке» until the manager fills the amount (and moves it to «Возврат»)."""
    found = []
    for (card_id,) in conn.execute("SELECT c.id FROM order_card c JOIN order_stage s ON s.id = c.stage_id WHERE c.point_id = ? "
                                   "AND s.code = 'at_point'", (point_id,)):
        card = orders.get_card(conn, card_id)
        back = conn.execute("SELECT id, at FROM order_event WHERE order_id = ? AND kind = 'point' AND text = ? AND at >= ? ORDER BY id DESC LIMIT 1",
                            (card_id, mgr.RETURN_LINE, card["entered_at"] or "")).fetchone()
        if back and not card["return_amount"]:                     # the amount is in: it waits for the move to «Возврат» only (review)
            found.append({"id": "return-%d" % back[0], "order_id": card_id, "at": back[1], "author": "", "kind": "return", "level": "green",
                          "more": 0, "title": "Заказ № %s: возврат отмечен, ждёт суммы от менеджера" % mgr.label(card),
                          "text": "Сумму возврата вносит менеджер, после этого заказ уйдёт с точки.", "href": "/my-point"})
        if not reserve.question_open(conn, card, now):
            continue
        stay = re.sub(r"\D", "", card["entered_at"] or "")
        asked = (store.get_state(conn, reserve.ASK_KEY % (card_id, stay)) or "").split("|", 1)[-1]
        found.append({"id": "question-%d-%s" % (card_id, stay), "order_id": card_id, "at": asked,
                      "author": "", "kind": "question", "level": "yellow", "more": 0, "title": "Клиент забрал заказ № %s?" % mgr.label(card),
                      "text": "Ответьте на «Моей точке»: «Выдан» или «На точке».", "href": "/my-point"})
    return found


def nav_counts(conn, login: str) -> dict:
    """The menu counter: as many as rows on the page; red when one of them is red. A failure here never breaks the menu."""
    try:
        rows = grouped(conn, login)
    except Exception:
        log.exception("alerts counter failed")
        return {"alerts": 0, "alerts_red": False}
    return {"alerts": len(rows), "alerts_red": any(r["level"] == "red" for r in rows)}


def with_orders(conn, rows: list) -> list:
    """The rows with their order's number and client (a chat row without an order: its client); rows of vanished orders are dropped."""
    ids = sorted({r["order_id"] for r in rows if r["order_id"] is not None})
    names = {}
    if ids:
        names = {r[0]: {"client": r[1], "number": r[2]} for r in conn.execute(
            "SELECT id, client_name, webmim_number FROM order_card WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)}
    return [dict(r, order=names[r["order_id"]] if r["order_id"] is not None else {"client": r.get("client", ""), "number": ""})
            for r in rows if r["order_id"] is None or r["order_id"] in names]


BELL_ROWS = 10


def is_handoff(row: dict) -> bool:
    """A row «Клиент хочет заказать шины…» (order_manager.hand_over): it opens through /alerts/<id>/go and can be taken by hand."""
    return row.get("kind") == "handoff" and str(row.get("text") or "").startswith(mgr.ASK)


def is_return_ask(row: dict) -> bool:
    """The yellow «заполните сумму возврата» (or «не заполнено — …») of a return the point pressed (order_manager.returned): it asks the
    manager; the point has its green «возврат отмечен, ждёт суммы» (point_rows) instead (the owner 2026-10-27)."""
    text = str(row.get("text") or "")
    return row.get("kind") == "point" and (text in (mgr.RETURN_ASK, order_oth.SIGNED_ASK) or text.startswith(mgr.RETURN_GAPS.split("%s")[0]))


def is_handoff_cancel(row: dict) -> bool:
    """The yellow «Точка … отменила передачу клиента» (order_manager.cancel_handoff): for the person who would take it, never the point's bell."""
    return row.get("kind") == "handoff" and mgr.CANCELLED_MARK in str(row.get("text") or "")


def may_take() -> bool:
    """Who «берёт в работу» a handed-over client: the people who work the orders (owner, partner, the chats' manager); never a point or
    «Наблюдатель». The same rule in «Заказы» (the card) and «Чаты» (an answer)."""
    return own_point() is None and can("orders", "edit")


def acceptable(rows: list) -> list:
    """The rows «Принять все» takes (the owner 2026-10-27): the records of the order journal only (a numeric id), red, yellow and green.
    Left out, each by its own button: the chat rows («Клиент написал» of a chat without an order, «Срочно ответить»), «Перезвонить»,
    «Входящие», the point's questions (string ids), and the handed-over clients (a press there «берёт в работу»)."""
    return [r for r in rows if isinstance(r["id"], int) and r["order_id"] is not None and not is_handoff(r) and r["level"] in order_alerts.ALERT_LEVELS]


def href(row: dict) -> str:
    if row.get("href"):                                           # «Перезвонить» (inquiry.due_rows) knows its own place
        return row["href"]
    if row.get("chat_row"):
        return "/chats/%d" % row["chat_row"]
    return "/alerts/%d/go" % row["id"] if is_handoff(row) else "/orders/%d" % row["order_id"]


def register(app):
    @app.route("/alerts")
    @login_required
    def alerts_page():
        conn = get_conn()
        rows = [dict(r, href=href(r), handoff=is_handoff(r)) for r in with_orders(conn, grouped(conn, session["user"]))]
        take = acceptable(rows)
        return render_template("alerts.html", rows=rows, staff=may_take(), acceptable=take, confirm=bool(take) and request.args.get("accept") == "1")

    @app.route("/alerts/feed")
    @login_required
    def alerts_feed():
        """The bell: the count (as the menu counter) and the latest rows of the page. Read-only."""
        conn = get_conn()
        all_rows = grouped(conn, session["user"])
        short_dt = current_app.jinja_env.filters["short_dt"]
        items = [{"id": r["id"], "order_id": r["order_id"], "level": r["level"], "client": r["order"]["client"] or "",
                  "text": r["text"], "more": r["more"], "at": short_dt(r["at"]),
                  "href": href(r),
                  "title": r.get("title") or ("Чат без заказа" if r.get("chat_row") else "Заказ №%d" % r["order_id"])}
                 for r in with_orders(conn, all_rows)[:BELL_ROWS]]
        return jsonify({"count": len(all_rows), "red": any(r["level"] == "red" for r in all_rows), "items": items})

    def _handoff_event(conn, event_id) -> dict:
        row = conn.execute("SELECT id, order_id, kind, text FROM order_event WHERE id = ?", (event_id,)).fetchone()
        if row is None:
            abort(404)
        return {"id": row[0], "order_id": row[1], "kind": row[2], "text": row[3]}

    @app.route("/alerts/<int:event_id>/go")
    @login_required
    def alerts_go(event_id):
        """The alert of a handed-over client opened: the staff «взяли в работу» (never the point), then the chat it came from, else the order."""
        conn = get_conn()
        event = _handoff_event(conn, event_id)
        if event["order_id"] is None:
            abort(404)
        if own_point() is None and is_handoff(event) and from_panel():          # from another site: the order only, nobody «взял» (хвост З20)
            if may_take():
                mgr.take(conn, event["order_id"], "открыл уведомление", session["user"], local_iso())
            chat_row = mgr.handoff_target(conn, event["order_id"])
            client = mgr.handoff_client(conn, event["order_id"]) if not chat_row and can("clients") else None
            if chat_row or client:                                # the chat, the client of the call (its recording): the alert is seen now
                order_alerts.mark_seen(conn, session["user"], event["order_id"], local_iso(), event["id"])
                conn.commit()
                return redirect("/chats/%d" % chat_row if chat_row else "/clients/%d" % client)
        return redirect("/orders/%d" % event["order_id"])

    @app.route("/alerts/<int:event_id>/take", methods=["POST"])
    @login_required
    def alerts_take(event_id):
        """«Взять в работу» by hand on the alert of a handed-over client (the staff only: the point is not given this button)."""
        conn = get_conn()
        event = _handoff_event(conn, event_id)
        if not may_take() or not is_handoff(event):
            abort(403)
        if mgr.take(conn, event["order_id"], "вручную", session["user"], local_iso()):
            flash("Клиент взят в работу, заказ №%d: точка узнает об этом." % event["order_id"], "ok")
        else:
            flash("Клиент уже в работе (заказ №%d)." % event["order_id"])
        return redirect("/alerts")

    @app.route("/alerts/<int:event_id>/ack", methods=["POST"])
    @login_required
    def alerts_ack(event_id):
        conn = get_conn()
        try:
            order_alerts.ack(conn, event_id, session["user"], local_iso())
            conn.commit()
        except order_alerts.AlertError as err:
            flash(str(err), "error")
        return redirect("/alerts")

    @app.route("/alerts/accept-all", methods=["POST"])
    @login_required
    def alerts_accept_all():
        """«Принять все» for the current user: of the ids the page showed, those that are still rows of THEIR page now (a record that came
        after loading is not among them, another user's or a point's is never): red = «Разобрался» (order_alerts.ack, as a single press),
        yellow and green = seen up to that record (as opening the order)."""
        conn = get_conn()
        wanted = set()
        for i in request.form.getlist("ids"):
            try:
                wanted.add(int(i))
            except ValueError:
                pass
        now, login = local_iso(), session["user"]
        taken, accepted = 0, {}
        for row in acceptable(grouped(conn, login)):
            if row["id"] not in wanted:
                continue
            if row["level"] == "red":
                try:
                    order_alerts.ack(conn, row["id"], login, now)
                except order_alerts.AlertError:
                    continue
                taken += 1
            else:
                accepted.setdefault(row["order_id"], []).append(row["id"])
        for order_id, ids in accepted.items():
            # «seen» is a mark up to a record: an open handed-over client of the order (its button «Взять в работу» is its own) must not fall
            # under it, so the mark stops below the first one (what is above it stays). The point never sees these rows: no block for it.
            blocks = [] if own_point() is not None else [e["id"] for e in order_alerts.open_alerts(conn, login, order_id)
                                                         if is_handoff(e) and e["id"] not in ids]
            top = [i for i in ids if not blocks or i < min(blocks)]
            if top:
                order_alerts.mark_seen(conn, login, order_id, now, max(top))
                taken += len(top)
        conn.commit()
        flash("Принято уведомлений: %d." % taken, "ok")
        return redirect("/alerts")
