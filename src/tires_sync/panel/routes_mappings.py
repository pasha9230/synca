from __future__ import annotations

import csv
import difflib
import hashlib
import io
from urllib.parse import urlencode

from flask import Response, flash, redirect, render_template, request

from ..catalog import loose_form
from ..mappings import CONFIDENT, MappingError, canonical_model, confirm_brand_mapping, confirm_mapping, save_old_model_name
from ..settings_store import get_active
from .common import author, get_conn, login_required, now_iso, safe_path
from .data import load_reference
from .routes_drafts import publish_now, publish_summary
from .routes_main import context as _context
from .. import store, tochki_feed
from . import service


FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value) -> str:
    """Stop spreadsheets from running a cell as a formula."""
    text = str(value)
    return "'" + text if text.startswith(FORMULA_START) else text


def csv_unsafe(value: str) -> str:
    return value[1:] if value.startswith("'") and value[1:].startswith(FORMULA_START) else value


def suggestion_kind(m) -> str:
    """How sure the proposal for an unmatched model is: conf (apply after a look), check (several candidates), none."""
    if m["avito_model"]:
        return "done"
    if m["tier"] in CONFIDENT and m["proposal"]:
        return "conf"
    return "check" if m["options"] else "none"


def publish_flash(app, changed) -> None:
    """A mapping that was saved changes the ads: publish the feed at once and say what happened."""
    if changed:
        lines = publish_now(app)
        if lines is not None:
            flash(publish_summary(lines))


def context(app):
    """The positions of the mappings page: those of the feed and, before its switch, those of 4tochki (their models wait for a pair too)."""
    conn, settings, parsed, ref = _context(app)
    return (conn, settings, tochki_feed.catalog_view(conn, parsed, app.config["UNLOAD_PATH"])[0], ref)


