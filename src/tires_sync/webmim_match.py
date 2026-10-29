"""The orders of WebMIM next to the orders of the panel: a snapshot of the supplier's orders (GET only, nothing is sent back) and their pairs.
An order of the panel is paired by its «Номер/ID заказа WebMIM»; without them the panel only suggests (the same phone, the same positions, a near
date) and a person confirms with «Это он» (link). Nothing is written to an order of the panel without that button."""
from __future__ import annotations

import json
import re
import time
from datetime import date, datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple

from . import connections, orders, webmim_orders
from .secretbox import SecretKeyError

DAYS = 60                   # how far back the snapshot reads (by the date WebMIM created the order)
PER_PAGE = 50
MAX_PAGES = 40              # 2000 orders a run at most
MAX_ITEMS = 100             # positions of so many orders a run at most; the rest the next hour
PAUSE = 0.3                 # seconds between two requests to WebMIM
HINT_DAYS = 7               # a suggested WebMIM order was made within so many days of the panel's order
HINT_MIN = 40               # the lowest score shown
HINTS = 3
SKIPPED = (200, 400, 404, 410, 422)      # the positions of this order only are refused (gone, not parsed): asked again once the order changes
FIELDS = ("server", "id", "number", "status_code", "status_text", "create_date", "external_number", "customer_name", "customer_phone_norm",
          "sum", "items", "fingerprint", "seen_at", "items_at")


def current_server(conn) -> str:
    """test / prod: the server chosen in «Подключения → WebMIM → Заказы в WebMIM»."""
    server = connections.get_config(conn, "webmim_orders", connections.supplier_owner(1)).get("server")
    return server if server in webmim_orders.SERVERS else webmim_orders.DEFAULT_SERVER


def client_for(conn, box, http, server: Optional[str] = None) -> Optional[webmim_orders.OrdersClient]:
    """The orders API client of the first supplier, as the access check makes it; None when the API user is not set up.
    A saved «apikey» of the earlier versions reads as «login». `server`: the one the caller has checked (else the chosen one)."""
    owner = connections.supplier_owner(1)
    config = connections.get_config(conn, "webmim_orders", owner)
    server = server if server in webmim_orders.SERVERS else current_server(conn)
    login = str(config.get("login") or "")
    password = connections.get_secret(conn, box, "webmim_orders", owner, "password") or ""
    if not login or not password:
        return None
    return webmim_orders.OrdersClient(webmim_orders.SERVERS[server], login, password, http=http)


# ---- the snapshot --------------------------------------------------------------------------------------------------------------------

