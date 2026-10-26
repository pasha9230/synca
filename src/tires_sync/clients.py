"""The client: one person behind the orders and the Avito chats, joined by a phone or by the Avito peer.

The orders and the chats are never changed here: a client is a table of its own with links to them (client_order, client_chat), its phones
(E.164, one phone belongs to one client) and its Avito peers (account + peer id). The links of the old data are made only by the command
`clients-link` (a report first, writing with --confirm); a new order or a deal made from a chat is linked as it is made."""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from . import connections, store

MANUAL = ("manual", "split")                       # links a person made: the automatic joining never moves them
NAME_MAX, NOTE_MAX = 120, 2000
CAR_MAX, CAR_YEAR_FIRST = 40, 1950
PRAGMATIC = "Прагматик"                            # the tag shown red at the top right of the card (the owner 2026-10-23, п. 13)
HANDED = "передан точкой"                          # never kept: read from the handoffs of the client's orders, chats and calls (п. 25)
TAG_MAX, TAGS_MAX = 30, 10
NEW_INQUIRY_GAP_HOURS = 24                         # a touch of the client after this long a silence is a new inquiry (the lanes CRM 2026-10-23)


class ClientError(ValueError):
    pass


def normalize(raw) -> str:
    """A phone the way the clients keep it (E.164): +7XXXXXXXXXX for a Russian number, + and the digits for another one (11–15 digits),
    empty when it is not a phone."""
    phone = connections.normalize_phone(raw)
    if phone:
        return phone
    digits = re.sub(r"\D", "", str(raw or ""))
    return "+" + digits if 11 <= len(digits) <= 15 and str(raw or "").strip().startswith("+") else ""


def own_phones(conn) -> set:
    """Our own numbers (the points' managers, the Avito cabinets, the owner's number in the ads): never a client's, never a join."""
    found = {r[0] for r in conn.execute("SELECT manager_phone FROM point")}
    for account in connections.list_accounts(conn):
        try:                                                        # one account whose data cannot be read must not hide the others
            identity = connections.get_avito_identity(conn, account["id"])
            found.update([identity.get("phone", "")] + list(identity.get("phones") or []))
            found.add(connections._file_phone(conn, account["id"]))       # the number of the mirrored file of the account
        except Exception:
            continue
    try:
        from .settings_store import get_active
        found.add(get_active(conn)[1].get("contact_phone", ""))
    except Exception:
        pass
    return {p for p in (normalize(f) for f in found) if p}


def usable(phone: str, own: set) -> str:
    """The phone if it can tell a person: not ours, not a dummy (+7 000…, the same digit seven times at the end: 8 900 000-00-00)."""
    body = phone[-10:] if phone else ""
    if not phone or phone in own or len(set(body[3:])) <= 1 or body.startswith("000"):
        return ""
    return phone


def display(phone) -> str:
    """+7 900 111-22-33 for a Russian number; the number as it is otherwise."""
    return connections.format_phone(phone)


# ---- reading -----------------------------------------------------------------------------------------------------------------------

def get_client(conn, client_id) -> dict:
    keys = ("id", "name", "note", "created_at", "created_by", "merged_into", "car_brand", "car_model", "car_year")
    row = conn.execute("SELECT %s FROM client WHERE id = ?" % ", ".join(keys), (client_id,)).fetchone()
    if row is None:
        raise ClientError("Такого клиента нет.")
    client = dict(zip(keys, row))
    client["phones"] = [r[0] for r in conn.execute("SELECT phone FROM client_phone WHERE client_id = ? ORDER BY phone", (client_id,))]
    client["peers"] = [{"account_id": r[0], "peer_id": r[1]} for r in conn.execute(
        "SELECT account_id, peer_id FROM client_peer WHERE client_id = ? ORDER BY account_id, peer_id", (client_id,))]
    client["orders"] = [dict(zip(("id", "client_name", "client_phone", "created_at", "stage", "kind", "how"), r)) for r in conn.execute(
        "SELECT c.id, c.client_name, c.client_phone, c.created_at, s.name, s.kind, l.how FROM client_order l JOIN order_card c ON c.id = l.order_id "
        "JOIN order_stage s ON s.id = c.stage_id WHERE l.client_id = ? ORDER BY c.id DESC", (client_id,))]
    client["chats"] = [dict(zip(("id", "account_id", "peer_name", "item_title", "last_at", "order_id", "how"), r)) for r in conn.execute(
        "SELECT a.id, a.account_id, a.peer_name, a.item_title, a.last_at, a.order_id, l.how FROM client_chat l JOIN avito_chat a ON a.id = l.chat_row "
        "WHERE l.client_id = ? ORDER BY a.last_at DESC", (client_id,))]
    from . import calls                                                          # the calls through the Avito substitute numbers
    client["calls"] = calls.calls_of_client(conn, client_id)
    return client


def card_view(conn, client: dict, accounts: Dict[int, str], bought: Optional[float], chat_links: bool, whole: bool = True) -> dict:
    """The client's card in the one look of the cards (_entity.html), a dict ready to be JSON. `bought` is None without the right «money»,
    `chat_links`: the person may open the chats. The events are what the data already keeps: the orders made, the journal of the client.
    `whole=False` (the point's part, card_view_for_point): the journal, the inquiries and the tags of the whole client are not read at all."""
    cid = client["id"]
    events = client_events(conn, client) if whole else []
    asked = inquiries(conn, cid) if whole else []
    fields = [{"label": "Телефоны", "value": client["phones"], "fmt": "phone"}, {"label": "Автомобиль", "value": car_text(client)},
              {"label": "Первое обращение", "value": asked[-1]["at"] if asked else None, "fmt": "dt"},
              {"label": "Последнее обращение", "value": asked[0]["at"] if asked else None, "fmt": "dt"}, {"label": "Обращений", "value": len(asked)},
              {"label": "Заказов", "value": len(client["orders"])}, {"label": "Чатов Авито", "value": len(client["chats"])},
              {"label": "Звонков", "value": len(client.get("calls") or [])},
              {"label": "Клиент с", "value": next((e["at"] for e in events if e.get("made")), client["created_at"]), "fmt": "dt"}]
    if bought is not None:
        fields.insert(1, {"label": "Куплено на", "value": bought, "fmt": "money"})
    via = {"call": "Звонок", "chat": "Чат Авито", "order": "Заказ"}
    hrefs = {"call": lambda i: None, "chat": lambda i: "/chats/%d" % i if chat_links else None, "order": lambda i: "/orders/%d" % i}
    refs = {"call": lambda i: "", "chat": lambda i: "Ч-%d" % i, "order": lambda i: "№%d" % i}
    asked_rows = [{"when": {"text": a["at"], "fmt": "dt"}, "via": via[a["via"]] + (" (пропущен)" if a.get("missed") else ""),
                   "ref": {"text": refs[a["via"]](a["id"]), "href": hrefs[a["via"]](a["id"])}, "n": len(asked) - n} for n, a in enumerate(asked)]
    orders = [{"id": o["id"], "ref": {"text": "№%d" % o["id"], "href": "/orders/%d" % o["id"]}, "name": o["client_name"],
               "phone": {"text": o["client_phone"], "fmt": "phone"}, "stage": o["stage"], "created": {"text": o["created_at"], "fmt": "dt"}}
              for o in client["orders"]]
    chats = [{"id": h["id"], "ref": {"text": "Ч-%d" % h["id"], "href": "/chats/%d" % h["id"] if chat_links else None}, "peer": h["peer_name"],
              "account": accounts.get(h["account_id"], h["account_id"]), "item": h["item_title"], "last": {"text": h["last_at"], "fmt": "dt"}}
              for h in client["chats"]]
    calls = [{"id": k["id"], "phone": k["buyer_phone"], "when": {"text": k["call_at"], "fmt": "dt"}, "account": k["point_name"] or accounts.get(k["account_id"], k["account_id"]),
              "result": ("Принят, %d:%02d" % divmod(k["talk_seconds"], 60)) if k["answered"] else "Пропущен", "record": k["record_text"],
              "listen": k["record_state"] == "saved", "order": {"text": "№%d" % k["order_id"], "href": "/orders/%d" % k["order_id"]}
              if k["order_id"] and k["order_id"] > 0 else None} for k in client.get("calls") or []]
    return {"type": "client", "kind": "Клиент", "ref": "№%d" % cid, "title": client["name"] or "Клиент без имени",
            "status": None,                                    # the status of the inquiry is drawn by _client_inquiry.html (the lane crm-inquiry)
            "crumbs": [{"label": "CRM", "href": "/clients"}, {"label": "№%d" % cid}], "tags": (tags_of(conn, cid) + ([HANDED] if handed(conn, cid) else [])) if whole else [],
            "fields": fields, "orders": orders, "purchases": purchases_view(conn, client), "chats": chats, "calls": calls,
            "inquiries": asked_rows, "events": events}


