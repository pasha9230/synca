"""«Приехало на точку»: the tyres the point physically got for an order — the base of its fee for the issue (the owner 2026-11-25, it replaces
«отменённые заказы не входят»).

The rule: the point is paid for the tyres it physically got. An order that came to «На точке» and stayed there counts: issued
(«Выполнено» — its sale counts, order_report), cancelled after it came (the client refused: the tyres lie on «На ОТХ»), given back to the
driver («На возврат», «Вернули поставщику») — all paid as done. «На точке» pressed and then undone (the stage moved back to «Едет на точку»,
«Заказ оформлен» … — a mistake, or a cheat) takes the fee off. The tyres that never came pay nothing; when the office cancels an order it
answers «Шины физически не приезжали?» (no by default): «да» takes the fee of the order off (the order's journal says so).

One row an order (order_arrival): the moment it came (the first of this stay), the undo, the «не приезжали» of the office. Written by
orders.move_card (and orders.create_card on «На точке»), so the office's board, the card, the bulk move, the chats and the point's
«Рабочее место» (also «Работать как точка») give the same result. Counted from ARRIVAL_FROM: earlier arrivals are never recounted."""
from __future__ import annotations

from typing import Dict, List, Optional

ARRIVAL_FROM = "2026-11-27"          # the first day the arrival pays (with POINT_FEE_PER_TYRE_FROM): a past report is never recounted
HERE = "at_point"
BEFORE = ("new", "talk", "wait", "placed", "moving", "from_point")     # the stages before the tyres come: a move back there is an undo
UNDONE = "Начисление точке за выдачу снято: этап «На точке» отменён (шины не считаются полученными)"
NONE = "Шины физически не приезжали на точку: начисление точке за выдачу по заказу снято"
NONE_BACK = "Отметка «шины не приезжали» снята: начисление точке за выдачу по заказу возвращено"


def _row(conn, order_id: int) -> Optional[tuple]:
    return conn.execute("SELECT arrived_at, undone_at, none_at FROM order_arrival WHERE order_id = ?", (int(order_id),)).fetchone()


def arrived(conn, order_id: int) -> bool:
    """The order's tyres count as got by the point now: came, not undone, not «не приезжали»."""
    row = _row(conn, order_id)
    return bool(row and row[0] and not row[1] and not row[2])


def came(conn, order_id: int, now: str, author: str) -> None:
    """The order came to «На точке» (no commit). A stay already counted keeps its first moment; an undone one or «не приезжали» starts anew."""
    row = _row(conn, order_id)
    if row and row[0] and not row[1] and not row[2]:
        return
    conn.execute("INSERT INTO order_arrival (order_id, arrived_at, author) VALUES (?,?,?) ON CONFLICT(order_id) DO UPDATE SET "
                 "arrived_at = excluded.arrived_at, author = excluded.author, undone_at = NULL, undone_by = NULL, none_at = NULL, none_by = NULL",
                 (int(order_id), now, author))


def moved(conn, order_id: int, was_code: str, code: str, now: str, author: str) -> None:
    """orders.move_card, in its transaction: «На точке» — came; back from «На точке» to a stage before it — undone (the journal says so)."""
    from . import orders
    if code == HERE:
        came(conn, order_id, now, author)
    elif was_code == HERE and code in BEFORE and arrived(conn, order_id):
        conn.execute("UPDATE order_arrival SET undone_at = ?, undone_by = ? WHERE order_id = ?", (now, author, int(order_id)))
        orders.add_event(conn, int(order_id), now, author, "field", UNDONE)


def mark_none(conn, order_id: int, now: str, author: str) -> bool:
    """«Шины физически не приезжали» of the office's «Отмена» (no commit): the fee of the order is taken off; the journal and the audit
    get it. True if it changed."""
    from . import orders, store
    row = _row(conn, order_id)
    if row and row[2]:
        return False
    conn.execute("INSERT INTO order_arrival (order_id, arrived_at, author, none_at, none_by) VALUES (?, '', ?, ?, ?) ON CONFLICT(order_id) "
                 "DO UPDATE SET none_at = excluded.none_at, none_by = excluded.none_by", (int(order_id), author, now, author))
    orders.add_event(conn, int(order_id), now, author, "field", NONE)
    store.write_audit(conn, now, author, "order_no_arrival", {"order": int(order_id), "was": bool(row and row[0] and not row[1])})
    return True


def unmark_none(conn, order_id: int, now: str, author: str) -> bool:
    """The office takes «не приезжали» back (no commit): the arrival, if there was one, counts again. True if it changed."""
    from . import orders, store
    row = _row(conn, order_id)
    if not row or not row[2]:
        return False
    conn.execute("UPDATE order_arrival SET none_at = NULL, none_by = NULL WHERE order_id = ?", (int(order_id),))
    orders.add_event(conn, int(order_id), now, author, "field", NONE_BACK)
    store.write_audit(conn, now, author, "order_no_arrival_undone", {"order": int(order_id)})
    return True


def state(conn, order_id: int) -> Dict[str, object]:
    """For the order window: {arrived, none, at}."""
    row = _row(conn, order_id)
    return {"arrived": bool(row and row[0] and not row[1] and not row[2]), "none": bool(row and row[2]), "at": (row[0] if row else "") or ""}


def unsold(conn, start: str, end: str) -> List[dict]:
    """The orders the point got and that left it without a sale (ARRIVAL_FROM on, the day of the arrival in start..end): cancelled after they
    came, on «На ОТХ» / «На возврат», given back to the supplier, finished without a sale. Not those still on «На точке» (in work: the sale
    or the cancel says what they are), not a record of storage (its tyres are the cancelled order's), not an order with a sale (the sale
    counts it, order_report). [{order, point_id, day, pieces}]."""
    from . import orders, oth_storage
    first = max(start, ARRIVAL_FROM)
    if first > end:
        return []
    rows = conn.execute(
        "SELECT a.order_id, c.point_id, substr(a.arrived_at, 1, 10), g.code, g.kind FROM order_arrival a JOIN order_card c ON c.id = a.order_id "
        "JOIN order_stage g ON g.id = c.stage_id WHERE a.arrived_at != '' AND a.undone_at IS NULL AND a.none_at IS NULL "
        "AND substr(a.arrived_at, 1, 10) >= ? AND substr(a.arrived_at, 1, 10) <= ? AND c.point_id IS NOT NULL", (first, end)).fetchall()
    if not rows:
        return []
    records = oth_storage.record_ids(conn)
    sold = {r[0] for r in conn.execute("SELECT DISTINCT note FROM sale WHERE source = 'order' AND reversed_at IS NULL")}
    out = []
    for order_id, point_id, day, code, kind in rows:
        if code == HERE or (kind == "open" and code not in orders.RETURN_STAGES) or order_id in records or "Заказ №%d" % order_id in sold:
            continue
        pieces = conn.execute("SELECT COALESCE(SUM(qty), 0) FROM order_item WHERE order_id = ? AND qty > 0", (order_id,)).fetchone()[0]
        out.append({"order": order_id, "point_id": point_id, "day": day, "pieces": int(pieces)})
    return out
