"""The report of what the orders sold: per point, for a period. Read only: the numbers come from the sales already written by the orders.

The point gets a fixed fee for every order; what is left of the margin is the net profit, a share of it goes to the owner and the rest to the partner
(`docs/DECISIONS.md`, 2026-10-19). Both rates have dates (`order_rate`): an order is counted with the rates in force on its day, so a new rate
changes only the orders from its date on; until a rate is saved the defaults below hold. Only the sales made through the orders are split;
the manual sales are shown in a line of their own (`docs/DECISIONS.md`, 2026-10-21); keeping them out of the total and the split is the
lane's choice until the owner decides how they are shared.

The purchase of an order paired with an order of WebMIM (server prod) is counted at WebMIM's price of the position, for the report only
(`docs/DECISIONS.md`, 2026-10-22: «только для аналитики»), in the sales from WEBMIM_FROM on: the sales keep what they wrote, nothing is
written anywhere. No pair, no price, the test server, an earlier sale: the sale as written."""
from __future__ import annotations

import json
import math
import re
from datetime import date
from typing import Dict, List, Optional

from . import order_oth, sales as sales_store, store, users, webmim_match

POINT_FEE = 75.0                     # rubles per order, paid to the point (the default: before the first dated rate)
OWNER_SHARE = 0.5                    # of the net profit; the rest is the partner's (the default)
HANDOFF_FEE = 50.0                   # rubles to the point for an issued order of a client it handed over, on top of the 75 (the owner 2026-10-23, п. 24)
HANDOFF_FEE_FROM = "2026-10-24"      # the day of the deploy: the orders of earlier days keep the old default, 0 (a past report is never recounted)
OTH_SALE_FEE = 50.0                  # rubles to the point for every tyre it sold from «На ОТХ» (ТЗ 80; «+50 ₽ за каждую проданную шину» of the window)
OTH_SALE_FEE_FROM = "2026-11-25"     # the sales of earlier days get 0: a past report is never recounted (as HANDOFF_FEE_FROM)
# ТЗ 92 / 95 (the owner 2026-11-24: «50 ₽ за каждую шину», «+200 ₽ за комплект»): from this day the bonus of a handed-over client is
# handoff_fee for every tyre of the issued order; the orders of earlier days keep it per order (a past report is never recounted)
HANDOFF_PER_TYRE_FROM = "2026-11-26"
# KPI (ТЗ 95): in a calendar month, for every full cycle of kpi_orders closed orders AND kpi_tyres tyres of the clients the point handed
# over, the premium kpi_bonus; the cycles are not limited: min(orders // kpi_orders, tyres // kpi_tyres) premiums (20 / 76 → 2). The rates
# of a month are those in force on its first day. The premium is the point's on the day of the order that closes the cycle.
KPI_ORDERS, KPI_TYRES, KPI_BONUS = 10, 38, 1500.0
KPI_FROM = "2026-12-01"              # the first whole month: no premium of a month before (a past month is never recounted)
DEFAULTS = {"point_fee": POINT_FEE, "owner_share": OWNER_SHARE, "handoff_fee": HANDOFF_FEE, "oth_sale_fee": OTH_SALE_FEE,
            "kpi_orders": KPI_ORDERS, "kpi_tyres": KPI_TYRES, "kpi_bonus": KPI_BONUS}
STARTS = {"handoff_fee": HANDOFF_FEE_FROM, "oth_sale_fee": OTH_SALE_FEE_FROM, "kpi_bonus": KPI_FROM}     # before it, with no rate saved: 0
POINT_RATES = ("point_fee", "handoff_fee", "oth_sale_fee", "kpi_orders", "kpi_tyres", "kpi_bonus")     # a point may have its own (point_rate)
COUNTS = ("kpi_orders", "kpi_tyres")                                                                    # whole numbers, 1 and more
MAX_COUNT = 10000
# the journal of an order the point sold from «На ОТХ» (kind «point»): oth_point_sale._sell writes the first with «: N шт. …», order_oth.sell
# the second as it is; the office's «Выполнено» of a record (order_oth.OFFICE_SOLD) and «Взять с ОТХ» are no sale of the point
OTH_SOLD_EVENT = order_oth.POINT_SOLD
MAX_POINT_FEE = 100000.0
NO_POINT = "точка не указана"
MANUAL = "Продажи вручную"
WEBMIM_SERVER = "prod"               # the test server's orders are made up: they never reach the money
WEBMIM_FROM = "2026-10-23"           # the owner 2026-10-22 «с даты выкладки»: the sales of this (Moscow) day on; the closed periods stay as they were
SHARE_LABELS = {"owner": "Владельцу", "partner": "Партнёру"}   # who the two shares go to, when the panel has no name for them
SHARE_LABELS_KEY = "share_labels"     # app_state: the labels the owner wrote on «Отчёт по заказам» (JSON {"owner", "partner"})
SHARE_LABEL_MAX = 40