_EVENT_ACTIONS = ("client_saved", "client_merged", "client_split", "client_unmerged", "client_tagged", "client_pair_skipped")


def _moscow(at: str) -> str:
    """A stamp of the journal (UTC, the panel's clock) in Moscow time, as the orders are written."""
    try:
        return (datetime.fromisoformat(at[:19]) + timedelta(hours=3)).isoformat(timespec="seconds")
    except ValueError:
        return at


def client_events(conn, client: dict, limit: int = 50) -> List[dict]:
    """The client's history, newest first, in Moscow time: made, its orders made, saved, joined and split (from the journal; no table of
    its own). The orders are stamped in Moscow time, the journal in UTC. A client split off was made by the split: its time is the journal's."""
    cid = client["id"]
    made = {"at": client["created_at"], "author": client["created_by"], "text": "Клиент заведён", "made": True, "seq": 0}
    events = [made] + [{"at": o["created_at"], "author": "", "text": "Заказ №%d создан (сейчас: %s)" % (o["id"], o["stage"]), "seq": o["id"]}
                       for o in client["orders"]]
    rows = conn.execute(
        "SELECT id, at, author, action, detail FROM audit WHERE action IN (%s) AND (json_extract(detail, '$.client') = ? OR json_extract(detail, '$.into') = ? "
        "OR json_extract(detail, '$.from') = ? OR json_extract(detail, '$.to') = ?) ORDER BY id DESC LIMIT ?" % ",".join("?" * len(_EVENT_ACTIONS)),
        _EVENT_ACTIONS + (cid, cid, cid, cid, limit))
    for row_id, at, author, action, detail in rows:
        d = json.loads(detail)
        what = "заказ №%s" % d["order"] if d.get("order") else ("чат Ч-%s" % d["chat"] if d.get("chat") else "")
        if action == "client_saved":
            text = "Имя, комментарий или автомобиль изменены"
        elif action == "client_tagged":
            text = "Тег «%s» %s" % (d.get("tag"), "поставлен" if d.get("on") else "снят")
        elif action == "client_merged":
            text = "Присоединён клиент №%s" % d.get("from") if d.get("into") == cid else "Присоединён к клиенту №%s" % d.get("into")
        elif action == "client_unmerged":
            text = "Отсоединён клиент №%s" % d.get("to") if d.get("client") == cid else "Отсоединён от клиента №%s" % d.get("client")
        elif action == "client_pair_skipped":
            text = "Не тот же клиент, что №%s" % (d.get("to") if d.get("client") == cid else d.get("client"))
        elif d.get("from") == cid:
            text = "Отделён %s: теперь клиент №%s" % (what, d.get("to"))
        else:
            text = "Отделён от клиента №%s: %s" % (d.get("from"), what)
            made["at"] = _moscow(at)
        events.append({"at": _moscow(at), "author": author, "text": text, "seq": 10 ** 9 + row_id})     # the journal is newer than the rest in a tie
    events.sort(key=lambda e: (e["at"] or "", e["seq"]), reverse=True)
    return [{k: v for k, v in e.items() if k != "seq"} for e in events[:limit]]


def purchases(conn, client_id) -> float:
    """What the client bought: the sales recorded by its orders («Продажи», reversed ones left out) — the sum the report of the orders uses."""
    row = conn.execute("SELECT COALESCE(SUM(s.sale_total), 0) FROM sale s JOIN client_order l ON s.note = 'Заказ №' || l.order_id "
                       "WHERE l.client_id = ? AND s.source = 'order' AND s.reversed_at IS NULL", (client_id,)).fetchone()
    return round(float(row[0]), 2)


def car_text(client: dict) -> str:
    """«Kia Rio, 2017»; empty without a car."""
    head = " ".join(p for p in (client.get("car_brand") or "", client.get("car_model") or "") if p)
    year = str(client["car_year"]) if client.get("car_year") else ""
    return ", ".join(p for p in (head, year) if p)


def tags_of(conn, client_id) -> List[str]:
    """The tags typed by hand: «Прагматик» first, then the rest by the alphabet. «передан точкой» is not here: see handed()."""
    tags = [r[0] for r in conn.execute("SELECT tag FROM client_tag WHERE client_id = ?", (client_id,))]
    return sorted(tags, key=lambda t: (t != PRAGMATIC, t.casefold()))


# a client is «передан точкой» when a point handed over one of its orders, chats or calls (order_manager._open_handoff writes the source)
_HANDED_SQL = ("EXISTS (SELECT 1 FROM order_handoff_source s WHERE s.order_id IN (SELECT order_id FROM client_order WHERE client_id = {c}) "
               "OR s.chat_row IN (SELECT chat_row FROM client_chat WHERE client_id = {c}) "
               "OR (s.kind = 'call' AND s.call_id IN (SELECT CAST(call_row AS TEXT) FROM client_call WHERE client_id = {c})))")


def handed(conn, client_id) -> bool:
    return bool(conn.execute("SELECT " + _HANDED_SQL.format(c="?"), (client_id,) * 3).fetchone()[0])


def all_tags(conn) -> List[str]:
    """The tags to filter the list by: «Прагматик», «передан точкой», then the free ones in use."""
    free = sorted({r[0] for r in conn.execute("SELECT DISTINCT t.tag FROM client_tag t JOIN client c ON c.id = t.client_id WHERE c.merged_into IS NULL")}
                  - {PRAGMATIC, HANDED}, key=str.casefold)
    return [PRAGMATIC, HANDED] + free


def _at(text) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(text or "")[:19])
    except ValueError:
        return None


