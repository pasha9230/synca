"""«Взять с ОТХ» (the owner 2026-10-29): the tyres of an order standing in «На ОТХ» (its client refused, they lie at the point) are sold to
a new order at the same point.

The card of a new order shows, under a position of the supplier, «На <точка> есть N шт (ОТХ, заказ №A)» and the button: the position gives
up to its pieces to a position «с ОТХ» of the same tyre (the purchase of the old order, kept from WebMIM: purchase_manual), the quantity of
the old order's position is lowered by them, and the old order emptied goes to «Отмена» by itself («Шины переданы в заказ №B», no rule of the
stage: its client is not written to). Only what lacks stays the supplier's: «Оформить в WebMIM», the check against WebMIM and the purchase
from WebMIM leave the positions «с ОТХ» alone; their sale is counted at the old order's purchase; the old order never has a sale, so the
revenue is counted once. The tyres must lie at the order's point: another point is never offered. The new order must not be in WebMIM yet.
«Вернуть на ОТХ» gives them back (the old order closed by the take stands in «На ОТХ» again) while the old order can take them; else the
tyres stay in the order, and a refusal sends it to «На ОТХ» with them: its positions «с ОТХ» are stock of OTH there (the purchase of the
first order), the ad holds them and a third order may take them (a chain). An order whose tyres all come from «На ОТХ» goes straight to
«Товар на точке» (at_point). «Свой остаток (ОТХ)» entered by hand (oth_manual) is offered and taken the same way, after the orders' tyres:
its row loses the pieces (from_item is minus the row's id, from_order 0), «Вернуть на ОТХ» gives them back to it, the purchase is the one of
the order's own position. The rows: order_item_oth (store.py).
The numbers (the owner 2026-11-04, the order 515): an order whose tyres all come from one order of purchase has that order's WebMIM number and
transfer number (its client's messages name them), no order of WebMIM of its own: _sync_numbers, borrowed_ids. «Дата счета» (the owner 2026-11-05) is
never stored: order_sheet counts it from the order of purchase, with the two numbers, by position."""
from __future__ import annotations

import json
import logging
from typing import Dict, Iterable, List, Optional, Set, Tuple

from . import orders, oth_stock

TAKEN_REASON = "другое: Шины переданы в заказ №%d"          # a reason of «Отмена / невыкуп» as the window writes «другое» (orders.cancel_reason_of)
NO_BACK = "Шины останутся в этом заказе; если клиент откажется — переведите заказ в «На ОТХ»."
_LIVE = "undone_at = ''"


class OthSaleError(orders.OrderError):
    pass


def _audit(conn, now: str, author: str, action: str, detail: dict) -> None:
    conn.execute("INSERT INTO audit (at, author, action, detail) VALUES (?,?,?,?)", (now, author, action, json.dumps(detail, ensure_ascii=False)))


def taken(conn, card_id) -> Dict[int, dict]:
    """The positions «с ОТХ» of an order: item id → {from_item, from_order, qty}."""
    return {r[0]: {"from_item": r[1], "from_order": r[2], "qty": r[3]} for r in conn.execute(
        "SELECT item_id, from_item, from_order, qty FROM order_item_oth WHERE order_id = ? AND " + _LIVE, (card_id,))}


def taken_ids(conn, card_id) -> Set[int]:
    return set(taken(conn, card_id))


def supplier_items(conn, card: dict) -> List[dict]:
    """The positions of the order that are the supplier's (not taken from «На ОТХ», not given by a placed source of a merge, order_merge):
    what WebMIM is asked for and checked against."""
    from .order_merge import placed_ids
    mine = taken_ids(conn, card["id"]) | placed_ids(conn, card["id"])
    return [i for i in card.get("items") or [] if i["id"] not in mine]


def sale_ids(conn) -> Set[int]:
    """The sales of the positions «с ОТХ»: counted at the purchase written (the old order's), never at WebMIM's price of the new order."""
    return {r[0] for r in conn.execute("SELECT sale_id FROM order_item_oth WHERE sale_id IS NOT NULL")}


def given(conn, card_id) -> Set[int]:
    """The positions of an old order that gave tyres to a new one (ever): their quantity in the Google table follows the order."""
    return {r[0] for r in conn.execute("SELECT DISTINCT from_item FROM order_item_oth WHERE from_order = ?", (card_id,))}


def _chain_end(conn, item_id) -> Tuple[Optional[int], Optional[int]]:
    """The last link of a chain (A → B → C) of a position «с ОТХ»: (the position of the first order, that order)."""
    end, found, seen = None, None, set()
    row = conn.execute("SELECT from_item, from_order FROM order_item_oth WHERE item_id = ?", (item_id,)).fetchone()
    while row is not None and row[0] not in seen:
        seen.add(row[0])
        end, found = row
        row = conn.execute("SELECT from_item, from_order FROM order_item_oth WHERE item_id = ?", (row[0],)).fetchone()
    return end, found


def first_order(conn, item_id) -> Optional[int]:
    """The order the tyres of a position «с ОТХ» were bought for: along a chain (A → B → C) to its first order."""
    return _chain_end(conn, item_id)[1]


def purchase_order(conn, item_id) -> Optional[int]:
    """first_order, and past a merge: the tyres of a merged order that a placed source gave were bought by that source, its number and invoice are
    theirs (order_merge.items_of), the merged order only has the transfer of its base. What the row of the Google table shows."""
    end, found = _chain_end(conn, item_id)
    if found:
        from . import order_merge
        merged = order_merge.items_of(conn, found).get(end)
        if merged and merged["placed"]:
            return merged["from_order"]
    return found


def from_number(conn, card_id) -> Dict[int, str]:
    """Item id of a position «с ОТХ» → the WebMIM number of the order its tyres were bought by (the Google table; the first of a chain)."""
    out = {}
    for item_id in taken(conn, card_id):
        row = conn.execute("SELECT webmim_number, webmim_id FROM order_card WHERE id = ?", (purchase_order(conn, item_id),)).fetchone()
        out[item_id] = (row[0] or row[1] or "") if row else ""
    return out


def _firsts(conn, card_id) -> List[int]:
    """The orders of purchase of the tyres «с ОТХ» the order holds now (0: «Свой остаток (ОТХ)»)."""
    return sorted({first_order(conn, r[0]) or 0 for r in conn.execute(
        "SELECT t.item_id FROM order_item_oth t JOIN order_item i ON i.id = t.item_id WHERE t.order_id = ? AND t." + _LIVE + " AND i.qty >= 1",
        (card_id,)).fetchall()})


