"""«Заказы WebMIM» of an order (the owner 2026-11-07: every placed order of WebMIM whose tyres reached the point is kept): the parts of an order,
each one order of WebMIM (or none), from its sources.

- «oth»: positions taken from «На ОТХ» (oth_sale): the WebMIM number, the transfer and the day of the invoice of the order of purchase (A of a
  chain A → B → C), until a person types the numbers for that very position (order_item_mim): the number typed on the order no longer stands
  for them (it is the order's own order of WebMIM).
- «stock»: «Свой остаток (ОТХ)» of the point (oth_manual): no order of WebMIM, the fields are empty unless typed for the position.
- «merged»: the positions a placed source of a merge gave (order_merge.items_of): the source's numbers.
- «supplier»: the supplier's positions: the order's own number, transfer and invoice.
- «extra»: the pieces added to the supplier's positions after the order had its own number (`snapshot`, order_mim_base): a second order of
  WebMIM, its numbers typed apart (order_mim_extra); the old numbers are lit while the new ones are empty.
A part never carries a price, a purchase or a profit. The Google table (order_sheet.card_rows) has one row per position and part: a part
«extra» is a row of its own (order_sheet.extra_key); the row of the first part keeps its pieces. Nothing here writes to WebMIM."""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

from . import orders, store

MIGRATED = "order_webmim_migrated"               # app_state: the migration ran (the moment)
_LIVE = "undone_at = ''"


def _clean(number, transfer) -> tuple:
    return orders._single(number, 60, "Номер заказа WebMIM"), orders._single(transfer, 40, "Номер перемещения")


def own_number(conn, card: dict) -> bool:
    """The order has an order of WebMIM of its own: a number or an id that is not the one of the order its tyres «с ОТХ» were bought by."""
    from . import oth_sale
    return bool(card.get("webmim_id") or card.get("webmim_number")) and card["id"] not in oth_sale.borrowed_ids(conn, card["id"])


# ---- (2) the numbers typed for one position «с ОТХ» ---------------------------------------------------------------------------------------

def item_numbers(conn, card_id) -> Dict[int, dict]:
    return {r[0]: {"webmim_number": r[1], "transfer_number": r[2]} for r in conn.execute(
        "SELECT item_id, webmim_number, transfer_number FROM order_item_mim WHERE order_id = ?", (int(card_id),))}


def set_item_numbers(conn, card_id, item_id, number, transfer, now: str, author: str) -> None:
    """The numbers of one position «с ОТХ» (or of the point's own stock); both empty: the source's again. A journal line and the audit."""
    from . import oth_sale
    if int(item_id) not in oth_sale.taken(conn, int(card_id)):
        raise orders.OrderError("Номер для строки вписывается только у позиции с ОТХ.")
    number, transfer = _clean(number, transfer)
    if number or transfer:
        conn.execute("INSERT INTO order_item_mim (item_id, order_id, webmim_number, transfer_number, at, author) VALUES (?,?,?,?,?,?) "
                     "ON CONFLICT(item_id) DO UPDATE SET webmim_number = excluded.webmim_number, transfer_number = excluded.transfer_number, "
                     "at = excluded.at, author = excluded.author", (int(item_id), int(card_id), number, transfer, now, author))
    else:
        conn.execute("DELETE FROM order_item_mim WHERE item_id = ?", (int(item_id),))
    name = conn.execute("SELECT name FROM order_item WHERE id = ?", (int(item_id),)).fetchone()
    orders.add_event(conn, int(card_id), now, author, "field", "Номера WebMIM строки «%s»: %s" % (
        name[0] if name else item_id, "№ %s, перемещение %s" % (number or "—", transfer or "—") if number or transfer else "как у источника"), level="grey")
    store.write_audit(conn, now, author, "order_item_mim", {"order": int(card_id), "item": int(item_id), "number": number, "transfer": transfer})


# ---- (3) the pieces added after the order had its own number ------------------------------------------------------------------------------

def _number_of(card: dict) -> str:
    return str(card.get("webmim_number") or card.get("webmim_id") or "")


def _away(conn, item_id) -> int:
    """The pieces a position gave to other orders and may get back: taken «с ОТХ» (live) and given to an active merge (not through «На ОТХ»:
    those are in order_item_oth already). Its pieces plus these stay the same whatever is taken, given back or merged (review 2026-11-07)."""
    taken = conn.execute("SELECT COALESCE(SUM(qty), 0) FROM order_item_oth WHERE from_item = ? AND " + _LIVE, (int(item_id),)).fetchone()[0]
    merged = conn.execute("SELECT COALESCE(SUM(i.qty), 0) FROM order_merge_item i JOIN order_merge m ON m.id = i.merge_id "
                          "WHERE i.from_item = ? AND i.oth = 0 AND m.undone_at = ''", (int(item_id),)).fetchone()[0]
    return int(taken or 0) + int(merged or 0)


