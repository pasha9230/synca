"""«Товар и фид» → «Пары поставщиков»: one tire at two suppliers (tire_match), as `match-report` shows it — the pairs found now and the
remembered ones, the ambiguous groups and the doubtful positions, the last changes of supplier and who waits for one. The page reads only;
the owner may forget a remembered pair (a mark: the memory no longer uses it) and bring it back, and say two positions are not one tire
(«Это не пара»: never merged, even with both in the unload) and take it back, all in the journal of actions. With the merge on, a mark that
changes a published ad (an Id leaves or comes, passes to another position, changes its model) first shows «было → стало» and is written only
once the owner (the role, not only the right) confirms what was shown (the owner 2026-10-25)."""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from flask import abort, flash, redirect, render_template, request

from .. import connections, mirror_feed, store, sync, tire_match, tochki_feed
from ..candidates import evaluate
from ..settings_store import active_settings
from ..suppliers import load_all_parsed, unload_path_for
from .common import author, current_user, get_conn, login_required, now, now_iso
from .data import load_reference

log = logging.getLogger(__name__)
_parsed: dict = {}                     # {"entry": (key, (parsed, digest, notes))}: one parse of the unloads, while nothing it reads changes


def _stat(path):
    try:
        st = Path(path).stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def positions(conn, settings, base: Path):
    """`load_all_parsed` (the positions as match-report and the cycle read them; the panel's cached merge has no 4tochki with its switch
    «в фид» on), kept while the files, the settings, the suppliers' setup and the hour (a file grows stale with the clock) are the same."""
    suppliers = []
    for supplier in connections.list_suppliers(conn):
        sid = supplier["id"]
        try:
            territory = connections.supplier_territory(conn, sid, settings)
        except ValueError:
            territory = None
        setup = json.dumps(connections.get_config(conn, "webmim", connections.supplier_owner(sid)), sort_keys=True, default=str)
        path = unload_path_for(base, sid)
        suppliers.append((sid, supplier["name"], territory, hashlib.sha256(setup.encode()).hexdigest(), _stat(path),
                          _stat(str(path) + ".meta.json")))
    key = (str(base), repr(settings), json.dumps(tochki_feed.config(conn), sort_keys=True, default=str), tuple(suppliers),
           _stat(tochki_feed.paths(base)["snapshot"]), datetime.utcnow().strftime("%Y-%m-%dT%H"))
    entry = _parsed.get("entry")
    if entry is None or entry[0] != key:
        entry = (key, load_all_parsed(conn, settings, base))
        _parsed["entry"] = entry
    return entry[1]