def borrowed_ids(conn, card_id=None) -> Set[int]:
    """The orders whose WebMIM number is the one of the order their tyres «с ОТХ» were bought by: not an order of WebMIM of their own, so it
    is never paired, reconciled or counted for them (the first order keeps its pair). `card_id` None: every order. No id of WebMIM is ever
    copied, and a number on an order that took nothing from OTH is never here."""
    sql = ("SELECT c.id, c.webmim_number, t.item_id FROM order_item_oth t JOIN order_card c ON c.id = t.order_id "
           "WHERE t." + _LIVE + " AND c.webmim_id = '' AND c.webmim_number != ''")
    found: Set[int] = set()
    for order_id, number, item_id in conn.execute(sql + (" AND c.id = ?" if card_id is not None else ""),
                                                  (card_id,) if card_id is not None else ()).fetchall():
        if order_id not in found:
            row = conn.execute("SELECT webmim_number FROM order_card WHERE id = ?", (first_order(conn, item_id),)).fetchone()
            if (row and row[0] == number) or _copied(conn, order_id).get("webmim_number") == number:     # a copy left behind on a closed order too
                found.add(order_id)
    return found


def given_away(conn, card_ids) -> Set[int]:
    """Those of the orders closed by a take: their tyres are in another order, and a number they share with it means that one."""
    ids = [int(c) for c in card_ids]
    return {r[0] for r in conn.execute(
        "SELECT c.id FROM order_card c JOIN order_stage s ON s.id = c.stage_id WHERE s.code = 'cancel' AND c.cancel_reason LIKE ? AND c.id IN (%s)"
        % ",".join("?" * len(ids)), [TAKEN_REASON.split("%")[0] + "%"] + ids)} if ids else set()


NUMBERS = ("webmim_number", "transfer_number")
_NUMBER_NAMES = ("Номер WebMIM", "Номер перемещения")


def _number_names(fields: Iterable[str], one: str, both: str) -> str:
    """«Номер WebMIM и перемещение <both>» or the one number «<one>»."""
    fields = list(fields)
    return "Номер WebMIM и перемещение " + both if len(fields) == 2 else "%s %s" % (_NUMBER_NAMES[NUMBERS.index(fields[0])], one)


def _numbers_source(conn, card: dict, firsts: List[int]) -> Tuple[Optional[int], str]:
    """The order whose numbers this one has: (its id, ""); (None, why not, "" when there is nothing to say). Never a guess: tyres of several
    orders, of the point's own stock too, or positions of the supplier (their own order of WebMIM is coming) leave the numbers to a person.
    A deal of the point's stock and the point's own sale have no WebMIM (a number would take the sale from the point: order_points.own_sales)."""
    from . import order_points
    if not firsts or firsts == [0] or card.get("deal_kind") == "stock" or (card["point_id"] and card["id"] in order_points.own_sales(conn, card["point_id"])):
        return None, ""
    if len(firsts) > 1:
        return None, "шины из разных источников (%s)" % ", ".join("заказ №%d" % n if n else "свой остаток" for n in firsts)
    if [i for i in supplier_items(conn, card) if int(i["qty"]) >= 1]:
        return None, "в заказе есть позиции поставщика"
    return firsts[0], ""


def _copied(conn, card_id) -> Dict[str, str]:
    """Field → the value the system copied into it and nobody wrote over since (the journal of actions, oldest first: a value equal to the
    source's proves nothing, a person may have typed it). No journal line: the field is a person's, so it is never taken away."""
    found: Dict[str, str] = {}
    for (detail,) in conn.execute("SELECT detail FROM audit WHERE action = 'oth_numbers' AND json_extract(detail, '$.order') = ? ORDER BY id", (int(card_id),)).fetchall():
        data = json.loads(detail)
        found.update({k: "" if data.get("typed") else v for k, v in (data.get("changes") or {}).items()})
    return found


def typed_numbers(conn, card: dict) -> Set[str]:
    """The two numbers a person wrote on an order holding tyres «с ОТХ»: its rows of the table keep them (order_sheet). A number of an order with an
    order of WebMIM of its own, with positions of the supplier (it is theirs), or of a merged order (by position, order_merge) says nothing of these tyres."""
    from . import order_merge
    if card["webmim_id"] or order_merge.is_target(conn, card["id"]) or [i for i in supplier_items(conn, card) if int(i["qty"]) >= 1]:
        return set()
    copied = _copied(conn, card["id"])
    return {f for f in NUMBERS if card[f] and copied.get(f) != card[f]}


def _sync_numbers(conn, card_id, before: Iterable[int], now: str, author: str, note: bool = False, closed: bool = False) -> bool:
    """Bring the two numbers in line with the tyres «с ОТХ» the order holds now, in the caller's transaction. An empty field gets the number of
    the one order of purchase; a copy (_copied) follows that order, and goes when no order gives the numbers; anything else was written by a
    person and stays. `note`: say why nothing was taken. `closed`: a closed order, or one that gave all its tyres on (they still came from `before`):
    only a copy that has a new value changes. True: a field changed."""
    row = conn.execute("SELECT webmim_id, webmim_number, transfer_number FROM order_card WHERE id = ?", (card_id,)).fetchone()
    if row is None or row[0]:
        return False
    from . import order_merge
    if order_merge.is_target(conn, card_id):                            # a merged order has its sources' numbers, by position (order_merge)
        return False
    firsts = _firsts(conn, card_id)
    source, why = _numbers_source(conn, orders.get_card(conn, card_id), firsts) if firsts or not closed else (next(iter(before)), "")
    numbers = {}
    for order_id in (set(before) | set(firsts)) - {0}:
        got = conn.execute("SELECT webmim_number, transfer_number FROM order_card WHERE id = ?", (order_id,)).fetchone()
        numbers[order_id] = tuple(got) if got else ("", "")
    wanted = numbers.get(source, ("", ""))
    taken_now, gone = {}, {}
    copied = _copied(conn, card_id)
    for at, field in enumerate(NUMBERS):
        value = row[at + 1]
        if value and value != wanted[at] and copied.get(field) == value and (wanted[at] or not closed):
            gone[field], value = "", ""
        if not value and wanted[at] and (field in gone or not closed):
            taken_now[field] = wanted[at]
    changes = {**gone, **taken_now}
    if changes:
        conn.execute("UPDATE order_card SET %s, updated_at = ? WHERE id = ?" % ", ".join("%s = ?" % f for f in changes),
                     list(changes.values()) + [now, card_id])
        _audit(conn, now, author, "oth_numbers", {"order": int(card_id), "from_order": source, "changes": changes})
    left = [f for f in gone if f not in taken_now]
    if left:
        why_gone = why or ("в заказе №%d %s больше нет" % (source, "их" if len(left) == 2 else "его") if source
                           else "заказ больше не держит шины того заказа, или в нём другие позиции")
        orders.add_event(conn, card_id, now, author, "field", "%s: %s." % (_number_names(left, "убран", "убраны"), why_gone))
    if taken_now:
        orders.add_event(conn, card_id, now, author, "field", "%s из заказа №%d" % (_number_names(taken_now, "взят", "взяты"), source))
    elif note and why and not (row[1] and row[2]) and any(any(n) for n in numbers.values()):
        orders.add_event(conn, card_id, now, author, "field", "%s: %s.%s" % (_number_names(
            [f for at, f in enumerate(NUMBERS) if not row[at + 1]], "не взят", "не взяты"), why,
            " Впишите вручную." if len(firsts) > 1 else ""))                   # the supplier's position: its own order brings the number, or the next take
    return bool(changes)


