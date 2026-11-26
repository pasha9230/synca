"""Взаиморасчёты с точками (ТЗ 91, 92, 95): what the office owes each point — the fee of an issued order, the bonuses of the clients it
handed over and of the tyres it sold from «На ОТХ», the KPI premiums — item by item, from order_report.build (the one count of the report,
the analytics and the point's page), and what the office already paid («Рассчитан»).

Only an account: nothing is paid here, no money moves. An item is «order:<id>» (all a point gets for one order) or «kpi:<YYYY-MM>:<n>»
(a premium) or «adj:<id>» (a correction of the office, ± with a reason). «Рассчитан» (point_paid) is set by the office with the money
right: each press is a payment row of the item's due sum — the sum now less what was paid. An item whose sum grew after it was paid owes a
«доплата»; one whose sum fell (a reversed sale) shows «переплата / к удержанию», and «Рассчитан» on it records the hold-back (a negative
row); a paid item gone from the report stays with the sum 0. «Отменить расчёт» cancels the payments of an item (kept, marked). The paid
item leaves the point's active debt and stays in the report, the CRM and the journal. «Вычли с кассы и получили бонусы» of a point is a request (point_paid_request + a task to the admin):
the point's sum stays until the office presses «Рассчитан» (the lane's choice for the owner's open question of ТЗ 95: the office checks
the cash first). The ledger starts at LEDGER_FROM: the earlier months were settled before the panel kept it."""
from __future__ import annotations

import json
from typing import Dict, Iterable, List, Optional, Tuple

from . import order_report, orders, store

LEDGER_FROM = "2026-11-01"           # the first day the ledger keeps: earlier items are not shown as a debt
STATUSES = {"unpaid": "Не рассчитан", "paid": "Рассчитан", "extra": "Доплата", "over": "Переплата / к удержанию"}
REQUEST_TASK = "Точка «%s»: вычли с кассы и получили бонусы %s — проверить кассу и отметить «Рассчитан»"


class PayoutError(ValueError):
    """A refused action; the text is for the person."""


def _money(value: float) -> str:
    whole = "{:,.2f}".format(round(value, 2)).replace(",", " ").replace(".00", "").replace(".", ",")
    return whole + " ₽"


def paid_items(conn, point_ids: Optional[Iterable[int]] = None) -> Dict[Tuple[int, str], dict]:
    """(point, item) → {amount (all its payments not cancelled), day (of the first), paid_at, author (of the last), count, ids} of «Рассчитан»."""
    query, args = "SELECT id, point_id, item, amount, day, paid_at, author FROM point_paid WHERE cancelled_at IS NULL", []
    if point_ids is not None:
        ids = sorted({int(i) for i in point_ids})
        if not ids:
            return {}
        query += " AND point_id IN (%s)" % ",".join("?" * len(ids))
        args = ids
    out: Dict[Tuple[int, str], dict] = {}
    for row_id, pid, item, amount, day, paid_at, author in conn.execute(query + " ORDER BY id", args):
        got = out.setdefault((pid, item), {"amount": 0.0, "day": day, "count": 0, "ids": []})
        got["amount"] = round(got["amount"] + amount, 2)
        got["paid_at"], got["author"] = paid_at, author
        got["count"] += 1
        got["ids"].append(row_id)
    return out


def _status(total: float, paid: Optional[dict]) -> Tuple[float, str]:
    due = round(total - (paid["amount"] if paid else 0.0), 2)
    if not paid:
        return due, "unpaid"
    return due, "paid" if due == 0 else "extra" if due > 0 else "over"


def _parts(item: str) -> dict:
    """The kind, the order, the month and the cycle of an item by its name."""
    bits = item.split(":")
    if bits[0] == "kpi" and len(bits) == 3:
        return {"kind": "kpi", "order": None, "month": bits[1], "cycle": int(bits[2]) if bits[2].isdigit() else 0}
    if bits[0] == "adj":
        return {"kind": "adj", "order": None, "month": "", "cycle": 0}
    return {"kind": "order", "order": int(bits[1]) if len(bits) > 1 and bits[1].isdigit() else None, "month": "", "cycle": 0}


