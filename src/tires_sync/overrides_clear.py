"""One-off clean-up: take the manual «on» off every position «старше N лет», so the old_production exclusion works again."""
from __future__ import annotations

import sqlite3
from typing import List, Tuple

from . import connections, staging, store
from .filters import is_aged
from .overrides import override_key
from .settings_store import StaleDraftError, cancel_draft, confirm_draft, create_draft, get_active, get_draft


def find_aged_on(offers, overrides) -> List[Tuple[str, object]]:
    """Positions of the unload that are aged and hold a manual «on» (an «off» is never touched), by goods id."""
    return [(gid, offer) for gid, offer in sorted(offers.items(), key=lambda kv: kv[0])
            if is_aged(offer) and overrides.get(override_key(gid)) == "on"]


def in_feed_now(conn, goods_id: str) -> List[str]:
    names = []
    for account in connections.list_feed_accounts(conn):
        if goods_id in store.load_published(conn, account["id"]):
            names.append("№%d «%s»" % (account["id"], account["name"]))
    return names


def apply_clear(conn, found, version: int, now: str, author: str = "cli") -> int:
    """Remove the «on» of `found` in a new settings version made as the panel does (draft → confirm). Refuses on a changed state."""
    active_id, raw = get_active(conn)
    if active_id != version:
        raise ValueError("настройки изменились после составления списка (версия %s, было %s): повторите" % (active_id, version))
    if staging.summary(conn)["count"]:
        raise ValueError("в панели есть неподтверждённый черновик: подтвердите или отмените его, иначе он устареет и пропадёт")
    drop = {override_key(gid) for gid, _ in found}
    overrides = {k: v for k, v in raw["overrides"].items() if not (k in drop and v == "on")}
    try:
        draft = create_draft(conn, {"overrides": overrides}, now)
    except sqlite3.OperationalError:
        raise ValueError("база занята: повторите") from None
    try:
        if get_draft(conn, draft)["base_version"] != version:      # the list was made on `version`: a newer one would lose its overrides
            raise ValueError("настройки изменились во время записи: повторите")
        if staging.summary(conn)["count"]:
            raise ValueError("в панели появился неподтверждённый черновик: повторите после его подтверждения или отмены")
        return confirm_draft(conn, draft, now, author, note="снято ручное вкл у позиций «старше» (%d)" % len(found))
    except StaleDraftError:
        raise ValueError("настройки изменились во время записи: повторите") from None
    except sqlite3.OperationalError:
        raise ValueError("база занята: повторите") from None
    finally:
        if conn.execute("SELECT status FROM draft WHERE id = ?", (draft,)).fetchone()[0] == "open":
            try:
                cancel_draft(conn, draft, now, author)
            except Exception:
                pass
