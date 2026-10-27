from __future__ import annotations

import json
from typing import Tuple

from .config import settings_from_dict
from .models import Settings


class SettingsNotInitialized(Exception):
    pass


class StaleDraftError(Exception):
    pass


class PercentLimitError(Exception):
    def __init__(self, max_pct: float) -> None:
        super().__init__("percent %s is outside the allowed range" % max_pct)
        self.max_pct = max_pct


def _dump(raw: dict) -> str:
    return json.dumps(raw, ensure_ascii=False, sort_keys=True)


ALLOWED_KEYS = frozenset({
    "territory_rn", "min_stock", "hysteresis", "min_diameter", "portion_limit", "stale_days",
    "price_jump_limit", "guard_min", "guard_fraction", "allow_mass_removal", "pct_limit", "date_end_days",
    "date_end_renew_days", "contact_phone", "point_mode", "point_values", "point_rest", "enabled", "pricing", "pack4", "pack4_rules", "overrides", "excluded_brands", "excluded_sizes",
})


def _check_keys(changes: dict) -> None:
    bad = sorted(set(changes) - ALLOWED_KEYS)
    if bad:
        raise ValueError("unknown or forbidden settings keys: %s" % ", ".join(bad))


def _pct_map(raw: dict) -> dict:
    return {(k, f): float(v[f]) for k, v in raw["pricing"].items() for f in ("pct_small", "pct_large")}


def _max_changed_pct(old_raw: dict, new_raw: dict) -> float:
    old = _pct_map(old_raw)
    changed = [abs(v) for k, v in _pct_map(new_raw).items() if old.get(k) != v]
    return max(changed) if changed else 0.0


def _audit(conn, now: str, author: str, action: str, detail: dict) -> None:
    conn.execute(
        "INSERT INTO audit (at, author, action, detail) VALUES (?,?,?,?)",
        (now, author, action, json.dumps(detail, ensure_ascii=False)),
    )


def _insert_version(conn, raw: dict, now: str, author: str, note: str) -> int:
    cur = conn.execute(
        "INSERT INTO settings_version (created, author, note, json) VALUES (?,?,?,?)",
        (now, author, note, _dump(raw)),
    )
    return cur.lastrowid


def init_settings(conn, raw: dict, now: str, author: str = "system") -> int:
    if conn.execute("SELECT 1 FROM settings_active").fetchone():
        raise ValueError("settings are already initialized")
    settings_from_dict(raw)
    with conn:
        vid = _insert_version(conn, raw, now, author, "initial")
        conn.execute("INSERT INTO settings_active (id, version_id) VALUES (1, ?)", (vid,))
        _audit(conn, now, author, "settings_init", {"version": vid})
    return vid


def get_active(conn) -> Tuple[int, dict]:
    row = conn.execute(
        "SELECT v.id, v.json FROM settings_active a JOIN settings_version v ON v.id = a.version_id"
    ).fetchone()
    if row is None:
        raise SettingsNotInitialized("settings are not initialized: run `settings init` first")
    raw = json.loads(row[1])
    raw["version"] = row[0]
    return row[0], raw


def active_settings(conn) -> Settings:
    return settings_from_dict(get_active(conn)[1], enforce_pct_limit=False)


def create_draft(conn, changes: dict, now: str) -> int:
    _check_keys(changes)
    base, raw = get_active(conn)
    settings_from_dict({**raw, **changes}, enforce_pct_limit=False)
    with conn:
        cur = conn.execute(
            "INSERT INTO draft (base_version, changes, created, status) VALUES (?,?,?,'open')",
            (base, _dump(changes), now),
        )
    return cur.lastrowid


def _load_draft(conn, draft_id: int):
    row = conn.execute("SELECT base_version, changes, status FROM draft WHERE id = ?", (draft_id,)).fetchone()
    if row is None:
        raise ValueError("draft %s does not exist" % draft_id)
    if row[2] != "open":
        raise ValueError("draft %s is %s" % (draft_id, row[2]))
    return row[0], json.loads(row[1])