def adjustments(conn, point_id: Optional[int] = None) -> List[dict]:
    query, args = "SELECT id, point_id, day, amount, reason, at, author FROM point_adjust", ()
    if point_id is not None:
        query, args = query + " WHERE point_id = ?", (int(point_id),)
    return [dict(zip(("id", "point_id", "day", "amount", "reason", "at", "author"), r)) for r in conn.execute(query + " ORDER BY id", args)]


def items(conn, start: str, end: str, point_id: Optional[int] = None, report: Optional[dict] = None) -> List[dict]:
    """Every item the points get for start..end by its day (one point with `point_id`), oldest first: {point_id, point, item, kind («order»,
    «kpi», «adj» — a correction of the office), order, day, issue, handoff, handoff_pieces, oth, oth_pieces, kpi, adj, reason, total (the
    sum now), paid_sum, due (total − paid_sum), status (STATUSES), paid (the payments or None), month, cycle}. `report`: order_report.build
    of a period that holds start..end (one count per screen). A paid item no longer in the report (its sale reversed) is kept with the sum
    0: its payment is «к удержанию». Only the points of the panel."""
    report = report if report is not None else order_report.build(conn, start, end)
    names = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM point")}
    wanted = [k for k in set(report["payouts"]) | set(report["kpi"]["premiums"])
              if k[0] == "id" and k[1] in names and (point_id is None or k[1] == int(point_id))]
    paid = paid_items(conn, None if point_id is None else [point_id])
    out, seen = [], set()
    counted = set()                                           # every item of the count, whatever its day: «still in the report»
    for key in wanted:
        counted |= {(key[1], e["item"]) for e in report["payouts"].get(key, []) if e["item"]}     # «order:N», «arrival:N»
        counted |= {(key[1], p["item"]) for p in report["kpi"]["premiums"].get(key, [])}

    def add(pid: int, item: str, day: str, total: float, **parts) -> None:
        seen.add((pid, item))
        mark = paid.get((pid, item))
        if not total and not mark and not parts.get("fixed"):          # a sum the office set to 0 stays in sight: it can be taken back
            return
        due, status = _status(total, mark)
        entry = dict(point_id=pid, point=names[pid], item=item, day=day, total=round(total, 2), paid_sum=mark["amount"] if mark else 0.0,
                     due=due, status=status, status_name=STATUSES[status], paid=mark, issue=0.0, handoff=0.0, handoff_pieces=0, oth=0.0,
                     oth_pieces=0, kpi=0.0, adj=0.0, reason="", issue_pieces=0, arrived=False, fixed={}, counted={})
        entry.update(_parts(item))
        entry.update(parts)
        out.append(entry)

    for key in wanted:
        pid = key[1]
        for e in report["payouts"].get(key, []):
            if e["item"] and start <= e["day"] <= end:
                add(pid, e["item"], e["day"], e["issue"] + e["handoff"] + e["oth"], issue=e["issue"],
                    issue_pieces=e.get("issue_pieces", 0), handoff=e["handoff"], handoff_pieces=e.get("handoff_pieces", 0), oth=e["oth"],
                    oth_pieces=e["oth_pieces"], arrived=bool(e.get("arrived")), fixed=e.get("fixed") or {}, counted=e.get("counted") or {})
        for p in report["kpi"]["premiums"].get(key, []):
            if start <= p["day"] <= end:
                add(pid, p["item"], p["day"], p["fee"], kpi=p["fee"], fixed=p.get("fixed") or {}, counted=p.get("counted") or {})
    for key, found in report.get("adjust", {}).items():      # the corrections of the count (order_report.build), the sums set by the office
        if key[0] != "id" or key[1] not in names or (point_id is not None and key[1] != int(point_id)):
            continue
        for a in found:
            if start <= a["day"] <= end:
                add(key[1], a["item"], a["day"], a["adj"], adj=a["adj"], reason=a["reason"], fixed=a.get("fixed") or {},
                    counted=a.get("counted") or {})
    for (pid, item), mark in paid.items():                    # paid, and gone from the count since: the payment is not lost
        if (pid, item) not in seen and (pid, item) not in counted and not item.startswith("adj:") and pid in names \
                and start <= mark["day"] <= end:
            add(pid, item, mark["day"], 0.0)
    out.sort(key=lambda e: (e["day"], e["point"], e["order"] or 0, e["item"]))
    return out