def inquiries(conn, client_id) -> List[dict]:
    """The client's inquiries, newest first: {"at", "via": call | chat | order, "id", "missed"}. Every touch is read where it is kept (the
    calls, the messages of the client's chats, the orders); the first one of the client and every one after NEW_INQUIRY_GAP_HOURS of silence
    is an inquiry. Our own messages keep a conversation going but never start an inquiry. A chat whose messages were never read counts from
    its start. All the times are Moscow's."""
    from .chats import iso                                                  # the chats read the clients: imported here, not at the top
    touches = [(r[0], "call", r[1], not r[2]) for r in conn.execute(
        "SELECT k.call_at, k.id, k.answered FROM client_call l JOIN avito_call k ON k.id = l.call_row WHERE l.client_id = ?", (client_id,))]
    touches += [(r[0], "order", r[1], False) for r in conn.execute(
        "SELECT c.created_at, c.id FROM client_order l JOIN order_card c ON c.id = l.order_id WHERE l.client_id = ?", (client_id,))]
    touches += [(r[0], "chat" if r[1] == "in" else "", r[2], False) for r in conn.execute(
        "SELECT m.at, m.direction, m.chat_row FROM client_chat l JOIN avito_message m ON m.chat_row = l.chat_row WHERE l.client_id = ? "
        "AND m.kind NOT IN ('system', 'deleted')", (client_id,))]                # Avito's own notes and deleted messages are nobody's touch
    touches += [(iso(r[1]) if r[1] else r[2], "chat", r[0], False) for r in conn.execute(
        "SELECT a.id, a.created_ts, a.last_at FROM client_chat l JOIN avito_chat a ON a.id = l.chat_row WHERE l.client_id = ? "
        "AND NOT EXISTS (SELECT 1 FROM avito_message m WHERE m.chat_row = a.id AND m.kind NOT IN ('system', 'deleted'))", (client_id,))]
    gap = timedelta(hours=NEW_INQUIRY_GAP_HOURS)
    found, last = [], None
    for at, via, ref, missed in sorted((t for t in touches if _at(t[0])), key=lambda t: (_at(t[0]), t[1] == "")):
        moment = _at(at)
        if via and (not found or moment - last >= gap):
            found.append({"at": moment.isoformat(timespec="seconds"), "via": via, "id": ref, "missed": missed})
        last = moment if last is None else max(last, moment)
    return found[::-1]


def purchases_view(conn, client: dict) -> List[dict]:
    """The client's orders as purchases, newest first: what (the positions with the size read from the name), how many, at what price of
    sale, the sums, when, the point, the stage; `won`: bought (a stage «успешный»), `lost`: refused. The purchase price is never here."""
    from .order_wait import SIZE_IN_TITLE                                   # the orders read the clients: imported here
    ids = [o["id"] for o in client["orders"]]
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    points = dict(conn.execute("SELECT c.id, p.name FROM order_card c JOIN point p ON p.id = c.point_id WHERE c.id IN (%s)" % marks, ids))
    items: Dict[int, List[dict]] = defaultdict(list)
    for order_id, name, qty, unit in conn.execute("SELECT order_id, name, qty, sale_unit FROM order_item WHERE order_id IN (%s) ORDER BY id" % marks, ids):
        size = SIZE_IN_TITLE.search(name or "")
        items[order_id].append({"name": name, "size": "%s/%s R%s" % size.groups() if size else "", "qty": qty, "unit": unit,
                                "sum": round(qty * unit, 2)})
    return [{"id": o["id"], "created": o["created_at"], "stage": o["stage"], "won": o["kind"] == "won", "lost": o["kind"] == "lost",
             "point": points.get(o["id"], ""), "items": items[o["id"]], "total": round(sum(i["sum"] for i in items[o["id"]]), 2)}
            for o in client["orders"]]


def list_clients(conn, query: str = "", limit: int = 200, fold: bool = False, tag: str = "") -> List[dict]:
    """The clients by the name, a phone or the car, the newest first; `fold`: in any case (SQLite's LIKE folds the Latin letters only);
    `tag`: only the clients with it («передан точкой» is read from the handoffs)."""
    where, args = "WHERE c.merged_into IS NULL", []
    query = str(query or "").strip()
    if query:
        phone = normalize(query)
        digits = re.sub(r"\D", "", query)
        if fold:
            conn.create_function("fold", 1, lambda text: (text or "").casefold())
        name, car = ("fold(c.name)", "fold(c.car_brand || ' ' || c.car_model)") if fold else ("c.name", "(c.car_brand || ' ' || c.car_model)")
        where += " AND (%s LIKE ? OR %s LIKE ? OR c.car_year = ? OR c.id IN (SELECT client_id FROM client_phone WHERE phone LIKE ?))" % (name, car)
        text = "%" + (query.casefold() if fold else query) + "%"
        args += [text, text, int(query) if re.fullmatch(r"\d{4}", query) else None, "%" + (phone or digits or query) + "%"]
    tag = str(tag or "").strip()
    if tag == HANDED:
        where += " AND " + _HANDED_SQL.format(c="c.id")
    elif tag:
        where += " AND c.id IN (SELECT client_id FROM client_tag WHERE tag = ?)"
        args.append(tag)
    rows = conn.execute("SELECT c.id, c.name, (SELECT COUNT(*) FROM client_order o JOIN order_card k ON k.id = o.order_id WHERE o.client_id = c.id), "
                        "(SELECT COUNT(*) FROM client_chat h JOIN avito_chat a ON a.id = h.chat_row WHERE h.client_id = c.id), (SELECT MIN(phone) FROM client_phone p WHERE p.client_id = c.id), "
                        "(SELECT COUNT(*) FROM client_call l WHERE l.client_id = c.id), c.car_brand, c.car_model, c.car_year, %s, "
                        "MAX(COALESCE((SELECT MAX(k.call_at) FROM client_call l JOIN avito_call k ON k.id = l.call_row WHERE l.client_id = c.id), ''), "
                        "COALESCE((SELECT MAX(a.last_at) FROM client_chat h JOIN avito_chat a ON a.id = h.chat_row WHERE h.client_id = c.id), ''), "
                        "COALESCE((SELECT MAX(k.created_at) FROM client_order o JOIN order_card k ON k.id = o.order_id WHERE o.client_id = c.id), '')) "
                        "FROM client c %s ORDER BY c.id DESC LIMIT ?" % (_HANDED_SQL.format(c="c.id"), where), args + [limit])
    found = [dict(zip(("id", "name", "orders", "chats", "phone", "calls", "car_brand", "car_model", "car_year", "handed", "last"), r)) for r in rows]
    tags: Dict[int, List[str]] = defaultdict(list)
    if found:
        for client_id, name in conn.execute("SELECT client_id, tag FROM client_tag WHERE client_id IN (%s)" % ",".join("?" * len(found)), [r["id"] for r in found]):
            tags[client_id].append(name)
    for r in found:
        r["tags"] = sorted(tags[r["id"]], key=lambda t: (t != PRAGMATIC, t.casefold())) + ([HANDED] if r.pop("handed") else [])
        r["car"] = car_text(r)
    return found


# ---- the CRM of a manager of a point (the owner 2026-10-26) ---------------------------------------------------------------------------
# A point sees its own part of a client only: the orders of the point, the chats and the calls of the point's Avito account (none without
# one), the phones of those, the sales of those orders. Never the other points' orders, the other accounts' chats and calls, the phones only
# they know, the tags, the comment, the car, the duplicates, the joins. The selection is in the queries of the point's part (point_part); the
# card is built from that part only (the client read whole is narrowed before card_view, which then reads nothing of the whole client).

def _point_part(conn, point_id, account_id) -> Dict[int, dict]:
    """{client: {"orders": [order ids], "chats": [chat rows], "calls": [call rows], "phones": {E.164}}} of the clients the point has a part
    of (the clients joined away have no links of their own)."""
    part: Dict[int, dict] = defaultdict(lambda: {"orders": [], "chats": [], "calls": [], "phones": set()})
    for client_id, order_id, phone in conn.execute(
            "SELECT l.client_id, k.id, k.client_phone FROM client_order l JOIN order_card k ON k.id = l.order_id WHERE k.point_id = ? "
            "ORDER BY k.id DESC", (point_id,)):
        part[client_id]["orders"].append(order_id)
        part[client_id]["phones"].add(normalize(phone))
    if account_id is not None:
        for client_id, chat_row in conn.execute("SELECT l.client_id, a.id FROM client_chat l JOIN avito_chat a ON a.id = l.chat_row "
                                                "WHERE a.account_id = ?", (account_id,)):
            part[client_id]["chats"].append(chat_row)
        for client_id, call_row, phone in conn.execute("SELECT l.client_id, k.id, k.buyer_phone FROM client_call l JOIN avito_call k ON k.id = l.call_row "
                                                       "WHERE k.account_id = ?", (account_id,)):
            part[client_id]["calls"].append(call_row)
            part[client_id]["phones"].add(normalize(phone))
    own = own_phones(conn)                                # our own numbers typed into an order are never the client's (as in client_phone)
    for links in part.values():
        links["phones"] = {p for p in links["phones"] if usable(p, own)}
    return dict(part)


