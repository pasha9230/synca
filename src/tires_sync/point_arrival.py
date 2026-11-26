"""«Приехало на точку»: the tyres the point physically got for an order — the base of its fee for the issue (the owner 2026-11-25, it replaces
«отменённые заказы не входят»).

The rule: the point is paid for the tyres it physically got. An order that came to «На точке» and stayed there counts: issued
(«Выполнено» — its sale counts, order_report), cancelled after it came (the client refused: the tyres lie on «На ОТХ»), given back to the
driver («На возврат», «Вернули поставщику») — all paid as done. «На точке» pressed and then undone (the stage moved back to «Едет на точку»,
«Заказ оформлен» … — a mistake, or a cheat) takes the fee off. The tyres that never came pay nothing; when the office cancels an order it
answers «Шины физически не приезжали?» (no by default; the answer «да» is of the money right): «да» takes the fee of the order off (the
order's journal says so), «Вернуть отметку» gives it back.

One fee a chain (the owner 2026-11-26, it replaces «приезд + продажа с ОТХ»): the point is paid for the END of the way of the tyres,
not for the arrival. Came → … → sold («Выполнено»: the sale pays, order_report) or given back to the supplier («На возврат» /
«Вернули поставщику»: the arrival pays, on that day). Tyres lying on «На ОТХ» (the order itself, its record of storage, «Согласование
возврата») wait: nothing is paid until they are sold or go back. Sold from «На ОТХ» — by the office, by the point, to a new client
(oth_swap) — the sale is the one fee of the chain (and the bonus of «На ОТХ» when the point itself sold); the arrival that brought
them pays nothing. A part sold, the rest given back: the rest is paid by the arrival. The tyres an order took from «На ОТХ» are not its
own arrival. A cancel with no tyres on «На ОТХ» (no record of storage) ends the chain: paid on its day, as before.

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


def recount(conn, order_id: int) -> None:
    """The tyres of an arrival counted again (no commit): an order made right on «На точке» gets its positions after (order_merge); one
    left with no tyres of its own (order_merge.unmerge: they went back) has no arrival any more."""
    if not arrived(conn, order_id):
        return
    pieces = own_pieces(conn, order_id)
    if pieces:
        conn.execute("UPDATE order_arrival SET pieces = ? WHERE order_id = ?", (pieces, int(order_id)))
    else:
        conn.execute("DELETE FROM order_arrival WHERE order_id = ?", (int(order_id),))


def merged(conn, order_id: int, sources: Iterable[int]) -> None:
    """The arrival of an order made by a merge on «На точке» (no commit): its tyres after the move, its moment the earliest arrival of the
    sources that came (an arrival before ARRIVAL_FROM stays unpaid), else its own."""
    recount(conn, order_id)
    ids = [int(s) for s in sources]
    if not ids or not arrived(conn, order_id):
        return
    first = conn.execute("SELECT MIN(arrived_at) FROM order_arrival WHERE arrived_at != '' AND undone_at IS NULL AND none_at IS NULL "
                         "AND order_id IN (%s)" % ",".join("?" * len(ids)), ids).fetchone()[0]
    if first:
        conn.execute("UPDATE order_arrival SET arrived_at = ? WHERE order_id = ? AND arrived_at > ?", (first, int(order_id), first))


def back_from_merge(conn, order_id: int, code: str, kind: str) -> None:
    """A source given back by «Разъединить» stands on its stage again (no commit): on an open stage before «На ОТХ» it has not left the
    point — its day of the fee goes."""
    from . import orders
    if kind == "open" and code not in orders.RETURN_STAGES:
        conn.execute("UPDATE order_arrival SET left_at = NULL WHERE order_id = ?", (int(order_id),))


GONE_BACK = ("return_given", "return")    # «На возврат», «Вернули поставщику»: the tyres go back to the supplier — the end of the chain
WAITING = ("oth", "return_agree")          # «На ОТХ», «Согласование возврата»: the tyres lie at the point, the chain goes on


def _gone_back(was_code: str, code: str) -> bool:
    """The move gives the tyres back to the supplier now (from «На ОТХ» / «Согласование возврата», or straight)."""
    return code in GONE_BACK and was_code not in GONE_BACK


def moved(conn, order_id: int, was_code: str, code: str, now: str, author: str, kind: str = "") -> None:
    """orders.move_card, in its transaction: «На точке» — came; back from «На точке» to a stage before it — undone (the journal says so).
    The end of the chain without a sale — the day of the fee (left_at): «На возврат» / «Вернули поставщику» (of the order, or of its record
    of storage: the arrival is the cancelled order's), «Отмена» with no record of storage, another closed stage, «Выполнено». «На ОТХ»,
    «Согласование возврата» and the cancel of the split (the tyres are the record's now) are no end: the tyres wait (the owner 2026-11-26).
    A day written by the rule before (on «На ОТХ») gives way to the day of the return."""
    from . import oth_storage
    if code == HERE:
        came(conn, order_id, now, author)
        return
    row = _row(conn, order_id)
    if not _active(row):
        source = oth_storage.source_of(conn, order_id) if _gone_back(was_code, code) else None
        if source is not None and arrived(conn, source):                # the record goes back: the arrival of its order ends
            conn.execute("UPDATE order_arrival SET left_at = ? WHERE order_id = ?", (now, int(source)))
        return
    if was_code == HERE and code in BEFORE:
        from . import orders
        conn.execute("UPDATE order_arrival SET undone_at = ?, undone_by = ? WHERE order_id = ?", (now, author, int(order_id)))
        orders.add_event(conn, int(order_id), now, author, "field", UNDONE)
    elif _gone_back(was_code, code) and was_code in WAITING:
        conn.execute("UPDATE order_arrival SET left_at = ? WHERE order_id = ?", (now, int(order_id)))
    elif not row["left_at"] and (code in GONE_BACK or (kind in ("closed", "won"))
                                 or (kind == "lost" and oth_storage.record_of(conn, order_id) is None)):
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
    # the sources of a merge not undone: their tyres are the merged order's (its own arrival, recount), never paid twice (the review 2026-11-26)
    records |= {r[0] for r in conn.execute("SELECT s.order_id FROM order_merge_source s JOIN order_merge m ON m.id = s.merge_id "
                                           "WHERE m.undone_at = '' AND s.ref = 0")}
    out = {}
    for order_id, point_id, arrived_at, left_at, pieces in conn.execute(
            "SELECT order_id, point_id, arrived_at, left_at, pieces FROM order_arrival WHERE arrived_at != '' AND undone_at IS NULL "
            "AND none_at IS NULL AND substr(arrived_at, 1, 10) >= ? AND point_id IS NOT NULL AND pieces > 0", (ARRIVAL_FROM,)):
        if order_id not in records:
            out[order_id] = {"point_id": point_id, "day": str(left_at or arrived_at)[:10], "pieces": int(pieces), "left": bool(left_at)}
    return out


def unsold(conn, start: str, end: str) -> List[dict]:
    """The arrivals (ARRIVAL_FROM on) whose chain ended without a sale of their tyres, by the day it ended (left_at) in start..end: given
    back to the supplier (the order or its record of storage on «На возврат» / «Вернули поставщику»), cancelled with no record, closed.
    Not those still on «На точке» or waiting on «На ОТХ» / «Согласование возврата» (the order or its record: the chain goes on), not a
    record of storage (its tyres are the cancelled order's), not an order with a sale of its own (the sale is the fee of the chain,
    order_report). The tyres sold from «На ОТХ» by another order (taken from the order or its record, the sale not reversed) and the
    sale of the record itself are that sale's: the arrival pays the rest only. The point is the arrival's. [{order, point_id, day, pieces}]."""
    from . import oth_storage
    paid = _paid(conn)
    if not paid:
        return []
    stages = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT c.id, g.code, g.kind FROM order_card c JOIN order_stage g ON g.id = c.stage_id")}
    sold = _sold_pieces(conn)
    out = []
    for order_id, found in sorted(paid.items()):
        code, kind = stages.get(order_id, ("", ""))
        if not found["left"] or not (start <= found["day"] <= end) or not code or "Заказ №%d" % order_id in sold:
            continue
        if kind == "open" and code not in GONE_BACK:                       # in work, or the tyres wait on «На ОТХ»
            continue
        record = oth_storage.record_of(conn, order_id) if kind == "lost" else None
        if record is not None and stages.get(record, ("", ""))[0] not in GONE_BACK:
            continue                                                       # the record holds them, sold whole or taken: not given back
        chain = [order_id] + ([record] if record is not None else [])
        pieces = found["pieces"] - _sold_elsewhere(conn, chain, sold)
        if pieces > 0:
            out.append(dict(found, order=order_id, pieces=pieces))
    return out


def _sold_pieces(conn) -> Dict[str, int]:
    """The note of an order («Заказ №N») → the tyres of its sales not reversed."""
    return {r[0]: int(r[1]) for r in conn.execute("SELECT note, SUM(pieces) FROM sale WHERE source = 'order' AND reversed_at IS NULL GROUP BY note")}


def _sold_elsewhere(conn, chain: List[int], sold: Dict[str, int]) -> int:
    """The tyres of the chain (the order, its record) a sale took: taken from «На ОТХ» by an order with a sale, or the record's own sale."""
    marks = ",".join("?" * len(chain))
    taken = sum(int(qty) for order, qty in conn.execute(
        "SELECT order_id, SUM(qty) FROM order_item_oth WHERE from_order IN (%s) AND undone_at = '' GROUP BY order_id" % marks, chain)
        if "Заказ №%d" % order in sold)
    return taken + sum(sold.get("Заказ №%d" % r, 0) for r in chain[1:])


def sold_from_oth(conn, order_ids: Iterable[int]) -> set:
    """Those of `order_ids` sold from «На ОТХ» themselves (order_oth.sell of the order or of its record, by the point or the office, and the
    order of the point's sale of a record, oth_point_sale): all their tyres came from «На ОТХ» of their point (order_report.oth_pieces)."""
    from . import order_oth
    ids = sorted({int(i) for i in order_ids})
    found = set()
    for chunk in range(0, len(ids), 500):
        part = ids[chunk:chunk + 500]
        found |= {r[0] for r in conn.execute(
            "SELECT DISTINCT order_id FROM order_event WHERE order_id IN (%s) AND ((kind = 'point' AND (text = ? OR text LIKE ?)) "
            "OR (kind = 'field' AND text = ?))" % ",".join("?" * len(part)),
            part + [order_oth.POINT_SOLD, order_oth.POINT_SOLD + ":%", order_oth.OFFICE_SOLD])}
    return found
