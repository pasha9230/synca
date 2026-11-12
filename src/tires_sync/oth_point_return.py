"""The point and the office around the return of «На ОТХ» (the owner 2026-11-12, the task E–G):
- «Есть покупатель» (E2): the point, on «Согласование возврата», for BUYER_DAYS since the order came to that status (order_oth.agreeing_since),
  says it found a buyer; the responsible (order_oth.office_assignee) gets one urgent task «Важное · …» with the link, the tyres and the point.
  A second press makes no second open task. The sale itself is the office's, from the card, as before; nothing is read from WebMIM.
- one act for several orders (E3): the office picks orders of one point on the agreement or on the return, one date and one PDF; the act is
  kept on the pair «point + date» (oth_return_act) and every order is agreed for the date as by «Согласовано» (order_oth.agree).
- «Водитель не забрал» (G): the point, in «Сегодня возврат!», says the driver did not take the tyres (a comment if it likes). Once per point
  and date (NOT_TAKEN_KEY): one task «Важное · Водитель не забрал возвраты, согласовать с менеджером» to the responsible of the first order of
  the date, a row of the bell of the owner, the partner and the operators (office_rows) while that task is open, and the sound of «Сегодня
  возврат!» of that date stops. The date of the return stays as it is. Nothing goes to VK nor to a client."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from . import order_oth, orders, store

AUTHOR = "система"
MARK = "Важное"
BUYER_DAYS = 2
BUYER_TITLE = "%s · Точка нашла покупателя на шины с ОТХ: проверьте возможность продажи и оформите продажу" % MARK
BUYER_ASK = "Есть покупатель на эти шины?"
NOT_TAKEN_TITLE = "%s · Водитель не забрал возвраты, согласовать с менеджером" % MARK
NOT_TAKEN_KEY = "oth_not_taken:%d:%s"            # app_state, per point and date: {"at", "by", "comment", "task", "sound", "ids"}
NOT_TAKEN_NEXT = "Отправьте в следующие дни возвратов"
COMMENT_MAX = 500
NOTE_MAX = 1000                                  # the system's own text (order_call.NOTE_MAX)
BELL_ROLES = ("owner", "partner", "chats")       # who sees «На ОТХ» in the office (users.MATRIX, section «catalog»)


def _day(iso: str) -> str:
    return "%s.%s" % (iso[8:10], iso[5:7])


def _items(card: dict) -> str:
    return "; ".join("%s (арт. %s), %d шт." % (i["name"], i.get("code") or i.get("goods_id") or "—", int(i["qty"]))
                     for i in card["items"] if int(i["qty"]) >= 1) or "позиций нет"


def _task(conn, card_id: int, title: str, note: str, now: str, who: str, assignee: str) -> int:
    from . import order_notify, tasks
    task_id = tasks.add(conn, kind="other", title=title, due_at=now[:16], note=note, now=now, author=who, owner_login=order_notify.get_owner_login(conn),
                        assignee=assignee, order_id=card_id, commit=False)
    conn.execute("UPDATE callback_task SET note = ? WHERE id = ?", (note[:NOTE_MAX], task_id))     # the whole text (tasks.add cuts a person's note)
    return task_id


def _open_task(conn, card_id, title: str) -> Optional[int]:
    row = conn.execute("SELECT id FROM callback_task WHERE order_id = ? AND title = ? AND done_at IS NULL AND cancelled_at IS NULL ORDER BY id LIMIT 1",
                       (card_id, title)).fetchone()
    return row[0] if row else None


# ---- E2: «Есть покупатель» -------------------------------------------------------------------------------------------------------------

def buyer_until(conn, card: dict) -> str:
    """Until when «Есть покупатель» is shown (ISO, to the second): BUYER_DAYS since the order came to «Согласование возврата»; '' when not there."""
    since = order_oth.agreeing_since(conn, card)
    try:
        return (datetime.fromisoformat(since) + timedelta(days=BUYER_DAYS)).isoformat(timespec="seconds") if since else ""
    except ValueError:
        return ""


def can_tell_buyer(conn, card: dict, now: str) -> bool:
    until = buyer_until(conn, card)
    return bool(until) and now < until


def tell_buyer(conn, card: dict, point_name: str, who: str, now: str) -> tuple:
    """«Есть покупатель» → «Да»: one urgent task to the responsible; (outcome, answer, done) as the presses of «Рабочее место»."""
    card = orders.get_card(conn, card["id"])                                         # as it is now
    label = card.get("webmim_number") or str(card["id"])
    if not can_tell_buyer(conn, card, now):
        conn.rollback()
        return ("не подходит: не на согласовании возврата или прошло %d суток" % BUYER_DAYS,
                "Заказ №%s: «Есть покупатель» можно отметить %d суток с начала согласования возврата. Напишите офису." % (label, BUYER_DAYS), False)
    if _open_task(conn, card["id"], BUYER_TITLE):
        conn.rollback()
        return "уже отмечено", "Заказ №%s: офис уже знает о покупателе, задача открыта." % label, True
    note = "Точка «%s», заказ №%s. Состав: %s. Заказ: %s" % (point_name or "не указана", label, _items(card), order_oth.order_link(conn, card["id"]))
    try:
        task_id = _task(conn, card["id"], BUYER_TITLE, note, now, who, order_oth.office_assignee(conn, card["id"]))
        orders.add_event(conn, card["id"], now, who, "point", "Точка «%s»: есть покупатель на шины с ОТХ (заказ на согласовании возврата)." % (point_name or ""))
        store.write_audit(conn, now, who, "oth_buyer_found", {"order": int(card["id"]), "task": task_id})     # commits it all
    except Exception:
        conn.rollback()
        raise
    return ("задача офису: есть покупатель", "Заказ №%s: офису поставлена задача проверить продажу и оформить её." % label, True)


# ---- G: «Водитель не забрал» -----------------------------------------------------------------------------------------------------------

def not_taken(conn, point_id, day: str) -> Optional[dict]:
    raw = order_oth.store_value(conn, NOT_TAKEN_KEY % (int(point_id), day)) if point_id is not None else ""
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def driver_not_taken(conn, point_id, point_name: str, comment: str, who: str, now: str) -> tuple:
    """«Водитель не забрал» of «Сегодня возврат!» (G). (answer, done, new). Once per point and date: a second press changes nothing (new False)."""
    told = order_oth.return_day(conn, point_id, now)
    if not told:
        conn.rollback()
        return "Сегодня возвратов нет: отмечать нечего.", False, False
    day = told["date"]
    comment = " ".join(str(comment or "").split())[:COMMENT_MAX]
    key = NOT_TAKEN_KEY % (int(point_id), day)
    mark = {"at": now, "by": who, "comment": comment, "sound": told["sound"], "ids": told["ids"], "task": 0}
    if conn.execute("INSERT OR IGNORE INTO app_state (key, value) VALUES (?, ?)", (key, json.dumps(mark, ensure_ascii=False))).rowcount != 1:
        conn.rollback()
        return NOT_TAKEN_NEXT + ".", True, False
    first = told["ids"][0]
    links = ", ".join("№%s %s" % (n, order_oth.order_link(conn, i)) for n, i in zip(told["orders"], told["ids"]))
    note = "Точка «%s», возврат %s: водитель не забрал шины. Заказы: %s. Комментарий точки: %s" % (point_name or "", _day(day), links, comment or "нет")
    try:
        mark["task"] = _task(conn, first, NOT_TAKEN_TITLE, note, now, who, order_oth.office_assignee(conn, first))
        conn.execute("UPDATE app_state SET value = ? WHERE key = ?", (json.dumps(mark, ensure_ascii=False), key))
        for card_id in told["ids"]:
            orders.add_event(conn, card_id, now, who, "point", "Точка «%s»: водитель не забрал возврат %s.%s" % (
                point_name or "", _day(day), " Комментарий: %s" % comment if comment else ""), level="grey")
        store.write_audit(conn, now, who, "oth_not_taken", {"point": int(point_id), "date": day, "orders": told["ids"], "task": mark["task"]})
    except Exception:
        conn.rollback()
        raise
    return NOT_TAKEN_NEXT + ".", True, True


def office_rows(conn, login: str) -> List[dict]:
    """The rows of the bell of the owner, the partner and the operators (BELL_ROLES): each «Водитель не забрал» whose task is open. Computed, as
    the point's rows (routes_alerts.point_rows); the id stays the same, so notify.js rings for it once."""
    from . import order_notify, users
    role = "owner" if login == order_notify.get_owner_login(conn) else users.get_role(conn, login)["role"]
    if role not in BELL_ROLES:
        return []
    names = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM point")}
    found = []
    for key, value in conn.execute("SELECT key, value FROM app_state WHERE key LIKE 'oth_not_taken:%' ORDER BY key").fetchall():
        try:
            mark = json.loads(value)
            point_id, day = int(key.split(":")[1]), key.split(":", 2)[2]
        except (ValueError, IndexError):
            continue
        task = mark.get("task")
        if not task or not conn.execute("SELECT 1 FROM callback_task WHERE id = ? AND done_at IS NULL AND cancelled_at IS NULL", (task,)).fetchone():
            continue
        ids = [int(i) for i in mark.get("ids") or []]
        found.append({"id": "not-taken-%d-%s" % (point_id, day.replace("-", "")), "order_id": ids[0] if ids else None, "at": mark.get("at") or "",
                      "author": "", "kind": "not_taken", "level": "yellow", "more": 0, "href": "/orders/%d" % ids[0] if ids else "/tasks",
                      "title": NOT_TAKEN_TITLE,
                      "text": "Точка «%s», возврат %s: заказы %s.%s" % (names.get(point_id, ""), _day(day), ", ".join("№%d" % i for i in ids),
                                                                        " Комментарий точки: %s" % mark["comment"] if mark.get("comment") else "")})
    return found