def point_part(conn, client_id, point_id, account_id) -> Optional[dict]:
    """The point's part of one client (see _point_part); None when the point has nothing of this client."""
    if not point_id:
        return None
    links = {"orders": [r[0] for r in conn.execute("SELECT k.id FROM client_order l JOIN order_card k ON k.id = l.order_id "
                                                    "WHERE l.client_id = ? AND k.point_id = ? ORDER BY k.id DESC", (client_id, point_id))],
             "chats": [], "calls": [], "phones": set()}
    links["phones"] = {normalize(r[0]) for r in conn.execute("SELECT client_phone FROM order_card WHERE id IN (%s)" % ",".join("?" * len(links["orders"])),
                                                             links["orders"])} if links["orders"] else set()
    if account_id is not None:
        links["chats"] = [r[0] for r in conn.execute("SELECT a.id FROM client_chat l JOIN avito_chat a ON a.id = l.chat_row "
                                                      "WHERE l.client_id = ? AND a.account_id = ?", (client_id, account_id))]
        for call_row, phone in conn.execute("SELECT k.id, k.buyer_phone FROM client_call l JOIN avito_call k ON k.id = l.call_row "
                                            "WHERE l.client_id = ? AND k.account_id = ?", (client_id, account_id)):
            links["calls"].append(call_row)
            links["phones"].add(normalize(phone))
    own = own_phones(conn)
    links["phones"] = {p for p in links["phones"] if usable(p, own)}
    return links if links["orders"] or links["chats"] or links["calls"] else None


def list_clients_for_point(conn, point_id, account_id, query: str = "", limit: int = 200) -> List[dict]:
    """The clients the point has a part of, the newest first, with the point's own counts, phone and last touch; `query` finds the name or a
    phone the point knows (never a phone only another point or account knows)."""
    part = _point_part(conn, point_id, account_id)
    if not part:
        return []
    ids = sorted(part, reverse=True)
    names = dict(conn.execute(                            # the same selection in SQL: no list of ids bound (a big account has tens of thousands)
        "SELECT id, name FROM client WHERE merged_into IS NULL AND id IN (SELECT l.client_id FROM client_order l JOIN order_card k ON k.id = l.order_id "
        "WHERE k.point_id = ? UNION SELECT l.client_id FROM client_chat l JOIN avito_chat a ON a.id = l.chat_row WHERE a.account_id = ? "
        "UNION SELECT l.client_id FROM client_call l JOIN avito_call k ON k.id = l.call_row WHERE k.account_id = ?)", (point_id, account_id, account_id)))
    query = " ".join(str(query or "").split()).casefold()
    phone, digits = normalize(query), re.sub(r"\D", "", query)
    found = []
    for client_id in ids:
        if client_id not in names:
            continue
        links = part[client_id]
        if query and query not in (names[client_id] or "").casefold() \
                and not any((phone and phone in p) or (len(digits) >= 3 and digits in p) for p in links["phones"]):
            continue
        found.append({"id": client_id, "name": names[client_id], "phone": min(links["phones"]) if links["phones"] else "",
                      "orders": len(links["orders"]), "chats": len(links["chats"]), "calls": len(links["calls"]), "links": links})
        if len(found) >= limit:
            break
    for r in found:
        r["last"] = _last_touch(conn, r.pop("links"))
    return found


def _last_touch(conn, links: dict) -> str:
    def newest(sql, ids):
        return (conn.execute(sql % ",".join("?" * len(ids)), ids).fetchone()[0] or "") if ids else ""
    return max(newest("SELECT MAX(created_at) FROM order_card WHERE id IN (%s)", links["orders"]),
               newest("SELECT MAX(last_at) FROM avito_chat WHERE id IN (%s)", links["chats"]),
               newest("SELECT MAX(call_at) FROM avito_call WHERE id IN (%s)", links["calls"]))


def card_view_for_point(conn, client: dict, links: dict, accounts: Dict[int, str], chat_links: bool) -> tuple:
    """(the card, the client narrowed) as the point sees it: card_view built from the point's part only (`links`: point_part), with the sum of the
    sales of the point's orders; no tags, comment, car, inquiries nor journal (they are of the whole client)."""
    orders_kept, chats_kept, calls_kept = set(links["orders"]), set(links["chats"]), set(links["calls"])
    narrow = {"id": client["id"], "name": client["name"], "created_at": client["created_at"], "created_by": "", "car_brand": "", "car_model": "",
              "car_year": None, "note": "", "merged_into": None, "phones": sorted(links["phones"]), "peers": [],
              "orders": [o for o in client["orders"] if o["id"] in orders_kept], "chats": [h for h in client["chats"] if h["id"] in chats_kept],
              "calls": [dict(k, point_name="", order_id=k["order_id"] if k["order_id"] in orders_kept else None)      # the call's point and
                        for k in client.get("calls") or [] if k["id"] in calls_kept]}                                    # order: its own only
    view = card_view(conn, narrow, accounts, None, chat_links, whole=False)
    notes = ["Заказ №%d" % i for i in links["orders"]]
    bought = conn.execute("SELECT COALESCE(SUM(sale_total), 0) FROM sale WHERE source = 'order' AND reversed_at IS NULL AND note IN (%s)"
                          % ",".join("?" * len(notes)), notes).fetchone()[0] if notes else 0
    keep = ("Телефоны", "Заказов", "Чатов Авито", "Звонков")
    view["fields"] = [f for f in view["fields"] if f["label"] in keep]
    view["fields"].insert(1, {"label": "Куплено на", "value": round(float(bought), 2), "fmt": "money"})
    view.update(tags=[], inquiries=[], events=[])
    return view, narrow


def merged_from(conn, client_id) -> List[dict]:
    """The clients joined into this one, the newest join first: each can be split off again (unmerge)."""
    return [{"id": r[0], "name": r[1]} for r in conn.execute("SELECT id, name FROM client WHERE merged_into = ? ORDER BY id DESC", (client_id,))]


def client_of_call(conn, call_row) -> Optional[int]:
    row = conn.execute("SELECT client_id FROM client_call WHERE call_row = ?", (call_row,)).fetchone()
    return row[0] if row else None


def _roots(conn) -> Dict[int, int]:
    """Every client → the client it lives in now (itself, or the end of its chain of joins)."""
    into = dict(conn.execute("SELECT id, merged_into FROM client"))
    roots = {}
    for start in into:
        seen, at = set(), start
        while into.get(at) and at not in seen:
            seen.add(at)
            at = into[at]
        roots[start] = at
    return roots


