"""API of the photo agent (a separate program on the owner's Mac): generation jobs, results, copy work, the review sheet.

Authentication is a bearer token only (never the panel session): its sha256 is kept in app_state, and it opens nothing but these paths."""
from __future__ import annotations

import hashlib
import hmac
import math
import secrets
from datetime import datetime, timedelta
from functools import wraps

from flask import abort, current_app, jsonify, request

from .. import connections, notify, photo_agent, photo_library, share_photos, store
from ..models import Season
from ..notify import NotifyError
from ..settings_store import active_settings
from ..transport import urllib_transport
from .common import get_conn, now_iso
from .data import load_merged, load_reference
from . import service

TOKEN_KEY = "image_agent_token"
MAX_JOBS = 10
MAX_UPLOAD = 30 * 1024 * 1024
COPY_KINDS = ("copy", "crop", "card")
MIME = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
STUCK_HOURS = 2


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_agent_token(conn) -> str:
    """A new agent token (shown once); only its hash is kept, and the previous token stops working."""
    token = secrets.token_urlsafe(32)
    store.set_state(conn, TOKEN_KEY, _digest(token))
    return token


def _authorized(conn) -> bool:
    """The owner's agent token, or the one-time token of the server's run going on now (photo_agent.tick_server)."""
    header = request.headers.get("Authorization", "")
    sent = header[len("Bearer "):].strip() if header.startswith("Bearer ") else ""
    if not sent:
        return False
    digest = _digest(sent)
    return any(stored and hmac.compare_digest(digest, stored)
               for stored in (store.get_state(conn, TOKEN_KEY) or "", photo_agent.server_token_digest(conn)))


def ref_image(url):
    """The supplier's sample fetched by the server (the agent and the owner's browser cannot reach the supplier host); 404 without one."""
    if not url:
        abort(404)
    fetch = current_app.config.get("PHOTO_FETCH") or share_photos.fetch
    try:
        data, kind = fetch(url)
    except share_photos.PhotoError:
        abort(502)
    if kind not in MIME:
        abort(502)
    response = current_app.response_class(data, mimetype=MIME[kind])
    response.headers["Cache-Control"] = "private, no-store"
    return response


def agent_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not _authorized(get_conn()):
            abort(403)
        request.max_content_length = MAX_UPLOAD               # before any body is read: pictures are bigger than the panel's usual uploads
        return view(*args, **kwargs)

    return wrapper


def _base(conn) -> str:
    return (store.get_state(conn, "public_base") or request.url_root).rstrip("/")


def _photo_url(conn, photo_id: int) -> str:
    sha, ext = conn.execute("SELECT sha256, ext FROM photo WHERE id=?", (photo_id,)).fetchone()
    return photo_library.photo_url(_base(conn), sha, ext)


def _approved(conn, key: str, kind: str):
    row = conn.execute("SELECT photo_id FROM set_frame WHERE set_key=? AND kind=? AND status='approved'", (key, kind)).fetchone()
    return row[0] if row else None


def _upload():
    """(bytes of the `file` part) or a 400."""
    file = request.files.get("file")
    data = file.read() if file else b""
    if not data:
        abort(400)
    return data


def set_positions(conn, config) -> dict:
    """Set key -> the offers of the set that are in the feed now (evaluated like the photo queue; stock at or above the threshold)."""
    settings = active_settings(conn)
    parsed = load_merged(conn, settings, config["UNLOAD_PATH"])
    ev = service.evaluation(conn, settings, parsed, load_reference(config["REFERENCE_PATH"]))
    groups = {}
    alias = photo_library.aliases(conn)
    for cand in ev.ok.values():
        offer = cand.offer
        if offer.stock < settings.min_stock:
            continue
        key = photo_library.set_key_for(offer)
        if key is not None:
            groups.setdefault(alias.get(key.text(), key.text()), []).append(offer)
    return groups


def _season_line(season: str, studded: bool):
    if season == Season.WINTER.value:
        return "Зима · шипы" if studded else "Зима · без шипов"
    if season == Season.SUMMER.value:
        return "Лето"
    return None


