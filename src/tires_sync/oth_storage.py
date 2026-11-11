"""«Запись хранения» (the plan agreed by the owner 2026-11-10; it replaces «the same order gets a new client» of 2026-11-06, oth_swap, for
the moves to «На ОТХ» made from now on).

A client's order A that goes to «На ОТХ» (any way: orders.move_card) is cancelled at once: «Отмена» with the reason «отказ клиента», its
WebMIM number gets the letter «О» («123О»), its transfer number is cleared, the client stays linked (it is their cancelled order) and is
written nothing (no rule of «Отмена» fires). In the same transaction the tyres become a record of storage Х: an order without a client on
«На ОТХ» with the point, the positions (purchase, sale price, terms), the WebMIM number and id, the transfer number, the dates of the
arrival and of «На ОТХ» (the records order_oth.reserve_dates reads) and the responsible of A. Everything that works from an order standing
on «На ОТХ» (the feed, the page «На ОТХ», the seventh day, the return to the supplier, the point's «Рабочее место», the Google table) works
from Х as it did from the order.

A new client takes a record whole only (take: «С ОТХ берётся вся запись: N шт.»), by the usual «Взять с ОТХ» (oth_sale: the positions
«с ОТХ», the numbers, the purchase of Х, Х closed «Шины переданы в заказ №B»); the sale price is the one of Х, or the price of the ad of the
order's account when that is higher. The same client back: A leaves «Отмена» for «Товар на точке» (orders.move_card → restore), its numbers
come back, Х is closed «Возвращено в заказ №A»; refused once Х is taken, split or on its way back to the supplier.

The orders that stood on «На ОТХ» / «На возврат» before live on as they were: nothing is migrated, a record is only made by a move.
No table of its own: app_state keeps Х → its record (MARK) and A → its live Х (OF); the journal of actions keeps the split.
The split is always on (the owner 2026-11-10: no switch in the base); SPLIT is for the tests of the orders put on «На ОТХ» the old way only."""
from __future__ import annotations

import json
import re
from typing import Dict, List, Optional

from . import orders

SPLIT = True                        # never changed in production; the tests of the orders on «На ОТХ» the old way set False (tests/conftest.py)
MARK = "oth_storage:%d"             # Х → {"order": A, "at", "by", "items": {A's item: Х's item}, "pieces", "was": {...}, "state", "to"}
OF = "oth_stored:%d"                # A → Х, while A stands cancelled by the split (dropped when A gets the tyres back)
REFUSED = "отказ клиента"           # orders.CANCEL_KINDS
LETTER = " О"                       # a space and the Cyrillic capital (the owner 2026-11-11): «5367654 О», «26R-3568954 О» are the numbers of
                                    # the cancelled order of the client; «123О» typed by hand earlier reads the same (refused_number)
BACK_REASON = "другое: Возвращено в заказ №%d"
HELD, TAKEN, BACK = "held", "taken", "back"
REOPEN = "reopen"                   # only inside the transaction of untake: the record taken back stands on «На ОТХ» again (why_not_move)
FROM_OPEN = ("oth", orders.RETURN_GIVEN, "done")    # a record moved to «На ОТХ» from these is not closed («Отменить» of «Продать» is from «Выполнено»)
_MARKED = re.compile(r"\d\s*[ОоOo]$")       # typed by hand too: a Latin «O», a small letter


def refused_number(number) -> bool:
    """«123 О» (or «123О»): a WebMIM or transfer number of an order cancelled by the split — never the number «123» of the order that holds
    the tyres."""
    return bool(_MARKED.search(str(number or "").strip()))


def with_letter(number) -> str:
    text = str(number or "").strip()
    return text if not text or refused_number(text) else text + LETTER


def without_letter(number) -> str:
    text = str(number or "").strip()
    return text[:-1].strip() if refused_number(text) else text


def _state(conn, key: str):
    row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    try:
        return json.loads(row[0]) if row else None
    except ValueError:
        return None


