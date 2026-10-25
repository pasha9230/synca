"""The address Avito's messenger webhook calls (avito_webhook), and its state on the page of an Avito account."""
from __future__ import annotations

import logging
import threading
from datetime import timedelta

from flask import request

from .. import avito_webhook, store
from .common import get_conn, now


def after_answer(app, job) -> None:
    """The message is kept beside the answer «ok» (a thread started before it goes), in this process, on its own connection to the base (Avito waits 2 seconds,
    a busy base may hold a write longer). `AVITO_HOOK_RUN` (tests) runs it in place. A failure is only logged: the reading every minute
    brings the message anyway."""
    def work():
        conn = store.connect(app.config["DB_PATH"])
        try:
            job(conn)
        except Exception:
            conn.rollback()
            logging.getLogger("tires_sync.avito_webhook").exception("a message of Avito's webhook was not kept")
        finally:
            conn.close()
    runner = app.config.get("AVITO_HOOK_RUN")
    if runner is not None:
        runner(work)
    else:
        threading.Thread(target=work, name="avito-webhook", daemon=True).start()


def register(app) -> None:
    def webhook_state(account_id):
        return avito_webhook.state(get_conn(), account_id, now() + timedelta(hours=3))

    app.jinja_env.globals["avito_webhook_state"] = webhook_state

    # ---- public: Avito calls it; no login, no cookie, no CSRF token (the /hook/ prefix), the secret in the address is the guard
    @app.route("/hook/avito/<secret>", methods=["POST"])
    def avito_hook(secret):
        try:
            conn = get_conn()
            if avito_webhook.account_of(conn, secret) is None:
                return app.response_class("", status=404, mimetype="text/plain")
            if (request.content_length or 0) > avito_webhook.MAX_BODY:
                return app.response_class("", status=413, mimetype="text/plain")
            body = request.stream.read(avito_webhook.MAX_BODY + 1)                     # no length given (chunked): still read no more than this
            local = (now() + timedelta(hours=3)).isoformat(timespec="seconds")        # the chats keep Moscow time
            status, text = avito_webhook.handle(conn, secret, body, local, run=lambda job: after_answer(app, job))
        except Exception as err:      # noqa: BLE001  Flask would log the failure with the address, that is with the secret
            logging.getLogger("tires_sync.avito_webhook").error("Avito's webhook: the request failed (%s)", type(err).__name__)
            return app.response_class("", status=503, mimetype="text/plain")
        return app.response_class(text, status=status, mimetype="text/plain")