def parse_date(text) -> str:
    """«25.03.22 13:41:17» (WebMIM) → «2022-03-25T13:41:17»; '' when it is not that."""
    for pattern in ("%d.%m.%y %H:%M:%S", "%d.%m.%y %H:%M", "%d.%m.%y", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y"):
        try:
            return datetime.strptime(str(text or "").strip(), pattern).isoformat(timespec="seconds")
        except ValueError:
            continue
    return ""


def phone_tail(phone) -> str:
    digits = re.sub(r"\D", "", str(phone or ""))
    return digits[-10:] if len(digits) >= 10 else ""


def digits(text) -> str:
    return re.sub(r"\D", "", str(text or ""))


def _text(value, limit: int) -> str:
    return "" if value is None else str(value).strip()[:limit]


def _order(row) -> Optional[dict]:
    """The fields kept of one order of GET /api/v2/orders; None for a row without a usable id."""
    if not isinstance(row, dict):
        return None
    order_id = _text(row.get("id"), 30)
    if not order_id.isascii() or not order_id.isdigit() or len(order_id) > 20:      # the limit of webmim_orders.OrdersClient.items
        return None
    out = {"id": order_id, "number": _text(row.get("number"), 30), "status_code": _text(row.get("statusCode"), 10),
           "status_text": _text(row.get("statusDescription"), 80), "create_date": parse_date(row.get("createDate")),
           "external_number": _text(row.get("externalNumber"), 40), "customer_name": _text(row.get("customerName"), 160),
           "customer_phone_norm": phone_tail(row.get("customerPhone"))}
    out["fingerprint"] = json.dumps([out[k] for k in sorted(out)], ensure_ascii=False)
    return out


def _number(value) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _items(rows: list) -> Tuple[List[dict], Optional[float]]:
    """(positions {goods_id, name, qty, price, sum}, their sum) of GET /api/v2/orders/{id}/items."""
    out, total, priced = [], 0.0, False
    for row in rows:
        goods = _text(row.get("goodsId"), 30)
        if not goods:
            continue
        qty, price, line = _number(row.get("quantity")), _number(row.get("price")), _number(row.get("sum"))
        if line is None and qty is not None and price is not None:
            line = qty * price
        out.append({"goods_id": goods, "name": _text(row.get("goodsName"), 200), "qty": qty, "price": price, "sum": line})
        total += line or 0.0
        priced = priced or line is not None
    return out, (round(total, 2) if priced or not out else None)


def _newest(rows) -> str:
    return max((parse_date(r.get("createDate")) for r in rows if isinstance(r, dict)), default="")


def _every_page(client, sleep: Callable[[float], None], counts: dict):
    """Every page of GET /api/v2/orders up to its count of pages (at most MAX_PAGES); the window by the date is applied by the caller, row by row:
    the order of the rows is not described and on the live server is not by the creation date (2026-10-28: a stop at the first old order read
    2 pages of many). Above MAX_PAGES the newest pages are read: the newest date of the first page against the last tells which end that is.
    Without the count of pages: forward until a short page. A PAUSE before every request after the first; an empty page ends the reading."""
    first, pages = client.orders_page(1, PER_PAGE)
    counts["pages_total"] = pages
    yield first
    if not pages:
        page, rows = 1, first
        while len(rows) >= PER_PAGE and page < MAX_PAGES:
            page += 1
            sleep(PAUSE)
            rows, _ = client.orders_page(page, PER_PAGE)
            yield rows
        return
    if not first:
        return
    order = list(range(2, min(pages, MAX_PAGES) + 1))
    if pages > MAX_PAGES:
        sleep(PAUSE)
        last, _ = client.orders_page(pages, PER_PAGE)
        yield last
        if _newest(last) > _newest(first):                       # the newest at the end: backwards from the page before the last
            order = list(range(pages - 1, pages - MAX_PAGES + 1, -1))
        else:
            order = list(range(2, MAX_PAGES))
    for page in order:
        sleep(PAUSE)
        rows, _ = client.orders_page(page, PER_PAGE)
        yield rows
        if not rows:
            return


def sync(conn, client, server: str, now: str, today: date, days: int = DAYS, sleep: Callable[[float], None] = time.sleep) -> dict:
    """Read the WebMIM orders of the last `days` days into webmim_order (GET only). New and changed orders get their positions read again.
    What was read before a failure is kept; the failure (OrdersError, OSError) goes on to the caller. A reading is repeated once after a
    blip by the client itself (reliability.WEBMIM); nothing more is retried here."""
    if server not in webmim_orders.SERVERS:
        raise ValueError("unknown server")
    cutoff = (today - timedelta(days=days)).isoformat()
    conn.commit()                                              # no write of an earlier step is held while WebMIM answers
    known = dict(conn.execute("SELECT id, fingerprint FROM webmim_order WHERE server = ?", (server,)).fetchall())
    counts = {"pages": 0, "pages_total": 0, "orders": 0, "new": 0, "changed": 0, "items": 0, "undated": 0}
    try:
        for rows in _every_page(client, sleep, counts):
            counts["pages"] += 1
            for raw in rows:
                order = _order(raw)
                if order is None or (order["create_date"] and order["create_date"] < cutoff):
                    continue
                counts["orders"] += 1
                counts["undated"] += 0 if order["create_date"] else 1
                before = known.get(order["id"])
                if before is None:
                    counts["new"] += 1
                    conn.execute("INSERT INTO webmim_order (server, id, number, status_code, status_text, create_date, external_number, customer_name, "
                                 "customer_phone_norm, fingerprint, seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                 (server, order["id"], order["number"], order["status_code"], order["status_text"], order["create_date"],
                                  order["external_number"], order["customer_name"], order["customer_phone_norm"], order["fingerprint"], now))
                elif before != order["fingerprint"]:
                    counts["changed"] += 1
                    conn.execute("UPDATE webmim_order SET number = ?, status_code = ?, status_text = ?, create_date = ?, external_number = ?, "
                                 "customer_name = ?, customer_phone_norm = ?, fingerprint = ?, seen_at = ?, items_at = '' WHERE server = ? AND id = ?",
                                 (order["number"], order["status_code"], order["status_text"], order["create_date"], order["external_number"],
                                  order["customer_name"], order["customer_phone_norm"], order["fingerprint"], now, server, order["id"]))
                else:
                    conn.execute("UPDATE webmim_order SET seen_at = ? WHERE server = ? AND id = ?", (now, server, order["id"]))
                known[order["id"]] = order["fingerprint"]          # a page shifted by a new order repeats it: only touched
            conn.commit()
        wanted = [r[0] for r in conn.execute("SELECT id FROM webmim_order WHERE server = ? AND items_at = '' AND create_date >= ? "
                                             "ORDER BY create_date DESC, id DESC LIMIT ?", (server, cutoff, MAX_ITEMS))]
        for order_id in wanted:
            sleep(PAUSE)
            try:
                positions, total = _items(client.items(order_id))
            except webmim_orders.OrdersError as err:
                if err.status not in SKIPPED:
                    raise                                      # no right, no login, too many requests, the server's own failure: the run ends
                conn.execute("UPDATE webmim_order SET items_at = ? WHERE server = ? AND id = ?", (now, server, order_id))
                conn.commit()                                  # this order only (gone, refused): asked again once it changes
                continue
            conn.execute("UPDATE webmim_order SET items = ?, sum = ?, items_at = ? WHERE server = ? AND id = ?",
                         (json.dumps(positions, ensure_ascii=False), total, now, server, order_id))
            conn.commit()
            counts["items"] += 1
    finally:
        conn.commit()
    return counts


