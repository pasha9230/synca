"""Analytics of the orders, read only (a ground for the day dashboard): the funnel «chat → order → issued» by its slices, the time on the
stages, the money of a period. Nothing here writes to the base.

The period is two days `start` and `end`, «ГГГГ-ММ-ДД», **both included** (the same as the report of the orders, `order_report.build`).
The times of the base are Moscow time already: `order_card.created_at`, `order_event.at` and the sale day are written as local Moscow
time; a chat start (`avito_chat.created_ts`, Unix seconds from Avito) is turned into a Moscow day here.

What the base does not hold is said, not guessed:
- an order made by hand has no source unless the manager chose one: such orders go to «не указан» (there is no «вручную» to tell apart);
- a chat with no start time from Avito (`created_ts` = 0) has no day: it is counted in `chats_undated`, never in a period;
- the stage history is kept by the names of the stages: a stay on a stage renamed or deleted since is shown under its old name, `stage_id` None;
- a deleted order (in «Удалённые») is not in the base any more: it is not counted, its chat counts as a chat without an order."""
from __future__ import annotations

import statistics
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional

from . import order_report, orders

MSK = timezone(timedelta(hours=3))
SLICES = ("source", "account", "responsible", "point")
NO_SOURCE, NO_CHAT, NO_RESPONSIBLE, NO_POINT = "не указан", "без чата Авито", "не указан", "точка не указана"
CREATED_PREFIX, MOVE_PREFIX = "Заказ создан на этапе «", "Этап: «"


def _period(start: str, end: str) -> tuple:
    """The two days checked: «ГГГГ-ММ-ДД», start not after end. Raises ValueError otherwise."""
    try:
        first, last = date.fromisoformat(str(start)), date.fromisoformat(str(end))
    except ValueError:
        raise ValueError("Период: нужны даты в формате ГГГГ-ММ-ДД.")
    if first > last:
        raise ValueError("Период: начало позже конца.")
    return first.isoformat(), last.isoformat()


def _in(day: str, start: str, end: str) -> bool:
    return bool(day) and start <= day[:10] <= end


def chat_day(created_ts) -> str:
    """The Moscow day a chat started, or "" when Avito gave no time."""
    ts = int(created_ts or 0)
    return datetime.fromtimestamp(ts, tz=MSK).date().isoformat() if ts > 0 else ""


def _cards(conn) -> List[dict]:
    rows = conn.execute("SELECT c.id, c.created_at, c.source, c.responsible, c.point_id, p.name, s.kind, s.code FROM order_card c "
                        "JOIN order_stage s ON s.id = c.stage_id LEFT JOIN point p ON p.id = c.point_id ORDER BY c.id")
    return [dict(zip(("id", "created_at", "source", "responsible", "point_id", "point", "kind", "code"), r)) for r in rows]


def _empty_row(key, label) -> dict:
    return {"key": key, "label": label, "chats": None, "chats_with_order": None, "orders": 0, "issued": 0, "cancelled": 0, "returned": 0, "open": 0,
            "chat_to_order": None, "order_to_issued": None}


def _count(row: dict, card: dict) -> None:
    row["orders"] += 1
    kind = card["kind"]
    if kind == "won":
        row["issued"] += 1
    elif kind == "lost":
        row["cancelled"] += 1
    elif kind == "closed":
        row["returned"] += 1
    else:
        row["open"] += 1


def _rates(row: dict) -> dict:
    """Conversions as shares (0.25 is 25 %), rounded to a ten-thousandth; None when there is nothing to divide by."""
    if row["chats"]:
        row["chat_to_order"] = round(row["chats_with_order"] / row["chats"], 4)
    if row["orders"]:
        row["order_to_issued"] = round(row["issued"] / row["orders"], 4)
    return row


