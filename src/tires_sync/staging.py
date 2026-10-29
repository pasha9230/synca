"""The working draft: every change made on the Catalog page piles up here until the owner confirms it."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from typing import Callable, Optional

from . import store
from .config import own_price_value, settings_from_dict
from .overrides import override_key
from .settings_store import StaleDraftError, cancel_draft, get_active

KEY = "work_draft"
TRACKED = ("enabled", "pricing", "overrides", "excluded_brands", "excluded_sizes", "price_overrides")
SEASONS = ("winter", "summer", "all")
SEASON_RU = {"winter": "зима", "summer": "лето", "all": "всесезонные"}


def _norm(raw: dict, key: str):
    value = raw.get(key)
    if key == "enabled":
        return sorted([b, s] for b, s in (value or []))
    if key in ("excluded_brands", "excluded_sizes"):
        return sorted(set(value or []))
    return value or {}


def _open_draft(conn, in_transaction: bool = False):
    value = store.get_state(conn, KEY)
    if not value:
        return None
    row = conn.execute("SELECT base_version, changes, status FROM draft WHERE id = ?", (int(value),)).fetchone()
    if row is None or row[2] != "open":
        if in_transaction:      # the caller commits or rolls back together with its own change
            conn.execute("INSERT OR REPLACE INTO app_state VALUES (?, '')", (KEY,))
        else:
            store.set_state(conn, KEY, "")
        return None
    return {"id": int(value), "base": row[0], "changes": json.loads(row[1])}


def _count(raw: dict, changes: dict) -> int:
    total = 0
    for key in TRACKED:
        if key not in changes:
            continue
        old, new = _norm(raw, key), _norm(changes, key)
        if key == "enabled":
            total += len({tuple(i) for i in old} ^ {tuple(i) for i in new})
        elif key in ("excluded_brands", "excluded_sizes"):
            total += len(set(old) ^ set(new))
        else:
            total += len([k for k in set(old) | set(new) if old.get(k) != new.get(k)])
    others = [k for k in changes if k not in TRACKED]
    return total + len(others) - ("pack4" in others and "pack4_rules" in others)     # the switch follows the rules: one change


def count_of(raw: dict, changes: dict) -> int:
    """How many changes a draft holds over the active settings (the number of the bar «Не применено»)."""
    return _count(raw, changes)


def _active_value(raw: dict, key: str):
    """The active value a pending one is compared with; the old bare switch «за 4 шт.» means the rule «every position»."""
    if key == "pack4_rules" and "pack4_rules" not in raw:
        return ["*|*|*"] if raw.get("pack4") else []
    return raw.get(key)


def summary(conn) -> dict:
    draft = _open_draft(conn)
    if draft is None:
        return {"draft_id": None, "count": 0, "stale": False, "changes": {}}
    active_id, raw = get_active(conn)
    return {"draft_id": draft["id"], "count": _count(raw, draft["changes"]),
            "stale": draft["base"] != active_id, "changes": draft["changes"]}


class DraftChangedError(ValueError):
    """The working draft is not the one the form was made from (another tab, the partner): nothing written, the page is to be reloaded."""


def rev(changes: dict) -> str:
    """The mark of what a draft holds: «Применить» applies, and the form «Общие» writes over, only the changes the person looked at."""
    return hashlib.sha1(json.dumps(changes, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _mutate(conn, fn: Callable[[dict], None], now: str, expect: Optional[str] = None) -> None:
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        active_id, raw = get_active(conn)
        draft = _open_draft(conn, in_transaction=True)
        if draft is not None and draft["base"] != active_id:
            raise StaleDraftError("Рабочий черновик устарел: настройки изменились. Отмените его и повторите.")
        pending = draft["changes"] if draft else {}
        if expect is not None and rev(pending) != expect:
            raise DraftChangedError("Черновик изменился, пока форма была открыта (другая вкладка или другой человек): "
                                    "ничего не записано, обновите страницу и повторите.")
        effective = {k: copy.deepcopy(_norm({**raw, **pending}, k)) for k in TRACKED}
        effective.update({k: copy.deepcopy(v) for k, v in pending.items() if k not in TRACKED})    # the other settings piled up: kept
        fn(effective)
        diff = {}
        for key in TRACKED:
            if _norm(effective, key) != _norm(raw, key):
                diff[key] = sorted([b, s] for b, s in effective["enabled"]) if key == "enabled" else effective[key]
        for key, value in effective.items():
            if key not in TRACKED and value != _active_value(raw, key):
                diff[key] = value
        if diff:
            settings_from_dict({**raw, **diff}, enforce_pct_limit=False)
        dumped = json.dumps(diff, ensure_ascii=False, sort_keys=True)
        if draft is not None:
            if diff:
                conn.execute("UPDATE draft SET changes = ? WHERE id = ? AND status = 'open'", (dumped, draft["id"]))
            else:
                conn.execute("UPDATE draft SET status = 'cancelled' WHERE id = ? AND status = 'open'", (draft["id"],))
                conn.execute("INSERT OR REPLACE INTO app_state VALUES (?, '')", (KEY,))
        elif diff:
            cur = conn.execute(
                "INSERT INTO draft (base_version, changes, created, status) VALUES (?,?,?,'open')", (active_id, dumped, now))
            conn.execute("INSERT OR REPLACE INTO app_state VALUES (?, ?)", (KEY, str(cur.lastrowid)))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def set_fields(conn, changes: dict, now: str, expect: Optional[str] = None) -> None:
    """Settings of the page «Настройки» (the threshold, «за 4 шт.», the points...) into the working draft, next to every other change piled
    up there (a value back to the active one leaves the draft). Only the keys a draft may change (settings_store._check_keys). `expect`: the
    rev of the draft the form was made from; the form sends every field, so over a changed draft it would put back what another changed."""
    from .settings_store import _check_keys
    _check_keys(changes)
    if any(key in TRACKED for key in changes):
        raise ValueError("these settings have their own setters")

    def fn(eff):
        eff.update(copy.deepcopy(changes))

    _mutate(conn, fn, now, expect)


def effective_raw(conn) -> dict:
    """The active settings with the working draft on top: what the settings pages show while changes wait for «Применить»."""
    raw = get_active(conn)[1]
    draft = _open_draft(conn)
    if draft is None or draft["base"] != raw["version"]:
        return raw
    return {**raw, **draft["changes"]}


def set_override(conn, goods_id: str, mode: Optional[str], now: str) -> None:
    set_overrides(conn, [goods_id], mode, now)


def set_overrides(conn, goods_ids, mode: Optional[str], now: str) -> None:
    if mode not in (None, "on", "off"):
        raise ValueError("Правило позиции: on, off или снять.")
    ids = list(dict.fromkeys(str(g) for g in goods_ids))
    if not ids:
        raise ValueError("Выберите позиции.")
    if len(ids) > 5000:
        raise ValueError("Слишком много позиций за один раз (не более 5000).")

    def fn(eff):
        for goods_id in ids:
            if mode is None:
                eff["overrides"].pop(override_key(goods_id), None)
            else:
                eff["overrides"][override_key(goods_id)] = mode

    _mutate(conn, fn, now)


def set_own_price(conn, goods_id: str, accounts, value: Optional[dict], now: str) -> None:
    """The own price of a position in each of `accounts` (ids of feed accounts): {"pct": percent to the purchase} or {"price": sale price};
    None takes it off («Как у бренда»). Into the working draft like every change of the catalog."""
    goods_id = str(goods_id or "").strip()
    ids = sorted({int(a) for a in accounts})
    if not goods_id or not ids:
        raise ValueError("Выберите позицию и хотя бы один аккаунт.")
    if value is not None:
        kind, number = own_price_value(value)
        value = {kind: number}

    def fn(eff):
        for account_id in ids:
            key = "%d:%s" % (account_id, goods_id)
            if value is None:
                eff["price_overrides"].pop(key, None)
            else:
                eff["price_overrides"][key] = dict(value)

    _mutate(conn, fn, now)


def _pct(value, name):
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("Процент %s: нужно конечное число." % name)
    return float(value)


def set_brand_state(conn, entries, now: str) -> None:
    """(brand, season, small, large, enabled) rows in one change. Percents None: keep them; enabled None: keep the switch.
    Switching a season on needs percents (given now or already set)."""
    entries = list(entries)
    if not entries:
        raise ValueError("Нечего менять.")
    for brand, season, small, large, enabled in entries:
        if season not in SEASONS:
            raise ValueError("Неизвестный сезон.")
        if (small is None) != (large is None):
            raise ValueError("Процент: нужны оба значения, до R17 и от R18.")

    def fn(eff):
        pairs = {tuple(i) for i in eff["enabled"]}
        for brand, season, small, large, enabled in entries:
            key = "%s|%s" % (brand, season)
            if small is not None:
                eff["pricing"][key] = {"pct_small": _pct(small, "до R17"), "pct_large": _pct(large, "от R18")}
            if enabled is None:
                continue
            if enabled and key not in eff["pricing"]:
                raise ValueError("У бренда %s в сезоне «%s» не заданы проценты: сначала укажите их." % (
                    re.sub(r"^S\d+:", "", brand), SEASON_RU[season]))
            pairs.add((brand, season)) if enabled else pairs.discard((brand, season))
        eff["enabled"] = [list(p) for p in pairs]

    _mutate(conn, fn, now)


def set_brand_enabled(conn, brand: str, enabled: bool, now: str) -> None:
    before = {s for b, s in get_active(conn)[1].get("enabled", []) if b == brand}

    def fn(eff):
        pairs = {tuple(i) for i in eff["enabled"]}
        if enabled:
            priced = [s for s in SEASONS if "%s|%s" % (brand, s) in eff["pricing"]]
            ready = [s for s in priced if s in before] or priced
            if not ready:
                raise ValueError("У бренда %s не заданы проценты: включить нечего." % brand)
            pairs |= {(brand, s) for s in ready}
        else:
            pairs = {p for p in pairs if p[0] != brand}
        eff["enabled"] = [list(p) for p in pairs]

    _mutate(conn, fn, now)


def set_brands_excluded(conn, add, remove, now: str, supplier: int = 1) -> None:
    """Put several of a supplier's brands on the list of brands that never go to the feed, and take others off it, in one draft change."""
    prefix = "S%d:" % supplier if supplier != 1 else ""
    add_keys = {prefix + b for b in add}
    remove_keys = {prefix + b for b in remove}

    def fn(eff):
        eff["excluded_brands"] = sorted((set(eff["excluded_brands"]) | add_keys) - remove_keys)

    _mutate(conn, fn, now)


