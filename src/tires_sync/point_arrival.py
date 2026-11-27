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
(also «Работать как точка») give the same result. Counted from ARRIVAL_FROM (every arrival written)."""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional

# order_arrival is written from this day (the deploy of #74); the arrivals before it are read from the journal (_journal_arrivals). The
# owner 2026-11-26: one fee, on the day of the end of the chain; an end before 2026-10-23 was settled (order_report.POINT_FEE_PER_TYRE_FROM)
ARRIVAL_FROM = "2026-11-26"
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
    if kind == "open" and code not in GONE_BACK:                   # «На ОТХ» too: the tyres wait there, no end of the chain
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
    A day written by the rule before (on «На ОТХ») gives way to the day of the return. The end of a holder of tyres of other arrivals (a
    record of storage, an order that took them from «На ОТХ», its record…) is written to those arrivals too, back along the chain
    (_upstream): a return at the end of the chain pays the first arrival (the review 2026-11-26)."""
    from . import oth_storage
    if code == HERE:
        came(conn, order_id, now, author)
        return
    back = _gone_back(was_code, code)
    end = back or kind in ("closed", "won") or (kind == "lost" and oth_storage.record_of(conn, order_id) is None)
    row = _row(conn, order_id)
    if _active(row):
        if was_code == HERE and code in BEFORE:
            from . import orders
            conn.execute("UPDATE order_arrival SET undone_at = ?, undone_by = ? WHERE order_id = ?", (now, author, int(order_id)))
            orders.add_event(conn, int(order_id), now, author, "field", UNDONE)
            return
        if (back and was_code in WAITING) or (end and not row["left_at"]):
            conn.execute("UPDATE order_arrival SET left_at = ? WHERE order_id = ?", (now, int(order_id)))
    if end:
        for up in _upstream(conn, int(order_id)):
            if back:                                                    # the return: its day, whatever was written
                conn.execute("UPDATE order_arrival SET left_at = ? WHERE order_id = ? AND undone_at IS NULL", (now, up))
            else:
                conn.execute("UPDATE order_arrival SET left_at = ? WHERE order_id = ? AND left_at IS NULL AND undone_at IS NULL", (now, up))


def _upstream(conn, order_id: int, seen: Optional[set] = None) -> List[int]:
    """The orders whose tyres `order_id` holds: the order of a record of storage, the orders it took tyres from «На ОТХ» of (not given
    back), and theirs, back to the first arrival."""
    from . import oth_storage
    seen = seen if seen is not None else {order_id}
    found = [oth_storage.source_of(conn, order_id)]
    found += [r[0] for r in conn.execute("SELECT DISTINCT from_order FROM order_item_oth WHERE order_id = ? AND undone_at = ''", (order_id,))]
    out: List[int] = []
    for up in found:
        if up is not None and up not in seen:
            seen.add(up)
            out += [up] + _upstream(conn, up, seen)
    return out


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


NO_STATUS_STAGES = ("cancel", "oth", "return_agree", "return_given", "return")   # where an order cancelled by that reason stands


def no_status(conn) -> set:
    """The orders cancelled with «точка не передала статус» (the owner 2026-11-27, orders.NO_STATUS: the tyres came, the point did not
    pass the status, the client cancelled after waiting): their arrival pays nothing — not «к начислению», not in the sums, nor a return of
    their tyres to the supplier later. The office's choice in «Отменить заказ» and the point's «ОТКАЗ» (in the reason, any spelling). The
    order back on its way (the reason cleared, another stage) pays as before. An arrival already settled («Рассчитан») stays as it was."""
    from . import orders
    found = set()
    for order_id, reason in conn.execute(
            "SELECT c.id, c.cancel_reason FROM order_card c JOIN order_stage s ON s.id = c.stage_id WHERE c.cancel_reason != '' "
            "AND s.code IN (%s)" % ",".join("?" * len(NO_STATUS_STAGES)), NO_STATUS_STAGES):
        if orders.NO_STATUS in str(reason or "").casefold().replace("ё", "е"):
            found.add(int(order_id))
    if found:
        settled = {r[0] for r in conn.execute("SELECT item FROM point_paid WHERE cancelled_at IS NULL AND item LIKE 'order:%'")}
        found = {o for o in found if "order:%d" % o not in settled}
    return found


