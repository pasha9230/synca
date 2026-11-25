"""Взаиморасчёты с точками (ТЗ 91, 92, 95): what the office owes each point — the fee of an issued order, the bonuses of the clients it
handed over and of the tyres it sold from «На ОТХ», the KPI premiums — item by item, from order_report.build (the one count of the report,
the analytics and the point's page), and what the office already paid («Рассчитан»).

Only an account: nothing is paid here, no money moves. An item is «order:<id>» (all a point gets for one order) or «kpi:<YYYY-MM>:<n>»
(a premium). «Рассчитан» (point_paid) is set by the office with the money right; the paid item leaves the point's active debt and stays in
the report, the CRM and the journal. «Вычли с кассы и получили бонусы» of a point is a request (point_paid_request + a task to the admin):
the point's sum stays until the office presses «Рассчитан» (the lane's choice for the owner's open question of ТЗ 95: the office checks
the cash first). The ledger starts at LEDGER_FROM: the earlier months were settled before the panel kept it."""
from __future__ import annotations

import json
from typing import Dict, Iterable, List, Optional, Tuple

from . import order_report, orders, store

LEDGER_FROM = "2026-11-01"           # the first day the ledger keeps: earlier items are not shown as a debt
STATUSES = {"unpaid": "Не рассчитан", "paid": "Рассчитан"}
REQUEST_TASK = "Точка «%s»: вычли с кассы и получили бонусы %s — проверить кассу и отметить «Рассчитан»"


class PayoutError(ValueError):
    """A refused action; the text is for the person."""


def _money(value: float) -> str:
    whole = "{:,.2f}".format(round(value, 2)).replace(",", " ").replace(".00", "").replace(".", ",")
    return whole + " ₽"


def paid_items(conn, point_ids: Optional[Iterable[int]] = None) -> Dict[Tuple[int, str], dict]:
    """(point, item) → {amount, day, paid_at, author} of what the office marked «Рассчитан»."""
    query, args = "SELECT point_id, item, amount, day, paid_at, author FROM point_paid", []
    if point_ids is not None:
        ids = sorted({int(i) for i in point_ids})
        if not ids:
            return {}
        query += " WHERE point_id IN (%s)" % ",".join("?" * len(ids))
        args = ids
    return {(r[0], r[1]): {"amount": r[2], "day": r[3], "paid_at": r[4], "author": r[5]} for r in conn.execute(query, args)}


def items(conn, start: str, end: str, point_id: Optional[int] = None, report: Optional[dict] = None) -> List[dict]:
    """Every item the points get for start..end (one point with `point_id`), oldest first: {point_id, point, item, kind («order» / «kpi»),
    order, day, issue, handoff, handoff_pieces, oth, oth_pieces, kpi, total, month, cycle, paid (None or the mark)}. Only the points of the
    panel (a sale of a deleted point has no one to pay)."""
    report = report if report is not None else order_report.build(conn, start, end)
    names = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM point")}
    wanted = [k for k in set(report["payouts"]) | set(report["kpi"]["premiums"])
              if k[0] == "id" and k[1] in names and (point_id is None or k[1] == int(point_id))]
    paid = paid_items(conn, [k[1] for k in wanted])
    out = []
    for key in wanted:
        pid = key[1]
        for e in report["payouts"].get(key, []):
            if not e["order"]:
                continue
            item = "order:%d" % e["order"]
            total = round(e["issue"] + e["handoff"] + e["oth"], 2)
            if not total:
                continue
            out.append(dict(point_id=pid, point=names[pid], item=item, kind="order", order=e["order"], day=e["day"], issue=e["issue"],
                            handoff=e["handoff"], handoff_pieces=e.get("handoff_pieces", 0), oth=e["oth"], oth_pieces=e["oth_pieces"], kpi=0.0,
                            total=total, month="", cycle=0, paid=paid.get((pid, item))))
        for p in report["kpi"]["premiums"].get(key, []):
            out.append(dict(point_id=pid, point=names[pid], item=p["item"], kind="kpi", order=None, day=p["day"], issue=0.0, handoff=0.0,
                            handoff_pieces=0, oth=0.0, oth_pieces=0, kpi=p["fee"], total=p["fee"], month=p["month"], cycle=p["cycle"],
                            paid=paid.get((pid, p["item"]))))
    out.sort(key=lambda e: (e["day"], e["point"], e["order"] or 0, e["item"]))
    return out


def balance(conn, point_id: int, today: str, report: Optional[dict] = None) -> dict:
    """The point's active debt on `today` (ТЗ 91): what is not «Рассчитан» since LEDGER_FROM — {issue, bonus (handed + OTH), kpi, total,
    count}."""
    found = [e for e in items(conn, LEDGER_FROM, today, point_id, report) if e["paid"] is None]
    out = {"issue": round(sum(e["issue"] for e in found), 2), "bonus": round(sum(e["handoff"] + e["oth"] for e in found), 2),
           "kpi": round(sum(e["kpi"] for e in found), 2), "count": len(found), "items": [e["item"] for e in found]}
    out["total"] = round(out["issue"] + out["bonus"] + out["kpi"], 2)
    return out


