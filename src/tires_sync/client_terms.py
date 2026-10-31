"""The conditions of the clients (buyers). The owner 2026-10-31: a client gets its own % of a brand over the purchase (up to R17 / from
R18), set in the invitation before the link is given and changed later in the client's profile; beside every field the owner sees our %
of the brand now (the feed's settings «Бренды и наценки», the general ones: no correction of an account). It is kept as a SHIFT from our %:
the client's % of a tyre = our % of that tyre (its brand at its supplier, its season) + the shift in points, so when we change our % (a
draft → «Применить») the client's moves by as many points (ours 10 → 13: the client's 12 → 15). Empty = as ours (shift 0).

Per diameter of a brand (client_brand_pct: small/large, `*_abs`):
- we have one % for it (every season and warehouse alike): the field is the client's own % and the shift is typed − ours;
- we have several (by season or warehouse): the field is the shift itself («на сколько пунктов выше/ниже нашего»), the client's % of each
  of ours is shown beside;
- we have none (a brand only 4 Точки have, say): the field is the client's % as it is (absolute, `*_abs` = 1) and is needed — without it
  the brand is not shown to the client. It stays absolute until the owner saves the client's conditions again.
The client's % must be within −50…300 (below the purchase is allowed: the owner's word) for every one of ours; else nothing is saved. A
change of our % that would take a client's % out of it stops «Применить» (follow_check, called by settings_store) with the list.
The journal: client_pct_changed («было → стало» of a client's brands), client_pct_followed (our % changed: whose % followed)."""
from __future__ import annotations

import json
import re
from typing import Dict, List, Optional, Tuple

from . import store

PCT_MIN, PCT_MAX = -50, 300
SHIFT_MIN, SHIFT_MAX = -350, 350
MAX_BRANDS = 2000
NUMBER = re.compile(r"^[+-]?[0-9]{1,3}(?:\.[0-9]{1,2})?$")      # ASCII digits only (\d takes other scripts too)
PCT_TEXT = "Процент: число от −50 до 300, не больше двух знаков после запятой."
SHIFT_TEXT = "Сдвиг: число пунктов от −350 до +350, не больше двух знаков после запятой."
DIAMETERS = (("small", "до R17"), ("large", "от R18"))
SEASON_RU = {"winter": "зима", "summer": "лето", "all": "всесезонные"}
MODES = ("pct", "shift", "abs")          # the field is: the client's % over our one %; the shift from several of ours; the client's own %

# a client's row: (small, small_abs, large, large_abs); a value None = as ours (shift 0) or, absolute, not set (the brand hidden)
Row = Tuple[Optional[float], bool, Optional[float], bool]
EMPTY: Row = (None, False, None, False)


class TermsError(ValueError):
    """A refused condition; `field` is the form field it is about (the message goes under it)."""

    def __init__(self, message: str, field: str = ""):
        super().__init__(message)
        self.field = field


class FollowError(ValueError):
    """Our new % would take a client's % out of −50…300: «Применить» is stopped (the owner 2026-10-31: never moved to the bound silently)."""

    def __init__(self, lines: List[str]):
        super().__init__("Применить нельзя: у клиентов %% вышел бы за %d…%d — сначала поправьте их условия: %s." % (
            PCT_MIN, PCT_MAX, "; ".join(lines[:10]) + (" и ещё %d" % (len(lines) - 10) if len(lines) > 10 else "")))
        self.lines = lines


def bare(key: str) -> str:
    from .panel.client_catalog import bare_brand          # the one rule of the catalog (imported here: the panel imports this module's users)
    return bare_brand(key)


def _number(text) -> str:
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        text = repr(float(text))
        text = text[:-2] if text.endswith(".0") else text
    return str(text).strip().replace(",", ".").replace("−", "-").replace(" ", "")


def parse(text, low: float, high: float, message: str, field: str = "") -> Optional[float]:
    """«12,5», «−3», «+2» -> a number within low…high with at most two decimals; empty -> None."""
    if text is None:
        return None
    value = _number(text)
    if value == "":
        return None
    if not NUMBER.match(value) or not low <= float(value) <= high:
        raise TermsError(message, field)
    return float(value)


def fmt(value) -> str:
    return "" if value is None else ("%g" % value).replace("-", "−")


def fmt_shift(value) -> str:
    return "" if value is None else ("%+g" % value).replace("-", "−")


# ---- our % of the brands (the feed's general settings) --------------------------------------------------------------------------------

