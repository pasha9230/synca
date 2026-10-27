from __future__ import annotations

import copy
import hashlib
import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Tuple

from . import connections, feed_check, mirror_feed, photo_library, store
from .decide import SETTINGS_DRIVEN
from .models import Action, Settings
from .publishing import expected_for
from .settings_store import active_settings
from .sync import run_cycle
from .webmim import parse_unload


@dataclass
class AccountResult:
    account_id: int
    name: str
    ok: bool
    error: str = ""
    counts: Dict[str, int] = field(default_factory=dict)
    feed_count: int = 0
    guard_tripped: bool = False
    unmatched: tuple = ()          # "brand / model" of the models that wait for a pair
    new_waiting: int = 0           # positions that left the unload in this cycle and now wait for the morning refill
    titles_held: int = 0           # tires the supplier renamed at once: their titles on Avito stay (guard)
    guarded: tuple = ()            # (code, reason) of the positions the guard against mass removal keeps in the feed
    removed_lately: int = 0        # positions the light stock passes took off within the last hour (counted by the guard)


def _photo_version(conn, account_id: int) -> str:
    """A short name of the account's photo look («» when it has none): it goes into the photo links, so a changed look is a new address."""
    from . import photo_style
    style = connections.get_photo_style(conn, account_id)
    return photo_style.version(style) if style else ""


def feed_tire_type(offer) -> str:
    from .feed import _tire_type
    return _tire_type(offer.season, bool(getattr(offer, "studded", False)))


TEMPLATE_PATH = Path(__file__).resolve().parent / "data" / "avito-templates" / "legkovye-shiny-2026-10-14.xml"


def _ids_of(path) -> set:
    try:
        return {a.findtext("Id") or "" for a in ET.parse(str(path)).getroot().findall("Ad")}
    except (OSError, ET.ParseError):
        return set()


def _bound_codes(bound_ids: dict, offers: dict, min_stock: int = 0, published=()) -> dict:
    """Id -> the code behind it, for the Ids that do not carry their code. When one Id is bound to several codes (its own position and
    an inherited one: one tire under two codes), the code is picked like the build picks the kept one (same_tire.dedupe): stock at the
    threshold, the published one first, then more stock. With none at the threshold: a live inherited one, else its own, never a gone one
    while another is bound."""
    published = set(published)
    stock = lambda c: int(getattr(offers[c], "stock", 0) or 0)
    codes_of: dict = {}
    for code, bound in bound_ids.items():
        codes_of.setdefault(bound, []).append(code)
    out = {}
    for bound, codes in codes_of.items():
        own = mirror_feed.supplier_code(bound)
        inherited = sorted(c for c in codes if c != own)
        eligible = [c for c in codes if c in offers and stock(c) >= min_stock]
        live = [c for c in inherited if c in offers]
        if len(codes) > 1 and eligible:
            code = sorted(eligible, key=lambda c: (c not in published, -stock(c), c))[0]
        else:
            code = live[0] if live else own if own in codes else inherited[0]
        if code != own:
            out[bound] = code
    return out