def duplicates(conn, client_id=None) -> List[dict]:
    """Pairs of clients that look like one person by a sure sign, for a person to join or to wave off (never joined here): a phone of an
    order or a call of one is the phone of the other, a chat of one bound to an order of the other, a phone the client wrote in a chat is the
    other's (not one Avito person on two accounts: Avito gives the person another id in every account, 0 of 2606 on 2026-10-24). Our own and dummy numbers, joined clients and the pairs waved off are left out.
    Without `client_id`: all the pairs {"a", "b", "why"} (a < b); with it: that client's {"other", "why"}. Read only."""
    from .chat_deals import extract_phone                                   # the deals of the chats read the clients: imported here
    roots, own = _roots(conn), own_phones(conn)
    owner = {p: roots.get(c, c) for p, c in conn.execute("SELECT phone, client_id FROM client_phone")}
    found: Dict[tuple, List[str]] = defaultdict(list)

    def add(one, two, why):
        one, two = roots.get(one, one), roots.get(two, two)
        if one and two and one != two and why not in found[(min(one, two), max(one, two))]:
            found[(min(one, two), max(one, two))].append(why)

    def by_phone(client, raw, why):
        phone = usable(normalize(raw), own)
        if phone and phone in owner:
            add(client, owner[phone], why)

    for client, order_id, phone in conn.execute("SELECT l.client_id, c.id, c.client_phone FROM client_order l JOIN order_card c ON c.id = l.order_id"):
        by_phone(client, phone, "телефон в заказе №%d" % order_id)
    for client, phone, at in conn.execute("SELECT l.client_id, k.buyer_phone, k.call_at FROM client_call l JOIN avito_call k ON k.id = l.call_row"):
        by_phone(client, phone, "звонок %s" % str(at)[:16].replace("T", " "))
    for one, chat_row, order_id, two in conn.execute("SELECT l.client_id, a.id, a.order_id, o.client_id FROM client_chat l JOIN avito_chat a ON a.id = l.chat_row "
                                                     "JOIN client_order o ON o.order_id = a.order_id"):
        add(one, two, "чат Ч-%d привязан к заказу №%d" % (chat_row, order_id))
    chat_client = dict(conn.execute("SELECT chat_row, client_id FROM client_chat"))
    for one, order_id, ref in conn.execute("SELECT l.client_id, c.id, c.avito_chat FROM client_order l JOIN order_card c ON c.id = l.order_id WHERE c.avito_chat LIKE 'Ч-%'"):
        chat = re.match(r"^Ч-(\d+)$", ref)
        if chat and int(chat.group(1)) in chat_client:
            add(one, chat_client[int(chat.group(1))], "чат Ч-%s привязан к заказу №%d" % (chat.group(1), order_id))
    if client_id:                                    # the card: only the messages of its own chats (the page of the duplicates reads them all)
        mine = sorted(c for c, r in roots.items() if r == roots.get(client_id, client_id)) or [client_id]
        rows = conn.execute("SELECT l.client_id, m.chat_row, m.text FROM client_chat l JOIN avito_message m ON m.chat_row = l.chat_row "
                            "WHERE m.direction = 'in' AND m.kind = 'text' AND l.client_id IN (%s)" % ",".join("?" * len(mine)), mine)
    else:
        rows = conn.execute("SELECT l.client_id, m.chat_row, m.text FROM client_chat l JOIN avito_message m ON m.chat_row = l.chat_row "
                            "WHERE m.direction = 'in' AND m.kind = 'text'")
    for client, chat_row, text in rows:
        by_phone(client, extract_phone(text) or "", "телефон в чате Ч-%d" % chat_row)
    skipped = {(min(roots.get(a, a), roots.get(b, b)), max(roots.get(a, a), roots.get(b, b))) for a, b in conn.execute("SELECT a, b FROM client_pair_skip")}
    pairs = {k: v for k, v in found.items() if v and k not in skipped}
    if client_id is None:
        return [{"a": a, "b": b, "why": why} for (a, b), why in sorted(pairs.items())]
    me = roots.get(client_id, client_id)
    return [{"other": b if a == me else a, "why": why} for (a, b), why in sorted(pairs.items(), key=lambda kv: kv[0][1] if kv[0][0] == me else kv[0][0])
            if me in (a, b)]


def client_of_order(conn, order_id) -> Optional[dict]:
    row = conn.execute("SELECT c.id, c.name FROM client_order l JOIN client c ON c.id = l.client_id WHERE l.order_id = ?", (order_id,)).fetchone()
    return {"id": row[0], "name": row[1]} if row else None


def client_of_chat(conn, chat_row) -> Optional[dict]:
    row = conn.execute("SELECT c.id, c.name FROM client_chat l JOIN client c ON c.id = l.client_id WHERE l.chat_row = ?", (chat_row,)).fetchone()
    return {"id": row[0], "name": row[1]} if row else None


# ---- writing (no commit here unless said: the caller saves together with its own change) -------------------------------------------

def _new_client(conn, name: str, now: str, author: str) -> int:
    return conn.execute("INSERT INTO client (name, note, created_at, created_by) VALUES (?, '', ?, ?)",
                        (str(name or "").strip()[:NAME_MAX], now, author)).lastrowid


def _by_phone(conn, phone: str) -> Optional[int]:
    row = conn.execute("SELECT client_id FROM client_phone WHERE phone = ?", (phone,)).fetchone() if phone else None
    return row[0] if row else None


def _by_peer(conn, account_id, peer_id) -> Optional[int]:
    if not peer_id:
        return None
    row = conn.execute("SELECT client_id FROM client_peer WHERE account_id = ? AND peer_id = ?", (account_id, str(peer_id))).fetchone()
    return row[0] if row else None


def _add_phone(conn, client_id: int, phone: str) -> None:
    if phone:
        conn.execute("INSERT OR IGNORE INTO client_phone (phone, client_id) VALUES (?, ?)", (phone, client_id))


def _add_peer(conn, client_id: int, account_id, peer_id) -> None:
    if peer_id:
        conn.execute("INSERT OR IGNORE INTO client_peer (account_id, peer_id, client_id) VALUES (?, ?, ?)", (account_id, str(peer_id), client_id))


def _detached(conn, order_id) -> bool:
    """The order stands unlinked because its client refused it and the goods went to «На ОТХ» (order_oth): never joined automatically."""
    return conn.execute("SELECT 1 FROM order_oth_client WHERE order_id = ?", (order_id,)).fetchone() is not None


def _link_order(conn, client_id: int, order_id: int, how: str, now: str) -> None:
    if _detached(conn, order_id):
        return
    conn.execute("INSERT OR IGNORE INTO client_order (order_id, client_id, how, at) VALUES (?, ?, ?, ?)", (order_id, client_id, how, now))


def _link_chat(conn, client_id: int, chat_row: int, how: str, now: str) -> None:
    conn.execute("INSERT OR IGNORE INTO client_chat (chat_row, client_id, how, at) VALUES (?, ?, ?, ?)", (chat_row, client_id, how, now))


def attach_order(conn, order_id: int, now: str, author: str) -> Optional[int]:
    """A new order (or one that has just got a phone) joins the client of its phone; a phone nobody has makes a new client. An order already
    linked only gives its client a new phone; one without a phone waits. No commit: the order's own saving commits it."""
    if _detached(conn, order_id):
        return None
    row = conn.execute("SELECT client_name, client_phone, avito_chat FROM order_card WHERE id = ?", (order_id,)).fetchone()
    linked = conn.execute("SELECT client_id FROM client_order WHERE order_id = ?", (order_id,)).fetchone()
    own = own_phones(conn)
    if linked:                                                                  # a new phone of the order is the client's too, unless another has it
        phone = usable(normalize(row[1]), own) if row else ""
        if phone and _by_phone(conn, phone) is None:
            _add_phone(conn, linked[0], phone)
        return None
    phone = usable(normalize(row[1]), own) if row else ""
    chat = re.match(r"^Ч-(\d+)$", row[2] or "") if row else None                 # a deal of a chat (chat_deals.ref): its Avito peer comes first
    peer = conn.execute("SELECT account_id, peer_id FROM avito_chat WHERE id = ?", (int(chat.group(1)),)).fetchone() if chat else None
    client_id = _by_peer(conn, *peer) if peer else None
    if client_id is None and not phone:
        return None
    client_id = client_id or _by_phone(conn, phone) or _new_client(conn, row[0], now, author)
    if _by_phone(conn, phone) in (None, client_id):
        _add_phone(conn, client_id, phone)
    _link_order(conn, client_id, order_id, "phone", now)
    return client_id