def fill_numbers(conn, card_id, now: str, author: str, write: bool = True) -> dict:
    """`oth-numbers-fill` (the owner 2026-11-05): an order that took its tyres before the numbers were copied gets them now, by _sync_numbers and only
    where it would act today; a person's value stays, nobody is written to. `write` False: the same work, rolled back. {order, before, after, source, why}:
    `why` says why nothing changes. Commits or rolls back its own transaction."""
    done = {"order": int(card_id), "before": ("", ""), "after": ("", ""), "source": None, "why": ""}
    if conn.execute("SELECT 1 FROM order_card WHERE id = ?", (card_id,)).fetchone() is None:
        return dict(done, why="заказа нет (удалён или неверный номер)")
    card = orders.get_card(conn, card_id)
    done["before"] = done["after"] = (card["webmim_number"], card["transfer_number"])
    firsts = _firsts(conn, card_id)
    from . import order_merge
    state = conn.execute("SELECT state FROM webmim_create WHERE order_id = ?", (card_id,)).fetchone()
    source, why = _numbers_source(conn, card, firsts)
    if card["webmim_id"]:
        why = "у заказа свой заказ WebMIM"
    elif order_merge.is_target(conn, card_id):
        why = "заказ объединённый: номера у него по позициям"
    elif not firsts:
        why = "заказ не держит шин с ОТХ"
    elif card["stage"]["kind"] != "open":
        why = "заказ закрыт (этап «%s»): номера закрытому заказу не вписываются" % card["stage"]["name"]
    elif state and state[0] != "refused":
        why = "заказ оформляется в WebMIM: номер придёт оттуда"
    elif source is None:
        why = why or "сделка из наличия, своя продажа точки или свой остаток: номера не нужны"
    if why:
        return dict(done, why=why)
    done["source"] = source
    try:
        changed = _sync_numbers(conn, card_id, firsts, now, author)
        got = conn.execute("SELECT webmim_number, transfer_number FROM order_card WHERE id = ?", (card_id,)).fetchone()
        done["after"] = (got[0], got[1])
    except Exception:
        conn.rollback()
        raise
    if not changed:
        conn.rollback()
        return dict(done, why="менять нечего: поля заполнены или у заказа №%d нет этих номеров" % source)
    if write:
        conn.commit()
    else:
        conn.rollback()
    return done


def numbers_written(conn, card_id, fields: Iterable[str], now: str, author: str) -> None:
    """A number was written through the order's fields (orders.update_card, in its transaction). Over a copy: it is a person's from now on.
    The orders holding tyres bought by this one follow it (the owner 2026-11-04): they show its number now, unless a person typed their own.
    An id of WebMIM alone changes no copy, but the rows of those orders: the number or the day they show may come by it."""
    fields = list(fields)
    if "webmim_id" in fields:
        _look_again(conn, card_id)
    fields = [f for f in fields if f in NUMBERS]
    if not fields:
        return
    copied = _copied(conn, card_id)
    typed = {f: orders._row(conn, card_id)[f] for f in fields if copied.get(f)}
    if typed:
        _audit(conn, now, author, "oth_numbers", {"order": int(card_id), "typed": True, "changes": typed})
    _follow_source(conn, card_id, now, author, {int(card_id)})


def _holders(conn, source_id) -> Set[int]:
    """The orders whose tyres «с ОТХ» were bought by `source_id`."""
    held = conn.execute("SELECT t.order_id, t.item_id FROM order_item_oth t WHERE t." + _LIVE).fetchall()     # a position given on too: its copy means the same order
    return {o for o, item_id in held if first_order(conn, item_id) == source_id}


def _look_again(conn, source_id) -> None:
    """The rows of the orders holding this order's tyres show its values: the next send of the Google table looks at them though nothing of their own
    changed (order_sheet._due: no record in their journal when nothing is copied, several sources, a press going on)."""
    held = conn.execute("SELECT t.order_id, t.item_id FROM order_item_oth t WHERE t." + _LIVE).fetchall()
    for order_id in {o for o, item_id in held if source_id in (first_order(conn, item_id), purchase_order(conn, item_id))}:
        conn.execute("UPDATE sheet_order SET seen_event = 0 WHERE order_id = ?", (order_id,))


def _follow_source(conn, source_id, now: str, author: str, seen: Set[int]) -> None:
    """The orders whose tyres «с ОТХ» were bought by `source_id`, and on along a copy that changed. A press of «Оформить в WebMIM» going on writes
    its own (items_changed)."""
    _look_again(conn, source_id)
    for order_id in sorted(_holders(conn, source_id) - seen):
        seen.add(order_id)
        state = conn.execute("SELECT state FROM webmim_create WHERE order_id = ?", (order_id,)).fetchone()
        if state and state[0] != "refused":
            continue
        if _sync_numbers(conn, order_id, [source_id], now, author, closed=orders.get_card(conn, order_id)["stage"]["kind"] != "open" or not _firsts(conn, order_id)):
            _follow_source(conn, order_id, now, author, seen)


def items_changed(conn, card_id, now: str, author: str) -> None:
    """A position of the supplier was added or removed by a person (orders, in their transaction): at any stage of work the numbers of the tyres
    «с ОТХ» follow, so «Оформить в WebMIM» is there when the supplier has to be asked (the copy would hide it and nothing would say the tyres
    are not ordered). A closed order stays as it is, a press of «Оформить в WebMIM» going on writes its own (_write_order would not write over a
    copy), a number written by a person stays."""
    firsts = _firsts(conn, card_id)
    if not firsts:
        return
    state = conn.execute("SELECT state FROM webmim_create WHERE order_id = ?", (card_id,)).fetchone()
    if orders.get_card(conn, card_id)["stage"]["kind"] == "open" and not (state and state[0] != "refused"):
        _sync_numbers(conn, card_id, firsts, now, author)


