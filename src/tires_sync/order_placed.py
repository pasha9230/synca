"""What happens by itself once an order is placed in WebMIM (the owner 2026-10-23: «я хочу чтобы шаблон отправлялся сам после оформленного
заказа в мим», «Да, и точке само»): the one hook `on_placed`, called after the first full success of «Оформить в WebMIM» (the order and all
its positions made, the number in the card). The client hears «Клиенту: заказ оформлен» in the order's Avito chat; the manager of the order's
point hears «Менеджеру: заказ оформлен» in VK (through the community).

Once per order: each message is a task of the order (order_task) under the system rule number RULE and `entered_at` «webmim-<WebMIM id>»
(the client's) or «webmim-<WebMIM id>-point» (the point's); the table's UNIQUE (order_id, rule_id, entered_at) with INSERT OR IGNORE lets no
second press, «Досоздать» nor second worker make another. It goes the way a person's «Отправить» goes (order_engine.send_task →
chats.send_reply, order_engine.send_point_task → vk_send: the task is taken first, the send mark is the task's own). What does not go stays a
waiting message of the card (the window «Сообщения по заказу»), with a red record and a warning: a lacking value, no chat, no VK, a failed
send. The template is the one of the exact name and the audience; renamed or gone, nothing is sent (no text of our own). A point that works
in the panel (channel «panel», the owner 2026-10-23) gets nothing in VK: a grey record says so, the order is on its «Рабочее место».
A message of the same template the order has already (the rule of the stage «Заказ оформлен» prepares it, the owner 2026-10-23): sent — not
again; waiting — that task is sent, no second one; skipped by a person — not sent (the person chose so); lacking values — it waits."""
from __future__ import annotations

import logging
from typing import Callable, List, Optional

from . import chats, order_engine as engine, order_points as points, order_templates as templates, orders, vk_send

CLIENT_TEMPLATE = "Клиенту: заказ оформлен"          # the names of the owner's templates (order_templates.SAMPLES; a test keeps them equal)
POINT_TEMPLATE = "Менеджеру: заказ оформлен"
RULE = engine.PLACED_RULE                            # the «rule» number of these system tasks (order_wait has 0 and −1); a rule of a stage
                                                     # met with the same template later makes its own task «skipped» (order_engine._make_task)
AUTHOR = "после оформления в WebMIM"
MERGED = ("Сообщения «заказ оформлен» клиенту и точке не отправлены: заказ объединён, клиент и точка уже знают. Отправить можно вручную "
          "из шаблонов.")                       # a merged order already told: information, not an error (the lead 2026-10-30)
IN_PANEL = "работает в панели: "                     # the line of a point in the panel (the panel shows it as information, not an error)
WAITING = "Сообщение ждёт в окне «Сообщения по заказу» карточки: отправьте его оттуда."
Deliver = Callable[[int, str, str], None]            # (chat row, text, token): chats.send_reply of the chat's account; raises when it did not go
SendVk = Callable[..., int]                          # (peer_id, text, random_id=): vk_send.sender_for; raises VkError when it did not go

CLIENT = {"template": CLIENT_TEMPLATE, "audience": "client", "suffix": "", "whom": "клиенту", "Whom": "Клиенту"}
POINT = {"template": POINT_TEMPLATE, "audience": "manager", "suffix": "-point", "whom": "точке", "Whom": "Точке"}


def on_placed(conn, card_id, webmim_id: str, now: str, author: str, deliver: Optional[Deliver], send_vk: Optional[SendVk] = None) -> List[str]:
    """Everything due once the order is placed in WebMIM; returns the lines for the result of the press (warnings among them). Never raises.
    `send_vk` None: the key of the community is not saved, the point's message waits. A merged order whose source was placed already
    (order_merge.told_placed): the client and the point heard it, nothing is sent; a grey line says so (the lead 2026-10-30)."""
    from . import order_merge
    if order_merge.told_placed(conn, card_id):
        line = MERGED
        try:
            orders.add_event(conn, card_id, now, author, "notify", line, level="grey")
            conn.commit()
        except Exception:                                                # noqa: BLE001  never raises: the press itself is done
            conn.rollback()
        return [line]
    return [tell_client(conn, card_id, webmim_id, now, author, deliver), tell_point(conn, card_id, webmim_id, now, author, send_vk)]