def settle(conn, chosen: Iterable[Tuple[int, str]], start: str, end: str, now: str, author: str) -> dict:
    """«Рассчитан» of the chosen (point, item) of start..end: each is marked with its sum now (counted again here, never taken from the
    form), once; the order's journal and the audit get it. Returns {count, amount}. An item already paid or not of the period is skipped."""
    chosen = {(int(p), str(i)) for p, i in chosen}
    if not chosen:
        raise PayoutError("Отметьте, что рассчитано.")
    known = {(e["point_id"], e["item"]): e for e in items(conn, start, end)}
    count, amount, by_point = 0, 0.0, {}
    for key in sorted(chosen):
        entry = known.get(key)
        if entry is None or entry["paid"] is not None:
            continue
        if conn.execute("INSERT OR IGNORE INTO point_paid (point_id, item, amount, day, paid_at, author) VALUES (?,?,?,?,?,?)",
                        (key[0], key[1], entry["total"], entry["day"], now, author)).rowcount != 1:
            continue
        count += 1
        amount = round(amount + entry["total"], 2)
        by_point.setdefault(entry["point"], []).append(key[1])
        if entry["order"]:
            orders.add_event(conn, entry["order"], now, author, "field", "Рассчитано с точкой «%s»: %s (выплата точке за заказ)" % (
                entry["point"], _money(entry["total"])))
    if not count:
        conn.rollback()
        raise PayoutError("Нечего отмечать: эти позиции уже рассчитаны или не входят в период.")
    store.write_audit(conn, now, author, "point_paid", {"count": count, "amount": amount, "items": by_point})
    return {"count": count, "amount": amount}


def set_source_point(conn, order_id: int, value, was, now: str, author: str) -> bool:
    """«Какая точка передала клиента» of the order window (ТЗ 92): the client a point gave by phone is its handed client — its bonus a tyre
    when the order is «Выполнено», its «Бонусы», its KPI. Only with «Откуда клиент» = «Передал ПВЗ»; another source clears it. A field left
    as the window showed it (`was`) changes nothing. The journal and the audit get it. True if it changed."""
    row = conn.execute("SELECT source FROM order_card WHERE id = ?", (int(order_id),)).fetchone()
    if row is None:
        raise PayoutError("Такого заказа нет.")
    text = str(value or "").strip()
    if text == str(was or "").strip() and row[0] == "pvz":
        return False
    point_id = None
    if text and row[0] == "pvz":
        if not text.isdigit():
            raise PayoutError("Точка выбрана неверно.")
        point_id = int(text)
    old = conn.execute("SELECT point_id FROM order_source_point WHERE order_id = ?", (int(order_id),)).fetchone()
    old = old[0] if old else None
    if old == point_id:
        return False
    name = ""
    if point_id is not None:
        found = conn.execute("SELECT name FROM point WHERE id = ?", (point_id,)).fetchone()
        if found is None:
            raise PayoutError("Такой точки нет.")
        name = found[0]
        conn.execute("INSERT OR REPLACE INTO order_source_point (order_id, point_id, at, author) VALUES (?,?,?,?)", (int(order_id), point_id, now, author))
        orders.add_event(conn, int(order_id), now, author, "field", "Откуда клиент: передала точка «%s» — ей бонус за каждую шину, когда заказ выполнен" % name)
    else:
        conn.execute("DELETE FROM order_source_point WHERE order_id = ?", (int(order_id),))
        orders.add_event(conn, int(order_id), now, author, "field", "Откуда клиент: точка больше не указана — бонус точке не начисляется")
    store.write_audit(conn, now, author, "order_source_point", {"order": int(order_id), "point": point_id, "name": name, "from": old})
    return True


def last_request(conn, point_id: int) -> Optional[dict]:
    row = conn.execute("SELECT id, at, author, amount, items, task_id FROM point_paid_request WHERE point_id = ? ORDER BY id DESC LIMIT 1",
                       (int(point_id),)).fetchone()
    if row is None:
        return None
    out = dict(zip(("id", "at", "author", "amount", "items", "task_id"), row))
    out["items"] = json.loads(out["items"] or "[]")
    paid = paid_items(conn, [point_id])
    out["open"] = any((int(point_id), i) not in paid for i in out["items"])         # waits while an item of it is not «Рассчитан»
    return out


def open_requests(conn) -> Dict[int, dict]:
    """Point → its last request while it waits for the office."""
    out = {}
    for (pid,) in conn.execute("SELECT DISTINCT point_id FROM point_paid_request").fetchall():
        found = last_request(conn, pid)
        if found and found["open"]:
            out[pid] = found
    return out


def request(conn, point_id: int, today: str, now: str, author: str) -> dict:
    """«Вычли с кассы и получили бонусы» of a point: a request to the office with the point's active debt now and a task to the admin. The
    sum is not zeroed: it leaves the debt when the office marks it «Рассчитан». A second request while one waits is refused."""
    from . import order_notify, tasks
    row = conn.execute("SELECT name FROM point WHERE id = ?", (int(point_id),)).fetchone()
    if row is None:
        raise PayoutError("Такой точки нет.")
    waiting = last_request(conn, point_id)
    if waiting and waiting["open"]:
        raise PayoutError("Запрос уже отправлен: ждём, пока офис проверит кассу и отметит «Рассчитан».")
    debt = balance(conn, point_id, today)
    if not debt["total"]:
        raise PayoutError("Нечего рассчитывать: всё уже рассчитано.")
    owner = order_notify.get_owner_login(conn)
    task_id = tasks.add(conn, kind="other", title=(REQUEST_TASK % (row[0], _money(debt["total"])))[:tasks.TITLE_MAX], due_at=now[:16],
                        note="Пункты выдачи → «%s»: %d поз. /points/payouts?p=%d" % (row[0], debt["count"], int(point_id)), now=now, author=author,
                        assignee=owner, owner_login=owner, commit=False)
    request_id = conn.execute("INSERT INTO point_paid_request (point_id, at, author, amount, items, task_id) VALUES (?,?,?,?,?,?)",
                              (int(point_id), now, author, debt["total"], json.dumps(debt["items"]), task_id)).lastrowid
    store.write_audit(conn, now, author, "point_paid_request", {"point": int(point_id), "amount": debt["total"], "count": debt["count"],
                                                                "task": task_id, "request": request_id})
    return {"id": request_id, "amount": debt["total"], "task_id": task_id}