def gate(conn, account, mode, eff, parsed, ref, feed_root, today=None, held=(), source: str = "next", id_codes=None) -> list:
    """The findings of the check of this account's draft feed (`next/avito_feed.xml`). Empty list when the draft cannot be read: the build
    itself reports that. The rules are in feed_check_rules.json (docs/feed-check-rules.md)."""
    folder = Path(feed_root) / account["feed_token"]
    try:
        path = folder / "avito_feed.xml" if source == "published" else folder / "next" / "avito_feed.xml"
        ads = ET.parse(str(path)).getroot().findall("Ad")
    except (OSError, ET.ParseError):
        return []
    identity = connections.get_avito_identity(conn, account["id"])
    others = set()
    for other in connections.list_feed_accounts(conn):
        if other["id"] != account["id"]:
            others |= _ids_of(Path(feed_root) / other["feed_token"] / "avito_feed.xml")
            others |= _ids_of(Path(feed_root) / other["feed_token"] / "next" / "avito_feed.xml")
    for any_account in connections.list_feed_accounts(conn):                  # Ids shared on purpose (until a date) are not a violation, for either account
        others -= mirror_feed.shared_ids(conn, any_account["id"])
    template, template_problem = feed_check.read_template(TEMPLATE_PATH)   # missing or broken: a critical finding (`template_missing`)
    suffix, prefix = connections.feed_style(conn, account["id"])
    foreign = _foreign_addresses(conn, account) if mode == "generated" else frozenset()
    own = _own_photos(conn, account)
    id_code = dict(_bound_codes(mirror_feed.adopted_maps(conn, account["id"])[0], getattr(parsed, "offers", {}) or {}, eff.min_stock,
                                store.load_published(conn, account["id"])),
                   **(id_codes or {}))       # Ids that do not carry their code (alias scheme, inherited Ids): from the account's bindings, so `feed-check` reads them too
    tochki = _tochki_context(conn, account, ads, id_code, getattr(parsed, "offers", {}) or {}, folder, source)
    ctx = feed_check.Context(
        mode=mode, template_tags=template or frozenset(t for ad in ads for t in (c.tag for c in ad)), reference=ref or {},
        stocks={code: int(getattr(o, "stock", 0) or 0) for code, o in (getattr(parsed, "offers", {}) or {}).items()},
        min_stock=eff.min_stock, phone=identity["phone"] if identity["phone_source"] == "cabinet" else "",
        other_ids=frozenset(others), pack4=bool(getattr(eff, "pack4", False)), photo_prefix=prefix if mode == "generated" and not mirror_feed.adopted_maps(conn, account["id"])[1] else "",
        previous_count=len(_ids_of(folder / "avito_feed.xml")), held=frozenset(str(h) for h in held),
        id_code=id_code,
        owner_phone="" if account["id"] == connections.PRIMARY_ACCOUNT else feed_check._phone(active_settings(conn).contact_phone),
        foreign_addresses=foreign, allowed_photos=own.allowed if own is not None else frozenset(),
        known_stocks=dict(getattr(parsed, "stocks", {}) or {}), template_problem=template_problem, **tochki)
    return feed_check.check_feed(ads, ctx)


def _tochki_context(conn, account, ads, id_code, offers, folder, source) -> dict:
    """What the 4tochki rules of the check need: nothing when 4tochki is not set up or the feed has none of it (the check is as before)."""
    from . import tire_match, tochki_feed
    sid = tochki_feed.config(conn)["supplier_id"]
    if not sid:
        return {}
    prefix = "s%d-" % sid
    codes = {(a.findtext("Id") or ""): id_code.get(a.findtext("Id") or "") or feed_check._code(a.findtext("Id") or "") for a in ads}
    if not any(c.startswith(prefix) or i.startswith(prefix) for i, c in codes.items()):
        return {"tochki_prefix": prefix, "tochki_trial": tochki_feed.trial(conn, account["id"]) is not None}
    synonyms = store.load_model_synonyms(conn)
    tire_ids = {}
    for code in set(codes.values()):
        ident = tire_match.identity(offers[code], synonyms) if code in offers else None
        if ident is not None:
            tire_ids[code] = tire_match.identity_text(ident)
    live = {}
    if source != "published":
        try:
            live = {a.findtext("Id"): (a.findtext("Model") or "") for a in ET.parse(str(folder / "avito_feed.xml")).getroot().findall("Ad")
                    if a.findtext("Id")}
        except (OSError, ET.ParseError):
            live = {}
    return {"tochki_prefix": prefix, "tochki_trial": tochki_feed.trial(conn, account["id"]) is not None, "tire_ids": tire_ids,
            "live_models": live}


