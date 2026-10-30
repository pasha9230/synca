"""«Объединить заказы» (the owner 2026-10-30; plan docs/runs/2026-10-30-order-merge): two and more orders of one point become one NEW order.

The tyres of a source are given as «Взять с ОТХ» gives them (oth_sale.take): the quantity of its position goes down to 0 (the row stays,
every link to it with it) and the new order gets a position of the same tyre at the source's purchase (purchase_manual: WebMIM never
rewrites it), with its line in order_merge_item; a source standing on «На ОТХ» gives them through order_item_oth too, so that all of
«с ОТХ» holds for them (the stock, the chain, «Б»). The new order has no WebMIM number of its own (one number on two orders breaks the
commands of the points and the reconciliation): a position's number is its source's, and a position of a placed source is never ordered
again (oth_sale.supplier_items). The sources go to «Отмена» («Объединён в заказ №N»), the revenue is the new order's sale, once.
A source with a sale or on a closed stage takes part «справочно»: nothing of it moves (its tyres are sold or gone), the new order shows it.
Nothing is sent: no rule wakes, the waiting tasks of the sources are skipped, every record of the merge is grey (no alert in the bell).
«Разъединить» gives it all back while the new order is not sold, not placed after the merge and its positions are as they came."""
from __future__ import annotations

import json
from typing import Dict, List, Optional, Set

from . import orders

REASON = "другое: Объединён в заказ №%d"
UNDONE_REASON = "другое: Разъединён на заказы %s"
AUTHOR_COPY = "[№%s] %s"                  # a record of a source copied into the new order's history
_ACTIVE = "m.undone_at = ''"


class MergeError(orders.OrderError):
    pass


def _label(card: dict) -> str:
    """The number the point knows the order by (order_manager.label): WebMIM's, else the panel's."""
    return str(card.get("webmim_number") or card["id"])


def _ref(card: dict) -> bool:
    """A source «справочно»: sold, or on a closed stage — its tyres are sold, at the supplier or never were."""
    return bool(card["sale_ids"]) or card["stage"]["kind"] in ("won", "closed", "lost") or card["stage"]["code"] == orders.RETURN_GIVEN


def _placed(conn, order_id) -> bool:
    row = conn.execute("SELECT webmim_number, webmim_id, supplier_number FROM order_card WHERE id = ?", (order_id,)).fetchone()
    return bool(row) and any(str(v or "").strip() for v in row)


def into(conn, order_id) -> Optional[int]:
    """The new order an order was merged into (an active merge), or None."""
    row = conn.execute("SELECT m.into_order FROM order_merge_source s JOIN order_merge m ON m.id = s.merge_id WHERE s.order_id = ? AND " + _ACTIVE,
                       (order_id,)).fetchone()
    return row[0] if row else None


def is_ref(conn, order_id) -> bool:
    """The order takes part in an active merge «справочно» (nothing of it moved)."""
    return conn.execute("SELECT 1 FROM order_merge_source s JOIN order_merge m ON m.id = s.merge_id WHERE s.order_id = ? AND s.ref = 1 AND "
                        + _ACTIVE, (order_id,)).fetchone() is not None


def _active(conn, into_order) -> Optional[dict]:
    row = conn.execute("SELECT id, at, author FROM order_merge m WHERE into_order = ? AND " + _ACTIVE, (into_order,)).fetchone()
    return None if row is None else {"id": row[0], "at": row[1], "author": row[2]}


def is_target(conn, order_id) -> bool:
    return _active(conn, order_id) is not None


def sources(conn, into_order) -> List[dict]:
    """The sources of the active merge into `into_order`: [{order_id, ref, label, stage}] in the order they were picked."""
    found = _active(conn, into_order)
    if found is None:
        return []
    out = []
    for order_id, ref in conn.execute("SELECT order_id, ref FROM order_merge_source WHERE merge_id = ? ORDER BY position", (found["id"],)).fetchall():
        try:
            card = orders.get_card(conn, order_id)
        except orders.OrderError:                                       # never: the bin refuses it (why_not_delete)
            continue
        out.append({"order_id": order_id, "ref": bool(ref), "label": _label(card), "stage": card["stage"]["name"], "sold": bool(card["sale_ids"])})
    return out