def _held(conn, item: dict) -> int:
    return int(item["qty"]) + _away(conn, item["id"])


def snapshot(conn, card_id, now: str, fresh: bool = False) -> bool:
    """The pieces of the supplier's positions of an order with its own number (with those it gave to other orders: _away), once; the mark
    (item 0) keeps the number it was taken for. A number corrected later only moves the mark: what was added and its second numbers stay.
    `fresh` («Оформить в WebMIM»: everything in the order then is in that order of WebMIM): taken again; the rows of a second order already
    known stay (their pieces may go to 0, never away). No commit. True when taken now."""
    from . import oth_sale
    card = orders.get_card(conn, int(card_id))
    if not own_number(conn, card):
        return False
    number = _number_of(card)
    mark = conn.execute("SELECT number FROM order_mim_base WHERE order_id = ? AND item_id = 0", (card["id"],)).fetchone()
    if mark is not None and not fresh:
        if mark[0] != number:
            conn.execute("UPDATE order_mim_base SET number = ? WHERE order_id = ?", (number, card["id"]))
        return False
    conn.execute("DELETE FROM order_mim_base WHERE order_id = ?", (card["id"],))
    conn.execute("INSERT INTO order_mim_base (order_id, item_id, qty, at, number) VALUES (?, 0, 0, ?, ?)", (card["id"], now, number))
    for item in oth_sale.supplier_items(conn, card):
        conn.execute("INSERT INTO order_mim_base (order_id, item_id, qty, at, number) VALUES (?,?,?,?,?)", (card["id"], item["id"], _held(conn, item), now, number))
    return True


def fresh_snapshot(conn, card_id, now: str, fresh: bool = True) -> None:
    """«Оформить» / «Досоздать в WebMIM» done: the snapshot taken again, inside a savepoint: a failure here never costs the number of the order
    made nor its record. `fresh=False` (the number written by itself from WebMIM: «Это он», «Заказ есть», the owner's «А» 07.11): taken
    once, a snapshot taken before is kept. No commit."""
    try:
        conn.execute("SAVEPOINT mim_snapshot")
        snapshot(conn, card_id, now, fresh=fresh)
        conn.execute("RELEASE SAVEPOINT mim_snapshot")
    except Exception:                                                 # noqa: BLE001
        conn.execute("ROLLBACK TO SAVEPOINT mim_snapshot")
        conn.execute("RELEASE SAVEPOINT mim_snapshot")
        logging.getLogger("tires_sync.orders").exception("the snapshot of order %s was not taken", card_id)


def extras(conn, card: dict) -> Dict[int, dict]:
    """item → {qty added past the snapshot (0 or more), webmim_number, transfer_number}: a position with pieces added, and every position that
    has a row of a second order already (order_mim_extra: it never goes away, its pieces follow the order; the table's row with them). {} with
    no snapshot."""
    from . import oth_sale
    rows = conn.execute("SELECT item_id, qty FROM order_mim_base WHERE order_id = ?", (card["id"],)).fetchall()
    base = {r[0]: r[1] for r in rows}
    if 0 not in base:
        return {}
    known = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT item_id, webmim_number, transfer_number FROM order_mim_extra WHERE order_id = ?", (card["id"],))}
    out = {}
    for item in oth_sale.supplier_items(conn, card):
        added = min(int(item["qty"]), max(0, _held(conn, item) - int(base.get(item["id"], 0))))
        if added >= 1 or item["id"] in known:
            number, transfer = known.get(item["id"], ("", ""))
            out[item["id"]] = {"qty": added, "webmim_number": number, "transfer_number": transfer, "in_base": item["id"] in base}
    return out


def keep_extras(conn, card: dict, now: str) -> bool:
    """The positions with pieces added get their row of a second order (empty numbers): from now on it is never lost. No commit."""
    made = False
    for item_id, extra in extras(conn, card).items():
        if extra["qty"] >= 1:
            made = conn.execute("INSERT OR IGNORE INTO order_mim_extra (order_id, item_id, at, author) VALUES (?,?,?, 'система')",
                                (card["id"], item_id, now)).rowcount == 1 or made
    return made


