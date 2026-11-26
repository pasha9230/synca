"""«Аналитика точек» (the owner 2026-11-26): how fast the orders reach a point and how they end there, per point and in all, for a period.

Read only, from what the panel already writes — nothing invented, a value with no data is None («нет данных» on the page):
- «приехало»: order_arrival (the press «Приехал» not undone, not «шины не приезжали»), `arrived_at` in the period;
- order → arrived: order_card.created_at → arrived_at;
- arrived → the end at the point: the first move of the order (order_event, kind «stage», «Этап: «A» → «B»») after the arrival onto
  «Выполнено» / «Отправлен в ТК» (issued), «На ОТХ» (not bought), «Отмена» (cancelled), «Согласование возврата» / «На возврат» /
  «Вернули поставщику» (returned); no such move recorded — the moment of the order's present stage when it is one of those;
- «Выдано»: the point's orders that came to «Выполнено» in the period (their present stage), «Принято»: the arrivals, «Передано клиентов»:
  order_handoff of the period (the same counts as «Сводки», point_notify.week_counts).
Nothing of the purchase, the prices or the money."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from statistics import mean, median
from typing import Dict, List, Optional

OUTCOMES = {"done": "issued", "shipped": "issued", "oth": "oth", "cancel": "cancel",
            "return_agree": "return", "return_given": "return", "return": "return"}
OUTCOME_NAMES = {"issued": "Выдано", "oth": "Невыкуп (на ОТХ)", "cancel": "Отмена", "return": "Возврат"}
MOVE = re.compile(r"Этап: «(.*)» → «(.*)»")


def _moment(text) -> Optional[datetime]:
    text = str(text or "").strip()[:19]
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace(" ", "T"))
    except ValueError:
        return None


def _hours(start, end) -> Optional[float]:
    a, b = _moment(start), _moment(end)
    if a is None or b is None or b < a:
        return None
    return round((b - a).total_seconds() / 3600, 1)


def _stat(values: List[float]) -> dict:
    return {"n": len(values), "avg": round(mean(values), 1) if values else None, "med": round(median(values), 1) if values else None}


def _after(end: str) -> str:
    return (date.fromisoformat(end[:10]) + timedelta(days=1)).isoformat()


def report(conn, start: str, end: str, point_id: Optional[int] = None) -> dict:
    """{"rows": [per point], "total": {...}} for the arrivals from `start` to `end` (days, both included); a point's row:
    {id, name, received, pieces, issued, handed, to_arrive (stat of hours), to_end (stat), outcomes {issued, oth, cancel, return, open},
    share {oth, cancel, return} (percent of the arrivals, None without arrivals)}."""
    after = _after(end)
    stages = {r[0]: r[1] for r in conn.execute("SELECT name, code FROM order_stage")}     # the names in the journal → their codes
    known = [{"id": r[0], "name": r[1]} for r in conn.execute("SELECT id, name FROM point ORDER BY name")]
    if point_id is not None:
        known = [p for p in known if p["id"] == point_id]
    arrivals = conn.execute(
        "SELECT a.order_id, COALESCE(a.point_id, c.point_id), a.arrived_at, a.pieces, c.created_at, s.code, c.entered_at "
        "FROM order_arrival a JOIN order_card c ON c.id = a.order_id LEFT JOIN order_stage s ON s.id = c.stage_id "
        "WHERE a.arrived_at >= ? AND a.arrived_at < ? AND a.arrived_at != '' AND a.undone_at IS NULL AND a.none_at IS NULL",
        (start[:10], after)).fetchall()
    per: Dict[int, dict] = {}

    def row_of(pid):
        return per.setdefault(pid, {"received": 0, "pieces": 0, "arrive": [], "end": [],
                                    "outcomes": {"issued": 0, "oth": 0, "cancel": 0, "return": 0, "open": 0}})
    for order_id, pid, arrived_at, pieces, created_at, code, entered_at in arrivals:
        if pid is None:
            continue
        got = row_of(pid)
        got["received"] += 1
        got["pieces"] += int(pieces or 0)
        hours = _hours(created_at, arrived_at)
        if hours is not None:
            got["arrive"].append(hours)
        outcome, at = None, None
        for when, text in conn.execute("SELECT at, text FROM order_event WHERE order_id = ? AND kind = 'stage' AND at >= ? ORDER BY id",
                                       (order_id, arrived_at)):
            moved = MOVE.fullmatch(text or "")
            kind = OUTCOMES.get(stages.get(moved.group(2), "")) if moved else None
            if kind:
                outcome, at = kind, when
                break
        if outcome is None and OUTCOMES.get(code or "") and str(entered_at or "") >= str(arrived_at):
            outcome, at = OUTCOMES[code], entered_at
        got["outcomes"][outcome or "open"] += 1
        hours = _hours(arrived_at, at) if outcome else None
        if hours is not None:
            got["end"].append(hours)
    rows, total = [], {"received": 0, "pieces": 0, "issued": 0, "handed": 0, "arrive": [], "end": [],
                       "outcomes": {"issued": 0, "oth": 0, "cancel": 0, "return": 0, "open": 0}}
    for p in known:
        got = row_of(p["id"])
        issued = conn.execute("SELECT COUNT(*) FROM order_card c JOIN order_stage s ON s.id = c.stage_id WHERE c.point_id = ? AND s.code = 'done' "
                              "AND c.entered_at >= ? AND c.entered_at < ?", (p["id"], start[:10], after)).fetchone()[0]
        handed = conn.execute("SELECT COUNT(*) FROM order_handoff WHERE point_id = ? AND created_at >= ? AND created_at < ?",
                              (p["id"], start[:10], after)).fetchone()[0]
        rows.append(_view(dict(got, id=p["id"], name=p["name"], issued=int(issued), handed=int(handed))))
        for key in ("received", "pieces"):
            total[key] += got[key]
        total["issued"] += int(issued)
        total["handed"] += int(handed)
        total["arrive"] += got["arrive"]
        total["end"] += got["end"]
        for key, value in got["outcomes"].items():
            total["outcomes"][key] += value
    return {"rows": rows, "total": _view(dict(total, id=None, name="Все точки"))}


def _view(got: dict) -> dict:
    received = got["received"]
    share = {k: (round(100.0 * got["outcomes"][k] / received, 1) if received else None) for k in ("oth", "cancel", "return")}
    return {"id": got["id"], "name": got["name"], "received": received, "pieces": got["pieces"], "issued": got["issued"],
            "handed": got["handed"], "to_arrive": _stat(got["arrive"]), "to_end": _stat(got["end"]), "outcomes": got["outcomes"], "share": share}
