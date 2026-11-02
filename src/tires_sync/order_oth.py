"""«На ОТХ»: the goods of an order the client refused are ours, kept in stock (the owner 2026-10-23).

Entering the stage unlinks the client: the link of the order (client_order) is put aside in order_oth_client, so the order is no longer the
client's (not in the card, not a purchase, never «заказал»), and the automatic joining never links it again (clients._detached). Leaving the
stage for an open one (the client came back) gives the link back; leaving it for a closed one («Выполнено», «Отмена», «Возврат», a refusal)
keeps the order unlinked. The positions of the orders standing in «На ОТХ» are our stock: the page «На ОТХ» of «Товар и фид» and a mark in
the catalog. «Вернуть поставщику» is a mark of ours (oth_return): nothing is written to WebMIM, the stage of the order does not change. The
feed is never touched: these goods are put on Avito by hand (the owner)."""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from . import orders


class OthError(ValueError):
    pass


def _audit(conn, now: str, author: str, action: str, detail: dict) -> None:
    """A record of the journal of actions saved with the caller's change (store.write_audit commits on its own)."""
    conn.execute("INSERT INTO audit (at, author, action, detail) VALUES (?,?,?,?)", (now, author, action, json.dumps(detail, ensure_ascii=False)))


def detach(conn, card: dict, now: str, author: str) -> Optional[int]:
    """The order has entered «На ОТХ»: its client (if any) is unlinked and kept aside; the marks «возвращено» and the agreed return of an
    earlier stay are dropped.
    Returns the client unlinked. No commit: saved with the move."""
    conn.execute("DELETE FROM oth_return WHERE order_id = ?", (card["id"],))
    conn.execute("DELETE FROM oth_return_plan WHERE order_id = ?", (card["id"],))     # a return agreed in an earlier stay is not this one's
    row = conn.execute("SELECT client_id, how, at FROM client_order WHERE order_id = ?", (card["id"],)).fetchone()
    if row is None:
        return None
    conn.execute("INSERT OR REPLACE INTO order_oth_client (order_id, client_id, how, linked_at, detached_at, detached_by) VALUES (?,?,?,?,?,?)",
                 (card["id"], row[0], row[1], row[2], now, author))
    conn.execute("DELETE FROM client_order WHERE order_id = ?", (card["id"],))
    orders.add_event(conn, card["id"], now, author, "field", "Клиент отвязан от заказа: отказ, товар на ОТХ (клиент №%d)" % row[0])
    _audit(conn, now, author, "oth_detach", {"order": int(card["id"]), "client": row[0]})
    return row[0]


def reattach(conn, card_id, now: str, author: str) -> Optional[int]:
    """The order has left «На ОТХ» for an open stage (the client came back): the client put aside is linked again — the one it was merged
    into, if it was. A client that is no more leaves the order to the usual joining by its phone. No commit."""
    row = conn.execute("SELECT client_id, how, linked_at FROM order_oth_client WHERE order_id = ?", (card_id,)).fetchone()
    if row is None:
        return None
    conn.execute("DELETE FROM order_oth_client WHERE order_id = ?", (card_id,))
    from . import clients
    from .inquiry import _final
    client_id = _final(conn, row[0])
    if client_id is None:
        orders.add_event(conn, card_id, now, author, "field", "Клиент №%d не найден: заказ привязывается по телефону" % row[0])
        return clients.attach_order(conn, card_id, now, author)
    conn.execute("INSERT OR IGNORE INTO client_order (order_id, client_id, how, at) VALUES (?, ?, ?, ?)", (card_id, client_id, row[1], row[2] or now))
    orders.add_event(conn, card_id, now, author, "field", "Клиент снова привязан к заказу: заказ ушёл с ОТХ (клиент №%d)" % client_id)
    _audit(conn, now, author, "oth_reattach", {"order": int(card_id), "client": client_id})
    return client_id


def leave(conn, card: dict, stage: dict, now: str, author: str) -> None:
    """A move of the order (orders.move_card): an unlinked order that goes into an open stage gives the client back — straight from «На ОТХ»
    or after a closed stage set by mistake (no other way to link it again); a closed stage keeps it unlinked."""
    if stage["code"] not in ("oth", orders.RETURN_GIVEN) and stage["kind"] == "open":      # «Возврат передан»: the goods went back, no client
        reattach(conn, card["id"], now, author)


