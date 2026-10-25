"""«Подключения → ВК»: the community that writes to the points' managers, and the address VK calls with their answers."""
from __future__ import annotations

import logging
import threading

from flask import flash, redirect, render_template, request

from .. import chats, connections, order_notify, store, vk_callback, vk_send
from ..avito import AvitoClient
from ..secretbox import SecretKeyError
from ..transport import urllib_transport
from .common import author, get_conn, login_required, now, now_iso
from .routes_connections import NO_KEY, _save_secrets

BOT = "бот ВК"


def channel(app, conn, local: str):
    """What the bot needs to act on a manager's message: (send to a manager in VK, deliver to a client in Avito, alert about an order).
    Each is built from the saved keys; a missing key is a refusal at the moment of use. `local`: the panel time of the message (Moscow).
    Needs no request: it also runs after the answer to VK has gone."""
    box = app.config.get("SECRET_BOX")
    vk_http = app.config.get("VK_HTTP") or urllib_transport
    sender = vk_send.sender_for(conn, box, http=vk_http)

    def deliver(chat_row, text, token):
        chat = chats.get_chat(conn, chat_row)
        if box is None or chat is None:
            raise chats.ChatError("Мастер-ключ недоступен: ключи Авито не расшифровать.")
        client_id = connections.get_secret(conn, box, "avito", chat["account_id"], "client_id")
        client_secret = connections.get_secret(conn, box, "avito", chat["account_id"], "client_secret")
        if not client_id or not client_secret:
            raise chats.ChatError("У аккаунта не заданы ключи Авито.")
        client = AvitoClient(client_id, client_secret, http=app.config.get("AVITO_HTTP") or urllib_transport, allow_write=True)
        chats.send_reply(conn, client, chat_row, text, token, local, BOT)

    def notify(card, text):
        return order_notify.deliver(conn, box, card, text, local, tg_http=app.config.get("TG_HTTP") or urllib_transport, vk_http=vk_http)
    return sender, deliver, notify


def after_answer(app, job) -> None:
    """The work on a VK message runs right after «ok» has gone, in this process, on its own connection to the base. VK does not say how long
    it waits; it repeats an event in 10 seconds when the answer is late, and stops calling a server that fails several times in a row,
    while a message to Avito alone may take up to a minute (two calls, 30 seconds each at worst). `VK_RUN` (tests) runs it in place."""
    def work():
        conn = store.connect(app.config["DB_PATH"])
        try:
            job(conn)
        except Exception:
            logging.getLogger("tires_sync.vk").exception("the work on a VK message failed")
        finally:
            conn.close()
    runner = app.config.get("VK_RUN")
    if runner is not None:
        runner(work)
    else:
        threading.Thread(target=work, name="vk-message", daemon=True).start()


def register(app) -> None:
    @app.route("/connections/vk")
    @login_required
    def connections_vk_page():
        conn = get_conn()
        box = app.config.get("SECRET_BOX")
        tabs = {"active": "vk", "account_id": None, "supplier_id": None, "suppliers": [], "accounts": []}
        return render_template(
            "conn_vk.html", tabs=tabs, box_ok=box is not None, vk=dict({"group_id": "", "confirmation": ""}, **connections.get_config(conn, "vk", 0)),
            vk_secrets=connections.secret_status(conn, box, "vk", 0), hook_url="%shook/vk/%s" % (request.url_root, vk_callback.path_token(conn)),
            events=vk_callback.recent(conn))

    @app.route("/connections/vk", methods=["POST"])
    @login_required
    def connections_vk():
        conn = get_conn()
        group_id = request.form.get("group_id", "").strip()
        confirmation = request.form.get("confirmation", "").strip()
        secret = request.form.get("callback_secret", "").strip()
        if not (group_id.isascii() and group_id.isdigit() and len(group_id) <= 20):
            flash("Номер сообщества: только цифры.")
        elif not vk_callback.CONFIRMATION.match(confirmation):
            flash("Строка подтверждения: латинские буквы и цифры, до 32 знаков (как в окне ВК).")
        elif secret and not vk_callback.SECRET.match(secret):
            flash("Секретный ключ: латинские буквы, цифры, «_» и «-», до 50 знаков.")
        else:
            connections.set_config(conn, "vk", 0, {"group_id": group_id, "confirmation": confirmation}, now_iso(), author())
            _save_secrets(app, "vk", 0, [("callback_secret", secret), ("token", request.form.get("token", ""))])
            flash("Настройки ВК сохранены.")
        return redirect("/connections/vk")

    # ---- public: VK calls it; no login, no cookie, no CSRF token (VK cannot have one)
    @app.route("/hook/vk/<token>", methods=["POST"])
    def vk_hook(token):
        conn = get_conn()
        status, text = vk_callback.handle(conn, app.config.get("SECRET_BOX"), token, request.get_data(cache=False, as_text=False)[:20000], now(),
                                          channel=lambda db, local: channel(app, db, local), run=lambda job: after_answer(app, job))
        return app.response_class(text, status=status, mimetype="text/plain")