def register(app) -> None:
    @app.route("/mappings")
    @login_required
    def mappings():
        conn, settings, parsed, ref = context(app)
        overview = service.brands_overview(conn, get_active(conn)[1], parsed, ref)
        query = request.args.get("q", "").strip().casefold()
        only_brand = request.args.get("b", "")
        show_all = request.args.get("a") == "all"
        kind = request.args.get("c") if request.args.get("c") in ("conf", "check", "none") else ""
        groups, unmatched_total, model_total = [], 0, 0
        counts = {"conf": 0, "check": 0, "none": 0}
        shown_models = shown_goods = 0
        for row in overview:
            if row["avito_brand"] is None:
                continue
            models = row["models"]
            unmatched_total += sum(1 for m in models if not m["avito_model"])
            model_total += len(models)
            label = (row["brand_key"] + " " + row["avito_brand"]).casefold()
            if only_brand and row["brand_key"] != only_brand:
                continue
            visible = [m for m in models if (show_all or not m["avito_model"])
                       and (not query or query in m["name"].casefold() or query in label)]
            for m in visible:
                m["kind"] = suggestion_kind(m)
                m["hint"] = m["proposal"] or (m["options"][0] if m["options"] else "")
                m["pct"] = round(100 * difflib.SequenceMatcher(None, loose_form(m["name"]), loose_form(m["hint"])).ratio()) if m["hint"] else 0
                if m["kind"] in counts:
                    counts[m["kind"]] += 1
            shown = [m for m in visible if not kind or m["kind"] == kind]
            if not shown:
                continue
            shown.sort(key=lambda m: (bool(m["avito_model"]), -m["goods"], m["name"]))
            shown_models += len(shown)
            shown_goods += sum(m["goods"] for m in shown)
            groups.append({"brand": row["brand_key"], "avito_brand": row["avito_brand"], "models": shown,
                           "avito_models": sorted(ref.get(row["avito_brand"], ())),
                           "confident": sum(1 for m in shown if m["kind"] == "conf"),
                           "dom_id": hashlib.md5(row["brand_key"].encode("utf-8")).hexdigest()[:8]})
        brands = [b for b in overview if b["avito_brand"] is None]
        try:
            mirror_rows = service.mirror_models_to_check(conn, ref, parsed)
        except Exception as err:                                       # an extra block must not take the whole page down
            mirror_rows = []
            flash("Не удалось сверить модели зеркальных фидов (%s). Остальная страница работает." % type(err).__name__)
        mirror_lists = {r["list_id"]: sorted(ref.get(r["brand"], ())) for r in mirror_rows}
        base = {k: v for k, v in request.args.items() if k != "c"}
        chips = [("", "Все", None), ("conf", "Уверенные", counts["conf"]), ("check", "Проверить", counts["check"]),
                 ("none", "Без подсказки", counts["none"])]
        chips = [{"label": label + (" (%d)" % n if n is not None else ""), "on": key == kind,
                  "url": "/mappings" + ("?" + urlencode(dict(base, **({"c": key} if key else {}))) if (base or key) else "")}
                 for key, label, n in chips]
        return render_template(
            "mappings.html", groups=groups, total=unmatched_total, model_total=model_total,
            confident=sum(g["confident"] for g in groups), brands=brands, mirror_rows=mirror_rows, mirror_lists=mirror_lists, filtered=bool(query or only_brand or kind),
            brand_options=[(r["brand_key"], r["avito_brand"]) for r in overview if r["avito_brand"] and r["models"]],
            q=request.args.get("q", ""), only_brand=only_brand, show_all=show_all, kind=kind, chips=chips,
            old_names=[{"brand": b, "model": m, "name": n} for (b, m), n in sorted(store.load_old_model_names(conn).items())],
            old_brands=sorted({r["avito_brand"] for r in overview if r["avito_brand"]}),
            old_models=sorted({m["avito_model"] for r in overview for m in r["models"] if m["avito_model"]}),     # the models we sell
            shown_models=shown_models, shown_goods=shown_goods, avito_brand_names=sorted(ref),
            next_url=request.full_path.rstrip("?"))

    @app.route("/mappings/accept-confident", methods=["POST"])
    @login_required
    def accept_confident():
        """Accept the confident proposals the page shows (every brand, switched on or not), for one Avito brand or all."""
        conn, settings, parsed, ref = context(app)
        brand = request.form.get("brand", "")
        only_brand = request.form.get("b", "")
        query = request.form.get("q", "").strip().casefold()
        done = 0
        for row in service.brands_overview(conn, get_active(conn)[1], parsed, ref):
            if row["avito_brand"] is None or brand and row["avito_brand"] != brand or only_brand and row["brand_key"] != only_brand:
                continue
            label = (row["brand_key"] + " " + row["avito_brand"]).casefold()
            for m in row["models"]:
                if m["avito_model"] or m["tier"] not in CONFIDENT or not m["proposal"]:
                    continue
                if query and query not in m["name"].casefold() and query not in label:
                    continue
                confirm_mapping(conn, row["avito_brand"], m["name"], m["proposal"], ref, now_iso(), author())
                done += 1
        flash("Принято сопоставлений: %d." % done, "ok")
        publish_flash(app, done)
        return redirect(safe_path(request.form.get("next", "")) or "/mappings")

    @app.route("/mappings/export.csv")
    @login_required
    def export_mappings():
        conn, settings, parsed, ref = context(app)
        out = io.StringIO()
        writer = csv.writer(out, delimiter=";", lineterminator="\n")
        writer.writerow(["brand", "supplier_model", "goods", "avito_model", "tier"])
        for row in service.brands_overview(conn, get_active(conn)[1], parsed, ref):
            if row["avito_brand"] is None:
                continue
            for m in row["models"]:
                if not m["avito_model"]:
                    writer.writerow([csv_safe(row["avito_brand"]), csv_safe(m["name"]), m["goods"],
                                     csv_safe(m["proposal"] or ""), m["tier"]])
        return Response(
            "\ufeff" + out.getvalue(), mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=mappings.csv"})

    @app.route("/mappings/import", methods=["POST"])
    @login_required
    def import_mappings():
        upload = request.files.get("file")
        if upload is None or not upload.filename:
            flash("Ошибка импорта: выберите файл CSV.")
            return redirect("/mappings")
        raw = upload.read(1_000_001)
        if len(raw) > 1_000_000:
            flash("Ошибка импорта: файл слишком большой (не более 1 МБ).")
            return redirect("/mappings")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            flash("Ошибка импорта: сохраните файл как CSV UTF-8.")
            return redirect("/mappings")
        lines = text.splitlines()
        delimiter = ";" if lines and ";" in lines[0] else ","
        reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
        if not reader.fieldnames or not {"brand", "supplier_model", "avito_model"} <= set(reader.fieldnames):
            flash("Ошибка импорта: нет столбцов brand, supplier_model, avito_model.")
            return redirect("/mappings")
        ref = load_reference(app.config["REFERENCE_PATH"])
        conn = get_conn()
        done = empty = 0
        errors = []
        for row in reader:
            target = (row.get("avito_model") or "").strip()
            if not target:
                empty += 1
                continue
            try:
                confirm_mapping(conn, csv_unsafe((row.get("brand") or "").strip()),
                                csv_unsafe((row.get("supplier_model") or "").strip()), csv_unsafe(target), ref, now_iso(), author())
                done += 1
            except MappingError as err:
                errors.append(str(err))
        message = "Принято: %d, пропущено пустых: %d, ошибок: %d." % (done, empty, len(errors))
        if errors:
            message += " Первые ошибки: " + "; ".join(errors[:5])
        flash(message)
        publish_flash(app, done)
        return redirect("/mappings")

    @app.route("/mappings/model", methods=["POST"])
    @login_required
    def confirm_model():
        ref = load_reference(app.config["REFERENCE_PATH"])
        brand = request.form.get("brand", "")
        act = request.form.get("act", "") or "manual"
        typed = request.form.get("proposal", "") if act == "accept" else request.form.get("avito_model", "")
        target = canonical_model(ref, brand, typed)
        try:
            if target is None and act:
                raise MappingError("Модели «%s» нет у бренда «%s» в справочнике Авито." % (typed.strip(), brand))
            confirm_mapping(get_conn(), brand, request.form.get("model_raw", ""),
                            target or typed, ref, now_iso(), author())
            message = "Сопоставлено: %s → %s." % (request.form.get("model_raw", ""), target or typed)
            ok = True
        except MappingError as err:
            message, ok = str(err), False
        flash("Сопоставление сохранено." if ok else message)
        publish_flash(app, ok)
        return redirect(safe_path(request.form.get("next", "")) or "/mappings")

    @app.route("/mappings/old-name", methods=["POST"], endpoint="save_old_model_name")
    @login_required
    def save_old_model_name_route():
        """The former name of an Avito model for the variable {old_model}. Nothing is published now: the next cycle builds it."""
        ref = load_reference(app.config["REFERENCE_PATH"])
        try:
            flash(save_old_model_name(get_conn(), ref, request.form.get("brand", ""), request.form.get("model", ""),
                                      request.form.get("old_name", ""), now_iso(), author()), "ok")
        except MappingError as err:
            flash(str(err))
        return redirect("/mappings#old-names")

    @app.route("/mappings/brand", methods=["POST"])
    @login_required
    def confirm_brand():
        ref = load_reference(app.config["REFERENCE_PATH"])
        avito_brand = request.form.get("avito_brand_manual", "").strip() or request.form.get("avito_brand", "")
        avito_brand = next((name for name in ref if name.casefold() == avito_brand.casefold()), avito_brand)   # case does not matter
        try:
            confirm_brand_mapping(get_conn(), request.form.get("brand_key", ""), avito_brand, ref, now_iso(),
                                  author())
            flash("Соответствие бренда сохранено.")
            publish_flash(app, True)
        except MappingError as err:
            flash(str(err))
        return redirect("/mappings")