def _inputs_key(account_id: int) -> str:
    return "feed_check_inputs_%d" % account_id


def _sha_of(path) -> str:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return ""


def save_check_inputs(conn, account_id: int, feed_path, held, id_codes) -> None:
    """Remember what the cycle gave the check of this draft (the held positions, the Ids that do not carry their code), with the draft's
    fingerprint: `feed-check` then judges the same file by the same rules as the cycle did (check_inputs)."""
    store.set_state(conn, _inputs_key(account_id), json.dumps({
        "sha": _sha_of(feed_path), "held": sorted({str(h) for h in held}),
        "id_codes": dict(id_codes or {})}, ensure_ascii=False))     # all of them: an Id read as its own code overrides a binding to another


def check_inputs(conn, account, feed_root, source: str = "next") -> Tuple[FrozenSet[str], Dict[str, str]]:
    """(held, id_codes) for the check of an account's draft or published feed outside the cycle (the unload is the last cycle's). The file
    the last cycle built: what that cycle gave its check. Another file (an earlier draft is published) or no record yet: also the positions
    the publication recorded as held with that file."""
    folder = Path(feed_root) / account["feed_token"]
    path = folder / "avito_feed.xml" if source == "published" else folder / "next" / "avito_feed.xml"
    try:
        saved = json.loads(store.get_state(conn, _inputs_key(account["id"])) or "{}")
        held, id_codes, sha = frozenset(saved.get("held") or ()), dict(saved.get("id_codes") or {}), saved.get("sha") or ""
    except (ValueError, TypeError, AttributeError):
        held, id_codes, sha = frozenset(), {}, ""
    if not sha or sha != _sha_of(path):
        held |= frozenset(code for code, item in store.load_published(conn, account["id"]).items() if item.first_hold)
    return held, id_codes


def _own_photos(conn, account):
    """The account's own library links (None: the switch is off or the account has no photo address), at the address of its photo links."""
    return photo_library.own_photos_for(conn, account["id"], connections.photo_address(conn, account["id"]))


def _foreign_addresses(conn, account) -> frozenset:
    """The address ids this account must not carry: those of the other accounts and of the general settings (the primary account's), minus its own.
    The primary account is not judged (the general settings are its own)."""
    if account["id"] == connections.PRIMARY_ACCOUNT:
        return frozenset()
    own = {row["address_id"] for row in connections.list_addresses(conn, account["id"])}
    foreign = set(active_settings(conn).point_values)
    for other in connections.list_feed_accounts(conn):
        if other["id"] != account["id"]:
            foreign |= {row["address_id"] for row in connections.list_addresses(conn, other["id"])}
    return frozenset(str(a) for a in foreign - own if a)


def _verdict(result, findings):
    """The result of an account after the check: critical findings make it a failure (the published feed stays), warnings go into the note."""
    bad = feed_check.critical(findings)
    if bad:
        result.ok, result.error = False, "проверка фида не пройдена, публикация остановлена: " + feed_check.summarize(bad)
    elif findings:
        result.error = (result.error + "; " if result.error else "") + "предупреждения проверки: " + feed_check.summarize(findings)
    return result


def _new_positions(conn, account, eff, xml_bytes, ref, today, feed_root, parsed, input_sha, now, expected) -> list:
    """Ads for the supplier's positions that the account's file does not have: the usual cycle (rules, points, prices, state) over only those
    positions, with the text template of the account. The file's own ads are never touched by it."""
    codes = mirror_feed.stored_codes(conn, account["id"])
    rest = copy.copy(parsed)
    left_out = set(connections.get_excluded_types(conn, account["id"]))
    rest.offers = {k: v for k, v in parsed.offers.items()
                   if k not in codes and feed_tire_type(v) not in left_out}
    out_dir = Path(feed_root) / account["feed_token"] / "new"
    suffix, prefix = connections.feed_style(conn, account["id"])
    run_cycle(xml_bytes, eff, ref, conn, today, out_dir=out_dir, description_template=account["description_template"], id_suffix=suffix, image_prefix=prefix,
              account_id=account["id"], record_seen=False, expected_removals=expected, parsed=rest, input_sha=input_sha, now=now,
              check_size=False, own_photos=_own_photos(conn, account))
    ads = list(ET.parse(out_dir / "avito_feed.xml").getroot().findall("Ad"))
    mirror_feed.match_shape(conn, account["id"], ads)
    return ads