def pairs_view(conn, parsed, ref, settings, unload_path, now=None, changes_before=None) -> dict:
    """What the page shows (the blocks of `match-report` without its «what if» counts); `now`: Moscow. Nothing is written: the base is
    rolled back and its counter of changes (`changes_before`: taken by the caller before it read anything) checked (RuntimeError)."""
    changes_before = conn.total_changes if changes_before is None else changes_before
    now = (now or datetime.fromisoformat(store.moscow_iso()[:19])).replace(tzinfo=None)
    with_tochki, _shown = tochki_feed.catalog_view(conn, parsed, unload_path)
    offers = with_tochki.offers
    match = tire_match.for_feed(conn, offers)
    ev = evaluate(offers, getattr(with_tochki, "issues", {}), settings, ref, store.load_confirmed_mappings(conn), store.load_brand_map(conn))

    def side(gid):
        out = {"code": gid, "supplier": tire_match.supplier_of(gid), "here": gid in offers}
        if gid in offers:
            o, cand = offers[gid], ev.ok.get(gid)
            out.update(name=o.name_orig or o.name, stock=o.stock, model=cand.avito_model if cand else "",
                       why="" if cand else (ev.excluded.get(gid) or ",".join(ev.problems.get(gid, [])[:1]) or "—"))
        return out

    by_supplier: dict = {}
    for gid in offers:
        by_supplier[tire_match.supplier_of(gid)] = by_supplier.get(tire_match.supplier_of(gid), 0) + 1
    apart = {g for g in match.groups if len({ev.ok[c].avito_model for c in g if c in ev.ok}) > 1}
    forgotten = store.forgotten_tire_pairs(conn)
    not_pairs = store.not_tire_pairs(conn)
    stored = [{"a": side(a), "b": side(b), "found_at": found_at, "forgotten": forgotten.get((a, b)), "apart": not_pairs.get((a, b))}
              for a, b, _ia, _ib, found_at in store.load_tire_pairs(conn)]
    switch_on = tire_match.merge_in_feed(conn)
    for pair in stored:                  # the merge on and one side gone: the pair carries the ad, forgetting or bringing it back moves it
        pair["live"] = switch_on and pair["a"]["here"] != pair["b"]["here"]

    def twos(group):                     # every two positions of different suppliers of a group: what «Это не пара» is said of
        out = []
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                if tire_match.supplier_of(a) != tire_match.supplier_of(b):
                    sa, sb = side(a), side(b)    # one Avito model: the old rule «one tire, one ad» (same_tire) still makes them one ad
                    out.append({"a": a, "b": b, "both_here": sa["here"] and sb["here"],
                                "same_model": sa.get("model") if sa.get("model") and sa.get("model") == sb.get("model") else ""})
        return out
    accounts = []
    today = now.date()
    for account in connections.list_feed_accounts(conn):
        if account["feed_mode"] == "mirror":                                    # as match-report: a mirror has no merge
            continue
        switches = []
        fresh = False
        for ad_id, (from_code, to_code, at) in sorted(store.last_match_switches(conn, account["id"]).items()):
            until = datetime.fromisoformat(at[:19]) + tire_match.SWITCH_PAUSE
            fresh = fresh or until > now
            switches.append({"ad_id": ad_id, "from": from_code, "to": to_code, "at": at, "until": until.isoformat(timespec="seconds")})
        waiting = []
        if fresh:                                    # only a change younger than a day makes a code wait (same_tire._waits_a_day)
            eff = connections.effective_settings(conn, account["id"], settings)
            left_out = set(connections.get_excluded_types(conn, account["id"]))
            account_parsed = parsed
            if left_out:
                from .. import cycle
                account_parsed = copy.copy(parsed)
                account_parsed.offers = {k: v for k, v in parsed.offers.items() if cycle.feed_tire_type(v) not in left_out}
            _, ev_on, _ds = sync.plan_decisions(conn, account_parsed, ref, eff, account["id"], today, now=now, cross_supplier=None)
            if ev_on.match is not None:
                waiting = [{"code": gid, "ad_id": ad_id, "until": until} for gid, (ad_id, until) in sorted(ev_on.match.waiting.items())]
        accounts.append({"id": account["id"], "name": account["name"], "switches": switches, "waiting": waiting})
    view = {
        "switch_on": switch_on,
        "by_supplier": sorted(by_supplier.items()),
        "groups": [{"sides": [side(g) for g in group], "apart": group in apart, "twos": twos(group)} for group in match.groups],
        "not_pairs": [{"a": side(a), "b": side(b), "at": at, "author": who} for (a, b), (at, who) in sorted(not_pairs.items())],
        "refused": [[side(g) for g in group] for group in match.refused],
        "ambiguous": [[side(g) for g in group] for group in match.ambiguous],
        "doubtful": [dict(side(gid), reason=why) for gid, why in sorted(match.doubtful.items())],
        "synonyms": [{"brand": b, "alias": a, "canonical": c} for (b, a), c in store.load_model_synonyms(conn).items()],
        "stored": stored,
        "used": sum(1 for p in stored if not p["forgotten"] and not p["apart"]),
        "remembered": [[side(g) for g in group] for group in match.remembered],
        "accounts": accounts,
    }
    conn.rollback()
    if conn.total_changes != changes_before:
        raise RuntimeError("страница пар изменила строки базы (%d)" % (conn.total_changes - changes_before))
    return view


def _account_parsed(conn, parsed, account_id):
    """The positions an account's feed is built from: its excluded tire types left out (as the cycle and match-report)."""
    left_out = set(connections.get_excluded_types(conn, account_id))
    if not left_out:
        return parsed
    from .. import cycle
    out = copy.copy(parsed)
    out.offers = {k: v for k, v in parsed.offers.items() if cycle.feed_tire_type(v) not in left_out}
    return out


def feed_ads(conn, parsed, ref, settings, now) -> dict:
    """{account id: {Id: (code, Avito model)}} of the ads the next cycle would leave in the feed of every account of our own build, with the
    merge on (sync.plan_decisions, the decisions match-report counts as live; the Id as build_ads writes it). Writes nothing."""
    from ..cli import MATCH_LIVE
    out = {}
    for account in connections.list_feed_accounts(conn):
        if account["feed_mode"] == "mirror":                                    # as match-report: a mirror has no merge
            continue
        acct = account["id"]
        eff = connections.effective_settings(conn, acct, settings)
        _, ev, ds = sync.plan_decisions(conn, _account_parsed(conn, parsed, acct), ref, eff, acct, now.date(), now=now, cross_supplier=None)
        id_map = dict(mirror_feed.adopted_maps(conn, acct)[0], **ev.aliases, **ev.inherit)
        suffix = "" if acct == connections.PRIMARY_ACCOUNT else "_a%d" % acct
        reissued = mirror_feed.reissued_own(conn, acct)
        out[acct] = {sync._final_id(d.goods_id, id_map, suffix, reissued, acct): (d.goods_id, ev.ok[d.goods_id].avito_model if d.goods_id in ev.ok else "")
                     for d in ds.decisions if d.action.value in MATCH_LIVE}
    return out


