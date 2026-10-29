"""The one search of the panel (the field in the top bar; the owner 2026-10-21): the orders, the clients, the chats and the positions of one
query, a few of each with a link to the page of the section. The matching is the pages' own (orders.board_matches, clients.list_clients,
chats.find, the catalog's offer_matches given by the panel); here: how the query is read, what the person may see, and the answer as data
(the panel shows it, a later interface may too).

The rights are one filter, scope_for: a section the role has not is never searched (not searched and hidden); the manager of a point finds
the orders of the own point and the chats of the point's account only, and a chat through an order (its number, its phone) only when the
order is of the point, the clients it has a part of (clients.list_clients_for_point, the owner 2026-10-26); without «money» no purchase
price."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Iterable, Optional
from urllib.parse import urlencode

from . import chats, clients, connections, orders, users

GROUPS = ("orders", "clients", "chats", "positions")
TITLES = {"orders": "Заказы", "clients": "Клиенты", "chats": "Чаты", "positions": "Позиции"}
SECTION = {"orders": "orders", "clients": "clients", "chats": "chats", "positions": "catalog"}
ALL_URL = {"orders": "/orders", "clients": "/clients", "chats": "/chats", "positions": "/catalog"}
LIMIT = 5                       # a group under the field; «все» opens the section with the same query
QUERY_MAX = chats.SEARCH_MAX
TEXT_DAYS = 92                  # the text of the messages: the last three months (no index: every message is read); names and numbers for ever

_CHAT = re.compile(r"ч\s*-?\s*(\d{1,9})", re.I)                      # «Ч-2346»: the panel's number of a chat
_ORDER = re.compile(r"(?:заказ\s*№?|№|#)\s*(\d{1,9})", re.I)          # «№123», «#123», «заказ 123»
_PHONE = orders.BOARD_PHONE


@dataclass(frozen=True)
class Scope:
    groups: frozenset
    point: Optional[int] = None          # the orders of this point only (the manager of a point)
    account: Optional[int] = None        # the chats of this Avito account only (the manager of a point)
    money: bool = False                  # the purchase price may be shown


def scope_for(role: str, point_id: Optional[int] = None, point_account: Optional[int] = None) -> Scope:
    """What a role finds: the groups of the sections it may look at; the manager of a point the orders of the point, the chats of its
    account (no account chosen: no chat; no point: nothing) and the clients of its own part, never the positions."""
    groups = {g for g in GROUPS if users.level(role, SECTION[g]) >= users.VIEW}
    if role != "point":
        return Scope(frozenset(groups), money=users.has_right(role, "money"))
    groups &= {"orders", "chats", "clients"}
    if not point_id:
        groups = set()
    if point_account is None:
        groups.discard("chats")
    return Scope(frozenset(groups), point=point_id or None, account=point_account, money=users.has_right(role, "money"))


def parse(q) -> dict:
    """{"text", "digits", "kind": chat | order | phone | number | text, "number", "phone": the last ten digits}."""
    text = " ".join(str(q or "").split())[:QUERY_MAX]
    digits = re.sub(r"\D", "", text)
    p = {"text": text, "digits": digits, "kind": "text", "number": None, "phone": ""}
    chat, order = _CHAT.fullmatch(text), _ORDER.fullmatch(text)
    if chat:
        p.update(kind="chat", number=int(chat.group(1)))
    elif order:
        p.update(kind="order", number=int(order.group(1)))
    elif len(digits) >= 10 and _PHONE.fullmatch(text):
        p.update(kind="phone", phone=digits[-10:])
    elif text.isdigit():
        p.update(kind="number", number=int(text))
    return p


def _orders(conn, p: dict, scope: Scope, limit: int) -> list:
    if p["kind"] == "chat":
        return []
    wanted = orders.board_query("№" + p["digits"] if p["kind"] == "order" else p["text"])
    first = {}
    for order_id, name, qty in conn.execute("SELECT order_id, name, qty FROM order_item ORDER BY id"):
        first.setdefault(order_id, "%s × %d" % (name, qty))
    sql, args = ("SELECT o.id, o.client_name, o.client_phone, o.webmim_number, s.name, o.transfer_number FROM order_card o "
                 "JOIN order_stage s ON s.id = o.stage_id", ())
    if scope.point is not None:
        sql, args = sql + " WHERE o.point_id = ?", (scope.point,)
    found = []
    for cid, name, phone, number, stage, transfer in conn.execute(sql + " ORDER BY o.id DESC", args):
        if not orders.board_matches(wanted, cid, name, phone, first.get(cid, ""), number or "", transfer or ""):
            continue
        found.append({"type": "order", "title": "Заказ №%d · %s" % (cid, name or phone or "Без имени"),
                      "subtitle": " · ".join(x for x in (stage, "перемещение %s" % transfer if transfer else "", first.get(cid, "")) if x),
                      "url": "/orders/%d" % cid})
        if len(found) > limit:
            break
    return found


def _clients(conn, p: dict, scope: Scope, limit: int) -> list:
    if p["kind"] in ("chat", "order") or (p["kind"] == "number" and len(p["digits"]) < 5):
        return []                                                  # a short number is an order's, not a piece of every phone
    found = []
    rows = (clients.list_clients_for_point(conn, scope.point, scope.account, p["phone"] or p["text"], limit=limit + 1) if scope.point is not None
            else clients.list_clients(conn, p["phone"] or p["text"], limit=limit + 1, fold=True))      # the point: its own part, its own counts
    for c in rows:
        counts = "заказов: %d · чатов: %d" % (c["orders"], c["chats"])
        found.append({"type": "client", "title": c["name"] or "Клиент №%d" % c["id"],
                      "subtitle": " · ".join(x for x in (clients.display(c["phone"]) if c["phone"] else "", counts) if x),
                      "url": "/clients/%d" % c["id"]})
    return found


def _chats(conn, p: dict, scope: Scope, since: str, limit: int) -> list:
    if p["kind"] == "chat":
        chat = chats.get_chat(conn, p["number"])
        rows = [chat] if chat is not None and (scope.account is None or chat["account_id"] == scope.account) else []
    else:
        found = chats.find(conn, "№%d" % p["number"] if p["kind"] == "order" else p["text"], since=since, point=scope.point)
        enabled = None if scope.account is not None else {a["id"] for a in connections.list_accounts(conn) if a["chats_enabled"]}
        rows = [c for c in chats.list_chats(conn, scope.account) if c["id"] in found and (enabled is None or c["account_id"] in enabled)]
    return [{"type": "chat", "title": "Ч-%d · %s" % (c["id"], c["peer_name"] or "без имени"), "subtitle": c["item_title"] or "",
             "url": "/chats/%d" % c["id"]} for c in rows[:limit + 1]]


def _rub(value) -> str:
    return "{:,.0f}".format(float(value)).replace(",", " ")


def _positions(p: dict, scope: Scope, source: Optional[Callable[[str], Iterable]], limit: int) -> list:
    """`source(text)`: the matching positions as (offer, None) and the gone ones as (offer, what is known), in the catalog's order."""
    if p["kind"] not in ("text", "number") or source is None:
        return []
    found = []
    for offer, gone in source(p["text"]):
        bits = [offer.code, "%d/%d R%d" % (offer.width, offer.height, offer.diameter), "нет у поставщика" if gone else "остаток %d" % offer.stock]
        price = gone["last_price"] if gone else offer.base_price
        if scope.money and price:                                  # the purchase price is money: only with the right
            bits.append("закупка %s ₽" % _rub(price))
        found.append({"type": "position", "title": offer.name_orig or offer.name, "subtitle": " · ".join(b for b in bits if b),
                      "url": "/catalog?" + urlencode({"q": offer.code or offer.goods_id})})
        if len(found) > limit:
            break
    return found


def run(conn, q, scope: Scope, now: datetime, positions: Optional[Callable[[str], Iterable]] = None, limit: int = LIMIT) -> dict:
    """{"q", "groups": [{"key", "title", "items": [{"type", "title", "subtitle", "url"}], "more", "all_url"}]}: the groups with something
    found, at most `limit` items each. `now`: Moscow time, as the messages are kept."""
    p = parse(q)
    if len(p["text"]) < 2 and not p["digits"]:
        return {"q": p["text"], "groups": []}
    since = (now - timedelta(days=TEXT_DAYS)).isoformat(timespec="seconds")
    finders = {"orders": lambda: _orders(conn, p, scope, limit), "clients": lambda: _clients(conn, p, scope, limit),
               "chats": lambda: _chats(conn, p, scope, since, limit), "positions": lambda: _positions(p, scope, positions, limit)}
    groups = []
    for key in GROUPS:
        if key not in scope.groups:
            continue                                               # never searched, not searched and hidden
        found = finders[key]()
        if found:
            groups.append({"key": key, "title": TITLES[key], "items": found[:limit], "more": len(found) > limit,
                           "all_url": ALL_URL[key] + "?" + urlencode({"q": p["text"]})})
    return {"q": p["text"], "groups": groups}