def attach_chat(conn, chat_row: int, order_id: Optional[int], now: str, author: str) -> Optional[int]:
    """A deal made from a chat: the client of the Avito peer, else the client of the order, else a new one; the chat, the peer and the order
    join it. No commit."""
    chat = conn.execute("SELECT account_id, peer_id, peer_name FROM avito_chat WHERE id = ?", (chat_row,)).fetchone()
    if chat is None or conn.execute("SELECT 1 FROM client_chat WHERE chat_row = ?", (chat_row,)).fetchone():
        return None
    account_id, peer_id, peer_name = chat
    of_order = client_of_order(conn, order_id) if order_id else None
    client_id = _by_peer(conn, account_id, peer_id) or (of_order["id"] if of_order else None) or _new_client(conn, peer_name, now, author)
    _add_peer(conn, client_id, account_id, peer_id)
    _link_chat(conn, client_id, chat_row, "chat", now)
    if order_id and of_order is None:
        phone = usable(normalize(conn.execute("SELECT client_phone FROM order_card WHERE id = ?", (order_id,)).fetchone()[0]), own_phones(conn))
        if phone and _by_phone(conn, phone) not in (None, client_id):        # the phone is another client's: the person decides, not us
            return client_id
        _add_phone(conn, client_id, phone)
        _link_order(conn, client_id, order_id, "chat", now)
    return client_id


def attach_call(conn, call_row: int, now: str, author: str, own: Optional[set] = None) -> Optional[int]:
    """A call through the Avito substitute number: the client of its number, else a new client with that number (the owner 2026-10-21: a
    number we talked with goes to the base at once). Our own numbers and a hidden one join nobody. No commit."""
    row = conn.execute("SELECT buyer_phone FROM avito_call WHERE id = ?", (call_row,)).fetchone()
    if row is None or conn.execute("SELECT 1 FROM client_call WHERE call_row = ?", (call_row,)).fetchone():
        return None
    phone = usable(normalize(row[0]), own if own is not None else own_phones(conn))
    if not phone:
        return None
    client_id = _by_phone(conn, phone)
    if client_id is None:
        client_id = _new_client(conn, "", now, author)
        _add_phone(conn, client_id, phone)
    conn.execute("INSERT OR IGNORE INTO client_call (call_row, client_id, how, at) VALUES (?, ?, 'phone', ?)", (call_row, client_id, now))
    return client_id


def _car(car: dict, now: str) -> tuple:
    """The car typed in the card, cleaned: (brand, model, year or None). A year is from 1950 to the next one."""
    brand, model = (" ".join(str(car.get(k) or "").split())[:CAR_MAX] for k in ("brand", "model"))
    year = str(car.get("year") or "").strip()
    if not year:
        return brand, model, None
    last = int(now[:4]) + 1 if now[:4].isdigit() else 2100
    if not year.isdigit() or not CAR_YEAR_FIRST <= int(year) <= last:
        raise ClientError("Год автомобиля: число от %d до %d." % (CAR_YEAR_FIRST, last))
    return brand, model, int(year)


def save(conn, client_id, name: str, note: str, now: str, author: str, car: Optional[dict] = None) -> None:
    """The name, the comment and, when `car` is given ({"brand", "model", "year"}), the car."""
    client = get_client(conn, client_id)
    name, note = str(name or "").strip()[:NAME_MAX], str(note or "").strip()[:NOTE_MAX]
    if car is not None:
        conn.execute("UPDATE client SET car_brand = ?, car_model = ?, car_year = ? WHERE id = ?", _car(car, now) + (client["id"],))
    conn.execute("UPDATE client SET name = ?, note = ? WHERE id = ?", (name, note, client["id"]))
    store.write_audit(conn, now, author, "client_saved", {"client": client["id"]})


def set_tag(conn, client_id, tag: str, on: bool, now: str, author: str) -> None:
    """A tag typed by hand on or off. «передан точкой» is never typed: it comes from the handoffs."""
    client = get_client(conn, client_id)
    tag = " ".join(str(tag or "").split())
    if not tag or len(tag) > TAG_MAX:
        raise ClientError("Тег: от 1 до %d символов." % TAG_MAX)
    if tag.casefold() == HANDED.casefold():
        raise ClientError("Тег «%s» ставится сам, когда точка передаёт клиента." % HANDED)
    used = tags_of(conn, client["id"]) + [PRAGMATIC] + [r[0] for r in conn.execute("SELECT DISTINCT tag FROM client_tag")]
    tag = next((t for t in used if t.casefold() == tag.casefold()), tag)          # «опт» and «Опт» are one tag: the spelling in use
    if on:
        if tag in tags_of(conn, client["id"]):
            return
        if len(tags_of(conn, client["id"])) >= TAGS_MAX:
            raise ClientError("У клиента уже %d тегов: уберите лишний." % TAGS_MAX)
        conn.execute("INSERT INTO client_tag (client_id, tag, at, author) VALUES (?, ?, ?, ?)", (client["id"], tag, now, author))
    elif not conn.execute("DELETE FROM client_tag WHERE client_id = ? AND tag = ?", (client["id"], tag)).rowcount:
        return
    store.write_audit(conn, now, author, "client_tagged", {"client": client["id"], "tag": tag, "on": bool(on)})


def _not_same(conn, one: int, two: int, now: str, author: str) -> None:
    conn.execute("INSERT OR IGNORE INTO client_pair_skip (a, b, at, author) VALUES (?, ?, ?, ?)", (min(one, two), max(one, two), now, author))


def skip_pair(conn, one, two, now: str, author: str) -> None:
    """«Не тот же»: the pair is never proposed to join again (a person may still join them by the number)."""
    a, b = sorted((get_client(conn, one)["id"], get_client(conn, two)["id"]))
    if a == b:
        raise ClientError("Это один и тот же клиент.")
    _not_same(conn, a, b, now, author)
    store.write_audit(conn, now, author, "client_pair_skipped", {"client": int(one), "to": int(two)})


_KEPT = ("name", "note", "car_brand", "car_model", "car_year")             # what a join may change in the client kept; the undoing gives it back