# ---- the pairs -----------------------------------------------------------------------------------------------------------------------

def _row(row) -> dict:
    out = dict(zip(FIELDS, row))
    out["items"] = json.loads(out["items"]) if out["items"] else None
    return out


def snapshot(conn, server: str) -> List[dict]:
    return [_row(r) for r in conn.execute("SELECT %s FROM webmim_order WHERE server = ? ORDER BY create_date DESC, id DESC" % ", ".join(FIELDS),
                                          (server,))]


def seen_at(conn, server: str) -> str:
    row = conn.execute("SELECT MAX(seen_at) FROM webmim_order WHERE server = ?", (server,)).fetchone()
    return row[0] or ""


def _index(snap: List[dict]) -> Tuple[Dict[str, dict], Dict[str, dict]]:
    return {o["id"]: o for o in snap}, {o["number"]: o for o in snap if o["number"]}


def paired(card: dict, by_id: Dict[str, dict], by_number: Dict[str, dict]) -> Optional[dict]:
    """The WebMIM order of the card's «ID заказа WebMIM» (first) or «Номер заказа WebMIM»."""
    order_id, number = digits(card.get("webmim_id")), digits(card.get("webmim_number"))
    return (by_id.get(order_id) if order_id else None) or (by_number.get(number) if number else None)


def made_elsewhere(conn, server: str) -> set:
    """The panel's orders made by «Оформить в WebMIM» on another server than `server` and still holding that order's id: their number is of the
    other server's (a test order is never a live one), so they are not paired, suggested or reconciled here."""
    return {r[0] for r in conn.execute("SELECT w.order_id FROM webmim_create w JOIN order_card c ON c.id = w.order_id "
                                       "WHERE w.server != ? AND w.webmim_id != '' AND c.webmim_id = w.webmim_id", (server,))}


def _cards(conn, where: str = "", args: tuple = ()) -> List[dict]:
    names = ("id", "client_name", "client_phone", "point_id", "deal_kind", "webmim_id", "webmim_number", "created_at", "stage_id")
    return [dict(zip(names, r)) for r in conn.execute("SELECT %s FROM order_card %s ORDER BY id DESC" % (", ".join(names), where), args)]


def taken(conn, snap: List[dict]) -> Dict[str, int]:
    """WebMIM order id → the panel's order it is paired with (the newest of several: see shared)."""
    return {order_id: cards[0] for order_id, cards in _pairs(conn, snap).items()}


def shared(conn, snap: List[dict]) -> Dict[str, List[int]]:
    """WebMIM orders paired with more than one order of the panel (a number typed twice, two people at once)."""
    return {order_id: cards for order_id, cards in _pairs(conn, snap).items() if len(cards) > 1}