def _paid(conn) -> Dict[int, dict]:
    """Order → {point_id, day, pieces} of every arrival that pays now, whatever its day: came from ARRIVAL_FROM, not undone, not «не
    приезжали», with tyres of its own, the order not a record of storage, not cancelled with «точка не передала статус» (no_status)."""
    from . import oth_storage
    records = oth_storage.record_ids(conn)
    unpaid = no_status(conn)
    # the sources of a merge not undone: their tyres are the merged order's (its own arrival, recount), never paid twice (the review 2026-11-26)
    records |= {r[0] for r in conn.execute("SELECT s.order_id FROM order_merge_source s JOIN order_merge m ON m.id = s.merge_id "
                                           "WHERE m.undone_at = '' AND s.ref = 0")}
    out = {}
    for order_id, point_id, arrived_at, left_at, pieces in conn.execute(
            "SELECT order_id, point_id, arrived_at, left_at, pieces FROM order_arrival WHERE arrived_at != '' AND undone_at IS NULL "
            "AND none_at IS NULL AND substr(arrived_at, 1, 10) >= ? AND point_id IS NOT NULL AND pieces > 0", (ARRIVAL_FROM,)):
        if order_id not in records and order_id not in unpaid:
            out[order_id] = {"point_id": point_id, "day": str(left_at or arrived_at)[:10], "pieces": int(pieces), "left": bool(left_at),
                             "arrived": str(arrived_at)[:10]}
    written = {r[0] for r in conn.execute("SELECT order_id FROM order_arrival")}
    for order_id, found in _journal_arrivals(conn, written | records | unpaid).items():
        out.setdefault(order_id, found)
    return out


# the owner 2026-11-26: the points are settled up to 2026-10-22; from this day the fee is the chain's (75 ₽ a tyre, order_report). The
# arrivals before order_arrival (ARRIVAL_FROM) are read from the journal of the order: its moves of the stage (orders.move_card writes
# «Этап: «A» → «B»» with the names of the stages)
JOURNAL_FROM = "2026-10-23"
_MOVE = re.compile(r"^Этап: «(.*)» → «(.*)»$")
_CREATED = re.compile(r"^Заказ создан на этапе «(.*)»$")             # orders.create_card


def got_tyres(conn, order_id: int) -> bool:
    """The tyres of the order came to the point (the owner 2026-11-26: «Невыкуп», not «Отмена»): its arrival, as written (not undone, not
    «шины не приезжали»); an order of before order_arrival — a stay on «На точке» in its journal, not undone (as _journal_arrivals)."""
    row = _row(conn, order_id)
    if row is not None:
        return _active(row)
    names = {code: name for code, name in conn.execute("SELECT code, name FROM order_stage")}
    here, before = names.get(HERE), {names[c] for c in BEFORE if c in names}
    came = False
    for kind, text in conn.execute("SELECT kind, text FROM order_event WHERE order_id = ? AND kind IN ('stage', 'created') ORDER BY at, id",
                                   (int(order_id),)):
        found = (_MOVE if kind == "stage" else _CREATED).match(str(text or ""))
        if not found:
            continue
        was, now = (found.group(1), found.group(2)) if kind == "stage" else ("", found.group(1))
        if now == here:
            came = True
        elif came and was == here and now in before:
            came = False                                                  # undone
    return came