def run_for_accounts(
    conn,
    xml_bytes: Optional[bytes],
    ref: Dict[str, FrozenSet[str]],
    today: date,
    feed_root,
    settings: Optional[Settings] = None,
    parsed=None,
    input_sha: Optional[str] = None,
    now=None,
) -> List[AccountResult]:
    """Dry-run cycle for every Avito account; each account writes its own private feed folder.

    Nothing is sent to Avito. One account failing never stops the others.
    """
    if not connections.list_accounts(conn):
        raise ValueError("Нет аккаунтов Авито: добавьте аккаунт на странице «Подключения».")
    accounts = connections.list_feed_accounts(conn)                # an account that only reads messages has no feed
    if not accounts:
        return []
    settings = settings or active_settings(conn)
    if parsed is None and xml_bytes is not None:                      # the check of the feeds needs the stock of every position
        try:
            parsed = parse_unload(xml_bytes, settings.territory_rn)
        except Exception:                                            # a broken file fails each account on its own, as before
            parsed = None
    results: List[AccountResult] = []
    expected = expected_for(conn, settings)
    for index, account in enumerate(accounts):
        out_dir = Path(feed_root) / account["feed_token"] / "next"
        identity = connections.get_avito_identity(conn, account["id"])
        if connections.has_avito_keys(conn, account["id"]) and identity["phone_source"] == "cabinet" and not identity["phone"]:
            results.append(AccountResult(                                            # the owner's general number is never put into another cabinet's ads
                account["id"], account["name"], False,
                "номер кабинета Авито не определён: откройте «Подключения → Авито» и нажмите «Проверить подключение»; без него фид аккаунта не собирается"))
            continue
        if account["feed_mode"] == "mirror":                                      # a feed kept by hand elsewhere: carried over as it is
            try:
                eff = connections.effective_settings(conn, account["id"], settings)
                if parsed is None and xml_bytes is not None:
                    parsed = parse_unload(xml_bytes, settings.territory_rn)
                skip = mirror_feed.unavailable(conn, account["id"], getattr(parsed, "offers", {}) or {}, eff.min_stock) if parsed is not None else {}
                skip = dict(skip, **mirror_feed.excluded_by_type(conn, account["id"], connections.get_excluded_types(conn, account["id"])))
                prices = mirror_feed.prices_for(conn, account["id"], getattr(parsed, "offers", {}) or {}, eff) if parsed is not None else {}
                runflat = mirror_feed.runflat_for(conn, account["id"], getattr(parsed, "offers", {}) or {}) if parsed is not None else {}
                extra: list = []
                note = ""
                try:
                    models = mirror_feed.models_for(conn, account["id"], ref) if ref else {}
                except Exception as err:                                     # the file's ads must reach Avito as they are if the model check broke
                    models = {}
                    note = "модели не проверены (%s: %s)" % (type(err).__name__, err)
                if parsed is not None:
                    try:
                        extra = _new_positions(conn, account, eff, xml_bytes or b"", ref, today, feed_root, parsed, input_sha, now, expected)
                        mirror_feed.apply_reissued_own(conn, account["id"], extra)
                    except Exception as err:                               # the file's ads must reach Avito even if the new positions did not build
                        note = (note + "; " if note else "") + "новые позиции не добавлены (%s: %s)" % (type(err).__name__, err)
                repeated = mirror_feed.duplicate_positions(conn, account["id"])
                if repeated:
                    note = (note + "; " if note else "") + "в файле повторяются позиции (Авито снимает повторы): %s" % ", ".join("%s/%s" % p for p in repeated[:5])
                count = mirror_feed.write_candidate(conn, account, feed_root, today, settings, skip=skip, prices=prices, extra_ads=extra, runflat=runflat, models=models,
                                                  contact_phone=identity["phone"] if identity["phone_source"] == "cabinet" else "",
                                                  own_photos=_own_photos(conn, account), offers=getattr(parsed, "offers", {}) or {})
                if extra and store.has_pending(conn, account["id"]):
                    store.rebind_pending(conn, account["id"], (Path(feed_root) / account["feed_token"] / "next" / "avito_feed.xml").read_bytes())
                results.append(_verdict(AccountResult(account["id"], account["name"], True, note, {}, count),
                                        gate(conn, account, "mirror", eff, parsed, ref, feed_root, today)))
            except Exception as err:
                results.append(AccountResult(account["id"], account["name"], False, "%s: %s" % (type(err).__name__, err)))
            continue
        try:
            suffix, prefix = connections.feed_style(conn, account["id"])
            id_map, image_map = mirror_feed.adopted_maps(conn, account["id"])
            left_out = set(connections.get_excluded_types(conn, account["id"]))
            account_parsed = parsed
            if left_out and parsed is not None:                      # tire types the owner left out of this account: as if the supplier did not have them
                account_parsed = copy.copy(parsed)
                account_parsed.offers = {k: v for k, v in parsed.offers.items() if feed_tire_type(v) not in left_out}
            lately = pass_removals_lately(conn, account["id"])
            report = run_cycle(
                xml_bytes, connections.effective_settings(conn, account["id"], settings), ref, conn, today, out_dir=out_dir,
                description_template=account["description_template"], id_suffix=suffix, image_prefix=prefix,
                id_map=id_map, image_map=image_map, contact_name=connections.get_contact_name(conn, account["id"]),
                title_template=connections.get_title_template(conn, account["id"]) or None,
                photo_version=_photo_version(conn, account["id"]), own_photos=_own_photos(conn, account),
                account_id=account["id"], record_seen=(index == 0), expected_removals=expected,
                parsed=account_parsed, input_sha=input_sha, now=now,
                removed_before=lately)                                 # an hour holds at most one threshold of removals, passes included
            result = AccountResult(account["id"], account["name"], True, "", report.counts, report.feed_count,
                                   report.guard_tripped,
                                   tuple(sorted({"%s / %s" % (u["brand"], u["model"]) for u in report.unmatched})),
                                   sum(1 for d in report.decisions if d.action is Action.HOLD and d.reason.startswith("cart_wait:")
                                       and now is not None and d.first_hold == now), report.titles_held,
                                   tuple((d.goods_id, d.reason[len("guard:"):]) for d in report.decisions
                                         if d.action is Action.HOLD and d.reason.startswith("guard:")), lately)
            held = [d.goods_id for d in report.decisions if d.action is Action.HOLD]
            try:
                save_check_inputs(conn, account["id"], out_dir / "avito_feed.xml", held, report.id_codes)
            except Exception as err:                                 # only `feed-check` reads it: it never stops the account's publication
                result.error = "запись для feed-check не сохранена (%s: %s)" % (type(err).__name__, err)
            results.append(_verdict(result, gate(conn, account, "generated", connections.effective_settings(conn, account["id"], settings),
                                                 parsed, ref, feed_root, today, held=held, id_codes=report.id_codes)))
        except Exception as err:
            results.append(AccountResult(account["id"], account["name"], False, "%s: %s" % (type(err).__name__, err)))
    return results