def _pairs(conn, snap: List[dict]) -> Dict[str, List[int]]:
    by_id, by_number = _index(snap)
    out: Dict[str, List[int]] = {}
    elsewhere = made_elsewhere(conn, snap[0]["server"]) if snap else set()
    for card in _cards(conn, "WHERE webmim_id != '' OR webmim_number != ''"):
        if card["id"] in elsewhere:
            continue
        order = paired(card, by_id, by_number)
        if order is not None:
            out.setdefault(order["id"], []).append(card["id"])
    return out


def _goods(conn, card_ids) -> Dict[int, List[dict]]:
    out: Dict[int, List[dict]] = {}
    for order_id, goods_id, name, qty in conn.execute("SELECT order_id, goods_id, name, qty FROM order_item ORDER BY id"):
        if order_id in card_ids:
            out.setdefault(order_id, []).append({"goods_id": str(goods_id), "name": name, "qty": qty})
    return out


def _day(text) -> Optional[date]:
    try:
        return date.fromisoformat(str(text or "")[:10])
    except ValueError:
        return None


def score(card: dict, items: List[dict], order: dict) -> Tuple[int, List[str]]:
    """(0..100, why) of a WebMIM order for a panel's order without a pair; 0: not suggested (the date is far, nothing in common)."""
    made, there = _day(card.get("created_at")), _day(order.get("create_date"))
    if made is None or there is None or abs((there - made).days) > HINT_DAYS:
        return 0, []
    points, why = 0, []
    if digits(order.get("external_number")) and digits(order["external_number"]) == str(card["id"]) \
            and re.fullmatch(r"\D{0,3}\d+", order["external_number"].strip()):
        points += 70                                           # a small number of another system may be the same: not sure alone
        why.append("внешний номер в WebMIM — №%d панели" % card["id"])
    phone, theirs_phone = phone_tail(card.get("client_phone")), order.get("customer_phone_norm") or ""
    if phone and phone == theirs_phone:
        points += 50
        why.append("тот же телефон")
    elif phone and theirs_phone:
        points -= 10
        why.append("телефон другой")
    mine = {i["goods_id"] for i in items if i["goods_id"]}
    theirs = {i["goods_id"] for i in order.get("items") or []}
    common = mine & theirs
    if common:
        points += round(40 * len(common) / len(mine))
        why.append("общие позиции: %d из %d" % (len(common), len(mine)))
    if points <= 0:
        return 0, []
    delta = abs((there - made).days)
    points += round(10 * (1 - delta / HINT_DAYS))
    why.append("в тот же день" if delta == 0 else "разница %d дн." % delta)
    return (min(points, 99), why) if points >= HINT_MIN else (0, [])


def hints(card: dict, items: List[dict], snap: List[dict], busy) -> List[dict]:
    found = []
    for order in snap:
        if order["id"] in busy:
            continue
        points, why = score(card, items, order)
        if points:
            found.append({"order": order, "score": points, "why": why})
    found.sort(key=lambda h: (-h["score"], h["order"]["create_date"]))
    return found[:HINTS]


def differences(items: List[dict], order: dict) -> List[dict]:
    """Positions that differ: in the panel only, in WebMIM only, another quantity. [] when WebMIM's positions are not read yet."""
    if order.get("items") is None:
        return []
    mine: Dict[str, dict] = {}
    for item in items:
        row = mine.setdefault(item["goods_id"], {"name": item["name"], "qty": 0})
        row["qty"] += int(item["qty"] or 0)
    theirs: Dict[str, dict] = {}
    for item in order["items"]:
        row = theirs.setdefault(item["goods_id"], {"name": item["name"], "qty": 0})
        row["qty"] += int(item["qty"] or 0)
    out = []
    for goods in sorted(set(mine) | set(theirs)):
        panel, there = mine.get(goods), theirs.get(goods)
        if panel is None or there is None or panel["qty"] != there["qty"]:
            out.append({"goods_id": goods, "name": (panel or there)["name"], "panel_qty": panel["qty"] if panel else 0,
                        "webmim_qty": there["qty"] if there else 0})
    return out


