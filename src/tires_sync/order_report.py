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

from . import order_oth, point_arrival, sales as sales_store, store, users, webmim_match

POINT_FEE = 75.0                     # rubles per order, paid to the point (the default: before the first dated rate)
OWNER_SHARE = 0.5                    # of the net profit; the rest is the partner's (the default)
HANDOFF_FEE = 50.0                   # rubles to the point for an issued order of a client it handed over, on top of the 75 (the owner 2026-10-23, п. 24)
# the owner 2026-11-25: the bonuses (a handed-over client, a tyre sold from «На ОТХ») and the KPI start on 2026-12-01, whatever rate is
# saved (a general one or a point's own): the points got one sum each for the time before, entered by the office as a «Корректировка»
BONUS_FROM = "2026-12-01"
HANDOFF_FEE_FROM = BONUS_FROM        # the handed clients' orders of earlier days get 0
OTH_SALE_FEE = 50.0                  # rubles to the point for every tyre it sold from «На ОТХ» (ТЗ 80; «+50 ₽ за каждую проданную шину» of the window)
OTH_SALE_FEE_FROM = BONUS_FROM       # the sales of earlier days get 0
# ТЗ 92 / 95 (the owner 2026-11-24: «50 ₽ за каждую шину», «+200 ₽ за комплект»): the bonus of a handed-over client is handoff_fee for
# every tyre of the issued order
HANDOFF_PER_TYRE_FROM = BONUS_FROM
# the owner 2026-11-25: the fee of the issue (point_fee, 75 ₽) is for every tyre of the order (a set of 4 — 300 ₽), from this day; the
# orders of earlier days keep it per order (a past report is never recounted)
POINT_FEE_PER_TYRE_FROM = "2026-11-27"
# KPI (ТЗ 95): in a calendar month, for every full cycle of kpi_orders closed orders AND kpi_tyres tyres of the clients the point handed
# over, the premium kpi_bonus; the cycles are not limited: min(orders // kpi_orders, tyres // kpi_tyres) premiums (20 / 76 → 2). The rates
# of a month are those in force on its first day. The premium is the point's on the day of the order that closes the cycle.
KPI_ORDERS, KPI_TYRES, KPI_BONUS = 10, 38, 1500.0
KPI_FROM = BONUS_FROM                # the first whole month: no premium of a month before, whatever rate is saved
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
        raise ValueError("Выплата точке за выдачу: от 0 до 100 000 ₽.")
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


HANDED_SLICES = {"": "Все заказы", "only": "Только переданные точкой", "none": "Без переданных точкой"}     # the slice of the report (ТЗ 95)


def handed_points(conn) -> tuple:
    """({order: the point key that handed its client over, or None — a point gone}, {point key: its name}): the one rule of «чей клиент» of
    the report, the KPI and the row of the table (order_sheet): the handoff (order_handoff), else «Откуда клиент» = a point (ТЗ 92); a merged
    order takes it from a handed source (order_merge.handed); a client changed on «На ОТХ» moves it or takes it off (oth_swap.fee_handed)."""
    names: Dict[tuple, str] = {}
    handed: Dict[int, Optional[tuple]] = {}
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
    return handed, names