STOCK_REASONS = ("low_stock", "absent_from_supplier")
GUARD_WINDOW_MINUTES = 60          # the guard of the light pass looks at an hour, as when only the hourly cycle took ads off


def _pass_key(account_id: int) -> str:
    return "stock_pass_removed_%d" % account_id


def _pass_marks(conn, account_id: int) -> list:
    """[[UTC time, count]] of the light passes' removals within the last hour."""
    since = (datetime.utcnow() - timedelta(minutes=GUARD_WINDOW_MINUTES)).isoformat(timespec="seconds")
    try:
        marks = json.loads(store.get_state(conn, _pass_key(account_id)) or "[]")
        return [[at, int(n)] for at, n in marks if str(at) >= since]
    except (ValueError, TypeError):
        return []


def remember_pass_removals(conn, account_id: int, count: int) -> None:
    """The light pass took `count` positions off the published feed: the hourly cycle's guard counts them for an hour."""
    marks = _pass_marks(conn, account_id) + [[datetime.utcnow().isoformat(timespec="seconds"), int(count)]]
    store.set_state(conn, _pass_key(account_id), json.dumps(marks))


def pass_removals_lately(conn, account_id: int) -> int:
    """Positions the light passes took off this account within the last hour (none without passes: the hourly cycle decides as before)."""
    return sum(n for _at, n in _pass_marks(conn, account_id))


