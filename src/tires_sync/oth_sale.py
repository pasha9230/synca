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
«Товар на точке» (at_point). The rows: order_item_oth (store.py)."""
from __future__ import annotations

import json
from typing import Dict, List, Optional, Set

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


def first_order(conn, item_id) -> Optional[int]:
    """The order the tyres of a position «с ОТХ» were bought for: along a chain (A → B → C) to its first order."""
    found, seen = None, set()
    row = conn.execute("SELECT from_item, from_order FROM order_item_oth WHERE item_id = ?", (item_id,)).fetchone()
    while row is not None and row[0] not in seen:
        seen.add(row[0])
        found = row[1]
        row = conn.execute("SELECT from_item, from_order FROM order_item_oth WHERE item_id = ?", (row[0],)).fetchone()
    return found


def from_number(conn, card_id) -> Dict[int, str]:
    """Item id of a position «с ОТХ» → the WebMIM number of the order its tyres were bought by (the Google table; the first of a chain)."""
    out = {}
    for item_id in taken(conn, card_id):
        row = conn.execute("SELECT webmim_number, webmim_id FROM order_card WHERE id = ?", (first_order(conn, item_id),)).fetchone()
        out[item_id] = (row[0] or row[1] or "") if row else ""
    return out


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
    return ("В заказе шины с ОТХ (заказ %s): переведите заказ на «На ОТХ»%s." % (
        ", ".join("№%d" % n for n in found), " или верните их («Вернуть на ОТХ» у позиций)" if can_return else ""))


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
    if stage["kind"] != "open" or stage["code"] in ("oth", orders.RETURN_GIVEN) or orders._needs_placed(stage, orders._placed_stage(conn)):
        return "Брать с ОТХ можно только до «Заказ оформлен»: заказ уже на этапе «%s»." % stage["name"]
    if card["sale_ids"]:
        return "По заказу записана продажа."
    if card["webmim_id"] or card["webmim_number"]:
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


def _stage(conn, code: str) -> dict:
    found = next((s for s in orders.list_stages(conn) if s["code"] == code), None)
    if found is None:
        raise OthSaleError("В воронке нет этапа «%s»: взять и вернуть шины с ОТХ нельзя." % {"cancel": "Отмена", "oth": "На ОТХ"}[code])
    return found


def take(conn, card_id, line_id, from_item, now: str, author: str) -> int:
    """«Взять с ОТХ»: the supplier's position `line_id` of the order gives up to its pieces to tyres of the position `from_item` lying on «На ОТХ».
    One transaction (the old order emptied is moved to «Отмена» in it). Returns the pieces taken; OthSaleError says why nothing was."""
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
    old = orders.get_card(conn, source["order_id"])
    item = next(i for i in old["items"] if i["id"] == source["item_id"])
    cancel = _stage(conn, "cancel")
    if not card["point_id"]:                                        # the order's point is the one the tyres lie at (its time, its checks)
        try:
            orders.update_card(conn, card_id, {"point_id": source["point_id"]}, now, author, commit=False)
        except orders.OrderError:
            conn.rollback()
            raise
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
    left = conn.execute("SELECT COALESCE(SUM(qty), 0) FROM order_item WHERE order_id = ?", (old["id"],)).fetchone()[0]
    if left > 0:
        conn.commit()
        return pieces
    try:
        moved = orders.move_card(conn, old["id"], cancel["id"], now, author, changes={"cancel_reason": TAKEN_REASON % card_id}, fire_rules=False,
                                 journal=False)
    except orders.OrderError as err:                                  # rolled back, the take with it
        raise OthSaleError("Шины не взяты: старый заказ №%d не закрылся (%s)." % (old["id"], err)) from None
    if not moved:                                                     # moved by somebody meanwhile: move_card rolled it all back
        raise OthSaleError("Шины не взяты: старый заказ №%d только что изменили. Обновите страницу." % old["id"])
    return pieces


def to_new_client(conn, old_id, name, phone, now: str, author: str) -> int:
    """Back from «На ОТХ» to «Товар на точке» with a new client (the owner 2026-10-30, the order 444): a new order of the same point, with the
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
            untake(conn, new, item_id, now, author)
        trash.delete_order(conn, new, now, author)
    except Exception as err:                                            # noqa: BLE001
        conn.rollback()
        orders.add_event(conn, new, now, author, "field", "Шины с ОТХ взяты не полностью (%s): проверьте заказ, не оформляйте его в WebMIM." % err,
                         level="red")
        conn.commit()


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
    if conn.execute("SELECT 1 FROM oth_return_plan WHERE order_id = ?", (link["from_order"],)).fetchone():
        raise OthSaleError("По заказу №%d согласован возврат поставщику: шины в него не вернуть (акт МХ-3 разойдётся). " % link["from_order"] + NO_BACK)
    old = conn.execute("SELECT c.id, s.code, c.cancel_reason FROM order_item i JOIN order_card c ON c.id = i.order_id JOIN order_stage s ON s.id = c.stage_id "
                       "WHERE i.id = ? AND c.id = ?", (link["from_item"], link["from_order"])).fetchone()
    reopen = old is not None and old[1] == "cancel" and str(old[2]).startswith(TAKEN_REASON.split("%")[0])
    if old is None or (old[1] != "oth" and not reopen):
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
    if not reopen:
        conn.commit()
        return pieces
    oth = _stage(conn, "oth")
    try:
        moved = orders.move_card(conn, link["from_order"], oth["id"], now, author, fire_rules=False, journal=False)
    except orders.OrderError as err:
        raise OthSaleError("Шины не возвращены: заказ №%d не встал на ОТХ (%s)." % (link["from_order"], err)) from None
    if not moved:
        raise OthSaleError("Шины не возвращены: заказ №%d только что изменили. Обновите страницу." % link["from_order"])
    return pieces