def build(conn, start: str, end: str, point: str = "", handed_slice: str = "", handed_by: Optional[int] = None) -> dict:
    """Lines per point (most profitable first), the total and the manual sales (not in the total). Reversed sales are not counted.
    `handed_slice` (ТЗ 95): "only" — the orders of the clients a point handed over (of the point `handed_by`, if given), with their KPI;
    "none" — the others, without the handed clients' bonuses and the KPI; "" — all."""
    rates = list_rates(conn)
    by_point = _by_point(list_point_rates(conn))
    # all points: a point's fixed sum may come from an order issued elsewhere
    rows = _counted(conn, sales_store.list_sales(conn, start, end))
    handed, names = handed_points(conn)                                                 # point key → its name in the report too

    def kept_order(order_id) -> bool:
        by = handed.get(order_id)
        if handed_slice == "none":
            return by is None
        if handed_slice == "only":
            return by is not None and (handed_by is None or by == ("id", int(handed_by)))
        return True
    if handed_slice in ("only", "none"):
        rows = [r for r in rows if kept_order(_order_id(r["note"]) if r["source"] == "order" else None)]
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
    paid_oth = point_arrival.paid_oth_pieces(conn, {_order_id(r["note"]) for items in grouped.values() for r in items} - {None})
    fixed = fixes(conn)
    point_names = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM point")}
    issues: Dict[tuple, List[tuple]] = {}                                               # point → (share, sum) of its fees for the issue
    adjs: Dict[tuple, List[tuple]] = {}                                                 # point → (share, sum) of the office's corrections

    def fixed_parts(key: tuple, note: str, entry: dict, parts) -> Dict[str, float]:
        return _fix(entry, fixed.get((key[1], "order:%d" % entry["order"])) if key[0] == "id" and entry["order"] else None, parts)
    for key, items in grouped.items():
        for r in items:
            per_point[key].setdefault(r["note"], []).append(r)
        for note, parts in per_point[key].items():
            order_id = _order_id(note)
            day = min(r["day"] for r in parts)
            got = fee_parts(rates, by_point, key, day, sum(r["pieces"] for r in parts), order_id in handed, handed.get(order_id),
                            order_id in oth_sold, paid_oth.get(order_id, 0))
            mine = _payout(payouts, key, note, day)
            mine["issue"], mine["issue_pieces"], mine["oth"], mine["oth_pieces"] = got["issue"], got["issue_pieces"], got["oth"], got["oth_pieces"]
            mine["oth_sold"] = order_id in oth_sold
            if got["handoff_to"] is not None:
                theirs = _payout(payouts, got["handoff_to"], note, day)
                theirs["handoff"], theirs["handoff_pieces"], theirs["handed"] = got["handoff"], got["handoff_pieces"], True
    for found in point_arrival.unsold(conn, start, end):                               # the tyres the point got, no sale (point_arrival)
        if not kept_order(found["order"]):
            continue
        key = ("id", found["point_id"])
        names.setdefault(key, point_names.get(found["point_id"]) or NO_POINT)
        mine = _payout(payouts, key, "Заказ №%d" % found["order"], found["day"])
        mine["issue"], mine["issue_pieces"] = issue_fee(rates, by_point, key, found["day"], found["pieces"])
        mine["arrived"] = True
    for key, found in payouts.items():                                                  # the sums the office set, then the lines of the count
        for note, entry in found.items():
            share = rate_on(rates, "owner_share", entry["day"])
            fixed_parts(key, note, entry, ("issue", "handoff", "oth"))
            issues.setdefault(key, []).append((share, entry["issue"]))
            if entry.get("oth_sold") or entry["oth"]:
                bonuses.setdefault(key, []).append((share, entry["oth"]))
                got = oth_lines.setdefault(key, {"point": names[key], "orders": 0, "pieces": 0, "fee": 0.0})
                got["orders"] += 1
                got["pieces"] += sum(r["pieces"] for r in per_point.get(key, {}).get(note, [])) if entry.get("oth_sold") else entry["oth_pieces"]
                got["fee"] = round(got["fee"] + entry["oth"], 2)
            if entry.get("handed") or entry["handoff"]:
                bonuses.setdefault(key, []).append((share, entry["handoff"]))
                got = handoff_lines.setdefault(key, {"point": names[key], "clients": 0, "fee": 0.0})
                got["clients"] += 1
                got["fee"] = round(got["fee"] + entry["handoff"], 2)
    kpi, progress = _kpi(conn, handed, rates, by_point, start, end)
    if handed_slice == "none":
        kpi = {}
    elif handed_slice == "only" and handed_by is not None:
        kpi = {k: v for k, v in kpi.items() if k == ("id", int(handed_by))}
    kpi_lines: Dict[tuple, dict] = {}
    for to, found in kpi.items():
        names.setdefault(to, NO_POINT)
        for premium in found:
            premium["kpi"] = premium["fee"]
            _fix(premium, fixed.get((to[1], premium["item"])) if to[0] == "id" else None, ("kpi",))
            premium["fee"] = premium["kpi"]
            bonuses.setdefault(to, []).append((rate_on(rates, "owner_share", premium["day"]), premium["fee"]))
            got = kpi_lines.setdefault(to, {"point": names[to], "cycles": 0, "fee": 0.0})
            got["cycles"] += 1
            got["fee"] = round(got["fee"] + premium["fee"], 2)
    adjust: Dict[tuple, List[dict]] = {}                                                # the office's corrections of the period (point_payouts.adjust)
    if not handed_slice:
        for adj_id, pid, day, amount, reason in conn.execute("SELECT id, point_id, day, amount, reason FROM point_adjust WHERE day >= ? AND day <= ? "
                                                             "ORDER BY id", (start, end)):
            if pid not in point_names:
                continue
            key = ("id", pid)
            names.setdefault(key, point_names[pid])
            entry = {"id": adj_id, "item": "adj:%d" % adj_id, "day": day, "adj": float(amount), "reason": reason}
            _fix(entry, fixed.get((pid, entry["item"])), ("adj",))
            adjust.setdefault(key, []).append(entry)
            adjs.setdefault(key, []).append((rate_on(rates, "owner_share", day), entry["adj"]))
    if point:                                                                           # one point: its orders and the fixed sums it gets
        grouped = {k: v for k, v in grouped.items() if names[k] == point}
        bonuses = {k: v for k, v in bonuses.items() if names[k] == point}
        handoff_lines = {k: v for k, v in handoff_lines.items() if names[k] == point}
        oth_lines = {k: v for k, v in oth_lines.items() if names[k] == point}
        payouts = {k: v for k, v in payouts.items() if names[k] == point}
        kpi = {k: v for k, v in kpi.items() if names[k] == point}
        kpi_lines = {k: v for k, v in kpi_lines.items() if names[k] == point}
        issues = {k: v for k, v in issues.items() if names[k] == point}
        adjs = {k: v for k, v in adjs.items() if names[k] == point}
        adjust = {k: v for k, v in adjust.items() if names[k] == point}
        rows = [r for r in rows if r["point"] == point]
    lines, total_by_share, used, webmim = [], {}, set(), []
    keys = list(grouped) + [k for k in list(issues) + list(bonuses) + list(adjs) if k not in grouped]
    for key in sorted(set(keys), key=keys.index):                                       # a point paid only for the clients it handed over,
        items = grouped.get(key, [])                                                    # the tyres it got, a correction
        fee_sum, bonus_sum, adj_sum, by_share = 0.0, 0.0, 0.0, {}
        for note, parts in per_point.get(key, {}).items():
            day = min(r["day"] for r in parts)
            share = rate_on(rates, "owner_share", day)
            used.add((rate_for(rates, by_point, key, "point_fee", day), share))   # the rate, not the sum of the order
            by_share[share] = by_share.get(share, 0.0) + sum(r["margin_total"] for r in parts)
        for e in payouts.get(key, {}).values():
            if e.get("arrived"):
                used.add((rate_for(rates, by_point, key, "point_fee", e["day"]), rate_on(rates, "owner_share", e["day"])))
        for share, fee in issues.get(key, []):
            fee_sum += fee
            by_share[share] = by_share.get(share, 0.0) - fee
        for share, extra in bonuses.get(key, []):
            bonus_sum += extra
            by_share[share] = by_share.get(share, 0.0) - extra
        for share, extra in adjs.get(key, []):
            adj_sum += extra
            by_share[share] = by_share.get(share, 0.0) - extra
        margin = round(sum(r["margin_total"] for r in items), 2)
        fee = round(fee_sum + bonus_sum + adj_sum, 2)
        net = round(margin - fee, 2)
        owner, partner = _split(net, by_share) if by_share else (0.0, 0.0)
        for share, part in by_share.items():
            total_by_share[share] = total_by_share.get(share, 0.0) + part
        lines.append(dict(point=names[key], orders=len(per_point.get(key, {})), pieces=sum(r["pieces"] for r in items), sale=round(sum(r["sale_total"] for r in items), 2),
                          cost=round(sum(r["cost_total"] for r in items), 2), margin=margin, fee=fee, net=net, owner=owner, partner=partner,
                          _issue=round(fee_sum, 2), _bonus=round(bonus_sum, 2), _kpi=kpi_lines.get(key, {}).get("fee", 0.0), _adj=round(adj_sum, 2)))
        webmim.append({"point": names[key], "count": sum(1 for r in items if r.get("webmim")), "positions": len(items)})
    lines.sort(key=lambda r: (-r["net"], r["point"]))
    fees = [dict(point=r["point"], orders=r["orders"], fee=r["fee"], issue=r.pop("_issue"), bonus=r.pop("_bonus"), kpi=r.pop("_kpi"), adj=r.pop("_adj"))
            for r in lines]
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
                     "kpi": round(sum(f["kpi"] for f in fees), 2),              # the issued orders and the bonuses (ТЗ 80; KPI is a bonus), line by line
                     "adj": round(sum(f["adj"] for f in fees), 2)},             # the office's corrections (point_adjust), in the fee too
            "kpi": {"lines": sorted(kpi_lines.values(), key=lambda r: r["point"]), "cycles": sum(r["cycles"] for r in kpi_lines.values()),
                    "fee": round(sum(r["fee"] for r in kpi_lines.values()), 2),
                    "premiums": kpi, "progress": progress},                     # point → its premiums of the period; point → month → the count
            "payouts": {key: sorted(found.values(), key=lambda e: (e["day"], e["order"] or 0)) for key, found in payouts.items()},
            "adjust": adjust}                                                   # point → the corrections of the period, the sums set by the office