def _journal_arrivals(conn, skip: set) -> Dict[int, dict]:
    """Order → {point_id, day, pieces, left, arrived, journal} of the arrivals read from the journal (orders with no order_arrival, not in
    `skip`): the first move to «На точке» of the last stay, whenever it was (the owner 2026-11-26: one fee, on the day of the end; only an end from JOURNAL_FROM on pays) (a move back to a stage before it undoes the stay, as
    `moved`); its end (left) — the first move after it to «На возврат» / «Вернули поставщику», «Отмена» with no record of storage, another
    closed stage or «Выполнено»; for an order cancelled into a record of storage, the record's move to «На возврат» / «Вернули
    поставщику» or its closing. The point is the order's now (none written then); the tyres — those of the record (its split), else the
    order's own now. Nothing is guessed: no move to «На точке» in the journal — no arrival."""
    from . import oth_storage
    stages = list(conn.execute("SELECT code, name, kind FROM order_stage"))
    names = {code: name for code, name, _kind in stages}
    kinds = {name: kind for _code, name, kind in stages}
    here = names.get(HERE)
    if not here:
        return {}
    before = {names[c] for c in BEFORE if c in names}
    back = {names[c] for c in GONE_BACK if c in names}
    waiting = {names[c] for c in WAITING if c in names}
    moves: Dict[int, List[tuple]] = {}
    for order_id, at, kind, text in conn.execute("SELECT order_id, at, kind, text FROM order_event WHERE kind IN ('stage', 'created') "
                                                 "ORDER BY order_id, at, id"):    # an arrival of before 23.10 too
        found = (_MOVE if kind == "stage" else _CREATED).match(str(text or ""))
        if found:                                                         # made right on a stage (a merge, a waitlist, a chat, the shop)
            moves.setdefault(order_id, []).append((str(at),) + ((found.group(1), found.group(2)) if kind == "stage" else ("", found.group(1))))
    cards = {r[0]: r[1] for r in conn.execute("SELECT id, point_id FROM order_card")}
    stored: Dict[int, int] = {}                                           # the order → its record of storage (oth_storage.record_of)
    for key, value in conn.execute("SELECT key, value FROM app_state WHERE key LIKE ?", (oth_storage.OF.replace("%d", "%"),)):
        digits = key.rsplit(":", 1)[1]
        if digits.isdigit() and str(value).strip().isdigit():
            stored[int(digits)] = int(str(value).strip())
    out: Dict[int, dict] = {}
    for order_id, found in moves.items():
        if order_id in skip or cards.get(order_id) is None:
            continue
        record = stored.get(order_id)
        came_at, left = None, None
        for at, was, now in found:
            if now == here:
                if came_at is None:
                    came_at = at
                left = None                                               # back on the point: it has not left
            elif came_at is not None:
                if was == here and now in before:
                    came_at, left = None, None                            # undone
                elif now in back and (left is None or was in waiting):
                    left = at
                elif left is None and (kinds.get(now) in ("closed", "won") or (kinds.get(now) == "lost" and record is None)):
                    left = at
        if came_at is None:                                               # 26.11 too: before the deploy of order_arrival
            continue
        if record is not None:
            mark = oth_storage.record(conn, record) or {}
            pieces = int(mark.get("pieces") or 0)
            for at, was, now in moves.get(record, []):                    # the record's end: given back, or closed
                if (now in back and was not in back) or (left is None and kinds.get(now) in ("closed", "lost", "won")):
                    left = at
        else:
            pieces = own_pieces(conn, order_id)
        if left and left[:10] < JOURNAL_FROM:                             # the end before 23.10: settled with the points
            continue
        if pieces > 0:
            out[order_id] = {"point_id": cards[order_id], "day": str(left or came_at)[:10], "pieces": pieces, "left": bool(left),
                             "arrived": came_at[:10], "journal": True}
    return out


def unsold(conn, start: str, end: str) -> List[dict]:
    """The arrivals (ARRIVAL_FROM on) whose chain ended without a sale of their tyres, by the day it ended (left_at) in start..end: given
    back to the supplier (the order or its record of storage on «На возврат» / «Вернули поставщику»), cancelled with no record, closed.
    Not those still on «На точке» or waiting on «На ОТХ» / «Согласование возврата» (the order or its record: the chain goes on), not a
    record of storage (its tyres are the cancelled order's), not an order with a sale of its own (the sale is the fee of the chain,
    order_report). The tyres sold from «На ОТХ» by another order (taken from the order or its record, the sale not reversed) and the
    sale of the record itself are that sale's: the arrival pays the rest only. The point is the arrival's. [{order, point_id, day, pieces}]."""
    return [dict(found, order=order_id) for order_id, found, ended in _chains(conn) if ended and start <= found["day"] <= end]


def waiting(conn) -> List[dict]:
    """The chains with no end yet (the owner 2026-11-26: «к начислению», shown apart, never in the sums): the tyres came (ARRIVAL_FROM
    on) and are still at the point — on «На точке», on «На ОТХ», agreeing the return, in the record of storage of the cancelled order,
    taken by an order not sold yet. The tyres a sale took already are that sale's. [{order, point_id, arrived (its day), pieces, record}]."""
    return [dict(found, order=order_id) for order_id, found, ended in _chains(conn) if not ended]


