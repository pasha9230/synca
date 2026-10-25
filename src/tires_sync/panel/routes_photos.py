"""Content-addressed photos `/photos/<sha256>.<jpg|png>`: public, no login (nginx serves them in production; this is for development and tests).

The pattern admits only 64 hex digits, so it cannot shadow the login-protected `/photos/...` pages."""
from __future__ import annotations

import re
from typing import Dict
from pathlib import Path

from flask import abort, flash, make_response, redirect, render_template, request, send_file

from .. import connections, photo_agent, photo_library, store
from ..secretbox import SecretKeyError
from .common import author, get_conn, login_required, now_iso
from .routes_agent import issue_agent_token, ref_image

NAME = re.compile(r"^[0-9a-f]{64}\.(jpg|png)$")
KINDS = {"jpg": "image/jpeg", "png": "image/png"}
TABS = ("pending", "approved", "decision", "queue", "models")
SHOW = ("todo", "done", "all")
SWITCH_KEY = "own_photos"


def _dicts(conn, sql: str, args=()) -> list:
    cur = conn.execute(sql, args)
    names = [d[0] for d in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def _thumb(conn, base: str, photo_id: int):
    row = conn.execute("SELECT sha256, ext FROM photo WHERE id=?", (photo_id,)).fetchone()
    return photo_library.photo_url(base, row[0], row[1]) if row else None


def review_rows(conn, tab: str, base: str) -> list:
    """Sets for the review tab, each with its frames and the account crops/cards/copies made from them."""
    sets = {r["key"]: dict(r, frames=[]) for r in _dicts(conn, "SELECT rowid AS set_id, * FROM photo_set ORDER BY rank, key")}
    for f in _dicts(conn, "SELECT * FROM set_frame ORDER BY id"):
        if f["set_key"] not in sets:
            continue
        derived = [{"account_id": a["account_id"], "kind": a["kind"], "url": _thumb(conn, base, a["photo_id"])}
                   for a in _dicts(conn, "SELECT * FROM account_photo WHERE source_photo_id=? ORDER BY kind, account_id", (f["photo_id"],))]
        sets[f["set_key"]]["frames"].append(dict(f, url=_thumb(conn, base, f["photo_id"]), derived=derived))
    rows = []
    for s in sets.values():
        st = {(f["kind"]): f["status"] for f in s["frames"] if f["status"] == "approved"}
        pending = any(f["status"] == "pending" for f in s["frames"])
        if (tab == "pending" and pending) or (tab == "approved" and len(st) == 2) or (tab == "decision" and s["state"] == "needs_decision"):
            rows.append(s)
    return rows


CLASS_TEXT = {"r15_18": "R15–R18", "r19p": "R19+"}
FRAME_LABEL = {"approved": "одобрен", "pending": "на проверке", "running": "в работе", "queued": "в очереди", "none": "нет"}


def season_text(row) -> str:
    """Russian season line of a set row (season + studded)."""
    season = row.get("season")
    if season == "winter":
        return "Зима · шипы" if row.get("studded") else "Зима · без шипов"
    return {"summer": "Лето", "all": "Всесезонные"}.get(season, "Не указан")


def queue_rows(conn, with_feed: bool = False) -> list:
    """Needed sets in rank order with the state and the attempts used of each frame kind.
    `with_feed`: also the sets sent to the feed that are not needed now (sold out, retired): they still go out when back in stock."""
    rows = []
    spellings = {}
    for alias, key in photo_library.aliases(conn).items():
        spellings.setdefault(key, []).append(alias.split("|")[1])
    where = "state = ? OR in_feed = 1" if with_feed else "state = ?"
    for s in _dicts(conn, "SELECT rowid AS set_id, * FROM photo_set WHERE %s ORDER BY rank, key" % where, (photo_library.STATE_NEEDED,)):
        frames = {}
        for kind in photo_library.FRAME_KINDS:
            def has(table, status):
                return conn.execute("SELECT 1 FROM %s WHERE set_key=? AND kind=? AND status=?" % table, (s["key"], kind, status)).fetchone() is not None
            state = next((n for n, table in (("approved", "set_frame"), ("pending", "set_frame"), ("running", "image_job"), ("queued", "image_job")) if has(table, n)), "none")
            frames[kind] = {"state": state, "label": FRAME_LABEL[state], "tries": photo_library.attempts_used(conn, s["key"], kind)}
        states = {f["state"] for f in frames.values()}
        group = "done" if states == {"approved"} else "active" if states & {"queued", "running", "pending"} else "waiting"
        rows.append(dict(s, size_text=CLASS_TEXT.get(s["size_class"], s["size_class"]), frames=frames, group=group,
                         gone=s["state"] != photo_library.STATE_NEEDED,
                         spellings=sorted([s["model_key"]] + spellings.get(s["key"], [])) if s["key"] in spellings else []))
    return rows


def model_groups(conn, show: str) -> list:
    """Models in stock by brand for «По моделям»: show = done (both frames approved) | todo (the rest) | all.
    [{brand, done, total, stock, rows}], brands by pieces in stock, models by pieces in stock."""
    rows = queue_rows(conn, with_feed=True)
    brands: Dict[str, dict] = {}
    for r in rows:
        b = brands.setdefault(r["brand_name"] or r["brand_key"] or "—", {"brand": r["brand_name"] or r["brand_key"] or "—", "done": 0, "total": 0, "stock": 0, "rows": []})
        if r["gone"]:                                           # sent to the feed, not in stock now: shown so it can be taken out
            if show != "todo":
                b["rows"].append(r)
            continue
        b["total"] += 1
        b["stock"] += r.get("stock") or 0
        b["done"] += r["group"] == "done"
        if show == "all" or (show == "done") == (r["group"] == "done"):
            b["rows"].append(r)
    out = [b for b in brands.values() if b["rows"]]
    for b in out:
        b["rows"].sort(key=lambda r: (r["gone"], -(r.get("stock") or 0), r["model_name"]))
    return sorted(out, key=lambda b: (-b["stock"], b["brand"]))


def queue_summary(conn, now: str) -> dict:
    spent = photo_library.spent_today(conn, now)
    stopped = photo_library.queue_stopped(conn)
    limit = spent >= photo_library.DAILY_CAP_USD - 1e-9
    count = lambda status: conn.execute("SELECT COUNT(*) FROM image_job WHERE status=?", (status,)).fetchone()[0]
    review = conn.execute("SELECT COUNT(*) FROM set_frame WHERE status='pending'").fetchone()[0]
    return {"spent": spent, "cap": photo_library.DAILY_CAP_USD, "queued": count("queued"), "running": count("running"), "stopped": stopped,
            "review": review, "spent_pct": min(100, round(100 * spent / photo_library.DAILY_CAP_USD)),
            "state": "Очередь остановлена владельцем" if stopped else "Лимит на сегодня исчерпан" if limit else "Очередь работает",
            "max_attempts": photo_library.MAX_ATTEMPTS}


def register(app) -> None:
    app.add_template_filter(season_text, "season_text")
    app.add_template_filter(lambda cls: CLASS_TEXT.get(cls, cls), "class_text")

    @app.route("/photos/<name>")
    def stored_photo(name):
        if not NAME.match(name):
            abort(404)
        path = Path(app.config["PHOTOS_ROOT"]) / name
        if not path.is_file():
            abort(404)
        return send_file(str(path), mimetype=KINDS[name.rsplit(".", 1)[1]], max_age=31536000)

    @app.route("/photos/ref/<int:set_id>")                  # by rowid: set keys hold "|" and model names
    @login_required
    def photos_ref(set_id):
        row = get_conn().execute("SELECT ref_url FROM photo_set WHERE rowid=?", (set_id,)).fetchone()
        return ref_image(row[0] if row else None)

    @app.route("/photos/review")
    @login_required
    def photos_review():
        return _page(request.args.get("tab", "pending"))

    def _page(tab, new_token=None):
        conn = get_conn()
        if tab not in TABS:
            tab = "pending"
        base = (store.get_state(conn, "public_base") or request.url_root).rstrip("/")
        counts = {t: len(review_rows(conn, t, base)) for t in TABS if t not in ("queue", "models")}
        queue = tab == "queue"
        show = request.args.get("show", "todo") if tab == "models" else ""
        show = show if show in SHOW else "todo"
        sold_text = (store.get_state(conn, photo_library.PRIORITY_KEY) or "") if queue else ""
        sold, missing = photo_library.priority_matches(conn, sold_text) if sold_text.strip() else ({}, {})
        rows_q = [dict(r, sold=sold.get(r["key"], 0)) for r in queue_rows(conn)] if queue else []
        return render_template("photos_review.html", tab=tab, rows=[] if queue else review_rows(conn, tab, base), counts=counts,
                               queue=queue_summary(conn, now_iso()) if queue else None, queue_rows=rows_q,
                               agent=photo_agent.status_view(conn, now_iso()) if tab in ("queue", "models") else None,
                               sold_text=sold_text, sold_missing=sorted(missing.items(), key=lambda x: (-x[1], x[0])), sold_count=len(sold),
                               models=model_groups(conn, show) if tab == "models" else [], show=show,
                               on=store.get_state(conn, SWITCH_KEY) == "1", new_token=new_token)

    def _back(tab):
        return redirect("/photos/review?tab=%s" % (tab if tab in TABS else "pending"))

    @app.route("/photos/frame/<int:frame_id>/<action>", methods=["POST"])
    @login_required
    def photos_frame_action(frame_id, action):
        if action not in ("approve", "reject", "regenerate", "withdraw"):
            abort(404)
        conn = get_conn()
        tab = request.form.get("tab", "pending")
        found = _dicts(conn, "SELECT * FROM set_frame WHERE id=?", (frame_id,))
        if not found:
            abort(404)
        frame = found[0]
        need = "approved" if action == "withdraw" else "pending"
        if frame["status"] != need:
            flash("Кадр уже обработан: обновите страницу.", "error")
            return _back(tab)
        now = now_iso()
        stale = False
        with conn:
            if action == "approve":
                conn.execute("UPDATE set_frame SET status='withdrawn', decided=? WHERE set_key=? AND kind=? AND status='approved'", (now, frame["set_key"], frame["kind"]))
            target = {"approve": "approved", "withdraw": "withdrawn"}.get(action, "rejected")
            if conn.execute("UPDATE set_frame SET status=?, decided=? WHERE id=? AND status=?", (target, now, frame_id, need)).rowcount == 0:
                stale = True
                conn.rollback()
        if stale:
            flash("Кадр уже обработан: обновите страницу.", "error")
            return _back(tab)
        if action in ("approve", "regenerate"):                   # set4 or a new try goes in at once, not next cycle
            photo_library.enqueue(conn, now)
        if action == "approve":
            photo_agent.after_approve(conn, frame["set_key"], now)
        elif action == "regenerate":
            photo_agent.after_regenerate(conn, frame["set_key"], now)
        conn.commit()
        flash({"approve": "Кадр одобрен.", "reject": "Кадр отклонён.", "regenerate": "Кадр отклонён, новый поставлен в очередь.", "withdraw": "Кадр снят."}[action])
        return _back(tab)

    @app.route("/photos/switch", methods=["POST"])
    @login_required
    def photos_switch():
        conn = get_conn()
        store.set_state(conn, SWITCH_KEY, "1" if request.form.get("on") == "1" else "0")
        conn.commit()
        flash("Свои фото в фиде включены." if request.form.get("on") == "1" else "Свои фото в фиде выключены.")
        return _back(request.form.get("tab", "pending"))

    @app.route("/photos/queue-switch", methods=["POST"])
    @login_required
    def photos_queue_switch():
        conn = get_conn()
        run = request.form.get("on") == "1"
        store.set_state(conn, photo_library.QUEUE_STOP_KEY, "0" if run else "1")
        conn.commit()
        flash("Очередь запущена." if run else "Очередь остановлена.")
        return _back("queue")

    @app.route("/photos/feed", methods=["POST"])
    @login_required
    def photos_feed():
        """Send models' own photos to the feed or take them out: one (`one` = "<set id>:1|0") or the ticked ones (`set` + `on`)."""
        conn = get_conn()
        one = request.form.get("one", "")
        if one:
            ids, _, on = one.partition(":")
            ids = [ids]
        else:
            ids, on = request.form.getlist("set"), request.form.get("on", "")
        if on not in ("0", "1") or not all(re.fullmatch(r"[0-9]{1,18}", i) for i in ids):
            abort(400)
        changed = photo_library.set_in_feed(conn, ids, on == "1")
        conn.commit()
        if not ids:
            flash("Ничего не выбрано.", "error")
        else:
            flash(("Отправлено в фид: %d." if on == "1" else "Убрано из фида: %d.") % changed)
        return redirect("/photos/review?tab=models&show=%s" % (request.form.get("show") if request.form.get("show") in SHOW else "done"))

    @app.route("/photos/generate", methods=["POST"])
    @login_required
    def photos_generate():
        """«Сгенерировать очередь» (no `set`) or «Сгенерировать» on one model (`set` = its id): the agent picks the request up within a minute."""
        conn = get_conn()
        now = now_iso()
        one = request.form.get("set", "")
        if one:
            if not re.fullmatch(r"[0-9]{1,18}", one):
                abort(400)
            row = conn.execute("SELECT key, brand_name, model_name FROM photo_set WHERE rowid=?", (int(one),)).fetchone()
            if row is None:
                abort(404)
            ok, why = photo_agent.rush_model(conn, row[0], now)
            if ok:
                flash("%s %s: генерация запрошена, начнётся в течение минуты." % (row[1] or "", row[2] or ""))
            else:
                flash(why, "error")
            show = request.form.get("show")
            return redirect("/photos/review?tab=models&show=%s" % (show if show in SHOW else "todo"))
        if photo_library.queue_stopped(conn):
            flash("Очередь остановлена: сначала запустите её.", "error")
            return _back("queue")
        if photo_library.spent_today(conn, now) >= photo_library.DAILY_CAP_USD - 1e-9:
            flash("Лимит расходов на сегодня исчерпан.", "error")
            return _back("queue")
        photo_library.enqueue(conn, now)
        if conn.execute("SELECT COUNT(*) FROM image_job WHERE status IN ('queued','running')").fetchone()[0] == 0:
            photo_agent.request_copies(conn, now)                 # still worth it: cards missing (e.g. no font before) are made
            flash("В очереди нет заданий: генерировать нечего. Копии и карточки обновятся в течение минуты.", "error")
            return _back("queue")
        photo_agent.request_run(conn, now, "all")
        flash("Генерация очереди запрошена, начнётся в течение минуты.")
        return _back("queue")

    @app.route("/photos/priority", methods=["POST"])
    @login_required
    def photos_priority():
        conn = get_conn()
        text = request.form.get("sold", "")[:200000]
        store.set_state(conn, photo_library.PRIORITY_KEY, text)
        photo_library.apply_priority(conn)
        photo_library.enqueue(conn, now_iso())
        conn.commit()
        sold, missing = photo_library.priority_matches(conn, text)
        flash("Список продаж сохранён: моделей вперёд — %d, строк без пары — %d." % (len(sold), sum(missing.values())))
        return _back("queue")

    @app.route("/connections/openai")
    @login_required
    def connections_openai_page():
        """«Подключения → OpenAI (фото)»: the key and the proxy the agent on the server uses; the values are never shown."""
        conn = get_conn()
        box = app.config.get("SECRET_BOX")
        return render_template("conn_openai.html", tabs={"active": "openai"}, box_ok=box is not None,
                               secrets=connections.secret_status(conn, box, photo_agent.SECRET_KIND, 0))

    @app.route("/connections/openai", methods=["POST"])
    @login_required
    def connections_openai():
        conn = get_conn()
        box = app.config.get("SECRET_BOX")
        key, proxy = request.form.get("api_key", "").strip(), request.form.get("proxy", "").strip()
        try:
            if proxy:
                proxy = photo_agent.check_proxy(proxy)
            if key and not re.fullmatch(r"[A-Za-z0-9_\-]{20,300}", key):
                raise ValueError("Ключ OpenAI: строка вида sk-…, без пробелов.")
            for name, value in (("api_key", key), ("proxy", proxy)):
                if value:
                    connections.set_secret(conn, box, photo_agent.SECRET_KIND, 0, name, value, now_iso(), author())
            conn.commit()
        except SecretKeyError:
            flash("Мастер-ключ недоступен: секреты не сохраняются и не читаются.", "error")
            return redirect("/connections/openai")
        except ValueError as err:
            conn.rollback()
            flash(str(err), "error")
            return redirect("/connections/openai")
        flash("Сохранено." if key or proxy else "Ничего не изменено: поля пустые.")
        return redirect("/connections/openai")

    @app.route("/connections/openai/clear", methods=["POST"])
    @login_required
    def connections_openai_clear():
        conn = get_conn()
        what = request.form.get("what", "")
        if what not in ("api_key", "proxy"):
            abort(400)
        conn.execute("DELETE FROM secret WHERE kind=? AND owner_id=0 AND name=?", (photo_agent.SECRET_KIND, what))
        store.write_audit(conn, now_iso(), author(), "secret_cleared", {"kind": photo_agent.SECRET_KIND, "owner_id": 0, "name": what})
        conn.commit()
        flash("Ключ OpenAI удалён." if what == "api_key" else "Прокси удалён: запросы к OpenAI пойдут напрямую.")
        return redirect("/connections/openai")

    @app.route("/photos/agent-token", methods=["POST"])
    @login_required
    def photos_agent_token():
        conn = get_conn()
        token = issue_agent_token(conn)
        conn.commit()
        resp = make_response(_page("pending", new_token=token))
        resp.headers["Cache-Control"] = "no-store"
        return resp
