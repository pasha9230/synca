"""«История точки» (the owner 2026-10-29): what the points did — accepted the goods, handed a client over, cancelled a handoff, gave the
tyres back, signed the act МХ-3, moved an order — with the time and the author, for the owner and the partner.

No table of its own: the panel writes every action of a point as a record of the journal of actions (audit, action «point_action», the
point and the order in its detail); the commands a point wrote in VK are read from the log of the commands (order_command_log, the sender
is the point's VK). Writing never stops the action: a busy base loses the record, never the press."""
from __future__ import annotations

import json
import logging
from typing import List, Optional

from . import order_points as points

ACTION = "point_action"
NAMES = {"arrived": "принял заказ (приехал)", "issued": "выдал заказ", "no_show": "клиент не пришёл", "returned": "отдал на возврат",
         "reserve_issued": "выдал (ответ на резерв)", "reserve_here": "на точке (ответ на резерв)", "handoff": "передал клиента",
         "handoff_cancel": "отменил передачу", "act_signed": "подписал акт МХ-3 и отдал", "to_oth": "оставил шины на ОТХ", "move": "сменил этап", "vk": "команда ВК"}
SHOWN = 300


def record(conn, now: str, author: str, point_id, action: str, order_id: Optional[int], text: str = "") -> None:
    """One action of a point; committed on its own (store.write_audit's way). Never raises."""
    try:
        conn.execute("INSERT INTO audit (at, author, action, detail) VALUES (?,?,?,?)", (now, author, ACTION, json.dumps(
            {"point": int(point_id), "name": _name(conn, point_id), "kind": action, "order": int(order_id) if order_id else None, "text": str(text or "")[:300]},
            ensure_ascii=False)))
        conn.commit()
    except Exception:                                                     # noqa: BLE001 - the action is done; only its record is lost
        conn.rollback()
        logging.getLogger("tires_sync.panel").exception("the action %s of point %s was not written to its history", action, point_id)


def _name(conn, point_id) -> str:
    row = conn.execute("SELECT name FROM point WHERE id = ?", (point_id,)).fetchone()
    return row[0] if row else ""


def history(conn, point_id: Optional[int] = None, limit: int = SHOWN) -> List[dict]:
    """The actions of the points (of one point when `point_id` is given), newest first: {"at", "author", "point_id", "point", "kind",
    "name", "order_id", "text"}."""
    names = {p["id"]: p["name"] for p in points.list_points(conn)}
    found = []
    sql, args = "SELECT at, author, detail FROM audit WHERE action = ?", [ACTION]
    if point_id is not None:                                              # the record starts with its point (record): a quiet point's old ones too
        sql, args = sql + " AND detail LIKE ?", args + ['{"point": %d,%%' % int(point_id)]
    for at, author, detail in conn.execute(sql + " ORDER BY id DESC LIMIT ?", args + [limit]):
        try:
            d = json.loads(detail)
            pid = int(d["point"])
        except (ValueError, TypeError, KeyError):
            continue
        if point_id is not None and pid != point_id:
            continue
        found.append({"at": at, "author": author, "point_id": pid, "point": names.get(pid, "точка №%d" % pid), "kind": d.get("kind", ""),
                      "name": NAMES.get(d.get("kind", ""), d.get("kind", "")), "order_id": d.get("order"), "text": d.get("text", "")})
    peers = {str(p["vk_peer_id"]): p["id"] for p in points.list_points(conn) if p.get("vk_peer_id")}
    if peers:                                                             # what the points wrote in VK (the page's presses are records above)
        wanted = [peer for peer, pid in peers.items() if point_id is None or pid == point_id]
        if wanted:
            for at, sender, word, order_id, outcome in conn.execute(
                    "SELECT at, sender, word, order_id, outcome FROM order_command_log WHERE sender IN (%s) AND message_id NOT LIKE 'page-%%' "
                    "AND message_id NOT LIKE 'chat-%%' AND message_id NOT LIKE 'call-%%' ORDER BY at DESC LIMIT ?" % ",".join("?" * len(wanted)),
                    wanted + [limit]):
                pid = peers[str(sender)]
                found.append({"at": at, "author": "ВК точки", "point_id": pid, "point": names.get(pid, "точка №%d" % pid), "kind": "vk",
                              "name": "команда ВК «%s»" % (word or ""), "order_id": order_id, "text": outcome or ""})
    found.sort(key=lambda r: r["at"] or "", reverse=True)
    return found[:limit]
