"""«Доставка в другой город» (the owner 2026-11-02): the client pays to our account, we book the transport company (ПЭК or КИТ) with a
pickup at the point, place the WebMIM order to the point, download the labels from the company's site; the point hands the tyres with the
labels to the company's courier.

The order keeps the checkbox, the city, the company and the track number (order_intercity) and the labels as PDF files (order_label); only
the office writes them (the routes). For such an order there is no reservation at the point (orders._start_reserve), no «можно забирать»
(order_manager.tell_client_arrived, order_arrived), no reminder of the reservation nor the move to «На ОТХ» after it (order_reserve), no
question of the seventh day on «На ОТХ» (order_oth), no «Клиент не пришёл»; «Выдал» of the point is «Передал курьеру ТК» (`ship`, also the
VK words «отправил N», «передал курьеру N»): the order goes to «Отправлен» (a successful stage, as «Выполнено»), the client hears
«Клиенту: заказ отправлен», the owner and the partner are told, and a track number not filled yet is a task «Внести трек-номер» for them.
The client of a placed order hears «Клиенту: заказ в другой город оформлен» instead of «Клиенту: заказ оформлен» (order_placed)."""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta
from typing import Callable, List, Optional, Tuple

from . import order_templates as templates, orders
from .orders import OrderError

CARRIERS = ("ПЭК", "КИТ")
TAG = "В другой город"
HINT = "Отдать курьеру ТК: шины + этикетки"
CITY_MAX, TRACK_MAX, NAME_MAX = 60, 60, 120
LABEL_MAX = 1_900_000                            # bytes: the panel takes 2 MB a request at most (MAX_CONTENT_LENGTH)
LABELS_MAX = 10
TRACK = re.compile(r"^[0-9A-Za-zА-Яа-яЁё\-/ ]*$")
SHIP_FROM = ("at_point", "placed", "moving")      # as «выдал»: the panel may still say the tyres are on the way
PLACED_TEMPLATE = "Клиенту: заказ в другой город оформлен"
SHIPPED_TEMPLATE = "Клиенту: заказ отправлен"
PLACED_CLIENT = {"template": PLACED_TEMPLATE, "audience": "client", "suffix": "", "whom": "клиенту", "Whom": "Клиенту",    # order_placed
                 "sample": next(b for n, _a, b in templates.SAMPLES if n == PLACED_TEMPLATE)}
SHIPPED_RULE = -4                                # the «rule» number of «заказ отправлен» (order_engine: −2 placed, −3 arrived)
SHIPPED_CLIENT = {"template": SHIPPED_TEMPLATE, "audience": "client", "suffix": "", "whom": "клиенту", "Whom": "Клиенту", "rule": SHIPPED_RULE,
                  "author": "после передачи курьеру ТК", "undone": "отметка «Передал курьеру ТК» отменена", "sample": next(b for n, _a, b in templates.SAMPLES if n == SHIPPED_TEMPLATE)}
TRACK_TASK = "Внести трек-номер"
SKIP = "заказ в другой город: клиент не приходит на точку"          # order_engine: a rule's message of the point's pickup
FIELD_NAMES = {"enabled": "Доставка в другой город", "city": "Город", "carrier": "ТК", "track": "Трек-номер"}
Tell = Callable[[List[str], str], object]


def of(conn, order_id) -> dict:
    """{"on", "city", "carrier", "track"} of the order (all empty when it has none)."""
    row = conn.execute("SELECT enabled, city, carrier, track FROM order_intercity WHERE order_id = ?", (order_id,)).fetchone()
    return {"on": bool(row and row[0]), "city": row[1] if row else "", "carrier": row[2] if row else "", "track": row[3] if row else ""}


def enabled_ids(conn) -> set:
    return {r[0] for r in conn.execute("SELECT order_id FROM order_intercity WHERE enabled = 1")}


def is_on(conn, order_id) -> bool:
    return of(conn, order_id)["on"]


def on(card: dict) -> bool:
    """The card (orders.get_card) is an order to another city."""
    return bool((card.get("intercity") or {}).get("on"))


def _line(value, limit: int, what: str) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        raise OrderError("%s: не больше %d знаков." % (what, limit))
    return text