def merge(conn, keep_id, gone_id, now: str, author: str) -> None:
    """Everything of `gone_id` (orders, chats, calls, phones, peers) goes to `keep_id`, its tags too, and its car when `keep_id` has none;
    `gone_id` stays, marked «объединён в», so links to it lead on. The journal keeps what moved and what the kept client was: unmerge()."""
    keep, gone = get_client(conn, keep_id), get_client(conn, gone_id)
    if keep["id"] == gone["id"]:
        raise ClientError("Это один и тот же клиент.")
    if keep["merged_into"] or gone["merged_into"]:
        raise ClientError("Клиент уже объединён с другим.")
    moved = {"orders": [o["id"] for o in gone["orders"]], "chats": [h["id"] for h in gone["chats"]], "phones": gone["phones"],
             "peers": ["%s:%s" % (p["account_id"], p["peer_id"]) for p in gone["peers"]], "calls": [k["id"] for k in gone.get("calls") or []]}
    links = {"orders": [o["id"] for o in keep["orders"]], "chats": [h["id"] for h in keep["chats"]], "calls": [k["id"] for k in keep.get("calls") or []]}
    for table in ("client_order", "client_chat", "client_call", "client_phone", "client_peer"):
        conn.execute("UPDATE %s SET client_id = ? WHERE client_id = ?" % table, (keep["id"], gone["id"]))
    note = "\n".join(t for t in (keep["note"], gone["note"]) if t)[:NOTE_MAX]
    conn.execute("UPDATE client SET note = ?, name = CASE WHEN name = '' THEN ? ELSE name END WHERE id = ?", (note, gone["name"], keep["id"]))
    if not car_text(keep) and car_text(gone):
        conn.execute("UPDATE client SET car_brand = ?, car_model = ?, car_year = ? WHERE id = ?",
                     (gone["car_brand"], gone["car_model"], gone["car_year"], keep["id"]))
    had = {t.casefold() for t in tags_of(conn, keep["id"])}
    added = [t for t in tags_of(conn, gone["id"]) if t.casefold() not in had]          # the gone client keeps its own rows: the undoing needs nothing back
    conn.executemany("INSERT INTO client_tag (client_id, tag, at, author) VALUES (?, ?, ?, ?)", [(keep["id"], t, now, author) for t in added])
    conn.execute("UPDATE client SET merged_into = ? WHERE id = ?", (keep["id"], gone["id"]))
    after = get_client(conn, keep["id"])
    store.write_audit(conn, now, author, "client_merged", dict({"into": keep["id"], "from": gone["id"]}, **moved, tags_added=added, keep_had=links,
                                                                keep_before={k: keep[k] for k in _KEPT}, keep_after={k: after[k] for k in _KEPT}))


def unmerge(conn, keep_id, gone_id, now: str, author: str) -> None:
    """Undo the join of `gone_id` into `keep_id` by its journal: back to `gone_id` go what moved and still is with `keep_id` (orders, chats,
    calls, phones, peers) and what joined `keep_id` by itself since by those phones and peers; the tags the join added leave `keep_id`; the
    name, the comment and the car of `keep_id` are given back unless changed since. Links a person made since stay. The last join first:
    a client joined into a third one is split off there first."""
    keep, gone = get_client(conn, keep_id), get_client(conn, gone_id)
    if gone["merged_into"] != keep["id"]:
        raise ClientError("Клиент №%d не присоединён к этому." % gone["id"])
    if keep["merged_into"]:
        raise ClientError("Этот клиент сам присоединён к №%d: сначала отсоедините его там." % keep["merged_into"])
    joins = conn.execute("SELECT MAX(a.id), json_extract(a.detail, '$.from') FROM audit a JOIN client c ON c.id = json_extract(a.detail, '$.from') "
                         "WHERE a.action = 'client_merged' AND json_extract(a.detail, '$.into') = ? AND c.merged_into = ? GROUP BY 2 ORDER BY 1 DESC",
                         (keep["id"], keep["id"])).fetchall()                 # the clients joined into keep, the latest join first
    if not any(j[1] == gone["id"] for j in joins):
        raise ClientError("В журнале нет записи об этом объединении: отделите заказы и чаты по одному.")
    if joins[0][1] != gone["id"]:                   # a later join may hold links of this one: undo the joins from the latest
        raise ClientError("Сначала разъедините клиента №%s: он присоединён позже." % joins[0][1])
    d = json.loads(conn.execute("SELECT detail FROM audit WHERE id = ?", (joins[0][0],)).fetchone()[0])
    k, g = keep["id"], gone["id"]
    phones = [p for p in d.get("phones") or [] if _by_phone(conn, p) == k]
    peers = [p for p in d.get("peers") or [] if _by_peer(conn, int(p.split(":", 1)[0]), p.split(":", 1)[1]) == k]
    own = own_phones(conn)
    had = d.get("keep_had")                         # the links of `keep_id` before the join; a journal without it: only what moved goes back
    joined = {key: set(had.get(key) or []) | set(d.get(key) or []) for key in ("orders", "chats", "calls")} if had is not None else None

    def since(key, rows):                           # the links made after the join, by themselves (a person's links never move)
        return [r for r in rows if joined is not None and r[0] not in joined[key]]

    chats = set(d.get("chats") or []) | {r[0] for r in since("chats", conn.execute(
        "SELECT a.id, a.account_id || ':' || a.peer_id FROM client_chat l JOIN avito_chat a ON a.id = l.chat_row WHERE l.client_id = ? AND l.how = 'chat'", (k,)))
        if r[1] in peers}
    chats = {c for c in chats if conn.execute("SELECT 1 FROM client_chat WHERE chat_row = ? AND client_id = ?", (c, k)).fetchone()}
    calls = set(d.get("calls") or []) | {r[0] for r in since("calls", conn.execute(
        "SELECT k.id, k.buyer_phone FROM client_call l JOIN avito_call k ON k.id = l.call_row WHERE l.client_id = ? AND l.how = 'phone'", (k,)))
        if usable(normalize(r[1]), own) in phones}
    orders = set(d.get("orders") or [])
    for order_id, phone, ref in since("orders", conn.execute("SELECT c.id, c.client_phone, c.avito_chat FROM client_order l JOIN order_card c ON c.id = l.order_id "
                                                             "WHERE l.client_id = ? AND l.how IN ('phone', 'chat')", (k,)).fetchall()):
        chat = re.match(r"^Ч-(\d+)$", ref or "")
        peer = conn.execute("SELECT account_id, peer_id FROM avito_chat WHERE id = ?", (int(chat.group(1)),)).fetchone() if chat else None
        of_chats = {r[0] for r in conn.execute("SELECT id FROM avito_chat WHERE order_id = ?", (order_id,))} | ({int(chat.group(1))} if chat else set())
        if (peer and "%s:%s" % peer in peers) or (not peer and usable(normalize(phone), own) in phones) or of_chats & chats:   # the person first, as attach_order
            orders.add(order_id)
    back = {}
    for table, key, ids in (("client_order", "order_id", orders), ("client_chat", "chat_row", chats), ("client_call", "call_row", calls)):
        back[table] = sorted(i for i in ids if conn.execute("UPDATE %s SET client_id = ? WHERE %s = ? AND client_id = ?" % (table, key), (g, i, k)).rowcount)
    for phone in phones:
        conn.execute("UPDATE client_phone SET client_id = ? WHERE phone = ? AND client_id = ?", (g, phone, k))
    for peer in peers:
        account_id, peer_id = peer.split(":", 1)
        conn.execute("UPDATE client_peer SET client_id = ? WHERE account_id = ? AND peer_id = ? AND client_id = ?", (g, int(account_id), peer_id, k))
    conn.executemany("DELETE FROM client_tag WHERE client_id = ? AND tag = ?", [(k, t) for t in d.get("tags_added") or []])
    before, after = d.get("keep_before"), d.get("keep_after")
    if before and after and all(keep[f] == after.get(f) for f in _KEPT):
        conn.execute("UPDATE client SET %s WHERE id = ?" % ", ".join("%s = ?" % f for f in _KEPT), [before.get(f) for f in _KEPT] + [k])
    conn.execute("UPDATE client SET merged_into = NULL WHERE id = ?", (g,))
    _not_same(conn, k, g, now, author)                                      # a person split them: never proposed to join again
    store.write_audit(conn, now, author, "client_unmerged", {"client": k, "to": g, "orders": back["client_order"], "chats": back["client_chat"],
                                                             "calls": back["client_call"], "phones": phones, "peers": peers})


