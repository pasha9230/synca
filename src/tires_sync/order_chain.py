"""«Цепочка заказов» (the owner 2026-11-19, task #83 of the spec): the orders of one client tied together, NOT merged. Each stays a card
of its own (its tyres, stage, chat, money); the card names the others of its chain, each a link to open it. A person ties an order to
another in its card (as a chat is bound), unties it there; «Добавить заказ» of an order or of a client makes a new order already tied.

A chain is a number shared by its orders (order_chain.chain): tying two chains makes one, an order untied leaves it, a chain of one order
is no chain. A deleted order drops out by itself (its card is gone); restored, it is back in its chain. Nothing is sent to anybody."""
from __future__ import annotations

import json
from typing import List, Optional

from . import orders

GREY = "grey"


def _chain(conn, order_id: int) -> Optional[int]:
    row = conn.execute("SELECT chain FROM order_chain WHERE order_id = ?", (order_id,)).fetchone()
    return row[0] if row else None


def _label(card: dict) -> str:
    return str(card.get("webmim_number") or card["id"])


def others(conn, order_id: int) -> List[dict]:
    """The other orders of the chain of `order_id`, oldest first: id, label (WebMIM's number, else the panel's), stage, created."""
    chain = _chain(conn, order_id)
    if chain is None:
        return []
    rows = conn.execute("SELECT c.id, c.webmim_number, s.name, c.created_at FROM order_chain l JOIN order_card c ON c.id = l.order_id "
                        "JOIN order_stage s ON s.id = c.stage_id WHERE l.chain = ? AND l.order_id != ? ORDER BY c.id", (chain, order_id)).fetchall()
    return [{"id": r[0], "label": str(r[1] or r[0]), "stage": r[2], "created": r[3]} for r in rows]


def find(conn, text: str) -> Optional[int]:
    """The order a person typed: WebMIM's number first (unique, what everybody knows), else the panel's; «№», spaces and «О» are ignored."""
    typed = str(text or "").replace("№", "").strip()
    if not typed:
        return None
    row = conn.execute("SELECT id FROM order_card WHERE webmim_number = ? ORDER BY id LIMIT 1", (typed,)).fetchone()
    if row is None and typed.isdigit():
        row = conn.execute("SELECT id FROM order_card WHERE id = ?", (int(typed),)).fetchone()
    return row[0] if row else None


def link(conn, order_id: int, other_id: int, now: str, author: str, commit: bool = True) -> None:
    """Tie `other_id` (and its chain) to the chain of `order_id`. A record in every order that joined, one line of the audit."""
    if order_id == other_id:
        raise orders.OrderError("Это тот же заказ.")
    card, other = orders.get_card(conn, order_id), orders.get_card(conn, other_id)     # OrderError: no such order
    mine, theirs = _chain(conn, order_id), _chain(conn, other_id)
    if mine is not None and mine == theirs:
        raise orders.OrderError("Заказ №%s уже в этой цепочке." % _label(other))
    chain = mine if mine is not None else (theirs if theirs is not None else min(order_id, other_id))
    before = set(_members(conn, chain))
    for oid in (order_id, other_id):
        if _chain(conn, oid) is None:
            conn.execute("INSERT INTO order_chain (order_id, chain, at, author) VALUES (?,?,?,?)", (oid, chain, now, author))
    if theirs is not None and theirs != chain:
        conn.execute("UPDATE order_chain SET chain = ? WHERE chain = ?", (chain, theirs))
    members = _members(conn, chain)
    for oid in members:
        if oid in before:
            joined = [m for m in members if m not in before]
        else:
            joined = [m for m in members if m != oid]
        names = ", ".join("№%s" % _label(orders._row(conn, m)) for m in joined)
        orders.add_event(conn, oid, now, author, "chain", "Связан с заказом %s (цепочка заказов клиента)" % names, level=GREY)
    conn.execute("INSERT INTO audit (at, author, action, detail) VALUES (?,?,?,?)",
                 (now, author, "order_chain_link", json.dumps({"order": order_id, "other": other_id, "chain": members})))
    if commit:
        conn.commit()


