"""«Изменить цену просмотра» (the owner 2026-11-13, step 2b): the window of one ad of «Аналитика рекламы» and its writes (ads_bid_set).
The owner and the partner only: the window is in «analytics» (the money right), the writes in «orders» with the money right (the partner
changes nothing in «analytics»), and each view checks the role itself. One ad a press; the client that may write is made in the press."""
from __future__ import annotations

import secrets
from datetime import timezone

from flask import abort, current_app, flash, redirect, render_template, request

from .. import ads_bid_set, ads_spend, connections
from ..amounts import parse_amount
from ..avito import AvitoClient
from ..transport import urllib_transport
from .common import author, current_user, get_conn, local_today, login_required, now, now_iso
from .routes_orders import local_iso

ROLES = frozenset({"owner", "partner"})


def _who() -> None:
    if current_user()["role"] not in ROLES:
        abort(403)


def _epoch() -> float:
    return now().replace(tzinfo=timezone.utc).timestamp()


def _keys(conn, account_id: int):
    box = current_app.config.get("SECRET_BOX")
    if box is None:
        return None
    client_id = connections.get_secret(conn, box, "avito", account_id, "client_id")
    client_secret = connections.get_secret(conn, box, "avito", account_id, "client_secret")
    return (client_id, client_secret) if client_id and client_secret else None


def _client(keys, write: bool = False) -> AvitoClient:
    return AvitoClient(keys[0], keys[1], http=current_app.config.get("AVITO_HTTP") or urllib_transport, allow_write=write)


def _ad(conn, account_id: int, avito_id: int) -> dict:
    """The feed account and the ad of it (known to the base: the list of ads, the prices read or the spending); else 404."""
    account = connections.get_account(conn, account_id)
    if account is None or not account.get("feed_enabled"):
        abort(404)
    known = any(conn.execute(q, (account_id, avito_id)).fetchone() for q in (
        "SELECT 1 FROM avito_ad WHERE account_id = ? AND avito_id = ?", "SELECT 1 FROM ad_bid WHERE account_id = ? AND avito_id = ?",
        "SELECT 1 FROM ad_spend_day WHERE account_id = ? AND avito_id = ? LIMIT 1"))
    if not known or avito_id <= 0:
        abort(404)
    title = conn.execute("SELECT title FROM avito_ad WHERE account_id = ? AND avito_id = ?", (account_id, avito_id)).fetchone()
    return {"account": account, "title": (title[0] if title else "") or "№%d" % avito_id}


def _amount(name: str):
    text = request.form.get(name, "").strip()
    if not text:
        return None
    try:
        return parse_amount(text)
    except ValueError:
        raise ads_bid_set.BidError("«%s»: нужно число." % {"bid": "Цена просмотра", "own": "Своё значение", "limit": "Сколько тратить в день"}.get(name, name))


def _seen():
    """«Было» the form showed (the price and the limit; empty: none); None when the form has no such fields (an old form)."""
    if "was_bid" not in request.form or "was_limit" not in request.form:
        return None
    return {"bid": _amount("was_bid"), "limit": _amount("was_limit")}