def _put(conn, key: str, value) -> None:
    conn.execute("INSERT INTO app_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, json.dumps(value, ensure_ascii=False)))


def _audit(conn, now: str, author: str, action: str, detail: dict) -> None:
    conn.execute("INSERT INTO audit (at, author, action, detail) VALUES (?,?,?,?)", (now, author, action, json.dumps(detail, ensure_ascii=False)))


def record(conn, card_id) -> Optional[dict]:
    """The record of storage `card_id` is: its mark; None for any other order."""
    found = _state(conn, MARK % int(card_id))
    return found if isinstance(found, dict) else None


def is_record(conn, card_id) -> bool:
    return record(conn, card_id) is not None


def record_ids(conn) -> set:
    return {int(k.rsplit(":", 1)[1]) for (k,) in conn.execute("SELECT key FROM app_state WHERE key LIKE 'oth_storage:%'")}


def record_of(conn, order_id) -> Optional[int]:
    """The record the tyres of the cancelled order A went to (alive or not); None: the order was not cancelled by the split, or has them back."""
    found = _state(conn, OF % int(order_id))
    return int(found) if isinstance(found, int) else None


def source_of(conn, card_id) -> Optional[int]:
    """The client's order a record of storage was made from."""
    mark = record(conn, card_id)
    return int(mark["order"]) if mark else None


def why_not_move(conn, card: dict, stage: dict) -> Optional[str]:
    """Why the move of `card` to `stage` would put the same tyres on «На ОТХ» twice (the review 2026-11-10); None: it would not.
    Б1: the order cancelled by the split never goes to «На ОТХ» or «На возврат» again, its tyres lie (or lay) in its record.
    Б2: a closed record (given back, taken) never goes back there, but by «Вернуть на ОТХ» of the order that took it; a record the office
    cancelled by hand by mistake goes back on «На ОТХ» (cancelled_by_mistake, the owner 2026-11-10)."""
    if stage["code"] not in ("oth", orders.RETURN_GIVEN):
        return None
    x = record_of(conn, card["id"])
    if x is not None:
        return ("Заказ отменён переводом на ОТХ: его шины в записи хранения №%d. На «%s» он не переводится, шины уже там или их взял другой "
                "заказ. Тот же клиент: «Вернуть заказ клиенту»." % (x, stage["name"]))
    mark = record(conn, card["id"])
    if mark is not None and stage["code"] == "oth" and cancelled_by_mistake(conn, card):
        return None                                                     # the owner 2026-11-10: the office's wrong «Отмена» is undone
    if mark is not None and card["stage"].get("code") not in FROM_OPEN and mark.get("state") != REOPEN:
        return ("Запись хранения №%d закрыта (этап «%s»): на «%s» она не возвращается. Шины снова на ОТХ только через «Вернуть на ОТХ» "
                "в заказе, который их взял." % (card["id"], card["stage"].get("name") or "", stage["name"]))
    return None


def cancelled_by_mistake(conn, card: dict) -> bool:
    """A record on «Отмена» the office put there by hand (the owner 2026-11-10: it may go back on «На ОТХ»): never taken by an order nor given
    back to its client (the mark still holds the tyres), every tyre of the split still in it, no sale. Not a record closed by the take or the
    give-back: those go back only by «Вернуть на ОТХ» of the order that took it, or never."""
    mark = record(conn, card["id"])
    if mark is None or card["stage"].get("code") != "cancel" or mark.get("state") != HELD or mark.get("to") or card["sale_ids"]:
        return False
    return sum(int(i["qty"]) for i in card["items"]) == int(mark.get("pieces") or 0)


def reopen(conn, card: dict, now: str, author: str) -> None:
    """The record cancelled by mistake stands on «На ОТХ» again (orders.move_card, in its transaction): no new split, the same tyres, the
    record still holds them. No commit."""
    conn.execute("UPDATE order_card SET cancel_reason = '' WHERE id = ?", (card["id"],))
    orders.add_event(conn, card["id"], now, author, "field", "Запись хранения снова на ОТХ: отмена была ошибкой (её шины не брали и клиенту не возвращали)")
    _audit(conn, now, author, "oth_storage_reopen", {"record": int(card["id"]), "order": (record(conn, card["id"]) or {}).get("order")})


def splits(conn, card: dict) -> bool:
    """A move of this order to «На ОТХ» makes a record of storage: a client's order (a name, a phone, a chat or a linked client). Not a record itself, nor an
    order nobody ordered (split off by a return: order_oth._split_off). A merged order and an order with pieces added
    under a second WebMIM number keep their numbers by position (order_merge, order_webmim): they go the old way."""
    if not SPLIT or is_record(conn, card["id"]):
        return False
    if conn.execute("SELECT 1 FROM order_oth_client WHERE order_id = ?", (card["id"],)).fetchone():
        return False                                                    # stood on «На ОТХ» the old way, its client put aside: our stock already
    if not (str(card.get("client_name") or "").strip() or str(card.get("client_phone") or "").strip() or str(card.get("avito_chat") or "").strip()
            or conn.execute("SELECT 1 FROM client_order WHERE order_id = ?", (card["id"],)).fetchone()):
        return False
    from . import order_merge
    if order_merge.is_target(conn, card["id"]) or conn.execute("SELECT 1 FROM order_mim_extra WHERE order_id = ? LIMIT 1", (card["id"],)).fetchone():
        return False
    return True


def _columns(conn, table: str) -> List[str]:
    return [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)]