def list_rates(conn) -> List[dict]:
    rows = conn.execute("SELECT name, valid_from, value, created, author FROM order_rate ORDER BY valid_from, name")
    return [dict(zip(("name", "valid_from", "value", "created", "author"), r)) for r in rows]


def rate_on(rates: List[dict], name: str, day: str) -> float:
    """The rate in force on `day`: the latest one dated on or before it, else the default (for the handed-over clients: 0 before HANDOFF_FEE_FROM)."""
    found = [r for r in rates if r["name"] == name and r["valid_from"] <= day]
    if found:
        return float(max(found, key=lambda r: r["valid_from"])["value"])
    return 0.0 if name in STARTS and day < STARTS[name] else DEFAULTS[name]


def _check_rate(name: str, value: float) -> None:
    if name == "point_fee" and not 0 <= value <= MAX_POINT_FEE:
        raise ValueError("Выплата точке: от 0 до 100 000 ₽ за заказ.")
    if name == "owner_share" and not 0 <= value <= 1:
        raise ValueError("Доля собственника: от 0 до 100 %.")
    if name == "handoff_fee" and not 0 <= value <= MAX_POINT_FEE:
        raise ValueError("Выплата точке за переданного клиента: от 0 до 100 000 ₽.")
    if name == "oth_sale_fee" and not 0 <= value <= MAX_POINT_FEE:
        raise ValueError("Бонус точке за шину, проданную с ОТХ: от 0 до 100 000 ₽.")
    if name == "kpi_bonus" and not 0 <= value <= MAX_POINT_FEE:
        raise ValueError("Премия KPI: от 0 до 100 000 ₽.")
    if name in COUNTS and not (value == int(value) and 1 <= value <= MAX_COUNT):
        raise ValueError("Порог KPI: целое число от 1 до %d." % MAX_COUNT)


def _valid_from(valid_from, today: str) -> str:
    try:
        valid_from = date.fromisoformat(str(valid_from)).isoformat()
    except ValueError:
        raise ValueError("Дата начала: нужен формат ГГГГ-ММ-ДД.")
    if valid_from < today:
        raise ValueError("Ставка действует с сегодняшнего дня или позже: прошлые отчёты не пересчитываются.")
    return valid_from


def set_rates(conn, valid_from: str, today: str, now: str, author: str, point_fee: Optional[float] = None,
              owner_share: Optional[float] = None, handoff_fee: Optional[float] = None, oth_sale_fee: Optional[float] = None,
              kpi_orders: Optional[float] = None, kpi_tyres: Optional[float] = None, kpi_bonus: Optional[float] = None) -> List[str]:
    """Save the rates from `valid_from` (today or later: a past report is never recounted). Returns the names that changed; each is audited."""
    valid_from = _valid_from(valid_from, today)
    given = (("point_fee", point_fee), ("owner_share", owner_share), ("handoff_fee", handoff_fee), ("oth_sale_fee", oth_sale_fee),
             ("kpi_orders", kpi_orders), ("kpi_tyres", kpi_tyres), ("kpi_bonus", kpi_bonus))
    for name, value in given:
        if value is not None:
            _check_rate(name, float(value))
    rates = list_rates(conn)
    changed = []
    for name, value in given:
        if value is None:
            continue
        value = round(float(value), 4 if name == "owner_share" else 2)        # rubles and kopecks; a share to a hundredth of a percent
        old = rate_on(rates, name, valid_from)
        if value == old:
            continue
        conn.execute("INSERT OR REPLACE INTO order_rate (name, valid_from, value, created, author) VALUES (?,?,?,?,?)",
                     (name, valid_from, value, now, author))
        store.write_audit(conn, now, author, "order_rate", {"name": name, "valid_from": valid_from, "from": old, "to": value})
        changed.append(name)
    return changed


# ---- the rates of one point (ТЗ 95, «Индивидуальные настройки») -------------------------------------------------------------------------

def list_point_rates(conn, point_id: Optional[int] = None) -> List[dict]:
    query = "SELECT r.point_id, COALESCE(p.name, ''), r.name, r.valid_from, r.value, r.created, r.author FROM point_rate r LEFT JOIN point p ON p.id = r.point_id"
    args: tuple = ()
    if point_id is not None:
        query, args = query + " WHERE r.point_id = ?", (int(point_id),)
    rows = conn.execute(query + " ORDER BY r.point_id, r.valid_from, r.name", args)
    return [dict(zip(("point_id", "point", "name", "valid_from", "value", "created", "author"), r)) for r in rows]


def _by_point(point_rates: List[dict]) -> Dict[int, List[dict]]:
    out: Dict[int, List[dict]] = {}
    for r in point_rates:
        out.setdefault(r["point_id"], []).append(r)
    return out


