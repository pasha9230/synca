"""The data of «Доход» of a point (ТЗ 80, 91, 92, 95; routes_mypoint.mypoint_income): the tabs «Доход», «Бонусы», «KPI» and the active debt
on top. Only what the point is paid (order_report.build through point_payouts): the rates and the sums, the point's own orders and clients;
nothing of the purchase, the profit or the prices."""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Dict, List

from .. import order_report, point_payouts

TABS = {"income": "Доход", "bonus": "Бонусы", "kpi": "KPI"}
PAY = {"active": "Не рассчитан", "paid": "Рассчитан", "all": "Все"}
DEALS = {"all": "Все", "done": "Выполненные", "work": "В работе", "lost": "Не состоялось"}
SORTS = {"date": "по дате", "sum": "по сумме"}


def rub(value) -> str:
    text = "{:,.2f}".format(round(float(value or 0), 2)).replace(",", " ").replace(".00", "").replace(".", ",")
    return text + " ₽"


def _day(text, default: str) -> str:
    try:
        return date.fromisoformat(str(text or "").strip()[:10]).isoformat()
    except ValueError:
        return default


def _month_bounds(month: str):
    first = date.fromisoformat(month + "-01")
    last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return first.isoformat(), last.isoformat()


def _filters(args, today: str) -> dict:
    tab = args.get("tab", "") if args.get("tab", "") in TABS else "income"
    month = str(args.get("m", "")).strip()[:7]
    try:                                                    # «Месяц» of the first page (a link of before): its days
        start, end = _month_bounds(month)
    except ValueError:
        start, end = _day(args.get("from"), point_payouts.LEDGER_FROM), _day(args.get("to"), today)
    if start > end:
        start, end = end, start
    km = str(args.get("km", "")).strip()[:7]
    try:
        _month_bounds(km)
    except ValueError:
        km = today[:7]
    return {"tab": tab, "from": start, "to": end, "q": str(args.get("q", "")).strip()[:60],
            "pay": args.get("pay", "") if args.get("pay", "") in PAY else "active",
            "deal": args.get("deal", "") if args.get("deal", "") in DEALS else "all",
            "sort": args.get("sort", "") if args.get("sort", "") in SORTS else "date",
            "dir": "asc" if args.get("dir") == "asc" else "desc", "km": km}


def empty(args, today: str) -> dict:
    return {"f": _filters(args, today), "tabs": TABS, "pay_names": PAY, "deals": DEALS, "sorts": SORTS, "today": today, "rows": [],
            "income": {"orders": []}}


def _cards(conn, ids) -> Dict[int, dict]:
    """Order → its number, client, stage, comment, positions (name × pieces) and wishes: two queries whatever the count."""
    ids = sorted({int(i) for i in ids if i})
    out: Dict[int, dict] = {}
    for chunk in range(0, len(ids), 500):
        part = ids[chunk:chunk + 500]
        marks = ",".join("?" * len(part))
        for cid, number, name, phone, comment, stage, kind, code in conn.execute(
                "SELECT c.id, c.webmim_number, c.client_name, c.client_phone, c.comment, g.name, g.kind, g.code FROM order_card c "
                "JOIN order_stage g ON g.id = c.stage_id WHERE c.id IN (%s)" % marks, part):
            out[cid] = {"label": number or str(cid), "client": name, "phone": phone, "comment": comment, "stage": stage, "kind": kind,
                        "code": code, "items": [], "wants": [], "pieces": 0}
        for oid, name, qty in conn.execute("SELECT order_id, name, qty FROM order_item WHERE order_id IN (%s) ORDER BY id" % marks, part):
            if oid in out:
                out[oid]["items"].append({"name": name, "qty": qty})
                out[oid]["pieces"] += qty
        for oid, brand, model, width, profile, radius, qty in conn.execute(
                "SELECT order_id, brand, model, width, profile, radius, qty FROM order_want WHERE order_id IN (%s) ORDER BY id" % marks, part):
            if oid in out:
                size = "%s/%s R%s" % (width, profile, radius) if width and profile and radius else ""
                out[oid]["wants"].append(" ".join(x for x in (brand, model, size) if x) + (" × %d" % qty if qty else ""))
    return out