IN_STOCK = "Товар в наличии на точке"     # in place of the date of delivery of an order whose tyres all lie at the point (the owner 2026-10-30)
_IN_STOCK_SQL = ("SELECT i.order_id FROM order_item i LEFT JOIN order_item_oth t ON t.item_id = i.id AND t." + _LIVE + " WHERE i.qty >= 1 %s "
                 "GROUP BY i.order_id HAVING COUNT(t.item_id) = COUNT(*)")


def in_stock_ids(conn, card_ids=None) -> Set[int]:
    """The orders whose every tyre is taken from «На ОТХ» (at least one): the tyres lie at the point, so no date of delivery is needed nor
    made up — IN_STOCK is shown where it would be. `card_ids` None: every order."""
    if card_ids is None:
        return {r[0] for r in conn.execute(_IN_STOCK_SQL % "")}
    ids = [int(c) for c in card_ids]
    return {r[0] for r in conn.execute(_IN_STOCK_SQL % ("AND i.order_id IN (%s)" % ",".join("?" * len(ids))), ids)} if ids else set()


def holds(conn, card_id) -> List[int]:
    """The old orders whose tyres the order holds now (positions «с ОТХ» with pieces): the order may not be cancelled or deleted with them."""
    return sorted({r[0] for r in conn.execute(
        "SELECT t.from_order FROM order_item_oth t JOIN order_item i ON i.id = t.item_id WHERE t.order_id = ? AND t." + _LIVE + " AND i.qty >= 1",
        (card_id,))})


def holds_text(found: List[int], can_return: bool = True) -> str:
    """`can_return`: the order may still give them back (why_not: not placed yet); else «На ОТХ» is the only way (the point never has the button)."""
    named = (["заказ " + ", ".join("№%d" % n for n in found if n)] if any(found) else []) + (["свой остаток"] if 0 in found else [])
    return ("В заказе шины с ОТХ (%s): переведите заказ на «На ОТХ»%s." % (
        ", ".join(named), " или верните их («Вернуть на ОТХ» у позиций)" if can_return else ""))


def gives(conn, card_id) -> List[int]:
    """The orders holding tyres this order gave from «На ОТХ» (positions «с ОТХ» with pieces): the order may not be deleted then —
    «Вернуть на ОТХ» would have nowhere to put them."""
    return sorted({r[0] for r in conn.execute(
        "SELECT t.order_id FROM order_item_oth t JOIN order_item i ON i.id = t.item_id WHERE t.from_order = ? AND t." + _LIVE + " AND i.qty >= 1",
        (card_id,))})


def why_not_delete(conn, card_id) -> Optional[str]:
    """Why the order may not be deleted: it holds tyres taken from «На ОТХ», or gave some still held."""
    held, gave = holds(conn, card_id), gives(conn, card_id)
    if held:
        return holds_text(held)
    if gave:
        return ("Шины этого заказа взяты с ОТХ в заказ %s: удалить его нельзя (им некуда будет вернуться). Переведите заказ в «Отмена»."
                % ", ".join("№%d" % n for n in gave))
    return None


def why_not(conn, card: dict) -> Optional[str]:
    """Why the order can take nothing from «На ОТХ» (or give it back) now; None: it can."""
    stage = card["stage"]
    if stage["kind"] != "open" or stage["code"] in orders.OTH_CODES or orders._needs_placed(stage, orders._placed_stage(conn)):
        return "Брать с ОТХ можно только до «Заказ оформлен»: заказ уже на этапе «%s»." % stage["name"]
    if card["sale_ids"]:
        return "По заказу записана продажа."
    if card["webmim_id"] or (card["webmim_number"] and card["id"] not in borrowed_ids(conn, card["id"])):     # the number of its tyres: not its own
        return "Заказ уже есть в WebMIM: шины с ОТХ к нему не добавить."
    placed = conn.execute("SELECT supplier_number FROM order_card WHERE id = ?", (card["id"],)).fetchone()
    if placed and str(placed[0] or "").strip():                        # «Оформил вручную» of 4 Точки (order_supplier), the move may have failed
        return "Заказ уже оформлен в 4 Точках: шины с ОТХ к нему не добавить."
    state = conn.execute("SELECT state FROM webmim_create WHERE order_id = ?", (card["id"],)).fetchone()
    if state and state[0] != "refused":
        return "Заказ уже оформляется в WebMIM: шины с ОТХ к нему не добавить."
    return None


def _point_name(row) -> str:
    return (row["point"] or "").strip() or "точке №%s" % row["point_id"]


def offers(conn, card: dict) -> Dict[int, List[dict]]:
    """Position of the supplier → the tyres of it lying on «На ОТХ» (oth_stock: no test point, no agreed return), other orders only:
    [{from_item, from_order, point_id, point, qty, take, why}]. `take`: the pieces the button takes; `why`: why it cannot (another point,
    the order already in WebMIM) — shown, the button is not. A position of a placed source of a merge (order_merge) is ordered already: none."""
    from .order_merge import placed_ids
    mine = taken_ids(conn, card["id"]) | placed_ids(conn, card["id"])
    lines = [i for i in card["items"] if i["id"] not in mine and i["goods_id"] != orders.POINT_GOODS and int(i["qty"]) >= 1]
    if not lines:
        return {}
    stock: Dict[str, List[dict]] = {}
    for r in oth_stock._rows(conn):
        if r["order_id"] != card["id"]:
            stock.setdefault(str(r["goods_id"]), []).append(r)
    blocked = why_not(conn, card)
    from . import oth_storage
    records = oth_storage.record_ids(conn)
    out: Dict[int, List[dict]] = {}
    for line in lines:
        for r in stock.get(str(line["goods_id"]), ()):
            why = blocked
            if why is None and card["point_id"] and card["point_id"] != r["point_id"]:
                why = "Шины лежат на «%s», а заказ на «%s»: взять нельзя." % (_point_name(r), card["point"]["name"] if card["point"] else card["point_id"])
            if why is None and r["order_id"] in records:                 # a record of storage: whole or not at all
                why = oth_storage.why_not_whole(conn, card, r["order_id"])
            out.setdefault(line["id"], []).append({"from_item": r["item_id"], "from_order": r["order_id"], "point_id": r["point_id"],
                                                   "point": _point_name(r), "qty": int(r["qty"]), "take": min(int(r["qty"]), int(line["qty"])),
                                                   "why": why})
    return out


