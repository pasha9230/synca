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

from . import sales as sales_store, store, users, webmim_match

POINT_FEE = 75.0                     # rubles per order, paid to the point (the default: before the first dated rate)
OWNER_SHARE = 0.5                    # of the net profit; the rest is the partner's (the default)
HANDOFF_FEE = 50.0                   # rubles to the point for an issued order of a client it handed over, on top of the 75 (the owner 2026-10-23, п. 24)
HANDOFF_FEE_FROM = "2026-10-24"      # the day of the deploy: the orders of earlier days keep the old default, 0 (a past report is never recounted)
OTH_SALE_FEE = 50.0                  # rubles to the point for every tyre it sold from «На ОТХ» (ТЗ 80; «+50 ₽ за каждую проданную шину» of the window)
OTH_SALE_FEE_FROM = "2026-11-25"     # the sales of earlier days get 0: a past report is never recounted (as HANDOFF_FEE_FROM)
DEFAULTS = {"point_fee": POINT_FEE, "owner_share": OWNER_SHARE, "handoff_fee": HANDOFF_FEE, "oth_sale_fee": OTH_SALE_FEE}
STARTS = {"handoff_fee": HANDOFF_FEE_FROM, "oth_sale_fee": OTH_SALE_FEE_FROM}     # before it, with no rate saved: 0
# the journal of an order the point sold from «На ОТХ» (kind «point»): oth_point_sale._sell writes the first with «: N шт. …», order_oth.sell
# the second as it is; the office's «Выполнено» of a record (order_oth.OFFICE_SOLD) and «Взять с ОТХ» are no sale of the point
OTH_SOLD_EVENT = "Точка продала шины с ОТХ"
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


def set_rates(conn, valid_from: str, today: str, now: str, author: str, point_fee: Optional[float] = None,
              owner_share: Optional[float] = None, handoff_fee: Optional[float] = None, oth_sale_fee: Optional[float] = None) -> List[str]:
    """Save the rates from `valid_from` (today or later: a past report is never recounted). Returns the names that changed; each is audited."""
    try:
        valid_from = date.fromisoformat(str(valid_from)).isoformat()
    except ValueError:
        raise ValueError("Дата начала: нужен формат ГГГГ-ММ-ДД.")
    if valid_from < today:
        raise ValueError("Ставка действует с сегодняшнего дня или позже: прошлые отчёты не пересчитываются.")
    if point_fee is not None and not 0 <= point_fee <= MAX_POINT_FEE:
        raise ValueError("Выплата точке: от 0 до 100 000 ₽ за заказ.")
    if owner_share is not None and not 0 <= owner_share <= 1:
        raise ValueError("Доля собственника: от 0 до 100 %.")
    if handoff_fee is not None and not 0 <= handoff_fee <= MAX_POINT_FEE:
        raise ValueError("Выплата точке за переданного клиента: от 0 до 100 000 ₽.")
    if oth_sale_fee is not None and not 0 <= oth_sale_fee <= MAX_POINT_FEE:
        raise ValueError("Бонус точке за шину, проданную с ОТХ: от 0 до 100 000 ₽.")
    rates = list_rates(conn)
    changed = []
    for name, value in (("point_fee", point_fee), ("owner_share", owner_share), ("handoff_fee", handoff_fee), ("oth_sale_fee", oth_sale_fee)):
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


