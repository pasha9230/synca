"""«Закрыть заказы разом»: the cards picked in the panel go to one closing stage («Отмена», «Выполнено» or a closing stage of the owner).
A copy of the base is taken first; each card is moved the usual way (its required data checked, a line in its history), but the rules of the
new stage stay asleep, so no task or message is made for the clients. A card is put back by moving it again."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from typing import List

from . import order_engine, orders
from .order_cleanup import backup, sign
from .orders import OrderError

DEFAULT_REASON = "Не заказ (массовая чистка)"
LIMIT = 900                                          # one form takes up to 1000 fields: more cards are closed in a second go


def targets(conn) -> List[dict]:
    """Where cards can be closed to in one go. «Возврат» needs a kind, a reason and a sum of its own for each card: not here."""
    return [s for s in orders.list_stages(conn) if s["kind"] != "open" and s["code"] != "return"]


def open_cards(conn, from_stage=None) -> List[dict]:
    """The cards still in work (on the ordinary stages), or on one of them."""
    stages = [s for s in orders.list_stages(conn) if s["kind"] == "open" and (from_stage is None or s["id"] == from_stage)]
    out = []
    for stage in stages:
        for (card_id,) in conn.execute("SELECT id FROM order_card WHERE stage_id = ? ORDER BY id", (stage["id"],)).fetchall():
            out.append(orders.get_card(conn, card_id))
    return out


def _target(conn, target_id) -> dict:
    stage = orders.get_stage(conn, target_id)
    if stage["id"] not in {s["id"] for s in targets(conn)}:
        raise OrderError("На этап «%s» разом переводить нельзя." % stage["name"])
    return stage


def _why_not(conn, card: dict, stage: dict, reason: str, placed) -> str:
    if card["stage_id"] == stage["id"]:
        return "уже на этом этапе"
    if card["stage"]["kind"] != "open":
        return "уже закрыт («%s»)" % card["stage"]["name"]
    if card["sale_ids"] and stage["kind"] not in ("won", "closed"):
        return "записана продажа: сначала сторно"
    filled = dict(card, cancel_reason=card["cancel_reason"] or reason)
    gaps = orders.missing_for(filled, stage, placed)
    return "не заполнено: %s" % ", ".join(gaps) if gaps else ""


def check(conn, card_ids, target_id, reason: str) -> dict:
    """Which of the picked cards will move and why the others will not."""
    stage = _target(conn, target_id)
    placed = orders._placed_stage(conn)
    ok, skip = [], []
    for card_id in sorted(set(card_ids))[:LIMIT]:
        try:
            card = orders.get_card(conn, card_id)
        except OrderError:
            continue                                                        # deleted meanwhile
        why = _why_not(conn, card, stage, reason, placed)
        if why:
            skip.append((card, why))
        else:
            ok.append(card)
    return {"stage": stage, "ok": ok, "skip": skip, "sign": sign(ok), "webmim": [c for c in ok if c["webmim_number"] or c["webmim_id"]]}


def run(conn, db_path, card_ids, target_id, reason: str, expected: str, now_local: str, author: str) -> dict:
    """Moves the cards the check shows. `expected` is the `sign` of what the person confirmed: when it changed since, nothing is done."""
    reason = " ".join((reason or "").split())[:200]
    plan = check(conn, card_ids, target_id, reason)
    if plan["sign"] != expected:
        raise OrderError("Пока страница была открыта, карточки изменились (сейчас к переводу %d). Проверьте ещё раз." % len(plan["ok"]))
    if not plan["ok"]:
        raise OrderError("Переводить нечего.")
    stage = plan["stage"]
    if stage["code"] == "cancel" and not reason and any(not c["cancel_reason"] for c in plan["ok"]):
        raise OrderError("Укажите причину отмены.")
    try:
        copy = backup(db_path, datetime.fromisoformat(now_local).strftime("%Y%m%d-%H%M%S"), "bulk-%s" % stage["code"])
    except (sqlite3.Error, OSError) as err:
        raise OrderError("Копию базы снять не удалось (%s): ничего не переведено." % err)
    moved, skipped, stopped, came_from = [], 0, "", {}
    for card in plan["ok"]:
        came_from[str(card["id"])] = card["stage_id"]
        changes = {"cancel_reason": reason} if stage["code"] == "cancel" and not card["cancel_reason"] else None
        try:
            if orders.move_card(conn, card["id"], stage["id"], now_local, author, changes=changes, fire_rules=False, journal=False):
                moved.append(card["id"])
                for task in order_engine.list_tasks(conn, card["id"], ("pending", "error")):    # a closed order waits for nothing
                    order_engine._finish(conn, task, "skipped", "заказ закрыт разом", now_local, author)
            else:
                skipped += 1                                                # somebody moved it first
        except OrderError:
            skipped += 1
        except sqlite3.Error as err:                                        # the base is busy too long: stop, what is moved stays moved
            conn.rollback()
            stopped = str(err)
            break
    left = len(plan["ok"]) - len(moved) - skipped
    conn.execute("INSERT INTO audit (at, author, action, detail) VALUES (?,?,?,?)",
                 (now_local, author, "orders_bulk_move", json.dumps({"stage": stage["id"], "moved": len(moved), "skipped": skipped, "left": left,
                                                                     "backup": copy.name, "stopped": stopped, "cards": moved,
                                                                     "from": {k: v for k, v in came_from.items() if int(k) in moved}}, ensure_ascii=False)))
    conn.commit()
    return {"moved": len(moved), "skipped": skipped, "left": left, "backup": copy.name, "stage": stage["name"]}


# ---- «Вернуть после массового перевода»: each card goes back to the stage it stood on before the bulk move -------------------------------

UNDONE = "orders_bulk_undo"
STAGE_LINE = re.compile(r"^Этап: «(.+)» → «(.+)»$")


def runs(conn, limit: int = 20) -> List[dict]:
    """The bulk moves done so far, newest first, with how many of their cards were put back since."""
    out = []
    for audit_id, at, who, detail in conn.execute("SELECT id, at, author, detail FROM audit WHERE action = 'orders_bulk_move' ORDER BY id DESC LIMIT ?", (limit,)):
        data = json.loads(detail)
        try:
            name = orders.get_stage(conn, data["stage"])["name"]
        except OrderError:
            name = "?"
        out.append({"id": audit_id, "at": at, "author": who, "stage": name, "moved": data.get("moved", 0)})
    return out


def _run(conn, audit_id) -> dict:
    row = conn.execute("SELECT id, at, author, detail FROM audit WHERE id = ? AND action = 'orders_bulk_move'", (audit_id,)).fetchone()
    if row is None:
        raise OrderError("Такого массового перевода нет.")
    data = json.loads(row[3])
    try:
        stage_name = orders.get_stage(conn, data["stage"])["name"]
    except OrderError:
        stage_name = ""
    return {"id": row[0], "at": row[1], "author": row[2], "stage": data["stage"], "stage_name": stage_name, "cards": data.get("cards", []),
            "from": data.get("from", {})}


def _before(conn, card_id, run: dict, by_name: dict):
    """The stage the card stood on before this run: from the run's own record, else from the line «Этап: «A» → «B»» it wrote in the history."""
    known = run["from"].get(str(card_id))
    if known:
        return known
    for (text,) in conn.execute("SELECT text FROM order_event WHERE order_id = ? AND kind = 'stage' AND at = ? AND author = ? ORDER BY id DESC",
                                (card_id, run["at"], run["author"])):
        line = STAGE_LINE.match(text)
        if line and line.group(1) in by_name and line.group(2) == run["stage_name"]:
            return by_name[line.group(1)]
    return None