def _reason(text) -> str:
    """«Причина отмены» of the order cancelled by the split: «отказ клиента», what was written (the point's reason, the choice of the
    window) after it."""
    detail = " ".join(str(text or "").split())
    if detail == REFUSED or detail.startswith(REFUSED + ":"):         # chosen so in the window; «Отказ клиента на точке: …» is a detail
        return detail[:300]
    return (REFUSED + (": " + detail if detail else ""))[:300]


def split(conn, card: dict, oth_stage: dict, now: str, author: str) -> dict:
    """The client's order `card` goes to «На ОТХ»: the record of storage is made and takes its tyres and numbers; the order itself gets the
    reason and the letter. No commit and no move: orders.move_card moves the order to the stage returned («Отмена») in the same
    transaction. {"record": Х, "stage": «Отмена»}; OrderError says why not."""
    cancel = next((s for s in orders.list_stages(conn) if s["code"] == "cancel"), None)
    if cancel is None:
        raise orders.OrderError("В воронке нет этапа «Отмена»: заказ не перевести на «%s»." % oth_stage["name"])
    a = int(card["id"])
    row = orders._row(conn, a)
    x = orders.create_card(conn, {"point_id": card["point_id"], "responsible": card.get("responsible") or "", "deal_kind": card.get("deal_kind") or "webmim",
                                  "comment": "Запись хранения: шины заказа №%d, клиент отказался" % a},
                           now, author, require_contact=False, commit=False, stage=oth_stage)
    columns = [c for c in _columns(conn, "order_item") if c not in ("id", "order_id")]
    items: Dict[str, int] = {}
    pieces = 0
    for item in card["items"]:
        if int(item["qty"]) < 1:
            continue
        cur = conn.execute("INSERT INTO order_item (order_id, %s) SELECT ?, %s FROM order_item WHERE id = ?" % (", ".join(columns), ", ".join(columns)),
                           (x, item["id"]))
        items[str(item["id"])] = cur.lastrowid
        pieces += int(item["qty"])
        # tyres the order took from «На ОТХ» before (a chain): they are the record's now, with the order they were bought by
        conn.execute("UPDATE order_item_oth SET item_id = ?, order_id = ? WHERE item_id = ?", (cur.lastrowid, x, item["id"]))
        conn.execute("UPDATE order_item_mim SET item_id = ?, order_id = ? WHERE item_id = ?", (cur.lastrowid, x, item["id"]))     # its numbers too
    conn.execute("INSERT OR IGNORE INTO order_intercity (order_id, enabled, city, carrier, track, updated_at, updated_by) "
                 "SELECT ?, enabled, city, carrier, track, ?, ? FROM order_intercity WHERE order_id = ?", (x, now, author, a))     # «Доставка в другой город»
    conn.execute("UPDATE order_card SET webmim_id = ?, webmim_number = ?, webmim_status = ?, transfer_number = ?, updated_at = ? WHERE id = ?",
                 (row["webmim_id"], without_letter(row["webmim_number"]), row["webmim_status"], row["transfer_number"], now, x))
    conn.execute("INSERT OR IGNORE INTO webmim_create (order_id, server, state, first_at, started_at, author, webmim_id, result, updated_at) "
                 "SELECT ?, server, state, first_at, started_at, author, webmim_id, result, updated_at FROM webmim_create WHERE order_id = ? AND state = 'done'",
                 (x, a))                                               # the order of WebMIM the button made is the record's: its server, its day
    conn.execute("UPDATE order_card SET webmim_id = '', webmim_number = ?, transfer_number = ?, cancel_reason = ?, updated_at = ? WHERE id = ?",
                 (with_letter(row["webmim_number"]), with_letter(row["transfer_number"]), _reason(card.get("cancel_reason")), now, a))   # both stay with « О» (2026-11-11)
    edited = orders.LABELS["reserve_until"] + ": "
    for at, who, text, level in conn.execute("SELECT at, author, text, level FROM order_event WHERE order_id = ? AND kind = 'field' "
                                             "AND (text LIKE 'Резерв%' OR text LIKE ?) ORDER BY id", (a, edited + "%")).fetchall():
        orders.add_event(conn, x, at, who, "field", text, level or "")    # the arrival at the point and the reservation: order_oth.reserve_dates
    if row["reserve_until"]:
        orders.add_event(conn, x, now, author, "field", orders.RESERVE_WAS % row["reserve_until"])
    orders.add_event(conn, x, now, author, "field", orders.OTH_ENTERED)
    orders.add_event(conn, x, now, author, "field", "Из заказа №%d (клиент отказался)" % a)
    orders.add_event(conn, a, now, author, "field", "Шины переданы на ОТХ, запись №%d" % x)
    _put(conn, MARK % x, {"order": a, "at": now, "by": author, "items": items, "pieces": pieces, "state": HELD, "to": None})
    _put(conn, OF % a, x)
    _audit(conn, now, author, "oth_storage_split", {"order": a, "record": x, "webmim_number": row["webmim_number"], "webmim_id": row["webmim_id"],
                                                    "transfer_number": row["transfer_number"], "items": items})
    from . import order_sheet
    order_sheet.enroll(conn, x, now)
    return {"record": x, "stage": cancel}