def split_order(conn, order_id, now: str, author: str) -> int:
    """The order becomes a client of its own (a person decided: the automatic joining never moves it back). Its phone stays with the old client."""
    old = client_of_order(conn, order_id)
    if old is None:
        raise ClientError("Заказ не привязан к клиенту.")
    row = conn.execute("SELECT client_name FROM order_card WHERE id = ?", (order_id,)).fetchone()
    new_id = _new_client(conn, row[0] if row else "", now, author)
    conn.execute("UPDATE client_order SET client_id = ?, how = 'split', at = ? WHERE order_id = ?", (new_id, now, order_id))
    _not_same(conn, old["id"], new_id, now, author)                         # the phone stays with the old client: no proposal to join them back
    store.write_audit(conn, now, author, "client_split", {"from": old["id"], "to": new_id, "order": int(order_id)})
    return new_id


def split_chat(conn, chat_row, now: str, author: str) -> int:
    """The chat becomes a client of its own, with its Avito peer."""
    old = client_of_chat(conn, chat_row)
    if old is None:
        raise ClientError("Чат не привязан к клиенту.")
    row = conn.execute("SELECT account_id, peer_id, peer_name FROM avito_chat WHERE id = ?", (chat_row,)).fetchone()
    new_id = _new_client(conn, row[2] if row else "", now, author)
    conn.execute("UPDATE client_chat SET client_id = ?, how = 'split', at = ? WHERE chat_row = ?", (new_id, now, chat_row))
    _not_same(conn, old["id"], new_id, now, author)
    if row:
        conn.execute("UPDATE client_peer SET client_id = ? WHERE account_id = ? AND peer_id = ?", (new_id, row[0], row[1]))
    store.write_audit(conn, now, author, "client_split", {"from": old["id"], "to": new_id, "chat": int(chat_row)})
    return new_id


# ---- joining the old data (the command clients-link) -------------------------------------------------------------------------------

class _Union:
    def __init__(self):
        self.parent: Dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def join(self, a: str, b: str) -> None:
        self.parent[self.find(a)] = self.find(b)


def plan_links(conn) -> dict:
    """What the joining would do, read only: groups of orders and chats with a common phone, a common Avito peer or a chat bound to an
    order; a group with one client gets the rest, a group with none gets a new client, a group with several clients is left for a person."""
    u = _Union()
    orders = conn.execute("SELECT id, client_name, client_phone, created_at FROM order_card").fetchall()
    chats = conn.execute("SELECT id, account_id, peer_id, peer_name, order_id FROM avito_chat").fetchall()
    order_client = dict(conn.execute("SELECT order_id, client_id FROM client_order"))
    chat_client = dict(conn.execute("SELECT chat_row, client_id FROM client_chat"))
    manual_orders = {r[0] for r in conn.execute("SELECT order_id FROM client_order WHERE how IN ('manual', 'split')")}
    detached = {r[0] for r in conn.execute("SELECT order_id FROM order_oth_client")}        # refused, on «На ОТХ»: nobody's, never joined
    orders = [r for r in orders if r[0] not in detached]
    manual_chats = {r[0] for r in conn.execute("SELECT chat_row FROM client_chat WHERE how IN ('manual', 'split')")}
    known_orders = {r[0] for r in orders}
    own = own_phones(conn)
    for order_id, _, phone, _ in orders:
        u.find("o%d" % order_id)
        if order_id in order_client:
            u.join("o%d" % order_id, "c%d" % order_client[order_id])
        if order_id not in manual_orders and usable(normalize(phone), own):
            u.join("o%d" % order_id, "p" + normalize(phone))
    for chat_row, account_id, peer_id, _, order_id in chats:
        u.find("h%d" % chat_row)
        if chat_row in chat_client:
            u.join("h%d" % chat_row, "c%d" % chat_client[chat_row])
        if chat_row in manual_chats:
            continue
        if peer_id:
            u.join("h%d" % chat_row, "a%s:%s" % (account_id, peer_id))
        if order_id in known_orders and order_id not in manual_orders:
            u.join("h%d" % chat_row, "o%d" % order_id)
    for phone, client_id in conn.execute("SELECT phone, client_id FROM client_phone"):
        u.join("p" + phone, "c%d" % client_id)
    for account_id, peer_id, client_id in conn.execute("SELECT account_id, peer_id, client_id FROM client_peer"):
        u.join("a%s:%s" % (account_id, peer_id), "c%d" % client_id)
    groups: Dict[str, List[str]] = defaultdict(list)
    for node in list(u.parent):
        groups[u.find(node)].append(node)
    names = {"o%d" % r[0]: (r[3] or "", r[1]) for r in orders}
    names.update({"h%d" % r[0]: ("", r[3]) for r in chats})
    plan = {"new_clients": [], "to_existing": [], "conflicts": [], "large": [], "orders": 0, "chats": 0}
    for members in groups.values():
        clients = sorted({int(m[1:]) for m in members if m[0] == "c"})
        free_orders = sorted(int(m[1:]) for m in members if m[0] == "o" and int(m[1:]) not in order_client)
        free_chats = sorted(int(m[1:]) for m in members if m[0] == "h" and int(m[1:]) not in chat_client)
        if not free_orders and not free_chats:
            continue
        phones = sorted(m[1:] for m in members if m[0] == "p")
        peers = sorted(m[1:] for m in members if m[0] == "a")
        has_order = clients or any(m[0] == "o" for m in members)
        if not has_order:
            continue                                                  # chats without a deal (questions, spam): no client until a deal
        if not phones and not peers and len(free_orders) + len(free_chats) <= 1 and not clients:
            continue                                                  # a lone order without a phone (no chat): nothing to join it by
        entry = {"orders": free_orders, "chats": free_chats, "phones": phones, "peers": peers}
        names_seen = {names[m][1].strip().lower() for m in members if m in names and names[m][1].strip()}
        if len(free_orders) + len(free_chats) > 5 or len(names_seen) > 2:            # worth a look before --confirm: one person or a shared number?
            plan["large"].append(dict(entry, names=sorted(names_seen)))
        if len(clients) > 1:
            plan["conflicts"].append(dict(entry, clients=clients))
            continue
        if clients:
            plan["to_existing"].append(dict(entry, client=clients[0]))
        else:
            latest = max((names[m] for m in members if m in names and names[m][1]), default=("", ""))
            plan["new_clients"].append(dict(entry, name=latest[1]))
        plan["orders"] += len(free_orders)
        plan["chats"] += len(free_chats)
    return plan


def apply_links(conn, plan: dict, now: str, author: str = "cli") -> dict:
    """Write a plan of plan_links in one transaction (all or nothing). Never changes an order or a chat."""
    made = {"clients": 0, "orders": 0, "chats": 0}
    try:
        for entry in plan["new_clients"] + plan["to_existing"]:
            client_id = entry.get("client") or _new_client(conn, entry.get("name", ""), now, author)
            made["clients"] += 0 if entry.get("client") else 1
            for phone in entry["phones"]:
                if _by_phone(conn, phone) in (None, client_id):
                    _add_phone(conn, client_id, phone)
            for peer in entry["peers"]:
                account_id, peer_id = peer.split(":", 1)
                if _by_peer(conn, int(account_id), peer_id) in (None, client_id):
                    _add_peer(conn, client_id, int(account_id), peer_id)
            for order_id in entry["orders"]:
                _link_order(conn, client_id, order_id, "phone", now)
                made["orders"] += 1
            for chat_row in entry["chats"]:
                _link_chat(conn, client_id, chat_row, "chat", now)
                made["chats"] += 1
        store.write_audit(conn, now, author, "clients_linked", dict(made, conflicts=len(plan["conflicts"])))      # commits
    except Exception:
        conn.rollback()
        raise
    return made
