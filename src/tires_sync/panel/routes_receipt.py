"""«Товарный чек» of an order (order_receipt, the owner 2026-11-13): the window of the card, its files and the send to the order's chat, and
«Настройки заказов › Товарный чек» with the seller's fields. Who sees the money of an order (the right «money»: the owner, the partner);
sending needs «отправлять клиенту» on top (common.NEEDS). A message to the client goes only by the press of «Сформировать и отправить в
чат», through chats.send_image (its mark, its log, its guards); the panel never sends a receipt by itself."""
from __future__ import annotations

import json
import secrets
from urllib.parse import quote

from flask import Response, abort, flash, redirect, render_template, request, session

from .. import chat_deals as deals, chats as chats_store, connections, order_receipt as receipts
from ..avito import AvitoClient
from ..transport import urllib_transport
from .common import author, get_conn, has_right, login_required, own_point
from .routes_orders import card_or_404, local_iso, local_now

SETTINGS_PAGE = "/orders/settings/receipt"
DRAFT = "receipt_draft_%d"                       # session: what the window sent and could not be made; the window shows it again
DRAFT_MAX = 2400                                 # the draft as JSON with \u escapes, at most: the session cookie stays small


def allowed() -> bool:
    return has_right("money") and own_point() is None


def send_chat(conn, card_id):
    """The chat a receipt goes to: the newest chat of the order whose account reads the chats and may answer; None without one."""
    able = {a["id"] for a in connections.list_accounts(conn) if a["chats_enabled"] and a["chats_send"]}
    return next((ch for ch in chats_store.chats_of_order(conn, card_id) if ch["account_id"] in able), None)