def issue_fee(rates: List[dict], by_point: Dict[int, List[dict]], key, day: str, pieces: int) -> tuple:
    """(the fee, the tyres it is paid for) of an order the point `key` issued on `day` with `pieces` tyres: point_fee a tyre from
    POINT_FEE_PER_TYRE_FROM (the owner 2026-11-25: «75 ₽ за шину, комплект 300 ₽»), before it once per order (tyres 0). The one place."""
    fee = rate_for(rates, by_point, key, "point_fee", day)
    if day >= POINT_FEE_PER_TYRE_FROM:
        return round(fee * pieces, 2), pieces
    return fee, 0


def handoff_fee(rates: List[dict], by_point: Dict[int, List[dict]], to, day: str, pieces: int) -> tuple:
    """(the bonus, the tyres it is paid for) of a handed-over client's order issued on `day` with `pieces` tyres: handoff_fee a tyre from
    HANDOFF_PER_TYRE_FROM (ТЗ 92: «1 шина = 50 ₽, комплект 200 ₽»), before it once per order (tyres 0). The one place of that rule."""
    if day < BONUS_FROM:                                        # the owner 2026-11-25: no bonus before, whatever rate is saved
        return 0.0, 0
    fee = rate_for(rates, by_point, to, "handoff_fee", day)
    if day >= HANDOFF_PER_TYRE_FROM:
        return round(fee * pieces, 2), pieces
    return fee, 0


