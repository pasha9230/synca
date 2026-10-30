"""«Приехал, а клиенту не сообщили» (the owner 2026-10-30: the orders 464, 467, 469 moved to «Товар на точке» by hand, 473 refused by Avito with
a 402 «Перейдите на подписку с API мессенджера»).

An order on «Товар на точке» (also one whose tyres came from «На ОТХ») is untold when, in this stay at the point, its client heard nothing of
«можно забирать»: no send of the arrival's mark went (`arrived-<order>-<arrival>`, order_manager.arrived_token), its waiting message was not
sent by hand nor skipped, no message of the template «Клиенту: товар на точке» was done after the arrival, nobody answered its line of
«Входящие». A grace of GRACE_MINUTES leaves the sends still running (a 402 the sync repeats for 30 minutes) alone.
The hourly tick (cli orders-tick) tells the responsible and the owner once a day per order, with the reason and the link to the order (a
yellow record for the bell, Telegram by order_notify.tell_people); the card shows a line with a button that only prepares the message —
it goes by «Отправить» of the window «Сообщения по заказу», under the same mark, so a 402 being repeated or an unsure send never goes twice.
A 402 of «no messenger API» is told the owner once a day per account. Nothing here ever writes to a client."""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional

from . import order_manager as mgr, orders, store

GRACE_MINUTES = 30
REPEAT_HOURS = 24
BY_HAND = "переведено вручную"
NO_API = "Авито отказал: нет подписки API мессенджера (402)"
REFUSED_402 = "Авито отказал (402)"
NO_CHAT = "нет чата"
UNKNOWN = "неизвестно, ушло ли: проверьте чат, повторно не отправляйте"
LINE = "Клиенту не сообщили, что шины на точке"
KEY = "arrived-untold:%d:%s"
API_KEY = "msg-api-402:%d"
API_LINE = "%s: Авито не даёт отправлять сообщения (нет подписки API мессенджера)"
Tell = Callable[[List[str], str], object]


def _at(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value)[:19])
    except ValueError:
        return None


def _hours(since: str, now: str) -> float:
    a, b = _at(since), _at(now)
    return (b - a).total_seconds() / 3600.0 if a and b else 1e9


def _no_api(error: str) -> bool:
    text = str(error or "").lower()
    return "402" in text and "подписк" in text


def told(conn, card: dict, now: str) -> bool:
    """The client of the order heard «можно забирать» in this stay at the point (by itself, by hand, by a rule's message), or a person
    decided not to (skipped, answered its line of «Входящие»)."""
    token = mgr.arrived_token(card, now)
    row = conn.execute("SELECT status FROM chat_send_log WHERE token = ?", (token,)).fetchone()
    if row and row[0] == "sent":
        return True
    task = mgr.arrived_task(conn, card["id"], token)
    if task is not None and task["status"] in ("done", "skipped"):
        return True
    if mgr._answered_by_hand(conn, token):
        return True
    return conn.execute("SELECT 1 FROM order_task WHERE order_id = ? AND kind = 'message' AND title = ? AND status = 'done' AND created_at >= ? "
                        "LIMIT 1", (card["id"], mgr.ARRIVED_TEMPLATE, card["entered_at"] or "")).fetchone() is not None


def reason(conn, card: dict, now: str) -> str:
    """Why the client was not told, in words for the owner."""
    token = mgr.arrived_token(card, now)
    row = conn.execute("SELECT status, error FROM chat_send_log WHERE token = ?", (token,)).fetchone()
    if row:
        status, error = row[0], str(row[1] or "")
        if _no_api(error):
            return NO_API
        if status == "retry" or "402" in error:
            return REFUSED_402
        if status in ("unknown", "sending", "lost"):
            return UNKNOWN
        return ("Авито отказал: %s" % error[:120]) if error else "не отправлено"
    task = mgr.arrived_task(conn, card["id"], token)
    chat, why = mgr._client_chat(conn, card["id"])
    no_chat = chat is None and "нет чата" in why
    if task is None:                                                    # nobody pressed «приехал»: moved by hand
        return BY_HAND + (", " + NO_CHAT if no_chat else "")
    if no_chat:
        return NO_CHAT
    if task is not None:
        result = str(task.get("result") or "")
        if "нет чата" in result:
            return NO_CHAT
        return re.sub(r"^Не отправлено само:\s*", "", result).rstrip(".") or "ждёт в «Сообщениях по заказу»"