def items_of(conn, card_id) -> Dict[int, dict]:
    """The positions of the order given by the sources of its active merge (not those through «На ОТХ»: oth_sale shows them): item id →
    {from_order, from_item, qty, number (its source's WebMIM number), label, transfer, placed}."""
    out = {}
    for item_id, from_item, from_order, qty in conn.execute(
            "SELECT i.item_id, i.from_item, i.from_order, i.qty FROM order_merge_item i JOIN order_merge m ON m.id = i.merge_id "
            "WHERE m.into_order = ? AND i.oth = 0 AND " + _ACTIVE, (card_id,)).fetchall():
        row = conn.execute("SELECT webmim_number, webmim_id, transfer_number, supplier_number FROM order_card WHERE id = ?", (from_order,)).fetchone()
        number, webmim_id, transfer, supplier = row if row else ("", "", "", "")
        out[item_id] = {"from_order": from_order, "from_item": from_item, "qty": qty, "number": number or webmim_id or "",
                        "label": str(number or from_order), "transfer": transfer or "", "placed": bool(number or webmim_id or supplier)}
    return out


def placed_ids(conn, card_id) -> Set[int]:
    """The positions of the order that came from a placed source: ordered under its number already, never asked of the supplier again."""
    return {i for i, row in items_of(conn, card_id).items() if row["placed"]}


def unordered(conn, card: dict) -> List[int]:
    """The positions of a merged order on «Заказ оформлен» or later that nobody ordered yet: they came from sources not placed, and the new order
    has no number of its own (the owner 2026-10-30: the card marks them and «Что делать сейчас» asks to order them; ordered once, the order has
    its number). [] for any other order, a deal of the point's stock, or before «Заказ оформлен»."""
    if not is_target(conn, card["id"]) or card.get("deal_kind") == "stock" or _placed(conn, card["id"]):
        return []
    placed = orders._placed_stage(conn)
    if placed is None or not orders._needs_placed(card["stage"], placed):
        return []
    from . import oth_sale
    return [i["id"] for i in oth_sale.supplier_items(conn, card) if int(i["qty"]) >= 1]


def ever(conn, card_id) -> Set[int]:
    """The positions of the order a merge ever gave or took (also undone): their rows of the Google table follow the order (order_sheet)."""
    return {r[0] for r in conn.execute("SELECT from_item FROM order_merge_item WHERE from_order = ? UNION "
                                       "SELECT i.item_id FROM order_merge_item i JOIN order_merge m ON m.id = i.merge_id WHERE m.into_order = ?",
                                       (card_id, card_id))}


def handed(conn) -> Dict[int, int]:
    """New order → the source a point handed over (order_handoff), for the fee of a handed client (the owner 2026-10-30: once, if any source
    was handed). A source with its own sale has had its fee; a new order handed by itself needs none of a source."""
    out = {}
    for new, source in conn.execute(
            "SELECT m.into_order, s.order_id FROM order_merge m JOIN order_merge_source s ON s.merge_id = m.id JOIN order_handoff h ON h.order_id = s.order_id "
            "JOIN order_card c ON c.id = s.order_id WHERE " + _ACTIVE + " AND c.sale_ids = '[]' "
            "AND m.into_order NOT IN (SELECT order_id FROM order_handoff) ORDER BY s.position").fetchall():
        out.setdefault(new, source)
    return out


def why_not_delete(conn, order_id) -> Optional[str]:
    if is_target(conn, order_id):
        return "Это объединённый заказ: сначала разъедините его («Разъединить»), потом удаляйте."
    found = into(conn, order_id)
    if found:
        return "Заказ объединён в заказ №%d: сначала разъедините его, потом удаляйте." % found
    return None