def _template(conn, who: dict, card_id=None, now: str = "") -> Optional[dict]:
    """The owner's template of the name; a message that came with a later release (`who["sample"]`, order_intercity) and is not in the base
    yet is made from its sample first (nobody has to press «Добавить образцы»), then it is the owner's to edit."""
    row = conn.execute("SELECT id, body FROM order_template WHERE name = ? AND audience = ? ORDER BY id LIMIT 1", (who["template"], who["audience"])).fetchone()
    if row is None and who.get("sample"):
        templates.add_template(conn, who["template"], who["audience"], who["sample"])
        if card_id is not None:                                             # the owner 2026-11-02: seen in the order's journal
            orders.add_event(conn, card_id, now, who.get("author", AUTHOR), "chat", "Шаблона «%s» не было в базе: создан из образца, "
                             "его можно поправить в «Шаблонах сообщений»." % who["template"], level="grey")
            conn.commit()
        row = conn.execute("SELECT id, body FROM order_template WHERE name = ? AND audience = ? ORDER BY id LIMIT 1", (who["template"], who["audience"])).fetchone()
    return {"id": row[0], "body": row[1]} if row else None


def _fill_in(who: dict, missing: List[str], conn=None) -> str:
    """What to do about lacking values. The transfer number comes later, by hand (the owner 2026-10-23: «Ждать номер»), or from MIM's
    cabinet when it is connected (webmim_cabinet, the owner 2026-11-09): then the message goes by itself (send_waiting)."""
    if templates.VARIABLES["transfer_number"] in missing:
        from . import webmim_cabinet
        if conn is not None and webmim_cabinet.enabled(conn) and len(missing) == 1:
            return ("Сообщение %s уйдёт само, если номер перемещения придёт из кабинета MIM; не придёт — впишите его и отправьте сообщение "
                    "в окне «Сообщения по заказу»." % who["whom"])
        return "Сообщение %s подготовится, когда впишете номер перемещения: потом отправьте его в окне «Сообщения по заказу»." % who["whom"]
    return "Заполните поля заказа: сообщение ждёт в окне «Сообщения по заказу»."


def _not_sent(conn, card_id, who: dict, now: str, why: str, what: str) -> str:
    orders.add_event(conn, card_id, now, who.get("author", AUTHOR), "chat", "Сообщение %s «%s» не отправлено: %s" % (who["whom"], who["template"], why), level="red")
    conn.commit()
    return "%s не отправлено «%s»: %s %s" % (who["Whom"], who["template"], why, what)


def _earlier(conn, card_id, template_id, undone: str = "") -> Optional[dict]:
    """The order's message task of this template made before (by a rule of a stage, or by this hook): the most advanced one, sent first.
    `undone`: the result of a task skipped because its occasion was undone (order_intercity): not a person's choice, not counted."""
    found = [t for t in engine.list_tasks(conn, card_id) if t["kind"] == "message" and str(t["arg"]) == str(template_id)
             and not (undone and t["status"] == "skipped" and t["result"] == undone)]
    rank = {"done": 0, "running": 1, "pending": 2, "error": 3, "skipped": 4}
    return min(found, key=lambda t: (rank.get(t["status"], 5), t["id"])) if found else None


def _use_earlier(conn, card_id, who: dict, task: dict, now: str, author: str, send) -> str:
    if task["status"] == "done":
        orders.add_event(conn, card_id, now, who.get("author", AUTHOR), "chat", "Сообщение %s «%s» уже отправлено %s: второй раз не отправляется." % (
            who["whom"], who["template"], who.get("author", AUTHOR) if task["rule_id"] == who.get("rule", RULE) else "по правилу этапа"))
        conn.commit()
        return "%s «%s» уже было отправлено раньше: второе не отправляется." % (who["Whom"], who["template"])
    if task["status"] == "skipped":
        orders.add_event(conn, card_id, now, who.get("author", AUTHOR), "chat", "Сообщение %s «%s» было пропущено вручную: само не отправляется." % (who["whom"], who["template"]))
        conn.commit()
        return "Сообщение %s «%s» было пропущено вручную: не отправлено." % (who["whom"], who["template"])
    if task["status"] == "running":
        return "Сообщение %s «%s» уже отправляется." % (who["whom"], who["template"])
    engine.refresh_tasks(conn, card_id, now)                     # its text as the card is now (the WebMIM number has just come)
    task = engine.get_task(conn, task["id"])
    if task["status"] == "error":
        lacking = task["result"].split(":", 1)[-1].rstrip(".").strip()
        return _not_sent(conn, card_id, who, now, "не хватает: %s." % lacking, _fill_in(who, [n.strip() for n in lacking.split(",")], conn))
    return _send(conn, card_id, who, task, now, author, send)