_MARK_TABLE = {"forget": "tire_pair_forget", "not_pair": "tire_not_pair"}


def _mark(conn, kind, code_a, code_b, undo) -> bool:
    if kind == "forget":
        return store.mark_tire_pair(conn, code_a, code_b, not undo, now_iso(), author())
    return store.mark_not_pair(conn, code_a, code_b, not undo, now_iso(), author())


def _marks(conn):
    return dict(store.forgotten_tire_pairs(conn)), dict(store.not_tire_pairs(conn))


@contextmanager
def _trial(conn, table):
    """The table of the marks shadowed by a temporary copy of this connection's own (sqlite reads an unqualified name from temp first), the
    connection in autocommit: a trial mark written in it is seen by the plan of this connection only, and the base is neither changed nor
    held (no lock, no open transaction: the cycle may write meanwhile). A commit or a failure inside cannot leave it in the base."""
    conn.commit()
    level = conn.isolation_level
    conn.isolation_level = None
    try:
        sql = conn.execute("SELECT sql FROM main.sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()[0]
        conn.execute(re.sub(r"^\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?", "CREATE TEMP TABLE ", sql, count=1, flags=re.I))
        try:
            conn.execute("INSERT INTO temp.%s SELECT * FROM main.%s" % (table, table))
            yield
        finally:
            conn.execute("DROP TABLE temp.%s" % table)
    finally:
        conn.isolation_level = level


def preview(conn, kind, code_a, code_b, undo, parsed, ref, settings, now) -> dict:
    """«было → стало» of a mark (`kind` "forget" or "not_pair", `undo`: its taking back) for the published ads, the merge on: the plan of every
    account as it is and with the mark written in a trial copy of its table (`_trial`). {"accounts": [{"id", "name", "rows"}] (only the
    accounts whose ads change), "digest"}; a row: {"what": stays|leaves|comes|moves|model, "id", "code", "was", "model", "was_model",
    "new_id"}. RuntimeError: the marks of the base changed meanwhile (never expected: the trial does not write there)."""
    code_a, code_b = sorted((str(code_a or "").strip(), str(code_b or "").strip()))
    names = {a["id"]: a["name"] for a in connections.list_feed_accounts(conn)}
    marks = _marks(conn)
    before = feed_ads(conn, parsed, ref, settings, now)
    with _trial(conn, _MARK_TABLE[kind]):
        after = feed_ads(conn, parsed, ref, settings, now) if _mark(conn, kind, code_a, code_b, undo) else before
    if _marks(conn) != marks:
        raise RuntimeError("отметки пар в базе изменились во время пробы %s | %s" % (code_a, code_b))
    accounts = []
    for acct in sorted(set(before) | set(after)):
        was, will = before.get(acct, {}), after.get(acct, {})
        rows, changed = [], False
        for ad_id in sorted(set(was) | set(will)):
            if ad_id not in will:
                rows.append({"what": "leaves", "id": ad_id, "code": was[ad_id][0], "model": was[ad_id][1]})
            elif ad_id not in was:
                rows.append({"what": "comes", "id": ad_id, "code": will[ad_id][0], "model": will[ad_id][1], "new_id": True})
            elif was[ad_id][0] != will[ad_id][0]:
                rows.append({"what": "moves", "id": ad_id, "code": will[ad_id][0], "was": was[ad_id][0], "model": will[ad_id][1],
                             "was_model": was[ad_id][1]})
            elif was[ad_id][1] != will[ad_id][1] and was[ad_id][1] and will[ad_id][1]:     # an empty one: a held ad, no model read
                rows.append({"what": "model", "id": ad_id, "code": will[ad_id][0], "model": will[ad_id][1], "was_model": was[ad_id][1]})
            elif will[ad_id][0] in (code_a, code_b):
                rows.append({"what": "stays", "id": ad_id, "code": will[ad_id][0], "model": will[ad_id][1]})
                continue
            else:
                continue
            changed = True
        if changed:
            accounts.append({"id": acct, "name": names.get(acct, ""), "rows": rows})
    seen = json.dumps([kind, code_a, code_b, bool(undo), accounts], sort_keys=True, ensure_ascii=False)
    return {"accounts": accounts, "digest": hashlib.sha256(seen.encode("utf-8")).hexdigest()[:16]}


def register(app) -> None:
    @app.route("/pairs")
    @login_required
    def pairs_page():
        conn = get_conn()
        connections.list_suppliers(conn)          # a fresh base gets its first supplier here (a commit of its own), not inside the check
        before = conn.total_changes
        moscow = now().replace(tzinfo=None) + timedelta(hours=3)
        try:
            settings = active_settings(conn)
            parsed, _digest, notes = positions(conn, settings, Path(app.config["UNLOAD_PATH"]))
            view = pairs_view(conn, parsed, load_reference(app.config["REFERENCE_PATH"]), settings, app.config["UNLOAD_PATH"], now=moscow,
                              changes_before=before)
        except RuntimeError:
            log.exception("pairs page")
            abort(500)
        except (OSError, ET.ParseError, ValueError, sync.SupplierDataError) as err:      # the unload of a supplier is missing or broken
            conn.rollback()
            log.warning("pairs page: the unload cannot be read", exc_info=True)
            return render_template("pairs.html", error=str(err) or err.__class__.__name__, notes=[])
        return render_template("pairs.html", notes=notes, **view)

    def confirmed(kind, title):
        """With the merge on, a mark that changes a published ad: the owner sees «было → стало» first. None: go on and write the mark as
        before; otherwise the answer (the screen, or a refusal)."""
        conn = get_conn()
        if not tire_match.merge_in_feed(conn):
            return None                                 # the merge off: the feed does not hear of the pairs, nothing to show
        a, b, undo = request.form.get("a", ""), request.form.get("b", ""), bool(request.form.get("undo"))
        if kind == "not_pair" and not undo and (not a.strip() or not b.strip() or a.strip() == b.strip()
                                                or tire_match.supplier_of(a.strip()) == tire_match.supplier_of(b.strip())):
            return None                                 # tire_match.not_a_pair refuses it with its message: nothing to show first
        connections.list_suppliers(conn)                # a fresh base gets its first supplier here (a commit of its own), not in the preview
        moscow = now().replace(tzinfo=None) + timedelta(hours=3)
        try:
            settings = active_settings(conn)
            parsed, _digest, _notes = positions(conn, settings, Path(app.config["UNLOAD_PATH"]))
            shown = preview(conn, kind, a, b, undo, parsed, load_reference(app.config["REFERENCE_PATH"]), settings, moscow)
        except RuntimeError:
            log.exception("pairs preview")
            abort(500)
        except (OSError, ET.ParseError, ValueError, sync.SupplierDataError) as err:
            conn.rollback()
            log.warning("pairs preview: the unload cannot be read", exc_info=True)
            flash("Не сделано: выгрузка поставщика не читается, нельзя показать, что изменится в фидах (%s)." % (str(err) or err.__class__.__name__))
            return redirect("/pairs")
        if not shown["accounts"]:
            return None                                 # no published ad changes: as before
        if current_user()["role"] != "owner":
            flash("Не сделано: это меняет опубликованные объявления, такое решает только владелец.")
            return redirect("/pairs")
        seen = request.form.get("seen", "")
        if seen == shown["digest"]:
            return None
        if seen:
            flash("Пока вы смотрели, картина изменилась: проверьте «было → стало» заново.")
        return render_template("pairs_confirm.html", title=title, action=request.path, a=a, b=b, undo=undo, **shown)

    @app.route("/pairs/forget", methods=["POST"])
    @login_required
    def pairs_forget():
        undo = bool(request.form.get("undo"))
        answer = confirmed("forget", "Вернуть пару" if undo else "Забыть пару")
        if answer is not None:
            return answer
        try:
            done = tire_match.forget_pair(get_conn(), request.form.get("a", ""), request.form.get("b", ""), author(), now_iso(), undo=undo)
        except ValueError as err:
            flash("%s%s." % (str(err)[:1].upper(), str(err)[1:]))
            return redirect("/pairs")
        if not done:
            flash("Пара уже %s." % ("используется памятью" if undo else "забыта"))
        elif undo:
            flash("Пара возвращена: память снова её использует.")
        else:
            flash("Пара забыта: память её больше не использует. Строка пары осталась, вернуть можно здесь же.")
        return redirect("/pairs")

    @app.route("/pairs/not-pair", methods=["POST"])
    @login_required
    def pairs_not_pair():
        undo = bool(request.form.get("undo"))
        answer = confirmed("not_pair", "Это пара (снять запрет)" if undo else "Это не пара")
        if answer is not None:
            return answer
        try:
            done = tire_match.not_a_pair(get_conn(), request.form.get("a", ""), request.form.get("b", ""), author(), now_iso(), undo=undo)
        except ValueError as err:
            flash("Не сделано: %s." % err)
            return redirect("/pairs")
        if not done:
            flash("Запрета и так нет." if undo else "Запрет уже есть.")
        elif undo:
            flash("Запрет снят: позиции снова могут склеиваться.")
        else:
            flash("Запрет записан: эти позиции больше не склеиваются, даже если обе в выгрузке. Снять можно здесь же.")
        return redirect("/pairs")