_STOCK_SQL = ("SELECT i.id, i.order_id, i.goods_id, i.code, i.name, i.qty, c.point_id, COALESCE(p.name, ''), c.entered_at, c.reserve_until, "
              "r.at, r.author, COALESCE(r.qty, 0) FROM order_item i JOIN order_card c ON c.id = i.order_id JOIN order_stage s ON s.id = c.stage_id "
              "LEFT JOIN point p ON p.id = c.point_id LEFT JOIN oth_return r ON r.item_id = i.id WHERE s.code = 'oth' AND i.qty >= 1 ")
_STOCK_KEYS = ("item_id", "order_id", "goods_id", "code", "name", "ordered", "point_id", "point", "since", "until", "returned_at", "returned_by",
               "returned_qty")
MAX_PIECES = 10000


def _with_left(row: dict) -> dict:
    """`ordered`: the pieces of the position in the order; `returned_qty`: marked «возвращено поставщику» so far (a return may be a part,
    the owner 2026-10-24); `left`: still ours, never below zero (the quantity of the position may be lowered after a return)."""
    row["left"] = max(int(row["ordered"]) - int(row["returned_qty"]), 0)
    return row


def stock(conn) -> Dict[str, List[dict]]:
    """The positions of the orders standing in «На ОТХ», oldest first: {"stock": the pieces still ours, "returned": the pieces marked
    «возвращено поставщику»}. A position returned in part is in both; `qty` is the pieces of that list (left there, returned here).
    No price: the purchase price needs its own right, and the page is about where the goods are."""
    rows = [_with_left(dict(zip(_STOCK_KEYS, r))) for r in conn.execute(_STOCK_SQL + "ORDER BY c.entered_at, i.order_id, i.id")]
    return {"stock": [dict(r, qty=r["left"]) for r in rows if r["left"] > 0],
            "returned": [dict(r, qty=r["returned_qty"]) for r in rows if r["returned_qty"] > 0]}


def goods_on_oth(conn) -> Dict[str, int]:
    """goods_id → how many pieces of it are on «На ОТХ» and not returned: the mark of the catalog."""
    found: Dict[str, int] = {}
    for row in stock(conn)["stock"]:
        if row["goods_id"]:
            found[row["goods_id"]] = found.get(row["goods_id"], 0) + int(row["qty"])
    return found


def _stock_item(conn, item_id) -> dict:
    row = conn.execute(_STOCK_SQL + "AND i.id = ?", (item_id,)).fetchone()
    if row is None:
        raise OthError("Этой позиции нет на ОТХ: заказ ушёл с этапа «На ОТХ» или позиция удалена.")
    return _with_left(dict(zip(_STOCK_KEYS, row)))


def _pieces(value) -> int:
    text = str(value if value is not None else "").strip()
    if not (text.isascii() and text.isdigit()) or len(text) > 5 or not 1 <= int(text) <= MAX_PIECES:
        raise OthError("Количество: целое число от 1.")
    return int(text)


def mark_returned(conn, item_id, now: str, author: str, qty=None) -> None:
    """«Вернуть поставщику»: `qty` pieces of the position (all that are left when None) are marked as gone back to the supplier, never more
    than the order had and never more than are not returned yet (the owner 2026-10-24). A mark of ours: nothing is sent to WebMIM."""
    item = _stock_item(conn, item_id)
    if item["left"] <= 0:
        raise OthError("Позиция уже вся возвращена поставщику.")
    pieces = item["left"] if qty is None else _pieces(qty)
    if pieces > item["left"]:
        raise OthError("Можно вернуть не больше %d шт.: в заказе %d шт., уже возвращено %d шт." % (item["left"], item["ordered"], item["returned_qty"]))
    # the limit is in the statement itself: a mark made a moment ago in another tab never lets the total pass the pieces of the order
    if conn.execute("INSERT INTO oth_return (item_id, order_id, qty, at, author) SELECT i.id, i.order_id, ?, ?, ? FROM order_item i "
                    "WHERE i.id = ? AND i.qty >= ? ON CONFLICT(item_id) DO UPDATE SET qty = oth_return.qty + excluded.qty, at = excluded.at, "
                    "author = excluded.author WHERE oth_return.qty + excluded.qty <= (SELECT qty FROM order_item WHERE id = excluded.item_id)",
                    (pieces, now, author, item["item_id"], pieces)).rowcount != 1:
        conn.rollback()
        raise OthError("Столько уже не вернуть: позицию только что отметили в другой вкладке. Обновите страницу.")
    total = int(item["returned_qty"]) + pieces
    whole = not item["returned_qty"] and total == int(item["ordered"])
    orders.add_event(conn, item["order_id"], now, author, "field", "ОТХ: возвращено поставщику — %s, %d шт.%s" % (
        item["name"], pieces, "" if whole else " (всего возвращено %d из %d)" % (total, item["ordered"])))
    _audit(conn, now, author, "oth_return", {"order": item["order_id"], "item": item["item_id"], "qty": pieces, "returned": total})
    conn.commit()


