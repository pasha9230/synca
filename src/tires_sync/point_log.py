"""«История точки» (the owner 2026-10-29): what the points did — accepted the goods, handed a client over, cancelled a handoff, gave the
tyres back, signed the act МХ-3, moved an order — with the time and the author, for the owner and the partner.

No table of its own: the panel writes every action of a point as a record of the journal of actions (audit, action «point_action», the
point and the order in its detail); the commands a point wrote in VK are read from the log of the commands (order_command_log, the sender
is the point's VK). Writing never stops the action: a busy base loses the record, never the press.

The owner 2026-11-02: «Отменить» of a press of «Рабочее место» that moved the order («Приехал», «Выдал», «Выдан», «Выдали», «На ОТХ»;
«Передал клиента» is order_manager.cancel_handoff). The press leaves what it changed in app_state (UNDO_KEY, one per order, the newest);
the undo is there, without a time limit, while the order stands where the press put it (its stage and the moment it came there), no later
press, command of the point or move happened, and no sale is recorded; it puts back the stage, the moment, the reservation date and the
reason of the cancel, without the stage's rules, tells the owner and the partner, and is an action of «История точки». What already went
to the client is not recalled."""
from __future__ import annotations

import json
import logging
from typing import Callable, List, Optional

from . import order_points as points, orders

ACTION = "point_action"
NAMES = {"arrived": "принял заказ (приехал)", "issued": "выдал заказ", "no_show": "клиент не пришёл", "returned": "отдал на возврат",
         "reserve_issued": "выдал (ответ на резерв)", "reserve_here": "на точке (ответ на резерв)", "handoff": "передал клиента",
         "handoff_cancel": "отменил передачу", "act_signed": "подписал акт МХ-3 и отдал", "to_oth": "оставил шины на ОТХ", "move": "сменил этап", "vk": "команда ВК",
         "oth_issued": "выдал (7-й день ОТХ)", "oth_here": "шины на точке (7-й день ОТХ)", "undo": "отменил отметку",
         "shipped": "передал курьеру ТК"}
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


UNDO_KEY = "point_undo:%d"
UNDOABLE = {"arrived": "«Приехал»", "issued": "«Выдал»", "reserve_issued": "«Выдан»", "oth_issued": "«Выдали»", "to_oth": "«На ОТХ»",
            "shipped": "«Передал курьеру ТК»", "act_signed": "«Подписал акт и отдал»"}
UNDO_HINT = "Сообщение, которое уже ушло клиенту, не отзывается."
UNDO_SHOWN = 10


def remember(conn, before: dict, action: str, point_id, author: str, now: str) -> None:
    """After a press of «Рабочее место»: a press that moved the order can be undone (the newest one); any other forgets the order's undo."""
    try:
        after = orders.get_card(conn, before["id"])
        if action not in UNDOABLE or after["stage_id"] == before["stage_id"]:
            forget(conn, before["id"])
            return
        conn.execute("INSERT OR REPLACE INTO app_state (key, value) VALUES (?, ?)", (UNDO_KEY % before["id"], json.dumps({
            "action": action, "point": int(point_id), "at": now, "author": author, "from_stage": before["stage_id"],
            "from_entered": before["entered_at"] or "", "from_until": before["reserve_until"] or "", "from_reason": before["cancel_reason"] or "",
            "from_return": [before["return_kind"] or "", before["return_reason"] or "", float(before["return_amount"] or 0)],   # act_signed fills them
            "to_stage": after["stage_id"], "to_entered": after["entered_at"] or "",
            "last_event": conn.execute("SELECT COALESCE(MAX(id), 0) FROM order_event WHERE order_id = ?", (before["id"],)).fetchone()[0]},
            ensure_ascii=False)))
        conn.commit()
    except Exception:                                                     # noqa: BLE001 - the press is done; only its undo is lost
        conn.rollback()
        logging.getLogger("tires_sync.panel").exception("the undo of %s of order %s was not kept", action, before.get("id"))


def forget(conn, order_id) -> None:
    conn.execute("DELETE FROM app_state WHERE key = ?", (UNDO_KEY % int(order_id),))
    conn.commit()


def _kept(conn, order_id) -> Optional[tuple]:
    row = conn.execute("SELECT value FROM app_state WHERE key = ?", (UNDO_KEY % int(order_id),)).fetchone()
    if row is None:
        return None
    try:
        return row[0], json.loads(row[0])
    except ValueError:
        return None


def undoable(conn, card: dict, point_id) -> Optional[dict]:
    """The press that can be undone on this order of this point, or None (none kept, another point's, moved since, a later action, a sale)."""
    kept = _kept(conn, card["id"])
    if kept is None:
        return None
    rec = kept[1]
    if (rec.get("point") != point_id or card["stage_id"] != rec.get("to_stage") or (card["entered_at"] or "") != rec.get("to_entered")
            or card["sale_ids"]):
        return None
    if rec.get("action") == "oth_issued" and conn.execute(
            "SELECT 1 FROM oth_return WHERE order_id = ? UNION ALL SELECT 1 FROM oth_return_plan WHERE order_id = ? LIMIT 1",
            (card["id"], card["id"])).fetchone():
        return None                                                       # back on «На ОТХ» would drop the marks of the return (order_oth.detach)
    if rec.get("action") == "act_signed" and orders.get_stage(conn, rec.get("from_stage"))["code"] == "oth":
        return None                                                       # the same: back on «На ОТХ» drops the agreement (review 2026-11-03)
    if conn.execute("SELECT 1 FROM order_event WHERE order_id = ? AND kind IN ('point', 'stage') AND id > ? LIMIT 1",
                    (card["id"], int(rec.get("last_event") or 0))).fetchone():
        return None                                                       # the point did something more (a press, a VK command), or it moved
    return dict(rec, raw=kept[0], name=UNDOABLE.get(rec.get("action"), ""))