def set_brand_excluded(conn, brand: str, excluded: bool, now: str, supplier: int = 1) -> None:
    """Put a supplier's brand on (or take it off) the list of brands that never go to the feed."""
    from .webmim import brand_key
    key = brand_key(brand or "")
    if not key:
        raise ValueError("Укажите бренд.")
    if supplier != 1:
        key = "S%d:%s" % (supplier, key)

    def fn(eff):
        brands = set(eff["excluded_brands"])
        if excluded:
            brands.add(key)
        else:
            brands.discard(key)
        eff["excluded_brands"] = sorted(brands)

    _mutate(conn, fn, now)


def set_size_excluded(conn, brand, diameter: int, excluded: bool, now: str, supplier: int = 1) -> None:
    """Keep a brand (None: every brand of the supplier) at a radius out of the feed."""
    from .webmim import brand_key
    if isinstance(diameter, bool) or not isinstance(diameter, int) or not 10 <= diameter <= 30:
        raise ValueError("Радиус: целое число от 10 до 30.")
    key = brand_key(brand) if brand else "*"
    prefix = "S%d:" % supplier if supplier != 1 else ""
    rule = "%s%s|%d" % (prefix, key, diameter)

    def fn(eff):
        rules = set(eff["excluded_sizes"])
        if excluded:
            rules.add(rule)
        else:
            rules.discard(rule)
        eff["excluded_sizes"] = sorted(rules)

    _mutate(conn, fn, now)


def cancel_all(conn, now: str, author: str) -> None:
    draft = _open_draft(conn)
    if draft is not None:
        cancel_draft(conn, draft["id"], now, author)
    store.set_state(conn, KEY, "")
