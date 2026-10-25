"""How full the supplier's catalog is, hour by hour: of our published positions, how many are in the unload right now.
It answers when the baskets of the supplier's other sellers are emptied and the catalog fills up again."""
from __future__ import annotations

from typing import Dict, List


def record(conn, parsed, min_stock: int, at: str, source: str, account_id: int = 1) -> Dict[str, int]:
    """One row: the unload's size and the state of our published positions in it. Writes one row, publishes nothing."""
    published = [r[0] for r in conn.execute("SELECT goods_id FROM published_state WHERE account_id = ?", (account_id,))]
    waiting = conn.execute("SELECT COUNT(*) FROM published_state WHERE account_id = ? AND first_hold IS NOT NULL", (account_id,)).fetchone()[0]
    present = low = absent = 0
    for gid in published:
        offer = parsed.offers.get(gid)
        if offer is None:
            absent += 1
        elif offer.stock < min_stock:
            low += 1
        else:
            present += 1
    row = {"items": len(parsed.offers), "published": len(published), "present": present, "absent": absent, "low": low, "waiting": waiting}
    conn.execute("INSERT INTO catalog_probe (at, source, items, published, present, absent, low, waiting) VALUES (?,?,?,?,?,?,?,?)",
                 (at, source, row["items"], row["published"], present, absent, low, waiting))
    conn.execute("DELETE FROM catalog_probe WHERE at < ?", (_days_back(at, 60),))
    conn.commit()
    return row


def _days_back(at: str, days: int) -> str:
    from datetime import datetime, timedelta
    return (datetime.fromisoformat(at) - timedelta(days=days)).isoformat(timespec="seconds")


def list_probes(conn, since: str) -> List[dict]:
    return [{"at": r[0], "source": r[1], "items": r[2], "published": r[3], "present": r[4], "absent": r[5], "low": r[6], "waiting": r[7]}
            for r in conn.execute("SELECT at, source, items, published, present, absent, low, waiting FROM catalog_probe WHERE at >= ? ORDER BY at", (since,))]


def refill_summary(probes: List[dict]) -> List[dict]:
    """Per day: the lowest point of the previous evening and the first probe after midnight at which more of our positions are in the unload."""
    out = []
    by_day: Dict[str, List[dict]] = {}
    for probe in probes:
        by_day.setdefault(probe["at"][:10], []).append(probe)
    days = sorted(by_day)
    for previous, day in zip(days, days[1:]):
        evening = by_day[previous][-1]
        for probe in by_day[day]:
            if probe["present"] > evening["present"]:
                out.append({"day": day, "before": evening["present"], "before_at": evening["at"], "first_at": probe["at"], "after": probe["present"],
                            "published": probe["published"]})
                break
        else:
            out.append({"day": day, "before": evening["present"], "before_at": evening["at"], "first_at": "", "after": None, "published": by_day[day][-1]["published"]})
    return out