def own_rate(point_rates: List[dict], name: str, day: str) -> Optional[float]:
    """The point's own rate in force on `day` (`point_rates`: of that point); None — the general one (none saved, or set back to it)."""
    found = [r for r in point_rates if r["name"] == name and r["valid_from"] <= day]
    if not found:
        return None
    value = max(found, key=lambda r: r["valid_from"])["value"]
    return None if value is None else float(value)


def rate_for(rates: List[dict], by_point: Dict[int, List[dict]], key, name: str, day: str) -> float:
    """The rate of the point `key` (a key of the report, ("id", n), or an id) on `day`: its own rate when it has one in force, else the
    general one (rate_on). Its own rate wins over a later general one: it was set for this point on purpose."""
    point_id = key[1] if isinstance(key, tuple) and key and key[0] == "id" else key if isinstance(key, int) else None
    if point_id is not None and name in POINT_RATES:
        own = own_rate(by_point.get(point_id, []), name, day)
        if own is not None:
            return own
    return rate_on(rates, name, day)


def point_rates_now(conn, point_id: int, day: str) -> Dict[str, float]:
    """Every rate of a point on `day` (its own or the general one): the hints of its pages."""
    rates, by_point = list_rates(conn), _by_point(list_point_rates(conn, point_id))
    return {name: rate_for(rates, by_point, int(point_id), name, day) for name in POINT_RATES}


def set_point_rates(conn, point_id: int, valid_from: str, today: str, now: str, author: str, values: Dict[str, Optional[float]]) -> List[str]:
    """Save the own rates of a point from `valid_from` (today or later). `values`: name → a number, or None — back to the general rate;
    a name left out is not touched. Returns the names that changed; each is audited (action «point_rate»)."""
    if not conn.execute("SELECT 1 FROM point WHERE id = ?", (int(point_id),)).fetchone():
        raise ValueError("Такой точки нет.")
    valid_from = _valid_from(valid_from, today)
    for name, value in values.items():
        if name not in POINT_RATES:
            raise ValueError("Такой ставки у точки нет.")
        if value is not None:
            _check_rate(name, float(value))
    own = list_point_rates(conn, point_id)
    changed = []
    for name in POINT_RATES:
        if name not in values:
            continue
        value = values[name]
        value = None if value is None else round(float(value), 2)
        old = own_rate(own, name, valid_from)
        if value == old:
            continue
        conn.execute("INSERT OR REPLACE INTO point_rate (point_id, name, valid_from, value, created, author) VALUES (?,?,?,?,?,?)",
                     (int(point_id), name, valid_from, value, now, author))
        store.write_audit(conn, now, author, "point_rate", {"point": int(point_id), "name": name, "valid_from": valid_from, "from": old, "to": value})
        changed.append(name)
    return changed


BONUS_TO = "handed"                 # who gets the fixed sum for a handed-over client: "handed" — the point that handed it over (the owner 2026-10-23),
                                     # "issued" — the point that issued the order, as the 75 ₽


def bonus_point(issued: tuple, handed_by: Optional[tuple]) -> tuple:
    """The point (its key in the report) that gets the fixed sum for a handed-over client's issued order: the one place of that choice."""
    return (handed_by or issued) if BONUS_TO == "handed" else issued


def _point_key(row: dict) -> tuple:
    """A sale's point in the report: its id (a renamed point stays one line, the owner 2026-10-26), else the name written in the sale."""
    return ("id", row["point_id"]) if row.get("point_id") is not None else ("name", row["point"])


def _split(net: float, by_share: Dict[float, float]) -> tuple:
    """The owner's and the partner's parts of `net`; `by_share` is the net of the orders per owner share in force on their days."""
    if len(by_share) <= 1:
        owner = round(net * next(iter(by_share), OWNER_SHARE), 2)
    else:
        owner = round(sum(round(part * share, 2) for share, part in by_share.items()), 2)
    return owner, round(net - owner, 2)