# ---- the merge -----------------------------------------------------------------------------------------------------------------------

def _why_not_source(conn, card: dict) -> Optional[str]:
    from . import oth_sale
    label = _label(card)
    found = into(conn, card["id"])
    if found:
        return "Заказ №%s уже объединён в заказ №%d." % (label, found)
    if is_target(conn, card["id"]):
        return "Заказ №%s уже объединён из других: сначала разъедините его." % label
    if _ref(card):
        return None
    state = conn.execute("SELECT state FROM webmim_create WHERE order_id = ?", (card["id"],)).fetchone()
    if state and state[0] in ("running", "unknown"):
        return "Заказ №%s сейчас оформляется в WebMIM: дождитесь итога." % label
    if state and state[0] not in ("refused", "done"):                   # «partial»: part of it is not in WebMIM yet («Досоздать»)
        return "Заказ №%s оформлен в WebMIM не полностью: сначала досоздайте его." % label
    if card["stage"]["code"] == "oth":
        if conn.execute("SELECT 1 FROM oth_return_plan WHERE order_id = ?", (card["id"],)).fetchone():
            return "По заказу №%s согласован возврат поставщику: объединить его нельзя." % label
        if conn.execute("SELECT 1 FROM oth_return WHERE order_id = ? AND qty > 0", (card["id"],)).fetchone():
            return "Часть шин заказа №%s отмечена возвращённой поставщику: объединить его нельзя." % label
    elif oth_sale.holds(conn, card["id"]):
        return "В заказе №%s шины с ОТХ: такой заказ не объединяется (переведите его на «На ОТХ» или верните шины)." % label
    if oth_sale.gives(conn, card["id"]):
        return "Шины заказа №%s взяты с ОТХ в другой заказ: такой заказ не объединяется." % label
    return None


def _items_in(conn, source: dict, new: int, merge_id: int, now: str, author: str) -> None:
    """The tyres of a source go to the new order (see the module)."""
    oth = source["stage"]["code"] == "oth"
    placed = _placed(conn, source["id"])                                # its tyres are ordered: their purchase is that order's
    for item in source["items"]:
        pieces = int(item["qty"])
        if pieces < 1:
            continue
        if conn.execute("UPDATE order_item SET qty = 0 WHERE id = ? AND order_id = ? AND qty = ?", (item["id"], source["id"], pieces)).rowcount != 1:
            raise MergeError("Позиции заказа №%s только что изменили: ничего не объединено. Обновите страницу." % _label(source))
        cur = conn.execute(
            "INSERT INTO order_item (order_id, goods_id, code, name, brand_key, season, diameter, territory, qty, supplier_price, sale_unit, terms, "
            "fee_pct, promo_pct, purchase_manual) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (new, item["goods_id"], item["code"], item["name"], item["brand_key"], item["season"], item["diameter"], item["territory"], pieces,
             float(item["supplier_price"]), float(item["sale_unit"]), json.dumps(item["terms"] or {}, ensure_ascii=False), float(item["fee_pct"]),
             float(item["promo_pct"]), 1 if oth or placed or item["purchase_manual"] else 0))     # not ordered yet: WebMIM gives its purchase
        conn.execute("INSERT INTO order_merge_item (item_id, merge_id, from_item, from_order, qty, oth) VALUES (?,?,?,?,?,?)",
                     (cur.lastrowid, merge_id, item["id"], source["id"], pieces, 1 if oth else 0))
        if oth:                                                         # «с ОТХ» as a take: the stock, the chain, «Б» of the table
            conn.execute("INSERT INTO order_item_oth (item_id, order_id, from_item, from_order, qty, at, author) VALUES (?,?,?,?,?,?,?)",
                         (cur.lastrowid, new, item["id"], source["id"], pieces, now, author))