def purchase_check(items: List[dict], order: dict) -> Dict[str, dict]:
    """goodsId → {ours: the purchase of one in the panel's order, differs}: the price of WebMIM is the one the order was placed at.
    Only shown (the owner decides whether the panel takes it); the money right only."""
    ours: Dict[str, float] = {}
    for item in items:
        ours.setdefault(str(item["goods_id"]), float(item.get("supplier_price") or 0))
    out = {}
    for row in order.get("items") or []:
        if ours.get(row["goods_id"]):                          # 0: no purchase in the panel yet, nothing to compare
            mine = ours[row["goods_id"]]
            out[row["goods_id"]] = {"ours": mine, "differs": row["price"] is not None and abs(mine - row["price"]) >= 0.5}
    return out


def card_block(conn, card: dict, with_hints: bool = True) -> dict:
    """The block «WebMIM» of the order's page: the paired order and its differences, or the suggestions."""
    server = current_server(conn)
    snap = snapshot(conn, server)
    by_id, by_number = _index(snap)
    test_order = card.get("id") in made_elsewhere(conn, server)
    order = None if test_order else paired(card, by_id, by_number)
    out = {"server": webmim_orders.SERVER_NAMES[server], "seen_at": seen_at(conn, server), "order": order, "differences": [], "hints": [], "purchase": {},
           "test": server != "prod", "test_order": test_order,
           "not_found": bool(order is None and not test_order and (card.get("webmim_number") or card.get("webmim_id")))}
    items = [{"goods_id": str(i["goods_id"]), "name": i["name"], "qty": i["qty"]} for i in card.get("items") or []]
    if order is not None:
        out["differences"] = differences(items, order)
        out["purchase"] = purchase_check(card.get("items") or [], order)
    elif with_hints and server == "prod" and not out["not_found"] and not test_order and card.get("deal_kind") != "stock":
        out["hints"] = hints(card, items, snap, taken(conn, snap))
    return out


def draft(order: dict) -> bool:
    """A WebMIM order «не оформлен»: a basket nobody placed, it wants no pair (the page hides it behind «показать черновики»)."""
    return " ".join(str(order.get("status_text") or "").lower().split()) == "не оформлен"


def reconcile(conn, today: date, days: int = DAYS) -> dict:
    """The page «Сверка с WebMIM»: the panel's orders of the window without a pair (with suggestions), with a number not in the snapshot,
    the WebMIM orders of the window nobody is paired with, those paired twice. Cancelled orders and deals from a point's stock want no pair."""
    server = current_server(conn)
    snap = snapshot(conn, server)
    by_id, by_number = _index(snap)
    busy = taken(conn, snap)
    start = (today - timedelta(days=days)).isoformat()
    cards = [c for c in _cards(conn, "WHERE created_at >= ?", (start,))]
    goods = _goods(conn, {c["id"] for c in cards})
    stages = {s["id"]: s for s in orders.list_stages(conn)}
    unpaired, not_found = [], []
    elsewhere = made_elsewhere(conn, server)
    for card in cards:
        if card["id"] in elsewhere:
            continue
        stage = stages.get(card["stage_id"]) or {"name": "", "code": "", "kind": ""}
        card["stage"] = stage["name"]
        if card["webmim_number"] or card["webmim_id"]:
            if paired(card, by_id, by_number) is None:
                not_found.append(card)
            continue
        if card["deal_kind"] == "stock" or stage["code"] == "cancel" or stage.get("kind") == "lost":
            continue
        card["hints"] = hints(card, goods.get(card["id"], []), snap, busy) if server == "prod" else []
        unpaired.append(card)
    alone = [o for o in snap if o["id"] not in busy and o["create_date"] >= start]
    return {"server": webmim_orders.SERVER_NAMES[server], "seen_at": seen_at(conn, server), "days": days, "paired": len(busy),
            "unpaired": unpaired, "not_found": not_found, "lonely": [o for o in alone if not draft(o)], "drafts": [o for o in alone if draft(o)],
            "shared": [{"order": by_id[k], "cards": v} for k, v in shared(conn, snap).items()], "total": len(snap), "test": server != "prod"}