# ---- the same client is back ---------------------------------------------------------------------------------------------------------

def why_not_back(conn, a_id) -> Optional[str]:
    """Why the cancelled order cannot have its tyres back from its record; None: it can."""
    x = record_of(conn, a_id)
    if x is None:
        return "Заказ не отменён переводом на ОТХ."
    mark = record(conn, x) or {}
    try:
        card = orders.get_card(conn, x)
    except orders.OrderError:
        return "Запись хранения №%d удалена: шины этому заказу не вернуть. Создайте новый заказ." % x
    code = card["stage"]["code"]
    if mark.get("state") == TAKEN or (code == "cancel" and mark.get("to")):
        return "Шины записи №%d уже взял заказ №%s: этому заказу их не вернуть. Создайте новый заказ." % (x, mark.get("to"))
    if code != "oth":
        return "Запись хранения №%d уже на этапе «%s» (шины уходят поставщику или ушли): заказ клиенту не вернуть. Создайте новый заказ." % (
            x, card["stage"]["name"])
    if conn.execute("SELECT 1 FROM oth_return_plan WHERE order_id = ?", (x,)).fetchone():
        return "По записи хранения №%d согласован возврат поставщику: заказ клиенту не вернуть." % x
    if conn.execute("SELECT 1 FROM oth_return WHERE order_id = ? AND qty > 0", (x,)).fetchone():
        return "Часть шин записи №%d отмечена «возвращено поставщику» или «На возврат»: заказ клиенту не вернуть." % x
    if sum(int(i["qty"]) for i in card["items"]) != int(mark.get("pieces") or 0):
        return "В записи хранения №%d уже не все шины заказа: заказ клиенту не вернуть. Создайте новый заказ." % x
    if card["sale_ids"]:
        return "По записи хранения №%d записана продажа." % x
    return None