def balance(conn, point_id: int, today: str, report: Optional[dict] = None) -> dict:
    """The point's account on `today` since LEDGER_FROM (ТЗ 91): {issue, bonus, kpi — of the items not paid yet; adj — the corrections not
    paid yet; extra — the «доплаты»; over — «переплата / к удержанию» (≤ 0); due — what is owed (hold-backs aside); total — due + over, what
    the office owes now; count, items — the items with something owed}."""
    found = [e for e in items(conn, LEDGER_FROM, today, point_id, report) if e["due"]]
    unpaid = [e for e in found if e["status"] == "unpaid"]
    out = {"issue": round(sum(e["issue"] for e in unpaid), 2), "bonus": round(sum(e["handoff"] + e["oth"] for e in unpaid), 2),
           "kpi": round(sum(e["kpi"] for e in unpaid), 2), "adj": round(sum(e["adj"] for e in unpaid), 2),
           "extra": round(sum(e["due"] for e in found if e["status"] == "extra"), 2),
           "over": round(sum(e["due"] for e in found if e["status"] == "over"), 2), "count": len([e for e in found if e["due"] > 0]),
           "items": [e["item"] for e in found if e["due"] > 0]}
    out["due"] = round(sum(e["due"] for e in found if e["due"] > 0), 2)
    out["total"] = round(sum(e["due"] for e in found), 2)                  # the corrections and the hold-backs counted
    return out


def ledger_report(conn, start: str, end: str, today: str) -> dict:
    """The one count of the ledger's screens and of «Рассчитан»: from LEDGER_FROM (or `start`, if earlier) to `today` (or `end`, if later),
    so an order sold in parts keeps its whole sum and its day whatever the period shown."""
    return order_report.build(conn, min(start, LEDGER_FROM), max(end, today))


def _paid_now(conn, point_id: int, item: str) -> float:
    row = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM point_paid WHERE point_id = ? AND item = ? AND cancelled_at IS NULL",
                       (int(point_id), item)).fetchone()
    return round(row[0], 2)


def settle(conn, chosen: Iterable[Tuple[int, str]], start: str, end: str, now: str, author: str, expect: Optional[float] = None,
           today: Optional[str] = None) -> dict:
    """«Рассчитан» of the chosen (point, item) of start..end: each gets a payment row of its due sum now (counted again here, never taken from
    the form, by the count of the page: ledger_report): the whole sum, a «доплата», or a hold-back of an overpayment (< 0). `expect`: the sum
    the person confirmed — another sum now refuses it all (the window asks again). One write at a time (BEGIN IMMEDIATE): the sums are
    counted inside it, and an item whose payments changed since they were read is refused (two presses never pay twice). The order's
    journal and the audit get it. Returns {count, amount}."""
    chosen = {(int(p), str(i)) for p, i in chosen}
    if not chosen:
        raise PayoutError("Отметьте, что рассчитано.")
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        known = {(e["point_id"], e["item"]): e for e in items(conn, start, end, report=ledger_report(conn, start, end, today or now[:10]))}
        picked = [known[k] for k in sorted(chosen) if k in known and known[k]["due"]]
        if not picked:
            raise PayoutError("Нечего отмечать: эти позиции уже рассчитаны или не входят в период.")
        amount = round(sum(e["due"] for e in picked), 2)
        if expect is not None and round(float(expect), 2) != amount:
            raise PayoutError("Сумма изменилась: было %s, сейчас %s. Проверьте и подтвердите ещё раз." % (_money(float(expect)), _money(amount)))
        for e in picked:
            if _paid_now(conn, e["point_id"], e["item"]) != round(e["paid_sum"], 2):
                raise PayoutError("Эту позицию только что рассчитали: обновите страницу.")
        result = _write_settle(conn, picked, amount, now, author)
    except Exception:
        conn.rollback()
        raise
    return result