def _moved(conn, table: str, column_filter: str, source_id: int, new: int) -> List[int]:
    ids = [r[0] for r in conn.execute("SELECT id FROM %s WHERE order_id = ?%s ORDER BY id" % (table, column_filter), (source_id,))]
    if ids:
        conn.execute("UPDATE %s SET order_id = ? WHERE id IN (%s)" % (table, ",".join("?" * len(ids))), [new] + ids)
    return ids


def _copy_history(conn, source: dict, new: int, logins: Set[str]) -> None:
    """Into the new order's history: the creation of the source, the notes of people and their changes of the fields, its comment and its
    transfer. Grey and of their own kind: no alert, no «Заказ оформлен» read from a copy, no words of a client (orders.client_words)."""
    label = _label(source)
    for at, who, kind, text in conn.execute("SELECT at, author, kind, text FROM order_event WHERE order_id = ? ORDER BY id", (source["id"],)).fetchall():
        if kind in ("created", "note") or (kind == "field" and who in logins):
            orders.add_event(conn, new, at, who, "merge", AUTHOR_COPY % (label, text), level="grey")
    if source["comment"]:
        orders.add_event(conn, new, source["updated_at"] or source["created_at"], source["created_by"], "merge",
                         AUTHOR_COPY % (label, "Комментарий заказа: %s" % source["comment"]), level="grey")
    if source["transfer_number"]:
        orders.add_event(conn, new, source["updated_at"] or source["created_at"], source["created_by"], "merge",
                         AUTHOR_COPY % (label, "Перемещение: %s" % source["transfer_number"]), level="grey")


def _begin(conn) -> None:
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")                                     # the check and the change under one write lock (two tabs, a sale meanwhile)


def _carry_reserve(conn, held: List[dict], new: int, now: str) -> None:
    """The reservation of a source goes on in the new order: the point asked and the client told in that stay are not asked and told again
    today (order_reserve keys them by the order and its arrival at the point; the new order arrived «now»)."""
    import re
    from . import order_reserve
    stay = re.sub(r"\D", "", now)
    for card in held:
        asked = conn.execute("SELECT value FROM app_state WHERE key = ?",
                             (order_reserve.ASK_KEY % (card["id"], re.sub(r"\D", "", card["entered_at"] or "")),)).fetchone()
        if asked:
            conn.execute("INSERT OR REPLACE INTO app_state (key, value) VALUES (?, ?)", (order_reserve.ASK_KEY % (new, stay), asked[0]))
        if order_reserve._done(conn, card["id"], "[резерв:клиенту:%s]" % (card["entered_at"] or "")):
            orders.add_event(conn, new, now, "система", "reserve", "Клиенту уже сообщено о сроке резерва в заказе №%s [резерв:клиенту:%s]" % (
                _label(card), now), level="grey")


def _client_of(conn, order_id) -> Optional[int]:
    row = (conn.execute("SELECT client_id FROM client_order WHERE order_id = ?", (order_id,)).fetchone()
           or conn.execute("SELECT client_id FROM order_oth_client WHERE order_id = ?", (order_id,)).fetchone())     # unlinked on «На ОТХ»
    return row[0] if row else None


def check(conn, ids, stage_id) -> dict:
    """What a merge of `ids` would do: {cards, moving, ref, stage, point_id}; MergeError says why it cannot."""
    ids = list(dict.fromkeys(int(i) for i in ids))
    if len(ids) < 2:
        raise MergeError("Выберите хотя бы два заказа.")
    cards = [orders.get_card(conn, i) for i in ids]
    stage = orders.get_stage(conn, stage_id)
    if stage["kind"] != "open" or stage["code"] in ("oth", orders.RETURN_GIVEN):
        raise MergeError("Новый заказ ставится на открытый этап (не «На ОТХ» и не «Возврат передан»).")
    places = {c["point_id"] for c in cards if c["point_id"]}
    if len(places) > 1:
        raise MergeError("Объединять можно только заказы одной точки.")
    for card in cards:
        why = _why_not_source(conn, card)
        if why:
            raise MergeError(why)
    moving = [c for c in cards if not _ref(c)]
    if not moving:
        raise MergeError("Нечего объединять: все выбранные заказы закрыты или с продажей — их шины не переходят.")
    if len({c["deal_kind"] for c in moving}) > 1:
        raise MergeError("Сделки «Из наличия точки» объединяются только с такими же.")
    return {"cards": cards, "moving": moving, "ref": [c for c in cards if _ref(c)], "stage": stage, "point_id": places.pop() if places else None}