def find_hint(conn, goods_ids) -> Dict[str, str]:
    """goods_id → «на ОТХ: Бета 2 шт, …»: the mark of the list where a position is chosen."""
    found: Dict[str, Dict[str, int]] = {}
    wanted = {str(g) for g in goods_ids}
    for r in oth_stock._rows(conn):
        if str(r["goods_id"]) in wanted:
            places = found.setdefault(str(r["goods_id"]), {})
            places[_point_name(r)] = places.get(_point_name(r), 0) + int(r["qty"])
    return {g: "на ОТХ: " + ", ".join("%s %d шт" % (p, n) for p, n in sorted(places.items())) for g, places in found.items()}


def _stage(conn, code: str) -> dict:
    found = next((s for s in orders.list_stages(conn) if s["code"] == code), None)
    if found is None:
        raise OthSaleError("В воронке нет этапа «%s»: взять и вернуть шины с ОТХ нельзя." % {"cancel": "Отмена", "oth": "На ОТХ"}[code])
    return found


def take(conn, card_id, line_id, from_item, now: str, author: str) -> int:
    """«Взять с ОТХ»: the supplier's position `line_id` of the order gives up to its pieces to tyres of the position `from_item` lying on «На ОТХ».
    One transaction (the old order emptied is moved to «Отмена» in it). Returns the pieces taken; OthSaleError says why nothing was.
    Tyres of a record of storage (oth_storage, the owner 2026-11-10) are taken whole only: every position of the record, or nothing."""
    from . import oth_storage
    owner = conn.execute("SELECT order_id FROM order_item WHERE id = ?", (int(from_item),)).fetchone()     # a manual row (below zero): none
    if owner is not None and oth_storage.is_record(conn, owner[0]):
        return oth_storage.take(conn, card_id, from_item, now, author)
    return _take_order(conn, card_id, line_id, from_item, now, author)


def _take_order(conn, card_id, line_id, from_item, now: str, author: str, commit: bool = True) -> int:
    """take() of the tyres of one position. `commit` False: in the caller's transaction (oth_storage.take: every position of a record)."""
    card = orders.get_card(conn, card_id)
    why = why_not(conn, card)
    if why:
        raise OthSaleError(why)
    line = next((i for i in card["items"] if i["id"] == int(line_id)), None)
    from .order_merge import placed_ids
    if line is None or line["id"] in taken_ids(conn, card_id) or line["id"] in placed_ids(conn, card_id):
        raise OthSaleError("Такой позиции поставщика в заказе нет: обновите страницу.")
    source = next((r for r in oth_stock._rows(conn) if r["item_id"] == int(from_item)), None)
    if source is None:
        raise OthSaleError("Этих шин на ОТХ уже нет: их взяли, вернули поставщику или заказ ушёл с ОТХ. Обновите страницу.")
    if source["order_id"] == card_id or str(source["goods_id"]) != str(line["goods_id"]):
        raise OthSaleError("Это не те шины: обновите страницу.")
    if card["point_id"] and card["point_id"] != source["point_id"]:
        raise OthSaleError("Шины лежат на «%s», а заказ на другой точке: взять нельзя. Другую точку не предлагаем." % _point_name(source))
    pieces = min(int(source["qty"]), int(line["qty"]))
    before = _firsts(conn, card_id)
    if source["manual"]:
        return _take_manual(conn, card, line, source, pieces, before, now, author)
    old = orders.get_card(conn, source["order_id"])
    item = next(i for i in old["items"] if i["id"] == source["item_id"])
    cancel = _stage(conn, "cancel")
    if not card["point_id"]:                                        # the order's point is the one the tyres lie at (its time, its checks)
        try:
            orders.update_card(conn, card_id, {"point_id": source["point_id"]}, now, author, commit=False)
        except orders.OrderError:
            conn.rollback()
            raise
    from . import order_oth                                         # the pieces of a mark «На возврат» are on sale until agreed (the owner
    free = int(item["qty"]) - int((conn.execute("SELECT qty FROM oth_return WHERE item_id = ?", (item["id"],)).fetchone() or (0,))[0])     # 2026-11-10)
    marked = pieces - free > 0 and order_oth.drop_marks(conn, old["id"], now, author, item_id=item["id"], pieces=pieces - free,
                                                        slot=order_oth.take_slot(card_id, item["id"]))     # «Вернуть на ОТХ» brings them back
    # the limit in the statement itself: a take a moment ago in another tab never lets the pieces go below the ones marked returned
    if conn.execute("UPDATE order_item SET qty = qty - ? WHERE id = ? AND qty - ? >= COALESCE((SELECT qty FROM oth_return WHERE item_id = ?), 0) "
                    "AND order_id IN (SELECT c.id FROM order_card c JOIN order_stage s ON s.id = c.stage_id WHERE s.code = 'oth') "
                    "AND order_id NOT IN (SELECT order_id FROM oth_return_plan)",
                    (pieces, item["id"], pieces, item["id"])).rowcount != 1:
        conn.rollback()
        raise OthSaleError("Столько шин на ОТХ уже нет: их только что взяли в другой вкладке. Обновите страницу.")
    # the supplier's position as it was read: a second press a moment ago (another worker) changed it — nothing more is taken
    if pieces >= int(line["qty"]):
        gone = conn.execute("DELETE FROM order_item WHERE id = ? AND order_id = ? AND qty = ?", (line["id"], card_id, line["qty"])).rowcount
    else:
        gone = conn.execute("UPDATE order_item SET qty = qty - ? WHERE id = ? AND order_id = ? AND qty = ?",
                            (pieces, line["id"], card_id, line["qty"])).rowcount
    if gone != 1:
        conn.rollback()
        raise OthSaleError("Позицию только что изменили (второе нажатие или другая вкладка): ничего не взято. Обновите страницу.")
    cur = conn.execute(
        "INSERT INTO order_item (order_id, goods_id, code, name, brand_key, season, diameter, territory, qty, supplier_price, sale_unit, terms, "
        "fee_pct, promo_pct, purchase_manual) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
        (card_id, line["goods_id"], line["code"] or item["code"], line["name"], line["brand_key"], line["season"], line["diameter"], line["territory"],
         pieces, float(item["supplier_price"]), float(line["sale_unit"]), json.dumps(item["terms"] or {}, ensure_ascii=False), float(line["fee_pct"]),
         float(line["promo_pct"])))
    conn.execute("INSERT INTO order_item_oth (item_id, order_id, from_item, from_order, qty, at, author) VALUES (?,?,?,?,?,?,?)",
                 (cur.lastrowid, card_id, item["id"], old["id"], pieces, now, author))
    orders.add_event(conn, card_id, now, author, "item", "Взято с ОТХ (заказ №%d, %s): %s × %d" % (old["id"], _point_name(source), line["name"], pieces))
    orders.add_event(conn, old["id"], now, author, "item", "Передано с ОТХ в заказ №%d: %s × %d" % (card_id, item["name"], pieces))
    _audit(conn, now, author, "oth_take", {"order": int(card_id), "item": cur.lastrowid, "from_order": old["id"], "from_item": item["id"], "qty": pieces})
    _sync_numbers(conn, card_id, before, now, author, note=True)
    left = conn.execute("SELECT COALESCE(SUM(qty), 0) FROM order_item WHERE order_id = ?", (old["id"],)).fetchone()[0]
    if left > 0:
        if commit:                                                    # sold_early_tasks commits: never inside the caller's transaction (oth_storage.take
            conn.commit()                                             # takes only the stock, which has no pieces of a mark «На возврат»)
            if marked:
                order_oth.sold_early_tasks(conn, old["id"], now, author)
        return pieces
    try:
        moved = orders.move_card(conn, old["id"], cancel["id"], now, author, changes={"cancel_reason": TAKEN_REASON % card_id}, fire_rules=False,
                                 journal=False, commit=commit)
    except orders.OrderError as err:                                  # rolled back, the take with it
        raise OthSaleError("Шины не взяты: старый заказ №%d не закрылся (%s)." % (old["id"], err)) from None
    if not moved:                                                     # moved by somebody meanwhile: move_card rolled it all back
        raise OthSaleError("Шины не взяты: старый заказ №%d только что изменили. Обновите страницу." % old["id"])
    return pieces


