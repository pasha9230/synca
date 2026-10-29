"""The automation engine of the Orders section: a rule fires on entering a stage or hanging in it and makes a task.

Nothing goes out of the panel by itself: a message is prepared as text, and only a person's press sends it to the client (send_task), to the
manager of the point in VK (send_point_task) or ticks it off. The one exception is order_placed (the owner 2026-10-23).
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from . import chats, order_points as points, order_rules as rules, order_templates as templates, orders, vk_send
from .orders import OrderError

MAX_CHAIN = 3
TASK_FIELDS = "id, order_id, rule_id, entered_at, kind, arg, stage_id, status, title, body, result, created_at, done_at"
STATUS_NAMES = {"pending": "ждёт", "running": "выполняется", "done": "выполнено", "skipped": "пропущено", "error": "ошибка"}


def _task(row) -> dict:
    return dict(zip([f.strip() for f in TASK_FIELDS.split(",")], row))


def list_tasks(conn, card_id, statuses=None) -> List[dict]:
    out = [_task(r) for r in conn.execute("SELECT %s FROM order_task WHERE order_id = ? ORDER BY id" % TASK_FIELDS, (card_id,))]
    return [t for t in out if statuses is None or t["status"] in statuses]


def get_task(conn, task_id) -> dict:
    row = conn.execute("SELECT %s FROM order_task WHERE id = ?" % TASK_FIELDS, (task_id,)).fetchone()
    if row is None:
        raise OrderError("Такой задачи нет.")
    return _task(row)


def waiting_counts(conn) -> Dict[int, int]:
    """Cards with tasks that wait for a person (pending, or failed and not yet looked at)."""
    return {r[0]: r[1] for r in conn.execute("SELECT order_id, COUNT(*) FROM order_task WHERE status IN ('pending', 'error') GROUP BY order_id")}


def _describe(conn, rule: dict) -> str:
    stage_name = ""
    if rule["action"] == "move":
        stage_name = orders.get_stage(conn, int(rule["arg"]))["name"]
    return {"remind": rule["arg"], "tag_add": "Добавить тег «%s»" % rule["arg"], "tag_remove": "Убрать тег «%s»" % rule["arg"],
            "move": "Перенести в «%s»" % stage_name, "record_sale": "Записать продажу"}.get(rule["action"], "")


def _make_task(conn, card: dict, rule: dict, now: str, chain: Tuple[int, ...]) -> bool:
    """One task per rule per stay in the stage; False when this stay already has it."""
    status, body, result = "pending", "", ""
    if rule["action"] == "message":
        template = templates.get_template(conn, rule["template_id"])
        text, missing = templates.render(conn, card, template["body"])
        title = template["name"]
        if missing:
            status, result = "error", "Не хватает: %s." % ", ".join(missing)
        else:
            body = text
        if conn.execute("SELECT 1 FROM order_task WHERE order_id = ? AND rule_id = ? AND kind = 'message' AND arg = ? AND status != 'skipped'",
                        (card["id"], PLACED_RULE, str(rule["template_id"]))).fetchone():
            status, result = "skipped", PLACED_SKIP              # the same text is the order's already, made after WebMIM: never a second one
        elif template["audience"] == "client" and rule["stage_id"] == (orders._placed_stage(conn) or {}).get("id") and conn.execute(
                "SELECT 1 FROM order_task WHERE order_id = ? AND kind = 'message' AND arg = ? AND status = 'done'",
                (card["id"], str(rule["template_id"]))).fetchone():
            status, result = "skipped", SENT_SKIP                # back in «Заказ оформлен»: the client heard «оформлен» already (a point
                                                                 # may be another one now: its message is prepared as before)
        elif template["audience"] == "manager" and points.in_panel(card.get("point")):
            status, result = "skipped", PANEL_SKIP               # the point works in the panel: nothing to it in VK, nobody has to skip it by hand
    else:
        title = _describe(conn, rule)
    cur = conn.execute("INSERT OR IGNORE INTO order_task (order_id, rule_id, entered_at, kind, arg, stage_id, status, title, body, result, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (card["id"], rule["id"], card["entered_at"], rule["action"], str(rule["template_id"]) if rule["action"] == "message" else rule["arg"],
                        card["stage_id"], status, title, body, result, now))
    if cur.rowcount != 1:                                          # this stay already has the task (a second worker, a second tick)
        conn.commit()
        return False
    orders.add_event(conn, card["id"], now, "правило №%d" % rule["id"], "rule",
                     "%s: %s" % ("Задача создана" if status == "pending" else "Задача пропущена (%s)" % result if status == "skipped"
                                 else "Задача не подготовлена", title), level="red" if status == "error" else "grey")
    conn.commit()
    if status == "pending" and rule["mode"] == "auto" and rule["action"] not in ("message", "remind"):
        run_task(conn, cur.lastrowid, now, "правило №%d" % rule["id"], chain)
    return True


def _applies(rule: dict, card: dict) -> bool:
    return bool(rule["enabled"]) and (rule["point_id"] is None or rule["point_id"] == card["point_id"])


def fire_enter(conn, card_id, now: str, chain: Tuple[int, ...] = (), of_point: Optional[int] = None) -> int:
    """The rules of the stage the card has just entered. `chain` is the stages passed through by rules in this go. `of_point`: only the rules
    set up for that point (the card got the point after it entered the stage; the general rules ran then)."""
    card = orders.get_card(conn, card_id)
    chain = chain or (card["stage_id"],)
    made = 0
    for rule in rules.list_rules(conn, card["stage_id"]):
        if rule["trigger"] == "enter" and (of_point is None or rule["point_id"] == of_point) and _applies(rule, card):
            made += 1 if _make_task(conn, card, rule, now, chain) else 0
            card = orders.get_card(conn, card_id)                  # an auto rule may have changed it (tags, stage)
            if card["stage_id"] != chain[-1]:
                break                                              # moved on: the rules of the new stage were run by that move
    return made


def fire_stale(conn, now: str) -> int:
    """The `stale` rules: the card has been in the stage for at least `hours`. Run from the hourly timer."""
    when = datetime.fromisoformat(now)
    made = 0
    for rule in rules.list_rules(conn):
        if rule["trigger"] != "stale" or not rule["enabled"]:
            continue
        if orders.get_stage(conn, rule["stage_id"])["kind"] != "open":          # finished orders do not «hang»
            continue
        for (card_id,) in conn.execute("SELECT id FROM order_card WHERE stage_id = ?", (rule["stage_id"],)).fetchall():
            card = orders.get_card(conn, card_id)
            try:
                waited = (when - datetime.fromisoformat(card["entered_at"])).total_seconds()
            except ValueError:
                continue
            if waited >= rule["hours"] * 3600 and _applies(rule, card):
                try:
                    made += 1 if _make_task(conn, card, rule, now, (card["stage_id"],)) else 0
                except Exception:                                  # one broken card must not stop the tick for the others
                    conn.rollback()
                    logging.getLogger("tires_sync.orders").exception("stale rule %s failed for card %s", rule["id"], card_id)
    return made


PLACED_RULE = -2                                                   # the «rule» number of the messages after «Оформить в WebMIM» (order_placed)
ARRIVED_RULE = -3                                                  # «можно забирать» that did not go by itself (order_manager._tell_arrived)
PLACED_SKIP = "подготовлено после оформления в WebMIM"
SENT_SKIP = "это сообщение по заказу уже отправлено"
PANEL_SKIP = "точка работает в панели: в ВК ей не пишем, заказ виден на её «Моей точке»"
RUNNING_TIMEOUT = 600                                              # seconds; a task «running» longer than this was cut off (a killed worker)


def refresh_tasks(conn, card_id, now: str) -> None:
    """Bring the waiting tasks of a card up to date: a message is built from the card as it is now (a lacking value filled in later makes
    an error task usable again, a value taken away makes it an error), and a task stuck in «running» after a killed worker comes back."""
    when = datetime.fromisoformat(now)
    card = None
    for task in list_tasks(conn, card_id, ("pending", "error", "running")):
        if task["status"] == "running":
            limit = chats.STALE_SENDING if task["kind"] == "message" else RUNNING_TIMEOUT       # a message: the same bound as its send mark
            try:
                stuck = (when - datetime.fromisoformat(task["done_at"])).total_seconds() > limit
            except (TypeError, ValueError):
                stuck = True
            if stuck:
                conn.execute("UPDATE order_task SET status = 'pending' WHERE id = ? AND status = 'running'", (task["id"],))
            continue
        if task["kind"] != "message" or not str(task["arg"]).isdigit():
            continue
        try:
            template = templates.get_template(conn, int(task["arg"]))
        except OrderError:
            continue
        card = card or orders.get_card(conn, card_id)
        text, missing = templates.render(conn, card, template["body"])
        status, body, result = ("error", "", "Не хватает: %s." % ", ".join(missing)) if missing else ("pending", text, "")
        if (status, body, result) != (task["status"], task["body"], task["result"]):
            conn.execute("UPDATE order_task SET status = ?, body = ?, result = ? WHERE id = ? AND status IN ('pending', 'error')", (status, body, result, task["id"]))
    conn.commit()


def _finish(conn, task: dict, status: str, result: str, now: str, author: str, text: str = "") -> None:
    cur = conn.execute("UPDATE order_task SET status = ?, result = ?, done_at = ? WHERE id = ? AND status IN ('pending', 'running', 'error')",
                       (status, result, now, task["id"]))
    if cur.rowcount != 1:                                          # somebody finished it first: no second line in the feed
        conn.commit()
        return
    orders.add_event(conn, task["order_id"], now, author, "rule", "%s: %s%s%s" % (task["title"], STATUS_NAMES[status], " (%s)" % result if result else "",
                                                                       ":\n" + text if text else ""),   # a sent message: its text is in the journal
                     level="red" if status == "error" else "grey")            # by the status: the client's words never colour the record
    conn.commit()


def run_task(conn, task_id, now: str, author: str, chain: Tuple[int, ...] = ()) -> str:
    """Do what the task says (or tick it off for a message and a reminder). Returns the status it ends in."""
    task = get_task(conn, task_id)
    if task["kind"] == "message" and task["status"] in ("pending", "error"):
        refresh_tasks(conn, task["order_id"], now)                 # the text goes out as the card is now
        task = get_task(conn, task_id)
        if task["status"] == "error":
            raise OrderError(task["result"])
    if task["status"] != "pending":
        raise OrderError("Эта задача уже обработана.")
    claim = conn.execute("UPDATE order_task SET status = 'running', done_at = ? WHERE id = ? AND status = 'pending'", (now, task_id))
    conn.commit()
    if claim.rowcount != 1:                                        # a double press or a second worker got there first
        raise OrderError("Эта задача уже выполняется или выполнена.")
    try:
        return _do_task(conn, task, now, author, chain)
    except BaseException:
        conn.rollback()
        conn.execute("UPDATE order_task SET status = 'pending' WHERE id = ? AND status = 'running'", (task_id,))
        conn.commit()
        raise


def _do_task(conn, task: dict, now: str, author: str, chain: Tuple[int, ...]) -> str:
    card = orders.get_card(conn, task["order_id"])
    action, arg = task["kind"], task["arg"]
    if action in ("message", "remind"):
        _finish(conn, task, "done", "отправлено вручную" if action == "message" else "", now, author)
        return "done"
    if action in ("move", "record_sale") and (card["entered_at"] != task["entered_at"] or card["stage_id"] != task["stage_id"]):
        _finish(conn, task, "skipped", "заказ уже ушёл с этого этапа", now, author)
        return "skipped"
    try:
        if action in ("tag_add", "tag_remove"):
            if not arg:
                raise OrderError("В задаче нет тега.")
            tags = [t for t in card["tags"] if t != arg]
            if action == "tag_add":
                tags = card["tags"] + [arg] if arg not in card["tags"] else card["tags"]
            orders.update_card(conn, card["id"], {"tags": ",".join(tags)}, now, author)
        elif action == "move":
            if not arg.isdigit():
                raise OrderError("В задаче нет этапа назначения.")
            target = int(arg)
            chain = chain or (card["stage_id"],)
            if target in chain or len(chain) >= MAX_CHAIN:
                raise OrderError("Петля правил: заказ уже был на этом этапе или правил в цепочке слишком много.")
            if not orders.move_card(conn, card["id"], target, now, author, None, chain=chain + (target,)):
                raise OrderError("Заказ уже на этом этапе или его перенесли раньше.")
        elif action == "record_sale":
            orders.record_sales(conn, card["id"], now[:10], now, author, now[:10])
        else:
            raise OrderError("Неизвестное действие.")
    except OrderError as err:
        conn.rollback()
        _finish(conn, task, "error", str(err), now, author)
        return "error"
    _finish(conn, task, "done", "", now, author)
    return "done"


SENT_TO_CLIENT = "отправлено клиенту"


def audience(conn, task: dict) -> str:
    """Who a message task is for: the audience of its template («client», «manager»); empty for another task or a template gone.
    «Можно забирать» that waited (ARRIVED_RULE) is the client's always, with the owner's template or with the sample."""
    if task["kind"] == "message" and task.get("rule_id") == ARRIVED_RULE:
        return "client"
    if task["kind"] != "message" or not str(task["arg"]).isdigit():
        return ""
    try:
        return templates.get_template(conn, int(task["arg"]))["audience"]
    except OrderError:
        return ""


def send_target(conn, task: dict, prefer=None) -> Tuple[Optional[dict], str]:
    """The Avito chat a message to the client goes to (the preferred chat when it is the order's and may write, else the newest that may),
    and why it cannot go when there is none. Messages to the manager never go to a chat of the client."""
    if audience(conn, task) != "client":
        return None, "Это сообщение не клиенту: в чат Авито оно не отправляется."
    chats = [{"id": r[0], "account_id": r[1], "may": bool(r[2] and r[3])} for r in conn.execute(
        "SELECT c.id, c.account_id, a.chats_enabled, a.chats_send FROM avito_chat c LEFT JOIN account a ON a.id = c.account_id WHERE c.order_id = ? "
        "ORDER BY c.last_at DESC, c.id DESC", (task["order_id"],))]
    if not chats:
        return None, "У заказа нет чата Авито: отправьте текст вручную и отметьте «Отправлено вручную»."
    may = [c for c in chats if c["may"]]
    if not may:
        return None, "У аккаунта чата выключена отправка ответов (Подключения → Авито → аккаунт)."
    return next((c for c in may if c["id"] == prefer), may[0]), ""


IN_PANEL = "Точка «%s» работает в панели: в ВК ей не пишем, заказ виден на её «Моей точке». Это сообщение можно пропустить."


def point_target(conn, task: dict) -> Tuple[Optional[dict], str]:
    """The point a message to the manager goes to in VK (the order's point with a bound VK, not working in the panel), and why it cannot go
    when there is none.
    Messages to the client never go to a point."""
    if audience(conn, task) != "manager":
        return None, "Это сообщение не менеджеру точки: в ВК оно не отправляется."
    point = orders.get_card(conn, task["order_id"]).get("point")
    if not point:
        return None, "У заказа нет точки: выберите точку в окне заказа или отправьте текст сами и отметьте «Отправлено вручную»."
    if points.in_panel(point):                                     # the owner 2026-10-23: the bot writes nothing to a point in the panel
        return None, IN_PANEL % point["name"]
    if not str(point.get("vk_peer_id") or "").strip():
        return None, "У точки «%s» не привязан ВК (Подключения → Точки): отправьте текст сами и отметьте «Отправлено вручную»." % point["name"]
    return point, ""


def vk_random_id(task: dict) -> int:
    """The random_id of VK for a task: the same for every press of it, so VK does not deliver a repeat (after a cut-off answer) twice."""
    seed = "task-%d-%s" % (task["id"], "".join(ch for ch in str(task["created_at"]) if ch.isdigit()))
    return int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8], 16) % (2 ** 31 - 2) + 1


SENT_TO_POINT = "отправлено точке в ВК"
NO_VK_KEY = "ВК не настроен: нет ключа сообщества (Подключения → ВК). Отправьте текст сами и отметьте «Отправлено вручную»."


def send_point_task(conn, task_id, text: str, send, now: str, author: str, point_id=None, bot: bool = False) -> str:
    """Send a message task to the manager of the order's point in VK by a person's press (or order_placed): `send(peer_id, text, random_id=)`
    is vk_send.sender_for, None without the key of the community. The task is taken first (a double press finds it taken); a failed send
    leaves the task waiting, and a repeat carries the same random_id. `point_id`: the point the window named. `bot`: sent by itself
    (order_placed), so signed «(бот) » (the owner 2026-10-24); a person's press is not. Returns the point's name."""
    task = get_task(conn, task_id)
    if task["kind"] != "message":
        raise OrderError("Это не сообщение.")
    if task["status"] in ("pending", "error", "running"):
        refresh_tasks(conn, task["order_id"], now)
        task = get_task(conn, task_id)
        if task["status"] == "error":
            raise OrderError(task["result"])
    if task["status"] != "pending":
        raise OrderError("Эта задача уже выполняется или выполнена.")
    point, why = point_target(conn, task)
    if point is None:
        raise OrderError(why)
    if point_id is not None and point["id"] != point_id:          # the window named another point: never send where the person did not see
        raise OrderError("Точка заказа изменилась: обновите страницу.")
    if send is None:
        raise OrderError(NO_VK_KEY)
    if len(text.strip()) > vk_send.VK_LIMIT:                       # VK would cut it: never a shortened text marked as sent in full
        raise OrderError("Текст для ВК не больше %d знаков: сократите его в окне «Сообщения по заказу»." % vk_send.VK_LIMIT)
    claim = conn.execute("UPDATE order_task SET status = 'running', done_at = ? WHERE id = ? AND status = 'pending'", (now, task_id))
    conn.commit()
    if claim.rowcount != 1:
        raise OrderError("Эта задача уже выполняется или выполнена.")
    try:
        from .order_manager import signed
        send(str(point["vk_peer_id"]).strip(), signed(text) if bot else text, random_id=vk_random_id(task))
    except BaseException as err:
        conn.rollback()
        conn.execute("UPDATE order_task SET status = 'pending', done_at = NULL WHERE id = ? AND status = 'running'", (task_id,))
        conn.commit()
        if isinstance(err, vk_send.VkError) and str(err).startswith("ВК недоступен"):     # no answer: VK may have taken it
            raise vk_send.VkError("%s Неизвестно, ушло ли: повторите тем же нажатием, текст не меняйте (ВК не задвоит его)." % err) from None
        raise
    conn.execute("UPDATE order_task SET body = ? WHERE id = ? AND status = 'running'", (text, task_id))
    _finish(conn, task, "done", "%s (точка «%s»)%s" % (SENT_TO_POINT, point["name"], "" if text.strip() == str(task["body"] or "").strip() else ", текст изменён"),
            now, author, text)
    return point["name"]


_SENT_TO_CLIENT_RECORD = re.compile(r"^(?P<title>.+?): выполнено \(%s[^\n]*\):\n" % SENT_TO_CLIENT)
_PLACED_RECORD = re.compile(r"^(?P<head>Сообщение клиенту после оформления в WebMIM \(«[^\n]*»\)):?\n")
_CHAT_REPLY_RECORD = "Отправлен ответ клиенту: "
_CLIENT_WROTE_RECORD = "Клиент написал: "                         # chats: the client's last words («после закрытия» quotes none)


def for_point(event: dict) -> dict:
    """A record of the journal of an order NOT from the point's own Avito account as its manager sees it: a message to the client and the
    client's words are named, their text is not (the client's correspondence, «Права точек», the owner 2026-10-22). The message to the point
    itself keeps its text. The point's own orders and the owner: every record whole."""
    text = str(event.get("text") or "")
    ours = event.get("kind") == "rule"                             # the panel's own records only: a person's note is never cut
    sent = ours and _SENT_TO_CLIENT_RECORD.match(text)
    placed = ours and _PLACED_RECORD.match(text)
    if sent:
        text = "Клиенту отправлено сообщение «%s»" % sent.group("title")
    elif placed:                                                   # the hook's record of a message to the client (with its text before 2026-10-23)
        text = placed.group("head")
    elif event.get("kind") == "chat" and text.startswith(_CHAT_REPLY_RECORD):       # chats.send_reply: the reply's first words
        text = _CHAT_REPLY_RECORD.rstrip(": ")
    elif event.get("kind") == "chat" and text.startswith(_CLIENT_WROTE_RECORD):
        text = "Клиент написал сообщение"
    else:
        return event
    return dict(event, text=text)


def new_messages(conn, card_id, moved_at: str) -> List[dict]:
    """The waiting messages (to anybody) that the move made at `moved_at` has just created: the window «Сообщения по заказу» opens for them."""
    return [t for t in list_tasks(conn, card_id, ("pending",)) if t["kind"] == "message" and t["created_at"] == moved_at]


def send_task(conn, task_id, text: str, deliver, now: str, author: str, prefer=None) -> str:
    """Send a message task to the client by a person's press: `deliver(chat_row, text, token)` sends it (chats.send_reply). The task is taken
    first (a double press finds it taken) and the send mark is the task's own, so the same task is never sent twice; a failed send leaves
    the task waiting. Returns the chat row it went to."""
    task = get_task(conn, task_id)
    if task["kind"] != "message":
        raise OrderError("Это не сообщение.")
    if task["status"] in ("pending", "error", "running"):               # running: one cut off by a killed panel comes back after a while
        refresh_tasks(conn, task["order_id"], now)
        task = get_task(conn, task_id)
        if task["status"] == "error":
            raise OrderError(task["result"])
    if task["status"] != "pending":
        raise OrderError("Эта задача уже выполняется или выполнена.")
    chat, why = send_target(conn, task, prefer)
    if chat is None:
        raise OrderError(why)
    if prefer is not None and chat["id"] != prefer:                # the window named another chat: never send where the person did not see
        raise OrderError("Чат Ч-%d больше не может принять это сообщение: обновите страницу." % prefer)
    claim = conn.execute("UPDATE order_task SET status = 'running', done_at = ? WHERE id = ? AND status = 'pending'", (now, task_id))
    conn.commit()
    if claim.rowcount != 1:
        raise OrderError("Эта задача уже выполняется или выполнена.")
    token = task_token(task)
    earlier = conn.execute("SELECT 1 FROM chat_send_log WHERE token = ? AND status = 'sent'", (token,)).fetchone()
    try:
        deliver(chat["id"], text, token)                           # an earlier press that went out is not sent again (the mark says «sent»)
    except BaseException:
        conn.rollback()
        conn.execute("UPDATE order_task SET status = 'pending', done_at = NULL WHERE id = ? AND status = 'running'", (task_id,))
        conn.commit()
        raise
    if earlier:                                                    # its record was lost: the text in the chat is the one of that press, not this one
        _finish(conn, task, "done", SENT_TO_CLIENT + " раньше (текст — в чате)", now, author)
        return chat["id"]
    conn.execute("UPDATE order_task SET body = ? WHERE id = ? AND status = 'running'", (text, task_id))
    _finish(conn, task, "done", SENT_TO_CLIENT + ("" if text.strip() == str(task["body"] or "").strip() else ", текст изменён"), now, author, text)
    return chat["id"]


def task_token(task: dict) -> str:
    """The send mark of a message task: «task-<id>-<digits of created_at>»; «можно забирать» that waited (ARRIVED_RULE) keeps the mark of its
    arrival («arrived-…», order_manager): one mark for the message sent by itself, by a press or after a chat is bound — never two sends."""
    if task.get("rule_id") == ARRIVED_RULE and str(task.get("entered_at") or "").startswith("arrived-"):
        return task["entered_at"]
    return "task-%d-%s" % (task["id"], "".join(ch for ch in task["created_at"] if ch.isdigit()))


def task_of_token(conn, token) -> Optional[dict]:
    """The message task whose send mark `token` is (task_token), or None: another mark, or the task is gone."""
    arrived = re.fullmatch(r"arrived-(\d+)-\d*", str(token or ""))
    if arrived:                                                    # the order is in the mark: the UNIQUE index (order, rule, entered_at) finds it
        row = conn.execute("SELECT %s FROM order_task WHERE order_id = ? AND rule_id = ? AND entered_at = ?" % TASK_FIELDS,
                           (int(arrived.group(1)), ARRIVED_RULE, str(token))).fetchone()
        return _task(row) if row else None
    found = re.fullmatch(r"task-(\d+)-(\d*)", str(token or ""))
    if not found:
        return None
    row = conn.execute("SELECT %s FROM order_task WHERE id = ?" % TASK_FIELDS, (int(found.group(1)),)).fetchone()
    if row is None:
        return None
    task = _task(row)
    return task if "".join(ch for ch in str(task["created_at"]) if ch.isdigit()) == found.group(2) else None


def close_after_send(conn, token, text: str, now: str, how: str) -> bool:
    """The minute's sync delivered the message of a task (sent again after a 402, sent again once after it was not found) or found it in the
    chat (chats.settle_sends): the task is done, as after a person's press, and not left «ждёт». False when there is no such task waiting."""
    task = task_of_token(conn, token)
    if task is None or task["kind"] != "message" or task["status"] not in ("pending", "running", "error"):
        return False
    if text and text.strip() != str(task["body"] or "").strip():
        conn.execute("UPDATE order_task SET body = ? WHERE id = ? AND status IN ('pending', 'running', 'error')", (text, task["id"]))
    _finish(conn, task, "done", "%s системой (%s)" % (SENT_TO_CLIENT, how), now, chats.SYSTEM, text)
    return True


def skip_task(conn, task_id, now: str, author: str) -> None:
    task = get_task(conn, task_id)
    if task["status"] not in ("pending", "error"):
        raise OrderError("Эта задача уже обработана.")
    _finish(conn, task, "skipped", "", now, author)