def _write_settle(conn, picked: List[dict], amount: float, now: str, author: str) -> dict:
    by_point: Dict[str, List[str]] = {}
    for e in picked:
        conn.execute("INSERT INTO point_paid (point_id, item, amount, day, paid_at, author) VALUES (?,?,?,?,?,?)",
                     (e["point_id"], e["item"], e["due"], e["day"], now, author))
        by_point.setdefault(e["point"], []).append(e["item"])
        if e["order"]:
            if e["due"] < 0:
                text = "Удержано с точки «%s»: %s (переплата: сумма за заказ уменьшилась после расчёта)" % (e["point"], _money(-e["due"]))
            else:
                text = "Рассчитано с точкой «%s»: %s (%s)" % (e["point"], _money(e["due"]),
                                                              "доплата за заказ" if e["status"] == "extra" else "выплата точке за заказ")
            orders.add_event(conn, e["order"], now, author, "field", text)
    store.write_audit(conn, now, author, "point_paid", {"count": len(picked), "amount": amount, "items": by_point})
    return {"count": len(picked), "amount": amount}


def unsettle(conn, point_id: int, item: str, now: str, author: str) -> float:
    """«Отменить расчёт» (the owner 2026-11-25): every payment of the item is cancelled (kept, marked), the item is owed again; the order's
    journal and the audit get it. Returns the sum cancelled."""
    mark = paid_items(conn, [point_id]).get((int(point_id), str(item)))
    if not mark:
        raise PayoutError("Эта позиция не рассчитана.")
    conn.execute("UPDATE point_paid SET cancelled_at = ?, cancelled_by = ? WHERE id IN (%s)" % ",".join("?" * len(mark["ids"])),
                 [now, author] + mark["ids"])
    name = conn.execute("SELECT name FROM point WHERE id = ?", (int(point_id),)).fetchone()
    order_id = _parts(str(item))["order"]
    if order_id:
        orders.add_event(conn, order_id, now, author, "field", "Расчёт с точкой «%s» отменён: %s снова к выплате" % (
            name[0] if name else point_id, _money(mark["amount"])))
    store.write_audit(conn, now, author, "point_paid_cancel", {"point": int(point_id), "item": str(item), "amount": mark["amount"]})
    return mark["amount"]


def unsettle_many(conn, chosen: Iterable[Tuple[int, str]], now: str, author: str) -> dict:
    """«Вернуть в долг» of the chosen items (a row, the rows checked, a whole point): the payments of each paid one are cancelled (unsettle);
    the items not paid are skipped. Returns {count, amount}."""
    count, amount = 0, 0.0
    paid = paid_items(conn)
    for point_id, item in sorted({(int(p), str(i)) for p, i in chosen}):
        if (point_id, item) in paid:
            amount = round(amount + unsettle(conn, point_id, item, now, author), 2)
            count += 1
    if not count:
        raise PayoutError("Среди отмеченного нет рассчитанных позиций.")
    return {"count": count, "amount": amount}


PARTS_OF = {"order": ("issue", "handoff", "oth"), "kpi": ("kpi",), "adj": ("adj",)}


def _item_parts(item: str) -> tuple:
    return PARTS_OF[_parts(item)["kind"]]


def _existing_parts(entry: dict) -> set:
    """The articles an item really has in the count of its point: a sum counted (or set before), the tyres of the issue, a handed client, a
    sale from «На ОТХ»."""
    found = set(entry.get("fixed") or {}) | {p for p, v in (entry.get("counted") or {}).items() if v}
    for part in ("issue", "handoff", "oth", "kpi", "adj"):
        if entry.get(part):
            found.add(part)
    if entry.get("issue_pieces") or entry.get("arrived"):
        found.add("issue")
    if entry.get("handoff_pieces"):
        found.add("handoff")
    if entry.get("oth_pieces"):
        found.add("oth")
    return found