def restore(conn, a_id, now: str, author: str) -> int:
    """The cancelled order A gets its tyres back (orders.move_card, on its way to «Товар на точке»): the numbers of the record are A's again,
    the record is closed «Возвращено в заказ №A». No commit. Returns the record; OrderError says why not."""
    why = why_not_back(conn, a_id)
    if why:
        raise orders.OrderError(why)
    x = record_of(conn, a_id)
    mark = record(conn, x)
    row = orders._row(conn, x)
    a = orders._row(conn, a_id)
    busy = orders.open_order_conflict(conn, a["client_phone"], a["point_id"], besides=int(a_id)) if a["client_phone"] else None
    if busy is not None:                                                # one open order per phone and point (orders.create_card)
        raise orders.OrderError("У клиента с телефоном %s уже есть открытый заказ №%d: этот заказ не вернуть, работайте в том." % (
            a["client_phone"], busy["id"]))
    number = row["webmim_number"] or without_letter(a["webmim_number"])
    transfer = row["transfer_number"] or without_letter(a["transfer_number"])
    conn.execute("UPDATE order_card SET webmim_id = ?, webmim_number = ?, webmim_status = ?, transfer_number = ?, cancel_reason = ?, updated_at = ? WHERE id = ?",
                 (row["webmim_id"], number, row["webmim_status"] or a["webmim_status"], transfer, "", now, a_id))
    conn.execute("UPDATE order_card SET webmim_id = '', webmim_number = '', transfer_number = '', updated_at = ? WHERE id = ?", (now, x))
    for old, new in (mark.get("items") or {}).items():                 # the tyres taken from «На ОТХ» before the split: the order's again
        conn.execute("UPDATE order_item_oth SET item_id = ?, order_id = ? WHERE item_id = ?", (int(old), a_id, int(new)))
        conn.execute("UPDATE order_item_mim SET item_id = ?, order_id = ? WHERE item_id = ?", (int(old), a_id, int(new)))
    cancel = next(s for s in orders.list_stages(conn) if s["code"] == "cancel")
    if not orders.move_card(conn, x, cancel["id"], now, author, changes={"cancel_reason": BACK_REASON % a_id}, fire_rules=False, journal=False,
                            commit=False):
        raise orders.OrderError("Запись хранения №%d только что изменили: обновите страницу." % x)
    orders.add_event(conn, x, now, author, "field", "Возвращено в заказ №%d: тот же клиент забирает шины" % a_id)
    orders.add_event(conn, a_id, now, author, "field", "Заказ возвращён клиенту: шины сняты с ОТХ (запись №%d), номер WebMIM %s" % (x, number or "не указан"))
    conn.execute("DELETE FROM app_state WHERE key = ?", (OF % int(a_id),))
    _put(conn, MARK % x, dict(mark, state=BACK))
    _audit(conn, now, author, "oth_storage_back", {"order": int(a_id), "record": x})
    return x


