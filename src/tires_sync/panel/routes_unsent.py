"""«Не дошло клиенту» in the order card (unsent.py, the owner 2026-10-31): the block's rows for the template and «Отправил вручную». Section
«Заказы»: «менять» to mark it; a manager of a point by the orders of its own point only (common.check_access: `card_id` of the address).
Nothing is sent from here."""
from __future__ import annotations

from flask import abort, flash, redirect

from .. import chats, order_engine, unsent
from .common import author, get_conn, login_required, own_point
from .routes_orders import local_iso


def _task_mark(task_id):
    """The send mark of a rule's message task («task-…», order_engine.task_token); None for no such task."""
    try:
        return order_engine.task_token(order_engine.get_task(get_conn(), int(task_id)))
    except Exception:                                      # no such task: unsent.sent_by_hand says so
        return None


def _system(row: dict) -> bool:
    """A row of «Не дошло клиенту» of a system message: its send mark, or the rule's message task's (always the system's)."""
    return chats.is_system_mark(row.get("token")) if row.get("kind") == "send" else chats.is_system_mark(_task_mark(row.get("id")))


def register(app) -> None:
    def unsent_for(order_id) -> list:
        try:
            rows = unsent.of_order(get_conn(), order_id)
            if own_point() is not None:                    # a system message (its mark, chats.is_system_mark) is not the point's (token-guard)
                rows = [r for r in rows if not _system(r)]
            return rows
        except Exception:                                  # the block never takes the card down
            get_conn().rollback()
            app.logger.exception("the unsent messages of order %s could not be read", order_id)
            return []

    app.jinja_env.globals["unsent_for"] = unsent_for

    @app.route("/orders/<int:card_id>/unsent/<kind>/<int:item_id>", methods=["POST"])
    @login_required
    def order_unsent_by_hand(card_id, kind, item_id):
        if kind not in ("send", "task"):
            abort(404)
        if own_point() is not None:                                        # a system message is the office's (token-guard): refused, not only hidden
            if kind == "send":
                row = get_conn().execute("SELECT token FROM order_unsent WHERE id = ?", (item_id,)).fetchone()
                if row is not None and chats.is_system_mark(row[0]):
                    abort(403)
            elif _task_mark(item_id) is not None and chats.is_system_mark(_task_mark(item_id)):
                abort(403)
        try:
            unsent.sent_by_hand(get_conn(), card_id, kind, item_id, local_iso(), author())
            flash("Отмечено: сообщение клиенту отправлено вручную.", "info")
        except unsent.UnsentError as err:
            flash(str(err), "error")
        return redirect("/orders/%d" % card_id)