def link(conn, card_id: int, order_id: str, now: str, author: str) -> dict:
    """«Это он»: the panel's order gets the number and the id of the WebMIM order (the journal of the order says so). The purchase is not
    taken again (the unload of today is not the price of an old order). Raises OrderError: the order has a WebMIM number already, the WebMIM
    order is not in the snapshot or is paired with another order, the snapshot is of the test server (its orders are not ours).
    The checks and the write are one IMMEDIATE transaction: two people cannot pair one WebMIM order with two orders."""
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        card = orders._row(conn, card_id)
        if card["webmim_number"] or card["webmim_id"]:
            raise orders.OrderError("У заказа уже есть номер WebMIM: поправьте его в карточке.")
        server = current_server(conn)
        if server != "prod":
            raise orders.OrderError("Снимок с тестового сервера WebMIM: его заказы не наши, сопоставлять не с чем.")
        snap = snapshot(conn, server)
        order = _index(snap)[0].get(digits(order_id) or "-")
        if order is None:
            raise orders.OrderError("Этого заказа WebMIM нет в снимке: обновите снимок.")
        other = taken(conn, snap).get(order["id"])
        if other is not None:
            raise orders.OrderError("Заказ WebMIM №%s уже сопоставлен с заказом №%d." % (order["number"] or order["id"], other))
        orders.update_card(conn, card_id, {"webmim_number": order["number"], "webmim_id": order["id"]}, now, author, commit=False)
        orders.add_event(conn, card_id, now, author, "field", "Сопоставлен с заказом WebMIM №%s (подтверждено вручную)" % (order["number"] or order["id"]))
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    return order


def run(conn, box, http, now: str, today: date, days: int = DAYS, sleep: Callable[[float], None] = time.sleep) -> Tuple[str, str]:
    """One reading of the snapshot for the command and the hourly tick: (read / off / failed, a line for the journal). A failure of the login or the
    network ends the reading (what was read stays); only the client repeats a reading once after a blip (reliability.WEBMIM), the login
    goes once; the line never carries a login, a password or a key."""
    if box is None:
        return "failed", "снимок WebMIM не прочитан: нет ключа шифрования"
    server = current_server(conn)
    try:
        client = client_for(conn, box, http)
    except webmim_orders.OrdersError as err:
        return "failed", "снимок WebMIM не прочитан: %s" % err
    except SecretKeyError:
        return "failed", "снимок WebMIM не прочитан: секреты не читаются, проверьте мастер-ключ"
    if client is None:
        return "off", "снимок WebMIM не читается: API-пользователь заказов не задан"
    if _refused_before(conn):
        return "failed", "снимок WebMIM не читается: логин или пароль API-пользователя не приняты, вход не повторяется до нового пароля или проверки в «Подключениях»"
    try:
        with client:
            counts = sync(conn, client, server, now, today, days, sleep)
    except webmim_orders.OrdersError as err:
        if str(err).startswith(REFUSED):                       # shown in «Подключения»; the hourly tick does not knock again with the same password
            connections.record_check(conn, "webmim_orders", connections.supplier_owner(1), False, "снимок заказов: %s" % err, now)
        return "failed", "снимок WebMIM (%s) прерван: %s" % (server, err)
    except OSError as err:
        return "failed", "снимок WebMIM (%s) прерван: сервер недоступен (%s)" % (server, type(err).__name__)
    pages = "%d из %d" % (counts["pages"], counts["pages_total"]) if counts["pages_total"] else "%d (сколько всего, WebMIM не сказал)" % counts["pages"]
    line = "снимок WebMIM (%s, %d дн.): страниц %s, заказов %d, новых %d, изменилось %d, позиции прочитаны у %d" % (
        server, days, pages, counts["orders"], counts["new"], counts["changed"], counts["items"])
    if counts["pages_total"] > MAX_PAGES:
        line += "; прочитаны не все страницы (предел %d), самые новые" % MAX_PAGES
    elif counts["pages"] < counts["pages_total"]:
        line += "; чтение остановила пустая страница"
    if counts["orders"] and counts["undated"] == counts["orders"]:
        return "failed", line + "; ни у одного заказа не разобрана дата создания: формат WebMIM другой, сверка не работает"
    return "read", line


REFUSED = "Логин или пароль не приняты"                     # webmim_orders.OrdersClient.token: the server said the pair is wrong


def _refused_before(conn) -> bool:
    """The last check (by a person or by this reading) found the login or the password wrong, and the password was not saved since."""
    owner = connections.supplier_owner(1)
    check = connections.get_check(conn, "webmim_orders", owner)
    if not check or not str(check["status"]).startswith("error: ") or REFUSED not in str(check["status"]):
        return False
    row = conn.execute("SELECT updated FROM secret WHERE kind = 'webmim_orders' AND owner_id = ? AND name = 'password'", (owner,)).fetchone()
    return not row or str(row[0]) <= str(check["at"])