def ours_of(pricing: dict) -> Dict[str, List[dict]]:
    """{bare brand: [{"key", "season", "label", "small", "large"}]} of the raw «pricing» of the settings ({"BRAND|season": {…}})."""
    out: Dict[str, List[dict]] = {}
    for key, rule in sorted((pricing or {}).items()):
        brand, _, season = key.rpartition("|")
        if not brand or not isinstance(rule, dict):
            continue
        where = "Склад 2, " if re.match(r"^S\d+:", brand) else ""
        out.setdefault(bare(brand), []).append({"key": brand, "season": season, "label": where + SEASON_RU.get(season, season),
                                                "small": float(rule["pct_small"]), "large": float(rule["pct_large"])})
    return out


def active_ours(conn) -> Dict[str, List[dict]]:
    from .settings_store import get_active
    return ours_of(get_active(conn)[1].get("pricing"))


def mode_of(ours: List[dict], diameter: str) -> Tuple[str, Optional[float]]:
    """How a field of a brand reads (MODES) and our one % when there is one."""
    values = sorted({o[diameter] for o in ours})
    if not values:
        return "abs", None
    return ("pct", values[0]) if len(values) == 1 else ("shift", None)


def client_pct(row: Row, diameter: str, our: Optional[float]) -> Optional[float]:
    """The client's % over one of ours (None: not shown — no % of ours and no own one)."""
    value, absolute = (row[0], row[1]) if diameter == "small" else (row[2], row[3])
    if absolute:
        return value
    if our is None:
        return None
    return round(our + (value or 0), 2)


def _in_range(pct: Optional[float]) -> bool:
    return pct is None or PCT_MIN <= pct <= PCT_MAX


# ---- a client's rows -------------------------------------------------------------------------------------------------------------------

def rows(conn, login: str) -> Dict[str, Row]:
    out: Dict[str, Row] = {}
    for key, small, small_abs, large, large_abs in conn.execute(
            "SELECT brand_key, small, small_abs, large, large_abs FROM client_brand_pct WHERE login = ? ORDER BY brand_key", (login,)):
        out.setdefault(bare(key), (small, bool(small_abs), large, bool(large_abs)))
    return out


def mark_of(mode: str, our: Optional[float]) -> str:
    """How a field was drawn (the hidden m<d>_<i>): «pct:<our one %>», «shift» or «abs»; a field of a kept own % is «abs:<own>|<this>»."""
    return "pct:%g" % our if mode == "pct" else mode


def mark(row: Row, diameter: str, ours: List[dict]) -> str:
    mode, our = mode_of(ours, diameter)
    value, absolute = (row[0], row[1]) if diameter == "small" else (row[2], row[3])
    return ("abs:%g|%s" % (value, mark_of(mode, our))) if absolute and mode != "abs" else mark_of(mode, our)


def field_value(row: Row, diameter: str, ours: List[dict]) -> str:
    """What a field shows: the client's % (pct, abs) or the shift (several of ours)."""
    mode, our = mode_of(ours, diameter)
    value, absolute = (row[0], row[1]) if diameter == "small" else (row[2], row[3])
    if absolute or mode == "abs":                    # an own % kept absolute shows itself; left as it is, it stays (parse_rows: «abs:»)
        return fmt(value) if absolute else ""
    if mode == "pct":
        return "" if value is None else fmt(round(our + value, 2))
    return fmt_shift(value)


def parse_rows(form, prefix: str, ours: Dict[str, List[dict]]) -> Dict[str, Row]:
    """The rows of a sent form ({bare key: Row}); every field read as it was drawn (the hidden m_<i>_<d>: its mode, and our % of «pct»):
    our % changed since the page was drawn — refused, never read another way. A value out of −50…300 for any of ours is refused under
    its field."""
    keys = form.getlist(prefix + "bk")
    if len(keys) > MAX_BRANDS:
        raise TermsError("Слишком много брендов: не больше %d." % MAX_BRANDS)
    out: Dict[str, Row] = {}
    for i, key in enumerate(keys):
        key = bare(str(key))
        if not key:
            continue
        mine = ours.get(key, [])
        parts = []
        for diameter, label in DIAMETERS:
            field = "%s%s_%d" % (prefix, diameter[0], i)
            mode, our = mode_of(mine, diameter)
            drawn = form.get("%sm%s_%d" % (prefix, diameter[0], i), mark_of(mode, our))   # a form without the mark: as it reads now
            if drawn.startswith("abs:") and "|" in drawn and mode != "abs":     # the client's own % set when we had none (the review):
                kept_text, drawn = drawn[4:].split("|", 1)                       # «abs:<own>|<the mode it was drawn in>»
                kept = parse(kept_text, PCT_MIN, PCT_MAX, PCT_TEXT, field)
                if kept is not None and _number(form.get(field, "")) == _number(fmt(kept)):
                    parts += [kept, True]                                        # left as drawn: it stays the client's own, as it is
                    continue
            if drawn != mark_of(mode, our):                                      # a new number is read only the way it was drawn
                raise TermsError("Наш %% бренда %s изменился, пока открыта страница: проверьте значения и сохраните снова." % key.upper(), field)
            if mode == "shift":
                value = parse(form.get(field, ""), SHIFT_MIN, SHIFT_MAX, SHIFT_TEXT, field)
                parts += [value or None, False]
                totals = [round(o[diameter] + (value or 0), 2) for o in mine]
            else:
                value = parse(form.get(field, ""), PCT_MIN, PCT_MAX, PCT_TEXT, field)
                if mode == "pct":
                    shift = None if value is None else round(value - our, 2)
                    parts += [shift or None, False]
                    totals = [value] if value is not None else []
                else:
                    parts += [value, value is not None]
                    totals = [value] if value is not None else []
            bad = [t for t in totals if not _in_range(t)]
            if bad:
                raise TermsError("Бренд %s %s: %% клиента вышел бы %g — нужно от %d до %d." % (key.upper(), label, bad[0], PCT_MIN, PCT_MAX), field)
        row = (parts[0], parts[1], parts[2], parts[3])
        if row != EMPTY:
            out[key] = row
    return out