def funnel(conn, start: str, end: str, by: Optional[str] = None) -> dict:
    """The funnel «chat → order → issued» of the period, whole (`by` None) or by one slice of `SLICES`.

    Orders: the orders created in the period (`created_at`, days included), counted by their stage now: «issued» is a stage of kind
    «успешный» (Выполнено), «cancelled» kind «отказ», «returned» kind «завершающий» (Возврат), «open» the rest.
    Chats: the Avito chats started in the period; `chats_with_order` of them are linked to an order (whenever it was made), and
    «chat_to_order» is that share. Whole, the orders are still those created in the period. By account, the orders are those of the
    period's chats of the account, each order once in a row (a client who wrote twice is one order) and once in the total (an order with
    chats of two accounts is in both rows, not twice in the total). By source, responsible and point there are no chats (`chats` None):
    a chat has neither of them until it has an order.
    Returns {"start", "end", "by", "rows": [...], "total": {...}, "chats_undated": n}; each row has key, label, chats, chats_with_order,
    orders, issued, cancelled, returned, open, chat_to_order, order_to_issued."""
    start, end = _period(start, end)
    if by is not None and by not in SLICES:
        raise ValueError("Разрез: один из %s." % ", ".join(SLICES))
    cards = _cards(conn)
    by_id = {c["id"]: c for c in cards}
    chats = conn.execute("SELECT id, account_id, order_id, created_ts FROM avito_chat").fetchall()
    undated = sum(1 for c in chats if not chat_day(c[3]))
    period_chats = [c for c in chats if _in(chat_day(c[3]), start, end)]
    period_cards = [c for c in cards if _in(c["created_at"], start, end)]
    rows: Dict[object, dict] = {}
    linked = [c for c in period_chats if c[2] in by_id]                    # an order of the base: not -1 (a deal being made), not a deleted one

    def row(key, label) -> dict:
        return rows.setdefault(key, _empty_row(key, label))

    if by == "account":
        names = dict(conn.execute("SELECT id, name FROM account"))
        per_row: Dict[object, set] = {}
        for _chat_id, account_id, order_id, _ts in period_chats:
            r = row(account_id, names.get(account_id, "аккаунт №%s" % account_id))
            r["chats"] = (r["chats"] or 0) + 1
            r["chats_with_order"] = (r["chats_with_order"] or 0) + (order_id in by_id)
            if order_id in by_id:
                per_row.setdefault(account_id, set()).add(order_id)
        for account_id, found in per_row.items():
            for order_id in found:
                _count(rows[account_id], by_id[order_id])
    elif by is None:
        r = row("all", "Все")
        r["chats"], r["chats_with_order"] = len(period_chats), len(linked)
        for c in period_cards:
            _count(r, c)
    else:
        for c in period_cards:
            if by == "source":
                key = c["source"] or ""
                label = orders.SOURCES.get(key, key) or NO_SOURCE
            elif by == "responsible":
                key = c["responsible"] or ""
                label = key or NO_RESPONSIBLE
            else:
                key = c["point_id"]
                label = c["point"] or NO_POINT
            _count(row(key, label), c)
    result = [_rates(r) for r in sorted(rows.values(), key=lambda r: (-r["orders"], str(r["label"])))]
    total = _empty_row("total", "Итого")
    if by == "account" or by is None:
        total["chats"] = sum(r["chats"] or 0 for r in result)
        total["chats_with_order"] = sum(r["chats_with_order"] or 0 for r in result)
    if by == "account":
        for order_id in sorted({c[2] for c in linked}):
            _count(total, by_id[order_id])
    else:
        for key in ("orders", "issued", "cancelled", "returned", "open"):
            total[key] = sum(r[key] for r in result)
    return {"start": start, "end": end, "by": by, "rows": result, "total": _rates(total), "chats_undated": undated}


def _entered(kind: str, text: str) -> Optional[str]:
    """The stage a journal record puts the order on, by its name: «Заказ создан на этапе «X»» and «Этап: «A» → «B»»."""
    if kind == "created" and text.startswith(CREATED_PREFIX) and text.endswith("»"):
        return text[len(CREATED_PREFIX):-1]
    if kind == "stage" and text.startswith(MOVE_PREFIX) and text.endswith("»") and "» → «" in text:
        return text[len(MOVE_PREFIX):-1].split("» → «", 1)[1]
    return None


def _hours(since: str, until: str) -> float:
    return (datetime.fromisoformat(until) - datetime.fromisoformat(since)).total_seconds() / 3600