def save(conn, card_id, form: dict, now: str, author: str) -> List[str]:
    """The checkbox and the fields from the card's form; a record of the journal per change. Returns the names changed. OrderError: a value
    that does not fit (the company not one of CARRIERS, a track number of other signs) or the checkbox taken off an order already sent."""
    card = orders.get_card(conn, card_id)
    was = of(conn, card_id)
    new = {"on": str(form.get("enabled") or "") in ("1", "on", "true"), "city": _line(form.get("city"), CITY_MAX, "Город"),
           "carrier": _line(form.get("carrier"), 10, "ТК"), "track": _line(form.get("track"), TRACK_MAX, "Трек-номер")}
    if new["carrier"] and new["carrier"] not in CARRIERS:
        raise OrderError("ТК: %s." % " или ".join(CARRIERS))
    if not TRACK.match(new["track"]):
        raise OrderError("Трек-номер: буквы, цифры, дефис и косая черта.")
    if was["on"] and not new["on"] and (card["stage"]["code"] == orders.SHIPPED or (card["stage"]["code"] == "done" and was_shipped(conn, card))):
        raise OrderError("Заказ уже отправлен в другой город: галочку не снять. Сначала перенесите заказ на другой этап.")
    changed = [key for key in ("on", "city", "carrier", "track") if new[key] != was[key]]
    if not changed:
        return []
    conn.execute("INSERT INTO order_intercity (order_id, enabled, city, carrier, track, updated_at, updated_by) VALUES (?,?,?,?,?,?,?) "
                 "ON CONFLICT(order_id) DO UPDATE SET enabled = excluded.enabled, city = excluded.city, carrier = excluded.carrier, "
                 "track = excluded.track, updated_at = excluded.updated_at, updated_by = excluded.updated_by",
                 (int(card_id), 1 if new["on"] else 0, new["city"], new["carrier"], new["track"], now, author))
    for key in changed:
        if key == "on":
            text = "Доставка в другой город: %s" % ("да" if new["on"] else "нет")
        else:
            text = "%s: %s" % (FIELD_NAMES[key], new[key] or "(пусто)")
        orders.add_event(conn, card_id, now, author, "field", text)
    conn.commit()
    if "track" in changed and new["track"]:
        close_track_tasks(conn, card_id, "done", now, author, "трек-номер %s" % new["track"])
    return [FIELD_NAMES["enabled" if key == "on" else key] for key in changed]


# ---- the labels -----------------------------------------------------------------------------------------------------------------------

def labels(conn, order_id) -> List[dict]:
    return [dict(zip(("id", "name", "size", "uploaded_at", "uploaded_by"), r)) for r in conn.execute(
        "SELECT id, name, size, uploaded_at, uploaded_by FROM order_label WHERE order_id = ? ORDER BY id", (order_id,))]


def add_label(conn, card_id, filename, data: bytes, now: str, author: str) -> int:
    """One PDF of the transport company's labels into the order. OrderError: not an order to another city, no file, not a PDF, too big,
    too many."""
    if not is_on(conn, card_id):
        raise OrderError("Этикетки — для заказа в другой город: сначала отметьте «Доставка в другой город».")
    name = os.path.basename(str(filename or "").replace("\\", "/")).strip()[:NAME_MAX]
    if not data:
        raise OrderError("Выберите файл этикеток (PDF).")
    if not name.lower().endswith(".pdf") or not bytes(data[:5]) == b"%PDF-":
        raise OrderError("Этикетки: только файл PDF.")
    if len(data) > LABEL_MAX:
        raise OrderError("Файл больше %.1f МБ: скачайте этикетки с сайта ТК ещё раз или по частям." % (LABEL_MAX / 1_000_000))
    if conn.execute("SELECT COUNT(*) FROM order_label WHERE order_id = ?", (card_id,)).fetchone()[0] >= LABELS_MAX:
        raise OrderError("В заказе уже %d файлов этикеток: удалите лишние." % LABELS_MAX)
    label_id = conn.execute("INSERT INTO order_label (order_id, name, size, data, uploaded_at, uploaded_by) VALUES (?,?,?,?,?,?)",
                            (int(card_id), name, len(data), bytes(data), now, author)).lastrowid
    orders.add_event(conn, card_id, now, author, "field", "Этикетки ТК: загружен файл «%s»" % name)
    conn.commit()
    return label_id


def label_file(conn, card_id, label_id) -> Tuple[str, bytes]:
    row = conn.execute("SELECT name, data FROM order_label WHERE id = ? AND order_id = ?", (label_id, card_id)).fetchone()
    if row is None:
        raise OrderError("Такого файла этикеток нет.")
    return row[0], bytes(row[1])


def delete_label(conn, card_id, label_id, now: str, author: str) -> None:
    name, _data = label_file(conn, card_id, label_id)
    conn.execute("DELETE FROM order_label WHERE id = ? AND order_id = ?", (label_id, card_id))
    orders.add_event(conn, card_id, now, author, "field", "Этикетки ТК: удалён файл «%s»" % name)
    conn.commit()


# ---- «Передал курьеру ТК» -------------------------------------------------------------------------------------------------------------

def _label(card: dict) -> str:
    return str(card.get("webmim_number") or card["id"])