def check_rows(data: Dict[str, Row], ours: Dict[str, List[dict]]) -> None:
    """Every client's % of the rows within −50…300 for every one of ours (an invitation accepted later, our % changed since)."""
    for key, row in data.items():
        for diameter, label in DIAMETERS:
            for our in [o[diameter] for o in ours.get(key, [])] or [None]:
                pct = client_pct(row, diameter, our)
                if not _in_range(pct):
                    raise TermsError("Бренд %s %s: %% клиента %g вне %d…%d." % (key.upper(), label, pct, PCT_MIN, PCT_MAX))


def _row_text(row: Row) -> str:
    parts = []
    for (diameter, label), (value, absolute) in zip(DIAMETERS, ((row[0], row[1]), (row[2], row[3]))):
        parts.append("%s %s" % (label, ("%s %%" % fmt(value)) if absolute else ("как у нас" if value is None else "%s п. к нашему" % fmt_shift(value))))
    return ", ".join(parts)


def save_rows(conn, login: str, data: Dict[str, Row], now: str, author: str, commit: bool = True, whole: bool = False) -> dict:
    """Write a client's rows (checked against our % now; nothing written when refused). `whole`: the given rows are all of them; else only
    the brands given change (a brand left empty in a form loses its row: it is given as EMPTY). `commit=False`: the caller's transaction
    (an invitation is accepted at once or not at all). The journal gets «было → стало» of the brands changed."""
    from . import users
    login = users.normalize(login)
    if not users.exists(conn, login) or users.get_role(conn, login)["role"] != "client":
        raise TermsError("Условия ставятся только клиенту.")
    clean = {bare(k): tuple(v) for k, v in data.items() if bare(k)}
    check_rows({k: v for k, v in clean.items() if v != EMPTY}, active_ours(conn))
    old = rows(conn, login)
    new = {k: v for k, v in clean.items() if v != EMPTY}
    if not whole:
        new = dict({k: v for k, v in old.items() if k not in clean}, **new)
    conn.execute("DELETE FROM client_brand_pct WHERE login = ?", (login,))
    conn.executemany("INSERT INTO client_brand_pct (login, brand_key, small, small_abs, large, large_abs) VALUES (?,?,?,?,?,?)",
                     sorted((login, k, v[0], int(v[1]), v[2], int(v[3])) for k, v in new.items()))
    changes = {k: [_row_text(old.get(k, EMPTY)), _row_text(new.get(k, EMPTY))] for k in sorted(set(old) | set(new)) if old.get(k) != new.get(k)}
    detail = {"subject": login, "changes": changes}
    if changes:
        audit(conn, now, author, "client_pct_changed", detail, commit)
    elif commit:
        conn.commit()
    return detail


def audit(conn, now: str, author: str, action: str, detail: dict, commit: bool) -> None:
    """store.write_audit commits; inside a caller's transaction the row is written without it."""
    if commit:
        store.write_audit(conn, now, author, action, detail)
    else:
        conn.execute("INSERT INTO audit (at, author, action, detail) VALUES (?,?,?,?)", (now, author, action, json.dumps(detail, ensure_ascii=False)))


# ---- our % changes: the clients' % follow ---------------------------------------------------------------------------------------------