def undo_check(conn, audit_id) -> dict:
    """The cards of a bulk move with the stage each goes back to; a card moved again since, or whose old stage is gone, stays."""
    run = _run(conn, audit_id)
    stages = orders.list_stages(conn)
    by_name = {}
    for s in stages:
        by_name.setdefault(s["name"], s["id"])
    by_id = {s["id"]: s for s in stages}
    first = orders._first_stage(conn)["id"]
    ok, skip = [], []
    for card_id in run["cards"]:
        try:
            card = orders.get_card(conn, card_id)
        except OrderError:
            continue
        back = _before(conn, card_id, run, by_name)
        if card["stage_id"] != run["stage"] or card["entered_at"] != run["at"]:
            skip.append((card, "после массового перевода карточку уже переводили"))
        elif back not in by_id:
            skip.append((card, "прежний этап не найден"))
        else:
            ok.append({"card": card, "back": by_id[back], "first": back == first})
    return {"run": run, "ok": ok, "skip": skip}


def undo(conn, db_path, audit_id, card_ids, now_local: str, author: str) -> dict:
    """Puts the picked cards of a bulk move back where they were: the reason of cancelling it wrote is taken away, the tasks it ticked off wait
    again. The rules of the old stage do not fire a second time. A copy of the base is taken first."""
    plan = undo_check(conn, audit_id)
    run, picked = plan["run"], set(card_ids)
    todo = [line for line in plan["ok"] if line["card"]["id"] in picked]
    if not todo:
        raise OrderError("Вернуть нечего: отметьте карточки.")
    try:
        copy = backup(db_path, datetime.fromisoformat(now_local).strftime("%Y%m%d-%H%M%S"), "bulk-undo")
    except (sqlite3.Error, OSError) as err:
        raise OrderError("Копию базы снять не удалось (%s): ничего не возвращено." % err)
    back, skipped, stopped = [], 0, ""
    for line in todo:
        card = line["card"]
        reason_set = conn.execute("SELECT 1 FROM order_event WHERE order_id = ? AND kind = 'field' AND at = ? AND author = ? AND text LIKE 'Причина отмены:%'",
                                  (card["id"], run["at"], run["author"])).fetchone()
        try:
            if orders.move_card(conn, card["id"], line["back"]["id"], now_local, author, fire_rules=False, journal=False):
                if reason_set:                                              # off the closed stage first: only then may the reason go
                    try:
                        orders.update_card(conn, card["id"], {"cancel_reason": ""}, now_local, author, commit=False)
                    except OrderError:
                        conn.rollback()                                     # the card is back all the same; the reason just stays
                cur = conn.execute("UPDATE order_task SET status = 'pending', result = '', done_at = NULL WHERE order_id = ? AND status = 'skipped' "
                                   "AND result = 'заказ закрыт разом' AND done_at = ?", (card["id"], run["at"]))
                orders.add_event(conn, card["id"], now_local, author, "note", "Возвращён после массового перевода от %s%s" % (
                    run["at"].replace("T", " ")[:16], ", задачи снова ждут: %d" % cur.rowcount if cur.rowcount else ""))
                conn.commit()
                back.append(card["id"])
            else:
                skipped += 1
        except OrderError:
            skipped += 1
        except sqlite3.Error as err:
            conn.rollback()
            stopped = str(err)
            break
    left = len(todo) - len(back) - skipped
    conn.execute("INSERT INTO audit (at, author, action, detail) VALUES (?,?,?,?)",
                 (now_local, author, UNDONE, json.dumps({"run": run["id"], "back": back, "skipped": skipped, "left": left, "backup": copy.name,
                                                          "stopped": stopped}, ensure_ascii=False)))
    conn.commit()
    return {"back": len(back), "skipped": skipped, "left": left, "backup": copy.name}