def _send(conn, card_id, who: dict, task: dict, now: str, author: str, send) -> str:
    """Send a waiting message task the way a person's press does; a warning when it cannot go (the task stays waiting)."""
    if who["audience"] == "client":
        chat, why = engine.send_target(conn, task)
        if chat is None:
            return _not_sent(conn, card_id, who, now, why, WAITING)
        if send is None:
            return _not_sent(conn, card_id, who, now, "ключи Авито недоступны.", WAITING)
    else:
        point, why = engine.point_target(conn, task)
        if point is None:
            return _not_sent(conn, card_id, who, now, why, WAITING)
        if send is None:
            return _not_sent(conn, card_id, who, now, engine.NO_VK_KEY, WAITING)
    try:
        if who["audience"] == "client":
            sent_to = engine.send_task(conn, task["id"], task["body"], send, now, author)
        else:
            sent_to = engine.send_point_task(conn, task["id"], task["body"], send, now, author, bot=True)       # sent by itself: «(бот) »
    except chats.ChatSendUnknown as err:                           # a 402 (the sync sends it again) or a lost one: its own words only (L3)
        if isinstance(err, (chats.ChatSendRetry, chats.ChatSendLost)):
            return _not_sent(conn, card_id, who, now, str(err), "").rstrip()
        return _not_sent(conn, card_id, who, now, "неизвестно, ушло ли.", "Проверьте чат Авито заказа: если сообщение там есть, повторно не отправляйте.")
    except (orders.OrderError, chats.ChatError, vk_send.VkError) as err:
        return _not_sent(conn, card_id, who, now, str(err)[:400], WAITING)      # 400: Avito's answer is in it
    if who["audience"] == "client":
        return "Клиенту отправлено «%s» в чат Ч-%d." % (who["template"], sent_to)
    return "Точке «%s» отправлено «%s» в ВК." % (sent_to, who["template"])


def _tell(conn, card_id, who: dict, webmim_id: str, now: str, author: str, send) -> str:
    """The message `who` of the order once per WebMIM order."""
    try:
        template = _template(conn, who, card_id, now)
        if template is None:
            return _not_sent(conn, card_id, who, now, "шаблона с таким именем «%s» нет (переименован или удалён)." % who["whom"],
                             "Сохраните его в «Настройка → Шаблоны сообщений» или напишите %s сами." % who["whom"])
        point = orders.get_card(conn, card_id).get("point") if who is POINT else None
        if points.in_panel(point):                                # the owner 2026-10-23: the point sees the order on «Рабочее место», nothing in VK
            line = "Точка «%s» %s«%s» в ВК не отправляется, заказ виден на её «Рабочем месте»." % (point["name"], IN_PANEL, who["template"])
            orders.add_event(conn, card_id, now, AUTHOR, "chat", line, level="grey")
            conn.commit()
            for task in engine.list_tasks(conn, card_id, ("pending", "error")):      # the rule's own (made before the hook): nobody skips it by hand
                if task["kind"] == "message" and str(task["arg"]) == str(template["id"]):
                    try:
                        engine.skip_task(conn, task["id"], now, AUTHOR)
                    except orders.OrderError:                     # a person took it meanwhile: theirs
                        conn.rollback()
            return line
        earlier = _earlier(conn, card_id, template["id"], who.get("undone", ""))
        if earlier is not None:
            return _use_earlier(conn, card_id, who, earlier, now, author, send)
        card = orders.get_card(conn, card_id)
        text, missing = templates.render(conn, card, template["body"], now)
        made = conn.execute("INSERT OR IGNORE INTO order_task (order_id, rule_id, entered_at, kind, arg, stage_id, status, title, body, result, created_at) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (card_id, who.get("rule", RULE), who.get("key") or "webmim-%s%s" % (webmim_id, who["suffix"]), "message", str(template["id"]), card["stage_id"],
                             "error" if missing else "pending", who["template"], "" if missing else text,
                             "Не хватает: %s." % ", ".join(missing) if missing else "", now))
        if made.rowcount != 1:                                    # this WebMIM order has its message already (sent, waiting or skipped)
            conn.commit()
            return "Сообщение %s об оформлении уже было подготовлено: второе не отправляется." % who["whom"]
        task_id = made.lastrowid
        orders.add_event(conn, card_id, now, who.get("author", AUTHOR), "rule", "Сообщение %s %s («%s») подготовлено." % (
            who["whom"], who.get("author", AUTHOR), who["template"]))
                                                                  # its text: in the record of the send (order_engine._finish), never twice
        conn.commit()
        if missing:
            return _not_sent(conn, card_id, who, now, "не хватает: %s." % ", ".join(missing), _fill_in(who, missing, conn))
        return _send(conn, card_id, who, engine.get_task(conn, task_id), now, author, send)
    except Exception as err:                                      # the order is placed; only the message did not go
        conn.rollback()
        logging.getLogger("tires_sync.orders").exception("the message %s after placing order %s in WebMIM", who["whom"], card_id)
        try:
            return _not_sent(conn, card_id, who, now, "сбой отправки (%s)." % err.__class__.__name__, "Проверьте сообщения и журнал заказа.")
        except Exception:
            conn.rollback()
            return "%s сообщение не отправлено: сбой панели, проверьте сообщения и журнал заказа." % who["Whom"]