def build(conn, start: str, end: str, point: str = "") -> dict:
    """Lines per point (most profitable first), the total and the manual sales (not in the total). Reversed sales are not counted."""
    rates = list_rates(conn)
    by_point = _by_point(list_point_rates(conn))
    month_start = start[:8] + "01" if start else ""
    # all points: a point's fixed sum may come from an order issued elsewhere; from the first day of the month of `start`: the KPI of the month
    every = sales_store.list_sales(conn, month_start, end)
    rows = _counted(conn, [r for r in every if not start or r["day"] >= start])
    names: Dict[tuple, str] = {}                                                        # point key → its name in the report
    handed = {}                                                                         # the clients the points handed over (order_manager.handoff)
    for order_id, point_id, name in conn.execute("SELECT h.order_id, p.id, p.name FROM order_handoff h LEFT JOIN point p ON p.id = h.point_id"):
        handed[order_id] = ("id", point_id) if point_id is not None else None
        if point_id is not None:
            names.setdefault(("id", point_id), name)
    for order_id, point_id, name in conn.execute("SELECT s.order_id, p.id, p.name FROM order_source_point s JOIN point p ON p.id = s.point_id"):
        if order_id not in handed:                                                      # «Откуда клиент» = a point (ТЗ 92): a handoff by phone
            handed[order_id] = ("id", point_id)
            names.setdefault(("id", point_id), name)
    from .order_merge import handed as merged_handed
    for new, source in merged_handed(conn).items():                                     # a merged order: once, if a source was handed over
        handed.setdefault(new, handed.get(source))
    from .oth_swap import fee_handed
    off, moved = fee_handed(conn)                                                       # the client changed on «На ОТХ» (oth_swap)
    for order_id, source in moved.items():
        if source in handed:
            handed[order_id] = handed[source]
    for order_id in off - set(moved):
        handed.pop(order_id, None)
    grouped: Dict[tuple, List[dict]] = {}
    for r in rows:
        if r["source"] == "order":
            key = _point_key(r)
            names[key] = names.get(key) or r["point"] or NO_POINT                       # rows come latest first: the name now, else the latest one written
            grouped.setdefault(key, []).append(r)
    per_point: Dict[tuple, Dict[str, List[dict]]] = {key: {} for key in grouped}
    bonuses: Dict[tuple, List[tuple]] = {}                                              # point → (share, sum) of the fixed sums it gets
    handoff_lines: Dict[tuple, dict] = {}
    oth_lines: Dict[tuple, dict] = {}
    payouts: Dict[tuple, Dict[str, dict]] = {}                                          # point → order → what it is paid for it (point_income)
    oth_sold = oth_sold_orders(conn, {_order_id(r["note"]) for items in grouped.values() for r in items} - {None})
    for key, items in grouped.items():
        for r in items:
            per_point[key].setdefault(r["note"], []).append(r)
        for note, parts in per_point[key].items():
            order_id = _order_id(note)
            day = min(r["day"] for r in parts)
            mine = _payout(payouts, key, note, day)
            mine["issue"] = rate_for(rates, by_point, key, "point_fee", day)
            if order_id in oth_sold:                                                    # the point that sold them, once per order, per tyre
                pieces = sum(r["pieces"] for r in parts)
                extra = round(rate_for(rates, by_point, key, "oth_sale_fee", day) * pieces, 2)
                bonuses.setdefault(key, []).append((rate_on(rates, "owner_share", day), extra))
                mine["oth"], mine["oth_pieces"] = extra, pieces
                got = oth_lines.setdefault(key, {"point": names[key], "orders": 0, "pieces": 0, "fee": 0.0})
                got["orders"] += 1
                got["pieces"] += pieces
                got["fee"] = round(got["fee"] + extra, 2)
            if order_id not in handed:
                continue
            to = bonus_point(key, handed[order_id])
            extra, pieces = handoff_fee(rates, by_point, to, day, sum(r["pieces"] for r in parts))      # on top of the 75 ₽
            bonuses.setdefault(to, []).append((rate_on(rates, "owner_share", day), extra))
            got = _payout(payouts, to, note, day)
            got["handoff"], got["handoff_pieces"] = extra, pieces
            got = handoff_lines.setdefault(to, {"point": names[to], "clients": 0, "fee": 0.0})
            got["clients"] += 1
            got["fee"] = round(got["fee"] + extra, 2)
    kpi, progress = _kpi(every, handed, rates, by_point, start, end)
    kpi_lines: Dict[tuple, dict] = {}
    for to, found in kpi.items():
        names.setdefault(to, NO_POINT)
        for premium in found:
            bonuses.setdefault(to, []).append((rate_on(rates, "owner_share", premium["day"]), premium["fee"]))
            got = kpi_lines.setdefault(to, {"point": names[to], "cycles": 0, "fee": 0.0})
            got["cycles"] += 1
            got["fee"] = round(got["fee"] + premium["fee"], 2)
    if point:                                                                           # one point: its orders and the fixed sums it gets
        grouped = {k: v for k, v in grouped.items() if names[k] == point}
        bonuses = {k: v for k, v in bonuses.items() if names[k] == point}
        handoff_lines = {k: v for k, v in handoff_lines.items() if names[k] == point}
        oth_lines = {k: v for k, v in oth_lines.items() if names[k] == point}
        payouts = {k: v for k, v in payouts.items() if names[k] == point}
        kpi = {k: v for k, v in kpi.items() if names[k] == point}
        kpi_lines = {k: v for k, v in kpi_lines.items() if names[k] == point}
        rows = [r for r in rows if r["point"] == point]
    lines, total_by_share, used, webmim = [], {}, set(), []
    for key in list(grouped) + [k for k in bonuses if k not in grouped]:               # a point paid only for the clients it handed over
        items = grouped.get(key, [])
        fee_sum, bonus_sum, by_share = 0.0, 0.0, {}
        for note, parts in per_point.get(key, {}).items():
            day = min(r["day"] for r in parts)
            fee, share = rate_for(rates, by_point, key, "point_fee", day), rate_on(rates, "owner_share", day)
            used.add((fee, share))
            fee_sum += fee
            by_share[share] = by_share.get(share, 0.0) + sum(r["margin_total"] for r in parts) - fee
        for share, extra in bonuses.get(key, []):
            bonus_sum += extra
            by_share[share] = by_share.get(share, 0.0) - extra
        margin = round(sum(r["margin_total"] for r in items), 2)
        fee = round(fee_sum + bonus_sum, 2)
        net = round(margin - fee, 2)
        owner, partner = _split(net, by_share) if by_share else (0.0, 0.0)
        for share, part in by_share.items():
            total_by_share[share] = total_by_share.get(share, 0.0) + part
        lines.append(dict(point=names[key], orders=len(per_point.get(key, {})), pieces=sum(r["pieces"] for r in items), sale=round(sum(r["sale_total"] for r in items), 2),
                          cost=round(sum(r["cost_total"] for r in items), 2), margin=margin, fee=fee, net=net, owner=owner, partner=partner,
                          _issue=round(fee_sum, 2), _bonus=round(bonus_sum, 2), _kpi=kpi_lines.get(key, {}).get("fee", 0.0)))
        webmim.append({"point": names[key], "count": sum(1 for r in items if r.get("webmim")), "positions": len(items)})
    lines.sort(key=lambda r: (-r["net"], r["point"]))
    fees = [dict(point=r["point"], orders=r["orders"], fee=r["fee"], issue=r.pop("_issue"), bonus=r.pop("_bonus"), kpi=r.pop("_kpi")) for r in lines]
    total = dict(point="Итого", orders=sum(r["orders"] for r in lines), pieces=sum(r["pieces"] for r in lines))
    for key in ("sale", "cost", "margin", "fee", "net"):
        total[key] = round(sum(r[key] for r in lines), 2)
    total["owner"], total["partner"] = _split(total["net"], total_by_share)
    manual_rows = [r for r in rows if r["source"] == "manual"]
    manual = None
    if manual_rows:
        manual = dict(point=MANUAL, sales=len(manual_rows), pieces=sum(r["pieces"] for r in manual_rows),
                      sale=round(sum(r["sale_total"] for r in manual_rows), 2), cost=round(sum(r["cost_total"] for r in manual_rows), 2),
                      margin=round(sum(r["margin_total"] for r in manual_rows), 2))
    handoff = sorted(handoff_lines.values(), key=lambda r: r["point"])
    webmim.sort(key=lambda r: r["point"])
    return {"lines": lines, "total": total, "manual": manual, "rates_used": sorted(used),
            "webmim": {"count": sum(r["count"] for r in webmim), "positions": sum(r["positions"] for r in webmim), "by_point": webmim},
            "handoff": {"lines": handoff, "clients": sum(r["clients"] for r in handoff), "fee": round(sum(r["fee"] for r in handoff), 2)},
            "oth": {"lines": sorted(oth_lines.values(), key=lambda r: r["point"]), "orders": sum(r["orders"] for r in oth_lines.values()),
                    "pieces": sum(r["pieces"] for r in oth_lines.values()), "fee": round(sum(r["fee"] for r in oth_lines.values()), 2)},
            "fees": {"lines": fees, "issue": round(sum(f["issue"] for f in fees), 2), "bonus": round(sum(f["bonus"] for f in fees), 2),   # the fee: for
                     "kpi": round(sum(f["kpi"] for f in fees), 2)},             # the issued orders and the bonuses (ТЗ 80; KPI is a bonus), line by line
            "kpi": {"lines": sorted(kpi_lines.values(), key=lambda r: r["point"]), "cycles": sum(r["cycles"] for r in kpi_lines.values()),
                    "fee": round(sum(r["fee"] for r in kpi_lines.values()), 2),
                    "premiums": kpi, "progress": progress},                     # point → its premiums of the period; point → month → the count
            "payouts": {key: sorted(found.values(), key=lambda e: (e["day"], e["order"] or 0)) for key, found in payouts.items()}}