def untold(conn, now: str, grace: int = GRACE_MINUTES) -> List[Dict]:
    """The orders on «Товар на точке» whose client was not told, longer than `grace` minutes there: [{card, reason}]."""
    out = []
    for (card_id,) in conn.execute("SELECT c.id FROM order_card c JOIN order_stage s ON s.id = c.stage_id WHERE s.code = 'at_point' ORDER BY c.id").fetchall():
        card = orders.get_card(conn, card_id)
        if _hours(card["entered_at"] or now, now) * 60 < grace or told(conn, card, now):
            continue
        out.append({"card": card, "reason": reason(conn, card, now)})
    return out


def _link(conn, card_id: int) -> str:
    base = (os.environ.get("TIRES_PUBLIC_BASE") or store.get_state(conn, "public_base") or "").rstrip("/")
    return "%s/orders/%d" % (base, card_id) if base else "панель → Заказы → №%d" % card_id


def check(conn, now: str, tell: Tell) -> int:
    """The hourly look: each untold order is told the responsible and the owner (`tell(logins, text)`) and marked yellow in its journal,
    once a day while it stays untold. Returns how many were told. Writes nothing to a client."""
    from . import order_notify
    owner = order_notify.get_owner_login(conn)
    made = 0
    for found in untold(conn, now):
        card, why = found["card"], found["reason"]
        key = KEY % (card["id"], re.sub(r"\D", "", card["entered_at"] or ""))
        last = store.get_state(conn, key)
        if last and _hours(last, now) < REPEAT_HOURS:
            continue
        label = mgr.label(card)
        orders.add_event(conn, card["id"], now, "система", "notify", "%s: %s. Отправить можно из «Сообщений по заказу»." % (LINE, why), level="yellow")
        conn.execute("INSERT OR REPLACE INTO app_state (key, value) VALUES (?, ?)", (key, now))
        conn.commit()
        logins = [x for x in dict.fromkeys([card.get("responsible") or "", owner]) if x]
        tell(logins, "Заказ №%s: %s — %s. Отправить вручную: %s" % (label, LINE.lower(), why, _link(conn, card["id"])))
        made += 1
    return made


def prepare(conn, card_id: int, now: str, author: str) -> bool:
    """The card's button: the message «можно забирать» of this arrival waits in «Сообщения по заказу» (made when it is not there yet); nothing
    is sent. False: the order is not on «Товар на точке»."""
    card = orders.get_card(conn, card_id)
    if card["stage"]["code"] != "at_point":
        return False
    token = mgr.arrived_token(card, now)
    task = mgr.arrived_task(conn, card_id, token)
    if task is not None:
        return True
    from . import order_templates as templates
    own = mgr._oth_arrived_body(conn, card)
    text, missing = templates.render(conn, card, own or mgr._arrived_body(conn), now)
    mgr._arrived_wait(conn, card, token, "" if missing else text, ("не хватает: %s" % ", ".join(missing)) if missing else "отправьте из окна",
                      now, own_text=own is not None)
    orders.add_event(conn, card_id, now, author, "chat", "Сообщение клиенту «%s» подготовлено: отправьте его из «Сообщений по заказу»." % mgr.ARRIVED_TEMPLATE,
                     level="grey")
    conn.commit()
    return True


def api_refusals(conn, now: str) -> List[str]:
    """The accounts Avito refused a message in the last day for «no messenger API» (a 402): one line per account, once a day."""
    since = (_at(now) - timedelta(hours=REPEAT_HOURS)).isoformat(timespec="seconds") if _at(now) else ""
    out = []
    rows = conn.execute("SELECT DISTINCT a.id, a.name FROM chat_send_log l JOIN avito_chat c ON c.id = l.chat_row JOIN account a ON a.id = c.account_id "
                        "WHERE l.error LIKE '%402%' AND l.at >= ? ORDER BY a.id", (since,)).fetchall()
    for account_id, name in rows:
        if not any(_no_api(e) for (e,) in conn.execute(
                "SELECT l.error FROM chat_send_log l JOIN avito_chat c ON c.id = l.chat_row WHERE c.account_id = ? AND l.at >= ?", (account_id, since))):
            continue
        key = API_KEY % account_id
        last = store.get_state(conn, key)
        if last and _hours(last, now) < REPEAT_HOURS:
            continue
        conn.execute("INSERT OR REPLACE INTO app_state (key, value) VALUES (?, ?)", (key, now))
        out.append(API_LINE % name)
    conn.commit()
    return out