def waits(conn, card: dict) -> bool:
    """The order stands cancelled by the split and its tyres still lie in its record on «На ОТХ»: the client may still be called back."""
    return card["stage"]["code"] == "cancel" and record_of(conn, card["id"]) is not None and why_not_back(conn, card["id"]) is None


# ---- a new client takes the record ---------------------------------------------------------------------------------------------------

def _stock(conn, x_id) -> List[dict]:
    from . import oth_stock
    return [r for r in oth_stock._rows(conn, manual=False) if r["order_id"] == int(x_id)]


def whole_text(rows: List[dict]) -> str:
    return "С ОТХ берётся вся запись: %d шт." % sum(int(r["qty"]) for r in rows)


def why_not_whole(conn, card: dict, x_id, lines: Optional[List[dict]] = None) -> Optional[str]:
    """Why the order cannot take the record whole: a position of the record the order has no supplier's position for, or fewer pieces
    wanted than lie there. None: every tyre of the record goes."""
    from . import oth_sale
    from .order_merge import placed_ids
    rows = _stock(conn, x_id)
    if not rows:
        return "Этих шин на ОТХ уже нет: обновите страницу."
    if lines is None:
        mine = oth_sale.taken_ids(conn, card["id"]) | placed_ids(conn, card["id"])
        lines = [i for i in card["items"] if i["id"] not in mine and int(i["qty"]) >= 1]
    wanted: Dict[str, int] = {}
    for line in lines:
        wanted[str(line["goods_id"])] = max(wanted.get(str(line["goods_id"]), 0), int(line["qty"]))
    lacking = [r for r in rows if wanted.get(str(r["goods_id"]), 0) < int(r["qty"])]
    if lacking:
        return "%s В заказе должны быть все её позиции целиком: %s." % (whole_text(rows), "; ".join("%s × %d" % (r["name"], int(r["qty"])) for r in rows))
    return None


def _feed_price(conn, card_id, goods_id) -> Optional[float]:
    """The price of one tyre in the published ad of the order's account (order_offers.ad_prices); None: the position is not in its feed."""
    from . import order_offers
    try:
        found = order_offers.ad_prices(conn, order_offers.account_for(conn, card_id=card_id), [goods_id])
    except Exception:                                                   # noqa: BLE001 - no account, no feed yet: the price of the record stands
        return None
    price = found.get(str(goods_id))
    return float(price) if price else None


def _day(iso: str) -> str:
    return "%s.%s.%s" % (iso[8:10], iso[5:7], iso[:4]) if len(iso) >= 10 else "неизвестно"