def _stage(conn, code) -> Optional[dict]:
    return next((s for s in orders.list_stages(conn) if s["code"] == code), None)


def was_shipped(conn, card: dict) -> bool:
    """The order went through «Отправлен» (it is on «Выполнено» after it, the owner 2026-11-02): its «заказ отправлен» was made and not
    undone by the point's «Отменить» (a task under SHIPPED_RULE, whatever the stage is called now)."""
    return conn.execute("SELECT 1 FROM order_task WHERE order_id = ? AND rule_id = ? AND NOT (status = 'skipped' AND result = ?) LIMIT 1",
                        (card["id"], SHIPPED_RULE, SHIPPED_CLIENT["undone"])).fetchone() is not None


def ship(conn, card: dict, who: str, now: str, deliver, tell: Optional[Tell] = None) -> Tuple[str, str]:
    """«Передал курьеру ТК» of the point (the button, the VK words): (outcome for the log, answer for the point). The order goes to
    «Отправлен», then the client's message, the owner and the partner, the task of the track number, and on to «Выполнено» (after_shipped)."""
    number = _label(card)
    if not on(card):
        return ("не подходит: заказ не в другой город", "Заказ №%s — не в другой город: «Передал курьеру ТК» ему не подходит; сообщение передано "
                "владельцу." % number)
    code = card["stage"]["code"]
    if code == orders.SHIPPED or (code == "done" and was_shipped(conn, card)):
        return "уже отмечено", "Заказ №%s уже отмечен: передан курьеру ТК." % number
    if code not in SHIP_FROM:
        return ("не подходит: заказ на этапе «%s»" % card["stage"]["name"],
                "Заказ №%s на этапе «%s»: «Передал курьеру ТК» ему не подходит." % (number, card["stage"]["name"]))
    stage = _stage(conn, orders.SHIPPED)
    try:
        moved = orders.move_card(conn, card["id"], stage["id"], now, who)
    except OrderError as err:
        return "не выполнено: %s" % err, "Не получилось: %s" % err
    if not moved:
        return "не перенесён: заказ уже на другом этапе", "Заказ №%s уже переместили, проверьте его в панели." % number
    point = (card.get("point") or {}).get("name") or ""
    sent, note, final = after_shipped(conn, card["id"], "Точка «%s» передала" % point, who, now, deliver, tell)
    return ("заказ перенесён на «%s»; клиенту %s" % (final, "отправлено" if sent else "не отправлено"),
            "Заказ №%s — отмечено: передан курьеру ТК (теперь на этапе «%s»). %s" % (number, final, note))


def shipped_by_hand(conn, before: dict, who: str, now: str, deliver, tell: Optional[Tell] = None) -> str:
    """The office moved an order to another city to «Отправлен» itself (the board, the card; the owner 2026-11-02: as the point's press):
    the same message, alert, task and «Выполнено». Returns the line for the person ('' when the move was not that)."""
    try:
        card = orders.get_card(conn, before["id"])
    except OrderError:
        return ""
    if card["stage"]["code"] != orders.SHIPPED or before["stage"]["code"] == orders.SHIPPED or not on(card):
        return ""
    if was_shipped(conn, card):                                           # sent once already: back to «Выполнено», nothing repeated
        _to_done(conn, card["id"], now, who)
        return "Заказ уже был отправлен: сообщения и задачи не повторяются. Заказ на этапе «%s»." % orders.get_card(conn, card["id"])["stage"]["name"]
    sent, note, final = after_shipped(conn, card["id"], "Офис (%s) отметил: передан" % who, who, now, deliver, tell)
    return "%s Заказ на этапе «%s»." % (note, final)


def after_shipped(conn, card_id, whose: str, who: str, now: str, deliver, tell: Optional[Tell]) -> Tuple[bool, str, str]:
    """What follows the entry into «Отправлен»: the journal, «Клиенту: заказ отправлен», the owner and the partner in Telegram, «Внести
    трек-номер» when there is no track number, then «Выполнено» with its rules (the owner 2026-11-02: «при отправлен переходит в выполнено»).
    Each step on its own: none undoes the move. Returns (sent to the client, note, the stage the order stands on)."""
    card = orders.get_card(conn, card_id)
    number, data = _label(card), of(conn, card_id)
    text = "%s заказ №%s курьеру ТК%s%s%s." % (
        whose, number, " %s" % data["carrier"] if data["carrier"] else "", " в %s" % data["city"] if data["city"] else "",
        ", трек-номер %s" % data["track"] if data["track"] else ": трек-номер не внесён — внесите его в заказ")
    orders.add_event(conn, card_id, now, who, "point", text, level="green" if data["track"] else "yellow")
    conn.commit()
    sent, note = tell_shipped(conn, card_id, now, who, deliver)
    if tell is not None:
        try:
            from . import order_oth
            tell(order_oth.office_logins(conn), text)
        except Exception:                                                 # noqa: BLE001 - the move is done; the journal has the record
            conn.rollback()
            logging.getLogger("tires_sync.orders").exception("the alert of the courier of order %s failed", card_id)
    if not data["track"]:
        try:
            track_tasks(conn, card_id, number, now, who)
        except Exception:                                                 # noqa: BLE001 - the journal's yellow record says it
            conn.rollback()
            logging.getLogger("tires_sync.orders").exception("the track tasks of order %s failed", card_id)
    _to_done(conn, card_id, now, who)
    return sent, note, orders.get_card(conn, card_id)["stage"]["name"]


