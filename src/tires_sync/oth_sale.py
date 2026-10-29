"""«Взять с ОТХ» (the owner 2026-10-29): the tyres of an order standing in «На ОТХ» (its client refused, they lie at the point) are sold to
a new order at the same point.

The card of a new order shows, under a position of the supplier, «На <точка> есть N шт (ОТХ, заказ №A)» and the button: the position gives
up to its pieces to a position «с ОТХ» of the same tyre (the purchase of the old order, kept from WebMIM: purchase_manual), the quantity of
the old order's position is lowered by them, and the old order emptied goes to «Отмена» by itself («Шины переданы в заказ №B», no rule of the
stage: its client is not written to). Only what lacks stays the supplier's: «Оформить в WebMIM», the check against WebMIM and the purchase
from WebMIM leave the positions «с ОТХ» alone; their sale is counted at the old order's purchase; the old order never has a sale, so the
revenue is counted once. The tyres must lie at the order's point: another point is never offered. The new order must not be in WebMIM yet.
«Вернуть на ОТХ» gives them back (the old order closed by the take stands in «На ОТХ» again). The rows: order_item_oth (store.py)."""
from __future__ import annotations

import json
from typing import Dict, List, Optional, Set

from . import orders, oth_stock

TAKEN_REASON = "Шины переданы в заказ №%d"
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
    """The positions of the order that are the supplier's (not taken from «На ОТХ»): what WebMIM is asked for and checked against."""
    mine = taken_ids(conn, card["id"])
    return [i for i in card.get("items") or [] if i["id"] not in mine]


def sale_ids(conn) -> Set[int]:
    """The sales of the positions «с ОТХ»: counted at the purchase written (the old order's), never at WebMIM's price of the new order."""
    return {r[0] for r in conn.execute("SELECT sale_id FROM order_item_oth WHERE sale_id IS NOT NULL")}


def given(conn, card_id) -> Set[int]:
    """The positions of an old order that gave tyres to a new one (ever): their quantity in the Google table follows the order."""
    return {r[0] for r in conn.execute("SELECT DISTINCT from_item FROM order_item_oth WHERE from_order = ?", (card_id,))}


def from_number(conn, card_id) -> Dict[int, str]:
    """Item id of a position «с ОТХ» → the WebMIM number of the order its tyres came from (the Google table)."""
    out = {}
    for item_id, number, webmim_id in conn.execute(
            "SELECT t.item_id, c.webmim_number, c.webmim_id FROM order_item_oth t JOIN order_card c ON c.id = t.from_order "
            "WHERE t.order_id = ? AND t." + _LIVE, (card_id,)):
        out[item_id] = number or webmim_id or ""
    return out


def holds(conn, card_id) -> List[int]:
    """The old orders whose tyres the order holds now (positions «с ОТХ» with pieces): the order may not be cancelled or deleted with them."""
    return sorted({r[0] for r in conn.execute(
        "SELECT t.from_order FROM order_item_oth t JOIN order_item i ON i.id = t.item_id WHERE t.order_id = ? AND t." + _LIVE + " AND i.qty >= 1",
        (card_id,))})


def holds_text(found: List[int]) -> str:
    return ("В заказе шины с ОТХ (заказ %s): сначала нажмите «Вернуть на ОТХ» у этих позиций или переведите заказ в «На ОТХ»."
            % ", ".join("№%d" % n for n in found))


def why_not(conn, card: dict) -> Optional[str]:
    """Why the order can take nothing from «На ОТХ» (or give it back) now; None: it can."""
    stage = card["stage"]
    if stage["kind"] != "open" or stage["code"] in ("oth", orders.RETURN_GIVEN) or orders._needs_placed(stage, orders._placed_stage(conn)):
        return "Брать с ОТХ можно только до «Заказ оформлен»: заказ уже на этапе «%s»." % stage["name"]
    if card["sale_ids"]:
        return "По заказу записана продажа."
    if card["webmim_id"] or card["webmim_number"]:
        return "Заказ уже есть в WebMIM: шины с ОТХ к нему не добавить."
    state = conn.execute("SELECT state FROM webmim_create WHERE order_id = ?", (card["id"],)).fetchone()
    if state and state[0] != "refused":
        return "Заказ уже оформляется в WebMIM: шины с ОТХ к нему не добавить."
    return None


def _point_name(row) -> str:
    return (row["point"] or "").strip() or "точке №%s" % row["point_id"]


