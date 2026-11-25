"""«Приехало на точку»: the tyres the point physically got for an order — the base of its fee for the issue (the owner 2026-11-25, it replaces
«отменённые заказы не входят»).

The rule: the point is paid for the tyres it physically got. An order that came to «На точке» and stayed there counts: issued
(«Выполнено» — its sale counts, order_report), cancelled after it came (the client refused: the tyres lie on «На ОТХ»), given back to the
driver («На возврат», «Вернули поставщику») — all paid as done. «На точке» pressed and then undone (the stage moved back to «Едет на точку»,
«Заказ оформлен» … — a mistake, or a cheat) takes the fee off. The tyres that never came pay nothing; when the office cancels an order it
answers «Шины физически не приезжали?» (no by default; the answer «да» is of the money right): «да» takes the fee of the order off (the
order's journal says so), «Вернуть отметку» gives it back.

The tyres are paid once (the review 2026-11-26): the tyres an order took from «На ОТХ» are not its own arrival (their first order's
arrival paid them, or they lay at the point already); a later order that takes from «На ОТХ» the tyres whose arrival was paid gets no
fee of the issue for them (paid_oth_pieces), only the bonus of a sale from «На ОТХ».

One row an order (order_arrival): the moment it came (the first of this stay), the point and the tyres of its own at that moment, the
moment it left the point without a sale (the day of the fee), the undo, the «не приезжали» of the office. Written by orders.move_card
(and orders.create_card on «На точке»), so the office's board, the card, the bulk move, the chats and the point's «Рабочее место»
(also «Работать как точка») give the same result. Counted from ARRIVAL_FROM: earlier arrivals are never recounted."""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional

ARRIVAL_FROM = "2026-11-27"          # the first day the arrival pays (with POINT_FEE_PER_TYRE_FROM): a past report is never recounted
HERE = "at_point"
BEFORE = ("new", "talk", "wait", "placed", "moving", "from_point")     # the stages before the tyres come: a move back there is an undo
UNDONE = "Начисление точке за выдачу снято: этап «На точке» отменён (шины не считаются полученными)"
NONE = "Шины физически не приезжали на точку: начисление точке за выдачу по заказу снято"
NONE_BACK = "Отметка «шины не приезжали» снята: начисление точке за выдачу по заказу возвращено"
FIELDS = "arrived_at, undone_at, none_at, point_id, pieces, left_at"


def _row(conn, order_id: int) -> Optional[dict]:
    row = conn.execute("SELECT %s FROM order_arrival WHERE order_id = ?" % FIELDS, (int(order_id),)).fetchone()
    return dict(zip([f.strip() for f in FIELDS.split(",")], row)) if row else None


def _active(row: Optional[dict]) -> bool:
    return bool(row and row["arrived_at"] and not row["undone_at"] and not row["none_at"])


def arrived(conn, order_id: int) -> bool:
    """The order's tyres count as got by the point now: came, not undone, not «не приезжали»."""
    return _active(_row(conn, order_id))


def own_pieces(conn, order_id: int) -> int:
    """The tyres of the order that are its own now: its positions less those taken from «На ОТХ» (order_item_oth, not given back)."""
    total = conn.execute("SELECT COALESCE(SUM(qty), 0) FROM order_item WHERE order_id = ? AND qty > 0", (int(order_id),)).fetchone()[0]
    taken = conn.execute("SELECT COALESCE(SUM(t.qty), 0) FROM order_item_oth t JOIN order_item i ON i.id = t.item_id "
                         "WHERE t.order_id = ? AND t.undone_at = ''", (int(order_id),)).fetchone()[0]
    return max(int(total) - int(taken), 0)


def came(conn, order_id: int, now: str, author: str) -> None:
    """The order came to «На точке» (no commit): the point and its own tyres of this moment are kept. A stay already counted keeps its
    first moment (back on the point after a cancel: it has not left); an undone one or «не приезжали» starts anew."""
    row = _row(conn, order_id)
    if _active(row):
        if row["left_at"]:
            conn.execute("UPDATE order_arrival SET left_at = NULL WHERE order_id = ?", (int(order_id),))
        return
    point = conn.execute("SELECT point_id FROM order_card WHERE id = ?", (int(order_id),)).fetchone()
    conn.execute("INSERT INTO order_arrival (order_id, arrived_at, author, point_id, pieces) VALUES (?,?,?,?,?) ON CONFLICT(order_id) DO UPDATE "
                 "SET arrived_at = excluded.arrived_at, author = excluded.author, point_id = excluded.point_id, pieces = excluded.pieces, "
                 "undone_at = NULL, undone_by = NULL, none_at = NULL, none_by = NULL, left_at = NULL",
                 (int(order_id), now, author, point[0] if point else None, own_pieces(conn, order_id)))


def moved(conn, order_id: int, was_code: str, code: str, now: str, author: str, kind: str = "") -> None:
    """orders.move_card, in its transaction: «На точке» — came; back from «На точке» to a stage before it — undone (the journal says so);
    to «Отмена», a closed stage or «На ОТХ» / «На возврат» — the order left the point: the day of its fee without a sale."""
    from . import orders
    if code == HERE:
        came(conn, order_id, now, author)
        return
    row = _row(conn, order_id)
    if not _active(row):
        return
    if was_code == HERE and code in BEFORE:
        conn.execute("UPDATE order_arrival SET undone_at = ?, undone_by = ? WHERE order_id = ?", (now, author, int(order_id)))
        orders.add_event(conn, int(order_id), now, author, "field", UNDONE)
    elif not row["left_at"] and (kind in ("lost", "closed", "won") or code in orders.RETURN_STAGES):
        conn.execute("UPDATE order_arrival SET left_at = ? WHERE order_id = ?", (now, int(order_id)))