def stage_times(conn, start: str, end: str, now: str, stuck_hours: Optional[float] = None) -> dict:
    """How long the orders stay on each stage, and which stay too long now.

    `stays`: per stage, the stays that **ended** in the period (the order left the stage on a day from `start` to `end`, both included):
    count, mean and median in hours. The stay an order is in now has not ended: it is not in `stays`.
    A stay cut by a deletion is not counted: after «восстановлен из удалённых» the next stay starts at the next move.
    `stuck`: the orders on an open stage (kind «обычный») now, longer than the limit: `stuck_hours` (above zero) when given, else the
    stage's own limit (`limit_hours`; a stage without a limit has no stuck orders then). `now` is the Moscow time «ГГГГ-ММ-ДДTЧЧ:ММ:СС».
    The stages are in the order of the funnel; a name of the journal that is no stage now comes last with `stage_id` None."""
    start, end = _period(start, end)
    if stuck_hours is not None and not stuck_hours > 0:
        raise ValueError("Порог «зависших»: больше нуля часов.")
    stages = orders.list_stages(conn)
    by_name = {s["name"]: s for s in stages}
    durations: Dict[str, List[float]] = {}
    last: Dict[int, tuple] = {}
    for order_id, at, kind, text in conn.execute("SELECT order_id, at, kind, text FROM order_event WHERE kind IN ('created', 'stage', 'restored') "
                                                 "ORDER BY order_id, id"):
        if kind == "restored":                                             # the time in «Удалённые» is no stay; the stage may be another now
            last.pop(order_id, None)
            continue
        name = _entered(kind, text)
        if name is None:
            continue
        previous = last.get(order_id)
        if previous is not None and kind == "stage" and _in(at, start, end):
            durations.setdefault(previous[0], []).append(_hours(previous[1], at))
        last[order_id] = (name, at)
    stays = []
    for name in [s["name"] for s in stages] + sorted(n for n in durations if n not in by_name):
        hours = durations.get(name, [])
        stage = by_name.get(name)
        if stage is None and not hours:
            continue
        stays.append({"stage_id": stage["id"] if stage else None, "name": name, "count": len(hours),
                      "mean_hours": round(statistics.mean(hours), 2) if hours else None,
                      "median_hours": round(statistics.median(hours), 2) if hours else None})
    stuck = []
    for s in stages:
        limit = stuck_hours if stuck_hours is not None else s["limit_hours"]
        if s["kind"] != "open" or not limit:
            continue
        ids = [card_id for card_id, entered in conn.execute("SELECT id, entered_at FROM order_card WHERE stage_id = ? ORDER BY id", (s["id"],))
               if _hours(entered, now) > limit]
        stuck.append({"stage_id": s["id"], "name": s["name"], "limit_hours": limit, "count": len(ids), "order_ids": ids})
    return {"start": start, "end": end, "stays": stays, "stuck": stuck}


def money(conn, start: str, end: str) -> dict:
    """The money of the period, exactly as the report of the orders counts it (`order_report.build`: the rates in force on each order's
    day, reversed sales left out, days included): revenue, profit (margin), the fees paid to the points, the net, the owner's and the
    partner's parts, the number of orders. The manual sales are a part of their own (`manual`), never in the order numbers.
    The orders here are those **sold** in the period (the day of their sale), not those created in it as in `funnel`: an order made in
    August and sold in September is August's in the funnel and September's in the money. `has_data` is False when the period has no sale
    at all (the zeros then mean «nothing», not «a loss of nothing»)."""
    start, end = _period(start, end)
    report = order_report.build(conn, start, end)
    total = report["total"]
    manual = report["manual"] or {"sales": 0, "pieces": 0, "sale": 0.0, "cost": 0.0, "margin": 0.0}
    return {"start": start, "end": end, "has_data": bool(report["lines"] or report["manual"]), "orders": total["orders"], "pieces": total["pieces"], "revenue": total["sale"], "cost": total["cost"],
            "profit": total["margin"], "point_fees": total["fee"], "net": total["net"], "owner": total["owner"], "partner": total["partner"],
            "by_point": [{"point": r["point"], "orders": r["orders"], "revenue": r["sale"], "profit": r["margin"], "point_fee": r["fee"],
                          "net": r["net"]} for r in report["lines"]],
            "manual": {"sales": manual["sales"], "pieces": manual["pieces"], "revenue": manual["sale"], "cost": manual["cost"],
                       "profit": manual["margin"]},
            "rates_used": report["rates_used"],
            "webmim": {"count": report["webmim"]["count"], "of": report["webmim"]["positions"]}}          # the positions whose purchase is WebMIM's (order_report)