def unmark_returned(conn, item_id, now: str, author: str, qty=None) -> None:
    """The mark «возвращено поставщику» was a mistake, for `qty` pieces (all the returned ones when None): they are our stock again."""
    item = _stock_item(conn, item_id)
    returned = int(item["returned_qty"])
    if not returned:
        raise OthError("Позиция не отмечена как возвращённая.")
    pieces = returned if qty is None else _pieces(qty)
    if pieces > returned:
        raise OthError("Отменить можно не больше %d шт.: столько отмечено возвращённым." % returned)
    if conn.execute("UPDATE oth_return SET qty = qty - ? WHERE item_id = ? AND qty >= ?", (pieces, item["item_id"], pieces)).rowcount != 1:
        conn.rollback()                                                                                        # taken off a moment ago
        raise OthError("Столько уже не отменить: отметку только что изменили в другой вкладке. Обновите страницу.")
    conn.execute("DELETE FROM oth_return WHERE item_id = ? AND qty <= 0", (item["item_id"],))
    rest = returned - pieces
    orders.add_event(conn, item["order_id"], now, author, "field", "ОТХ: отметка «возвращено поставщику» снята — %s, %d шт.%s" % (
        item["name"], pieces, " (возвращено %d из %d)" % (rest, item["ordered"]) if rest else ""))
    _audit(conn, now, author, "oth_return_undo", {"order": item["order_id"], "item": item["item_id"], "qty": pieces, "returned": rest})
    conn.commit()


# ---- the return to the supplier with the act МХ-3 (the owner 2026-10-29) ------------------------------------------------------------
# The owner or the partner agrees the return with the supplier in the order's card («Согласовано с поставщиком»): the panel names the
# nearest odd date — today when the day is odd (the agreement is made in the morning, the supplier comes on the odd days: waiting for the
# next odd day would lose two), else tomorrow (the day after an even one is always odd, the 1st of a month included). The point sees
# «Возврат <дата>» and «Подпишите акт МХ-3» on «Рабочее место» (the act is paper: the panel makes nothing of it) and presses «Подписал акт и
# отдал»: the order goes to «Возврат передан» and the responsible finishes it in «Возврат» with the amount. Nothing goes to WebMIM nor to
# the client.

ACT = "акт МХ-3"
SIGNED_LINE = "Точка «%s»: %s подписан, шины отданы поставщику (возврат с ОТХ)."
SIGNED_ASK = "Точка отдала шины с ОТХ поставщику по акту МХ-3: заполните сумму возврата и переведите заказ в «Возврат»."


def return_date(today) -> str:
    """The nearest odd date from `today` (a date), ISO."""
    from datetime import timedelta
    return (today if today.day % 2 else today + timedelta(days=1)).isoformat()


def return_plan(conn, order_id) -> Optional[dict]:
    row = conn.execute("SELECT agreed_at, agreed_by, return_date, signed_at, signed_by FROM oth_return_plan WHERE order_id = ?", (order_id,)).fetchone()
    return None if row is None else dict(zip(("agreed_at", "agreed_by", "return_date", "signed_at", "signed_by"), row))


def agree(conn, card: dict, today, now: str, author: str) -> str:
    """«Согласовано с поставщиком» for an order on «На ОТХ»: the date of the return (return_date). Once: a second press changes nothing and
    says the date. Returns the date (ISO)."""
    if card["stage"]["code"] != "oth":
        raise OthError("Согласовать возврат можно только для заказа на этапе «На ОТХ».")
    if not card["point_id"]:
        raise OthError("У заказа не указана точка: некому подписать акт МХ-3. Укажите точку в заказе.")
    day = return_date(today)
    if conn.execute("INSERT OR IGNORE INTO oth_return_plan (order_id, agreed_at, agreed_by, return_date) VALUES (?,?,?,?)",
                    (card["id"], now, author, day)).rowcount != 1:
        conn.rollback()
        return return_plan(conn, card["id"])["return_date"]
    orders.add_event(conn, card["id"], now, author, "field", "Возврат с ОТХ согласован с поставщиком: передать на возврат %s (%s)." % (
        "%s.%s" % (day[8:10], day[5:7]), ACT))
    _audit(conn, now, author, "oth_return_agreed", {"order": int(card["id"]), "date": day})
    conn.commit()
    return day