def set_extra_numbers(conn, card_id, item_id, number, transfer, now: str, author: str) -> None:
    """The second order of WebMIM of the pieces added to a position. A journal line and the audit."""
    card = orders.get_card(conn, int(card_id))
    if int(item_id) not in extras(conn, card):
        raise orders.OrderError("У этой позиции нет добавленных шин без номера.")
    number, transfer = _clean(number, transfer)
    conn.execute("INSERT INTO order_mim_extra (order_id, item_id, webmim_number, transfer_number, at, author) VALUES (?,?,?,?,?,?) "
                 "ON CONFLICT(order_id, item_id) DO UPDATE SET webmim_number = excluded.webmim_number, transfer_number = excluded.transfer_number, "
                 "at = excluded.at, author = excluded.author", (card["id"], int(item_id), number, transfer, now, author))
    name = next((i["name"] for i in card["items"] if i["id"] == int(item_id)), str(item_id))
    orders.add_event(conn, card["id"], now, author, "field", "Второй заказ WebMIM для добавленных шин «%s»: № %s, перемещение %s" % (
        name, number or "—", transfer or "—"), level="grey")
    store.write_audit(conn, now, author, "order_mim_extra", {"order": card["id"], "item": int(item_id), "number": number, "transfer": transfer})


# ---- (4) the chain -----------------------------------------------------------------------------------------------------------------------

def chain(conn, item_id) -> List[int]:
    """The orders a position «с ОТХ» came through, from the nearest to the first (C ← B ← A: [B, A]); 0 for «Свой остаток»."""
    out, seen = [], set()
    row = conn.execute("SELECT from_item, from_order FROM order_item_oth WHERE item_id = ? AND " + _LIVE, (int(item_id),)).fetchone()
    while row is not None and row[0] not in seen:
        seen.add(row[0])
        out.append(int(row[1] or 0))
        row = conn.execute("SELECT from_item, from_order FROM order_item_oth WHERE item_id = ?", (row[0],)).fetchone()
    return out


def oth_note(conn, item_id) -> str:
    """«с ОТХ, заказ №A, через №B» (the owner 2026-11-07): A the order of purchase (its numbers are the row's), B the orders between;
    «с ОТХ, свой остаток» for the point's own stock."""
    from . import oth_sale
    links = chain(conn, item_id)
    if not links or links[0] == 0:
        return "с ОТХ, свой остаток"
    if links[-1] == 0:                                                 # a chain that began at the point's own stock: no order of purchase
        return "с ОТХ, свой остаток, через %s" % ", ".join("№%d" % o for o in reversed(links) if o)
    first = oth_sale.purchase_order(conn, item_id) or links[-1]
    between = [o for o in reversed(links) if o and o != first]
    return "с ОТХ, заказ №%d" % first + (", через %s" % ", ".join("№%d" % o for o in between) if between else "")


# ---- (1) the parts -------------------------------------------------------------------------------------------------------------------------

def _lines(items) -> List[dict]:
    return [{"id": i["id"], "name": i["name"], "qty": int(i["qty"])} for i in items]