def handoff_fee(rates: List[dict], by_point: Dict[int, List[dict]], to, day: str, pieces: int) -> tuple:
    """(the bonus, the tyres it is paid for) of a handed-over client's order issued on `day` with `pieces` tyres: handoff_fee a tyre from
    HANDOFF_PER_TYRE_FROM (ТЗ 92: «1 шина = 50 ₽, комплект 200 ₽»), before it once per order (tyres 0). The one place of that rule."""
    fee = rate_for(rates, by_point, to, "handoff_fee", day)
    if day >= HANDOFF_PER_TYRE_FROM:
        return round(fee * pieces, 2), pieces
    return fee, 0


def source_point(conn, order_id) -> Optional[int]:
    """The point set as «Откуда клиент» of the order (ТЗ 92), else None."""
    row = conn.execute("SELECT point_id FROM order_source_point WHERE order_id = ?", (int(order_id),)).fetchone()
    return row[0] if row else None


def handed_point(conn, order_id) -> Optional[int]:
    """The point that handed the client of the order over: its handoff (order_handoff), else «Откуда клиент» (ТЗ 92); None — none."""
    row = conn.execute("SELECT point_id FROM order_handoff WHERE order_id = ?", (int(order_id),)).fetchone()
    if row and row[0] is not None:
        return row[0]
    return source_point(conn, order_id)