def merge(conn, ids, stage_id, now: str, author: str, client_from=None, client_name: str = "", client_phone: str = "") -> int:
    """The merge (see the module), one transaction. The client: of the order `client_from` (one of `ids`), else the one typed. The date and
    the time of the delivery, the transfer and the main chat: of the chosen client's order (the owner 2026-10-30), else of the first source
    whose tyres move. Returns the new order."""
    _begin(conn)
    try:
        plan = check(conn, ids, stage_id)                               # inside the write lock: nobody changes them between the check and the merge
        cards, moving, stage = plan["cards"], plan["moving"], plan["stage"]
        if client_from not in (None, ""):
            chosen = next((c for c in cards if c["id"] == int(client_from)), None)
            if chosen is None:
                raise MergeError("Клиента выбирают из объединяемых заказов или вписывают нового.")
            name, phone, client = chosen["client_name"], chosen["client_phone"], _client_of(conn, chosen["id"])
            base = chosen if not _ref(chosen) else moving[0]
        else:
            chosen, name, phone, client, base = None, client_name, client_phone, None, moving[0]
        start = conn.execute("SELECT COALESCE(MAX(id), 0) FROM order_event").fetchone()[0]
        logins = {r[0] for r in conn.execute("SELECT login FROM panel_user")}
        fields = {"client_name": name, "point_id": plan["point_id"], "delivery_date": base["delivery_date"], "delivery_time": base["delivery_time"],
                  "transfer_number": base["transfer_number"], "deal_kind": base["deal_kind"]}
        new = orders.create_card(conn, fields, now, author, require_contact=False, commit=False, stage=stage)   # no rule, no message
        merge_id = conn.execute("INSERT INTO order_merge (into_order, at, author) VALUES (?,?,?)", (new, now, author)).lastrowid
        cancel = next((s for s in orders.list_stages(conn) if s["code"] == "cancel"), None)
        if cancel is None:
            raise MergeError("В воронке нет этапа «Отмена»: объединить нельзя.")
        chats_of = {}
        for position, card in enumerate(cards):
            ref = _ref(card)
            saved = {"chats": [], "calls": [], "callbacks": []}
            if not ref:
                _items_in(conn, card, new, merge_id, now, author)
                if not orders.move_card(conn, card["id"], cancel["id"], now, author, changes={"cancel_reason": REASON % new}, fire_rules=False,
                                        journal=False, commit=False):
                    raise MergeError("Заказ №%s только что изменили: ничего не объединено. Обновите страницу." % _label(card))
                conn.execute("UPDATE order_task SET status = 'skipped', result = 'заказ объединён', done_at = ? WHERE order_id = ? "
                             "AND status IN ('pending', 'error')", (now, card["id"]))          # a cancelled order waits for nothing
                saved = {"chats": _moved(conn, "avito_chat", "", card["id"], new), "calls": _moved(conn, "avito_call", "", card["id"], new),
                         "callbacks": _moved(conn, "callback_task", " AND done_at IS NULL AND cancelled_at IS NULL", card["id"], new)}
                chats_of[card["id"]] = saved["chats"]
                orders.add_event(conn, card["id"], now, author, "merge", "Объединён в заказ №%d: шины, чаты и звонки — там." % new, level="grey")
            else:
                orders.add_event(conn, card["id"], now, author, "merge", "Включён в объединённый заказ №%d справочно: шины и продажа остаются здесь." % new,
                                 level="grey")
            conn.execute("INSERT INTO order_merge_source (merge_id, order_id, position, ref, stage_id, entered_at, cancel_reason, reserve_until, chats, "
                         "calls, callbacks) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (merge_id, card["id"], position, 1 if ref else 0, card["stage_id"], card["entered_at"], card["cancel_reason"], card["reserve_until"],
                          json.dumps(saved["chats"]), json.dumps(saved["calls"]), json.dumps(saved["callbacks"])))
            _copy_history(conn, card, new, logins)
        main = (chosen if chosen is not None and chosen["id"] in chats_of else base)["avito_chat"]      # the chats moved with the tyres
        orders.update_card(conn, new, {"client_phone": phone, "avito_chat": main}, now, author, commit=False, link_client=False)
        busy = orders.open_order_conflict(conn, phone, plan["point_id"], besides=new)       # the sources are cancelled already
        if busy is not None:
            raise MergeError("У клиента с телефоном %s уже есть открытый заказ №%d: добавьте его в объединение или выберите другого клиента."
                             % (phone, busy["id"]))
        conn.execute("DELETE FROM client_order WHERE order_id = ?", (new,))
        if client is not None:
            conn.execute("INSERT INTO client_order (order_id, client_id, how, at) VALUES (?, ?, 'manual', ?)", (new, client, now))
        else:
            orders._attach_client(conn, new, now, author)
        if stage["code"] == "at_point":
            ahead = [c["reserve_until"] for c in moving if c["stage"]["code"] == "at_point" and c["reserve_until"] >= now[:10]]
            if ahead:
                conn.execute("UPDATE order_card SET reserve_until = ? WHERE id = ?", (max(ahead), new))
                _carry_reserve(conn, [c for c in moving if c["stage"]["code"] == "at_point" and c["reserve_until"] == max(ahead)], new, now)
            else:
                orders._start_reserve(conn, orders.get_card(conn, new), now, author)
        card = orders.get_card(conn, new)
        placed = orders._placed_stage(conn)
        gaps = orders.missing_for(card, stage, placed)
        if gaps:
            raise MergeError("Чтобы поставить новый заказ на «%s», не хватает: %s." % (stage["name"], ", ".join(gaps)))
        lacking = unordered(conn, card)                                 # placed or later with tyres not ordered: marked, ordered from the card
        if lacking:                                                     # (the owner 2026-10-30), nothing is sent
            orders.add_event(conn, new, now, author, "merge", "Не заказано у поставщика: %d поз. — закажите из карточки («Заказ поставщику»)."
                             % len(lacking), level="grey")
        if orders._needs_placed(stage, placed):
            from . import order_sheet
            order_sheet.enroll(conn, new, now)                          # its rows go to the Google table, the sources' rows stay
        orders.add_event(conn, new, now, author, "merge", "Создан объединением заказов %s. Ничего не отправлено." % ", ".join(
            "№%s%s" % (_label(c), " (справочно)" if _ref(c) else "") for c in cards), level="grey")
        conn.execute("UPDATE order_event SET level = 'grey' WHERE id > ? AND order_id IN (%s)" % ",".join("?" * (len(cards) + 1)),
                     [start, new] + [c["id"] for c in cards])            # «Этап», «Заказ создан», «Резерв»: no alert in the bell
        conn.execute("INSERT INTO audit (at, author, action, detail) VALUES (?,?,?,?)", (now, author, "order_merge", json.dumps(
            {"merge": merge_id, "into": new, "orders": [c["id"] for c in cards], "ref": [c["id"] for c in plan["ref"]]}, ensure_ascii=False)))
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    return new