def _card(cards: Dict[int, dict], order_id: int) -> dict:
    """The card of `order_id`, or a stub when the order is gone (its sale stays in the report)."""
    return cards.get(order_id) or {"label": str(order_id), "client": "", "phone": "", "comment": "", "stage": "—", "kind": "", "code": "",
                                   "items": [], "wants": [], "pieces": 0}


def _digits(text) -> str:
    return re.sub(r"\D", "", str(text or ""))


def _matches(q: str, card: dict) -> bool:
    if not q:
        return True
    low = q.lower()
    if low.lstrip("№ ") and low.lstrip("№ ") in card["label"].lower():
        return True
    if low in (card["client"] or "").lower() or any(low in i["name"].lower() for i in card["items"]):
        return True
    digits = _digits(q)
    return len(digits) >= 3 and digits in _digits(card["phone"])


def _sorted(rows: List[dict], f: dict, amount: str) -> List[dict]:
    key = (lambda r: (r[amount] or 0, r["day"])) if f["sort"] == "sum" else (lambda r: (r["day"], r["order"] or 0))
    return sorted(rows, key=key, reverse=f["dir"] == "desc")


def _pay_ok(paid, f: dict) -> bool:
    return f["pay"] == "all" or (f["pay"] == "paid") == (paid is not None)


def build(conn, point_id: int, args, today: str) -> dict:
    f = _filters(args, today)
    view = empty(args, today)
    upto = min(f["to"], today)
    report = order_report.build(conn, f["from"], f["to"])
    entries = point_payouts.items(conn, f["from"], f["to"], point_id, report)
    ledger = report if (f["from"], f["to"]) == (point_payouts.LEDGER_FROM, upto) else order_report.build(conn, point_payouts.LEDGER_FROM, upto)
    view["debt"] = point_payouts.balance(conn, point_id, upto, ledger)
    view["debt_day"] = upto
    view["request"] = point_payouts.last_request(conn, point_id)
    rates = order_report.point_rates_now(conn, point_id, upto)
    view["rates"] = rates
    view["income"] = {"issue": round(sum(e["issue"] for e in entries), 2), "bonus": round(sum(e["handoff"] + e["oth"] for e in entries), 2),
                      "kpi": round(sum(e["kpi"] for e in entries), 2), "orders": []}
    view["income"]["total"] = round(view["income"]["issue"] + view["income"]["bonus"] + view["income"]["kpi"], 2)
    by_order = {e["order"]: e for e in entries if e["order"]}
    if f["tab"] == "income":
        cards = _cards(conn, [e["order"] for e in entries if e["issue"]])
        rows = []
        for e in entries:
            card = _card(cards, e["order"]) if e["order"] else None
            if not e["issue"] or card is None or not _pay_ok(e["paid"], f) or not _matches(f["q"], card):
                continue
            rows.append(dict(e, card=card))
        view["rows"] = _sorted(rows, f, "issue")
    elif f["tab"] == "bonus":
        view["rows"] = _bonus_rows(conn, point_id, f, by_order, rates)
    else:
        view["kpi"] = _kpi(conn, point_id, f["km"], today)
    return view


