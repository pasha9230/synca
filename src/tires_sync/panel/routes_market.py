"""The snapshots of the competitors' prices from the owner's Mac (market.py; the plan price-parser, stage 0): one POST with a bearer token of
its own (as the photo agent's: only its sha256 is kept, a new one revokes the old, none issued — always 403), no session, no cookie, no CSRF.
The owner issues the token and stops the intake in the catalog (the right «money», as the purchase). The server never goes to Avito."""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone

from flask import abort, flash, jsonify, make_response, redirect, render_template, request

from .. import market, store
from .common import author, get_conn, login_required, now, now_iso
from .routes_main import context

TOKEN_KEY = "market_agent_token"
STOP_KEY = "market_intake_stopped"
MAX_BODY = 5 * 1024 * 1024
MAX_SEARCHES, MAX_LISTINGS = 500, 200                   # a run of the plan: 80–150 sizes, a page of a search has some fifty listings
EARLIEST, LATEST = timedelta(days=2), timedelta(hours=1)    # a run_at further from the server's clock is a wrong clock: refused
STOPPED = {"blocked": "прогон остановлен", "error": "ошибка прогона"}


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_token(conn) -> str:
    token = secrets.token_urlsafe(32)
    store.set_state(conn, TOKEN_KEY, _digest(token))
    return token


def authorized(conn) -> bool:
    header = request.headers.get("Authorization", "")
    sent = header[len("Bearer "):].strip() if header.startswith("Bearer ") else ""
    stored = store.get_state(conn, TOKEN_KEY) or ""
    return bool(sent and stored) and hmac.compare_digest(_digest(sent), stored)


def stopped(conn) -> bool:
    return store.get_state(conn, STOP_KEY) == "1"


def _our_codes(conn) -> set:
    """The codes of our feed (every account; «-x4» of a set is the same position)."""
    from .. import connections
    return {re.sub(r"-x\d+$", "", g) for acc in connections.list_feed_accounts(conn) for g in store.load_published(conn, acc["id"])}


def _payload(data):
    """(run_at in UTC, slot, note, searches) of the body, or an error text. `run_at` comes with its offset («2026-10-31T10:40:00+03:00»,
    «…Z»); without one it is UTC, as the server's clock: Moscow time without its offset is three hours ahead and refused."""
    if not isinstance(data, dict):
        return "нужен JSON-объект"
    run_at = str(data.get("run_at") or "")
    try:
        when = datetime.fromisoformat(run_at.replace("Z", "+00:00")) \
            if re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d(:\d\d)?(Z|[+-]\d\d:\d\d)?", run_at) else None
    except ValueError:
        when = None
    if when is not None and when.tzinfo is not None:
        when = when.astimezone(timezone.utc).replace(tzinfo=None)
    if when is None:
        return "run_at: дата и время ГГГГ-ММ-ДДTЧЧ:ММ:СС"
    clock = now().replace(tzinfo=None)
    if not clock - EARLIEST <= when <= clock + LATEST:
        return "run_at: время прогона далеко от часов сервера (%s)" % clock.isoformat(timespec="minutes")
    slot, status = str(data.get("slot") or ""), str(data.get("status") or "ok")
    if slot not in market.SLOTS:
        return "slot: morning или evening"
    if status not in ("ok", "blocked", "error"):
        return "status: ok, blocked или error"
    searches = data.get("searches") or []
    if not isinstance(searches, list) or len(searches) > MAX_SEARCHES or any(
            not isinstance(s, dict) or not isinstance(s.get("listings") or [], list) or len(s.get("listings") or []) > MAX_LISTINGS for s in searches):
        return "searches: список поисков (до %d), в каждом listings до %d" % (MAX_SEARCHES, MAX_LISTINGS)
    note = str(data.get("note") or "").strip()
    if status in STOPPED:
        note = STOPPED[status] + (": " + note if note else "")
    return when.isoformat(timespec="seconds"), slot, note[:200], searches


def register(app) -> None:
    @app.route("/api/market-snapshots", methods=["POST"])
    def market_intake():
        conn = get_conn()
        if not authorized(conn):
            abort(403)
        if stopped(conn):
            return jsonify(error="приём снимков остановлен владельцем"), 503
        request.max_content_length = MAX_BODY                 # before the body is read
        if (request.content_length or 0) > MAX_BODY:
            return jsonify(error="тело запроса больше %d МБ" % (MAX_BODY // 1024 // 1024)), 413
        try:
            parsed_body = _payload(request.get_json(silent=True))
        except RecursionError:
            parsed_body = "слишком глубокая вложенность JSON"
        if isinstance(parsed_body, str):
            return jsonify(error=parsed_body), 400
        run_at, slot, note, searches = parsed_body
        _conn, _settings, parsed, ref = context(app)
        ours = market.ours_from_parsed(parsed.offers, ref, store.load_brand_map(conn), store.load_confirmed_mappings(conn), _our_codes(conn))
        written = market.save(conn, ours, searches, run_at, slot, now_iso(), note, store.load_model_synonyms(conn))
        return jsonify(snapshots=written)

    @app.route("/catalog/market-token", methods=["POST"])
    @login_required
    def catalog_market_token():
        conn = get_conn()
        token = issue_token(conn)
        store.write_audit(conn, now_iso(), author(), "market_token", {})
        resp = make_response(render_template("market_token.html", token=token))     # never in a flash: the session cookie is not secret
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.route("/catalog/market-switch", methods=["POST"])
    @login_required
    def catalog_market_switch():
        conn = get_conn()
        stop = request.form.get("stop") == "1"
        store.set_state(conn, STOP_KEY, "1" if stop else "0")
        store.write_audit(conn, now_iso(), author(), "market_switch", {"stopped": stop})
        flash("Приём снимков цен остановлен." if stop else "Приём снимков цен включён.")
        return redirect("/catalog")