def offers(conn, card: dict) -> Dict[int, List[dict]]:
    """Position of the supplier → the tyres of it lying on «На ОТХ» (oth_stock: no test point, no agreed return), other orders only:
    [{from_item, from_order, point_id, point, qty, take, why}]. `take`: the pieces the button takes; `why`: why it cannot (another point,
    the order already in WebMIM) — shown, the button is not."""
    mine = taken_ids(conn, card["id"])
    lines = [i for i in card["items"] if i["id"] not in mine and i["goods_id"] != orders.POINT_GOODS and int(i["qty"]) >= 1]
    if not lines:
        return {}
    stock: Dict[str, List[dict]] = {}
    for r in oth_stock._rows(conn):
        if r["order_id"] != card["id"]:
            stock.setdefault(str(r["goods_id"]), []).append(r)
    blocked = why_not(conn, card)
    out: Dict[int, List[dict]] = {}
    for line in lines:
        for r in stock.get(str(line["goods_id"]), ()):
            why = blocked
            if why is None and card["point_id"] and card["point_id"] != r["point_id"]:
                why = "Шины лежат на «%s», а заказ на «%s»: взять нельзя." % (_point_name(r), card["point"]["name"] if card["point"] else card["point_id"])
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


def take(conn, card_id, line_id, from_item, now: str, author: str) -> int:
    """«Взять с ОТХ»: the supplier's position `line_id` of the order gives up to its pieces to tyres of the position `from_item` lying on «На ОТХ».
    One transaction (the old order emptied is moved to «Отмена» in it). Returns the pieces taken; OthSaleError says why nothing was."""
    card = orders.get_card(conn, card_id)
    why = why_not(conn, card)
    if why:
        raise OthSaleError(why)
    line = next((i for i in card["items"] if i["id"] == int(line_id)), None)
    if line is None or line["id"] in taken_ids(conn, card_id):
        raise OthSaleError("Такой позиции поставщика в заказе нет: обновите страницу.")
    source = next((r for r in oth_stock._rows(conn) if r["item_id"] == int(from_item)), None)
    if source is None:
        raise OthSaleError("Этих шин на ОТХ уже нет: их взяли, вернули поставщику или заказ ушёл с ОТХ. Обновите страницу.")
    if source["order_id"] == card_id or str(source["goods_id"]) != str(line["goods_id"]):
        raise OthSaleError("Это не те шины: обновите страницу.")
    if card["point_id"] and card["point_id"] != source["point_id"]:
        raise OthSaleError("Шины лежат на «%s», а заказ на другой точке: взять нельзя. Другую точку не предлагаем." % _point_name(source))
    pieces = min(int(source["qty"]), int(line["qty"]))
    old = orders.get_card(conn, source["order_id"])
    item = next(i for i in old["items"] if i["id"] == source["item_id"])
    # the limit in the statement itself: a take a moment ago in another tab never lets the pieces go below the ones marked returned
    if conn.execute("UPDATE order_item SET qty = qty - ? WHERE id = ? AND qty - ? >= COALESCE((SELECT qty FROM oth_return WHERE item_id = ?), 0) "
                    "AND order_id IN (SELECT c.id FROM order_card c JOIN order_stage s ON s.id = c.stage_id WHERE s.code = 'oth') "
                    "AND order_id NOT IN (SELECT order_id FROM oth_return_plan)",
                    (pieces, item["id"], pieces, item["id"])).rowcount != 1:
        conn.rollback()
        raise OthSaleError("Столько шин на ОТХ уже нет: их только что взяли в другой вкладке. Обновите страницу.")
    if pieces >= int(line["qty"]):
        conn.execute("DELETE FROM order_item WHERE id = ?", (line["id"],))
    else:
        conn.execute("UPDATE order_item SET qty = qty - ? WHERE id = ?", (pieces, line["id"]))
    cur = conn.execute(
        "INSERT INTO order_item (order_id, goods_id, code, name, brand_key, season, diameter, territory, qty, supplier_price, sale_unit, terms, "
        "fee_pct, promo_pct, purchase_manual) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
        (card_id, line["goods_id"], line["code"] or item["code"], line["name"], line["brand_key"], line["season"], line["diameter"], line["territory"],
         pieces, float(item["supplier_price"]), float(line["sale_unit"]), json.dumps(line["terms"] or {}, ensure_ascii=False), float(line["fee_pct"]),
         float(line["promo_pct"])))
    conn.execute("INSERT INTO order_item_oth (item_id, order_id, from_item, from_order, qty, at, author) VALUES (?,?,?,?,?,?,?)",
                 (cur.lastrowid, card_id, item["id"], old["id"], pieces, now, author))
    if not card["point_id"]:
        conn.execute("UPDATE order_card SET point_id = ?, updated_at = ? WHERE id = ?", (source["point_id"], now, card_id))
        orders.add_event(conn, card_id, now, author, "field", "Точка: %s — там лежат шины с ОТХ" % _point_name(source))
    orders.add_event(conn, card_id, now, author, "item", "Взято с ОТХ (заказ №%d, %s): %s × %d" % (old["id"], _point_name(source), line["name"], pieces))
    orders.add_event(conn, old["id"], now, author, "item", "Передано с ОТХ в заказ №%d: %s × %d" % (card_id, item["name"], pieces))
    _audit(conn, now, author, "oth_take", {"order": int(card_id), "item": cur.lastrowid, "from_order": old["id"], "from_item": item["id"], "qty": pieces})
    left = conn.execute("SELECT COALESCE(SUM(qty), 0) FROM order_item WHERE order_id = ?", (old["id"],)).fetchone()[0]
    if left > 0:
        conn.commit()
        return pieces
    cancel = next(s for s in orders.list_stages(conn) if s["code"] == "cancel")
    try:
        moved = orders.move_card(conn, old["id"], cancel["id"], now, author, changes={"cancel_reason": TAKEN_REASON % card_id}, fire_rules=False,
                                 journal=False)
    except orders.OrderError as err:                                  # rolled back, the take with it
        raise OthSaleError("Шины не взяты: старый заказ №%d не закрылся (%s)." % (old["id"], err)) from None
    if not moved:                                                     # moved by somebody meanwhile: move_card rolled it all back
        raise OthSaleError("Шины не взяты: старый заказ №%d только что изменили. Обновите страницу." % old["id"])
    return pieces