def take(conn, card_id, from_item, now: str, author: str) -> int:
    """«Взять с ОТХ» of a record of storage: every tyre of it or none, in one transaction (oth_sale._take_order per position: the purchase
    of the record, «с ОТХ», the numbers; the record emptied goes to «Отмена» «Шины переданы в заказ №B»). The sale price of each position is
    the record's, or the ad's of the order's account when that is higher. Returns the pieces taken; OthSaleError says why nothing was.
    Its own transaction, never inside the caller's: an open one is a RuntimeError and nothing of it is committed (the callers,
    oth_sale.take and to_new_client, have none open)."""
    from . import oth_sale, order_oth
    if conn.in_transaction:
        raise RuntimeError("oth_storage.take: открыта транзакция вызывающего, взятие записи хранения идёт своей транзакцией")
    conn.execute("BEGIN IMMEDIATE")
    try:
        source = next((r for r in oth_stock_rows(conn) if r["item_id"] == int(from_item)), None)
        if source is None:
            raise oth_sale.OthSaleError("Этих шин на ОТХ уже нет: их взяли, вернули поставщику или запись ушла с ОТХ. Обновите страницу.")
        x = int(source["order_id"])
        card = orders.get_card(conn, card_id)
        blocked = oth_sale.why_not(conn, card)
        if blocked:
            raise oth_sale.OthSaleError(blocked)
        why = why_not_whole(conn, card, x)
        if why:
            raise oth_sale.OthSaleError(why)
        record_card = orders.get_card(conn, x)
        sale = {i["id"]: float(i["sale_unit"]) for i in record_card["items"]}
        dates = order_oth.reserve_dates(conn).get(x) or {}
        mark = record(conn, x) or {}
        taken = 0
        for r in _stock(conn, x):
            card = orders.get_card(conn, card_id)
            mine = oth_sale.taken_ids(conn, card_id)
            line = next((i for i in card["items"] if i["id"] not in mine and str(i["goods_id"]) == str(r["goods_id"]) and int(i["qty"]) >= int(r["qty"])), None)
            if line is None:                                            # two positions of one tyre in the record, one line in the order
                raise oth_sale.OthSaleError(why_not_whole(conn, card, x) or whole_text(_stock(conn, x)))
            before = set(oth_sale.taken_ids(conn, card_id))
            taken += oth_sale._take_order(conn, card_id, line["id"], r["item_id"], now, author, commit=False)
            new_item = next(iter(set(oth_sale.taken_ids(conn, card_id)) - before))
            was, ad = sale[r["item_id"]], _feed_price(conn, card_id, r["goods_id"])
            price = ad if ad is not None and ad > was else was
            conn.execute("UPDATE order_item SET sale_unit = ? WHERE id = ?", (price, new_item))
            orders.add_event(conn, card_id, now, author, "item", "Цена продажи шин с ОТХ: %s ₽ — %s" % (
                _money(price), "цена объявления аккаунта выше прежней %s ₽" % _money(was) if price > was else "прежняя, как в записи №%d" % x))
        orders.add_event(conn, card_id, now, author, "field", "Шины записи хранения №%d (из заказа №%s): на точке с %s, на ОТХ с %s" % (
            x, mark.get("order"), _day(dates.get("arrived") or ""), _day(dates.get("oth_since") or "")))
        if mark.get("order"):
            orders.add_event(conn, int(mark["order"]), now, author, "field", "Шины с ОТХ (запись №%d) взял заказ №%d" % (x, card_id))
        if orders.get_card(conn, x)["stage"]["code"] == "cancel":
            _put(conn, MARK % x, dict(mark, state=TAKEN, to=int(card_id)))
        _audit(conn, now, author, "oth_storage_take", {"order": int(card_id), "record": x, "pieces": taken})
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    return taken


def oth_stock_rows(conn) -> List[dict]:
    from . import oth_stock
    return oth_stock._rows(conn, manual=False)


def _money(value) -> str:
    return ("%.2f" % float(value)).rstrip("0").rstrip(".").replace(".", ",")


def untake(conn, card_id, x_id, now: str, author: str) -> int:
    """«Вернуть на ОТХ» of a position taken from the record `x_id`: every position the order took from it goes back, in one transaction
    (oth_sale._untake_order per position), and the record stands on «На ОТХ» again, held (the review 2026-11-10: a record is never split by
    a give-back). Its own transaction, as take. Returns the pieces given back; OthSaleError says why nothing was."""
    from . import oth_sale
    if conn.in_transaction:
        raise RuntimeError("oth_storage.untake: открыта транзакция вызывающего, возврат записи хранения идёт своей транзакцией")
    conn.execute("BEGIN IMMEDIATE")
    try:
        mark = record(conn, x_id) or {}
        mine = sorted(i for i, link in oth_sale.taken(conn, card_id).items() if link["from_order"] == int(x_id))
        if mark.get("state") == TAKEN:
            _put(conn, MARK % int(x_id), dict(mark, state=REOPEN))  # why_not_move: the one way back of a closed record
        pieces = sum(oth_sale._untake_order(conn, card_id, item, now, author, commit=False) for item in mine)
        if mark:
            _put(conn, MARK % int(x_id), dict(mark, state=HELD, to=None))
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    return pieces