def unagree(conn, card: dict, now: str, author: str) -> bool:
    """«Отменить согласование» (the owner and the partner, before the act МХ-3 is signed): the agreement goes, the tyres are our stock of OTH
    again (the ad shows «Шины в наличии» in the next cycle, oth_stock), the point no longer sees the return. One statement: a signed plan, or
    one cancelled a moment ago, is left as it is. False: nothing to cancel (or the order left «На ОТХ»)."""
    if card["stage"]["code"] != "oth":
        return False
    if conn.execute("DELETE FROM oth_return_plan WHERE order_id = ? AND signed_at IS NULL", (card["id"],)).rowcount != 1:
        conn.rollback()
        return False
    orders.add_event(conn, card["id"], now, author, "field", "Согласование возврата с поставщиком отменено: шины снова на ОТХ.")
    _audit(conn, now, author, "oth_return_unagreed", {"order": int(card["id"])})
    conn.commit()
    return True


UNAGREED_POINT = "Возврат по заказу №%s поставщику отменён, шины остаются на точке"     # to the point after «Отменить согласование» (2026-10-30)


def act_orders(conn, order_id) -> str:
    """The orders the act МХ-3 of a return is likely made by (the owner 2026-10-30: the supplier's driver brings it for the order the tyres
    were bought by): those of the positions of the order taken from «На ОТХ», the first order of a chain, by the number the point knows
    (WebMIM's, else the panel's) — the same as the mark «Шины с ОТХ, заказ №…» of its card. '' for an ordinary order on OTH."""
    from .order_templates import _oth_orders
    return _oth_orders(conn, order_id)


def to_return(conn, point_id) -> List[dict]:
    """The orders of the point on «На ОТХ» agreed for the return and not yet given: (card, plan), the earliest date first."""
    rows = conn.execute("SELECT c.id FROM order_card c JOIN order_stage s ON s.id = c.stage_id JOIN oth_return_plan r ON r.order_id = c.id "
                        "WHERE c.point_id = ? AND s.code = 'oth' AND r.signed_at IS NULL ORDER BY r.return_date, c.id", (point_id,)).fetchall()
    return [{"card": orders.get_card(conn, r[0]), "plan": return_plan(conn, r[0])} for r in rows]


def sign_act(conn, card: dict, point_name: str, now: str, author: str) -> bool:
    """«Подписал акт и отдал»: the plan is marked signed (one statement: a second press, another tab, signs nothing more) and the order goes
    from «На ОТХ» to «Возврат передан» with a yellow record for the responsible, in one transaction. False: nothing to sign (no agreement, signed already, the
    order left «На ОТХ»)."""
    if card["stage"]["code"] != "oth":
        return False
    orders._ensure_return_given_stage(conn)                                           # a pipeline nobody opened since the stage came
    stage = next(s for s in orders.list_stages(conn) if s["code"] == orders.RETURN_GIVEN)
    if conn.execute("UPDATE oth_return_plan SET signed_at = ?, signed_by = ? WHERE order_id = ? AND signed_at IS NULL",
                    (now, author, card["id"])).rowcount != 1:
        conn.rollback()
        return False
    orders.add_event(conn, card["id"], now, author, "point", SIGNED_LINE % (point_name, ACT))     # saved with the move (move_card commits it all)
    orders.add_event(conn, card["id"], now, author, "point", SIGNED_ASK, level="yellow")
    _audit(conn, now, author, "oth_act_signed", {"order": int(card["id"])})
    try:
        moved = orders.move_card(conn, card["id"], stage["id"], now, author)
    except orders.OrderError:
        conn.rollback()
        raise
    if not moved:                                                                     # moved meanwhile: move_card rolled it all back (an order
        # already on the target would be committed instead: the check of «На ОТХ» above keeps it from there)
        conn.rollback()
        return False
    return True