WAIT_ALERT_MINUTES = 15                            # a client waiting longer than this is «горит» on the dashboard


def day_numbers(conn, now: datetime, with_money: bool = True, without_points: bool = False) -> dict:
    """The figures of the day dashboard at `now` (Moscow, naive): the orders in work, overdue and new today (the board's own summary), the
    money of today and of the last 7 days (`money`, the report of the orders), the chats unread and the clients waiting for an answer
    (`chats.waiting_minutes`, the timer of the chats page); `without_points`: not those of the points' accounts (the staff's dashboard, the
    owner 2026-10-26). Read only; no formula of its own."""
    from . import chats, order_points
    summary = orders.board(conn, now)["summary"]
    today = now.date()
    week_start = (today - timedelta(days=6)).isoformat()
    waiting = []
    enabled = {r[0] for r in conn.execute("SELECT id FROM account WHERE chats_enabled = 1")}     # the chats the panel reads (as unread_total)
    if without_points:
        enabled -= {r[0] for r in conn.execute(order_points.POINT_ACCOUNTS)}
    rows = [c for c in chats.list_chats(conn) if c["account_id"] in enabled and c["status"] == "open"]
    starts = chats.wait_starts(conn, [c["id"] for c in rows])                  # as the chats page: Avito's auto-answer is no answer
    for chat in rows:
        if chat["id"] in starts:
            chat["wait_since"] = starts[chat["id"]]
        minutes = chats.waiting_minutes(chat, now)
        if minutes is not None:
            waiting.append({"id": chat["id"], "peer_name": chat["peer_name"], "minutes": minutes, "responsible": chat.get("responsible", "")})
    waiting.sort(key=lambda w: -w["minutes"])
    return {"orders_open": summary["open"], "orders_overdue": summary["overdue"], "orders_new_today": summary["new_today"],
            "money_today": money(conn, today.isoformat(), today.isoformat()) if with_money else None,         # not counted for whom it is not shown
            "money_week": money(conn, week_start, today.isoformat()) if with_money else None,
            "chats_unread": chats.unread_total(conn, without_points), "waiting": waiting,
            "waiting_long": [w for w in waiting if w["minutes"] >= WAIT_ALERT_MINUTES]}


def my_day(conn, login: str, now: datetime, limit: int = 10) -> dict:
    """«Мои дела на сегодня»: the open orders the person answers for (overdue first, then the longest on their stage) and their chats where
    a client waits."""
    from . import chats
    stages = {s["id"]: s for s in orders.list_stages(conn)}
    mine = []
    for card_id, name, phone, stage_id, entered in conn.execute(
            "SELECT id, client_name, client_phone, stage_id, entered_at FROM order_card WHERE responsible = ? ORDER BY id", (login,)):
        stage = stages.get(stage_id)
        if stage is None or stage["kind"] != "open":
            continue
        try:
            hours = _hours(entered, now.isoformat(timespec="seconds"))
        except ValueError:                                          # a broken time never breaks the page (as orders._seconds_on_stage)
            hours = 0.0
        mine.append({"id": card_id, "client": name or phone or "Без имени", "stage": stage["name"], "hours": round(hours, 1),
                     "overdue": bool(stage["limit_hours"]) and hours > stage["limit_hours"]})
    mine.sort(key=lambda o: (not o["overdue"], -o["hours"]))
    chats_mine = []
    enabled = {r[0] for r in conn.execute("SELECT id FROM account WHERE chats_enabled = 1")}
    rows = [c for c in chats.list_chats(conn) if c["account_id"] in enabled and c["status"] == "open" and c.get("responsible") == login]
    starts = chats.wait_starts(conn, [c["id"] for c in rows])                  # as the chats page: Avito's auto-answer is no answer
    for chat in rows:
        if chat["id"] in starts:
            chat["wait_since"] = starts[chat["id"]]
        minutes = chats.waiting_minutes(chat, now)
        if minutes is not None:
            chats_mine.append({"id": chat["id"], "peer_name": chat["peer_name"], "minutes": minutes})
    chats_mine.sort(key=lambda c: -c["minutes"])
    return {"orders": mine[:limit], "orders_total": len(mine), "chats": chats_mine[:limit], "chats_total": len(chats_mine)}