def register(app) -> None:
    def receipt_window(card) -> dict:
        """The values of the window «Товарный чек» of the card; None for who does not see the order's money."""
        if not allowed():
            return None
        conn = get_conn()
        try:
            values = receipts.defaults(conn, card, local_now().date().isoformat())
        except Exception:                                   # the window never takes the card down
            conn.rollback()
            app.logger.exception("the receipt window of order %s", card["id"])
            return None
        draft = session.pop(DRAFT % card["id"], None)
        if draft:                                           # what the person typed comes back with the error
            lines = [{"code": c, "name": n, "qty": q, "unit": u, "price": p} for c, n, q, u, p in zip(
                draft.get("line_code", []), draft.get("line_name", []), draft.get("line_qty", []), draft.get("line_unit", []), draft.get("line_price", []))]
            values = dict(values, **{k: (draft.get(k) or [""])[0] for k in list(receipts.SELLER_FIELDS) + ["number", "invoice_day", "day"]},
                          lines=lines)
        total = None
        try:
            total = receipts.total({"lines": [dict(l, price=receipts._amount(l["price"], ""), qty=receipts._amount(l["qty"], ""))
                                              for l in values["lines"] if l["name"]]})
        except receipts.ReceiptError:
            pass
        chat = send_chat(conn, card["id"])
        return {"values": values, "missing": receipts.missing(values), "total": receipts.money(total) if total is not None else "",
                "chat": dict(chat, ref=deals.ref(chat["id"])) if chat else None,
                "token": ((draft or {}).get("receipt_token") or [""])[0][:64] or secrets.token_hex(12),
                "kept": receipts.kept(conn, card["id"]), "open": bool(draft) or request.args.get("receipt") == "1",
                "needed": bool(card.get("receipt_needed")), "seller_settings": SETTINGS_PAGE}

    app.jinja_env.globals["receipt_window"] = receipt_window
    app.jinja_env.globals["receipt_money"] = receipts.money
    app.jinja_env.globals["receipt_qty"] = lambda q: receipts.quantity(receipts._amount(q, "")) if str(q).strip() else ""

    def guard():
        if not allowed():
            abort(403)

    def again(card_id, err, same_mark: bool = False):
        """Back to the window with the typed values and the error. `same_mark` (review 13.11): the picture may have gone or goes later (an
        unsure outcome, a 402 the sync sends again): the window keeps its mark, so a press again never sends a second receipt."""
        draft = {k: [v[:receipts.FIELD_MAX] for v in request.form.getlist(k)[:receipts.LINES_MAX + 1]]
                 for k in request.form if k not in ("csrf", "kind") and (same_mark or k != "receipt_token")}
        if len(json.dumps(draft)) <= DRAFT_MAX:          # the session is a cookie: a big window is not kept (its values come from the order again)
            session[DRAFT % card_id] = draft
        flash(str(err), "error")
        return redirect("/orders/%d?receipt=1#order-receipt" % card_id)

    def made(card_id):
        receipt = receipts.from_form(request.form)
        lacking = receipts.missing(receipt)
        if lacking:
            raise receipts.ReceiptError("Не заполнено: %s. Впишите в окне или в «Настройки заказов › Товарный чек»." % ", ".join(lacking))
        return receipt

    @app.route("/orders/<int:card_id>/receipt/file", methods=["POST"])
    @login_required
    def order_receipt_file(card_id):
        """The receipt of the window as a file: the PDF to download, the picture to look at. Nothing is kept, nothing is sent."""
        guard()
        card_or_404(get_conn(), card_id)
        try:
            receipt = made(card_id)
            body = receipts.jpeg(receipt) if request.form.get("kind") == "jpg" else receipts.pdf(receipt)
        except receipts.ReceiptError as err:
            return again(card_id, err)
        if request.form.get("kind") == "jpg":
            return Response(body, mimetype="image/jpeg", headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
        name = "receipt-%d.pdf" % card_id
        return Response(body, mimetype="application/pdf", headers={
            "Content-Disposition": "attachment; filename=\"%s\"" % name, "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    def writer(conn, chat):
        box = app.config.get("SECRET_BOX")
        if box is None:
            raise chats_store.ChatError("Мастер-ключ недоступен: ключи Авито не расшифровать.")
        client_id = connections.get_secret(conn, box, "avito", chat["account_id"], "client_id")
        client_secret = connections.get_secret(conn, box, "avito", chat["account_id"], "client_secret")
        if not client_id or not client_secret:
            raise chats_store.ChatError("У аккаунта не заданы ключи Авито.")
        return AvitoClient(client_id, client_secret, http=app.config.get("AVITO_HTTP") or urllib_transport, allow_write=True)

    @app.route("/orders/<int:card_id>/receipt/send", methods=["POST"])
    @login_required
    def order_receipt_send(card_id):
        """«Сформировать и отправить в чат»: the picture to the order's chat once per mark of the window (chats.send_image), then the receipt
        is kept with the order and the journal says so."""
        guard()
        conn = get_conn()
        card_or_404(conn, card_id)
        try:
            receipt = made(card_id)
            picture = receipts.jpeg(receipt)
        except receipts.ReceiptError as err:
            return again(card_id, err)
        chat = send_chat(conn, card_id)
        if chat is None:
            return again(card_id, "У заказа нет чата Авито, куда можно отправить: привяжите чат к заказу. Чек можно скачать.")
        token = str(request.form.get("receipt_token") or "").strip()[:64]
        now = local_iso()
        try:
            chats_store.send_image(conn, writer(conn, chat), chat["id"], picture, token, now, author())
        except chats_store.ChatSendUnknown as err:                     # may have gone, or goes with the sync (402): the same mark again
            return again(card_id, err, same_mark=True)
        except chats_store.ChatError as err:
            return again(card_id, err)
        receipts.keep(conn, card_id, token, receipt, chat["id"], now, author())
        flash("%s отправлен клиенту в чат %s." % (receipts.title(receipt), deals.ref(chat["id"])), "ok")
        return redirect("/orders/%d" % card_id)

    @app.route("/orders/<int:card_id>/receipts/<int:receipt_id>.<kind>")
    @login_required
    def order_receipt_kept(card_id, receipt_id, kind):
        guard()
        try:
            data = receipts.kept_file(get_conn(), card_id, receipt_id, kind)
        except receipts.ReceiptError:
            abort(404)
        if kind == "jpg":
            return Response(data, mimetype="image/jpeg", headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
        return Response(data, mimetype="application/pdf", headers={
            "Content-Disposition": "attachment; filename=\"receipt-%d-%d.pdf\"; filename*=UTF-8''%s" % (card_id, receipt_id, quote("чек-%d.pdf" % receipt_id)),
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @app.route(SETTINGS_PAGE)
    @login_required
    def orders_receipt_settings():
        guard()
        return render_template("orders_receipt_settings.html", seller=receipts.seller(get_conn()), fields=receipts.SELLER_FIELDS,
                               needed=receipts.NEEDED, field_max=receipts.FIELD_MAX)

    @app.route(SETTINGS_PAGE, methods=["POST"])
    @login_required
    def orders_receipt_settings_save():
        guard()
        try:
            changed = receipts.set_seller(get_conn(), request.form, local_iso(), author())
        except receipts.ReceiptError as err:
            flash(str(err), "error")
            return redirect(SETTINGS_PAGE)
        flash("Сохранено: %s." % ", ".join(changed) if changed else "Ничего не изменилось.", "ok" if changed else "info")
        return redirect(SETTINGS_PAGE)