BONUS_TO = "handed"                  # who gets the fixed sum for a handed-over client: "handed" — the point that handed it over (the owner 2026-10-23),
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
    rows = _counted(conn, sales_store.list_sales(conn, start, end))       # all points: a point's fixed sum may come from an order issued elsewhere
    names: Dict[tuple, str] = {}                                                        # point key → its name in the report
    handed = {}                                                                         # the clients the points handed over (order_manager.handoff)
    for order_id, point_id, name in conn.execute("SELECT h.order_id, p.id, p.name FROM order_handoff h LEFT JOIN point p ON p.id = h.point_id"):
        handed[order_id] = ("id", point_id) if point_id is not None else None
        if point_id is not None:
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
            mine["issue"] = rate_on(rates, "point_fee", day)
            if order_id in oth_sold:                                                    # the point that sold them, once per order, per tyre
                pieces = sum(r["pieces"] for r in parts)
                extra = round(rate_on(rates, "oth_sale_fee", day) * pieces, 2)
                bonuses.setdefault(key, []).append((rate_on(rates, "owner_share", day), extra))
                mine["oth"], mine["oth_pieces"] = extra, pieces
                got = oth_lines.setdefault(key, {"point": names[key], "orders": 0, "pieces": 0, "fee": 0.0})
                got["orders"] += 1
                got["pieces"] += pieces
                got["fee"] = round(got["fee"] + extra, 2)
            if order_id not in handed:
                continue
            extra, to = rate_on(rates, "handoff_fee", day), bonus_point(key, handed[order_id])      # on top of the 75 ₽
            bonuses.setdefault(to, []).append((rate_on(rates, "owner_share", day), extra))
            _payout(payouts, to, note, day)["handoff"] = extra
            got = handoff_lines.setdefault(to, {"point": names[to], "clients": 0, "fee": 0.0})
            got["clients"] += 1
            got["fee"] = round(got["fee"] + extra, 2)
    if point:                                                                           # one point: its orders and the fixed sums it gets
        grouped = {k: v for k, v in grouped.items() if names[k] == point}
        bonuses = {k: v for k, v in bonuses.items() if names[k] == point}
        handoff_lines = {k: v for k, v in handoff_lines.items() if names[k] == point}
        oth_lines = {k: v for k, v in oth_lines.items() if names[k] == point}
        payouts = {k: v for k, v in payouts.items() if names[k] == point}
        rows = [r for r in rows if r["point"] == point]
    lines, total_by_share, used, webmim = [], {}, set(), []
    for key in list(grouped) + [k for k in bonuses if k not in grouped]:               # a point paid only for the clients it handed over
        items = grouped.get(key, [])
        fee_sum, bonus_sum, by_share = 0.0, 0.0, {}
        for note, parts in per_point.get(key, {}).items():
            day = min(r["day"] for r in parts)
            fee, share = rate_on(rates, "point_fee", day), rate_on(rates, "owner_share", day)
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
                          _issue=round(fee_sum, 2), _bonus=round(bonus_sum, 2)))
        webmim.append({"point": names[key], "count": sum(1 for r in items if r.get("webmim")), "positions": len(items)})
    lines.sort(key=lambda r: (-r["net"], r["point"]))
    fees = [dict(point=r["point"], orders=r["orders"], fee=r["fee"], issue=r.pop("_issue"), bonus=r.pop("_bonus")) for r in lines]
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
            "fees": {"lines": fees, "issue": round(sum(f["issue"] for f in fees), 2), "bonus": round(sum(f["bonus"] for f in fees), 2)},   # the fee: for
                                                                                        # the issued orders and the bonuses (ТЗ 80), line by line
            "payouts": {key: sorted(found.values(), key=lambda e: (e["day"], e["order"] or 0)) for key, found in payouts.items()}}


def _payout(payouts: Dict[tuple, Dict[str, dict]], key: tuple, note: str, day: str) -> dict:
    """The line of the order `note` in what the point `key` is paid (made empty: no sum yet)."""
    return payouts.setdefault(key, {}).setdefault(note, {"order": _order_id(note), "day": day, "issue": 0.0, "handoff": 0.0, "oth": 0.0, "oth_pieces": 0})


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
    entries = [dict(e, total=round(e["issue"] + e["handoff"] + e["oth"], 2)) for e in build(conn, start, end)["payouts"].get(("id", int(point_id)), [])]
    out = {"orders": entries}
    for name in ("issue", "handoff", "oth", "total"):
        out[name] = round(sum(e[name] for e in entries), 2)
    out["bonus"] = round(out["handoff"] + out["oth"], 2)
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
