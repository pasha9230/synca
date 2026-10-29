from __future__ import annotations

import json
import re
from datetime import timedelta

from flask import abort, flash, jsonify, redirect, render_template, request

from .. import avito_upload, connections, staging, store
from ..cycle import run_for_accounts
from ..publishing import feed_lock, publish_live_accounts, remember_expected
from ..settings_store import active_settings
from ..transport import urllib_transport
from .data import load_merged, load_reference
from ..settings_store import (
    PercentLimitError, StaleDraftError, cancel_draft, confirm_draft, get_active,
)
from .common import author, get_conn, login_required, now, now_iso, remember_page, return_page, review_or_back, safe_path
from .routes_main import context
from . import service


def _int_or_none(value):
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


PANEL_LOCK_WAIT = 60.0


def apply_publish(app, verify=None, upload=False) -> list:
    """Rebuild every account feed from the stored supplier unload and publish the live ones on our side.
    `verify` (optional) runs inside the feed lock before anything is built: a text means «stop, with this reason».
    `upload`: after our side is published, start the Avito autoload upload for the accounts that were published (outside the lock)."""
    feed_root = app.config.get("FEED_ROOT")
    if not feed_root:
        return ["Каталог фидов не задан (TIRES_FEED_ROOT): фид не опубликован."]
    conn = get_conn()
    with feed_lock(feed_root, wait=PANEL_LOCK_WAIT, strict=False) as acquired:        # shorter than the worker timeout, so the page answers
        if not acquired:
            return ["Фид сейчас обновляется другим процессом (цикл или другая публикация): повторите через минуту."]
        if verify is not None:
            problem = verify()
            if problem:
                return [problem]
        try:
            settings = active_settings(conn)
            parsed = load_merged(conn, settings, app.config["UNLOAD_PATH"])
            if 1 in parsed.unavailable or not parsed.offers:
                raise ValueError("выгрузка первого поставщика недоступна или пуста: публикация остановлена")
            ref = load_reference(app.config["REFERENCE_PATH"])
            built = run_for_accounts(conn, None, ref, now().date(), feed_root, settings=settings, parsed=parsed,
                                     input_sha="panel", now=now() + timedelta(hours=3))
        except (OSError, ValueError) as err:
            return ["Фид не пересобран: %s" % err]
        failed = {r.account_id for r in built if not r.ok}
        lines = [
            "%s: в фиде не осталось ни одного объявления, пустой фид не публикуется" % r.name if "feed is empty" in r.error
            else "%s: фид не собран (%s)" % (r.name, r.error)
            for r in built if not r.ok]
        published = []
        for res in publish_live_accounts(conn, feed_root, skip_ids=failed):
            if res["ok"]:
                published.append(res)
            if res.get("skipped") and not res["ok"] and res["message"].startswith("фид не собран"):
                continue
            lines.append("Опубликовано: %s, %s" % (res["name"], res["message"]) if res["ok"]
                         else "Не публикуется: %s, %s" % (res["name"], res["message"]) if res.get("skipped")
                         else "%s: %s" % (res["name"], res["message"]))
    if upload:
        lines.extend(_start_avito_uploads(app, conn, published))
    return lines


def _start_avito_uploads(app, conn, published) -> list:
    if not published:
        return ["Загрузка на Авито не запущена: ни один фид не опубликован."]
    box = app.config.get("SECRET_BOX")
    if box is None:
        return ["Загрузка на Авито не запущена: мастер-ключ недоступен."]
    http = app.config.get("AVITO_HTTP") or urllib_transport
    lines = []
    for res in published:
        started, text = avito_upload.start_upload(conn, box, res["id"], http)
        lines.append("Опубликовано на Авито: %s, %s" % (res["name"], text) if started
                     else "Авито, %s: загрузка не запущена, %s" % (res["name"], text))
        store.write_audit(conn, now_iso(), author(), "avito_upload_started" if started else "avito_upload_refused",
                          {"account": res["id"], "text": text[:200]})
    return lines


def publish_now(app):
    """After a confirmed change: rebuild the feeds and publish the live accounts at once. None when there is nothing to publish to."""
    if not app.config.get("FEED_ROOT"):
        return None
    conn = get_conn()
    if not connections.list_feed_accounts(conn):
        return None
    lines = apply_publish(app)
    store.set_state(conn, "last_publish", json.dumps({"at": now_iso(), "lines": lines}, ensure_ascii=False))
    return lines