# ---- the owner 2026-11-02: «На ОТХ» is held six days; on the seventh from 12:30 the point is asked «Заказ выдали или он на точке?» --------
# On its «Рабочее место» (a banner with «Выдали» / «На точке»), once per stay on «На ОТХ» (app_state ASK_KEY, moved by one UPDATE only).
# «Выдали»: the client came after all, the order is linked to its client again and closed as issued. «На точке»: the order stays on «На ОТХ»
# (its tyres stay our stock until the return is agreed: the feed is not touched) and shows on the point's «На возврат»; the owner and the
# partner get an alert and a task «Согласовать возврат» each; their «Согласовано с поставщиком» (agree) starts the act МХ-3 as before.
HOLD_DAYS = 6
ASK_AT = "12:30"
ASK_KEY = "oth_ask:%d:%s"                        # per order and stay on «На ОТХ»: asked|<at> → issued|<at> / here|<at>
RETURN_TASK = "Согласовать возврат"


def _ask_key(card: dict) -> str:
    return ASK_KEY % (card["id"], re.sub(r"\D", "", card["entered_at"] or ""))


def ask_due(card: dict, now: str) -> bool:
    """The seventh day on «На ОТХ» (the day it entered is the first) from 12:30, or any later moment."""
    entered = str(card.get("entered_at") or "")[:10]
    try:
        day = date.fromisoformat(entered) + timedelta(days=HOLD_DAYS)
    except ValueError:
        return False
    return now >= "%sT%s" % (day.isoformat(), ASK_AT)


def ask_seventh_day(conn, now: str) -> int:
    """The tick: the orders on «На ОТХ» whose seventh day has come are asked, once per stay. Not one whose return is agreed already (the act
    МХ-3 is on its way), nor a point's own sale. Returns how many were asked."""
    from . import order_points as points
    asked = 0
    for (card_id,) in conn.execute("SELECT c.id FROM order_card c JOIN order_stage s ON s.id = c.stage_id WHERE s.code = 'oth' AND c.point_id IS NOT NULL "
                                   "AND NOT EXISTS (SELECT 1 FROM oth_return_plan r WHERE r.order_id = c.id) ORDER BY c.id").fetchall():
        card = orders.get_card(conn, card_id)
        if not ask_due(card, now) or card_id in points.own_sales(conn, card["point_id"]):
            continue
        if conn.execute("INSERT OR IGNORE INTO app_state (key, value) VALUES (?, ?)", (_ask_key(card), "asked|" + now)).rowcount != 1:
            continue
        point = card.get("point") or {}
        orders.add_event(conn, card_id, now, "система", "point", "Точке «%s» задан вопрос на «Рабочем месте»: «%s»" % (
            point.get("name") or "", question(conn, card, now)), level="grey")
        if point and point.get("channel") != points.PANEL:      # review 2026-11-02: a point on VK may never open its page
            orders.add_event(conn, card_id, now, "система", "point", "Точка «%s» работает в ВК: вопрос 7-го дня на ОТХ (выдали заказ или он на "
                             "точке) она может не увидеть — узнайте у точки сами" % point.get("name", ""), level="yellow")
        conn.commit()
        asked += 1
    return asked


def question(conn, card: dict, now: str) -> str:
    """The words of the question (the owner's template «Точке: ОТХ 7-й день» if there is one, else the sample)."""
    from .order_templates import OTH_ASK_SAMPLE, render
    row = conn.execute("SELECT body FROM order_template WHERE name = ? AND audience = ? ORDER BY id LIMIT 1", OTH_ASK_SAMPLE[:2]).fetchone()
    return render(conn, card, row[0] if row else OTH_ASK_SAMPLE[2], now)[0].strip()


def question_open(conn, card: dict) -> bool:
    """«Рабочее место» shows the question: the order is on «На ОТХ», asked in this stay and not answered."""
    return card["stage"]["code"] == "oth" and (store_value(conn, _ask_key(card)) or "").startswith("asked|")


def to_agree(conn, card: dict) -> bool:
    """The point said «На точке» in this stay on «На ОТХ» and the return is not agreed yet: the point's «На возврат», waiting for the owner."""
    return (card["stage"]["code"] == "oth" and (store_value(conn, _ask_key(card)) or "").startswith("here|")
            and return_plan(conn, card["id"]) is None)


def store_value(conn, key: str) -> str:
    row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else ""