def untake(conn, card_id, item_id, now: str, author: str) -> int:
    """«Вернуть на ОТХ»: the position «с ОТХ» leaves the order and its pieces go back to the old order's position; the old order closed by
    the take stands in «На ОТХ» again. Returns the pieces given back."""
    card = orders.get_card(conn, card_id)
    link = taken(conn, card_id).get(int(item_id))
    if link is None:
        raise OthSaleError("Эта позиция не с ОТХ.")
    why = why_not(conn, card)
    if why:
        raise OthSaleError(why.replace("Брать с ОТХ", "Вернуть на ОТХ"))
    line = next(i for i in card["items"] if i["id"] == int(item_id))
    old = conn.execute("SELECT c.id, s.code, c.cancel_reason FROM order_item i JOIN order_card c ON c.id = i.order_id JOIN order_stage s ON s.id = c.stage_id "
                       "WHERE i.id = ? AND c.id = ?", (link["from_item"], link["from_order"])).fetchone()
    reopen = old is not None and old[1] == "cancel" and str(old[2]).startswith(TAKEN_REASON.split("%")[0])
    if old is None or (old[1] != "oth" and not reopen):
        raise OthSaleError("Заказ №%d ушёл с ОТХ: вернуть шины в него нельзя. Удалите позицию вручную, если шины не нужны." % link["from_order"])
    pieces = int(line["qty"])
    conn.execute("UPDATE order_item SET qty = qty + ? WHERE id = ?", (pieces, link["from_item"]))
    conn.execute("DELETE FROM order_item WHERE id = ?", (line["id"],))
    if conn.execute("UPDATE order_item_oth SET undone_at = ? WHERE item_id = ? AND " + _LIVE, (now, line["id"])).rowcount != 1:
        conn.rollback()
        raise OthSaleError("Позицию только что вернули в другой вкладке. Обновите страницу.")
    orders.add_event(conn, card_id, now, author, "item", "Возвращено на ОТХ (заказ №%d): %s × %d" % (link["from_order"], line["name"], pieces))
    orders.add_event(conn, link["from_order"], now, author, "item", "Шины вернулись на ОТХ из заказа №%d: %s × %d" % (card_id, line["name"], pieces))
    _audit(conn, now, author, "oth_untake", {"order": int(card_id), "item": line["id"], "from_order": link["from_order"], "qty": pieces})
    if not reopen:
        conn.commit()
        return pieces
    oth = next(s for s in orders.list_stages(conn) if s["code"] == "oth")
    try:
        moved = orders.move_card(conn, link["from_order"], oth["id"], now, author, fire_rules=False, journal=False)
    except orders.OrderError as err:
        raise OthSaleError("Шины не возвращены: заказ №%d не встал на ОТХ (%s)." % (link["from_order"], err)) from None
    if not moved:
        raise OthSaleError("Шины не возвращены: заказ №%d только что изменили. Обновите страницу." % link["from_order"])
    return pieces


def note_sales(conn, card: dict, ids: List[int]) -> None:
    """The sales just written for the order (one per position, in the order of card["items"]): those of the positions «с ОТХ» are kept. No commit."""
    mine = taken_ids(conn, card["id"])
    for item, sale_id in zip(card["items"], ids):
        if item["id"] in mine:
            conn.execute("UPDATE order_item_oth SET sale_id = ? WHERE item_id = ?", (sale_id, item["id"]))