def unlink(conn, order_id: int, now: str, author: str, commit: bool = True) -> bool:
    """Take `order_id` out of its chain; a chain left with one order is no chain. False: it was in none."""
    chain = _chain(conn, order_id)
    if chain is None:
        return False
    card = orders._row(conn, order_id)
    rest = [m for m in _members(conn, chain) if m != order_id]
    conn.execute("DELETE FROM order_chain WHERE order_id = ?", (order_id,))
    if len(rest) < 2:
        conn.execute("DELETE FROM order_chain WHERE chain = ?", (chain,))
    orders.add_event(conn, order_id, now, author, "chain", "Отвязан от цепочки заказов %s" % ", ".join("№%s" % _label(orders._row(conn, m)) for m in rest), level=GREY)
    for m in rest:
        orders.add_event(conn, m, now, author, "chain", "Заказ №%s отвязан от цепочки" % _label(card), level=GREY)
    conn.execute("INSERT INTO audit (at, author, action, detail) VALUES (?,?,?,?)",
                 (now, author, "order_chain_unlink", json.dumps({"order": order_id, "rest": rest})))
    if commit:
        conn.commit()
    return True


def _members(conn, chain: int) -> List[int]:
    """The orders of the chain that are there (a deleted card drops out), oldest first."""
    return [r[0] for r in conn.execute("SELECT l.order_id FROM order_chain l JOIN order_card c ON c.id = l.order_id WHERE l.chain = ? ORDER BY l.order_id",
                                       (chain,)).fetchall()]


COPIED = ("client_name", "client_phone", "point_id", "responsible", "source")
MADE_FROM = "Создан из заказа №%s: тот же клиент"
TWIN_SECONDS = 60                                             # a second press of «Новый заказ этому клиенту» this soon is the same order


def _just_made(conn, made: str, now: str, besides: int) -> Optional[int]:
    """The order «Новый заказ этому клиенту» of the same order made in the last TWIN_SECONDS (a double press, a second tab), if any."""
    from datetime import datetime, timedelta
    since = (datetime.fromisoformat(now) - timedelta(seconds=TWIN_SECONDS)).isoformat(timespec="seconds")
    row = conn.execute("SELECT e.order_id FROM order_event e JOIN order_card c ON c.id = e.order_id WHERE e.kind = 'chain' AND e.text = ? "
                       "AND e.at >= ? AND e.order_id != ? ORDER BY e.order_id DESC LIMIT 1", (made, since, besides)).fetchone()
    return row[0] if row else None


def again(conn, order_id: int, now: str, author: str) -> int:
    """«Добавить заказ» of an order (the owner 2026-11-19): a new order of the same client — the name, the phone, the point, the responsible
    and the source copied, no tyres, no chat — on the first stage, tied to the chain. An open order of the same phone does not stop it:
    the person asked for a second order (create_card's many_open); the guard of a double press stays on every other way to make one."""
    card = orders.get_card(conn, order_id)
    fields = {k: card[k] for k in COPIED if card.get(k) not in (None, "")}
    made = MADE_FROM % _label(card)
    try:
        new = orders.create_card(conn, fields, now, author, require_contact=False, commit=False, many_open=True)
        twin = _just_made(conn, made, now, besides=new)       # asked after the insert: the write holds the lock, two presses make one order
        if twin is not None:
            conn.rollback()
            return twin
        orders.add_event(conn, new, now, author, "chain", made, level=GREY)
        link(conn, order_id, new, now, author, commit=False)
    except Exception:
        conn.rollback()                                       # all or nothing: no order without its chain
        raise
    conn.commit()
    orders._fire_rules(conn, new, now, ())                    # the rules of the first stage, as create_card's own commit fires them
    return new


def for_client(conn, client_id: int, now: str, author: str) -> int:
    """«Добавить заказ» of a client's card in CRM: «Добавить заказ» of the client's newest order (tied to its chain); a client without an
    order gets a plain new order of the name and the first phone (the usual guard of one open order holds: there is none of theirs)."""
    row = conn.execute("SELECT l.order_id FROM client_order l JOIN order_card c ON c.id = l.order_id WHERE l.client_id = ? ORDER BY c.id DESC LIMIT 1",
                       (client_id,)).fetchone()
    if row is not None:
        return again(conn, row[0], now, author)
    client = conn.execute("SELECT name FROM client WHERE id = ?", (client_id,)).fetchone()
    if client is None:
        raise orders.OrderError("Такого клиента нет.")
    phone = conn.execute("SELECT phone FROM client_phone WHERE client_id = ? ORDER BY rowid LIMIT 1", (client_id,)).fetchone()
    return orders.create_card(conn, {"client_name": client[0], "client_phone": phone[0] if phone else ""}, now, author, require_contact=False)