# ---- «Разъединить» ---------------------------------------------------------------------------------------------------------------------

def why_not_unmerge(conn, into_order) -> Optional[str]:
    found = _active(conn, into_order)
    if found is None:
        return "Этот заказ не объединённый (или уже разъединён)."
    card = orders.get_card(conn, into_order)
    if card["sale_ids"]:
        return "По заказу записана продажа: сначала сторнируйте её, потом разъединяйте."
    if card["stage"]["kind"] in ("won", "closed") or card["stage"]["code"] == orders.RETURN_GIVEN:
        return "Заказ на этапе «%s»: разъединить нельзя." % card["stage"]["name"]
    if card["stage"]["code"] == "oth" or conn.execute(                  # the new client refused: its tyres became our stock, maybe given back
            "SELECT 1 FROM order_event WHERE order_id = ? AND kind = 'field' AND (text LIKE ? OR text = ?) AND at >= ? LIMIT 1",
            (into_order,) + orders.OTH_MARKS + (found["at"],)).fetchone() or conn.execute(
            "SELECT 1 FROM oth_return WHERE order_id = ? AND qty > 0", (into_order,)).fetchone():
        return "Заказ после объединения был на «На ОТХ»: его шины стали складом ОТХ, разъединить нельзя."
    state = conn.execute("SELECT state FROM webmim_create WHERE order_id = ?", (into_order,)).fetchone()
    if _placed(conn, into_order) or (state and state[0] != "refused"):
        return "Заказ уже оформлен в WebMIM или 4 Точках после объединения: разъединить нельзя."
    for item_id, from_item, from_order, qty, oth in conn.execute(
            "SELECT item_id, from_item, from_order, qty, oth FROM order_merge_item WHERE merge_id = ?", (found["id"],)).fetchall():
        row = conn.execute("SELECT qty FROM order_item WHERE id = ? AND order_id = ?", (item_id, into_order)).fetchone()
        given_on = conn.execute("SELECT 1 FROM order_item_oth WHERE from_item = ? AND undone_at = ''", (item_id,)).fetchone()
        link = conn.execute("SELECT 1 FROM order_item_oth WHERE item_id = ? AND undone_at = ''", (item_id,)).fetchone()
        home = conn.execute("SELECT qty FROM order_item WHERE id = ? AND order_id = ?", (from_item, from_order)).fetchone()     # where they go back
        if row is None or int(row[0]) != int(qty) or given_on or (oth and not link) or home is None or int(home[0]) != 0:
            return "Позиции, пришедшие из заказов, изменили (количество, удаление или передача дальше): верните как было, чтобы разъединить."
    cancel = next((s["id"] for s in orders.list_stages(conn) if s["code"] == "cancel"), None)
    for order_id, in conn.execute("SELECT order_id FROM order_merge_source WHERE merge_id = ? AND ref = 0", (found["id"],)).fetchall():
        row = conn.execute("SELECT stage_id, cancel_reason, webmim_number FROM order_card WHERE id = ?", (order_id,)).fetchone()
        if row is None or row[0] != cancel or row[1] != REASON % into_order:
            return "Заказ №%s после объединения сняли с «Отмены» или изменили: разъединить нельзя." % ((row[2] if row else "") or order_id)
    return None