def _take_manual(conn, card: dict, line: dict, source: dict, pieces: int, before: List[int], now: str, author: str) -> int:
    """take() from a row of «Свой остаток (ОТХ)»: the row loses the pieces (never below 0, in the statement), the order gets a position «с ОТХ»
    at the purchase of its own position (no old order). One transaction."""
    from . import oth_manual
    card_id = card["id"]
    if not card["point_id"]:
        try:
            orders.update_card(conn, card_id, {"point_id": source["point_id"]}, now, author, commit=False)
        except orders.OrderError:
            conn.rollback()
            raise
    if not oth_manual.take(conn, source["manual"], pieces, now, author, card_id):
        conn.rollback()
        raise OthSaleError("Столько шин на ОТХ уже нет: их только что взяли в другой вкладке. Обновите страницу.")
    if pieces >= int(line["qty"]):
        gone = conn.execute("DELETE FROM order_item WHERE id = ? AND order_id = ? AND qty = ?", (line["id"], card_id, line["qty"])).rowcount
    else:
        gone = conn.execute("UPDATE order_item SET qty = qty - ? WHERE id = ? AND order_id = ? AND qty = ?",
                            (pieces, line["id"], card_id, line["qty"])).rowcount
    if gone != 1:
        conn.rollback()
        raise OthSaleError("Позицию только что изменили (второе нажатие или другая вкладка): ничего не взято. Обновите страницу.")
    cur = conn.execute(
        "INSERT INTO order_item (order_id, goods_id, code, name, brand_key, season, diameter, territory, qty, supplier_price, sale_unit, terms, "
        "fee_pct, promo_pct, purchase_manual) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
        (card_id, line["goods_id"], line["code"], line["name"], line["brand_key"], line["season"], line["diameter"], line["territory"],
         pieces, float(line["supplier_price"]), float(line["sale_unit"]), json.dumps(line["terms"] or {}, ensure_ascii=False), float(line["fee_pct"]),
         float(line["promo_pct"])))
    conn.execute("INSERT INTO order_item_oth (item_id, order_id, from_item, from_order, qty, at, author) VALUES (?,?,?,0,?,?,?)",
                 (cur.lastrowid, card_id, -int(source["manual"]), pieces, now, author))
    orders.add_event(conn, card_id, now, author, "item", "Взято с ОТХ (свой остаток, %s): %s × %d" % (_point_name(source), line["name"], pieces))
    _audit(conn, now, author, "oth_take", {"order": int(card_id), "item": cur.lastrowid, "manual": int(source["manual"]), "qty": pieces})
    _sync_numbers(conn, card_id, before, now, author, note=True)
    conn.commit()
    return pieces