def tell_client(conn, card_id, webmim_id: str, now: str, author: str, deliver: Optional[Deliver]) -> str:
    """«Клиенту: заказ оформлен» to the client of the order, once per WebMIM order; an order to another city gets «Клиенту: заказ в другой
    город оформлен» instead (order_intercity, the owner 2026-11-02), under the same mark: one of them, never both."""
    from . import order_intercity
    return _tell(conn, card_id, order_intercity.PLACED_CLIENT if order_intercity.is_on(conn, card_id) else CLIENT, webmim_id, now, author, deliver)


def tell_point(conn, card_id, webmim_id: str, now: str, author: str, send_vk: Optional[SendVk]) -> str:
    """«Менеджеру: заказ оформлен» to the manager of the order's point in VK, once per WebMIM order."""
    return _tell(conn, card_id, POINT, webmim_id, now, author, send_vk)


def send_waiting(conn, card_id, now: str, author: str, deliver: Optional[Deliver], send_vk: Optional[SendVk] = None) -> List[str]:
    """The transfer number has just come by itself (webmim_cabinet.take, its one write): the messages «заказ оформлен» to the client and the
    point that waited for it (a task in «error» lacking the transfer number) go now, the very way on_placed sends them (_use_earlier: the
    text as the card is now, the task's claim, the send mark). A message sent, being sent, skipped by a person, waiting for a chat, or not
    made at all is left as it is: nothing new is made here, so nothing goes twice. A task of a rule of a stage goes only while the order is
    still in that very stay of its stage (the review 2026-11-09: an order moved on or back since never hears «оформлен» late). Never raises."""
    from . import order_intercity, order_merge
    try:
        if order_merge.told_placed(conn, card_id):
            return []
        whom = [(order_intercity.PLACED_CLIENT if order_intercity.is_on(conn, card_id) else CLIENT, deliver), (POINT, send_vk)]
    except Exception:                                                     # noqa: BLE001
        conn.rollback()
        logging.getLogger("tires_sync.orders").exception("the waiting messages of order %s", card_id)
        return []
    lacking = templates.VARIABLES["transfer_number"]
    waiting = []                                   # both found first: the first send brings every task of the card up to date
    try:
        card = orders._row(conn, card_id)
        for who, send in whom:
            row = conn.execute("SELECT id FROM order_template WHERE name = ? AND audience = ? ORDER BY id LIMIT 1",
                               (who["template"], who["audience"])).fetchone()
            task = _earlier(conn, card_id, row[0], who.get("undone", "")) if row else None
            if task is not None and task["rule_id"] != who.get("rule", RULE) and (
                    task["stage_id"] != card["stage_id"] or task["entered_at"] != card["entered_at"]):
                continue                                                  # made by a stage the order has left since: a person decides
            if task is not None and task["status"] == "error" and lacking in str(task["result"]):
                waiting.append((who, send, task))
    except Exception:                                                     # noqa: BLE001
        conn.rollback()
        logging.getLogger("tires_sync.orders").exception("the waiting messages of order %s", card_id)
        return []
    lines = []
    for who, send, task in waiting:
        try:
            lines.append(_use_earlier(conn, card_id, who, engine.get_task(conn, task["id"]), now, author, send))
        except Exception as err:                                          # noqa: BLE001  the number is written; only the message did not go
            conn.rollback()
            logging.getLogger("tires_sync.orders").exception("the message %s of order %s after the transfer number", who["whom"], card_id)
            try:
                lines.append(_not_sent(conn, card_id, who, now, "сбой отправки (%s)." % err.__class__.__name__, "Проверьте сообщения и журнал заказа."))
            except Exception:                                             # noqa: BLE001
                conn.rollback()
    return lines