def oth_fee(rates: List[dict], by_point: Dict[int, List[dict]], key, day: str, pieces: int) -> tuple:
    """(the bonus, the tyres) of the tyres the point `key` sold from «На ОТХ» on `day`: oth_sale_fee a tyre from BONUS_FROM. The one place."""
    if day < BONUS_FROM:
        return 0.0, 0
    return round(rate_for(rates, by_point, key, "oth_sale_fee", day) * pieces, 2), pieces


def fee_parts(rates: List[dict], by_point: Dict[int, List[dict]], key, day: str, pieces: int, handed: bool = False, handed_to=None,
              oth_sold: bool = False, paid_before: int = 0) -> dict:
    """What the points get for one order issued by `key` on `day` with `pieces` tyres — the one formula of the report, the ledger and the
    row of the Google table: {issue, issue_pieces (to `key`), oth, oth_pieces (to `key`, a sale from «На ОТХ»), handoff, handoff_pieces,
    handoff_to (the point of a handed-over client, bonus_point; None — none)}."""
    out = {"handoff": 0.0, "handoff_pieces": 0, "handoff_to": None, "oth": 0.0, "oth_pieces": 0}
    # `paid_before`: the tyres taken from «На ОТХ» whose arrival paid the issue already (point_arrival.paid_oth_pieces): not again
    out["issue"], out["issue_pieces"] = issue_fee(rates, by_point, key, day, max(pieces - paid_before, 0)) if pieces > paid_before or not paid_before \
        else (0.0, 0)
    if oth_sold:
        out["oth"], out["oth_pieces"] = oth_fee(rates, by_point, key, day, pieces)
    if handed:                                                  # handed_to None: the point is gone, the issuing one gets it (bonus_point)
        out["handoff_to"] = bonus_point(key, handed_to)
        out["handoff"], out["handoff_pieces"] = handoff_fee(rates, by_point, out["handoff_to"], day, pieces)
    return out


LEGACY_STARTS = {"handoff_fee": "2026-10-24", "oth_sale_fee": "2026-11-25"}     # the starts of the bonuses before the owner's 2026-11-25
LEGACY_PER_TYRE_FROM = "2026-11-27"


def _legacy_rate(rates: List[dict], by_point: Dict[int, List[dict]], key, name: str, day: str) -> float:
    point_id = key[1] if isinstance(key, tuple) and key and key[0] == "id" else None
    own = own_rate(by_point.get(point_id, []), name, day) if point_id is not None else None
    if own is not None:
        return own
    found = [r for r in rates if r["name"] == name and r["valid_from"] <= day]
    if found:
        return float(max(found, key=lambda r: r["valid_from"])["value"])
    return DEFAULTS[name] if day >= LEGACY_STARTS[name] else 0.0