def to_new_client(conn, old_id, name, phone, now: str, author: str) -> int:
    """All the tyres given (oth_swap.why_not, the plan of 2026-11-06): the old order itself takes the new client and goes to «Товар на точке»
    (oth_swap.to_client), it is returned. Otherwise, back from «На ОТХ» to «Товар на точке» with a new client (the owner 2026-10-30, the order 444): a new order of the same point, with the
    tyres still ours on OTH of the old one at their sale price, takes them as «Взять с ОТХ» does (take: the first order's purchase, «с ОТХ»,
    the old order emptied goes to «Отмена» «Шины переданы в заказ №…»). The caller puts the new order on «Товар на точке» (all from OTH).
    Returns the new order; OthSaleError says why nothing was made."""
    old = orders.get_card(conn, old_id)
    if old["stage"]["code"] != "oth":
        raise OthSaleError("Новый клиент берёт шины заказа, который стоит на «На ОТХ»: заказ на этапе «%s»." % old["stage"]["name"])
    name, phone = " ".join(str(name or "").split())[:200], str(phone or "").strip()[:40]
    if not phone:
        raise OthSaleError("Укажите телефон нового клиента.")
    if not name:
        raise OthSaleError("Укажите имя нового клиента.")
    if conn.execute("SELECT 1 FROM oth_return_plan WHERE order_id = ?", (old_id,)).fetchone():
        raise OthSaleError("По заказу №%d согласован возврат поставщику (акт МХ-3): шины новому клиенту не передать." % old_id)
    if old["sale_ids"]:                                                 # an order with a sale never changes its client (the plan of 2026-11-06)
        raise OthSaleError("По заказу №%d записана продажа: клиента не сменить." % old_id)
    from . import oth_swap, oth_storage
    stored = oth_storage.is_record(conn, old_id)                        # a record of storage: a new order takes it whole (the owner 2026-11-10)
    if stored and oth_storage.returning(conn, old_id):                  # a tyre of it marked «На возврат»: the whole record goes back (2026-11-11)
        raise OthSaleError(oth_storage.RETURN_WHOLE)
    if not stored and oth_swap.why_not(conn, old) is None:              # every tyre goes: the order itself takes the client (oth_swap)
        oth_swap.to_client(conn, old_id, name, phone, now, author)
        return int(old_id)
    rows = [r for r in oth_stock._rows(conn) if r["order_id"] == old_id]
    if not rows:
        raise OthSaleError("На ОТХ по заказу №%d нет шин, которые можно передать (возвращены поставщику, шина точки или тестовая точка)." % old_id)
    items = {i["id"]: i for i in old["items"]}
    lines = [dict(goods_id=r["goods_id"], code=items[r["item_id"]]["code"], name=items[r["item_id"]]["name"], brand_key=items[r["item_id"]]["brand_key"],
                  season=items[r["item_id"]]["season"], diameter=items[r["item_id"]]["diameter"], territory=items[r["item_id"]]["territory"],
                  qty=int(r["qty"]), sale_unit=items[r["item_id"]]["sale_unit"], supplier_price=items[r["item_id"]]["supplier_price"],
                  terms=items[r["item_id"]]["terms"], fee_pct=items[r["item_id"]]["fee_pct"], promo_pct=items[r["item_id"]]["promo_pct"]) for r in rows]
    try:
        new = orders.create_card_with_items(conn, {"client_name": name, "client_phone": phone, "point_id": old["point_id"]}, lines, now, author)
    except orders.OpenOrderExists as err:
        raise OthSaleError("У клиента с телефоном %s уже есть открытый заказ №%d этой точки: откройте его." % (phone, err.order_id)) from None
    orders.add_event(conn, new, now, author, "field", "Заказ создан для нового клиента шин с ОТХ заказа №%d." % old_id)
    _audit(conn, now, author, "oth_new_client", {"order": new, "from_order": int(old_id)})
    conn.commit()
    made = [i["id"] for i in orders.get_card(conn, new)["items"]]          # the positions in the order of the rows (create_card_with_items)
    try:
        if len(made) != len(rows):                                      # never a position left out of the takes
            raise OthSaleError("позиции нового заказа не совпали с шинами на ОТХ")
        if stored:                                                      # every position in one transaction, or none
            if oth_storage.take(conn, new, rows[0]["item_id"], now, author) != sum(int(r["qty"]) for r in rows):
                raise OthSaleError("на ОТХ осталось меньше шин, чем было")
            return new
        for r, line_id in zip(rows, made):
            if take(conn, new, line_id, r["item_id"], now, author) != int(r["qty"]):     # some taken a moment ago in another tab
                raise OthSaleError("на ОТХ осталось меньше шин, чем было")
    except orders.OrderError as err:
        _undo_new(conn, new, now, author)
        raise OthSaleError("Шины новому клиенту не переданы (%s): обновите страницу и попробуйте снова." % err) from None
    return new


def _undo_new(conn, new: int, now: str, author: str) -> None:
    """A take of to_new_client failed: what it took goes back to OTH and the new order goes to «Удалённые» (it never was the client's). A
    failure of that is left as a red record of the new order."""
    from . import trash
    try:
        for item_id in list(taken(conn, new)):
            if item_id in taken(conn, new):                             # a record goes back whole with its first position
                untake(conn, new, item_id, now, author)
        trash.delete_order(conn, new, now, author)
    except Exception as err:                                            # noqa: BLE001
        conn.rollback()
        orders.add_event(conn, new, now, author, "field", "Шины с ОТХ взяты не полностью (%s): проверьте заказ, не оформляйте его в WebMIM." % err,
                         level="red")
        conn.commit()


def untake(conn, card_id, item_id, now: str, author: str) -> int:
    """«Вернуть на ОТХ»: the position «с ОТХ» leaves the order and its pieces go back to the old order's position; the old order closed by
    the take stands in «На ОТХ» again. Returns the pieces given back.
    Tyres of a record of storage go back whole (oth_storage.untake, the review 2026-11-10): every position taken from it, in one transaction."""
    link = taken(conn, card_id).get(int(item_id))
    if link is not None and link["from_order"]:
        from . import oth_storage
        if oth_storage.is_record(conn, link["from_order"]):
            return oth_storage.untake(conn, card_id, link["from_order"], now, author)
    return _untake_order(conn, card_id, item_id, now, author)


def _untake_order(conn, card_id, item_id, now: str, author: str, commit: bool = True) -> int:
    """untake() of one position. `commit` False: in the caller's transaction (oth_storage.untake: every position of a record)."""
    card = orders.get_card(conn, card_id)
    link = taken(conn, card_id).get(int(item_id))
    if link is None:
        raise OthSaleError("Эта позиция не с ОТХ.")
    why = why_not(conn, card)
    if why:
        raise OthSaleError(why.replace("Брать с ОТХ", "Вернуть на ОТХ"))
    line = next(i for i in card["items"] if i["id"] == int(item_id))
    before = _firsts(conn, card_id)
    if not link["from_order"]:
        return _untake_manual(conn, card, line, link, before, now, author)
    if conn.execute("SELECT 1 FROM oth_return_plan WHERE order_id = ?", (link["from_order"],)).fetchone():
        raise OthSaleError("По заказу №%d согласован возврат поставщику: шины в него не вернуть (акт МХ-3 разойдётся). " % link["from_order"] + NO_BACK)
    old = conn.execute("SELECT c.id, s.code, c.cancel_reason FROM order_item i JOIN order_card c ON c.id = i.order_id JOIN order_stage s ON s.id = c.stage_id "
                       "WHERE i.id = ? AND c.id = ?", (link["from_item"], link["from_order"])).fetchone()
    reopen = old is not None and old[1] == "cancel" and str(old[2]).startswith(TAKEN_REASON.split("%")[0])
    if old is None or (old[1] not in orders.OTH_CODES and not reopen):     # «На возврат» before the agreement: ours until the act, back in that order (never on sale)
        raise OthSaleError("Заказ №%d ушёл с ОТХ: вернуть шины в него нельзя. " % link["from_order"] + NO_BACK)
    pieces = int(line["qty"])
    conn.execute("UPDATE order_item SET qty = qty + ? WHERE id = ?", (pieces, link["from_item"]))
    conn.execute("DELETE FROM order_item WHERE id = ?", (line["id"],))
    if conn.execute("UPDATE order_item_oth SET undone_at = ? WHERE item_id = ? AND " + _LIVE, (now, line["id"])).rowcount != 1:
        conn.rollback()
        raise OthSaleError("Позицию только что вернули в другой вкладке. Обновите страницу.")
    orders.add_event(conn, card_id, now, author, "item", "Возвращено на ОТХ (заказ №%d): %s × %d" % (link["from_order"], line["name"], pieces))
    orders.add_event(conn, link["from_order"], now, author, "item", "Шины вернулись на ОТХ из заказа №%d: %s × %d" % (card_id, line["name"], pieces))
    _audit(conn, now, author, "oth_untake", {"order": int(card_id), "item": line["id"], "from_order": link["from_order"], "qty": pieces})
    _sync_numbers(conn, card_id, before, now, author)
    if not reopen:
        if commit:                                                    # _marks_back commits: never inside the caller's transaction (oth_storage.untake:
            conn.commit()                                             # a record's take dropped no mark «На возврат», its stock has none)
            _marks_back(conn, card_id, link, pieces, now, author)
        return pieces
    oth = _stage(conn, "oth")
    try:
        moved = orders.move_card(conn, link["from_order"], oth["id"], now, author, fire_rules=False, journal=False, oth_split=False, commit=commit)
    except orders.OrderError as err:
        raise OthSaleError("Шины не возвращены: заказ №%d не встал на ОТХ (%s)." % (link["from_order"], err)) from None
    if not moved:
        raise OthSaleError("Шины не возвращены: заказ №%d только что изменили. Обновите страницу." % link["from_order"])
    if commit:                                                        # as above: not in the caller's transaction
        _marks_back(conn, card_id, link, pieces, now, author)
    return pieces