def publish_summary(lines) -> str:
    """One line for the flash after an automatic publication."""
    done = [line.replace("Опубликовано: ", "", 1) for line in lines if line.startswith("Опубликовано")]
    other = [line for line in lines if not line.startswith("Опубликовано")]
    if done and not other:
        return "Фид обновлён для Авито: " + "; ".join(done) + "."
    if done:
        return "Фид обновлён для Авито: " + "; ".join(done) + ". Не обновлено: " + "; ".join(other[:3]) + "."
    return "Фид для Авито не обновлён: " + "; ".join(other[:3]) + "."


def _row_entries(form, prefix: str, brand: str) -> list:
    """(brand, season, small, large, publish) for the seasons of one row of the brands form (nothing for untouched ones)."""
    entries = []
    for season in service.SEASONS:
        small, large = form.get(prefix + season + "_small", "").strip(), form.get(prefix + season + "_large", "").strip()
        switch = (form.get(prefix + season + "_on") == "on") if prefix + season + "_form" in form else None
        if not small and not large and switch is None:
            continue
        if bool(small) != bool(large):
            raise ValueError("%s: заполните оба процента, до R17 и от R18." % re.sub(r"^S\d+:", "", brand))
        if small or large:
            small, large = service._float(small, "до R17"), service._float(large, "от R18")
        else:
            small = large = None
        entries.append((brand, season, small, large, switch))
    return entries