def fix_items(conn, chosen: Iterable[Tuple[int, str]], part: str, amount: float, reason: str, now: str, author: str,
              start: str = LEDGER_FROM, end: str = "2099-12-31", today: Optional[str] = None) -> int:
    """The office sets the sum of an article of the chosen items (the owner 2026-11-25: «корректировать сумму», штучно и массово) with a
    reason: `part` — an article of order_report.FIX_PARTS, or "all" with amount 0 — «Отменить начисление» (every article of each item is 0).
    The count everywhere (the report, the analytics, the ledger, the point's page) takes the sum set; the counted one stays (taken back by
    unfix_items). A paid item then owes a «доплата» or a hold-back. The order's journal and the audit get it. Returns the items changed."""
    reason = " ".join(str(reason or "").split())[:ADJUST_REASON_MAX]
    if not reason:
        raise PayoutError("Напишите причину.")
    amount = round(float(amount), 2)
    if amount != amount or abs(amount) > 1000000:
        raise PayoutError("Сумма: не больше 1 000 000 ₽.")
    if part != "all" and part not in order_report.FIX_PARTS:
        raise PayoutError("Выберите статью.")
    if part == "all" and amount:
        raise PayoutError("Отменить начисление можно только суммой 0.")
    if amount < 0 and part != "adj":
        raise PayoutError("Минус — только в «Корректировке» (удержание с причиной); у статей начисления сумма от 0.")
    names = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM point")}
    today = today or now[:10]
    known = {(e["point_id"], e["item"]): e for e in items(conn, start, end, report=ledger_report(conn, start, end, today))}
    done = 0
    for point_id, item in sorted({(int(p), str(i)) for p, i in chosen}):
        if point_id not in names or _parts(item)["kind"] not in PARTS_OF:
            continue
        entry = known.get((point_id, item))
        if entry is None:
            raise PayoutError("Позиции «%s» нет в расчёте точки «%s» за период." % (item, names[point_id]))
        has = _existing_parts(entry)
        parts = [p for p in _item_parts(item) if p in has] if part == "all" else [part]
        if part != "all" and part not in has:
            raise PayoutError("У позиции «%s» точки «%s» нет статьи «%s»." % (item, names[point_id], order_report.FIX_PARTS[part]))
        if not parts:
            continue
        for p in parts:
            conn.execute("INSERT INTO point_item_fix (point_id, item, part, amount, reason, at, author) VALUES (?,?,?,?,?,?,?)",
                         (point_id, item, p, amount, reason, now, author))
        order_id = _parts(item)["order"]
        if order_id:
            text = ("Начисление точке «%s» за заказ отменено офисом: %s" % (names[point_id], reason) if part == "all" else
                    "Начисление точке «%s» (%s) изменено офисом: %s — %s" % (names[point_id], order_report.FIX_PARTS[part], _money(amount), reason))
            orders.add_event(conn, order_id, now, author, "field", text)
        store.write_audit(conn, now, author, "point_item_fix", {"point": point_id, "item": item, "part": part, "amount": amount, "reason": reason})
        done += 1
    if not done:
        raise PayoutError("Отметьте позиции.")
    return done


def unfix_items(conn, chosen: Iterable[Tuple[int, str]], now: str, author: str) -> int:
    """«Вернуть по расчёту»: the sums the office set for the chosen items are taken back (kept, marked); the counted sums count again."""
    names = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM point")}
    done = 0
    for point_id, item in sorted({(int(p), str(i)) for p, i in chosen}):
        cur = conn.execute("UPDATE point_item_fix SET undone_at = ?, undone_by = ? WHERE point_id = ? AND item = ? AND undone_at IS NULL",
                           (now, author, point_id, item))
        if not cur.rowcount:
            continue
        order_id = _parts(item)["order"]
        if order_id:
            orders.add_event(conn, order_id, now, author, "field", "Начисление точке «%s» снова по расчёту (правка офиса снята)" % names.get(point_id, point_id))
        store.write_audit(conn, now, author, "point_item_fix_undone", {"point": point_id, "item": item})
        done += 1
    if not done:
        raise PayoutError("Среди отмеченного нет позиций, изменённых вручную.")
    return done


ADJUST_REASON_MAX = 300


