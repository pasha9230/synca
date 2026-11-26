"""«Сводки» of a point (the owner 2026-11-26; the page /my-point/notices, was «Оповещение»): what the point did and what waits for it.

- One block of counts, the last calendar week beside this month: the tyres' orders that came (point_notify.week_counts), handed out
  (routes_mypoint._issued, the rule of «Выдано»), the clients handed over, the orders sold from «На ОТХ» by the point itself (the event of
  order_report.OTH_SOLD_EVENT, never the office working as the point — order_report.oth_sold_orders' rule) and the month's KPI
  (income_view's own count). The sums in rubles only while the point may see «Доход» (the caller says so); never a purchase or a price.
- «Требует действия сейчас»: the orders on the way whose delivery day has passed and no «Приехал» yet, the orders waiting at the point
  for STALE_DAYS and more (beside the returns of today, point_notify.returns_today), each with its link to «Рабочее место».
- «Другие точки»: every point's counts (came, handed out, the share of «Приехал» marked in time) — names and numbers only, no money, no
  client; the owner switches the tab off (COMPARE_OFF_KEY, on by default).

Nothing here sends anything or changes an order; nothing of the purchase or the prices is read."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Dict, List, Optional

LATE_DAYS = 1          # «Едут»: the delivery day was this many days ago (yesterday) and no «Приехал»
STALE_DAYS = 5         # «На точке»: the client has not come for this many days
ON_TIME_DAYS = 1       # «Приехал» marked on the delivery day or the next one counts as in time
SHOWN = 20             # the rows of each list of «Требует действия сейчас»
COMPARE_OFF_KEY = "point_compare_off"      # app_state «1»: the tab «Другие точки» is hidden (the owner's switch; on by default)
MONTHS = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь")


def month_start(today: str) -> str:
    return today[:8] + "01"


def month_name(today: str) -> str:
    return MONTHS[int(today[5:7]) - 1]


def _after(day: str) -> str:
    return (date.fromisoformat(day[:10]) + timedelta(days=1)).isoformat()


def oth_sold(conn, point_id, start: str, end: str) -> int:
    """The orders of the point it sold itself from «На ОТХ» from `start` to `end` (days, both included): the point's event of the sale,
    the office working as the point not counted (order_report.oth_sold_orders' rule)."""
    from . import order_report
    from .orders import ACTING_MARK
    return int(conn.execute(
        "SELECT COUNT(DISTINCT e.order_id) FROM order_event e JOIN order_card c ON c.id = e.order_id WHERE c.point_id = ? AND e.kind = 'point' "
        "AND (e.text = ? OR e.text LIKE ?) AND instr(e.author, ?) = 0 AND e.at >= ? AND e.at < ?",
        (point_id, order_report.OTH_SOLD_EVENT, order_report.OTH_SOLD_EVENT + ":%", ACTING_MARK, start[:10], _after(end))).fetchone()[0])


def counts(conn, point_id, start: str, end: str, issued: List[dict]) -> Dict[str, int]:
    """{received, issued, handed, oth} of the point from `start` to `end`; `issued` — the rows of «Выдано» (their «at»)."""
    from . import point_notify
    out = point_notify.week_counts(conn, point_id, start, end)
    out["issued"] = sum(1 for r in issued if start <= str(r["at"])[:10] <= end)
    out["oth"] = oth_sold(conn, point_id, start, end)
    return out


def on_time(conn, point_id, start: str, end: str) -> Dict[str, int]:
    """«Приехал» of the period marked in time: {"in_time", "dated"} — of the arrivals (not undone, not «шины не приезжали») whose order has
    a delivery day, those marked on that day or within ON_TIME_DAYS after it (an earlier mark is in time too)."""
    rows = conn.execute("SELECT a.arrived_at, c.delivery_date FROM order_arrival a JOIN order_card c ON c.id = a.order_id "
                        "WHERE COALESCE(a.point_id, c.point_id) = ? AND a.arrived_at >= ? AND a.arrived_at < ? AND a.undone_at IS NULL "
                        "AND a.none_at IS NULL AND c.delivery_date != ''", (point_id, start[:10], _after(end))).fetchall()
    dated = in_time = 0
    for arrived, planned in rows:
        try:
            limit = (date.fromisoformat(str(planned)[:10]) + timedelta(days=ON_TIME_DAYS)).isoformat()
        except ValueError:
            continue
        dated += 1
        in_time += str(arrived)[:10] <= limit
    return {"in_time": in_time, "dated": dated, "pct": round(in_time * 100 / dated) if dated else None}


def _label(number, card_id) -> str:
    return str(number or card_id)


def to_act(conn, point_id, today: str) -> Dict[str, List[dict]]:
    """«Требует действия сейчас»: {"late": the orders on «Заказ оформлен» / «Перемещение на точку» whose delivery day was LATE_DAYS and more
    ago, «stale»: those on «Товар на точке» for STALE_DAYS and more}, the oldest first; the point's own sales are not of «Рабочее место»."""
    from . import order_points
    late_day = (date.fromisoformat(today[:10]) - timedelta(days=LATE_DAYS)).isoformat()
    stale_day = (date.fromisoformat(today[:10]) - timedelta(days=STALE_DAYS - 1)).isoformat()
    late = conn.execute("SELECT c.id, c.webmim_number, c.client_name, c.delivery_date FROM order_card c JOIN order_stage s ON s.id = c.stage_id "
                        "WHERE c.point_id = ? AND s.code IN ('placed', 'moving') AND c.delivery_date != '' AND c.delivery_date <= ? "
                        "ORDER BY c.delivery_date, c.id", (point_id, late_day)).fetchall()
    stale = conn.execute("SELECT c.id, c.webmim_number, c.client_name, c.entered_at FROM order_card c JOIN order_stage s ON s.id = c.stage_id "
                         "WHERE c.point_id = ? AND s.code = 'at_point' AND c.entered_at != '' AND c.entered_at < ? ORDER BY c.entered_at, c.id",
                         (point_id, stale_day)).fetchall()
    mine = order_points.own_sales(conn, point_id) if late or stale else set()
    day = date.fromisoformat(today[:10])

    def days(text) -> int:
        try:
            return (day - date.fromisoformat(str(text)[:10])).days
        except ValueError:
            return 0
    return {"late": [{"id": r[0], "label": _label(r[1], r[0]), "client": r[2] or "", "day": r[3], "days": days(r[3])}
                     for r in late if r[0] not in mine][:SHOWN],
            "stale": [{"id": r[0], "label": _label(r[1], r[0]), "client": r[2] or "", "day": str(r[3])[:10], "days": days(r[3])}
                      for r in stale if r[0] not in mine][:SHOWN]}


def money(conn, point_id: int, periods: Dict[str, tuple], report: dict) -> Dict[str, float]:
    """What the point got for each period (point_payouts.items of the one count `report`): {period: rubles}. Only while it may see «Доход»."""
    from . import point_payouts
    return {name: round(sum(e["total"] for e in point_payouts.items(conn, start, end, point_id, report)), 2)
            for name, (start, end) in periods.items()}


# ---- the owner's switch of «Другие точки» ---------------------------------------------------------------------------------------------

def compare_shown(conn) -> bool:
    row = conn.execute("SELECT value FROM app_state WHERE key = ?", (COMPARE_OFF_KEY,)).fetchone()
    return not (row and row[0] == "1")


def set_compare_shown(conn, on: bool, now: str, author: str) -> bool:
    """«Показывать точкам вкладку «Другие точки»»; audited, committed. True if it changed."""
    from . import store
    if compare_shown(conn) == bool(on):
        return False
    if on:
        conn.execute("DELETE FROM app_state WHERE key = ?", (COMPARE_OFF_KEY,))
    else:
        conn.execute("INSERT OR REPLACE INTO app_state (key, value) VALUES (?, '1')", (COMPARE_OFF_KEY,))
    store.write_audit(conn, now, author, "point_compare_shown", {"on": bool(on)})
    conn.commit()
    return True


def compare(conn, known: List[dict], start: str, end: str, issued_of, mine: Optional[int] = None) -> List[dict]:
    """«Другие точки»: per point (by name, never a rating) {id, name, received, issued, in_time, dated, pct, mine}; `issued_of(point_id)`
    gives its rows of «Выдано». Names and counts only."""
    from . import point_notify
    rows = []
    for p in known:
        came = point_notify.week_counts(conn, p["id"], start, end)
        rows.append(dict(on_time(conn, p["id"], start, end), id=p["id"], name=p["name"], received=came["received"],
                         issued=sum(1 for r in issued_of(p["id"]) if start <= str(r["at"])[:10] <= end), mine=p["id"] == mine))
    return rows