def _marks_back(conn, card_id, link: dict, pieces: int, now: str, author: str) -> None:
    """After «Вернуть на ОТХ»: the marks «На возврат» the take took off are back with their task (the owner 2026-11-10). Never raises: the tyres are back."""
    from . import order_oth
    try:
        order_oth.restore_marks(conn, link["from_order"], now, author, order_oth.take_slot(card_id, link["from_item"]), "шины вернулись на ОТХ", pieces=pieces)
        conn.commit()
    except Exception:                                                 # noqa: BLE001
        conn.rollback()
        logging.getLogger("tires_sync.orders").exception("the marks «На возврат» of order %s were not brought back", link["from_order"])


def _untake_manual(conn, card: dict, line: dict, link: dict, before: List[int], now: str, author: str) -> int:
    """untake() of tyres taken from «Свой остаток (ОТХ)»: they lie at the order's point again, as a manual row of it."""
    from . import oth_manual
    pieces = int(line["qty"])
    point_id = oth_manual.point_of(conn, -int(link["from_item"])) or card["point_id"]     # the row's point; a row removed since: the order's
    if not point_id:
        raise OthSaleError("У заказа нет точки: некуда вернуть шины.")
    if conn.execute("UPDATE order_item_oth SET undone_at = ? WHERE item_id = ? AND " + _LIVE, (now, line["id"])).rowcount != 1:
        conn.rollback()
        raise OthSaleError("Позицию только что вернули в другой вкладке. Обновите страницу.")
    conn.execute("DELETE FROM order_item WHERE id = ?", (line["id"],))
    if pieces >= 1:
        oth_manual.give_back(conn, str(line["goods_id"]), int(point_id), pieces, now, author, card["id"])
    orders.add_event(conn, card["id"], now, author, "item", "Возвращено на ОТХ (свой остаток): %s × %d" % (line["name"], pieces))
    _audit(conn, now, author, "oth_untake", {"order": int(card["id"]), "item": line["id"], "manual": -int(link["from_item"]), "qty": pieces})
    _sync_numbers(conn, card["id"], before, now, author)
    conn.commit()
    return pieces


def can_untake(conn, card_id, item_id) -> bool:
    """«Вернуть на ОТХ» is shown: the old order can take the tyres back (it stands in «На ОТХ», or in «Отмена» closed by this take, and no
    return to the supplier is agreed for it). Otherwise there is no button (the owner 2026-10-30): the tyres stay in the order, and a client
    who refuses sends the order to «На ОТХ» with them, as any refusal."""
    link = taken(conn, card_id).get(int(item_id))
    if link is not None and not link["from_order"]:
        return True                                                   # «Свой остаток (ОТХ)»: the tyres go back to the point's manual row
    if link is None or conn.execute("SELECT 1 FROM oth_return_plan WHERE order_id = ?", (link["from_order"],)).fetchone():
        return False
    old = conn.execute("SELECT s.code, c.cancel_reason FROM order_item i JOIN order_card c ON c.id = i.order_id JOIN order_stage s ON s.id = c.stage_id "
                       "WHERE i.id = ? AND c.id = ?", (link["from_item"], link["from_order"])).fetchone()
    return old is not None and (old[0] in orders.OTH_CODES or (old[0] == "cancel" and str(old[1]).startswith(TAKEN_REASON.split("%")[0])))


def all_from_oth(conn, card: dict) -> bool:
    """Every tyre of the order is taken from «На ОТХ» (no position of the supplier with pieces) and it is not placed yet: the card offers
    «Шины уже на точке — перевести на «Товар на точке»» (the owner 2026-10-30), past «Заказ оформлен» and «Перемещение»."""
    mine = taken_ids(conn, card["id"])
    held = [i for i in card["items"] if int(i["qty"]) >= 1]
    return bool(held) and all(i["id"] in mine for i in held) and why_not(conn, card) is None


def point_text(conn, card: dict, now: str) -> str:
    """The point's message of «Шины уже на точке»: the owner's template «Точке: шины с ОТХ отложены под клиента» if there is one, else the
    sample (also in place of a retired sample kept word for word); the values it lacks are left out."""
    from . import order_templates as templates
    row = conn.execute("SELECT body FROM order_template WHERE name = ? AND audience = 'manager' ORDER BY id LIMIT 1",
                       (templates.OTH_POINT_SAMPLE[0],)).fetchone()
    kept = row and row[0] not in templates.RETIRED.get(templates.OTH_POINT_SAMPLE[0], ())
    return templates.render(conn, card, row[0] if kept else templates.OTH_POINT_SAMPLE[2], now)[0]


def note_sales(conn, card: dict, items: List[dict], ids: List[int]) -> None:
    """The sales just written for `items` of the order (one per position, in their order): those of the positions «с ОТХ» are kept. No commit."""
    mine = taken_ids(conn, card["id"])
    for item, sale_id in zip(items, ids):
        if item["id"] in mine:
            conn.execute("UPDATE order_item_oth SET sale_id = ? WHERE item_id = ?", (sale_id, item["id"]))