def adjust(conn, point_id: int, amount: float, reason: str, day: str, now: str, author: str) -> int:
    """«Корректировка» of what a point is paid (the owner 2026-11-25): a sum ± with a reason, on `day`; the counted sums stay as they are, the
    correction is an item of its own (owed until «Рассчитан»), shown to the point. Audited."""
    if not conn.execute("SELECT 1 FROM point WHERE id = ?", (int(point_id),)).fetchone():
        raise PayoutError("Такой точки нет.")
    reason = " ".join(str(reason or "").split())[:ADJUST_REASON_MAX]
    if not reason:
        raise PayoutError("Напишите причину корректировки.")
    amount = round(float(amount), 2)
    if not amount or abs(amount) > 1000000 or amount != amount:
        raise PayoutError("Корректировка: сумма не 0 и не больше 1 000 000 ₽.")
    adj_id = conn.execute("INSERT INTO point_adjust (point_id, day, amount, reason, at, author) VALUES (?,?,?,?,?,?)",
                          (int(point_id), day, amount, reason, now, author)).lastrowid
    store.write_audit(conn, now, author, "point_adjust", {"point": int(point_id), "amount": amount, "reason": reason, "day": day, "id": adj_id})
    return adj_id


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


REQUEST_FIELDS = ("id", "point_id", "at", "author", "amount", "items", "task_id", "closed_at", "closed_by")


def last_request(conn, point_id: int, owed: Iterable[str]) -> Optional[dict]:
    """The point's last request; `open` while the office has not closed it and an item of it is still owed (`owed`: balance()["items"])."""
    row = conn.execute("SELECT %s FROM point_paid_request WHERE point_id = ? ORDER BY id DESC LIMIT 1" % ", ".join(REQUEST_FIELDS),
                       (int(point_id),)).fetchone()
    if row is None:
        return None
    out = dict(zip(REQUEST_FIELDS, row))
    out["items"] = json.loads(out["items"] or "[]")
    owed = set(owed)
    out["open"] = out["closed_at"] is None and any(i in owed for i in out["items"])
    return out


def open_requests(conn, today: str, report: Optional[dict] = None) -> Dict[int, dict]:
    """Point → its last request while it waits for the office (`report`: a build that holds LEDGER_FROM..today)."""
    out = {}
    for (pid,) in conn.execute("SELECT DISTINCT point_id FROM point_paid_request").fetchall():
        found = last_request(conn, pid, balance(conn, pid, today, report)["items"])
        if found and found["open"]:
            out[pid] = found
    return out


def close_request(conn, request_id: int, now: str, author: str) -> bool:
    """The office closes a request (the cash checked, settled otherwise) and its task to the admin, if still open: audited. True if it was
    open."""
    row = conn.execute("SELECT point_id, task_id FROM point_paid_request WHERE id = ? AND closed_at IS NULL", (int(request_id),)).fetchone()
    if row is None:
        return False
    conn.execute("UPDATE point_paid_request SET closed_at = ?, closed_by = ? WHERE id = ?", (now, author, int(request_id)))
    if row[1]:
        conn.execute("UPDATE callback_task SET done_at = ?, done_by = ? WHERE id = ? AND done_at IS NULL AND cancelled_at IS NULL",
                     (now, author, row[1]))
    store.write_audit(conn, now, author, "point_paid_request_closed", {"request": int(request_id), "point": row[0]})
    return True


def request(conn, point_id: int, today: str, now: str, author: str) -> dict:
    """«Вычли с кассы и получили бонусы» of a point: a request to the office with the point's account now and a task to the admin. The
    sum is not zeroed: it leaves the debt when the office marks it «Рассчитан». A second request while one waits is refused."""
    from . import order_notify, tasks
    row = conn.execute("SELECT name FROM point WHERE id = ?", (int(point_id),)).fetchone()
    if row is None:
        raise PayoutError("Такой точки нет.")
    report = order_report.build(conn, LEDGER_FROM, today)
    debt = balance(conn, point_id, today, report)
    waiting = last_request(conn, point_id, debt["items"])
    if waiting and waiting["open"]:
        raise PayoutError("Запрос уже отправлен: ждём, пока офис проверит кассу и отметит «Рассчитан».")
    if debt["total"] <= 0:
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
