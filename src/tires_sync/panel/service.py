from __future__ import annotations

import hashlib
import json
import math
import re

import dataclasses
from .. import connections, store
from ..candidates import Evaluation, evaluate
from ..models import Season
from ..overrides import override_key
from ..suppliers import supplier_of_id, unload_path_for
from ..mappings import pending_mappings


def evaluation(conn, settings, parsed, ref) -> Evaluation:
    return evaluate(parsed.offers, parsed.issues, settings, ref, store.load_confirmed_mappings(conn),
                    store.load_brand_map(conn))


def _when(value) -> str:
    """A UTC stamp (a download, a check of a connection) in Moscow time, as the filter dt_utc shows it: 2026-10-07T15:46:50 -> 07.10.2026 18:46:50."""
    from datetime import datetime, timedelta, timezone
    text = str(value)
    try:
        at = datetime.fromisoformat(text[:25])
    except ValueError:
        return text
    if at.tzinfo is not None:
        at = at.astimezone(timezone.utc).replace(tzinfo=None)
    return (at + timedelta(hours=3)).strftime("%d.%m.%Y %H:%M:%S")


def _step(level: str, title: str, detail: str, href: str) -> dict:
    return {"level": level, "title": title, "detail": detail, "href": href}


def _read_meta(unload_path) -> dict:
    try:
        with open(str(unload_path) + ".meta.json", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _tochki_step(unload_path, supplier, settings, now):
    """4tochki with its switch «в фид» on: its own last load, as the cycle takes it (`suppliers._add_tochki`), not a WebMIM file."""
    from datetime import datetime, timezone
    from .. import tochki_feed
    from ..suppliers import file_is_stale
    title, href = "Поставщик «%s»" % supplier["name"], "/connections/tochki"
    loaded = tochki_feed.load_snapshot(unload_path, supplier["id"])
    if loaded is None or not loaded[0].offers:
        return _step("bad", title, "нет загруженных позиций 4 точек: проверьте загрузку на странице подключения", href)
    count = len(loaded[0].offers)
    shown = "%d %s" % (count, plural(count, "позиция", "позиции", "позиций"))
    try:
        fetched = datetime.fromisoformat(loaded[1]["fetched_at"])
    except (TypeError, ValueError):
        fetched = None
    if fetched is not None and fetched.tzinfo is not None:                     # the load writes UTC without a zone; one with a zone is compared in UTC
        fetched = fetched.astimezone(timezone.utc).replace(tzinfo=None)
    if fetched is None or file_is_stale(fetched, now or datetime.utcnow(), settings.stale_days):
        return _step("warn", title, "%s, загрузка 4 точек устарела (%s): цикл её не берёт, объявления 4 точек держатся как были" % (
            shown, _when(loaded[1]["fetched_at"]) if fetched else "дата неизвестна"), href)
    return _step("ok", title, "%s, загружена %s" % (shown, _when(loaded[1]["fetched_at"])), href)


def readiness(conn, unload_path, parsed, ref, settings, now=None) -> list:
    from .. import tochki_feed
    from .data import stale_unload
    steps = []
    check = connections.get_check(conn, "webmim", 0)
    if check is None:
        steps.append(_step("warn", "Подключение к WebMIM", "ещё не проверялось", "/connections"))
    elif check["status"].startswith("ok"):
        steps.append(_step("ok", "Подключение к WebMIM", "%s (%s)" % (check["status"][4:], _when(check["at"])), "/connections"))
    else:
        steps.append(_step("bad", "Подключение к WebMIM", check["status"][7:] or check["status"], "/connections"))
    if not parsed.offers:
        steps.append(_step("bad", "Выгрузка поставщика", "Нет выгрузки поставщика: проверьте подключение и скачайте её", "/connections"))
    else:
        meta = _read_meta(unload_path)
        count = len(parsed.offers)
        shown = "%d %s" % (count, plural(count, "позиция", "позиции", "позиций"))
        if meta.get("fetched_at"):
            steps.append(_step("ok", "Выгрузка поставщика", "%s, скачана %s" % (shown, _when(meta["fetched_at"])), "/connections"))
        else:
            steps.append(_step("warn", "Выгрузка поставщика", "%s, время скачивания неизвестно (загружена вручную)" % shown, "/connections"))
    tochki = tochki_feed.config(conn)
    for supplier in connections.list_suppliers(conn):
        if supplier["id"] == 1:
            continue
        if supplier["id"] == tochki["supplier_id"]:
            if tochki["feed"]:                                                  # off: shown in the catalog only, its state is on «Подключения»
                steps.append(_tochki_step(unload_path, supplier, settings, now))
            continue
        title = "Поставщик «%s»" % supplier["name"]
        path = unload_path_for(unload_path, supplier["id"])
        meta = _read_meta(path)
        count = sum(1 for g in parsed.offers if supplier_of_id(g) == supplier["id"])
        # in the order of the cycle (suppliers.load_all_parsed): no file, then no positions of the territory (a broken file too), then old
        stale = stale_unload(conn, settings, unload_path, supplier["id"], now) if path.exists() else None
        if not path.exists():
            steps.append(_step("warn", title, "нет файла выгрузки: проверьте подключение и территорию", "/connections/webmim/%d" % supplier["id"]))
        elif not count and stale is None:
            steps.append(_step("bad", title, "в выгрузке нет позиций нужной территории", "/connections/webmim/%d" % supplier["id"]))
        elif stale is not None:                                             # load_merged has none of its positions then, only stocks
            steps.append(_step("warn", title, "выгрузка устарела (скачана %s): цикл её не берёт, объявления держатся как были" % _when(meta["fetched_at"]),
                               "/connections/webmim/%d" % supplier["id"]))
        else:
            when = ("скачана %s" % _when(meta["fetched_at"])) if meta.get("fetched_at") else "время скачивания неизвестно"
            steps.append(_step("ok", title, "%d %s, %s" % (count, plural(count, "позиция", "позиции", "позиций"), when),
                               "/connections/webmim/%d" % supplier["id"]))
    brands = len(ref)
    models = sum(len(items) for items in ref.values())
    if not ref:
        steps.append(_step("bad", "Справочник Авито", "Справочник Авито не загружен: без него модели не сопоставляются", "/mappings"))
    elif brands < 50:
        steps.append(_step("warn", "Справочник Авито", "брендов %d, моделей %d: похоже на временный справочник, нужен полный" % (brands, models), "/mappings"))
    else:
        steps.append(_step("ok", "Справочник Авито", "брендов %d, моделей %d" % (brands, models), "/mappings"))
    accounts = connections.list_feed_accounts(conn)
    if not accounts:
        steps.append(_step("bad", "Аккаунт Авито", "Аккаунтов Авито пока нет: добавьте на странице «Подключения»", "/connections"))
    else:
        for account in accounts:
            if not connections.list_addresses(conn, account["id"]):
                steps.append(_step(
                    "warn", "Авито адреса %s" % connections.display_name(conn, account["id"]),
                    "адреса не добавлены: фид использует общие точки, Авито такие объявления не примет",
                    "/accounts/%d/addresses" % account["id"]))
            status = connections.get_check(conn, "avito", account["id"])
            title = "Аккаунт Авито %s" % connections.display_name(conn, account["id"])
            if status is None:
                steps.append(_step("warn", title, "подключение ещё не проверялось", "/connections"))
            elif status["status"].startswith("ok"):
                steps.append(_step("ok", title, status["status"][4:], "/connections"))
            else:
                steps.append(_step("bad", title, status["status"][7:] or status["status"], "/connections"))
    pairs = len(settings.enabled)
    if pairs:
        steps.append(_step("ok", "Включённые сочетания", "Включено сочетаний: %d" % pairs, "/catalog"))
    else:
        steps.append(_step("bad", "Включённые сочетания", "Включено сочетаний: 0. Включите хотя бы одно в «Настройки» → «Бренды и наценки»", "/settings/brands"))
    return steps


def description_sample(conn, settings, parsed, ref):
    """A real product for the template preview: a publishable one if there is any, else any offer."""
    from ..candidates import Candidate

    ev = evaluation(conn, settings, parsed, ref)
    if ev.ok:
        return next(iter(ev.ok.values()))
    offer = next(iter(parsed.offers.values()), None)
    if offer is None:
        return None
    brand = resolve_brand(offer.brand_key, ref, store.load_brand_map(conn)) or offer.brand_raw
    return Candidate(offer.goods_id, offer, int(round(offer.base_price / 10.0)) * 10, brand, offer.model_raw)


def last_publish(conn):
    raw = store.get_state(conn, "last_publish")
    try:
        data = json.loads(raw) if raw else None
    except ValueError:
        return None
    if not data:
        return None
    data["lines"] = [{"text": t, "ok": t.startswith("Опубликовано"), "neutral": t.startswith("Не публикуется")}
                     for t in data.get("lines", [])]
    return data


STALE_FEED_HOURS = 3           # the cycle runs every hour (2026-10-20): two missed runs and a bit


def feed_status(conn, feed_root, now) -> list:
    """Per account: when the feed Avito reads was last updated (the public file) and when the newest one was built (the draft). Moscow time."""
    from datetime import datetime, timedelta
    from pathlib import Path
    if not feed_root:
        return []

    def stamp(path):
        try:
            return datetime.utcfromtimestamp(path.stat().st_mtime)
        except OSError:
            return None

    def moscow(value):
        return value + timedelta(hours=3) if value else None

    result = []
    for account in connections.list_feed_accounts(conn):
        folder = Path(feed_root) / account["feed_token"]
        public, draft = stamp(folder / "avito_feed.xml"), stamp(folder / "next" / "avito_feed.xml")
        result.append({"id": account["id"], "name": connections.display_name(conn, account["id"]), "live": bool(account["live"]),
                       "public_at": moscow(public), "draft_at": moscow(draft),
                       "age_hours": (now - public).total_seconds() / 3600 if public else None})
    return result


FEED_OLD_HOURS = 13             # the feed tile turns yellow: older than the night between the evening and the morning cycles


def catalog_problems(conn, settings, parsed, ref) -> list:
    """The positions the catalog's filter «Проблемы» shows (`catalog_groups`: «problem»). Only positions: a row of the unload that never
    became one (a size or price that is not a number, an Id twice) has a problem in the evaluation, but the catalog has nothing to show."""
    ev = evaluation(conn, settings, parsed, ref)
    return [g for g in parsed.offers if g in ev.problems and ev.problems[g][0].split(":", 1)[0] != "model_needs_mapping"]


def feed_tiles(conn, feeds) -> list:
    """One tile per feed: the cabinet's Avito id and the account's name, the ads in the feed, when Avito's file was updated (MSK, from
    `feed_status`; an account without a file there is shown with its ads only)."""
    status = {f["id"]: f for f in feeds}
    short = lambda value: value.strftime("%d.%m %H:%M") if value else None
    tiles = []
    for account in connections.list_feed_accounts(conn):
        feed = status.get(account["id"])
        cabinet = connections.get_avito_identity(conn, account["id"])["id"]
        title = " · ".join(["Фид Авито"] + (["ID %s" % cabinet] if cabinet else []) + [account["name"]])
        public_at, draft_at = (feed["public_at"], feed["draft_at"]) if feed else (None, None)
        newer = draft_at is not None and (public_at is None or draft_at > public_at)
        old = feed is not None and feed["age_hours"] is not None and feed["age_hours"] > FEED_OLD_HOURS
        tiles.append({"title": title, "ads": len(store.load_published(conn, account["id"])), "live": bool(account["live"]), "known": feed is not None,
                      "public_short": short(public_at), "draft_short": short(draft_at) if newer else None,
                      "warn": not account["live"] or old})
    return tiles


def dashboard_data(conn, settings, parsed, ref, unload_path="", feed_root="", now=None, catalog=None) -> dict:
    """`catalog`: the settings and positions as the catalog page shows them (unconfirmed changes, 4tochki for viewing, the owner's
    exclusions hidden), so that «Позиции с проблемами» counts what «Разобрать» opens; without it, the active settings and `parsed`."""
    unmatched, unmatched_goods = unmatched_models(conn, parsed, ref)
    if store.get_state(conn, "nav_pending_pairs") != str(unmatched):
        store.set_state(conn, "nav_pending_pairs", str(unmatched))
    not_ready = [step for step in readiness(conn, unload_path, parsed, ref, settings, now) if step["level"] != "ok"]
    last = last_publish(conn)
    from .. import staging
    staged = staging.summary(conn)
    attention = []
    if unmatched:
        attention.append({"level": "warn", "title": "Модели ждут сопоставления",
                          "detail": "%d %s (%d %s): без сопоставления объявление не соберётся" % (
                              unmatched, plural(unmatched, "модель", "модели", "моделей"),
                              unmatched_goods, plural(unmatched_goods, "позиция", "позиции", "позиций")),
                          "href": "/mappings", "action": "Сопоставить"})
    problems = catalog_problems(conn, *(catalog or (settings, parsed)), ref)
    if problems:
        attention.append({"level": "warn", "title": "Позиции с проблемами",
                          "detail": "%d: ошибка цены, нет данных или обязательных полей" % len(problems),
                          "href": "/catalog?st=problem", "action": "Разобрать"})
    if staged["draft_id"] is not None and not staged["stale"] and staged["count"]:
        attention.append({"level": "warn", "title": "Есть неподтверждённые изменения",
                          "detail": "%d: пока вы их не подтвердите, фид не меняется" % staged["count"],
                          "href": "/catalog/review", "action": "Посмотреть и применить"})
    if not_ready:
        attention.append({"level": "bad" if any(st["level"] == "bad" for st in not_ready) else "warn",
                          "title": "Подготовка к публикации",
                          "detail": "Не готово: %s" % ", ".join(step["title"] for step in not_ready),
                          "href": "#readiness", "action": "Показать"})
    failed = [line for line in (last["lines"] if last else []) if not line["ok"] and not line["neutral"]]
    if failed:
        attention.append({"level": "warn", "title": "Последняя публикация прошла не полностью",
                          "detail": "; ".join(line["text"] for line in failed)[:200],
                          "href": "/publish/result", "action": "Подробнее"})
    from .. import avito_report
    many = len(connections.list_feed_accounts(conn)) > 1
    for account in connections.list_feed_accounts(conn):
        if account["feed_mode"] == "mirror" and not account["live"]:
            continue                                                       # the errors of somebody else's file, not ours until the account is in the air with our link
        report = avito_report.load(conn, account["id"])
        if not report:
            continue
        when = report["at"][8:10] + "." + report["at"][5:7] + " " + report["at"][11:16]
        who = "«%s»: " % connections.display_name(conn, account["id"]) if many else ""
        errors = []
        for group in report["groups"]:
            n = group["count"]
            if group["kind"] == "payment":
                attention.insert(0, {"level": "bad", "title": "Авито не активировал %d %s: нужно пополнить аванс" % (n, plural(n, "объявление", "объявления", "объявлений")),
                                     "detail": "%sКак только на счёте появятся деньги, объявления включатся сами; фид менять не нужно. Данные Авито на %s МСК." % (who, when),
                                     "href": "https://pro.avito.ru/cpa", "action": "Пополнить аванс", "external": True})
            else:
                errors.append(group)
        if len(errors) == 1:
            group = errors[0]
            n = group["count"]
            attention.append({"level": "warn", "title": "Авито не принял %d %s" % (n, plural(n, "объявление", "объявления", "объявлений")),
                              "detail": "%s%s. Например: %s. Данные Авито на %s МСК." % (who, group["text"][:160], ", ".join(group["examples"][:3]), when),
                              "href": "/history/ads", "action": "События позиций"})
        elif errors:                                                       # one card for the account, with the most frequent reasons, not one card per kind of error
            total = sum(g["count"] for g in errors)
            top = sorted(errors, key=lambda g: -g["count"])
            more = len(top) - 3
            reasons = "; ".join("%d× %s" % (g["count"], g["text"][:70]) for g in top[:3]) + (" и ещё %d %s" % (more, plural(more, "вид", "вида", "видов")) if more > 0 else "")
            attention.append({"level": "warn", "title": "Авито не принял %d %s" % (total, plural(total, "объявление", "объявления", "объявлений")),
                              "detail": "%s%s. Например: %s. Данные Авито на %s МСК." % (who, reasons, ", ".join(top[0]["examples"][:3]), when),
                              "href": "/history/ads", "action": "События позиций"})
    feeds = feed_status(conn, feed_root, now) if now is not None else []
    short = lambda value: value.strftime("%d.%m %H:%M") if value else None
    for feed in feeds:
        feed["public_short"], feed["draft_short"] = short(feed["public_at"]), short(feed["draft_at"])
        multi = "«%s»: " % feed["name"] if len(feeds) > 1 else ""
        if not feed["live"]:
            tail = ("Авито читает фид, обновлённый %s МСК" % feed["public_short"]) if feed["public_at"] else "Фид для Авито ещё не публиковался"
            built = "; свежий фид собран %s МСК, но у нас не опубликован" % feed["draft_short"] if feed["draft_at"] else ""
            attention.insert(0, {"level": "bad", "title": "Публикация фида выключена",
                                 "detail": "%s%s%s. Включите публикацию в «Подключения»." % (multi, tail, built),
                                 "href": "/connections/avito", "action": "Включить"})
        elif feed["public_at"] is None:
            attention.insert(0, {"level": "warn", "title": "Фид ещё не публиковался", "detail": multi + "публикация включена, но файла для Авито пока нет: он появится после ближайшего цикла.",
                                 "href": "/history", "action": "Журнал"})
        elif feed["age_hours"] > STALE_FEED_HOURS:
            attention.insert(0, {"level": "warn", "title": "Фид давно не обновлялся",
                                 "detail": "%sпоследнее обновление %s МСК (%d ч назад), цикл идёт каждый час. Проверьте журнал запусков." % (
                                     multi, feed["public_short"], feed["age_hours"]),
                                 "href": "/history", "action": "Журнал"})
    return {
        "run": store.last_run(conn),
        "pending_pairs": unmatched,
        "pending_goods": unmatched_goods,
        "has_data": bool(parsed.offers),
        "readiness": not_ready,
        "attention": attention,
        "feeds": feed_tiles(conn, feeds),
    }


from collections import defaultdict

from ..catalog import classify_model, normalize_model, resolve_brand, resolve_model, suggest_brand

SEASONS = ("winter", "summer", "all")


class _Filtered:
    def __init__(self, offers, issues=None):
        self.offers = offers
        self.issues = {gid: v for gid, v in (issues or {}).items() if gid in offers}
        self.other_territories = {}


def hide_excluded(parsed, raw: dict, conn=None, ref=None):
    """The catalog view without brands and radii the owner excluded. With `conn` (the catalog, its search and the dashboard; not the
    economics): the positions of 4tochki only of the brands we work with (the owner 2026-10-24, task 3: «тот же фильтр, что уже выбран
    для МИМ») - a brand of WebMIM with the same Avito name switched on in any season, or the 4tochki brand itself switched on."""
    worked = _tochki_worked(conn, raw, parsed, ref) if conn is not None else None
    if worked is None and not (raw.get("excluded_brands") or raw.get("excluded_sizes")):
        return parsed
    prefix = worked[0] if worked else None
    return _Filtered({g: o for g, o in parsed.offers.items() if not excluded_by_owner(raw, o)
                      and (prefix is None or not g.startswith(prefix) or worked[1](o.brand_key))}, getattr(parsed, "issues", {}))


def _tochki_worked(conn, raw: dict, parsed, ref):
    """(the Id prefix of 4tochki positions, is this brand worked) or None when no 4tochki row is chosen or it has nothing on view.
    The season is not checked (the catalog of WebMIM shows every season of a switched-on brand too). A brand without an Avito name
    is compared by its key without «S<N>:», case aside."""
    from .. import tochki_feed
    sid = tochki_feed.config(conn)["supplier_id"]
    if not sid:
        return None
    prefix, key_prefix = "s%d-" % sid, "S%d:" % sid
    if not any(g.startswith(prefix) for g in parsed.offers):
        return None
    brand_map = store.load_brand_map(conn)
    ref = ref or {}

    def name(key):
        bare = re.sub(r"^S\d+:", "", key)
        return (resolve_brand(key, ref, brand_map) or bare).casefold()
    on = {b for b, _season in raw.get("enabled") or ()}
    own = {b for b in on if b.startswith(key_prefix)}
    webmim = {name(b) for b in on if not re.match(r"S\d+:", b)}
    seen = {}

    def worked(key):
        if key not in seen:
            seen[key] = key in own or name(key) in webmim
        return seen[key]
    return prefix, worked


def supplier_of_id(goods_id: str) -> int:
    match = re.match(r"s(\d+)-", goods_id or "")
    return int(match.group(1)) if match else 1


def supplier_of(offer) -> int:
    return supplier_of_id(offer.goods_id)


SUPPLIER_MARKS = {"mim": {"kind": "mim", "text": "MIM", "title": "Поставщик: WebMIM"},
                  "t4": {"kind": "t4", "text": "4Т", "title": "Поставщик: 4 точки"}}


def supplier_mark(supplier_id: int, tochki_supplier_id) -> dict:
    """The sign of the supplier of a position for the catalog of all suppliers: 4 точки is the supplier row chosen on its tab, every other row is WebMIM."""
    return SUPPLIER_MARKS["t4" if tochki_supplier_id and supplier_id == tochki_supplier_id else "mim"]


def filter_offers(parsed, width=None, height=None, diameter=None, season=None, studs=None, supplier=None):
    """Keep only goods of the chosen size; None means any. A size may be one number or several (any of them)."""
    width, height, diameter = (None if v is None else ({v} if isinstance(v, int) else set(v)) or None
                               for v in (width, height, diameter))
    if width is None and height is None and diameter is None and season is None and studs is None and supplier is None:
        return parsed
    return _Filtered({
        gid: o for gid, o in parsed.offers.items()
        if (width is None or o.width in width) and (height is None or o.height in height)
        and (diameter is None or o.diameter in diameter) and (season is None or o.season.value == season)
        and (studs is None or o.studded == (studs == "studded")) and (supplier is None or supplier_of(o) == supplier)}, getattr(parsed, "issues", {}))


def size_options(parsed) -> dict:
    offers = parsed.offers.values()
    return {"w": sorted({o.width for o in offers}), "h": sorted({o.height for o in offers}),
            "d": sorted({o.diameter for o in offers})}


KIND_ORDER = ("summer", "all", "winter", "winter_studded")


def _offer_status(gid, offer, ev, settings):
    if gid in ev.ok:
        if offer.stock < settings.min_stock:
            return "no", REASON_TEXT["low_stock"], "low_stock"
        return "feed", "в фиде", "feed"
    if gid in ev.excluded:
        return "no", REASON_TEXT.get(ev.excluded[gid], ev.excluded[gid]), ev.excluded[gid]
    if gid in ev.problems:
        code = ev.problems[gid][0].split(":", 1)[0]
        return "no", REASON_TEXT.get(code, code), code
    return "no", REASON_TEXT["no_data"], "no_data"


SORT_KEYS = ("price", "brand", "name", "diameter", "stock")


def _compact(text: str) -> str:
    return re.sub(r"[^0-9a-zа-яё]", "", text.lower())


def search_terms(query: str) -> list:
    """The words of a catalog search, each with its packed form (no spaces or signs): [(word, packed)]."""
    return [(w, _compact(w)) for w in (query or "").lower().split()]


def offer_matches(offer, avito_brand: str, terms: list) -> bool:
    """The search of the catalog: every word of the query must be found in ONE field of the position: the name, the brand (ours and Avito's), the model,
    the article, the id, or the size written in any usual way (205/55 R16, 205 55 16, 20555r16, 205/55/16). Spaces and signs do not matter; words never
    match across the border of two fields."""
    if not terms:
        return True
    return fields_match(offer_fields(offer, avito_brand), terms)


def offer_fields(offer, avito_brand: str) -> tuple:
    """The fields offer_matches looks in: (the fields in lower case, the same packed), one per field."""
    size = ["%d/%d R%d" % (offer.width, offer.height, offer.diameter), "%d/%d/%d" % (offer.width, offer.height, offer.diameter),
            "%d %d %d" % (offer.width, offer.height, offer.diameter), "%d%d%d" % (offer.width, offer.height, offer.diameter)]
    fields = [f.lower() for f in [offer.name_orig or "", offer.name or "", offer.brand_key or "", avito_brand or "", offer.model_raw or "",
                                   offer.code or "", offer.goods_id or ""] + size if f]
    return fields, [_compact(f) for f in fields]


def fields_match(fields: tuple, terms: list) -> bool:
    """offer_matches on the fields of offer_fields (a search over many positions may keep them)."""
    plain, packed = fields
    return all(any(w in f for f in plain) or (len(cw) >= 2 and any(cw in p for p in packed)) for w, cw in terms)


def _gone_item(offer, info) -> dict:
    day = info["gone_since"][:10]
    return {"goods_id": offer.goods_id, "name": offer.name_orig or offer.name, "code": offer.code, "image": offer.image,
            "kind": tire_kind(offer.season.value, offer.studded), "supplier_price": info["last_price"], "price": None, "stock": 0,
            "diameter": offer.diameter, "model_key": normalize_model(offer.model_raw), "brand": offer.brand_key, "model": " ".join(offer.model_raw.split()),
            "size": "%d/%d R%d" % (offer.width, offer.height, offer.diameter),
            "car_type": offer.car_type.upper() if offer.car_type.upper() in ("C", "LT") else "",
            "state": "gone", "reason": "нет у поставщика с %s.%s.%s (был остаток %d)" % (day[8:10], day[5:7], day[:4], info["last_stock"]), "scode": "gone",
            "manual": None, "problem": False, "published": False, "pending": False}


VIEW_ONLY_TEXT = "4 точки: в фид не идут до этапа 2"
STOCK_CAP = 41          # 4tochki sends 41 for «more than 40»


def catalog_groups(conn, settings, parsed, ref, query="", status="all", sort="brand", descending=False,
                   base_raw=None, gone=(), view_only=frozenset(), capped_supplier=None, twins_from=None) -> list:
    """Every supplier position grouped by brand, with our price and why it is (not) in the feed. `view_only`: positions shown but
    never published (4tochki with its switch off); `capped_supplier`: the supplier whose stock above 40 comes as 41. `twins_from`: the
    positions where the same tire at another supplier is looked for (tire_match; the catalog before its filters), else `parsed`."""
    from .. import tire_match
    ev = evaluation(conn, settings, parsed, ref)
    source = twins_from if twins_from is not None else parsed
    match = tire_match.cached_pairs(source, store.load_model_synonyms(conn), capped_supplier, store.not_tire_pairs(conn))   # as the feed
    group_of = match.group_of()

    def twins(gid):
        own = tire_match.supplier_of(gid)
        out = []
        for other in group_of.get(gid, ()):
            twin = source.offers.get(other)
            if twin is None or tire_match.supplier_of(other) == own:
                continue
            cand = ev.ok.get(other)
            out.append({"goods_id": other, "mark": supplier_mark(supplier_of(twin), capped_supplier), "price": cand.price if cand else None,
                        "stock_text": ">40" if capped_supplier and supplier_of(twin) == capped_supplier and twin.stock >= STOCK_CAP
                        else str(twin.stock)})
        return out
    terms = search_terms(query)
    base = base_raw or {}
    base_overrides = base.get("overrides", {}) if base_raw else None
    base_enabled = {tuple(i) for i in base.get("enabled", [])}
    base_pricing = base.get("pricing", {})
    season_counts = defaultdict(lambda: defaultdict(int))
    groups = {}
    accounts = connections.list_feed_accounts(conn)
    published_any = set()
    suppressed_any = set()
    for acc in accounts:
        published_any |= set(store.load_published(conn, acc["id"]))
        suppressed_any |= store.load_suppressed(conn, acc["id"])
    brand_map = store.load_brand_map(conn)
    avito_names = {}
    own_any = {key.partition(":")[2] for key in settings.price_overrides}       # an own price on any account («своя цена»)
    for gid, offer in parsed.offers.items():
        season_counts[offer.brand_key][offer.season.value] += 1
        if terms:
            if offer.brand_key not in avito_names:
                avito_names[offer.brand_key] = (resolve_brand(offer.brand_key, ref, brand_map) or "").lower()
            if not offer_matches(offer, avito_names[offer.brand_key], terms):
                continue
        manual = settings.overrides.get(override_key(gid))
        if gid in view_only:
            state, text, code = "no", VIEW_ONLY_TEXT, "view_only"
        elif manual == "off":
            state, text, code = "no", "снято вами", "manual_off"
        elif gid in ev.forced and gid in ev.ok:
            need = 1 + (settings.hysteresis if gid in suppressed_any else 0)
            if offer.stock <= 0:
                state, text, code = "no", "остаток 0", "stock_zero"
            elif offer.stock < need:
                state, text, code = "no", "ждёт восстановления остатка", "waiting_stock"
            else:
                state, text, code = "feed", "добавлено вручную" + (", остаток ниже порога" if offer.stock < settings.min_stock else ""), "manual_on"
        else:
            state, text, code = _offer_status(gid, offer, ev, settings)
        if status == "gone" or (status in ("feed", "no") and state != status):
            continue
        cand = ev.ok.get(gid)
        group = groups.setdefault(offer.brand_key, {"brand": offer.brand_key, "items": [], "in_feed": 0})
        group["in_feed"] += 1 if state == "feed" else 0
        group["items"].append({
            "goods_id": gid, "name": offer.name_orig or offer.name, "code": offer.code, "image": offer.image,
            "kind": tire_kind(offer.season.value, offer.studded), "supplier_price": offer.base_price,
            "price": cand.price if cand else None, "stock": offer.stock, "diameter": offer.diameter,
            "model_key": normalize_model(offer.model_raw), "brand": offer.brand_key, "model": " ".join(offer.model_raw.split()),
            "size": "%d/%d R%d" % (offer.width, offer.height, offer.diameter),
            "car_type": offer.car_type.upper() if offer.car_type.upper() in ("C", "LT") else "",
            "state": state, "reason": text, "scode": code, "manual": manual,
            "problem": gid in ev.problems and ev.problems[gid][0].split(":", 1)[0] != "model_needs_mapping", "published": gid in published_any,
            "pending": base_overrides is not None and manual != base_overrides.get(override_key(gid)), "view_only": gid in view_only,
            "own_price": gid in own_any,
            "below_cost": bool(cand and gid in settings.own_prices and offer.base_price > 0 and cand.price < offer.base_price),
            "supplier_mark": supplier_mark(supplier_of(offer), capped_supplier),
            "stock_text": ">40" if capped_supplier and supplier_of(offer) == capped_supplier and offer.stock >= STOCK_CAP else str(offer.stock),
            "twins": twins(gid)})
    if status in ("all", "gone"):                  # what the supplier took away: shown with stock 0, never in the feed (display only)
        for offer, info in gone:
            if terms:
                if offer.brand_key not in avito_names:
                    avito_names[offer.brand_key] = (resolve_brand(offer.brand_key, ref, brand_map) or "").lower()
                if not offer_matches(offer, avito_names[offer.brand_key], terms):
                    continue
            group = groups.setdefault(offer.brand_key, {"brand": offer.brand_key, "items": [], "in_feed": 0})
            group["items"].append(dict(_gone_item(offer, info), supplier_mark=supplier_mark(supplier_of(offer), capped_supplier)))
    keyfun = {
        "price": _by_sale_price(descending, lambda i: i["name"].lower()),
        "name": lambda i: i["name"].lower(), "diameter": lambda i: (i["diameter"], i["name"].lower()),
        "stock": lambda i: i["stock"], "brand": lambda i: i["name"].lower()}[sort if sort in SORT_KEYS else "brand"]
    for group in groups.values():
        group["items"].sort(key=keyfun, reverse=descending and sort != "price")
        group["total"] = len(group["items"])
        seasons = {}
        for season in ("winter", "summer", "all"):
            rule = settings.pricing.get((group["brand"], Season(season)))
            key = "%s|%s" % (group["brand"], season)
            enabled = (group["brand"], Season(season)) in settings.enabled
            old_rule = base_pricing.get(key)
            seasons[season] = {
                "enabled": enabled, "small": rule.pct_small if rule else "", "large": rule.pct_large if rule else "",
                "count": season_counts[group["brand"]].get(season, 0),
                "pending": base_raw is not None and (
                    enabled != ((group["brand"], season) in base_enabled)
                    or (rule.pct_small if rule else None) != (old_rule["pct_small"] if old_rule else None)
                    or (rule.pct_large if rule else None) != (old_rule["pct_large"] if old_rule else None))}
        group["seasons"] = seasons
    return sorted(groups.values(), key=lambda g: g["brand"].lower(), reverse=(sort == "brand" and descending))


def _by_sale_price(descending: bool, rest):
    """Sort key by the sale price, the direction built in; positions without one go last both ways, by `rest`.
    The purchase price never orders the page: it is money, and the roles without the right would read it from the order."""
    return lambda i: (i["price"] is None, 0 if i["price"] is None else (-i["price"] if descending else i["price"]), rest(i))


def flat_items(groups, sort: str = "brand", descending: bool = False) -> list:
    """Every position of every brand in one list, in the order the owner asked for."""
    items = [item for group in groups for item in group["items"]]
    keyfun = {
        "price": _by_sale_price(descending, lambda i: (i["brand"].lower(), i["name"].lower())),
        "name": lambda i: i["name"].lower(), "diameter": lambda i: (i["diameter"], i["brand"].lower(), i["name"].lower()),
        "stock": lambda i: (i["stock"], i["brand"].lower()),
        "brand": lambda i: (i["brand"].lower(), i["name"].lower())}[sort if sort in SORT_KEYS else "brand"]
    items.sort(key=keyfun, reverse=descending and sort != "price")
    return items


OUT_OF_STOCK_TEXT = "нет в наличии"


def catalog_preview(conn, settings, parsed, ref, goods_id, today, gone_since=""):
    """The ad of one position as it would be built right now (first Avito account's addresses and description). `parsed` holds the position:
    the panel's set, or the one position the catalog shows from elsewhere (4tochki before its switch, an old unload, the archive: `gone_since`
    the day the supplier took it away)."""
    from ..feed import render_ad
    offer = parsed.offers.get(goods_id)
    if offer is None:
        return None
    ev = evaluation(conn, settings, parsed, ref)
    state, reason, _code = _offer_status(goods_id, offer, ev, settings)
    if gone_since or offer.stock <= 0:                   # nothing to sell: the card says so first (the owner 2026-10-29)
        day = gone_since[:10]
        state, reason = "no", OUT_OF_STOCK_TEXT + (" (нет у поставщика с %s.%s.%s)" % (day[8:10], day[5:7], day[:4]) if day else "")
    accounts = connections.list_feed_accounts(conn)
    account = accounts[0] if accounts else None
    eff = connections.effective_settings(conn, account["id"], settings) if account else settings
    cand = ev.ok.get(goods_id)
    data = {"goods_id": goods_id, "name": offer.name_orig or offer.name, "code": offer.code, "stock": offer.stock,
            "image": offer.image, "built": cand is not None, "state": state, "reason": reason,
            "account": connections.display_name(conn, account["id"]) if account else None, "pack": None, "price_note": None}
    if cand is None:
        cand, data["price_note"] = _unbuilt_candidate(conn, settings, ref, offer)
    point = next(iter(eff.point_values), "—")
    ad = render_ad(cand, cand.price, point, eff, today, template=account["description_template"] if account else None,
                   old_models=store.load_old_model_names(conn))
    fields = {tag: (ad.findtext(tag) or "") for tag in (
        "Title", "Price", "Brand", "Model", "TireType", "Condition", "Description", "Address", "SellerAddressID")}
    data.update(fields=fields, address=fields["Address"] or fields["SellerAddressID"], price=cand.price or None)
    if data["built"] and eff.pack4_for(offer) and offer.stock >= 4:
        data["pack"] = {"price": cand.price * 4}
    return data


def catalog_item_view(conn, settings, parsed, ref, goods_id, today, money: bool, orders: bool, gone_since=""):
    """The page of one position in the one look of the cards (_entity.html), a dict ready to be JSON; None when the supplier has no such position.
    The ad as catalog_preview builds it, the stock, the sale price, the ads of the accounts (their last published feeds) and the orders with it.
    `money`: the purchase price is in it (the right «money»), `orders`: the orders are (the person may open them). A position without stock
    (`gone_since`: taken away by the supplier) says «нет в наличии», its last known prices and when it was last in stock (catalog_archive)."""
    from .. import catalog_archive
    from ..feed import ad_from_text
    p = catalog_preview(conn, settings, parsed, ref, goods_id, today, gone_since)
    if p is None:
        return None
    offer = parsed.offers[goods_id]
    out = bool(gone_since) or offer.stock <= 0
    if out:
        info = catalog_archive.item_info(conn, goods_id)
        last = info["last_seen"] if info and info["last_stock"] > 0 else None     # the archive keeps the last time it was seen with its stock
        fields = [{"label": "Код", "value": p["code"], "mono": True}, {"label": "Id поставщика", "value": goods_id, "mono": True},
                  {"label": "Остаток", "value": OUT_OF_STOCK_TEXT},
                  {"label": "Последний раз в наличии", "value": last, "fmt": "dt"},
                  {"label": "Последняя цена продажи (за 1 шт.)", "value": p["price"], "fmt": "money"}]
        if money:
            fields.append({"label": "Последняя закупка (за 1 шт.)", "value": offer.base_price if offer.base_price > 0 else None, "fmt": "money"})
    else:
        fields = [{"label": "Код", "value": p["code"], "mono": True}, {"label": "Id поставщика", "value": goods_id, "mono": True},
                  {"label": "Остаток", "value": "%d шт." % p["stock"]},
                  {"label": "Цена продажи (за 1 шт.)", "value": p["price"], "fmt": "money"}]
        if p["pack"]:
            fields.append({"label": "Комплект за 4 шт.", "value": p["pack"]["price"], "fmt": "money"})
        if money:
            fields.append({"label": "Закупка (за 1 шт.)", "value": offer.base_price if offer.base_price > 0 else None, "fmt": "money"})
    fields += [{"label": "Бренд, модель", "value": ", ".join(x for x in (p["fields"]["Brand"], p["fields"]["Model"]) if x)},
               {"label": "Размер", "value": "%s/%s R%s" % (offer.width, offer.height, offer.diameter)},
               {"label": "Тип шин", "value": p["fields"]["TireType"]}, {"label": "Адрес в объявлении", "value": p["address"], "wide": True}]
    ads = []
    for account_id, price, point, snapshot in conn.execute(
            "SELECT account_id, price, point, snapshot FROM published_state WHERE goods_id = ? ORDER BY account_id", (goods_id,)):
        try:
            ad_id = ad_from_text(snapshot).findtext("Id") or ""
        except Exception:                                                 # an old or broken snapshot: the row is still shown, without the Id
            ad_id = ""
        ads.append({"account": connections.display_name(conn, account_id), "ad_id": {"text": ad_id, "mono": True},
                    "price": {"text": price, "fmt": "money"}, "point": point})
    rows = conn.execute(
        "SELECT c.id, c.client_name, s.name, i.qty, i.sale_unit, c.created_at FROM order_item i JOIN order_card c ON c.id = i.order_id "
        "JOIN order_stage s ON s.id = c.stage_id WHERE i.goods_id = ? ORDER BY c.id DESC LIMIT 50", (goods_id,)) if orders else []
    found = [{"ref": {"text": "№%d" % r[0], "href": "/orders/%d" % r[0]}, "client": r[1], "stage": r[2], "qty": r[3],
              "unit": {"text": r[4], "fmt": "money"}, "created": {"text": r[5], "fmt": "dt"}} for r in rows]
    return {"type": "item", "kind": "Позиция", "ref": p["code"] or goods_id, "title": p["name"],
            "status": {"text": OUT_OF_STOCK_TEXT if out else p["reason"], "tone": "ok" if p["state"] == "feed" else "warn"}, "out_of_stock": out,
            "crumbs": [{"label": "Каталог", "href": "/catalog"}, {"label": p["code"] or goods_id}],
            "goods_id": goods_id, "image": p["image"], "built": p["built"], "reason": p["reason"], "price_note": p["price_note"],
            "ad_title": p["fields"]["Title"], "description": p["fields"]["Description"],
            "fields": fields, "ads": ads, "orders": found if orders else None}


def own_price_text(value) -> str:
    """{"pct": 12.5} -> «+12,5 % к закупке», {"price": 6990} -> «цена 6 990 ₽»; None -> «как у бренда»."""
    if not value:
        return "как у бренда"
    if "pct" in value:
        return ("%+g" % float(value["pct"])).replace(".", ",").replace("-", "−") + " % к закупке"
    return "цена %s ₽" % "{:,}".format(int(value["price"])).replace(",", " ")


def held_by_other(conn, account: dict, offer, key, offers, min_stock: int) -> Optional[str]:
    """The code of another lot of the same tire whose ad the account carries (one tire, one ad: same_tire), when this lot has none there: its own
    price then does not reach Avito (the own price belongs to the lot, the lead 2026-10-29). By the last published feed and, for a mirror account
    only, the mirrored file (an adopted account keeps the old file in the base). The other lot must be in today's unload (a held lot gone from it
    hands its Id over next cycle), a mirrored ad also not below the threshold (else it is not published)."""
    account_id = account["id"]
    from ..mirror_feed import supplier_code
    from ..same_tire import identity_of_ad
    if key is None:
        return None
    gid = offer.goods_id
    like = ("%%>%s<%%" % offer.width, "%%>%s<%%" % offer.diameter)
    rows = [(g, text) for g, text in conn.execute(
        "SELECT goods_id, snapshot FROM published_state WHERE account_id = ? AND snapshot LIKE ? AND snapshot LIKE ?", (account_id,) + like)]
    mirrored = []
    if account.get("feed_mode") == "mirror":
        mirrored = [(supplier_code(ad_id), text) for ad_id, text in conn.execute(
            "SELECT ad_id, raw FROM imported_ad WHERE account_id = ? AND raw LIKE ? AND raw LIKE ?", (account_id,) + like)]
    if any(g == gid for g, _ in rows + mirrored) or conn.execute(
            "SELECT 1 FROM published_state WHERE account_id = ? AND goods_id = ?", (account_id, gid)).fetchone():
        return None
    live = [g for g, _ in rows if g in offers] + [g for g, _ in mirrored if g in offers and offers[g].stock >= min_stock]
    texts = dict(mirrored + rows)
    return next((g for g in live if g != gid and identity_of_ad(texts[g]) == key), None)


def own_price_rows(conn, general, active, offer, ref=None, offers=None) -> list:
    """One row per feed account for the card of a position: the price now (with the working draft), by the brand, the own price and whether it is
    below the purchase. `general`: the settings with the draft, `active`: without it (a row whose own price is not applied yet is «pending").
    `held_by`: another lot of the same tire carries the account's ad (held_by_other), {goods_id, code}."""
    from ..pricing import position_price
    from ..same_tire import identity
    from .. import tochki_feed
    tire = identity(_unbuilt_candidate(conn, general, ref, offer)[0]) if ref is not None else None
    tochki = tochki_feed.config(conn)["supplier_id"]
    rows = []
    for account in connections.list_feed_accounts(conn):
        eff = connections.effective_settings(conn, account["id"], general)
        key = "%d:%s" % (account["id"], offer.goods_id)
        own, was = general.price_overrides.get(key), active.price_overrides.get(key)
        price = position_price(offer, eff)
        brand = position_price(offer, dataclasses.replace(eff, own_prices={}))
        rows.append({"account_id": account["id"], "account": account["name"], "price": price, "brand_price": brand,
                     "own": {own[0]: own[1]} if own else None, "own_text": own_price_text({own[0]: own[1]}) if own else "",
                     "below": bool(own and price is not None and offer.base_price > 0 and price < offer.base_price),
                     "pending": own != was, "held_by": None})
        other = held_by_other(conn, account, offer, tire, offers or {}, eff.min_stock)
        if other is not None:
            holder = (offers or {}).get(other)
            mark = supplier_mark(supplier_of(holder), tochki) if holder is not None else None
            rows[-1]["held_by"] = {"goods_id": other, "code": getattr(holder, "code", "") or other,
                                   "where": "склад " + mark["title"].replace("Поставщик: ", "") if mark else ""}
    return rows


def own_price_lines(conn, raw: dict, changes: dict, offers, old, new) -> list:
    """«было → стало» of the own prices of positions in a draft: the price of one tyre in each account before and after."""
    from ..pricing import position_price
    if "price_overrides" not in changes:
        return []
    before, after = raw.get("price_overrides") or {}, changes.get("price_overrides") or {}
    names = {a["id"]: a["name"] for a in connections.list_accounts(conn)}
    lines = []
    for key in sorted(set(before) | set(after)):
        if before.get(key) == after.get(key):
            continue
        account_id, _, goods_id = key.partition(":")
        account_id = int(account_id)
        offer = (offers or {}).get(goods_id)
        label = "%s (%s)" % (offer.name_orig or offer.name, offer.code) if offer else goods_id
        what = "Своя цена" if after.get(key) else "Как у бренда"
        text = "%s · %s: %s" % (what, names.get(account_id, "аккаунт %d" % account_id), label)
        if offer is not None:
            was = position_price(offer, connections.effective_settings(conn, account_id, old))
            now_ = position_price(offer, connections.effective_settings(conn, account_id, new))
            text += ": %s → %s" % (_rub(was), _rub(now_))
            if after.get(key) and now_ is not None and offer.base_price > 0 and now_ < offer.base_price:
                text += " — ниже закупки"
        else:
            text += " (позиции нет в выгрузке)"
        lines.append(text + " (%s)" % own_price_text(after.get(key)))
    return lines


OWN_JUMP = 0.30            # a fixed own price this far from the feed's price (either way) is asked once more (the lead 2026-10-29)


def own_price_risky(conn, raw: dict, changes: dict, offers, old) -> list:
    """The fixed own prices of a draft to confirm once more (a typo, 699 for 6990): below the purchase, or more than OWN_JUMP away from the price
    the account's feed has now (its last published one, else the price by today's settings). Nothing is changed nor limited: only asked."""
    from ..pricing import own_price, position_price
    if "price_overrides" not in changes:
        return []
    before, after = raw.get("price_overrides") or {}, changes.get("price_overrides") or {}
    names = {a["id"]: a["name"] for a in connections.list_accounts(conn)}
    out = []
    for key in sorted(after):
        value = after[key]
        if before.get(key) == value or "price" not in value:
            continue
        account_id, _, goods_id = key.partition(":")
        account_id = int(account_id)
        offer = (offers or {}).get(goods_id)
        if offer is None:
            continue
        price = own_price(offer.base_price, ("price", value["price"]))
        row = conn.execute("SELECT price FROM published_state WHERE account_id = ? AND goods_id = ?", (account_id, goods_id)).fetchone()
        was = row[0] if row and row[0] else position_price(offer, connections.effective_settings(conn, account_id, old))
        why = []
        if offer.base_price > 0 and price < offer.base_price:
            why.append("ниже закупки")
        if was and abs(price / was - 1) > OWN_JUMP:
            why.append("отличается от цены фида на %+d %%" % round((price / was - 1) * 100))
        if why:
            out.append("%s · %s (%s): было %s → станет %s, закупка %s — %s" % (
                names.get(account_id, "аккаунт %d" % account_id), offer.name_orig or offer.name, offer.code, _rub(was), _rub(price),
                _rub(offer.base_price if offer.base_price > 0 else None), ", ".join(why)))
    return out


def _rub(value) -> str:
    return "—" if value is None else "%s ₽" % "{:,}".format(int(value)).replace(",", " ")


def _unbuilt_candidate(conn, settings, ref, offer):
    """The ad of a position that is not in the feed, built as far as the data allows: the price is left out when there is no percent."""
    from ..candidates import Candidate
    from ..catalog import resolve_brand, resolve_model
    from ..pricing import position_price
    price, note = 0, None
    if (offer.brand_key, offer.season) not in settings.pricing and offer.goods_id not in settings.own_prices:
        note = "процент не задан"
    elif offer.base_price <= 0:
        note = "некорректная цена поставщика"
    else:
        price = position_price(offer, settings)
    brand = resolve_brand(offer.brand_key, ref, store.load_brand_map(conn))
    model = resolve_model(brand, offer.model_raw, store.load_confirmed_mappings(conn), ref) if brand else None
    return Candidate(offer.goods_id, offer, price, brand or offer.brand_raw, model or offer.model_raw), note


def orphan_overrides(settings, parsed) -> list:
    """Manual rules for positions the supplier no longer lists."""
    result = []
    for key, mode in sorted(settings.overrides.items()):
        goods_id = key.split(":", 1)[1] if ":" in key else key
        if goods_id not in parsed.offers:
            result.append({"goods_id": goods_id, "mode": mode})
    return result


def tire_kind(season: str, studded: bool) -> str:
    if season == "winter":
        return "winter_studded" if studded else "winter"
    return season


def excluded_by_owner(raw: dict, offer) -> bool:
    """The owner switched this brand (or this brand in this radius) off for the supplier: it is not shown in the catalog."""
    from ..filters import owner_exclusion
    return owner_exclusion(offer.brand_key, offer.diameter, raw.get("excluded_brands") or (), raw.get("excluded_sizes") or ()) is not None


def brands_overview(conn, raw: dict, parsed, ref, proposals: bool = True) -> list:
    brand_map = store.load_brand_map(conn)
    enabled = {(b, s) for b, s in raw["enabled"]}
    pricing = raw["pricing"]
    counts = defaultdict(lambda: defaultdict(int))
    studs = defaultdict(lambda: defaultdict(int))
    models = defaultdict(dict)
    confirmed = store.load_confirmed_mappings(conn)
    for offer in parsed.offers.values():
        if excluded_by_owner(raw, offer):
            continue
        counts[offer.brand_key][offer.season.value] += 1
        if offer.season.value == "winter":
            studs[offer.brand_key]["studded" if offer.studded else "friction"] += 1
        item = models[offer.brand_key].setdefault(normalize_model(offer.model_raw), {"key": normalize_model(offer.model_raw), "name": " ".join(offer.model_raw.split()), "goods": 0, "stock": 0, "seasons": set(), "cartypes": set(), "sizes": {}})
        item["goods"] += 1
        size = item["sizes"].setdefault((offer.diameter, offer.width, offer.height), [0, 0])
        size[0] += 1
        size[1] += offer.stock
        item["stock"] += offer.stock
        item["seasons"].add(tire_kind(offer.season.value, offer.studded))
        if offer.car_type.upper() in ("C", "LT"):
            item["cartypes"].add(offer.car_type.upper())
    keys = (set(counts) | {b for b, _ in enabled} | {k.split("|")[0] for k in pricing}) - set(raw.get("excluded_brands") or ())
    rows = []
    for key in sorted(keys, key=lambda k: (-sum(counts[k].values()) if k in counts else 0, k)):
        avito = resolve_brand(key, ref, brand_map)
        seasons = {}
        for season in SEASONS:
            rule = pricing.get("%s|%s" % (key, season))
            seasons[season] = {
                "count": counts[key].get(season, 0) if key in counts else 0,
                "studded": studs[key].get("studded", 0) if season == "winter" else None,
                "friction": studs[key].get("friction", 0) if season == "winter" else None,
                "enabled": (key, season) in enabled,
                "small": rule["pct_small"] if rule else "",
                "large": rule["pct_large"] if rule else "",
            }
        model_rows = []
        for item in sorted(models.get(key, {}).values(), key=lambda m: (-m["goods"], m["name"])):
            found, proposal, tier, options = None, None, "none", []
            if avito:
                found = resolve_model(avito, item["name"], confirmed, ref)
                if not found and proposals:
                    tier, options = classify_model(avito, item["name"], ref)
                    proposal = options[0] if options and tier in ("prefix", "code") else None
            model_rows.append(dict(item, seasons=[k for k in KIND_ORDER if k in item["seasons"]], cartypes=sorted(item["cartypes"]),
                                   avito_model=found, proposal=proposal, tier=tier, options=list(options),
                                   sizes=["%d/%d R%d (%d)" % (w, h, d, stock) for (d, w, h), (_, stock) in sorted(item["sizes"].items())]))
        rows.append({
            "models": model_rows,
            "avito_models": sorted(ref.get(avito, frozenset())) if avito and any(not m["avito_model"] for m in model_rows) else [],
            "brand_key": key,
            "goods": sum(counts[key].values()) if key in counts else 0,
            "avito_brand": avito,
            "suggestions": [] if avito else suggest_brand(key, ref),
            "seasons": seasons,
        })
    return rows


from datetime import date

from ..config import settings_from_dict
from ..preview import preview_change
from ..settings_store import get_active, get_draft, get_version, needs_strong_confirmation


def _float(value: str, name: str) -> float:
    try:
        number = float(value.strip().replace(",", ".").replace("\u2212", "-"))
    except ValueError:
        raise ValueError("%s: нужно число, получено %r" % (name, value))
    if not math.isfinite(number):
        raise ValueError("%s: нужно конечное число, получено %r" % (name, value))
    return number


INT_FIELDS = ("min_stock", "min_diameter", "portion_limit", "stale_days", "date_end_days")


def changes_from_settings_form(raw: dict, form) -> dict:
    changes = {}
    for name in INT_FIELDS:
        if name in form and form[name].strip():
            value = _float(form[name], name)
            if value != int(value):
                raise ValueError("%s: нужно целое число" % name)
            if int(value) != raw[name]:
                changes[name] = int(value)
    if "pack4_form" in form:
        old = pack4_rules_of(raw)
        rules = set(old) - set(form.getlist("p4_del") if hasattr(form, "getlist") else [])
        if form.get("p4_add"):
            if not form.get("p4_brand", "").strip():
                raise ValueError("Выберите бренд для правила «за 4 шт.» (или «Все бренды»).")
            rules.add("%s|%s|%s" % (form["p4_brand"].strip(), form.get("p4_season", "*"), form.get("p4_d", "*").strip()))
        if sorted(rules) != old:
            changes["pack4_rules"] = sorted(rules)
            changes["pack4"] = bool(rules)
    if "point_mode" in form and form["point_mode"] != raw["point_mode"]:
        changes["point_mode"] = form["point_mode"]
    values = {key.split("|", 1)[1]: _float(form[key], key) for key in form
              if key.startswith("pv|") and form[key].strip()}
    if values and values != {k: float(v) for k, v in raw["point_values"].items()}:
        changes["point_values"] = values
    if "point_rest" in form:
        rest = form["point_rest"].strip() or None
        if rest != raw.get("point_rest"):
            changes["point_rest"] = rest
    return changes


def pack4_rules_of(raw: dict) -> list:
    """The «за 4 шт.» rules; the old bare switch means every position."""
    if "pack4_rules" in raw:
        return sorted(raw["pack4_rules"] or [])
    return ["*|*|*"] if raw.get("pack4") else []


def pack4_rule_text(rule: str) -> str:
    brand, season, diameter = rule.split("|")
    supplier = re.match(r"^S(\d+):", brand)
    brand = re.sub(r"^S\d+:", "", brand) + (" (поставщик %s)" % supplier.group(1) if supplier else "")
    return " · ".join((
        "все бренды" if brand == "*" else brand,
        "все сезоны" if season == "*" else SEASON_NAMES.get(season, season),
        "все радиусы" if diameter == "*" else "R" + diameter))


SEASON_NAMES = {"winter": "зима", "summer": "лето", "all": "всесезонные"}
SETTING_LABELS = {
    "min_stock": "Минимальный остаток", "hysteresis": "Запас гистерезиса", "min_diameter": "Минимальный диаметр",
    "portion_limit": "Лимит новых за цикл", "stale_days": "Дней «пропуска» до снятия",
    "date_end_days": "Срок размещения вперёд, дней", "point_mode": "Распределение по точкам",
    "point_rest": "Точка «остаток»", "pack4": "Второе объявление «за 4 шт.»",
}
POINT_MODES = {"percent": "проценты", "count": "количество"}
REASON_TEXT = {
    "model_needs_mapping": "не сопоставлена модель с моделью Авито",
    "brand_not_in_reference": "бренд не найден в справочнике Авито",
    "no_pricing_rule": "не задан процент цены",
    "bad_price": "некорректная цена у поставщика",
    "unknown_season": "неизвестный сезон",
    "brand_excluded": "бренд исключён из выгрузки",
    "size_excluded": "размер исключён из выгрузки",
    "commercial": "коммерческая шина (C/LT)",
    "old_production": "старая продукция",
    "diameter": "диаметр меньше минимального",
    "low_stock": "остаток ниже порога",
    "manual_ad": "в кабинете есть ручное объявление этой шины",
    "not_enabled": "сочетание не включено",
    "no_data": "нет данных поставщика",
    "manual_off": "снято вами",
    "tochki_twin": "у WebMIM есть или была такая же шина (проба 4 точек: без двойников)",
    "tochki_pair": "в паре с WebMIM, а склейка в пробе 4 точек выключена",
    "tochki_unclear": "не понять, нет ли двойника (проба 4 точек)",
    "tochki_not_allowed": "не в списке допуска пробы 4 точек",
    "with_tube": "шина с камерой",
}


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if 11 <= n % 100 <= 14:
        return many
    if n % 10 == 1:
        return one
    if 2 <= n % 10 <= 4:
        return few
    return many


def _pct(value) -> str:
    if value is None:
        return "не задан"
    return ("%+g" % float(value)).replace("-", "−") + " %"


def _points(values: dict) -> str:
    return ", ".join("%s %g" % (key, float(value)) for key, value in values.items())


def _override_lines(raw: dict, changes: dict, offers, min_stock) -> list:
    old, new = raw.get("overrides") or {}, changes.get("overrides") or {}
    lines = []
    for key in sorted(set(old) | set(new)):
        if old.get(key) == new.get(key):
            continue
        goods_id = key.split(":", 1)[1] if ":" in key else key
        offer = (offers or {}).get(goods_id)
        label = "%s (%s)" % (offer.name_orig or offer.name, offer.code) if offer else goods_id
        mode = new.get(key)
        if mode == "off":
            lines.append("Снять: %s" % label)
        elif mode == "on":
            warn = " — остаток %d ниже порога" % offer.stock if offer and min_stock and offer.stock < min_stock else ""
            lines.append("Добавить вручную: %s%s" % (label, warn))
        else:
            lines.append("Вернуть обычные правила: %s" % label)
    return lines


def size_rule_text(rule: str) -> str:
    brand, _, diameter = rule.rpartition("|")
    match = re.match(r"S(\d+):(.*)", brand)
    name, tail = (match.group(2), " (поставщик №%s)" % match.group(1)) if match else (brand, "")
    return "%s · R%s%s" % ("все бренды" if name == "*" else name, diameter, tail)


def _brand_text(key: str) -> str:
    match = re.match(r"S(\d+):(.*)", key)
    return "%s (поставщик №%s)" % (match.group(2), match.group(1)) if match else key


def describe_changes(raw: dict, changes: dict, offers=None, min_stock=None) -> list:
    lines = _override_lines(raw, changes, offers, min_stock)
    if "enabled" in changes:
        old = {tuple(item) for item in raw["enabled"]}
        new = {tuple(item) for item in changes["enabled"]}
        for brand, season in sorted(new - old):
            lines.append("Включить публикацию: %s · %s" % (brand, SEASON_NAMES.get(season, season)))
        for brand, season in sorted(old - new):
            lines.append("Выключить публикацию: %s · %s" % (brand, SEASON_NAMES.get(season, season)))
    if "pricing" in changes:
        for key in sorted(changes["pricing"]):
            new_rule = changes["pricing"][key]
            old_rule = raw["pricing"].get(key)
            if new_rule == old_rule:
                continue
            brand, season = key.split("|", 1)
            lines.append("Процент %s · %s: до R17 %s → %s, от R18 %s → %s" % (
                brand, SEASON_NAMES.get(season, season),
                _pct(old_rule["pct_small"] if old_rule else None), _pct(new_rule["pct_small"]),
                _pct(old_rule["pct_large"] if old_rule else None), _pct(new_rule["pct_large"])))
    for key in sorted(changes):
        if key == "excluded_brands":
            old_set, new_set = set(raw.get(key) or []), set(changes[key] or [])
            for brand in sorted(new_set - old_set):
                lines.append("Исключить бренд из выгрузки: %s" % _brand_text(brand))
            for brand in sorted(old_set - new_set):
                lines.append("Вернуть бренд в выгрузку: %s" % _brand_text(brand))
            continue
        if key == "excluded_sizes":
            old_set, new_set = set(raw.get(key) or []), set(changes[key] or [])
            for rule in sorted(new_set - old_set):
                lines.append("Исключить размер из выгрузки: %s" % size_rule_text(rule))
            for rule in sorted(old_set - new_set):
                lines.append("Вернуть размер в выгрузку: %s" % size_rule_text(rule))
            continue
        if key in ("enabled", "pricing", "overrides", "price_overrides"):
            continue
        old, new = raw.get(key), changes[key]
        if key == "point_values":
            lines.append("Доли по точкам: %s → %s" % (_points(old), _points(new)))
        elif key == "pack4":
            if "pack4_rules" not in changes:      # an old draft with the bare switch
                lines.append("%s: %s → %s" % (SETTING_LABELS[key], "включено для всех" if old else "выключено", "включено для всех" if new else "выключено"))
        elif key == "pack4_rules":
            was = set(pack4_rules_of(raw))
            for rule in sorted(set(new) - was):
                lines.append("«За 4 шт.»: добавить для %s" % pack4_rule_text(rule))
            for rule in sorted(was - set(new)):
                lines.append("«За 4 шт.»: убрать для %s" % pack4_rule_text(rule))
        elif key == "point_mode":
            lines.append("%s: %s → %s" % (SETTING_LABELS[key], POINT_MODES.get(old, old), POINT_MODES.get(new, new)))
        else:
            lines.append("%s: %s → %s" % (SETTING_LABELS.get(key, key), "—" if old is None else old, "—" if new is None else new))
    return lines


def pair_funnel(parsed, ref, confirmed, brand_map, new_settings, pairs) -> list:
    """For newly enabled brand x season pairs: how many goods pass and why the rest do not."""
    if not pairs:
        return []
    ev = evaluate(parsed.offers, parsed.issues, new_settings, ref, confirmed, brand_map)
    wanted = {(brand, season) for brand, season in pairs}
    totals = defaultdict(int)
    oks = defaultdict(int)
    reasons = defaultdict(lambda: defaultdict(int))
    for gid, offer in parsed.offers.items():
        pair = (offer.brand_key, offer.season.value)
        if pair not in wanted:
            continue
        totals[pair] += 1
        if gid in ev.ok:
            if offer.stock < new_settings.min_stock:
                reasons[pair]["low_stock"] += 1
            else:
                oks[pair] += 1
        elif gid in ev.problems:
            reasons[pair][ev.problems[gid][0].split(":", 1)[0]] += 1
        elif gid in ev.excluded:
            reasons[pair][ev.excluded[gid]] += 1
        else:
            reasons[pair]["no_data"] += 1
    rows = []
    for pair in sorted(wanted):
        ranked = sorted(reasons[pair].items(), key=lambda item: -item[1])
        rows.append({
            "brand": pair[0], "season": pair[1], "label": "%s · %s" % (pair[0], SEASON_NAMES.get(pair[1], pair[1])),
            "total": totals[pair], "ok": oks[pair],
            "reasons": [(REASON_TEXT.get(code, code), count) for code, count in ranked],
        })
    return rows


def account_preview(conn, parsed, ref, old, new, today: date, now=None):
    """The change as every account sees it (own price delta, stock threshold, addresses, published state): the
    appearing and disappearing positions are united, counts and price changes come from the account losing most."""
    accounts = connections.list_feed_accounts(conn) or [{"id": 1}]
    previews = []
    for account in accounts:
        aid = account["id"]
        previews.append(preview_change(
            parsed, ref, store.load_confirmed_mappings(conn), store.load_published(conn, aid),
            store.load_suppressed(conn, aid), store.load_known_points(conn),
            connections.effective_settings(conn, aid, old), connections.effective_settings(conn, aid, new), today,
            store.load_brand_map(conn), now=now))
    lead = max(previews, key=lambda p: len(p.disappear))
    if len(previews) == 1:
        return lead
    changes = {}
    for p in previews:
        for gid, before, after in p.price_changes:
            changes.setdefault(gid, (gid, before, after))
    return dataclasses.replace(
        lead, appear=sorted(set().union(*[p.appear for p in previews])),
        disappear=sorted(set().union(*[p.disappear for p in previews])), price_changes=sorted(changes.values()))


def ads_gone(disappear, offers, old, new=None) -> int:
    """Ads (not positions) that leave the feed: a position with a «за 4 шт.» twin takes two ads off, and a twin also
    goes when the rules stop covering a position that stays."""
    gone = set(disappear)
    twins = sum(1 for gid in gone if gid in offers and old.pack4_for(offers[gid]) and offers[gid].stock >= 4)
    if new is not None:
        twins += sum(1 for gid, offer in offers.items() if gid not in gone and offer.stock >= 4
                     and old.pack4_for(offer) and not new.pack4_for(offer))
    return len(gone) + twins


def rollback_effect(conn, version_id: int, parsed, ref, today: date, now=None) -> dict:
    """How many ads a rollback to this version would take off, against the removal guard of the active settings."""
    active_raw = get_active(conn)[1]
    old = settings_from_dict(active_raw, enforce_pct_limit=False)
    new = settings_from_dict(get_version(conn, version_id), enforce_pct_limit=False)
    preview = account_preview(conn, parsed, ref, old, new, today, now)
    live_before = sum(preview.old_counts.get(a, 0) for a in ("publish", "keep", "update", "hold"))
    limit = max(old.guard_min, int(old.guard_fraction * live_before))
    gone = len(preview.disappear)
    return {"gone": gone, "gone_ads": ads_gone(preview.disappear, parsed.offers, old, new), "limit": limit,
            "over": gone > 0 and gone > limit}


def draft_review(conn, draft_id: int, parsed, ref, today: date, now=None) -> dict:
    draft = get_draft(conn, draft_id)
    active_id, raw = get_active(conn)
    merged = {**raw, **draft["changes"]}
    old = settings_from_dict(raw, enforce_pct_limit=False)
    new = settings_from_dict(merged, enforce_pct_limit=False)
    preview = account_preview(conn, parsed, ref, old, new, today, now)
    own_risky = own_price_risky(conn, raw, draft["changes"], parsed.offers, old)
    old_enabled = {tuple(item) for item in raw["enabled"]}
    new_enabled = {tuple(item) for item in merged["enabled"]}
    names = {gid: offer.name_orig or offer.name for gid, offer in parsed.offers.items()}
    price_rows = [
        {"id": gid, "name": names.get(gid, gid), "old": old_price, "new": new_price}
        for gid, old_price, new_price in preview.price_changes
    ]
    live_before = sum(preview.old_counts.get(a, 0) for a in ("publish", "keep", "update", "hold"))
    limit = max(old.guard_min, int(old.guard_fraction * live_before))
    gone_total = len(preview.disappear)
    brands = defaultdict(lambda: {"appear": 0, "disappear": 0})
    for gid in preview.appear:
        brands[parsed.offers[gid].brand_key if gid in parsed.offers else "?"]["appear"] += 1
    for gid in preview.disappear:
        brands[parsed.offers[gid].brand_key if gid in parsed.offers else "?"]["disappear"] += 1
    rev = hashlib.sha1(json.dumps(draft["changes"], ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    return {
        "rev": rev,
        "guard": {"over": gone_total > 0 and gone_total > limit, "limit": limit, "gone": gone_total,
                  "gone_ads": ads_gone(preview.disappear, parsed.offers, old, new)},
        "by_brand": [dict(brand=b, **v) for b, v in sorted(brands.items())],
        "draft": draft,
        "lines": own_price_lines(conn, raw, draft["changes"], parsed.offers, old, new)
                 + describe_changes(raw, draft["changes"], parsed.offers, new.min_stock),
        "funnel": pair_funnel(parsed, ref, store.load_confirmed_mappings(conn), store.load_brand_map(conn), new,
                              sorted(new_enabled - old_enabled)),
        "price_rows": price_rows[:20],
        "price_more": max(0, len(price_rows) - 20),
        "diff": {key: {"old": raw.get(key), "new": value} for key, value in draft["changes"].items()},
        "preview": preview,
        "own_risky": own_risky,
        "pct_strong": needs_strong_confirmation(raw, draft["changes"]),
        "needs_strong": needs_strong_confirmation(raw, draft["changes"]) or bool(own_risky),
        "stale": draft["base_version"] != active_id,
    }


def tochki_off_state(conn, supplier_id: int, unload_path: str):
    """None: not the 4tochki row, or its switch «в фид» is on (its positions are the panel's own set). Else "ready" (a snapshot with
    positions: the settings lists show them, for choosing brands and percents in advance) or "missing". Only the settings lists read this;
    the set that builds the feeds (`suppliers.load_all_parsed`, `panel.data.load_merged`) still ignores 4tochki with the switch off."""
    from .. import tochki_feed
    cfg = tochki_feed.config(conn)
    if cfg["feed"] or not cfg["supplier_id"] or supplier_id != cfg["supplier_id"]:
        return None
    loaded = tochki_feed.load_snapshot(unload_path, supplier_id)
    return "ready" if loaded is not None and loaded[0].offers else "missing"


def _supplier_parsed(conn, supplier_id: int, unload_path: str):
    """(positions, load time) for the settings pages of one supplier: the panel's set, or the raw parse of a secondary WebMIM unload the
    cycle holds as old (`data.stale_unload`), whose lists stay shown and settable (their load time marks them «только просмотр»); for the
    4tochki row with its switch off, the last snapshot (see `tochki_off_state`), so that brands can be chosen before the feed is on."""
    from .. import tochki_feed
    from ..settings_store import active_settings
    from .data import load_merged, stale_unload
    settings = active_settings(conn)
    cfg = tochki_feed.config(conn)
    if not cfg["feed"] and cfg["supplier_id"] == supplier_id:            # 4tochki before its switch: its lists come from the last snapshot
        loaded = tochki_feed.load_snapshot(unload_path, supplier_id)     # read once: the answer is this object or nothing
        if loaded is not None and loaded[0].offers:
            return loaded[0], None
    held = stale_unload(conn, settings, unload_path, supplier_id)
    if held is not None:
        return held
    return load_merged(conn, settings, unload_path), None


def _supplier_offers(conn, supplier_id: int, unload_path: str) -> list:
    parsed = _supplier_parsed(conn, supplier_id, unload_path)[0]
    return [o for o in parsed.offers.values() if supplier_of(o) == supplier_id]


def _stale_text(fetched) -> str:
    return "только просмотр: выгрузка устарела (скачана %s)" % _when(fetched.isoformat(timespec="seconds")) if fetched is not None else ""


def supplier_stale_text(conn, supplier_id: int, unload_path: str) -> str:
    """The mark of a supplier's settings lists made from an unload the cycle does not take (old), else ""."""
    return _stale_text(_supplier_parsed(conn, supplier_id, unload_path)[1])


def supplier_catalog(conn, supplier_id: int, unload_path: str):
    """Brands and radii this supplier actually has (keys carry the S<N>: prefix of additional suppliers)."""
    offers = _supplier_offers(conn, supplier_id, unload_path)
    return {o.brand_key for o in offers}, {o.diameter for o in offers}


def exclusions(conn, supplier_id: int, unload_path: str) -> dict:
    """Excluded brands and radii of one supplier for the settings page (draft changes marked)."""
    from .. import staging
    raw = get_active(conn)[1]
    staged = staging.summary(conn)
    changes = {} if staged["stale"] else staged["changes"]
    prefix = "S%d:" % supplier_id if supplier_id != 1 else ""
    mine = lambda key: key.startswith(prefix) if prefix else not key.startswith("S") or ":" not in key.split("|")[0]
    brands_now = set(raw.get("excluded_brands") or [])
    brands_new = set(changes.get("excluded_brands", raw.get("excluded_brands")) or [])
    sizes_now = set(raw.get("excluded_sizes") or [])
    sizes_new = set(changes.get("excluded_sizes", raw.get("excluded_sizes")) or [])

    def rows(old, new, text):
        return [{"key": key, "label": text(key), "state": "add" if key not in old else "remove" if key not in new else ""}
                for key in sorted(old | new) if mine(key)]

    parsed, fetched = _supplier_parsed(conn, supplier_id, unload_path)            # one decision for the lists and their mark
    offers = [o for o in parsed.offers.values() if supplier_of(o) == supplier_id]
    brands, diameters = {o.brand_key for o in offers}, {o.diameter for o in offers}
    strip = lambda key: key[len(prefix):]
    stats = {}
    for offer in offers:
        item = stats.setdefault(offer.brand_key, [0, 0])
        item[0] += 1
        item[1] += offer.stock
    brand_rows = [{"label": strip(key), "count": stats.get(key, [0, 0])[0], "stock": stats.get(key, [0, 0])[1], "excluded": key in brands_new,
                   "state": "add" if key in brands_new and key not in brands_now else "remove" if key in brands_now and key not in brands_new else ""}
                  for key in sorted((set(stats) | brands_now | brands_new)) if mine(key)]
    return {
        "brand_rows": brand_rows,
        "excluded_brands": rows(brands_now, brands_new, strip),
        "excluded_sizes": rows(sizes_now, sizes_new, lambda k: "%s · R%s" % (
            "Все бренды" if strip(k).startswith("*|") else strip(k).rpartition("|")[0], k.rpartition("|")[2])),
        "brand_choices": sorted(strip(b) for b in brands - brands_new if mine(b)),
        "brand_all": sorted(strip(b) for b in brands if mine(b)),
        "diameter_choices": sorted(diameters),
        "stale": _stale_text(fetched),
    }


def pricing_table(conn, supplier_id: int, unload_path: str, ref=None) -> list:
    """Brands of one supplier with the percents of every season they have (draft values marked), for the settings page."""
    from .. import staging
    raw = get_active(conn)[1]
    staged = staging.summary(conn)
    changes = {} if staged["stale"] else staged["changes"]
    pricing_now, pricing = raw.get("pricing") or {}, {**(raw.get("pricing") or {}), **(changes.get("pricing") or {})}
    enabled_now = {tuple(i) for i in raw.get("enabled") or []}
    enabled = {tuple(i) for i in changes["enabled"]} if "enabled" in changes else enabled_now
    parsed = _supplier_parsed(conn, supplier_id, unload_path)[0]
    prefix = "S%d:" % supplier_id if supplier_id != 1 else ""
    counts = defaultdict(lambda: defaultdict(int))
    for offer in parsed.offers.values():
        if supplier_of(offer) == supplier_id and not excluded_by_owner(raw, offer):
            counts[offer.brand_key][offer.season.value] += 1
    pairs = {}
    if ref is not None:
        for row in brands_overview(conn, raw, parsed, ref):
            pairs[row["brand_key"]] = (row["avito_brand"] is not None, sum(1 for m in row["models"] if not m["avito_model"]))
    mine = (lambda b: b.startswith(prefix)) if prefix else (lambda b: not re.match(r"S\d+:", b))
    for brand, _season in enabled:
        if mine(brand) and brand not in counts and brand not in (raw.get("excluded_brands") or ()):
            counts[brand]            # a switched-on brand without goods today: it must stay switchable off
    rows = []
    for key in sorted(counts, key=lambda k: (-sum(counts[k].values()), k)):
        seasons = {}
        for season in SEASONS:
            rule, old = pricing.get("%s|%s" % (key, season)), pricing_now.get("%s|%s" % (key, season))
            seasons[season] = {
                "count": counts[key].get(season, 0),
                "small": rule["pct_small"] if rule else "", "large": rule["pct_large"] if rule else "",
                "enabled": (key, season) in enabled,
                "enabled_pending": ((key, season) in enabled) != ((key, season) in enabled_now),
                "pending": rule != old,
            }
        known, unmatched = pairs.get(key, (True, 0))
        rows.append({"brand": key, "label": key[len(prefix):], "seasons": seasons, "brand_known": known, "unmatched": unmatched})
    return rows


def warn_forced(conn, brand: str, unload_path: str) -> str:
    """A manual "on" outlives a brand switch-off: tell the owner how many positions stay."""
    from .. import staging
    from ..overrides import override_key
    raw = get_active(conn)[1]
    staged = staging.summary(conn)
    changes = {} if staged["stale"] else staged["changes"]
    overrides = {**(raw.get("overrides") or {}), **(changes.get("overrides") or {})}
    found = re.match(r"S(\d+):", brand)                                  # the brand key of an additional supplier carries its number
    offers = _supplier_offers(conn, int(found.group(1)) if found else 1, unload_path)
    kept = [o.goods_id for o in offers if o.brand_key == brand and overrides.get(override_key(o.goods_id)) == "on"]
    if kept:
        return "Позиций, добавленных вручную, у бренда %s: %d. Они останутся в фиде: снимите их отдельно." % (brand, len(kept))
    return ""


def _feed_phones(feed_root, token: str):
    """The phones of an account's ads in its published feed and in its draft: {"published": set or None, "next": set or None}."""
    import xml.etree.ElementTree as ET
    from pathlib import Path
    out = {}
    for key, folder in (("published", ""), ("next", "next")):
        path = Path(feed_root) / token / folder / "avito_feed.xml" if folder else Path(feed_root) / token / "avito_feed.xml"
        try:
            ads = ET.parse(str(path)).getroot().findall("Ad")
        except (OSError, ET.ParseError):
            out[key] = None
            continue
        out[key] = {(a.findtext("ContactPhone") or "").strip() for a in ads}
    return out


def phone_reconciliation(conn, feed_root) -> list:
    """One row per Avito account for the owner to check by eye: the phone of the cabinet (as Avito gave it at the connection check), the phone(s)
    that go out in its ads, and what is wrong. Another account with the same number is a warning, not an error: Avito itself says so."""
    accounts = connections.list_accounts(conn)
    own = {a["id"]: connections.get_avito_identity(conn, a["id"])["phone"] for a in accounts}
    places = {}                                       # account -> {address id and normalized address text: shown name}
    for a in accounts:
        keys = {}
        for r in connections.list_addresses(conn, a["id"]):
            if r["enabled"]:
                keys["id:" + r["address_id"]] = r["name"] or r["address_id"]
                if r["address_text"]:
                    keys["text:" + " ".join(r["address_text"].lower().split())] = r["address_text"]
        places[a["id"]] = keys
    rows = []
    for account in accounts:
        phone = own[account["id"]]
        sent = _feed_phones(feed_root, account["feed_token"]) if feed_root else {"published": None, "next": None}
        shown = sent["next"] if sent["next"] is not None else sent["published"]
        goes = sorted({connections.normalize_phone(p) or p for p in shown}) if shown else []
        from_file = connections.get_avito_identity(conn, account["id"])["phone_source"] == "file"
        same = [connections.display_name(conn, o["id"]) for o in accounts if o["id"] != account["id"] and phone and own[o["id"]] == phone]
        keys = connections.has_avito_keys(conn, account["id"])
        if from_file:
            level, text = "ok", "телефон из файла, по решению владельца (номер кабинета в объявления не подставляется)"
        elif not phone:
            level, text = ("bad", "номер кабинета не определён: нажмите «Проверить подключение»") if keys else ("neutral", "ключи Авито не заданы")
        elif shown is None:
            level, text = "neutral", "фид не опубликован"
        elif goes == [phone]:
            level, text = "ok", "совпадает"
        else:
            level, text = "bad", "в объявлениях другой номер: при следующем цикле будет заменён номером кабинета"
        shared_places = sorted({other_name for o in accounts if o["id"] != account["id"] for k, other_name in places[o["id"]].items() if k in places[account["id"]]})
        rows.append({"shared_places": shared_places, "id": account["id"], "label": connections.display_name(conn, account["id"]), "name": account["name"],
                     "phone": connections.format_phone(phone) if phone else "", "goes": [connections.format_phone(g) for g in goes],
                     "level": level, "text": text, "same": same, "from_file": from_file, "mirror": account["feed_mode"] == "mirror"})
    return rows


def mirror_models_to_check(conn, ref, parsed) -> list:
    """The models of the mirrored feeds that Avito's reference does not have: one row per (account, brand, model) with every ad of it,
    the full supplier name of each (WebMIM's original name) and a suggestion. The model of a mirrored ad is the owner's file as it is:
    nothing else checks it before Avito does."""
    from collections import Counter
    from .. import mirror_feed
    from ..catalog import classify_model, normalize_model
    offers = getattr(parsed, "offers", {}) or {}
    rows = []
    normalized: dict = {}                             # brand -> [(model, normalized)], once per brand
    for account in connections.list_accounts(conn):
        if account["feed_mode"] != "mirror":
            continue
        label = connections.display_name(conn, account["id"])
        for pair in mirror_feed.unmatched_models(conn, account["id"], ref):
            brand = pair["avito_brand"]
            ads = []
            for ad_id in pair["ad_ids"]:
                offer = offers.get(mirror_feed.supplier_code(ad_id))
                name = " ".join(((offer.name_orig or offer.name) if offer is not None else "").split())
                ads.append({"id": ad_id, "name": name, "stock": offer.stock if offer is not None else None})
            models = ref.get(brand, frozenset())
            if brand not in normalized:
                normalized[brand] = [(m, normalize_model(m)) for m in sorted(models)]
            by_name = Counter()                       # the reference models that the full name of an ad contains: the most specific one wins
            for ad in ads:
                text = normalize_model(ad["name"])
                inside = [(len(n), m) for m, n in normalized[brand] if len(n) >= 4 and n in text]
                if inside:
                    by_name[max(inside)[1]] += 1
            named = [m for m, _n in sorted(by_name.items(), key=lambda kv: (-kv[1], kv[0]))]
            _tier, found = classify_model(brand, pair["model"], ref)
            options = named + [m for m in found if m not in named]
            rows.append({"account": label, "brand": brand, "model": pair["model"], "ads": ads, "hint": options[0] if options else "",
                         "from_name": bool(named), "options": options[:3],
                         "list_id": "dl-mr-" + hashlib.md5(brand.encode("utf-8")).hexdigest()[:8]})
    return rows


def unmatched_models(conn, parsed, ref):
    """(models, goods) of every brand that has no pair in the Avito reference yet: the one definition of «без пары»."""
    models = goods = 0
    for row in brands_overview(conn, get_active(conn)[1], parsed, ref, proposals=False):
        if row["avito_brand"] is None:
            continue
        for m in row["models"]:
            if not m["avito_model"]:
                models += 1
                goods += m["goods"]
    return models, goods


SEASON_LABELS = {"summer": "Лето", "winter": "Зима", "all": "Всесезонные"}


def share_snapshot(conn, settings, parsed, ref, goods_ids) -> tuple:
    """(items, skipped) for a client link. An item holds only what a client may see (plus `image`, the supplier's photo address,
    used once to copy the photo): never the supplier's price, the stock or the supplier's code. Positions without a selling price are skipped."""
    ev = evaluation(conn, settings, parsed, ref)
    items, skipped = [], []
    for gid in dict.fromkeys(goods_ids):
        offer = parsed.offers.get(gid)
        if offer is None:
            skipped.append((gid, "позиции больше нет в выгрузке"))
            continue
        cand = ev.ok.get(gid)
        if cand is None:
            cand, _note = _unbuilt_candidate(conn, settings, ref, offer)
        price = getattr(cand, "price", 0) or 0
        if price <= 0:
            skipped.append((offer.name_orig or offer.name, "не рассчитана цена продажи"))
            continue
        items.append({
            "name": offer.name_orig or offer.name, "brand": offer.brand_raw, "model": offer.model_raw,
            "size": "%d/%d R%d" % (offer.width, offer.height, offer.diameter),
            "season": SEASON_LABELS.get(offer.season.value, ""), "studded": bool(offer.studded),
            "load": offer.load, "speed": offer.speed, "runflat": bool(offer.runflat), "country": offer.country,
            "year": offer.mnf_year, "price": int(round(price)), "image": offer.image})
    return items, skipped