def register(app) -> None:
    @app.route("/api/image-jobs")
    @agent_required
    def agent_jobs():
        conn = get_conn()
        try:
            limit = max(1, min(MAX_JOBS, int(request.args.get("limit", "1"))))
        except ValueError:
            abort(400)
        stamp = now_iso()
        cutoff = (datetime.fromisoformat(stamp) - timedelta(hours=STUCK_HOURS)).isoformat(timespec="seconds")
        with conn:                                              # an agent that died: its job is a failed attempt, not a job running forever
            conn.execute("UPDATE image_job SET status='failed', error='timeout', updated=? WHERE status='running' AND updated < ?", (stamp, cutoff))
        if photo_library.queue_stopped(conn) or photo_library.spent_today(conn, stamp) >= photo_library.DAILY_CAP_USD - 1e-9:
            return jsonify([])
        rows = conn.execute(
            "SELECT j.id, j.kind, s.key, s.brand_name, s.model_name, s.season, s.studded, s.size_class, s.size_hint FROM image_job j"
            " JOIN photo_set s ON s.key = j.set_key WHERE j.status = 'queued' AND s.state = ? ORDER BY s.rank, j.id",
            (photo_library.STATE_NEEDED,)).fetchall()
        rush = photo_library.rush_keys(conn)                    # the models asked for by name first; `only=rush`: nothing else
        if request.args.get("only") == "rush":
            rows = [r for r in rows if r[2] in rush]
        rows = sorted(rows, key=lambda r: rush.index(r[2]) if r[2] in rush else len(rush))[:limit]
        base = _base(conn)
        jobs = []
        with conn:
            for job_id, kind, key, brand, model, season, studded, cls, hint in rows:
                if conn.execute("UPDATE image_job SET status='running', attempts=attempts+1, updated=? WHERE id=? AND status='queued'", (stamp, job_id)).rowcount != 1:
                    continue
                approved = _approved(conn, key, "three_quarter")
                jobs.append({"id": job_id, "kind": kind, "set_key": key, "brand": brand, "model": model, "season": season,
                             "studded": bool(studded), "size_class": cls, "size_hint": hint,
                             "ref_url": "%s/api/image-jobs/%d/ref" % (base, job_id),
                             "approved_three_quarter_url": _photo_url(conn, approved) if approved else None})
        return jsonify(jobs)

    @app.route("/api/image-jobs/work")
    @agent_required
    def agent_work():
        """Is there work: the run the owner asked for in the panel (scope all | rush) and copies due; nothing = the agent exits."""
        return jsonify(photo_agent.work(get_conn(), now_iso()))

    @app.route("/api/image-jobs/run-done", methods=["POST"])
    @agent_required
    def agent_run_done():
        form = request.form
        try:
            done, failed = int(form.get("done", "0") or 0), int(form.get("failed", "0") or 0)
        except ValueError:
            abort(400)
        if not (0 <= done <= 10000 and 0 <= failed <= 10000):
            abort(400)
        photo_agent.finish(get_conn(), now_iso(), form.get("request_at", "")[:40], form.get("copies_at", "")[:40], done, failed,
                           form.get("message", ""))
        return jsonify({"ok": True})

    @app.route("/api/image-jobs/<int:job_id>/ref")
    @agent_required
    def agent_ref(job_id):
        row = get_conn().execute("SELECT s.ref_url FROM image_job j JOIN photo_set s ON s.key = j.set_key WHERE j.id = ?", (job_id,)).fetchone()
        return ref_image(row[0] if row else None)

    @app.route("/api/image-jobs/<int:job_id>/result", methods=["POST"])
    @agent_required
    def agent_result(job_id):
        conn = get_conn()
        job = conn.execute("SELECT set_key, kind, status FROM image_job WHERE id=?", (job_id,)).fetchone()
        if job is None:
            abort(404)
        if job[2] != "running":
            abort(409)
        try:
            cost = float(request.form.get("cost_usd", "0") or 0)
        except ValueError:
            abort(400)
        if not math.isfinite(cost) or cost < 0 or cost > 100:
            abort(400)
        data = _upload()
        stamp = now_iso()
        try:
            photo_id = photo_library.save_photo(conn, current_app.config["PHOTOS_ROOT"], data, "agent", stamp)
        except ValueError:
            abort(400)
        with conn:
            conn.execute("INSERT INTO set_frame (set_key, kind, photo_id, status, created) VALUES (?,?,?,?,?)", (job[0], job[1], photo_id, "pending", stamp))
            conn.execute("UPDATE image_job SET status='done', cost_usd=?, error='', updated=? WHERE id=?", (cost, stamp, job_id))
        return jsonify({"photo_id": photo_id}), 201

    @app.route("/api/image-jobs/<int:job_id>/fail", methods=["POST"])
    @agent_required
    def agent_fail(job_id):
        conn = get_conn()
        job = conn.execute("SELECT status FROM image_job WHERE id=?", (job_id,)).fetchone()
        if job is None:
            abort(404)
        if job[0] != "running":
            abort(409)
        error = request.form.get("error", "")[:500]
        try:
            cost = float(request.form.get("cost_usd", "0") or 0)       # money spent before the failure (e.g. upload lost)
        except ValueError:
            abort(400)
        if not math.isfinite(cost) or cost < 0 or cost > 100:
            abort(400)
        # nothing paid and the job not to blame (OpenAI unreachable, key, money): back to the queue, not one of the 3 attempts
        status = "queued" if request.form.get("requeue") == "1" and cost == 0 else "failed"
        with conn:
            conn.execute("UPDATE image_job SET status=?, error=?, cost_usd=?, updated=? WHERE id=?", (status, error, cost, now_iso(), job_id))
        return jsonify({"ok": True})

    @app.route("/api/photo-copies/todo")
    @agent_required
    def agent_copies_todo():
        conn = get_conn()
        accounts = connections.list_feed_accounts(conn)
        settings = active_settings(conn)
        effective = {a["id"]: connections.effective_settings(conn, a["id"], settings) for a in accounts}
        have = {tuple(r) for r in conn.execute("SELECT account_id, source_photo_id, kind, variant FROM account_photo")}
        positions = set_positions(conn, current_app.config)
        todo = []

        def add(account_id, photo_id, kind, variant="", card=None):
            if (account_id, photo_id, kind, variant) not in have:
                todo.append({"account_id": account_id, "source_photo_id": photo_id, "source_url": _photo_url(conn, photo_id),
                             "kind": kind, "variant": variant, "card": card})

        for key, brand, season, studded in conn.execute(
                "SELECT s.key, s.brand_key, s.season, s.studded FROM photo_set s WHERE EXISTS"
                " (SELECT 1 FROM set_frame f WHERE f.set_key = s.key AND f.status = 'approved') ORDER BY s.rank, s.key"):
            for kind in photo_library.FRAME_KINDS:
                frame = _approved(conn, key, kind)
                if frame is None:
                    continue
                for acct in accounts:
                    if acct["id"] != connections.PRIMARY_ACCOUNT:
                        add(acct["id"], frame, "copy")
            three_quarter = _approved(conn, key, "three_quarter")
            if three_quarter is None:
                continue
            line = _season_line(season, bool(studded))
            for acct in accounts:
                add(acct["id"], three_quarter, "crop")
                if line is None:
                    continue
                seen = set()
                for offer in sorted(positions.get(key, []), key=lambda o: o.goods_id):
                    size = photo_library.card_size_text(offer)
                    for suffix in ("x1", "x4"):
                        variant = photo_library.card_variant(offer, suffix == "x4")
                        if variant in seen or (suffix == "x4" and not effective[acct["id"]].pack4_for(offer)):
                            continue
                        seen.add(variant)
                        add(acct["id"], three_quarter, "card", variant,
                            {"size": size, "season_line": line, "set_line": "Комплект 4 шт." if suffix == "x4" else None})
        return jsonify(todo)

    @app.route("/api/photo-copies", methods=["POST"])
    @agent_required
    def agent_copy_upload():
        conn = get_conn()
        kind = request.form.get("kind", "")
        variant = request.form.get("variant", "")
        try:
            account_id = int(request.form.get("account_id", ""))
            source_id = int(request.form.get("source_photo_id", ""))
        except ValueError:
            abort(400)
        if kind not in COPY_KINDS or len(variant) > 200:
            abort(400)
        if account_id not in {a["id"] for a in connections.list_feed_accounts(conn)}:
            abort(400)
        if kind == "copy" and account_id == connections.PRIMARY_ACCOUNT:
            abort(400)
        if conn.execute("SELECT 1 FROM set_frame WHERE photo_id=? AND status='approved'", (source_id,)).fetchone() is None:
            abort(400)
        data = _upload()
        try:
            photo_id = photo_library.save_photo(conn, current_app.config["PHOTOS_ROOT"], data, kind, now_iso())
        except ValueError:
            abort(400)
        with conn:
            conn.execute("INSERT OR REPLACE INTO account_photo (account_id, source_photo_id, kind, variant, photo_id) VALUES (?,?,?,?,?)",
                         (account_id, source_id, kind, variant, photo_id))
        return jsonify({"photo_id": photo_id}), 201

    @app.route("/api/review-sheet", methods=["POST"])
    @agent_required
    def agent_review_sheet():
        conn = get_conn()
        row = conn.execute("SELECT brand_name, model_name, season, studded, size_class FROM photo_set WHERE key=?", (request.form.get("set_key", ""),)).fetchone()
        if row is None:
            abort(404)
        data = _upload()
        if not data.startswith(b"\xff\xd8\xff"):
            abort(400)
        brand, model, season, studded, cls = row
        what = _season_line(season, bool(studded)) or season
        caption = "Фото на проверке: %s %s, %s, %s\n%s/photos/review" % (brand, model, what, cls, _base(conn))
        send = notify.notifier_photo_for(conn, current_app.config.get("SECRET_BOX"), http=current_app.config.get("TELEGRAM_HTTP") or urllib_transport)
        if send is None:
            current_app.logger.info("review sheet for %s not sent: Telegram is not configured", request.form.get("set_key"))
            return "", 204
        try:
            send(caption, data)
        except NotifyError as exc:
            return jsonify({"error": str(exc)}), 502
        return jsonify({"ok": True})