def confirm_draft(conn, draft_id: int, now: str, author: str, strong: bool = False, note: str = "") -> int:
    conn.execute("BEGIN IMMEDIATE")
    try:
        base, changes = _load_draft(conn, draft_id)
        _check_keys(changes)
        active_id, raw = get_active(conn)
        if base != active_id:
            raise StaleDraftError("draft %s is based on version %s, active is %s" % (draft_id, base, active_id))
        merged = {**raw, **changes}
        limit = float(raw["pct_limit"])
        changed_max = _max_changed_pct(raw, merged)
        limit_changed = "pct_limit" in changes and float(changes["pct_limit"]) != limit
        if not strong and (changed_max > limit or limit_changed):
            raise PercentLimitError(max(changed_max, float(changes["pct_limit"])) if limit_changed else changed_max)
        settings_from_dict(merged, enforce_pct_limit=False)
        vid = _insert_version(conn, merged, now, author, note or "draft %d" % draft_id)
        conn.execute("UPDATE settings_active SET version_id = ? WHERE id = 1", (vid,))
        conn.execute("DELETE FROM app_state WHERE key = 'nav_pending_pairs'")
        conn.execute("UPDATE draft SET status = 'confirmed' WHERE id = ?", (draft_id,))
        _audit(conn, now, author, "settings_confirm",
               {"draft": draft_id, "from": base, "to": vid, "changes": changes, "strong": strong})
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return vid


def cancel_draft(conn, draft_id: int, now: str, author: str) -> None:
    _load_draft(conn, draft_id)
    with conn:
        conn.execute("UPDATE draft SET status = 'cancelled' WHERE id = ?", (draft_id,))
        _audit(conn, now, author, "settings_cancel", {"draft": draft_id})


def rollback(conn, to_version: int, now: str, author: str) -> int:
    row = conn.execute("SELECT json FROM settings_version WHERE id = ?", (to_version,)).fetchone()
    if row is None:
        raise ValueError("version %s does not exist" % to_version)
    old_active = get_active(conn)[0]
    with conn:
        vid = _insert_version(conn, json.loads(row[0]), now, author, "rollback to %d" % to_version)
        conn.execute("UPDATE settings_active SET version_id = ? WHERE id = 1", (vid,))
        conn.execute("DELETE FROM app_state WHERE key = 'nav_pending_pairs'")
        _audit(conn, now, author, "settings_rollback", {"to_copy_of": to_version, "from": old_active, "to": vid})
    return vid


def get_draft(conn, draft_id: int) -> dict:
    row = conn.execute("SELECT base_version, changes, status FROM draft WHERE id = ?", (draft_id,)).fetchone()
    if row is None:
        raise ValueError("draft %s does not exist" % draft_id)
    return {"id": draft_id, "base_version": row[0], "changes": json.loads(row[1]), "status": row[2]}


def needs_strong_confirmation(raw: dict, changes: dict) -> bool:
    merged = {**raw, **changes}
    limit = float(raw["pct_limit"])
    limit_changed = "pct_limit" in changes and float(changes["pct_limit"]) != limit
    return _max_changed_pct(raw, merged) > limit or limit_changed


def rollback_needs_strong(conn, version_id: int) -> bool:
    """A rolled-back version may hold percents beyond today's limit: the same extra confirmation as for a draft."""
    raw = get_active(conn)[1]
    return any(abs(v) > float(raw["pct_limit"]) for v in _pct_map(get_version(conn, version_id)).values())


def get_version(conn, version_id: int) -> dict:
    row = conn.execute("SELECT json FROM settings_version WHERE id = ?", (version_id,)).fetchone()
    if row is None:
        raise ValueError("version %s does not exist" % version_id)
    return json.loads(row[0])


def list_versions(conn) -> list:
    active = conn.execute("SELECT version_id FROM settings_active").fetchone()
    active_id = active[0] if active else None
    rows = conn.execute("SELECT id, created, author, note FROM settings_version ORDER BY id DESC")
    return [{"id": r[0], "created": r[1], "author": r[2], "note": r[3], "active": r[0] == active_id} for r in rows]