def follow(conn, old_pricing: dict, new_pricing: dict) -> dict:
    """What a change of our % does to the clients: {"clients": the clients whose % moves (every client follows ours), "lines": «клиент,
    бренд, было → станет» of the clients with a shift of their own, "out": those that would leave −50…300}."""
    old, new = ours_of(old_pricing), ours_of(new_pricing)
    changed = {}
    for brand in set(old) | set(new):
        before = {(o["key"], o["season"]): o for o in old.get(brand, [])}
        after = {(o["key"], o["season"]): o for o in new.get(brand, [])}
        moved = [(k, before.get(k), after.get(k)) for k in set(before) | set(after)
                 if (before.get(k) or {}).get("small") != (after.get(k) or {}).get("small")
                 or (before.get(k) or {}).get("large") != (after.get(k) or {}).get("large")]
        if moved:
            changed[brand] = sorted(moved, key=lambda m: m[0])
    if not changed:
        return {"clients": 0, "lines": [], "out": []}
    logins = [r[0] for r in conn.execute("SELECT login FROM panel_user_role WHERE role = 'client' ORDER BY login")]
    lines, out = [], []
    for login in logins:
        mine = rows(conn, login)
        for brand, moved in sorted(changed.items()):
            row = mine.get(brand, EMPTY)
            for (key, season), b, a in moved:
                for diameter, label in DIAMETERS:
                    was = client_pct(row, diameter, b[diameter]) if b else client_pct(row, diameter, None)
                    will = client_pct(row, diameter, a[diameter]) if a else client_pct(row, diameter, None)
                    if was == will:
                        continue
                    text = "%s, %s (%s) %s: %s → %s" % (login, brand.upper(), (a or b)["label"], label, "—" if was is None else "%g %%" % was,
                                                        "—" if will is None else "%g %%" % will)
                    if brand in mine:                     # a client without a row of the brand is ours exactly: nothing to tell
                        lines.append(text)
                        if not _in_range(will):
                            out.append(text)
    for brand, moved in sorted(changed.items()):                # ours itself out of bounds: every client without its own % loses it
        for (key, season), b, a in moved:
            for diameter, label in DIAMETERS:
                if a and logins and not _in_range(a[diameter]):
                    out.append("клиенты без своего %%, %s (%s) %s: %g %%" % (brand.upper(), a["label"], label, a[diameter]))
    return {"clients": len(logins), "lines": lines, "out": out}


def follow_check(conn, old_pricing: dict, new_pricing: dict, now: str, author: str) -> None:
    """Inside «Применить» (settings_store, its transaction): stopped when a client's % would leave −50…300; else a journal line of whose
    % followed (no commit: the caller's)."""
    found = follow(conn, old_pricing, new_pricing)
    if found["out"]:
        raise FollowError(found["out"])
    if found["lines"]:
        audit(conn, now, author, "client_pct_followed", {"clients": found["clients"], "lines": found["lines"][:50],
                                                         "more": max(0, len(found["lines"]) - 50)}, False)


# ---- an invitation of a client -------------------------------------------------------------------------------------------------------

def invite_terms(data: Dict[str, Row]) -> str:
    """The client's rows an invitation puts in the profile (checked by parse_rows), as JSON {"rows": {bare: [4 values]}}."""
    return json.dumps({"rows": {bare(k): [v[0], bool(v[1]), v[2], bool(v[3])] for k, v in data.items() if bare(k)}},
                      ensure_ascii=False, sort_keys=True)


def from_invite(text: str) -> Optional[dict]:
    """The rows of an invitation ({"rows": {key: Row}}; None: not a client's). An invitation made before 2026-10-31 (the role's or the
    profile's general % and a set of brands: no such conditions any more) brings none. A broken one is refused."""
    if not text:
        return None
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError
        if "rows" not in data:
            return {"rows": {}}
        if not isinstance(data["rows"], dict) or len(data["rows"]) > MAX_BRANDS:
            raise ValueError
        out = {}
        for key, value in data["rows"].items():
            small, small_abs, large, large_abs = value
            row = (parse(small, SHIFT_MIN, SHIFT_MAX, SHIFT_TEXT), bool(small_abs), parse(large, SHIFT_MIN, SHIFT_MAX, SHIFT_TEXT), bool(large_abs))
            if (row[1] and not _in_range(row[0])) or (row[3] and not _in_range(row[2])):
                raise ValueError
            if bare(str(key)) and row != EMPTY:
                out[bare(str(key))] = row
        return {"rows": out}
    except (ValueError, KeyError, TypeError) as err:
        raise TermsError("Условия приглашения испорчены: попросите новую ссылку.") from err


def describe(data: Dict[str, Row]) -> List[str]:
    """A short line for the owner's lists: «как у нас» or «свой %: GISLAVED, NOKIAN и ещё 3»."""
    if not data:
        return ["% как у нас"]
    keys = sorted(data)
    return ["свой %%: %s%s" % (", ".join(k.upper() for k in keys[:3]), " и ещё %d" % (len(keys) - 3) if len(keys) > 3 else "")]