def parts(conn, card: dict, take_snapshot: bool = False, now: str = "") -> List[dict]:
    """The orders of WebMIM of the order, in the order of the card: [{kind, label, source, number, transfer, day, items: [{id, name, qty}],
    typed, highlight, needs_numbers}]. `take_snapshot`: the card is looked at (the panel): an order with its own number gets its snapshot now."""
    from . import order_merge, order_sheet, oth_sale
    if take_snapshot:
        when = now or store.moscow_iso()
        if snapshot(conn, card["id"], when) | keep_extras(conn, card, when):
            conn.commit()
    numbers = oth_sale.from_number(conn, card["id"])
    typed = item_numbers(conn, card["id"])
    merged = order_merge.items_of(conn, card["id"])
    added = extras(conn, card)
    groups: Dict[tuple, dict] = {}

    def put(kind, label, source, number, transfer, day, item, qty, typed_here=False):
        key = (kind, source, number, transfer, day)
        part = groups.setdefault(key, {"kind": kind, "label": label, "source": source, "number": number, "transfer": transfer, "day": day,
                                       "items": [], "typed": False, "highlight": False, "needs_numbers": False})
        part["items"].append({"id": item["id"], "name": item["name"], "qty": qty})
        part["typed"] = part["typed"] or typed_here

    for item in card["items"]:
        qty = int(item["qty"])
        if qty < 1:
            continue
        if item["id"] in numbers:
            links = chain(conn, item["id"])
            if not links or links[0] == 0 or links[-1] == 0:                 # the point's own stock, also along a chain that began there
                mine = typed.get(item["id"], {})
                put("stock", "Свой остаток точки" if links and links[0] == 0 else oth_note(conn, item["id"]), None, mine.get("webmim_number", ""), mine.get("transfer_number", ""), "", item, qty, bool(mine))
                continue
            values = order_sheet._oth_values(conn, card, item["id"], numbers[item["id"]], set())
            first = oth_sale.purchase_order(conn, item["id"])
            put("oth", oth_note(conn, item["id"]), first, values["№ заказа MIM"], values["Перемещение MIM"], values["Дата счета"], item, qty,
                item["id"] in typed)
        elif item["id"] in merged and merged[item["id"]]["placed"]:
            came = merged[item["id"]]
            put("merged", "Из заказа №%d (объединение)" % came["from_order"], came["from_order"], came["number"], came["transfer"],
                order_sheet._invoice_day(conn, orders.get_card(conn, came["from_order"])), item, qty)
        else:
            extra = added.get(item["id"])
            own = qty - (extra["qty"] if extra else 0)
            if own >= 1:
                put("supplier", "Свой заказ WebMIM", card["id"], card["webmim_number"] or card["webmim_id"], card["transfer_number"],
                    order_sheet._invoice_day(conn, card) if (card["webmim_number"] or card["webmim_id"]) else "", item, own)
            if extra and extra["qty"] >= 1:
                put("extra", "Добавленные шины: второй заказ WebMIM", card["id"], extra["webmim_number"], extra["transfer_number"],
                    order_sheet.day_of_number(conn, extra["webmim_number"]), item, extra["qty"])
    out = list(groups.values())
    waiting = [p for p in out if p["kind"] == "extra" and not p["number"]]
    for part in out:
        if part["kind"] == "extra":
            part["needs_numbers"] = not part["number"]
        if part["kind"] == "supplier" and waiting:
            part["highlight"] = True                                          # the old numbers lit while the second ones are empty
    return out


# ---- the migration -----------------------------------------------------------------------------------------------------------------------

def migrate(conn, now: str) -> int:
    """Once (MIGRATED): the old orders read as before. A position «с ОТХ» whose row showed the number (or the transfer) typed on the order (the
    rule before 2026-11-07: oth_sale.typed_numbers) keeps it as typed for itself; every order with its own number gets its snapshot as it is
    now (nothing of before becomes «added»). A busy base: nothing kept, the next start tries again. Returns the positions kept."""
    try:
        if conn.execute("SELECT 1 FROM app_state WHERE key = ?", (MIGRATED,)).fetchone():
            return 0
        conn.execute("SELECT 1 FROM order_item_oth LIMIT 1")
    except Exception:                                                         # noqa: BLE001 - a base without the orders' tables
        return 0
    try:
        conn.commit()                                                         # nothing of an earlier step left open in the migration's lock
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM app_state WHERE key = ?", (MIGRATED,)).fetchone():
            conn.rollback()
            return 0
        kept = _migrate(conn, now)
        conn.execute("INSERT INTO app_state (key, value) VALUES (?, ?)", (MIGRATED, now))
        conn.commit()
        return kept
    except Exception:                                                         # noqa: BLE001
        conn.rollback()
        logging.getLogger("tires_sync.orders").exception("the migration of «Заказы WebMIM» failed")
        return 0


def _migrate(conn, now: str) -> int:
    from . import oth_sale
    kept = 0
    ids = [r[0] for r in conn.execute("SELECT DISTINCT order_id FROM order_item_oth WHERE " + _LIVE).fetchall()]
    for card_id in ids:
        card = orders.get_card(conn, card_id)
        typed = oth_sale.typed_numbers(conn, card)
        if not typed:
            continue
        numbers = oth_sale.from_number(conn, card_id)
        for item_id, number in numbers.items():
            mine = card["webmim_number"] if "webmim_number" in typed and card["webmim_number"] != number else ""
            transfer = card["transfer_number"] if "transfer_number" in typed else ""
            first = oth_sale.purchase_order(conn, item_id)
            if first and conn.execute("SELECT 1 FROM order_card WHERE id = ?", (first,)).fetchone() is None:
                mine, transfer = card["webmim_number"] or card["webmim_id"], card["transfer_number"]    # the source gone: the old rule read the order
            if mine or transfer:
                conn.execute("INSERT OR IGNORE INTO order_item_mim (item_id, order_id, webmim_number, transfer_number, at, author) VALUES (?,?,?,?,?,?)",
                             (item_id, card_id, mine, transfer, now, "перенос"))
                kept += 1
    for (card_id,) in conn.execute("SELECT id FROM order_card WHERE webmim_id != '' OR webmim_number != ''").fetchall():
        snapshot(conn, card_id, now)
    return kept