def mark_none(conn, order_id: int, now: str, author: str) -> bool:
    """«Шины физически не приезжали» of the office's «Отмена» (no commit): the fee of the order is taken off; the journal and the audit
    get it. True if it changed."""
    from . import orders, store
    row = _row(conn, order_id)
    if row and row["none_at"]:
        return False
    conn.execute("INSERT INTO order_arrival (order_id, arrived_at, author, none_at, none_by) VALUES (?, '', ?, ?, ?) ON CONFLICT(order_id) "
                 "DO UPDATE SET none_at = excluded.none_at, none_by = excluded.none_by", (int(order_id), author, now, author))
    orders.add_event(conn, int(order_id), now, author, "field", NONE)
    store.write_audit(conn, now, author, "order_no_arrival", {"order": int(order_id), "was": _active(row)})
    return True


def unmark_none(conn, order_id: int, now: str, author: str) -> bool:
    """«Вернуть отметку» of the office (no commit): the arrival, if there was one, counts again. True if it changed."""
    from . import orders, store
    row = _row(conn, order_id)
    if not row or not row["none_at"]:
        return False
    conn.execute("UPDATE order_arrival SET none_at = NULL, none_by = NULL WHERE order_id = ?", (int(order_id),))
    orders.add_event(conn, int(order_id), now, author, "field", NONE_BACK)
    store.write_audit(conn, now, author, "order_no_arrival_undone", {"order": int(order_id)})
    return True


def state(conn, order_id: int) -> Dict[str, object]:
    """For the order window: {arrived, none, at}."""
    row = _row(conn, order_id)
    return {"arrived": _active(row), "none": bool(row and row["none_at"]), "at": (row["arrived_at"] if row else "") or ""}


def _paid(conn) -> Dict[int, dict]:
    """Order → {point_id, day, pieces} of every arrival that pays now, whatever its day: came from ARRIVAL_FROM, not undone, not «не
    приезжали», with tyres of its own, the order not a record of storage."""
    from . import oth_storage
    records = oth_storage.record_ids(conn)
    out = {}
    for order_id, point_id, arrived_at, left_at, pieces in conn.execute(
            "SELECT order_id, point_id, arrived_at, left_at, pieces FROM order_arrival WHERE arrived_at != '' AND undone_at IS NULL "
            "AND none_at IS NULL AND substr(arrived_at, 1, 10) >= ? AND point_id IS NOT NULL AND pieces > 0", (ARRIVAL_FROM,)):
        if order_id not in records:
            out[order_id] = {"point_id": point_id, "day": str(left_at or arrived_at)[:10], "pieces": int(pieces)}
    return out


def unsold(conn, start: str, end: str) -> List[dict]:
    """The orders the point got and that left it without a sale (ARRIVAL_FROM on), by the day they left (a cancel, «На ОТХ», a return) in
    start..end: cancelled after they came, on «На ОТХ» / «На возврат», given back to the supplier, finished without a sale. Not those
    still on «На точке» (in work: the sale or the cancel says what they are), not a record of storage (its tyres are the cancelled order's),
    not an order with a sale (the sale counts it, order_report), never the tyres taken from «На ОТХ» (paid by their own arrival). The point
    and the tyres are those of the arrival. [{order, point_id, day, pieces}]."""
    from . import orders
    paid = _paid(conn)
    if not paid:
        return []
    stages = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT c.id, g.code, g.kind FROM order_card c JOIN order_stage g ON g.id = c.stage_id")}
    sold = {r[0] for r in conn.execute("SELECT DISTINCT note FROM sale WHERE source = 'order' AND reversed_at IS NULL")}
    out = []
    for order_id, found in sorted(paid.items()):
        code, kind = stages.get(order_id, ("", ""))
        if not (start <= found["day"] <= end) or code == HERE or (kind == "open" and code not in orders.RETURN_STAGES) or not code \
                or "Заказ №%d" % order_id in sold:
            continue
        out.append(dict(found, order=order_id))
    return out


def paid_oth_pieces(conn, order_ids: Iterable[int]) -> Dict[int, int]:
    """Order → the tyres it took from «На ОТХ» whose arrival was paid already (the order they came with — the source of the record of
    storage, or the order that stood on «На ОТХ» the old way — has a paying arrival): its fee of the issue is not paid for them again."""
    ids = sorted({int(i) for i in order_ids if i})
    if not ids:
        return {}
    from . import oth_storage
    paid = _paid(conn)
    if not paid:
        return {}
    out: Dict[int, int] = {}
    for chunk in range(0, len(ids), 500):
        part = ids[chunk:chunk + 500]
        for order_id, from_order, qty in conn.execute(
                "SELECT order_id, from_order, qty FROM order_item_oth WHERE undone_at = '' AND order_id IN (%s)" % ",".join("?" * len(part)), part):
            source = oth_storage.source_of(conn, from_order) or from_order
            if source in paid:
                out[order_id] = out.get(order_id, 0) + int(qty)
    return out