def undo_list(conn, point_id, limit: int = UNDO_SHOWN) -> List[dict]:
    """The presses of the point that can still be undone, the newest first: {"card", "rec"}."""
    rows = []
    for key, value in conn.execute("SELECT key, value FROM app_state WHERE key LIKE 'point_undo:%'"):
        try:
            rec = json.loads(value)
            rows.append((str(rec.get("at") or ""), int(key.split(":", 1)[1]), rec))
        except (ValueError, IndexError, AttributeError):
            continue
    found = []
    for _at, order_id, rec in sorted(rows, key=lambda r: (r[0], r[1]), reverse=True):
        if rec.get("point") != point_id:
            continue
        try:
            card = orders.get_card(conn, order_id)
        except orders.OrderError:
            continue
        kept = undoable(conn, card, point_id)
        if kept is not None:
            found.append({"card": card, "rec": kept})
        if len(found) >= limit:
            break
    return found


def undo(conn, card: dict, point: dict, who: str, now: str, tell: Optional[Callable] = None) -> str:
    """«Отменить»: the order goes back where the press found it (stage, moment, reservation date, reason of the cancel), in one transaction
    with the claim of the undo (a second press undoes nothing more). Raises orders.OrderError when it cannot. Returns the answer."""
    from . import order_oth
    rec = undoable(conn, card, point["id"])
    if rec is None:
        raise orders.OrderError("Отметку уже не отменить: по заказу было действие позже, его перенесли или отметку уже отменили.")
    if conn.execute("DELETE FROM app_state WHERE key = ? AND value = ?", (UNDO_KEY % card["id"], rec["raw"])).rowcount != 1:
        conn.rollback()
        raise orders.OrderError("Отметку уже отменили.")
    author = "менеджер %s (%s)" % (point["name"], who)
    stage = orders.get_stage(conn, rec["from_stage"])
    last = conn.execute("SELECT COALESCE(MAX(id), 0) FROM order_event WHERE order_id = ?", (card["id"],)).fetchone()[0]
    if not orders.move_card(conn, card["id"], stage["id"], now, author, fire_rules=False, commit=False):     # a refusal rolls back the claim
        conn.rollback()
        raise orders.OrderError("Заказ только что перенесли: отметку уже не отменить.")
    conn.execute("UPDATE order_card SET entered_at = ?, reserve_until = ?, cancel_reason = ? WHERE id = ?",
                 (rec["from_entered"], rec["from_until"], rec["from_reason"], card["id"]))
    conn.execute("DELETE FROM order_event WHERE order_id = ? AND id > ? AND kind = 'field' AND text LIKE 'Резерв: товар на точке%'",
                 (card["id"], last))                                  # the move's new date is not the order's: the date put back is in the text
    if stage["code"] == "at_point" and conn.execute("SELECT 1 FROM chat_send_log WHERE token = ? AND status = 'sent'", (
            "reserve-%d-%s" % (card["id"], "".join(ch for ch in rec["from_entered"] if ch.isdigit())),)).fetchone():
        from . import order_reserve                                   # a reminder of before the undo moves nothing to «На ОТХ» (the lead 2026-11-02)
        orders.add_event(conn, card["id"], now, "система", "reserve", "Напоминание о сроке резерва до отмены отметки не считается: на «На ОТХ» "
                         "заказ сам не переведётся %s" % (order_reserve.OTH_MARK % rec["from_entered"]), level="grey")
    if rec["action"] == "act_signed":                                     # the return is agreed again, the act not signed, the refund as it was
        kind, reason, amount = (rec.get("from_return") or ["", "", 0.0])[:3]
        conn.execute("UPDATE order_card SET return_kind = ?, return_reason = ?, return_amount = ? WHERE id = ?", (kind, reason, amount, card["id"]))
        conn.execute("UPDATE oth_return_plan SET signed_at = NULL, signed_by = '' WHERE order_id = ?", (card["id"],))
    if rec["action"] == "oth_issued":                                     # the question of the seventh day is open again
        conn.execute("UPDATE app_state SET value = ? WHERE key = ?", ("asked|" + now, order_oth._ask_key(dict(card, entered_at=rec["from_entered"]))))
    label = card.get("webmim_number") or str(card["id"])
    until = rec["from_until"]
    text = "Точка «%s» отменила отметку %s по заказу №%s (%s): заказ снова на этапе «%s»%s. %s" % (
        point["name"], rec["name"], label, who, stage["name"],
        ", резерв до %s.%s.%s" % (until[8:10], until[5:7], until[:4]) if stage["code"] == "at_point" and len(until) == 10 else "", UNDO_HINT)
    orders.add_event(conn, card["id"], now, author, "point", text, level="yellow")
    conn.commit()
    if rec["action"] == "shipped":                                        # the track number is not awaited any more (order_intercity)
        from . import order_intercity
        order_intercity.close_track_tasks(conn, card["id"], "cancel", now, author, "отметка «Передал курьеру ТК» отменена")
        order_intercity.skip_waiting_message(conn, card["id"], now, author)
    record(conn, now, who, point["id"], "undo", card["id"], "отменил %s" % rec["name"])
    if tell is not None:
        try:
            tell(order_oth.office_logins(conn), text)
        except Exception:                                                 # noqa: BLE001 - the undo is done; the journal has the record
            logging.getLogger("tires_sync.panel").exception("the alert of the undo of order %s failed", card["id"])
    return "Отметка %s отменена: заказ №%s снова на этапе «%s». %s" % (rec["name"], label, stage["name"], UNDO_HINT)