# ---- E3: one act for several orders --------------------------------------------------------------------------------------------------

def can_act(conn, card: dict) -> bool:
    """A checkbox of the page «На ОТХ»: the order is on the agreement of its return, or agreed and the act not signed."""
    if card["stage"]["code"] not in order_oth.RETURNING or not card.get("point_id"):
        return False
    plan = order_oth.return_plan(conn, card["id"])
    return order_oth.agreeing(conn, card) or (plan is not None and not plan["signed_at"])


def act_for_orders(conn, ids, day, filename: str, data: bytes, today, now: str, author: str) -> Dict[str, object]:
    """The act of a return for several orders of one point (E3). Everything is checked first (the orders, one point, the date, the PDF);
    then every order is agreed for the date (order_oth.agree, as «Согласовано»: an order agreed for this date already stays as it is) and the
    PDF is kept on the pair «point + date». {"date", "point", "agreed": [ids], "failed": {id: why}, "replaced"}. OthError / ActError: nothing done."""
    from . import oth_return_act
    wanted = sorted({int(i) for i in ids if str(i).strip().isdigit()})
    if not wanted:
        raise order_oth.OthError("Отметьте галочками заказы для акта.")
    cards = []
    for card_id in wanted:
        try:
            card = orders.get_card(conn, card_id)
        except orders.OrderError:
            raise order_oth.OthError("Заказа №%d нет: обновите страницу." % card_id) from None
        if not can_act(conn, card):
            raise order_oth.OthError("Заказ №%d не на согласовании и не на возврате: обновите страницу." % card_id)
        cards.append(card)
    if len({c["point_id"] for c in cards}) > 1:
        raise order_oth.OthError("Выбраны заказы разных точек. Один акт — на одну точку и дату: отметьте заказы одной точки.")
    day = order_oth.check_return_day(day, today)
    for card in cards:
        plan = order_oth.return_plan(conn, card["id"])
        if plan is not None and plan["return_date"] != day:
            raise order_oth.OthError("Заказ №%d уже согласован на %s: снимите согласование в его карточке или выберите эту дату." % (
                card["id"], _day(plan["return_date"])))
    oth_return_act.check(filename, data)
    point_id = cards[0]["point_id"]
    agreed, failed = [], {}
    for card in cards:
        try:
            order_oth.agree(conn, card, today, now, author, day)
            agreed.append(card["id"])
        except (order_oth.OthError, orders.OrderError) as err:                     # moved meanwhile: the others still go
            conn.rollback()
            failed[card["id"]] = str(err)
    _act, replaced = oth_return_act.save(conn, point_id, day, filename, data, now, author)
    return {"date": day, "point": point_id, "agreed": agreed, "failed": failed, "replaced": replaced}