def register(app) -> None:
    def page(account_id: int, avito_id: int) -> str:
        return "/economics/ads/bid/%d/%d" % (account_id, avito_id)

    @app.route("/economics/ads/bid/<int:account_id>/<int:avito_id>")
    @login_required
    def ads_bid_page(account_id, avito_id):
        """The window: a fresh read of the ad (not after a write: the control read is shown), the options, the fields, «было → стало»."""
        _who()
        conn = get_conn()
        ad = _ad(conn, account_id, avito_id)
        keys = _keys(conn, account_id)
        error = ""
        if keys is None:
            error = "У аккаунта не заданы ключи Авито: менять цену нельзя."
        elif request.args.get("after") != "1":
            try:
                ads_bid_set.read_fresh(conn, _client(keys), account_id, avito_id, now_iso(), _epoch())
            except ads_bid_set.BidError as err:
                error = str(err)
            except Exception:                                              # noqa: BLE001 — a network failure: the window says so
                current_app.logger.exception("cpxpromo read of an ad failed")
                error = "Авито не ответил: цена не прочитана. Попробуйте позже."
        state = ads_bid_set.current(conn, account_id, avito_id)
        feed = [a["id"] for a in connections.list_feed_accounts(conn)]
        near = ads_spend.near_options(state["options"], state["bid"] if state and state["bid"] is not None else (state or {}).get("rec_bid")) if state else []
        return render_template("ads_bid.html", ad=ad, account_id=account_id, avito_id=avito_id, state=state, error=error,
                               can_write=keys is not None and not error and bool(state and state.get("action_type")),
                               short=ads_spend.bid_short(state) if state else "", near=near,
                               drr=ads_bid_set.drr30(conn, account_id, avito_id, local_today(), feed), warn=ads_bid_set.threshold(conn),
                               jump=ads_bid_set.JUMP, undo=ads_bid_set.undo_values(conn, account_id, avito_id),
                               history=ads_bid_set.history(conn, account_id, avito_id, 5), token=secrets.token_hex(16))

    def write(account_id: int, avito_id: int, bid, limit, kind: str):
        conn = get_conn()
        ad = _ad(conn, account_id, avito_id)
        keys = _keys(conn, account_id)
        if keys is None:
            raise ads_bid_set.BidError("У аккаунта не заданы ключи Авито.")
        result = ads_bid_set.change(conn, ad["account"], avito_id, bid, limit, confirmed=request.form.get("confirm") == "1",
                                    token=request.form.get("token", ""), at=local_iso(), stamp=now_iso(), now=_epoch(), author=author(),
                                    writer=lambda: _client(keys, write=True), kind=kind, seen=_seen(), reader=lambda: _client(keys))
        flash(result["text"], "ok" if result["ok"] and not result["duplicate"] else ("info" if result["duplicate"] else "error"))

    @app.route("/economics/ads/bid/<int:account_id>/<int:avito_id>", methods=["POST"])
    @login_required
    def ads_bid_set_price(account_id, avito_id):
        """«Записать на Авито»: an option of Avito or the own value, and the daily limit."""
        _who()
        try:
            pick = request.form.get("pick", "")
            bid = _amount("own") if pick == "own" else _amount("pick") if pick else None
            write(account_id, avito_id, bid, _amount("limit"), "set")
        except ads_bid_set.BidError as err:
            flash(str(err), "error")
            return redirect(page(account_id, avito_id) + "?after=1")
        return redirect(page(account_id, avito_id) + "?after=1")

    @app.route("/economics/ads/bid/<int:account_id>/<int:avito_id>/undo", methods=["POST"])
    @login_required
    def ads_bid_undo(account_id, avito_id):
        """«Вернуть прежнюю»: the price and the limit before the last change through the panel (the journal), through the same checks."""
        _who()
        conn = get_conn()
        _ad(conn, account_id, avito_id)
        undo = ads_bid_set.undo_values(conn, account_id, avito_id)
        try:
            if undo is None:
                raise ads_bid_set.BidError("Через панель цену этого объявления не меняли: возвращать нечего.")
            if undo["bid"] is None:
                raise ads_bid_set.BidError(undo["note"])
            state = ads_bid_set.current(conn, account_id, avito_id) or {}
            write(account_id, avito_id, undo["bid"], undo["limit"] if undo["limit"] is not None else state.get("day_limit"), "undo")
        except ads_bid_set.BidError as err:
            flash(str(err), "error")
        return redirect(page(account_id, avito_id) + "?after=1")

    @app.route("/economics/ads/bid-threshold", methods=["POST"])
    @login_required
    def ads_bid_threshold():
        """The daily limit above which «Подтверждаю» is wanted (the section's own setting)."""
        _who()
        back = request.form.get("back", "")
        back = back if back.startswith("/economics/ads/bid/") else "/economics/ads"
        try:
            value = _amount("threshold")
            changed = ads_bid_set.set_threshold(get_conn(), value or 0, local_iso(), author())
            flash("Порог сохранён." if changed else "Порог не менялся.", "ok" if changed else "info")
        except ads_bid_set.BidError as err:
            flash(str(err), "error")
        return redirect(back + ("?after=1" if back != "/economics/ads" else ""))