def _chains(conn) -> List[tuple]:
    """(order, {point_id, day, pieces (not sold further down the chain), arrived, record}, ended) of every arrival that pays, with no
    sale of its own. The chain is followed to its end (the review 2026-11-26): the record of storage of a cancelled order, the orders that
    took tyres from «На ОТХ» (of the order or a record), their own records, and so on. It ends when nothing of it waits any more: no
    holder on «На точке» / «На ОТХ» / agreeing the return, no taker in work. The tyres a sale took are that sale's; the day is the last
    end of the chain (the return written by `moved`, the last sale)."""
    paid = _paid(conn)
    if not paid:
        return []
    stages = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT c.id, g.code, g.kind FROM order_card c JOIN order_stage g ON g.id = c.stage_id")}
    sold: Dict[int, tuple] = {}                                           # order → (tyres, the day of its first sale)
    for note, pieces, day in conn.execute("SELECT note, SUM(pieces), MIN(day) FROM sale WHERE source = 'order' AND reversed_at IS NULL "
                                          "AND note LIKE 'Заказ №%' GROUP BY note"):
        digits = str(note)[len("Заказ №"):]
        if digits.isdigit():
            sold[int(digits)] = (int(pieces), str(day))
    takers: Dict[int, List[tuple]] = {}
    for order_id, source, qty in conn.execute("SELECT order_id, from_order, SUM(qty) FROM order_item_oth WHERE undone_at = '' "
                                              "GROUP BY order_id, from_order"):
        takers.setdefault(source, []).append((order_id, int(qty)))
    from .oth_storage import OF, record_ids
    records = record_ids(conn)
    record_of: Dict[int, int] = {}
    for key, value in conn.execute("SELECT key, value FROM app_state WHERE key LIKE ?", (OF.replace("%d", "%"),)):
        digits = key.rsplit(":", 1)[1]
        if digits.isdigit() and str(value).strip().isdigit():
            record_of[int(digits)] = int(str(value).strip())

    source_of = {x: a for a, x in record_of.items()}
    parents: Dict[int, List[tuple]] = {}                                  # holder → [(where its tyres came from, how many; None — all)]
    for x, a in source_of.items():
        parents.setdefault(x, []).append((a, None))
    for source, found in takers.items():
        for order_id, qty in found:
            parents.setdefault(order_id, []).append((source, qty))

    def nexts(holder: int) -> List[tuple]:
        kind = stages.get(holder, ("", ""))[1]
        return ([(record_of[holder], None)] if kind == "lost" and holder in record_of else []) + takers.get(holder, [])

    below: Dict[int, int] = {}
    shares: Dict[int, Dict[int, int]] = {}

    def sold_below(holder: int, path: frozenset) -> int:
        """The tyres a sale took from what `holder` held (further down the chain included), each sold tyre counted once."""
        if holder in below:
            return below[holder]
        got = sold[holder][0] if holder in records and holder in sold else 0  # the record sold itself («Выполнено» by the office)
        for nxt, qty in nexts(holder):
            if nxt in path:
                continue
            if qty is not None and nxt in sold:                           # the taker sold: those tyres are its sale's
                got += qty
            else:
                got += share(nxt, path | {holder}).get(holder, 0)
        below[holder] = got
        return got

    def share(holder: int, path: frozenset) -> Dict[int, int]:
        """What of the tyres sold below `holder` goes back up to each holder it got tyres from: its own arrival's first, the rest up the
        chain in turn, never more than it got from each (the review 2026-11-26: a sold tyre is taken off one arrival only)."""
        if holder in shares:
            return shares[holder]
        rest = sold_below(holder, path) - (paid[holder]["pieces"] if holder in paid and holder not in records else 0)
        out: Dict[int, int] = {}
        for up, qty in parents.get(holder, []):
            if qty is None:                                               # a record and its order: what the order brought into it
                qty = (paid[up]["pieces"] if up in paid else 0) + sum(q for _, q in parents.get(up, []) if q is not None)
            give = max(min(rest, qty), 0)
            out[up], rest = out.get(up, 0) + give, rest - give
        shares[holder] = out
        return out

    def walk(holder: int, seen: set) -> tuple:
        """(something of what `holder` held still waits, the day of the last sale further on)."""
        seen.add(holder)
        code, kind = stages.get(holder, ("", ""))
        waits, last = kind == "open" and code not in GONE_BACK, ""
        if holder in records and holder in sold:
            last = sold[holder][1]
        for nxt, qty in nexts(holder):
            if nxt in seen:
                continue
            if qty is not None and nxt in sold:
                last = max(last, sold[nxt][1])
                continue
            wait, day = walk(nxt, seen)
            waits, last = waits or wait, max(last, day)
        return waits, last

    out = []
    for order_id, found in sorted(paid.items()):
        if order_id not in stages or order_id in sold:                    # a sale of its own: the sale is the fee of the chain
            continue
        got = sold_below(order_id, frozenset())
        waits, last = walk(order_id, set())
        pieces = found["pieces"] - min(got, found["pieces"])
        ended = not waits
        day = max(found["day"] if found["left"] else "", last[:10])
        if pieces > 0 and (not ended or found["left"]):
            out.append((order_id, dict(found, pieces=pieces, record=record_of.get(order_id), day=day or found["day"]), ended))
    return out


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