def kpi_item(month: str, cycle: int) -> str:
    return "kpi:%s:%d" % (month, cycle)


def _kpi(every: List[dict], handed: dict, rates: List[dict], by_point: Dict[int, List[dict]], start: str, end: str) -> tuple:
    """The KPI of the handing points (ТЗ 95), from the sales `every` (the whole month of `start` up to `end`): ({point key: [{month, cycle,
    day, fee, item}]} — the premiums whose cycle closed within start..end; {point key: {month: {orders, tyres, cycles, need_orders,
    need_tyres, bonus, fee}}} — the count of each month up to `end`). An order is closed on the day of its first sale (not reversed)."""
    closed: Dict[tuple, Dict[str, dict]] = {}                                   # point → order note → {day, tyres}
    for r in every:
        if r["source"] != "order":
            continue
        order_id = _order_id(r["note"])
        to = handed.get(order_id)
        if to is None:
            continue
        got = closed.setdefault(to, {}).setdefault(r["note"], {"order": order_id, "day": r["day"], "tyres": 0})
        got["day"] = min(got["day"], r["day"])
        got["tyres"] += r["pieces"]
    premiums: Dict[tuple, List[dict]] = {}
    progress: Dict[tuple, Dict[str, dict]] = {}
    for to, found in closed.items():
        months: Dict[str, List[dict]] = {}
        for entry in found.values():
            months.setdefault(entry["day"][:7], []).append(entry)
        for month, entries in sorted(months.items()):
            first = month + "-01"
            need_orders = int(rate_for(rates, by_point, to, "kpi_orders", first))
            need_tyres = int(rate_for(rates, by_point, to, "kpi_tyres", first))
            bonus = rate_for(rates, by_point, to, "kpi_bonus", first)
            count = {"orders": 0, "tyres": 0, "cycles": 0, "need_orders": need_orders, "need_tyres": need_tyres, "bonus": bonus, "fee": 0.0}
            for entry in sorted(entries, key=lambda e: (e["day"], e["order"] or 0)):
                count["orders"] += 1
                count["tyres"] += entry["tyres"]
                cycles = min(count["orders"] // max(need_orders, 1), count["tyres"] // max(need_tyres, 1))
                while count["cycles"] < cycles:
                    count["cycles"] += 1
                    if bonus and (not start or entry["day"] >= start):
                        premiums.setdefault(to, []).append({"month": month, "cycle": count["cycles"], "day": entry["day"], "fee": bonus,
                                                            "item": kpi_item(month, count["cycles"])})
            count["fee"] = round(count["cycles"] * bonus, 2)
            progress.setdefault(to, {})[month] = count
    return premiums, progress


def _payout(payouts: Dict[tuple, Dict[str, dict]], key: tuple, note: str, day: str) -> dict:
    """The line of the order `note` in what the point `key` is paid (made empty: no sum yet)."""
    return payouts.setdefault(key, {}).setdefault(note, {"order": _order_id(note), "day": day, "issue": 0.0, "handoff": 0.0, "handoff_pieces": 0,
                                                         "oth": 0.0, "oth_pieces": 0})


def oth_sold_orders(conn, order_ids) -> set:
    """Those of `order_ids` the point sold from «На ОТХ» (OTH_SOLD_EVENT in their journal): one query, by the index of the journal."""
    ids = sorted({int(i) for i in order_ids})
    found = set()
    for chunk in range(0, len(ids), 500):
        part = ids[chunk:chunk + 500]
        found |= {r[0] for r in conn.execute(
            "SELECT DISTINCT order_id FROM order_event WHERE order_id IN (%s) AND kind = 'point' AND (text = ? OR text LIKE ?)"
            % ",".join("?" * len(part)), part + [OTH_SOLD_EVENT, OTH_SOLD_EVENT + ":%"])}
    return found


INCOME_SHOWN_KEY = "point_income_shown"   # app_state «1»: the points see «Доход» (the owner's switch; off until the owner checks the sums)


def income_shown(conn) -> bool:
    row = conn.execute("SELECT value FROM app_state WHERE key = ?", (INCOME_SHOWN_KEY,)).fetchone()
    return bool(row) and row[0] == "1"


def set_income_shown(conn, on: bool, now: str, author: str) -> bool:
    """The switch «Показывать точкам страницу дохода»; audited. True if it changed."""
    if income_shown(conn) == bool(on):
        return False
    if on:
        conn.execute("INSERT OR REPLACE INTO app_state (key, value) VALUES (?, '1')", (INCOME_SHOWN_KEY,))
    else:
        conn.execute("DELETE FROM app_state WHERE key = ?", (INCOME_SHOWN_KEY,))
    store.write_audit(conn, now, author, "point_income_shown", {"on": bool(on)})
    return True


def point_income(conn, start: str, end: str, point_id: int) -> dict:
    """What the point `point_id` is paid for the period, order by order, from `build` (the one count of the report, the analytics and the
    point's page): {"orders": [{order, day, issue, handoff, oth, oth_pieces, total}], "issue", "handoff", "oth", "bonus", "total"}. The rates
    and the sums only: nothing of the purchase, the profit or the prices."""
    report = build(conn, start, end)
    key = ("id", int(point_id))
    entries = [dict(e, total=round(e["issue"] + e["handoff"] + e["oth"], 2), item="order:%d" % e["order"] if e["order"] else "")
               for e in report["payouts"].get(key, [])]
    out = {"orders": entries, "premiums": report["kpi"]["premiums"].get(key, []), "progress": report["kpi"]["progress"].get(key, {})}
    for name in ("issue", "handoff", "oth"):
        out[name] = round(sum(e[name] for e in entries), 2)
    out["kpi"] = round(sum(p["fee"] for p in out["premiums"]), 2)
    out["bonus"] = round(out["handoff"] + out["oth"], 2)
    out["total"] = round(sum(e["total"] for e in entries) + out["kpi"], 2)
    return out


def _counted(conn, rows: List[dict]) -> List[dict]:
    """The sales as the analytics counts them: those of the orders from WEBMIM_FROM on at WebMIM's price where there is one (the one rule of
    the report and of the order's page)."""
    from . import oth_sale
    from_oth = oth_sale.sale_ids(conn)                     # the tyres taken from «На ОТХ»: at the old order's purchase, as written

    def takes(r):
        return r["source"] == "order" and r["day"] >= WEBMIM_FROM and r.get("id") not in from_oth
    prices = webmim_prices(conn, {_order_id(r["note"]) for r in rows if takes(r)} - {None})
    return [_at_webmim(r, prices.get(_order_id(r["note"]), {}).get(str(r["goods_id"]))) if takes(r) else r for r in rows]


def order_webmim(conn, card_id: int) -> Optional[dict]:
    """The order's page: {cost, count, positions} — the purchase of its sales that the analytics counts at WebMIM's price; None when none of
    them is (no sale written yet, no pair, no price, a sale before WEBMIM_FROM)."""
    names = [c.strip() for c in sales_store.COLUMNS.split(",")]
    rows = [dict(zip(names, r)) for r in conn.execute("SELECT %s FROM sale WHERE source = 'order' AND note = ? AND reversed_at IS NULL ORDER BY id"
                                                      % sales_store.COLUMNS, ("Заказ №%d" % int(card_id),))]
    taken = [r for r in _counted(conn, rows) if r.get("webmim")]
    if not taken:
        return None
    return {"cost": round(sum(r["cost_total"] for r in taken), 2), "count": len(taken), "positions": len(rows)}


def webmim_prices(conn, card_ids) -> Dict[int, Dict[str, float]]:
    """Order of the panel → goodsId → WebMIM's price of one, for the orders paired with an order of WebMIM on the prod server (read only).
    A position without a price, with a price below a kopeck, or with two different prices is left out: its sale counts as written. An order of
    WebMIM paired with two orders of the panel (`webmim_match.shared`: a number typed twice) gives no price: whose it is, is not known."""
    if not card_ids:
        return {}
    snap = webmim_match.snapshot(conn, WEBMIM_SERVER)
    by_id, by_number = {o["id"]: o for o in snap}, {o["number"]: o for o in snap if o["number"]}
    pairs: Dict[str, List[int]] = {}
    orders: Dict[int, dict] = {}
    elsewhere = webmim_match.made_elsewhere(conn, WEBMIM_SERVER)           # a test order made by the button is never a live one
    from . import oth_sale
    elsewhere |= oth_sale.borrowed_ids(conn)                               # the number of tyres from OTH: the WebMIM order is the first order's
    for cid, webmim_id, webmim_number in conn.execute("SELECT id, webmim_id, webmim_number FROM order_card WHERE webmim_id != '' OR webmim_number != ''"):
        if cid in elsewhere:
            continue
        order = webmim_match.paired({"webmim_id": webmim_id, "webmim_number": webmim_number}, by_id, by_number)
        if order is not None:
            pairs.setdefault(order["id"], []).append(cid)
            orders[cid] = order
    out: Dict[int, Dict[str, float]] = {}
    for cid, order in orders.items():
        if cid not in card_ids or len(pairs[order["id"]]) > 1:
            continue
        seen: Dict[str, set] = {}
        for row in order.get("items") or []:
            price = row.get("price")
            ok = isinstance(price, (int, float)) and not isinstance(price, bool) and math.isfinite(price)
            price = round(float(price), 2) if ok else None
            seen.setdefault(str(row.get("goods_id")), set()).add(price if price and price > 0 else None)
        found = {goods: next(iter(got)) for goods, got in seen.items() if len(got) == 1 and None not in got}
        if found:
            out[cid] = found
    return out


def _at_webmim(row: dict, price: Optional[float]) -> dict:
    """The sale counted at WebMIM's price of one (a copy; the sale itself is never changed). The price of WebMIM is the supplier's price before
    the discount, as in the unload (the owner 2026-10-22): the terms are linear in it, so the purchase, the bonus and ЕЖ scale with it; the
    revenue, the commission and the promotion do not depend on it."""
    if price is None or not row["price_total"] or not row["pieces"]:
        return row
    out = dict(row, webmim=True)
    k = price * row["pieces"] / row["price_total"]
    out["price_total"] = round(price * row["pieces"], 2)
    out["cost_total"], out["bonus_total"], out["monthly_total"] = (round(row[key] * k, 2) for key in ("cost_total", "bonus_total", "monthly_total"))
    out["margin_total"] = round(row["margin_total"] - (out["cost_total"] - row["cost_total"]) + (out["bonus_total"] - row["bonus_total"])
                                + (out["monthly_total"] - row["monthly_total"]), 2)
    return out


def _order_id(note: str) -> Optional[int]:
    found = re.match(r"^Заказ №(\d+)$", str(note or ""))
    return int(found.group(1)) if found else None


def saved_share_labels(conn) -> Dict[str, str]:
    """{"owner", "partner"} as the owner wrote them (an empty one: not written)."""
    row = conn.execute("SELECT value FROM app_state WHERE key = ?", (SHARE_LABELS_KEY,)).fetchone()
    try:
        saved = json.loads(row[0]) if row else {}
    except ValueError:
        saved = {}
    saved = saved if isinstance(saved, dict) else {}
    return {role: _label(saved.get(role)) for role in SHARE_LABELS}


def set_share_labels(conn, owner: str, partner: str, now: str, author: str) -> bool:
    """Write the labels (an empty one falls back to the names of the users, then to SHARE_LABELS); audited. True if anything changed."""
    old, new = saved_share_labels(conn), {"owner": _label(owner), "partner": _label(partner)}
    if new == old:
        return False
    if any(new.values()):
        conn.execute("INSERT OR REPLACE INTO app_state (key, value) VALUES (?, ?)", (SHARE_LABELS_KEY, json.dumps(new, ensure_ascii=False)))
    else:
        conn.execute("DELETE FROM app_state WHERE key = ?", (SHARE_LABELS_KEY,))
    store.write_audit(conn, now, author, "share_labels", {"from": old, "to": new})
    return True


def share_labels(conn) -> Dict[str, str]:
    """{"owner", "partner"}: who the two shares of the net profit go to, for the people. The label the owner wrote, else the display names of
    the active users with the role (several joined by «, »; a user made before the roles is an owner), else SHARE_LABELS. A label only: the
    split itself is the rates'."""
    names: Dict[str, List[str]] = {"owner": [], "partner": []}
    for user in users.list_users(conn):
        if user["active"] and user["role"] in names and user["display_name"].strip():
            names[user["role"]].append(user["display_name"].strip())
    saved = saved_share_labels(conn)
    return {role: saved[role] or ", ".join(found) or SHARE_LABELS[role] for role, found in names.items()}


def _label(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:SHARE_LABEL_MAX]


def export_csv(report: dict, labels: Optional[Dict[str, str]] = None) -> str:
    import csv
    import io
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";", lineterminator="\n")
    labels = labels or SHARE_LABELS
    writer.writerow(["Точка", "Заказов", "Шин", "Выручка", "Закупка", "Прибыль", "Выплата точке", "Чистая прибыль", sales_store._safe(labels["owner"]), sales_store._safe(labels["partner"])])
    for r in report["lines"] + [report["total"]]:
        writer.writerow([sales_store._safe(r["point"]), r["orders"], r["pieces"]] + [sales_store._money(r[k]) for k in ("sale", "cost", "margin", "fee", "net", "owner", "partner")])
    handoff = report.get("handoff") or {}
    if handoff.get("clients"):
        writer.writerow(["Переданные клиенты (входят в строки выше)", handoff["clients"], "", "", "", "", sales_store._money(handoff["fee"]), "", "", ""])
    manual = report.get("manual")
    if manual:
        writer.writerow(["%s (не входят в итог)" % MANUAL, "", manual["pieces"]] + [sales_store._money(manual[k]) for k in ("sale", "cost", "margin")]
                        + ["", "", "", ""])
    webmim = report.get("webmim") or {}
    if webmim.get("count"):                                    # the purchase of these positions is WebMIM's, not the one the sales wrote
        writer.writerow(["Закупка из заказов WebMIM (боевой сервер), позиций", "%d из %d" % (webmim["count"], webmim["positions"]), "", "", "", "", "", "", "", ""])
    return "﻿" + out.getvalue()