def _removed_lately(conn, account_id: int) -> int:
    """Ads of the account taken off in the last hour (by the hourly cycle and the light passes), for any reason the guard counts."""
    since = (datetime.utcnow() + timedelta(hours=3, minutes=-GUARD_WINDOW_MINUTES)).isoformat(timespec="seconds")   # ad_event is in Moscow time
    marks = ",".join("?" * len(SETTINGS_DRIVEN))
    return conn.execute("SELECT COUNT(*) FROM ad_event WHERE account_id = ? AND event = 'removed' AND at >= ? AND reason NOT IN (%s)" % marks,
                        (account_id, since) + tuple(sorted(SETTINGS_DRIVEN))).fetchone()[0]


@dataclass
class StockResult:
    account_id: int
    name: str
    ok: bool
    error: str = ""
    removed: tuple = ()            # codes taken off the feed by this pass
    left: tuple = ()               # codes the pass would take off but found no ad of in the published file: the hourly cycle does it
    guard_tripped: bool = False
    guarded: tuple = ()            # (code, reason), as AccountResult.guarded
    feed_count: int = 0
    broken: bool = False           # the pass itself failed (not the check of its draft, not the publication)


def stock_pass(conn, parsed, ref, today: date, feed_root, settings: Optional[Settings] = None, now=None) -> List[StockResult]:
    """The light pass «stock only» (owner, 2026-10-24, «вариант Б»): between the hourly cycles a position whose stock fell below the threshold,
    or that left the unload, leaves the feed at once. The decisions are the cycle's own (sync.plan_decisions: threshold, own point, guard
    against mass removal, types left out), but only the removals for stock are applied, and the draft is the account's published file
    without the ads of those positions: no other ad is built again (prices, titles, photos, Ids stay), nothing new comes in. The draft is
    checked like the cycle's (gate); the caller publishes it. Only accounts that build their own feed and publish it; nothing to take off:
    nothing is written."""
    from .feed import assemble_feed, write_atomic
    from .sync import _pending_item, plan_decisions
    settings = settings or active_settings(conn)
    results: List[StockResult] = []
    for account in connections.list_feed_accounts(conn):
        if account["feed_mode"] == "mirror" or not account["live"]:
            continue
        folder = Path(feed_root) / account["feed_token"]
        result = StockResult(account["id"], account["name"], True)
        try:
            identity = connections.get_avito_identity(conn, account["id"])
            if connections.has_avito_keys(conn, account["id"]) and identity["phone_source"] == "cabinet" and not identity["phone"]:
                continue                                                   # the hourly cycle reports it; its feed is not built
            try:
                ads = ET.parse(str(folder / "avito_feed.xml")).getroot().findall("Ad")
            except (OSError, ET.ParseError):
                continue                                                   # nothing published yet (or broken): the hourly cycle builds it
            eff = connections.effective_settings(conn, account["id"], settings)
            left_out = set(connections.get_excluded_types(conn, account["id"]))
            account_parsed = parsed
            if left_out:                                                   # as the cycle sees the account (run_for_accounts)
                account_parsed = copy.copy(parsed)
                account_parsed.offers = {k: v for k, v in parsed.offers.items() if feed_tire_type(v) not in left_out}
            # the guard over the last hour: every removal of it (the passes' and the hourly cycle's) counts with this pass's own
            published, ev, ds = plan_decisions(conn, account_parsed, ref, eff, account["id"], today, now=now,
                                                removed_before=_removed_lately(conn, account["id"]))
            if ds.guard_tripped:
                result.guard_tripped = True
                result.guarded = tuple((d.goods_id, d.reason[len("guard:"):]) for d in ds.decisions
                                       if d.action is Action.HOLD and d.reason.startswith("guard:"))
                if any(reason in STOCK_REASONS for _code, reason in result.guarded):   # nothing for stock to hold: nothing to say
                    results.append(result)
                continue
            going = {d.goods_id: d for d in ds.decisions if d.action is Action.REMOVE and d.reason in STOCK_REASONS
                     and not (d.goods_id in parsed.offers and d.goods_id not in account_parsed.offers)}   # a type left out: a setting, the hourly cycle's
            if not going:
                continue
            held_before, id_codes = check_inputs(conn, account, feed_root, "published")
            id_code = dict(_bound_codes(mirror_feed.adopted_maps(conn, account["id"])[0], parsed.offers, eff.min_stock, published), **id_codes)
            code_of = lambda ad_id: id_code.get(ad_id) or feed_check._code(ad_id)
            found: Dict[str, List[str]] = {}
            for ad in ads:
                code = code_of(ad.findtext("Id") or "")
                if code in going:
                    found.setdefault(code, []).append(ad.findtext("Id") or "")
            for code in list(found):
                # another lot of the same tire takes this ad's Id in the same step (same_tire): the hourly cycle takes it off and hands the Id
                # over at once; taken off alone, the next lot would come out under a new Id and Avito blocks it as a repeat
                if set(found[code]) & {i for c, i in ev.inherit.items() if c != code}:
                    del found[code]
            result.left = tuple(sorted(set(going) - set(found)))
            if not found:
                results.append(result)
                continue
            kept = [ad for ad in ads if code_of(ad.findtext("Id") or "") not in found]
            pending = []
            for code in sorted(found):
                item = _pending_item(going[code], None, {}, {})
                singles = [i for i in found[code] if not feed_check.PACK.search(i.split("_")[0])]
                item["ad_id"] = singles[0] if singles else None             # the Id of the single ad: remembered for the same tire later
                pending.append(item)
            feed_bytes = assemble_feed(kept)
            draft = folder / "next" / "avito_feed.xml"
            draft.parent.mkdir(parents=True, exist_ok=True)
            write_atomic(draft, feed_bytes)
            store.save_pending(conn, account["id"], feed_bytes, pending)
            # what stays in the file as the last cycle left it (held, or to be taken off by the hourly cycle for another reason) is not judged by stock
            held = set(held_before) | {d.goods_id for d in ds.decisions
                                       if d.action is Action.HOLD or (d.action is Action.REMOVE and d.goods_id not in found)}
            save_check_inputs(conn, account["id"], draft, held, id_codes)
            result.removed, result.feed_count = tuple(sorted(found)), len(kept)
            results.append(_verdict(result, gate(conn, account, "generated", eff, parsed, ref, feed_root, today, held=held, id_codes=id_codes)))
        except Exception as err:
            result.ok, result.error, result.broken = False, "%s: %s" % (type(err).__name__, err), True
            results.append(result)
    return results