def _bonus_rows(conn, point_id: int, f: dict, by_order: Dict[int, dict], rates: Dict[str, float]) -> List[dict]:
    """«Бонусы»: the clients the point handed over (its handoffs and «Откуда клиент» = the point, of the period) and the tyres it sold from
    «На ОТХ», each with its bonus: the paid one of a closed deal, the expected one (the rate a tyre × the pieces of the order) of a deal in
    work, none of a failed one."""
    end = f["to"] + "T23:59:59"
    handed = {r[0] for r in conn.execute("SELECT order_id FROM order_handoff WHERE point_id = ? AND created_at >= ? AND created_at <= ?",
                                         (point_id, f["from"], end))}
    handed |= {r[0] for r in conn.execute("SELECT order_id FROM order_source_point WHERE point_id = ? AND at >= ? AND at <= ?",
                                          (point_id, f["from"], end))}
    bonus = {oid for oid, e in by_order.items() if e["handoff"] or e["oth"]}
    cards = _cards(conn, handed | bonus)
    per_tyre = f["to"] >= order_report.HANDOFF_PER_TYRE_FROM
    rows = []
    for oid in handed | bonus:
        card = _card(cards, oid)
        if not _matches(f["q"], card):
            continue
        entry = by_order.get(oid)
        paid = entry["paid"] if entry else None
        if entry and (entry["handoff"] or entry["oth"]):
            deal, amount, expected = "done", round(entry["handoff"] + entry["oth"], 2), False
        elif card["kind"] == "lost" or card["code"] == "cancel":
            deal, amount, expected = "lost", 0.0, False
        elif card["kind"] == "won":
            deal, amount, expected = "done", None, False             # closed outside the period: its bonus is in that period
        else:
            deal, expected = "work", True
            amount = round(rates["handoff_fee"] * card["pieces"], 2) if per_tyre else rates["handoff_fee"]
        if f["deal"] != "all" and deal != f["deal"]:
            continue
        if deal == "done" and amount and not _pay_ok(paid, f) or deal != "done" and f["pay"] == "paid":
            continue
        kinds = []
        if oid in handed or (entry and entry["handoff"]):
            kinds.append("переданный клиент")
        if entry and entry["oth"]:
            kinds.append("продажа с ОТХ")
        rows.append({"order": oid, "day": entry["day"] if entry else "", "card": card, "deal": deal, "deal_name": DEALS[deal],
                     "amount": amount, "expected": expected, "paid": paid, "kinds": ", ".join(kinds)})
    for r in rows:
        r["sort_day"] = r["day"] or ""
    key = (lambda r: (r["amount"] or 0, r["sort_day"])) if f["sort"] == "sum" else (lambda r: (r["sort_day"], r["order"]))
    return sorted(rows, key=key, reverse=f["dir"] == "desc")


def _kpi(conn, point_id: int, month: str, today: str) -> dict:
    """The month's KPI of the point: the closed orders and tyres of its handed clients, the cycles done, the progress of the next one."""
    first, last = _month_bounds(month)
    rates = order_report.point_rates_now(conn, point_id, first)
    count = {"orders": 0, "tyres": 0, "cycles": 0, "need_orders": int(rates["kpi_orders"]), "need_tyres": int(rates["kpi_tyres"]),
             "bonus": rates["kpi_bonus"], "fee": 0.0}
    premiums = []
    if first <= today:
        mine = order_report.point_income(conn, first, min(last, today), point_id)
        count = mine["progress"].get(month, count)
        premiums = mine["premiums"]
    paid = point_payouts.paid_items(conn, [point_id])
    for p in premiums:
        p["paid"] = paid.get((point_id, p["item"]))
    in_orders = count["orders"] - count["cycles"] * count["need_orders"]
    in_tyres = count["tyres"] - count["cycles"] * count["need_tyres"]
    out = dict(count, month=month, premiums=premiums,
               cycle_orders=min(in_orders, count["need_orders"]), cycle_tyres=min(in_tyres, count["need_tyres"]),
               pct_orders=min(100, int(in_orders * 100 / max(count["need_orders"], 1))),
               pct_tyres=min(100, int(in_tyres * 100 / max(count["need_tyres"], 1))),
               sets=round(count["need_tyres"] / 4), standard=(count["need_orders"], count["need_tyres"], count["bonus"]) == (
                   order_report.KPI_ORDERS, order_report.KPI_TYRES, order_report.KPI_BONUS),
               started=last >= order_report.KPI_FROM or count["bonus"] > 0, this_month=month == today[:7])
    return out