def legacy_fee(rates: List[dict], by_point: Dict[int, List[dict]], key, day: str, pieces: int, handed: bool = False, handed_to=None,
               oth_sold: bool = False) -> float:
    """What an order sold on `day` cost in fixed sums by the rules before the owner's 2026-11-25 (the bonuses from LEGACY_STARTS, a handed
    client per order before LEGACY_PER_TYRE_FROM): only the rows of the Google table already written keep it (order_sheet.KEEPS_BEFORE)."""
    fee = issue_fee(rates, by_point, key, day, pieces)[0]
    if oth_sold:
        fee += round(_legacy_rate(rates, by_point, key, "oth_sale_fee", day) * pieces, 2)
    if handed:
        rate = _legacy_rate(rates, by_point, bonus_point(key, handed_to), "handoff_fee", day)
        fee += round(rate * pieces, 2) if day >= LEGACY_PER_TYRE_FROM else rate
    return round(fee, 2)


# ---- the sums the office set by hand (point_item_fix, the owner 2026-11-25) ------------------------------------------------------------------

FIX_PARTS = {"issue": "за выдачу", "handoff": "переданный клиент", "oth": "с ОТХ", "kpi": "KPI", "adj": "корректировка"}


def fixes(conn) -> Dict[tuple, Dict[str, dict]]:
    """(point id, item) → part → {amount, reason, at, author, id}: the last sum the office set for an article of an item, not taken back."""
    out: Dict[tuple, Dict[str, dict]] = {}
    for fid, pid, item, part, amount, reason, at, author in conn.execute(
            "SELECT id, point_id, item, part, amount, reason, at, author FROM point_item_fix WHERE undone_at IS NULL ORDER BY id"):
        out.setdefault((pid, item), {})[part] = {"id": fid, "amount": float(amount), "reason": reason, "at": at, "author": author}
    return out


def _fix(entry: dict, found: Optional[Dict[str, dict]], parts) -> Dict[str, float]:
    """Set the parts of `entry` the office fixed; returns part → (the sum set − the counted one). The counted sums stay in entry["counted"]."""
    delta: Dict[str, float] = {}
    if not found:
        return delta
    for part in parts:
        if part in found:
            entry.setdefault("counted", {})[part] = entry.get(part, 0.0)
            entry.setdefault("fixed", {})[part] = found[part]
            delta[part] = round(found[part]["amount"] - entry.get(part, 0.0), 2)
            entry[part] = found[part]["amount"]
    return delta


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


def _kpi(conn, handed: dict, rates: List[dict], by_point: Dict[int, List[dict]], start: str, end: str) -> tuple:
    """The KPI of the handing points (ТЗ 95): ({point key: [{month, cycle, day, fee, item}]} — the premiums whose cycle closed within
    start..end; {point key: {month: {orders, tyres, cycles, need_orders, need_tyres, bonus, fee}}} — the count of each month from the month
    of `start` up to `end`). An order is closed once, on the day of the first of ALL its sales (not reversed): sold in two months, it counts
    in the first one only, with every tyre sold up to `end`."""
    closed: Dict[tuple, List[dict]] = {}                                        # point → [{order, day, tyres}]
    month_start = start[:8] + "01" if start else ""
    for note, first, tyres in conn.execute(
            "SELECT note, MIN(day), SUM(CASE WHEN day <= ? THEN pieces ELSE 0 END) FROM sale WHERE source = 'order' AND reversed_at IS NULL "
            "GROUP BY note", (end or "9999-12-31",)):
        order_id = _order_id(note)
        to = handed.get(order_id)
        if to is None or (end and first > end) or (month_start and first < month_start):
            continue
        closed.setdefault(to, []).append({"order": order_id, "day": first, "tyres": tyres})
    premiums: Dict[tuple, List[dict]] = {}
    progress: Dict[tuple, Dict[str, dict]] = {}
    for to, found in closed.items():
        months: Dict[str, List[dict]] = {}
        for entry in found:
            months.setdefault(entry["day"][:7], []).append(entry)
        for month, entries in sorted(months.items()):
            first = month + "-01"
            if first < KPI_FROM:                                                # the owner 2026-11-25: no KPI before, whatever rate is saved
                continue
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
    return payouts.setdefault(key, {}).setdefault(note, {"order": _order_id(note), "day": day, "issue": 0.0, "issue_pieces": 0, "handoff": 0.0,
                                                         "handoff_pieces": 0,
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