def can_untake(conn, card_id, item_id) -> bool:
    """«Вернуть на ОТХ» is shown: the old order can take the tyres back (it stands in «На ОТХ», or in «Отмена» closed by this take, and no
    return to the supplier is agreed for it). Otherwise there is no button (the owner 2026-10-30): the tyres stay in the order, and a client
    who refuses sends the order to «На ОТХ» with them, as any refusal."""
    link = taken(conn, card_id).get(int(item_id))
    if link is None or conn.execute("SELECT 1 FROM oth_return_plan WHERE order_id = ?", (link["from_order"],)).fetchone():
        return False
    old = conn.execute("SELECT s.code, c.cancel_reason FROM order_item i JOIN order_card c ON c.id = i.order_id JOIN order_stage s ON s.id = c.stage_id "
                       "WHERE i.id = ? AND c.id = ?", (link["from_item"], link["from_order"])).fetchone()
    return old is not None and (old[0] == "oth" or (old[0] == "cancel" and str(old[1]).startswith(TAKEN_REASON.split("%")[0])))


def all_from_oth(conn, card: dict) -> bool:
    """Every tyre of the order is taken from «На ОТХ» (no position of the supplier with pieces) and it is not placed yet: the card offers
    «Шины уже на точке — перевести на «Товар на точке»» (the owner 2026-10-30), past «Заказ оформлен» and «Перемещение»."""
    mine = taken_ids(conn, card["id"])
    held = [i for i in card["items"] if int(i["qty"]) >= 1]
    return bool(held) and all(i["id"] in mine for i in held) and why_not(conn, card) is None


def point_text(conn, card: dict, now: str) -> str:
    """The point's message of «Шины уже на точке»: the owner's template «Точке: шины с ОТХ отложены под клиента» if there is one, else the
    sample; the values it lacks are left out."""
    from . import order_templates as templates
    row = conn.execute("SELECT body FROM order_template WHERE name = ? AND audience = 'manager' ORDER BY id LIMIT 1",
                       (templates.OTH_POINT_SAMPLE[0],)).fetchone()
    return templates.render(conn, card, row[0] if row else templates.OTH_POINT_SAMPLE[2], now)[0]


def note_sales(conn, card: dict, items: List[dict], ids: List[int]) -> None:
    """The sales just written for `items` of the order (one per position, in their order): those of the positions «с ОТХ» are kept. No commit."""
    mine = taken_ids(conn, card["id"])
    for item, sale_id in zip(items, ids):
        if item["id"] in mine:
            conn.execute("UPDATE order_item_oth SET sale_id = ? WHERE item_id = ?", (sale_id, item["id"]))