def unmerge(conn, into_order, now: str, author: str) -> List[int]:
    """«Разъединить» (see the module), one transaction. Returns the sources given back. MergeError says why it cannot."""
    _begin(conn)
    try:
        why = why_not_unmerge(conn, into_order)                         # inside the write lock: a sale or a WebMIM order meanwhile counts
        if why:
            raise MergeError(why)
        found = _active(conn, into_order)
        start = conn.execute("SELECT COALESCE(MAX(id), 0) FROM order_event").fetchone()[0]
        cancel = next(s["id"] for s in orders.list_stages(conn) if s["code"] == "cancel")
        stages = {s["id"]: s for s in orders.list_stages(conn)}
        if conn.execute("UPDATE order_merge SET undone_at = ?, undone_by = ? WHERE id = ? AND undone_at = ''", (now, author, found["id"])).rowcount != 1:
            raise MergeError("Заказ только что разъединили в другой вкладке. Обновите страницу.")
        for item_id, from_item, qty, oth in conn.execute("SELECT item_id, from_item, qty, oth FROM order_merge_item WHERE merge_id = ?",
                                                         (found["id"],)).fetchall():
            if conn.execute("UPDATE order_item SET qty = 0 WHERE id = ? AND qty = ?", (item_id, qty)).rowcount != 1:
                raise MergeError("Позиции только что изменили: ничего не разъединено. Обновите страницу.")
            if conn.execute("UPDATE order_item SET qty = ? WHERE id = ? AND qty = 0", (qty, from_item)).rowcount != 1:
                raise MergeError("Позицию исходного заказа только что изменили: ничего не разъединено. Обновите страницу.")
            if oth:
                conn.execute("UPDATE order_item_oth SET undone_at = ? WHERE item_id = ? AND undone_at = ''", (now, item_id))
        back, labels = [], []
        for order_id, ref, stage_id, entered_at, reason, reserve, chats, calls, callbacks in conn.execute(
                "SELECT order_id, ref, stage_id, entered_at, cancel_reason, reserve_until, chats, calls, callbacks FROM order_merge_source "
                "WHERE merge_id = ? ORDER BY position", (found["id"],)).fetchall():
            card = orders.get_card(conn, order_id)
            labels.append("№%s" % _label(card))
            if ref:
                orders.add_event(conn, order_id, now, author, "merge", "Разъединено: заказ больше не входит в заказ №%d." % into_order, level="grey")
                continue
            if conn.execute("UPDATE order_card SET stage_id = ?, entered_at = ?, cancel_reason = ?, reserve_until = ?, updated_at = ? "
                            "WHERE id = ? AND stage_id = ?", (stage_id, entered_at, reason, reserve, now, order_id, cancel)).rowcount != 1:
                raise MergeError("Заказ №%s только что изменили: ничего не разъединено. Обновите страницу." % _label(card))
            for table, ids in (("avito_chat", chats), ("avito_call", calls), ("callback_task", callbacks)):
                ids = json.loads(ids or "[]")
                if ids:
                    conn.execute("UPDATE %s SET order_id = ? WHERE order_id = ? AND id IN (%s)" % (table, ",".join("?" * len(ids))),
                                 [order_id, into_order] + ids)
            orders.add_event(conn, order_id, now, author, "merge", "Разъединено: заказ снова на этапе «%s», шины вернулись из заказа №%d." % (
                stages.get(stage_id, {}).get("name", ""), into_order), level="grey")
            back.append(order_id)
        left = conn.execute("SELECT COUNT(*) FROM order_item WHERE order_id = ? AND qty >= 1", (into_order,)).fetchone()[0]
        card = orders.get_card(conn, into_order)
        if not left and card["stage"]["kind"] == "open":                # only what came: the new order is closed; with its own positions it stays
            if not orders.move_card(conn, into_order, cancel, now, author, changes={"cancel_reason": (UNDONE_REASON % ", ".join(labels))[:300]},
                                    fire_rules=False, journal=False, commit=False):
                raise MergeError("Заказ только что изменили: ничего не разъединено. Обновите страницу.")
        orders.add_event(conn, into_order, now, author, "merge", "Разъединён на заказы %s. Ничего не отправлено." % ", ".join(labels), level="grey")
        mine = [int(into_order)] + [r[0] for r in conn.execute("SELECT order_id FROM order_merge_source WHERE merge_id = ?", (found["id"],))]
        conn.execute("UPDATE order_event SET level = 'grey' WHERE id > ? AND order_id IN (%s)" % ",".join("?" * len(mine)), [start] + mine)
        conn.execute("INSERT INTO audit (at, author, action, detail) VALUES (?,?,?,?)", (now, author, "order_unmerge", json.dumps(
            {"merge": found["id"], "into": int(into_order), "orders": back}, ensure_ascii=False)))
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    return back