def register(app) -> None:
    app.jinja_env.globals["plural"] = service.plural
    # the pages whose bar «Не применено: N» applies the working draft in place (the owner 2026-10-29); the other pages keep «Посмотреть и применить»
    app.jinja_env.globals["SETTINGS_BAR_PAGES"] = ("settings_page", "settings_brands_page", "settings_brand_card", "settings_exclusions_page",
                                                   "economics_terms")

    @app.route("/brands")
    @login_required
    def brands():
        return redirect("/catalog" + ("?" + request.query_string.decode() if request.query_string else ""))

    def _suppliers_ex(conn):
        return [{"supplier": sp, "ex": service.exclusions(conn, sp["id"], app.config["UNLOAD_PATH"])}
                for sp in connections.list_suppliers(conn)]

    @app.route("/settings")
    @login_required
    def settings_page():
        conn = get_conn()
        suppliers_ex = _suppliers_ex(conn)
        point_names = {}
        for account in connections.list_feed_accounts(conn):
            for row in connections.list_addresses(conn, account["id"]):
                if row["name"]:
                    point_names.setdefault(str(row["address_id"]), row["name"])
        raw = staging.effective_raw(conn)                       # the pending changes shown as they will be after «Применить»
        pack4_rules = [{"rule": r, "text": service.pack4_rule_text(r)} for r in service.pack4_rules_of(raw)]
        multi = len(suppliers_ex) > 1
        pack4_brands = [{"value": ("S%d:" % sp["supplier"]["id"] if sp["supplier"]["id"] != 1 else "") + x,
                         "label": x + (" · %s" % sp["supplier"]["name"] if multi else "")}
                        for sp in suppliers_ex for x in sp["ex"]["brand_all"]]
        pack4_brands.sort(key=lambda b: b["label"])
        pack4_diameters = sorted({d for sp in suppliers_ex for d in sp["ex"]["diameter_choices"]})
        return render_template("settings.html", raw=raw, point_names=point_names,
                               pack4_rules=pack4_rules, pack4_brands=pack4_brands, pack4_diameters=pack4_diameters)

    @app.route("/settings/brands")
    @login_required
    def settings_brands_page():
        conn = get_conn()
        return render_template("settings_brands.html", raw=get_active(conn)[1], pricing_tables=[
            {"supplier": sp, "rows": service.pricing_table(conn, sp["id"], app.config["UNLOAD_PATH"], load_reference(app.config["REFERENCE_PATH"])),
             "stale": service.supplier_stale_text(conn, sp["id"], app.config["UNLOAD_PATH"]),
             "tochki_off": service.tochki_off_state(conn, sp["id"], app.config["UNLOAD_PATH"])}
            for sp in connections.list_suppliers(conn)])

    @app.route("/settings/brands/card")
    @login_required
    def settings_brand_card():
        conn = get_conn()
        try:
            supplier_id = int(request.args.get("supplier", "1"))
        except ValueError:
            abort(404)
        supplier = connections.get_supplier(conn, supplier_id)
        if supplier is None:
            abort(404)
        rows = service.pricing_table(conn, supplier_id, app.config["UNLOAD_PATH"], load_reference(app.config["REFERENCE_PATH"]))
        row = next((r for r in rows if r["brand"] == request.args.get("brand", "")), None)
        if row is None:
            abort(404)
        return render_template("settings_brand_card.html", raw=get_active(conn)[1], supplier=supplier, row=row,
                               card_url=request.full_path.rstrip("?"), stale=service.supplier_stale_text(conn, supplier_id, app.config["UNLOAD_PATH"]))

    @app.route("/settings/exclusions")
    @login_required
    def settings_exclusions_page():
        conn = get_conn()
        return render_template("settings_exclusions.html", raw=get_active(conn)[1], suppliers_ex=_suppliers_ex(conn))

    @app.route("/settings/draft", methods=["POST"])
    @login_required
    def settings_draft():
        """The form «Общие» into the working draft with every other pending change (the owner 2026-10-29: changes pile up, «Применить» once)."""
        conn = get_conn()
        try:
            changes = service.changes_from_settings_form(staging.effective_raw(conn), request.form)
            if changes:
                staging.set_fields(conn, changes, now_iso(), expect=request.form.get("draft_rev", ""))
        except staging.DraftChangedError as err:
            flash(str(err))
            return redirect("/settings")
        except (ValueError, StaleDraftError) as err:
            flash("Ошибка в форме: %s" % err)
            return redirect("/settings")
        if not changes:
            flash("Изменений нет.")
            return redirect("/settings")
        return review_or_back(conn, "/settings")

    @app.route("/settings/pricing", methods=["POST"])
    @login_required
    def settings_pricing():
        conn = get_conn()
        form = request.form
        try:
            supplier_id = int(form.get("supplier", ""))
        except ValueError:
            abort(404)
        if connections.get_supplier(conn, supplier_id) is None:
            abort(404)
        back = "/settings/brands#pricing-%d" % supplier_id
        asked = safe_path(form.get("next", ""))
        if asked.startswith("/settings/brands/card?"):
            back = asked
        table = {row["brand"]: row for row in service.pricing_table(conn, supplier_id, app.config["UNLOAD_PATH"])}
        entries, skipped = [], 0
        try:
            if form.get("scope") == "brand":
                row = table.get(form.get("brand", ""))
                if row is None:
                    raise ValueError("У этого поставщика нет бренда «%s»." % form.get("brand", ""))
                entries = _row_entries(form, "", row["brand"])
                if not entries:
                    raise ValueError("Укажите проценты хотя бы для одного сезона.")
            elif form.get("scope") == "table":
                only = form.get("only", "")
                index = 0
                while "r%d_brand" % index in form:
                    if not only or only == str(index):
                        brand = form["r%d_brand" % index]
                        if brand not in table:
                            raise ValueError("У этого поставщика нет бренда «%s»." % brand)
                        entries += _row_entries(form, "r%d_" % index, brand)
                    index += 1
                if not entries:
                    raise ValueError("Нечего применять: отметьте сезоны или заполните проценты.")
            elif form.get("scope") == "brand_all":
                if form.get("brand", "") not in table:
                    raise ValueError("У этого поставщика нет бренда «%s»." % form.get("brand", ""))
                switch_on = form.get("enabled") == "on"
                staging.set_brand_enabled(conn, form["brand"], switch_on, now_iso())
                if not switch_on:
                    extra = service.warn_forced(conn, form["brand"], app.config["UNLOAD_PATH"])
                    if extra:
                        flash(extra)
                return review_or_back(conn, back)
            elif form.get("scope") == "bulk":
                wanted = form.get("season", "*")
                if wanted != "*" and wanted not in service.SEASONS:
                    raise ValueError("Неизвестный сезон.")
                publish = {"on": True, "off": False}.get(form.get("publish", ""))
                small_raw, large_raw = form.get("small", "").strip(), form.get("large", "").strip()
                if bool(small_raw) != bool(large_raw) or (not small_raw and publish is None):
                    raise ValueError("Заполните оба процента: до R17 и от R18.")
                if small_raw:
                    small, large = service._float(small_raw, "до R17"), service._float(large_raw, "от R18")
                else:
                    small = large = None
                for row in table.values():
                    for season in service.SEASONS:
                        sc = row["seasons"][season]
                        if wanted not in ("*", season) or not (sc["count"] or (publish is False and sc["enabled"])):
                            continue
                        if publish and small is None and sc["small"] == "":
                            skipped += 1                       # nothing to publish with: no percents yet
                            continue
                        entries.append((row["brand"], season, small, large, publish))
                if not entries:
                    raise ValueError("У этого поставщика нет подходящих позиций: проценты у них не заданы." if skipped
                                     else "У этого поставщика нет позиций выбранного сезона.")
            else:
                raise ValueError("Неизвестное действие.")
            staging.set_brand_state(conn, entries, now_iso())
            if skipped:
                flash("Пропущено сочетаний без процентов: %d." % skipped)
        except (ValueError, StaleDraftError) as err:
            flash(str(err))
            return redirect(back)
        return review_or_back(conn, back)

    @app.route("/draft/<int:draft_id>")
    @login_required
    def draft(draft_id):
        conn, settings, parsed, ref = context(app)
        try:
            review = service.draft_review(conn, draft_id, parsed, ref, now().date(), now() + timedelta(hours=3))
        except ValueError:
            abort(404)
        back_url = return_page("/catalog", keep=True)
        labels = (("/settings/brands", "Бренды и наценки"), ("/settings/exclusions", "Исключения"), ("/settings", "Настройки"),
                  ("/mappings", "Сопоставления"))
        back_label = next((label for prefix, label in labels if back_url.startswith(prefix)), "Каталог")
        return render_template("draft.html", r=review, back_url=back_url, back_label=back_label)

    @app.route("/draft/<int:draft_id>/confirm", methods=["POST"])
    @login_required
    def draft_confirm(draft_id):
        """«Применить» of the page /draft/N, or of the bar of a settings page (`next`: the page, where it comes back after the publication;
        every check stays, a missing confirmation sends to /draft/N as before, and from there back to the page)."""
        strong = request.form.get("strong") == "on"
        stay = safe_path(request.form.get("next", ""))
        conn, settings, parsed, ref = context(app)
        try:
            review = service.draft_review(conn, draft_id, parsed, ref, now().date(), now() + timedelta(hours=3))
        except ValueError:
            abort(404)

        def again(text):
            flash(text)
            if stay:
                remember_page(stay)
            return redirect("/draft/%d" % draft_id)
        if request.form.get("rev") != review["rev"]:
            return again("Черновик изменился после просмотра: проверьте итог ещё раз и подтвердите заново.")
        if review["guard"]["over"] and request.form.get("understand") != "on":
            return again("Подтвердите снятие: исчезнет %d объявлений, это больше порога защиты (%d). Отметьте «Я понимаю»." % (
                review["guard"]["gone"], review["guard"]["limit"]))
        if review["own_risky"] and not strong:                 # a fixed own price far from the feed's or below the purchase: asked once more
            return again("Своя цена ниже закупки или сильно отличается от цены фида: отметьте «Подтверждаю значения вне диапазона».")
        try:
            confirm_draft(get_conn(), draft_id, now_iso(), author(), strong=strong)
        except StaleDraftError:
            return again("Черновик устарел: настройки уже изменены. Создайте новый.")
        except PercentLimitError as err:
            return again("Процент %s вне допустимого диапазона: нужно усиленное подтверждение." % err.max_pct)
        except ValueError as err:
            flash(str(err))
            return redirect(stay or return_page())
        if review["guard"]["gone"]:
            remember_expected(get_conn(), active_settings(get_conn()).version, review["guard"]["gone_ads"])
        lines = publish_now(app)                               # a confirmed change always goes to the feed Avito reads
        if stay:
            return_page()                                      # forget the page the draft was opened from: we are back on it
            flash("Настройки применены. " + (publish_summary(lines) if lines is not None else ""), "ok")
            return redirect(stay)
        flash("Настройки применены.")
        if lines is not None:
            return redirect("/publish/result")
        return redirect(return_page())

    @app.route("/settings/pending")
    @login_required
    def settings_pending():
        """What «Применить» of the bar of a settings page changes in the feed, for the bar (asked by its script: the count takes a while)."""
        conn = get_conn()
        staged = staging.summary(conn)
        if staged["draft_id"] is None or staged["stale"]:
            return jsonify(open=False, count=0)
        _, settings, parsed, ref = context(app)
        review = service.draft_review(conn, staged["draft_id"], parsed, ref, now().date(), now() + timedelta(hours=3))
        return jsonify(open=True, draft=staged["draft_id"], rev=review["rev"], line=service.pending_line(review),
                       count=staging.count_of(get_active(conn)[1], review["draft"]["changes"]),
                       guard={"over": review["guard"]["over"], "gone": review["guard"]["gone"], "limit": review["guard"]["limit"]},
                       strong=bool(review["needs_strong"]))

    @app.route("/publish/result")
    @login_required
    def publish_result():
        return render_template("publish_result.html", last=service.last_publish(get_conn()), back=return_page("/catalog", keep=True))

    @app.route("/draft/<int:draft_id>/cancel", methods=["POST"])
    @login_required
    def draft_cancel(draft_id):
        try:
            cancel_draft(get_conn(), draft_id, now_iso(), author())
            flash("Черновик отменён.")
        except ValueError as err:
            flash(str(err))
        return redirect(return_page())