def _answer(conn, card: dict, to: str, now: str) -> str:
    """The question moves from «asked» to `to` (no commit): the state read, '' when it was not open (answered, or never asked)."""
    key = _ask_key(card)
    was = store_value(conn, key)
    if not was.startswith("asked|") or conn.execute("UPDATE app_state SET value = ? WHERE key = ? AND value = ?",
                                                    (to + "|" + now, key, was)).rowcount != 1:
        return ""
    return was


def office_logins(conn) -> list:
    """The owner (the login of the alerts) and every active partner (Заман's role; never a login written here)."""
    from . import order_notify, users
    owner = order_notify.get_owner_login(conn)
    return [owner] + sorted(u["login"] for u in users.list_users(conn) if u["active"] and u["role"] == "partner" and u["login"] != owner)


def answer_issued(conn, card: dict, who: str, now: str) -> tuple:
    """«Выдали»: the client is linked again and the order goes to «Выполнено», with the answer, in one transaction. (outcome, answer, done)."""
    label = card.get("webmim_number") or str(card["id"])
    if card["stage"]["code"] != "oth" or not _answer(conn, card, "issued", now):
        conn.rollback()
        return "уже отмечено", "Заказ №%s уже отмечен или ушёл с «На ОТХ»: обновите страницу." % label, False
    done = next((s for s in orders.list_stages(conn) if s["code"] == "done"), None)
    if done is None:
        conn.rollback()
        return "не выполнено: нет этапа «Выполнено»", "Не получилось: в воронке нет этапа «Выполнено».", False
    reattach(conn, card["id"], now, who)
    orders.add_event(conn, card["id"], now, who, "point", "Точка ответила на 7-й день ОТХ: заказ выдали")
    try:
        moved = orders.move_card(conn, card["id"], done["id"], now, who)
    except orders.OrderError as err:                                                 # rolled back with the answer: the question stays
        return "не выполнено: %s" % err, "Не получилось: %s" % err, False
    if not moved:
        conn.rollback()
        return "не перенесён: заказ уже на другом этапе", "Заказ №%s уже переместили, проверьте его в панели." % label, False
    return "заказ перенесён на «%s»" % done["name"], "Заказ №%s — отмечено: выдан (теперь на этапе «%s»)." % (label, done["name"]), True


def answer_here(conn, card: dict, who: str, now: str, tell=None) -> tuple:
    """«На точке»: the tyres lie at the point for the return; the owner and the partner get a yellow record, an alert (`tell(logins, text)`)
    and a task «Согласовать возврат» each. (outcome, answer, done)."""
    from . import order_notify, tasks
    label = card.get("webmim_number") or str(card["id"])
    if card["stage"]["code"] != "oth" or not _answer(conn, card, "here", now):
        conn.rollback()
        return "уже отмечено", "Заказ №%s уже отмечен или ушёл с «На ОТХ»: обновите страницу." % label, False
    point = (card.get("point") or {}).get("name") or "точка не указана"
    text = "Заказ №%s (точка «%s») 7 дней на ОТХ, шины на точке: согласуйте возврат с поставщиком — /orders/%d" % (label, point, card["id"])
    orders.add_event(conn, card["id"], now, who, "point", "Точка ответила на 7-й день ОТХ: шины на точке. Согласуйте возврат с поставщиком "
                     "(«Согласовано с поставщиком» в заказе)", level="yellow")
    conn.commit()
    owner = order_notify.get_owner_login(conn)
    due = (datetime.fromisoformat(now) + timedelta(days=1)).isoformat(timespec="minutes")
    for login in office_logins(conn):
        try:
            tasks.add(conn, kind="other", title=RETURN_TASK, due_at=due, note="Заказ №%s, точка «%s», /orders/%d" % (label, point, card["id"]),
                      now=now, author="система", assignee=login, owner_login=owner, order_id=card["id"])
            conn.commit()
        except Exception:                                                            # noqa: BLE001 - the answer is kept; the record says what to do
            conn.rollback()
            logging.getLogger("tires_sync.orders").exception("the task «%s» of order %s for %s failed", RETURN_TASK, card["id"], login)
    if tell is not None:
        try:
            tell(office_logins(conn), text)
        except Exception:                                                            # noqa: BLE001
            logging.getLogger("tires_sync.orders").exception("the alert of the return of order %s failed", card["id"])
    return "на точке: на возврат, задача «%s»" % RETURN_TASK, "Заказ №%s — отмечено: шины на точке. Возврат согласуют с поставщиком." % label, True