def _to_done(conn, card_id, now: str, who: str) -> None:
    """On to «Выполнено» with its rules; a refusal is a red record of the journal and the order stays on «Отправлен». Never raises."""
    done = _stage(conn, "done")
    try:
        if done is not None:
            orders.move_card(conn, card_id, done["id"], now, who)
    except OrderError:
        pass
    except Exception:                                                     # noqa: BLE001 - a busy base: the message and the alert went already
        conn.rollback()
        logging.getLogger("tires_sync.orders").exception("the move of shipped order %s to done failed", card_id)


def tell_shipped(conn, card_id, now: str, who: str, deliver) -> Tuple[bool, str]:
    """«Клиенту: заказ отправлен», once per stay on «Отправлен» (a task of the order under SHIPPED_RULE, sent as order_placed sends);
    what does not go waits in «Сообщения по заказу» with the reason. Never raises: (sent, note for the point)."""
    from . import order_placed
    try:
        card = orders.get_card(conn, card_id)
        line = order_placed._tell(conn, card_id, dict(SHIPPED_CLIENT, key="shipped-%s" % re.sub(r"\D", "", card["entered_at"] or "")),
                                  "", now, who, deliver)
    except Exception:                                                     # noqa: BLE001 - _tell never raises; a guard all the same
        conn.rollback()
        logging.getLogger("tires_sync.orders").exception("the message of the shipped order %s", card_id)
        return False, "Клиенту сообщение «заказ отправлен» не отправлено: менеджер отправит его сам."
    sent = line.startswith("Клиенту отправлено")
    return sent, ("Клиенту отправлено сообщение «заказ отправлен»." if sent else
                  "Клиенту сообщение «заказ отправлен» не отправлено: менеджер отправит его сам.")


def track_tasks(conn, card_id, number: str, now: str, who: str) -> List[int]:
    """«Внести трек-номер» for the owner and every partner (a task of «Задачи», due in an hour)."""
    from . import order_notify, order_oth, tasks
    owner = order_notify.get_owner_login(conn)
    due = (datetime.fromisoformat(now[:19]) + timedelta(hours=1)).isoformat()[:16]
    return [tasks.add(conn, kind="other", title="%s заказа №%s" % (TRACK_TASK, number), due_at=due, note="Заказ передан курьеру ТК",
                      now=now, author=who, assignee=login, owner_login=owner, order_id=card_id) for login in order_oth.office_logins(conn)]


def skip_waiting_message(conn, card_id, now: str, who: str) -> None:
    """«Заказ отправлен» still waiting (not sent: no chat, lacking values) once the courier's mark is undone: never sent by hand later."""
    from . import order_engine as engine
    for task in engine.list_tasks(conn, card_id, ("pending", "error")):
        if task["rule_id"] == SHIPPED_RULE:
            try:
                engine.skip_task(conn, task["id"], now, who)
            except OrderError:                                            # a person took it meanwhile
                conn.rollback()
                continue
            conn.execute("UPDATE order_task SET result = ? WHERE id = ? AND status = 'skipped'", (SHIPPED_CLIENT["undone"], task["id"]))
            conn.commit()                                                 # a later «Передал» makes its own message (order_placed._earlier)


def close_track_tasks(conn, card_id, how: str, now: str, who: str, result: str = "") -> int:
    """The open «Внести трек-номер» of the order: done (the number came) or cancelled (the courier's mark undone)."""
    from . import tasks
    closed = 0
    for (task_id,) in conn.execute("SELECT id FROM callback_task WHERE order_id = ? AND title LIKE ? AND done_at IS NULL AND cancelled_at IS NULL",
                                   (card_id, TRACK_TASK + "%")).fetchall():
        try:
            tasks.close(conn, task_id, how, now, who, result)
            closed += 1
        except tasks.TaskError:                                           # closed meanwhile
            conn.rollback()
    return closed
