"""Orders: a configurable pipeline of stages and the cards that move through it. No outgoing messages here."""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from .amounts import parse_amount
from . import economics, order_alerts, sales as sales_store


class OrderError(ValueError):
    pass


class OpenOrderExists(OrderError):
    """A new order refused: the client's phone has an open order (the owner 2026-10-27: a second open order on one phone is forbidden).
    `order_id`: that order, for a link to it."""

    def __init__(self, order_id: int):
        super().__init__("У клиента уже есть открытый заказ №%d — откройте его." % order_id)
        self.order_id = order_id


KINDS = ("open", "won", "lost", "closed")
KIND_NAMES = {"open": "обычный", "won": "успешный", "lost": "отказ", "closed": "завершающий"}
DEFAULT_STAGES = (
    ("new", "Новый заказ", "#7F56D9", "open"), ("talk", "Переговоры", "#2E90FA", "open"), ("wait", "Ожидает наличие", "#0BA5EC", "open"),
    ("placed", "Заказ оформлен", "#F79009", "open"), ("moving", "Перемещение на точку", "#EF6820", "open"),
    ("at_point", "Товар на точке", "#17B26A", "open"), ("oth", "На ОТХ", "#667085", "open"), ("return_given", "Возврат передан", "#DC6803", "open"),
    ("done", "Выполнено", "#079455", "won"), ("cancel", "Отмена / невыкуп", "#D92D20", "lost"), ("return", "Возврат", "#B54708", "closed"))
# «Отмена» and «Отказ/невыкуп» are one stage with the reason (the owner 2026-10-29); the reason is a text of cancel_reason
CANCEL_KINDS = ("отменил клиент", "не выкупил", "другое")
NOT_BOUGHT = "не выкупил"
OLD_CANCEL_NAME = "Отмена"                      # the name of «cancel» before the merge: renamed only while the owner kept it
COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
STAGE_FIELDS = "id, code, name, color, position, kind, hint, limit_hours"


DEFAULT_RESPONSIBLE_KEY = "orders_default_responsible"


def default_responsible(conn) -> str:
    """Who answers for the orders the robot makes (from the chats) until a manager is named: set by the owner, empty = whoever made the card."""
    row = conn.execute("SELECT value FROM app_state WHERE key = ?", (DEFAULT_RESPONSIBLE_KEY,)).fetchone()
    return row[0] if row else ""


def set_default_responsible(conn, login) -> None:
    login = _single(login, 60, "Ответственный по умолчанию")
    conn.execute("INSERT OR REPLACE INTO app_state VALUES (?, ?)", (DEFAULT_RESPONSIBLE_KEY, login))
    conn.commit()


def _stage_dict(row) -> dict:
    return dict(zip(("id", "code", "name", "color", "position", "kind", "hint", "limit_hours"), row))


def ensure_pipeline(conn, now: str) -> None:
    """The owner's default stages, created once (when there is none)."""
    if not conn.execute("SELECT COUNT(*) FROM order_stage").fetchone()[0]:
        for position, (code, name, color, kind) in enumerate(DEFAULT_STAGES):
            conn.execute("INSERT INTO order_stage (code, name, color, position, kind) VALUES (?,?,?,?,?)", (code, name, color, position, kind))
        conn.commit()
    _ensure_wait_stage(conn)
    _merge_refused_stage(conn, now)
    _ensure_return_given_stage(conn)


def _ensure_wait_stage(conn) -> None:
    """A pipeline made before «Ожидает наличие» existed gets it once, right after «Переговоры» (a system stage: it cannot be deleted, so it never comes back)."""
    if conn.execute("SELECT 1 FROM order_stage WHERE code = 'wait'").fetchone():
        return
    talk = conn.execute("SELECT position FROM order_stage WHERE code = 'talk'").fetchone()
    at = (talk[0] + 1) if talk else (conn.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM order_stage").fetchone()[0])
    code, name, color, kind = next(d for d in DEFAULT_STAGES if d[0] == "wait")
    cur = conn.execute("INSERT INTO order_stage (code, name, color, position, kind) SELECT ?,?,?,?,? WHERE NOT EXISTS (SELECT 1 FROM order_stage WHERE code = 'wait')",
                       (code, name, color, at, kind))                  # one statement: a second request at the same moment adds nothing and shifts nothing
    if cur.rowcount == 1:
        conn.execute("UPDATE order_stage SET position = position + 1 WHERE position >= ? AND code != 'wait'", (at,))
    conn.commit()


def _merge_refused_stage(conn, now: str) -> None:
    """«Отказ/невыкуп» (code refused) joins «Отмена» (the owner 2026-10-29: one stage «Отмена / невыкуп» with the reason). Once, in one IMMEDIATE
    transaction (a second request finds nothing to do): its orders go to «cancel» with the reason «не выкупил» (a reason of their own stays after
    it), their time on the stage kept, a record in each journal; its tasks, the commands and the moves of rules pointing to it point to «cancel»;
    its own rules come over switched off (a message of «невыкуп» must not go on an ordinary cancel; the owner switches them on); the stage goes.
    «cancel» is renamed «Отмена / невыкуп» only while it is still called «Отмена». A pipeline without «cancel»: «refused» becomes it."""
    if not conn.execute("SELECT 1 FROM order_stage WHERE code = 'refused'").fetchone():
        return
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        refused = conn.execute("SELECT id FROM order_stage WHERE code = 'refused'").fetchone()
        if refused is None:                                     # another request merged it meanwhile
            conn.rollback()
            return
        old = refused[0]
        cancel = conn.execute("SELECT id, name FROM order_stage WHERE code = 'cancel'").fetchone()
        name = next(d[1] for d in DEFAULT_STAGES if d[0] == "cancel")
        if cancel is None:
            conn.execute("UPDATE order_stage SET code = 'cancel', name = ? WHERE id = ?", (name, old))
            conn.commit()
            return
        new = cancel[0]
        for card_id, reason in conn.execute("SELECT id, cancel_reason FROM order_card WHERE stage_id = ?", (old,)).fetchall():
            text = NOT_BOUGHT + (": " + reason if reason and reason.strip() != NOT_BOUGHT else "")
            conn.execute("UPDATE order_card SET stage_id = ?, cancel_reason = ? WHERE id = ?", (new, text[:300], card_id))
            add_event(conn, card_id, now, "система", "stage", "Этап «Отказ/невыкуп» объединён с отменой: заказ в «%s», причина «%s»."
                      % (name if cancel[1] == OLD_CANCEL_NAME else cancel[1], text[:300]), level="grey")
        conn.execute("UPDATE order_task SET stage_id = ? WHERE stage_id = ?", (new, old))
        # a move into «Отказ/невыкуп» went without a reason; into «cancel» it needs one: such a rule comes switched off (the owner looks at it)
        conn.execute("UPDATE order_rule SET arg = ?, enabled = 0 WHERE action = 'move' AND arg = ?", (str(new), str(old)))
        conn.execute("UPDATE order_rule SET stage_id = ?, enabled = 0 WHERE stage_id = ?", (new, old))
        for (command,) in conn.execute("SELECT id FROM order_command WHERE to_stage_id = ? AND stage_id != ?", (old, new)).fetchall():
            conn.execute("INSERT OR REPLACE INTO app_state VALUES (?, '1')", (MERGED_COMMAND_KEY % command,))   # order_manager: «не выкупил»
        conn.execute("UPDATE order_command SET to_stage_id = ? WHERE to_stage_id = ?", (new, old))
        conn.execute("DELETE FROM order_command WHERE stage_id = ? AND word IN (SELECT word FROM order_command WHERE stage_id = ?)", (old, new))
        conn.execute("UPDATE order_command SET stage_id = ? WHERE stage_id = ?", (new, old))
        conn.execute("DELETE FROM order_command WHERE stage_id = ? AND to_stage_id = ?", (new, new))     # «cancel → refused»: a move to itself
        conn.execute("INSERT OR REPLACE INTO app_state VALUES (?, ?)", (MERGED_KEY % old, str(new)))      # trash: a deleted order of it comes back here
        conn.execute("DELETE FROM order_stage WHERE id = ?", (old,))
        if cancel[1] == OLD_CANCEL_NAME:
            conn.execute("UPDATE order_stage SET name = ? WHERE id = ?", (name, new))
        for position, sid in enumerate(r[0] for r in conn.execute("SELECT id FROM order_stage ORDER BY position, id").fetchall()):
            conn.execute("UPDATE order_stage SET position = ? WHERE id = ?", (position, sid))
    except Exception:
        conn.rollback()
        raise
    conn.commit()


MERGED_KEY = "stage_merged_%d"                  # app_state: the stage an old stage was merged into (trash.restore)
MERGED_COMMAND_KEY = "command_was_refused_%d"     # app_state: a word of the points that led to «Отказ/невыкуп» (its move gives «не выкупил»)


def merged_command(conn, command_id) -> bool:
    return conn.execute("SELECT 1 FROM app_state WHERE key = ?", (MERGED_COMMAND_KEY % int(command_id),)).fetchone() is not None


def merged_into(conn, stage_id) -> Optional[int]:
    """The stage `stage_id` (gone) was merged into, or None."""
    row = conn.execute("SELECT value FROM app_state WHERE key = ?", (MERGED_KEY % int(stage_id),)).fetchone()
    return int(row[0]) if row and str(row[0]).isdigit() else None


def cancel_reason_of(kind, text) -> str:
    """The reason of «Отмена / невыкуп» from its choice and its text: «отменил клиент», «не выкупил» (the text after them, if any),
    «другое: …» (the text is needed). OrderError for a choice not of CANCEL_KINDS or «другое» without a text."""
    kind, text = str(kind or "").strip(), " ".join(str(text or "").split())
    if kind not in CANCEL_KINDS:
        raise OrderError("Причина отмены: отменил клиент, не выкупил или другое.")
    if kind == "другое" and not text:
        raise OrderError("Причина отмены «другое»: напишите, что случилось.")
    return (kind + (": " + text if text else ""))[:300]

RETURN_GIVEN = "return_given"


def _ensure_return_given_stage(conn) -> None:
    """A pipeline made before «Возврат передан» existed (the owner 2026-10-29: the point signed the act МХ-3 and gave the tyres of «На ОТХ»
    back to the supplier) gets it once, right after «На ОТХ» (one statement, like «Ожидает наличие»). An open stage: the money of the return
    is not back yet; the manager closes the order in «Возврат» with the amount."""
    if conn.execute("SELECT 1 FROM order_stage WHERE code = ?", (RETURN_GIVEN,)).fetchone():
        return
    oth = conn.execute("SELECT position FROM order_stage WHERE code = 'oth'").fetchone()
    at = (oth[0] + 1) if oth else (conn.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM order_stage").fetchone()[0])
    code, name, color, kind = next(d for d in DEFAULT_STAGES if d[0] == RETURN_GIVEN)
    cur = conn.execute("INSERT INTO order_stage (code, name, color, position, kind) SELECT ?,?,?,?,? WHERE NOT EXISTS (SELECT 1 FROM order_stage WHERE code = ?)",
                       (code, name, color, at, kind, code))
    if cur.rowcount == 1:                                       # the owner's own stages have no code: «code != …» alone would skip them
        conn.execute("UPDATE order_stage SET position = position + 1 WHERE position >= ? AND (code IS NULL OR code != ?)", (at, code))
    conn.commit()


FROM_POINT = ("from_point", "Передано с точки", "#9E77ED", "open")    # a client handed over by a point (order_manager.handoff)


def ensure_from_point_stage(conn) -> dict:
    """The system stage of the clients the points hand over: first, right before «Новый заказ» (the owner 2026-10-23; the quick add of the
    board stays with «Новый заказ», where _first_stage makes the orders), made once when the first client comes (one statement, like
    «Ожидает наличие»: two requests at once add one). The other stages and the cards are not touched except for their order."""
    code, name, color, kind = FROM_POINT
    new = conn.execute("SELECT position FROM order_stage WHERE code = 'new'").fetchone()
    at = new[0] if new else 0
    cur = conn.execute("INSERT INTO order_stage (code, name, color, position, kind) SELECT ?,?,?,?,? WHERE NOT EXISTS "
                       "(SELECT 1 FROM order_stage WHERE code = ?)", (code, name, color, at, kind, code))
    if cur.rowcount == 1:                                       # the owner's own stages have no code: «code != …» alone would skip them
        conn.execute("UPDATE order_stage SET position = position + 1 WHERE position >= ? AND (code IS NULL OR code != ?)", (at, code))
    conn.commit()
    return _stage_dict(conn.execute("SELECT %s FROM order_stage WHERE code = ?" % STAGE_FIELDS, (code,)).fetchone())


def list_stages(conn) -> List[dict]:
    return [_stage_dict(r) for r in conn.execute("SELECT %s FROM order_stage ORDER BY position, id" % STAGE_FIELDS)]


def get_stage(conn, stage_id) -> dict:
    row = conn.execute("SELECT %s FROM order_stage WHERE id = ?" % STAGE_FIELDS, (stage_id,)).fetchone()
    if row is None:
        raise OrderError("Такого этапа нет.")
    return _stage_dict(row)


def _check_stage(name, color, kind, hint, limit_hours):
    name = " ".join(str(name or "").split())
    if not name or len(name) > 60:
        raise OrderError("Название этапа: от 1 до 60 знаков.")
    if not COLOR.match(str(color or "")):
        raise OrderError("Цвет этапа: шестнадцатеричный код вида #7F56D9.")
    if kind not in KINDS:
        raise OrderError("Тип этапа: обычный, успешный, отказ или завершающий.")
    hint = str(hint or "").strip()
    if len(hint) > 300:
        raise OrderError("Подсказка этапа не длиннее 300 знаков.")
    try:
        limit = int(limit_hours or 0)
    except (TypeError, ValueError):
        raise OrderError("Срок на этапе: целое число часов.")
    if not 0 <= limit <= 24 * 365:
        raise OrderError("Срок на этапе: от 0 до 8760 часов (0 — без срока).")
    return name, str(color).upper(), kind, hint, limit


def add_stage(conn, name, color, kind, hint, limit_hours) -> int:
    name, color, kind, hint, limit = _check_stage(name, color, kind, hint, limit_hours)
    last = conn.execute("SELECT COALESCE(MAX(position), -1) FROM order_stage").fetchone()[0]
    cur = conn.execute("INSERT INTO order_stage (name, color, position, kind, hint, limit_hours) VALUES (?,?,?,?,?,?)",
                       (name, color, last + 1, kind, hint, limit))
    conn.commit()
    return cur.lastrowid


def update_stage(conn, stage_id, name, color, kind, hint, limit_hours) -> None:
    old = get_stage(conn, stage_id)
    name, color, kind, hint, limit = _check_stage(name, color, kind, hint, limit_hours)
    if old["code"] and kind != old["kind"]:
        raise OrderError("Тип системного этапа менять нельзя: от него зависят правила (продажа, отмена, возврат).")
    conn.execute("UPDATE order_stage SET name = ?, color = ?, kind = ?, hint = ?, limit_hours = ? WHERE id = ?",
                 (name, color, kind, hint, limit, stage_id))
    conn.commit()


def shift_stage(conn, stage_id, direction: int) -> None:
    """Move one stage up (-1) or down (+1); at the edge nothing happens. Positions stay 0..n-1."""
    ids = [s["id"] for s in list_stages(conn)]
    if stage_id not in ids:
        raise OrderError("Такого этапа нет.")
    index = ids.index(stage_id)
    other = index + (1 if direction > 0 else -1)
    if not 0 <= other < len(ids):
        return
    ids[index], ids[other] = ids[other], ids[index]
    for position, sid in enumerate(ids):
        conn.execute("UPDATE order_stage SET position = ? WHERE id = ?", (position, sid))
    conn.commit()


REMOVABLE_SYSTEM = ("talk",)                    # «Переговоры» (the owner, 2026-10-19: not needed): the only system stage that may go, and only empty


def delete_stage(conn, stage_id) -> None:
    stage = get_stage(conn, stage_id)
    if stage["code"] and stage["code"] not in REMOVABLE_SYSTEM:
        raise OrderError("Системный этап удалить нельзя: его можно переименовать и перекрасить.")
    if conn.execute("SELECT COUNT(*) FROM order_card WHERE stage_id = ?", (stage_id,)).fetchone()[0]:
        raise OrderError("В этапе есть заказы: сначала перенесите их в другой этап.")
    if (conn.execute("SELECT COUNT(*) FROM order_rule WHERE action = 'move' AND arg = ? AND stage_id != ?", (str(stage_id), stage_id)).fetchone()[0]
            or conn.execute("SELECT COUNT(*) FROM order_command WHERE to_stage_id = ? AND stage_id != ?", (stage_id, stage_id)).fetchone()[0]):
        raise OrderError("На этот этап ссылаются правила или команды других этапов: сначала уберите их.")
    conn.execute("DELETE FROM order_rule WHERE stage_id = ?", (stage_id,))
    conn.execute("DELETE FROM order_command WHERE stage_id = ?", (stage_id,))
    conn.execute("DELETE FROM order_stage WHERE id = ?", (stage_id,))
    for position, sid in enumerate(s["id"] for s in list_stages(conn)):
        conn.execute("UPDATE order_stage SET position = ? WHERE id = ?", (position, sid))
    conn.commit()


from . import order_points as points_store   # noqa: E402  (kept next to its first use)

CHAT_REF = re.compile(r"^Ч-(\d{1,9})$")
AVITO_PAGE = re.compile(r"^https://(www\.|m\.)?avito\.ru/[^\s<>\"']*$")


def chat_link(value):
    """(address, is_external) for the card's chat field when it holds our chat number or an Avito page; otherwise None."""
    text = str(value or "").strip()
    found = CHAT_REF.match(text)
    if found:
        return "/chats/%s" % found.group(1), False
    return (text, True) if AVITO_PAGE.match(text) else None


RETURN_KINDS = {"client": "клиент вернул нам", "supplier": "возврат поставщику"}
SOURCES = {"avito": "Авито", "repeat": "Повторное обращение", "pvz": "Передал ПВЗ"}
DEAL_KINDS = {"webmim": "Через WebMIM (перемещение)", "stock": "Из наличия точки"}
POINT_GOODS = "point"            # the code of a tyre of the point's stock the unload has not (a deal «stock» only); its purchase 0 = not given yet
MARKS = {"settled_point_at": "Расчёт с точкой", "codes_written_off_at": "Списание кодов маркировки", "settled_supplier_at": "Расчёты с поставщиком"}
EDITABLE = ("client_name", "client_phone", "avito_chat", "point_id", "delivery_date", "delivery_time", "tags", "comment",
            "webmim_id", "webmim_number", "webmim_status", "cancel_reason", "return_kind", "return_reason", "return_amount",
            "source", "deal_kind", "transfer_number", "settled_point_at", "codes_written_off_at", "settled_supplier_at", "responsible", "reserve_until",
            "special_terms", "special_amount", "receipt_needed")
LABELS = {"client_name": "Клиент", "client_phone": "Телефон", "avito_chat": "Чат Авито", "point_id": "Точка", "delivery_date": "Дата доставки",
          "delivery_time": "Время доставки", "tags": "Теги", "comment": "Комментарий", "webmim_id": "ID заказа WebMIM",
          "webmim_number": "Номер заказа WebMIM", "webmim_status": "Статус WebMIM", "cancel_reason": "Причина отмены",
          "return_kind": "Тип возврата", "return_reason": "Причина возврата", "return_amount": "Сумма возврата",
          "source": "Откуда", "deal_kind": "Тип сделки", "transfer_number": "Номер перемещения", "responsible": "Ответственный", "reserve_until": "Крайняя дата резерва", "special_terms": "Особые условия", "special_amount": "Сумма по особым условиям",
          "receipt_needed": "Нужен товарный чек", **MARKS}
CARD_FIELDS = ("id, pipeline_id, stage_id, client_name, client_phone, avito_chat, point_id, delivery_date, delivery_time, tags, comment, "
               "webmim_id, webmim_number, webmim_status, cancel_reason, return_kind, return_reason, return_amount, sale_ids, "
               "source, deal_kind, transfer_number, settled_point_at, codes_written_off_at, settled_supplier_at, responsible, reserve_until, special_terms, special_amount, receipt_needed, "
               "created_at, created_by, entered_at, updated_at")
MAX_TAGS, MAX_TAG, MAX_COMMENT, MAX_NOTE = 10, 30, 2000, 2000


def tag_list(text) -> List[str]:
    seen: List[str] = []
    for part in str(text or "").split(","):
        tag = " ".join(part.split())
        if tag and tag not in seen:
            seen.append(tag)
    return seen


def _single(value, limit: int, label: str) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        raise OrderError("%s: не длиннее %d знаков." % (label, limit))
    return text


def _clean(key: str, value, conn):
    label = LABELS[key]
    if key == "client_name":
        return _single(value, 120, label)
    if key == "client_phone":
        text = _single(value, 30, label)
        if text and not 10 <= len(re.sub(r"\D", "", text)) <= 15:
            raise OrderError("Телефон клиента: от 10 до 15 цифр.")
        return text                                                     # as typed: the texts to clients quote it; the client keeps it in E.164
    if key == "avito_chat":
        return _single(value, 200, label)
    if key == "point_id":
        text = str(value or "").strip()
        if not text:
            return None
        if not (text.isascii() and text.isdigit()):
            raise OrderError("Точка выбрана неверно.")
        try:
            points_store.get_point(conn, int(text))
        except points_store.PointError:
            raise OrderError("Такой точки нет.")
        return int(text)
    if key in ("delivery_date", "reserve_until"):
        text = str(value or "").strip()
        if not text:
            return ""
        try:
            parsed = date.fromisoformat(text)
        except ValueError:
            raise OrderError("%s: формат ГГГГ-ММ-ДД." % label)
        if not date(2020, 1, 1) <= parsed <= date(2100, 1, 1) or len(text) != 10:
            raise OrderError("%s вне разумных пределов." % label)
        return parsed.isoformat()
    if key == "delivery_time":
        return _single(value, 60, label)
    if key == "tags":
        tags = tag_list(value)
        if len(tags) > MAX_TAGS or any(len(t) > MAX_TAG for t in tags):
            raise OrderError("Теги: не больше %d, каждый не длиннее %d знаков." % (MAX_TAGS, MAX_TAG))
        return ",".join(tags)
    if key == "comment":
        text = str(value or "").strip()
        if len(text) > MAX_COMMENT:
            raise OrderError("Комментарий не длиннее %d знаков." % MAX_COMMENT)
        return text
    if key == "source":
        text = str(value or "")
        if text and text not in SOURCES:
            raise OrderError("Откуда: Авито, повторное обращение или передал ПВЗ.")
        return text
    if key == "deal_kind":
        text = str(value or "webmim")
        if text not in DEAL_KINDS:
            raise OrderError("Тип сделки: через WebMIM или из наличия точки.")
        return text
    if key == "transfer_number":
        return _single(value, 40, label)
    if key == "special_terms":
        return _single(value, 300, label)
    if key == "special_amount":
        if not str(value or "").strip():
            return 0.0
        try:
            amount = parse_amount(value)                                       # the project's way: «1.500» is 1500, «5 000 ₽» is 5000
        except ValueError:
            raise OrderError("%s: число." % label)
        if not 0 <= amount <= 10_000_000 or amount != amount:
            raise OrderError("%s: от 0 до 10 000 000 ₽." % label)
        return round(amount, 2)
    if key == "receipt_needed":
        return 1 if str(value or "").strip().lower() in ("1", "on", "true", "да") else 0
    if key == "responsible":
        return _single(value, 60, label)
    if key in MARKS:
        text = str(value or "").strip()
        if not text:
            return ""
        try:
            parsed = date.fromisoformat(text)
        except ValueError:
            raise OrderError("%s: дата в формате ГГГГ-ММ-ДД." % label)
        if not date(2020, 1, 1) <= parsed <= date(2100, 1, 1) or len(text) != 10:
            raise OrderError("%s: дата вне разумных пределов." % label)
        return parsed.isoformat()
    if key in ("webmim_id", "webmim_number", "webmim_status"):
        return _single(value, 60, label)
    if key in ("cancel_reason", "return_reason"):
        return _single(value, 300, label)
    if key == "return_kind":
        text = str(value or "")
        if text and text not in RETURN_KINDS:
            raise OrderError("Тип возврата: клиент вернул нам или возврат поставщику.")
        return text
    if key == "return_amount":
        raw = re.sub(r"[\s ₽]", "", str(value or "0")).replace(",", ".")
        try:
            amount = float(raw)
        except ValueError:
            raise OrderError("Сумма возврата: число.")
        if not 0 <= amount <= 10_000_000 or amount != amount:
            raise OrderError("Сумма возврата: от 0 до 10 000 000 ₽.")
        return round(amount, 2)
    raise OrderError("Неизвестное поле.")


def add_event(conn, card_id, now: str, author: str, kind: str, text: str, level: str = "") -> None:
    """A record of the order journal; its colour (order_alerts) is given or follows from the kind and the text."""
    level = order_alerts.check_level(level) if level else order_alerts.level_for(kind, text)
    conn.execute("INSERT INTO order_event (order_id, at, author, kind, text, level) VALUES (?,?,?,?,?,?)", (card_id, now, author, kind, text, level))


def list_events(conn, card_id) -> List[dict]:
    events = [{"id": r[0], "at": r[1], "author": r[2], "kind": r[3], "text": r[4], "level": r[5]} for r in conn.execute(
        "SELECT id, at, author, kind, text, level FROM order_event WHERE order_id = ? ORDER BY id DESC", (card_id,))]
    for event in events:
        event["level"] = order_alerts.level_of(event)
    return events


def _first_stage(conn) -> dict:
    stages = list_stages(conn)
    for stage in stages:
        if stage["code"] == "new":
            return stage
    if not stages:
        raise OrderError("Воронка не настроена.")
    return stages[0]


def _default_time(conn, clean: dict, current_time: str, current_point=None) -> None:
    """A point chosen for a deal brings the point's usual delivery time, unless a time is there or was typed."""
    point = clean.get("point_id")
    if not point or point == current_point or clean.get("delivery_time") or current_time:
        return
    try:
        usual = points_store.get_point(conn, point)["default_time"]
    except points_store.PointError:
        return
    if usual:
        clean["delivery_time"] = usual


def create_card(conn, fields: dict, now: str, author: str, require_contact: bool = True, commit: bool = True, stage: Optional[dict] = None,
                scope_point: Optional[int] = None) -> int:
    """A new card on the first stage (or on `stage`). `commit=False` writes it without saving and without the rules of the stage: the caller saves and fires them.
    Refused (OpenOrderExists) while the client's phone has an open order of the same point or of no point (open_order_conflict);
    `scope_point`: the point the order is made for when the caller sets it after (a call, a chat of a point's account)."""
    clean = {k: _clean(k, v, conn) for k, v in fields.items() if k in EDITABLE}
    if require_contact and not any(clean.get(k) for k in ("client_name", "client_phone", "avito_chat")):
        raise OrderError("Укажите хотя бы имя, телефон или чат клиента.")
    _default_time(conn, clean, "")
    stage = stage or _first_stage(conn)
    base = {"client_name": "", "client_phone": "", "avito_chat": "", "point_id": None, "delivery_date": "", "delivery_time": "", "tags": "",
            "comment": "", "webmim_id": "", "webmim_number": "", "webmim_status": "", "cancel_reason": "", "return_kind": "",
            "return_reason": "", "return_amount": 0.0, "responsible": ""}
    base.update(clean)
    keys = sorted(base)
    cur = conn.execute(
        "INSERT INTO order_card (stage_id, %s, created_at, created_by, entered_at, updated_at) VALUES (?, %s, ?, ?, ?, ?)" % (
            ", ".join(keys), ", ".join("?" for _ in keys)),
        [stage["id"]] + [base[k] for k in keys] + [now, author, now, now])
    busy = open_order_conflict(conn, base["client_phone"], base["point_id"] or scope_point, besides=cur.lastrowid)
    if busy is not None:                                      # asked after the insert: the write holds the lock, so two tabs at once make one order
        if commit:
            conn.rollback()                                   # the caller saves nothing of it; with commit=False the caller rolls back
        raise OpenOrderExists(busy["id"])
    add_event(conn, cur.lastrowid, now, author, "created", "Заказ создан на этапе «%s»" % stage["name"])
    _attach_client(conn, cur.lastrowid, now, author)
    if commit:
        conn.commit()
        _fire_rules(conn, cur.lastrowid, now, ())
    return cur.lastrowid


def create_card_with_items(conn, fields: dict, items: List[dict], now: str, author: str, require_contact: bool = True,
                           scope_point: Optional[int] = None) -> int:
    """A new card with its positions, all or nothing: a position that cannot be added leaves no card behind. `items` are the keyword
    arguments of add_item (with qty). The rules of the first stage run after the positions, so that their texts see them."""
    try:
        card_id = create_card(conn, fields, now, author, require_contact, commit=False, scope_point=scope_point)
        for item in items:
            add_item(conn, card_id, now=now, author=author, commit=False, **item)
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    _fire_rules(conn, card_id, now, ())
    return card_id


def _row(conn, card_id) -> dict:
    row = conn.execute("SELECT %s FROM order_card WHERE id = ?" % CARD_FIELDS, (card_id,)).fetchone()
    if row is None:
        raise OrderError("Такого заказа нет.")
    return dict(zip([c.strip() for c in CARD_FIELDS.split(",")], row))


def get_card(conn, card_id) -> dict:
    card = _row(conn, card_id)
    card["stage"] = get_stage(conn, card["stage_id"])
    row = conn.execute("SELECT c.id, c.name FROM client_order l JOIN client c ON c.id = l.client_id WHERE l.order_id = ?", (card_id,)).fetchone()
    card["client"] = {"id": row[0], "name": row[1]} if row else None
    try:
        card["point"] = points_store.get_point(conn, card["point_id"]) if card["point_id"] else None
    except points_store.PointError:
        card["point"] = None
    card["tags"] = tag_list(card["tags"])
    card["sale_ids"] = json.loads(card["sale_ids"] or "[]")
    card["items"] = list_items(conn, card_id)
    from . import order_wait
    card["wants"] = order_wait.list_wants(conn, card_id)
    card["responsible"] = card["responsible"] or card["created_by"]            # whoever made the card answers for it until another manager is named
    from . import oth_sale
    card["in_stock"] = int(card_id) in oth_sale.in_stock_ids(conn, [card_id])   # every tyre taken from «На ОТХ»: no date of delivery (2026-10-30)
    return card


def card_view(card: dict) -> dict:
    """The head of the order's card in the one look of the cards (_entity.html), a dict ready to be JSON."""
    stage = card["stage"]
    tone = {"won": "ok", "open": "neutral", "closed": "warn"}.get(stage["kind"], "err")
    name = card["client_name"] or "Клиент без имени"
    return {"type": "order", "kind": "Заказ", "ref": "№%d" % card["id"], "title": name, "hint": "Заказ №%d · %s" % (card["id"], name),
            "status": {"text": stage["name"], "tone": tone},
            "crumbs": [{"label": "Заказы", "href": "/orders"}, {"label": "№%d" % card["id"]}]}


def _placed_stage(conn) -> Optional[dict]:
    return next((st for st in list_stages(conn) if st["code"] == "placed"), None)


def gaps_to_place(conn, card: dict) -> List[str]:
    """What the card lacks for «Заказ оформлен» and every stage after it (the same check the move makes)."""
    placed = _placed_stage(conn)
    return missing_for(card, placed, placed) if placed else []


def _stage_gaps(conn, card_id) -> List[str]:
    """What the card's CURRENT stage needs and the card lacks."""
    card = get_card(conn, card_id)
    return missing_for(card, card["stage"], _placed_stage(conn))


def _guard_stage(conn, card_id, before: List[str]) -> None:
    """A change must not take away what the card's stage needs (later steps send it to the supplier and the point)."""
    new = [gap for gap in _stage_gaps(conn, card_id) if gap not in before]
    if new:
        stage = get_card(conn, card_id)["stage"]["name"]
        raise OrderError("Заказ на этапе «%s»: нельзя убрать — %s. Верните его на более ранний этап, чтобы править." % (stage, ", ".join(new)))


def update_card(conn, card_id, changes: dict, now: str, author: str, commit: bool = True, link_client: Optional[bool] = None) -> List[str]:
    """Save the fields. A new phone joins the order to its client (`link_client`), except on «На ОТХ»: the client refused, the order is
    not theirs (хвост З15: no empty client is made, no phone is added to the client put aside)."""
    card = _row(conn, card_id)
    clean = {k: _clean(k, v, conn) for k, v in changes.items() if k in EDITABLE}
    _default_time(conn, clean, card["delivery_time"], card["point_id"])
    changed = {k: v for k, v in clean.items() if card[k] != v and not (k in ("return_amount", "special_amount") and float(card[k]) == float(v))}
    if not changed:
        return []
    before = _stage_gaps(conn, card_id)
    conn.execute("UPDATE order_card SET %s, updated_at = ? WHERE id = ?" % ", ".join("%s = ?" % k for k in changed),
                 list(changed.values()) + [now, card_id])
    for key, value in changed.items():
        if key == "receipt_needed":
            shown = "да" if value else "нет"
        elif key == "special_amount":
            shown = ("%.2f" % value).rstrip("0").rstrip(".") if value else "(пусто)"
        else:
            shown = "(пусто)" if value in ("", None, 0, 0.0) else str(value)[:80]
        add_event(conn, card_id, now, author, "field", "%s: %s" % (LABELS[key], shown))
    try:
        _guard_stage(conn, card_id, before)
    except OrderError:
        conn.rollback()
        raise
    if link_client is None:
        link_client = (conn.execute("SELECT code FROM order_stage WHERE id = ?", (card["stage_id"],)).fetchone() or [""])[0] != "oth"
    if "client_phone" in changed and link_client:
        _attach_client(conn, card_id, now, author)
    if commit:
        conn.commit()
    return list(changed)


def _attach_client(conn, card_id, now: str, author: str) -> None:
    """The order joins the client of its phone (clients.attach_order); saved with the order's own change. Never breaks the order."""
    from . import clients
    conn.execute("SAVEPOINT client_link")                               # a half-made client is undone, the order is kept
    try:
        clients.attach_order(conn, card_id, now, author)
        conn.execute("RELEASE client_link")
    except sqlite3.Error:
        conn.execute("ROLLBACK TO client_link")
        conn.execute("RELEASE client_link")
        logging.getLogger("tires_sync.orders").exception("the client of order %s could not be found", card_id)


def add_note(conn, card_id, text, now: str, author: str) -> None:
    _row(conn, card_id)
    text = str(text or "").strip()
    if not text or len(text) > MAX_NOTE:
        raise OrderError("Заметка: от 1 до %d знаков." % MAX_NOTE)
    add_event(conn, card_id, now, author, "note", text)
    conn.commit()


ITEM_FIELDS = ("id, order_id, goods_id, code, name, brand_key, season, diameter, territory, qty, supplier_price, sale_unit, terms, fee_pct, promo_pct, "
               "purchase_manual")


def _number(value, label: str, low: float, high: float, low_open: bool = False) -> float:
    try:
        number = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else parse_amount(value)
    except (TypeError, ValueError):
        raise OrderError("%s: нужно число." % label)
    if number != number or number in (float("inf"), float("-inf")) or number > high or number < low or (low_open and number == low):
        raise OrderError("%s вне допустимых пределов." % label)
    return number


def _qty(value) -> int:
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        raise OrderError("Количество: целое число от 1 до 100.")
    try:
        qty = int(value) if isinstance(value, (int, float)) else int(str(value).strip())
    except (TypeError, ValueError):
        raise OrderError("Количество: целое число от 1 до 100.")
    if not 1 <= qty <= 100:
        raise OrderError("Количество: целое число от 1 до 100.")
    return qty


def item_money(item: dict) -> dict:
    return economics.unit(float(item["supplier_price"]), float(item["sale_unit"]), item["terms"] or None, float(item["fee_pct"]),
                          float(item["promo_pct"]), int(item["qty"]))


def list_items(conn, card_id) -> List[dict]:
    out = []
    for row in conn.execute("SELECT %s FROM order_item WHERE order_id = ? ORDER BY id" % ITEM_FIELDS, (card_id,)):
        item = dict(zip([c.strip() for c in ITEM_FIELDS.split(",")], row))
        item["terms"] = json.loads(item["terms"] or "{}")
        item["money"] = item_money(item)
        out.append(item)
    return out


def totals(items: List[dict]) -> dict:
    """The money of the order's positions. A position with the purchase 0 (a point tyre before its purchase is typed) makes the margin
    unknown (None, the names in `no_purchase`): never a made-up profit (record_sales refuses it the same way)."""
    unknown = [i["name"] for i in items if not float(i["supplier_price"]) > 0]
    return {"sale": round(sum(i["money"]["sale"] for i in items), 2), "cost": round(sum(i["money"]["cost"] for i in items), 2),
            "margin": None if unknown else round(sum(i["money"]["margin"] for i in items), 2), "no_purchase": unknown,
            "qty": sum(int(i["qty"]) for i in items)}


def _no_sale(conn, card_id) -> None:
    """The positions of an order with a recorded sale are frozen: the order and «Продажи» must show the same money."""
    if json.loads(_row(conn, card_id)["sale_ids"] or "[]"):
        raise OrderError("По заказу записана продажа: сначала сторнируйте её, потом правьте позиции.")


def add_item(conn, card_id, *, goods_id, name, brand_key, season, diameter, territory, qty, sale_unit, supplier_price, terms,
             fee_pct, promo_pct, now: str, author: str, code: str = "", commit: bool = True) -> int:
    _row(conn, card_id)
    _no_sale(conn, card_id)
    goods_id, name = str(goods_id or "").strip(), " ".join(str(name or "").split())
    if not goods_id or not name or len(name) > 200:
        raise OrderError("Позиция не указана.")
    point = goods_id == POINT_GOODS
    if point and _row(conn, card_id)["deal_kind"] != "stock":
        raise OrderError("Шину без выгрузки WebMIM можно добавить только в сделку «Из наличия точки»: выберите этот тип сделки и нажмите «Сохранить».")
    qty = _qty(qty)
    sale = _number(sale_unit, "Цена продажи", 0, 1_000_000, low_open=True)
    price = _number(supplier_price, "Цена поставщика", 0, 10_000_000, low_open=not point)       # a point tyre: 0 until the purchase is typed
    fee = _number(fee_pct or 0, "Комиссия", 0, 100)
    promo = _number(promo_pct or 0, "Реклама", 0, 100)
    cur = conn.execute(
        "INSERT INTO order_item (order_id, goods_id, code, name, brand_key, season, diameter, territory, qty, supplier_price, sale_unit, terms, fee_pct, promo_pct,"
        " purchase_manual) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (card_id, goods_id, str(code or "").strip()[:60], name, str(brand_key or ""), str(season or "all"), int(diameter or 0), str(territory or ""), qty, price, sale,
         json.dumps(terms or {}, ensure_ascii=False), fee, promo, 1 if point else 0))
    add_event(conn, card_id, now, author, "item", "Добавлено: %s × %d по %s ₽" % (name, qty, format(sale, ".0f")))
    if commit:
        conn.commit()
    return cur.lastrowid


def _item_row(conn, item_id) -> dict:
    row = conn.execute("SELECT order_id, name, qty, sale_unit, supplier_price FROM order_item WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise OrderError("Такой позиции нет.")
    return {"order_id": row[0], "name": row[1], "qty": row[2], "sale_unit": row[3], "supplier_price": row[4]}


def _purchase_audit(conn, now: str, author: str, card_id, item_id, before: float, after: float, source: str) -> None:
    """The purchase is money: the journal of the order names no amount (everybody reads it), the audit keeps «было → стало»."""
    conn.execute("INSERT INTO audit (at, author, action, detail) VALUES (?,?,?,?)",
                 (now, author, "order_item_purchase", json.dumps({"order": card_id, "item": item_id, "from": before, "to": after, "source": source})))


def update_item(conn, item_id, qty, sale_unit, now: str, author: str, supplier_price=None) -> None:
    """supplier_price: the purchase typed by hand (only with the money right; None keeps it). A changed one is marked «вручную»: WebMIM then keeps off it."""
    old = _item_row(conn, item_id)
    _no_sale(conn, old["order_id"])
    qty = _qty(qty)
    if qty != int(old["qty"]) and _from_oth(conn, item_id):
        raise OrderError("Это шины с ОТХ: их количество не меняется. Нажмите «Вернуть на ОТХ» и возьмите снова сколько нужно.")
    sale = _number(sale_unit, "Цена продажи", 0, 1_000_000, low_open=True)
    price = None if supplier_price is None else _number(supplier_price, "Закупка за 1 шт", 0, 10_000_000, low_open=True)
    conn.execute("UPDATE order_item SET qty = ?, sale_unit = ? WHERE id = ?", (qty, sale, item_id))
    add_event(conn, old["order_id"], now, author, "item", "Изменено: %s × %d по %s ₽" % (old["name"], qty, format(sale, ".0f")))
    if price is not None and round(price, 2) != round(float(old["supplier_price"]), 2):     # the field shows kopecks: an untouched one is no change
        conn.execute("UPDATE order_item SET supplier_price = ?, purchase_manual = 1 WHERE id = ?", (price, item_id))
        add_event(conn, old["order_id"], now, author, "item", "Закупка изменена вручную: %s" % old["name"])
        _purchase_audit(conn, now, author, old["order_id"], item_id, float(old["supplier_price"]), price, "manual")
    conn.commit()


def refresh_purchase(conn, card_id, prices: Dict[str, float], source: str, now: str, author: str) -> int:
    """The purchase of the positions from WebMIM (prices: goods_id → the price of one). Keeps the hand ones, the positions WebMIM does not name and
    an order with a recorded sale (its money is in «Продажи» already). Returns how many positions changed."""
    if json.loads(_row(conn, card_id)["sale_ids"] or "[]"):
        return 0
    changed = 0
    for item_id, goods_id, before in conn.execute("SELECT id, goods_id, supplier_price FROM order_item WHERE order_id = ? AND purchase_manual = 0 ORDER BY id",
                                                  (card_id,)).fetchall():
        price = prices.get(str(goods_id))
        if not isinstance(price, (int, float)) or isinstance(price, bool) or not 0 < price <= 10_000_000 or float(price) == float(before):
            continue
        conn.execute("UPDATE order_item SET supplier_price = ? WHERE id = ?", (float(price), item_id))
        _purchase_audit(conn, now, author, card_id, item_id, float(before), float(price), source)
        changed += 1
    if changed:
        add_event(conn, card_id, now, author, "item", "Закупка обновлена по заказу WebMIM (%s): позиций %d" % (PURCHASE_SOURCES.get(source, source), changed))
    conn.commit()
    return changed


PURCHASE_SOURCES = {"unload": "выгрузка поставщика", "webmim_api": "API WebMIM", "manual": "вручную"}


def _from_oth(conn, item_id) -> bool:
    """A position «с ОТХ» (oth_sale): its pieces belong to an old order, only «Вернуть на ОТХ» gives them back."""
    return conn.execute("SELECT 1 FROM order_item_oth WHERE item_id = ? AND undone_at = ''", (item_id,)).fetchone() is not None


def _gave_oth(conn, item_id) -> Optional[int]:
    """The order holding tyres this position gave from «На ОТХ» (oth_sale), if any: «Вернуть на ОТХ» puts them back here."""
    row = conn.execute("SELECT t.order_id FROM order_item_oth t JOIN order_item i ON i.id = t.item_id WHERE t.from_item = ? AND t.undone_at = '' "
                       "AND i.qty >= 1 LIMIT 1", (item_id,)).fetchone()
    return row[0] if row else None


def remove_item(conn, item_id, now: str, author: str) -> None:
    old = _item_row(conn, item_id)
    _no_sale(conn, old["order_id"])
    if _from_oth(conn, item_id):
        raise OrderError("Это шины с ОТХ: нажмите «Вернуть на ОТХ», и они вернутся в свой заказ.")
    holder = _gave_oth(conn, item_id)
    if holder:
        raise OrderError("Шины этой позиции взяты с ОТХ в заказ №%d: удалить её нельзя, им некуда будет вернуться." % holder)
    before = _stage_gaps(conn, old["order_id"])
    conn.execute("DELETE FROM order_item WHERE id = ?", (item_id,))
    add_event(conn, old["order_id"], now, author, "item", "Удалено: %s" % old["name"])
    try:
        _guard_stage(conn, old["order_id"], before)
    except OrderError:
        conn.rollback()
        raise
    conn.commit()


PLACED_CODES = ("placed", "moving", "at_point", "oth", "done")


def _needs_placed(stage: dict, placed: Optional[dict]) -> bool:
    """«Заказ оформлен» and every stage after it need the data of placing; so does a user stage that stands after it."""
    if stage["code"] in PLACED_CODES:
        return True
    if stage["code"] in ("new", "talk", "wait", "cancel", "refused", "return", RETURN_GIVEN, FROM_POINT[0]):
        return False
    return placed is not None and stage["position"] >= placed["position"]


def missing_for(card: dict, stage: dict, placed: Optional[dict] = None) -> List[str]:
    code, gaps = stage["code"], []
    if _needs_placed(stage, placed):
        if not any(int(i["qty"]) >= 1 for i in card["items"]):
            gaps.append("позиции")
        if not card["point_id"]:
            gaps.append("точка")
        if not card["delivery_date"] and not card.get("in_stock"):       # the tyres lie at the point already (oth_sale.IN_STOCK)
            gaps.append("дата доставки")
        if not card["client_phone"]:
            gaps.append("телефон клиента")
        if not card["client_name"]:
            gaps.append("имя клиента")
    if code == "wait":
        if not (card["client_name"] or card["client_phone"]):
            gaps.append("клиент (имя или телефон)")
        if not card.get("wants"):
            gaps.append("что нужно клиенту")
    if code == "cancel":
        if not card["cancel_reason"]:
            gaps.append("причина отмены")
    elif code == "return":
        if card["return_kind"] not in RETURN_KINDS:
            gaps.append("тип возврата")
        if not card["return_reason"]:
            gaps.append("причина возврата")
        if not float(card["return_amount"] or 0) > 0:
            gaps.append("сумма возврата")
    return gaps


def _fire_rules(conn, card_id, now: str, chain: tuple, of_point=None) -> None:
    """The rules of the stage a card has entered (`of_point`: only those of that point). A rule that breaks never takes the work with the card down."""
    try:
        from . import order_engine
        order_engine.fire_enter(conn, card_id, now, chain, of_point)
    except Exception:
        conn.rollback()
        logging.getLogger("tires_sync.orders").exception("rules of the stage failed for card %s", card_id)


RESERVE_DAYS = 3                                 # the point keeps the reservation three days from the day the goods arrive there
OTH_ENTERED = "На ОТХ: шины на точке, срока продажи нет"     # the owner 2026-10-30: whether they go back to the supplier is the owner's call
OTH_MARKS = ("На ОТХ: срок продажи до %", OTH_ENTERED)       # the record of entering «На ОТХ», before 2026-10-30 (with a date) and since


def _start_reserve(conn, card: dict, now: str, author: str) -> None:
    """The goods have arrived at the point: the reservation runs three days from today. A date the managers set in advance (the client asked
    for later) that is still ahead is kept; an old date left from an earlier stay is replaced."""
    today = now[:10]
    if card["reserve_until"] and card["reserve_until"] >= today:
        add_event(conn, card["id"], now, author, "field", "Резерв: товар на точке, срок до %s (задан заранее)" % card["reserve_until"])
        return
    until = (date.fromisoformat(today) + timedelta(days=RESERVE_DAYS)).isoformat()
    conn.execute("UPDATE order_card SET reserve_until = ? WHERE id = ?", (until, card["id"]))
    add_event(conn, card["id"], now, author, "field", "Резерв: товар на точке, срок до %s" % until)


def _start_oth(conn, card: dict, now: str, author: str) -> None:
    """The client refused and we bought the goods out: they lie at the point with no time to sell them (the owner 2026-10-30: whether they
    go back to the supplier is the owner's call). The reservation date left from «Товар на точке» says nothing about this stay: cleared.
    The client refused: the order is no longer theirs (order_oth.detach), the goods are our stock."""
    conn.execute("UPDATE order_card SET reserve_until = '' WHERE id = ?", (card["id"],))
    add_event(conn, card["id"], now, author, "field", OTH_ENTERED)
    from . import order_oth
    order_oth.detach(conn, card, now, author)


def _leave_oth(conn, card: dict, stage: dict, now: str, author: str) -> None:
    """Out of «На ОТХ» into an open stage: the client came back and is linked again; into a closed one the order stays unlinked."""
    from . import order_oth
    order_oth.leave(conn, card, stage, now, author)


def _refuse_move(conn, card_id, now: str, author: str, why: str, journal: bool) -> OrderError:
    """A refused move: what came with it is rolled back; the refusal is a red record of the journal unless the caller keeps quiet."""
    conn.rollback()
    if journal:
        try:
            add_event(conn, card_id, now, author, "refused", "Перевод не выполнен: %s" % why)     # not «stage»: that kind means the card did move
            conn.commit()
        except sqlite3.Error:                                                   # a busy base must not hide the refusal itself
            conn.rollback()
            logging.getLogger("tires_sync.orders").warning("the refused move of card %s could not be noted in its journal", card_id)
    return OrderError(why)


def move_card(conn, card_id, stage_id, now: str, author: str, changes: Optional[dict] = None, chain: tuple = (), fire_rules: bool = True,
              journal: bool = True) -> bool:
    """Move a card to a stage. The fields that come with the move are saved first; the move itself needs the stage's required data.
    `fire_rules=False` (the bulk move) leaves the rules of the new stage asleep: no task, no message for hundreds of cards at once.
    A refusal goes to the journal as a red record; not with `journal=False` (the bulk move) and not for a rule (`chain` is given only by the
    rules: their task writes its own red «…: ошибка (…)», one record is enough)."""
    stage = get_stage(conn, stage_id)
    changed = update_card(conn, card_id, changes, now, author, commit=False, link_client=False) if changes else []   # not saved until
    card = get_card(conn, card_id)                                                                               # the move is allowed
    phoned = "client_phone" in changed
    if card["stage_id"] == stage["id"]:
        if phoned and stage["code"] != "oth":
            _attach_client(conn, card_id, now, author)
        conn.commit()                                                           # no move, but the fields that came are kept
        return False
    journal = journal and not chain
    gaps = missing_for(card, stage, _placed_stage(conn))
    if gaps:
        raise _refuse_move(conn, card_id, now, author, "Чтобы перейти в «%s», заполните: %s." % (stage["name"], ", ".join(gaps)), journal)
    if card["sale_ids"] and stage["kind"] not in ("won", "closed"):
        raise _refuse_move(conn, card_id, now, author, "По заказу записана продажа: сначала сторнируйте её, потом переносите заказ.", journal)
    if stage["kind"] == "lost":
        from . import oth_sale
        held = oth_sale.holds(conn, card_id)                                    # the tyres taken from «На ОТХ» would be lost with the order
        if held:
            raise _refuse_move(conn, card_id, now, author, oth_sale.holds_text(held, oth_sale.why_not(conn, card) is None), journal)
    keep_sale = "" if stage["kind"] in ("won", "closed") else " AND sale_ids = '[]'"         # the same rule in SQL: a sale recorded a moment ago counts
    cur = conn.execute("UPDATE order_card SET stage_id = ?, entered_at = ?, updated_at = ? WHERE id = ? AND stage_id = ?" + keep_sale,
                       (stage["id"], now, now, card_id, card["stage_id"]))
    if cur.rowcount != 1:                         # somebody moved it first
        conn.rollback()
        return False
    add_event(conn, card_id, now, author, "stage", "Этап: «%s» → «%s»" % (card["stage"]["name"], stage["name"]))
    for (refused,) in conn.execute("SELECT e.id FROM order_event e WHERE e.order_id = ? AND e.kind = 'refused' AND e.level = 'red' AND NOT EXISTS ("
                                   "SELECT 1 FROM order_event_ack a WHERE a.event_id = e.id AND a.login = ?)", (card_id, order_alerts.EVERYBODY)).fetchall():
        order_alerts.resolve(conn, refused, now, author)                       # the card moved: its refused moves are no longer a cause to act
    if stage["code"] == "at_point":
        if card["stage"].get("code") == "oth":
            card = dict(card, reserve_until="")                                 # back from «На ОТХ»: a date of an old stay is not a reservation
        _start_reserve(conn, card, now, author)
    elif stage["code"] == "oth":
        _start_oth(conn, card, now, author)
    _leave_oth(conn, card, stage, now, author)
    from_oth = card["stage"].get("code") == "oth"
    after_oth = from_oth or (card["stage"].get("kind") != "open" and conn.execute(          # «На ОТХ», then a finished stage, then open again
        "SELECT 1 FROM order_event WHERE order_id = ? AND kind = 'field' AND (text LIKE ? OR text = ?) LIMIT 1", (card_id,) + OTH_MARKS).fetchone())
    if stage["code"] not in ("oth", RETURN_GIVEN):                              # the client of the phone, after the stage's own linking:
        unlinked = bool(after_oth) and conn.execute("SELECT 1 FROM client_order WHERE order_id = ?", (card_id,)).fetchone() is None
        if (phoned or unlinked) if stage["kind"] == "open" else (phoned and not from_oth):
            _attach_client(conn, card_id, now, author)                          # a new phone, or one typed on «На ОТХ» (review of З15);
                                                                                # an order left unlinked on purpose elsewhere stays so,
                                                                                # a finished stage after «На ОТХ» stays unlinked
    placed = _placed_stage(conn)
    if _needs_placed(stage, placed) and not _needs_placed(card["stage"], placed):
        from . import order_sheet
        order_sheet.enroll(conn, card_id, now)            # placed now (not a later move of an order placed before): its rows go to the Google table
    conn.commit()
    if fire_rules:
        _fire_rules(conn, card_id, now, chain)
    return True


def _seconds_on_stage(entered_at: str, now: datetime) -> float:
    try:
        return max((now - datetime.fromisoformat(entered_at)).total_seconds(), 0.0)
    except ValueError:
        return 0.0


BOARD_PHONE = re.compile(r"\+?[\d\s()\-.]+")          # a query of ten digits and more written so is a phone (search.py too)


def board_query(query: str) -> tuple:
    """The search of the board (and of the top bar, search.py): (the words in lower case, the digits, the number of an order — «123», «№123»,
    «#123», «заказ 123» — or None, the last ten digits when the query is a phone: +7, 8, brackets, spaces, or "")."""
    text = " ".join(str(query or "").split())
    needle, digits = text.lower(), re.sub(r"\D", "", text)
    found = re.fullmatch(r"(?:№|#|заказ\s*)?\s*(\d{1,9})", needle)
    phone = digits[-10:] if len(digits) >= 10 and BOARD_PHONE.fullmatch(text) else ""
    return needle, digits, int(found.group(1)) if found else None, phone


def board_matches(wanted: tuple, card_id: int, name: str, phone: str, first_item: str, webmim_number: str = "", transfer_number: str = "") -> bool:
    """The order fits the search of the board (board_query): a number finds that order, the order with that number of WebMIM (or a phone with
    those digits, or a transfer number ending in them, four and more), not every order with the digit; words find the client's name or the
    first position, six characters and more the transfer number as typed («26R-004417», the owner 2026-10-29: never every «26R» of the year); three digits and more find the phone; a whole phone
    (the last ten digits) finds it written in any way."""
    needle, digits, number, phone10 = wanted
    if not needle:
        return True
    phone_digits = re.sub(r"\D", "", phone)
    if phone10 and phone10 in phone_digits:
        return True
    transfer = transfer_number.lower().strip()
    if number is not None:                       # nine digits at most: a WebMIM number of ten and more is read as a phone (they are of seven)
        return (card_id == number or (len(digits) >= 4 and digits in phone_digits)
                or bool(webmim_number and re.sub(r"\D", "", webmim_number) == digits)
                or bool(transfer and len(digits) >= 4 and re.sub(r"\D", "", transfer).endswith(digits)))
    hay = " ".join([name.lower(), first_item.lower()])
    return needle in hay or (len(digits) >= 3 and digits in phone_digits) or (len(needle) >= 6 and needle in transfer)


ORIGIN_NO_ACCOUNT = "Без аккаунта"
ORIGIN_OTHER = "Не с аккаунта точки"         # the owner 2026-10-29: the point never sees an account's name (it was the name, 2026-10-22)
ORIGIN_HINT = "Заказ не с аккаунта Авито вашей точки: только принять и выдать"


ACCOUNT_SHOWN = "аккаунт Авито"


def account_tags(conn) -> set:
    """The tags chat_deals._account_tag makes of the Avito accounts' names (spaces folded, cut to MAX_TAG), in lower case."""
    return {" ".join(str(r[0]).split())[:MAX_TAG].lower() for r in conn.execute("SELECT name FROM account WHERE name != ''")}


def without_account_names(conn, text: str) -> str:
    """A text a manager of a point reads (the journal of the order, a row of the bell, the comment) with the names of the Avito accounts put
    out (the owner 2026-10-29: a point never sees an account's name): a whole word or phrase, in any case; never a piece of a word."""
    text = str(text or "")
    names = {" ".join(str(r[0]).split()) for r in conn.execute("SELECT name FROM account WHERE name != ''")}
    names |= {n[:MAX_TAG].strip() for n in names}                       # the tag of a long name is cut (chat_deals._account_tag)
    for name in sorted(names, key=len, reverse=True):
        if len(name) >= 2 and name.lower() in text.lower():
            text = re.sub(r"(?<!\w)%s(?!\w)" % r"\s+".join(re.escape(w) for w in name.split()), ACCOUNT_SHOWN, text, flags=re.I)
    return text


def origin_labels(conn, point_id, card_ids) -> Dict[int, dict]:
    """The orders of a point that did not come from the point's own Avito account (the owner 2026-10-22: the point only receives them and
    hands them over): {order: {"account": the name of the account of its earliest chat ("" without a chat), "text": the badge — the
    account's name (the owner: «название аккаунта авито») or «Без аккаунта», "hint": what it means}}. An order with a chat of the point's
    account (point.avito_account_id) is the point's own and has no label; a point without an account has none of its own."""
    ids = sorted({int(i) for i in card_ids})
    if not ids:
        return {}
    row = conn.execute("SELECT avito_account_id FROM point WHERE id = ?", (point_id,)).fetchone()
    own = row[0] if row else None
    chats_of: Dict[int, list] = {}
    for order_id, account_id, name in conn.execute(
            "SELECT c.order_id, c.account_id, COALESCE(a.name, '') FROM avito_chat c LEFT JOIN account a ON a.id = c.account_id "
            "WHERE c.order_id IN (%s) ORDER BY c.id" % ",".join("?" * len(ids)), ids):
        chats_of.setdefault(order_id, []).append((account_id, name))
    labels = {}
    for card_id in ids:
        found = chats_of.get(card_id, [])
        if own is not None and any(account == own for account, _ in found):
            continue
        labels[card_id] = {"account": "", "text": ORIGIN_OTHER if found else ORIGIN_NO_ACCOUNT, "hint": ORIGIN_HINT}   # never the account's name
    return labels


def client_words(event: dict) -> bool:
    """A record of the journal that quotes the client (chats: «Клиент написал…», «Клиент звонил…»)."""
    return event.get("kind") == "chat" and str(event.get("text") or "").startswith(("Клиент написал", "Клиент звонил"))


def board(conn, now: datetime, point_id: Optional[int] = None, tag: str = "", query: str = "", with_sums: bool = True) -> dict:
    """The board: the cards by stage with their sums of sale, the columns' sums and «Сумма в работе»; `with_sums=False` (the manager of a point,
    the owner 2026-10-26): every sum is None — not counted, not given."""
    stages = list_stages(conn)
    names = {p["id"]: p["name"] for p in points_store.list_points(conn)}
    sums: Dict[int, float] = {}
    first: Dict[int, str] = {}
    count: Dict[int, int] = {}
    for order_id, name, qty, sale_unit in conn.execute("SELECT order_id, name, qty, sale_unit FROM order_item ORDER BY id"):
        sums[order_id] = sums.get(order_id, 0.0) + qty * sale_unit
        count[order_id] = count.get(order_id, 0) + 1
        first.setdefault(order_id, "%s × %d" % (name, qty))
    wanted = board_query(query)
    columns = {s["id"]: {"stage": s, "count": 0, "sum": 0.0, "cards": []} for s in stages}
    summary = {"open": 0, "overdue": 0, "new_today": 0, "sum_open": 0.0}
    rows = conn.execute("SELECT id, stage_id, client_name, client_phone, point_id, delivery_date, tags, created_at, entered_at, webmim_number "
                        "FROM order_card ORDER BY id DESC").fetchall()
    from . import oth_sale
    stocked = oth_sale.in_stock_ids(conn)                                       # no date of delivery: «Товар в наличии на точке» (open stages only)
    for cid, sid, name, phone, pid, delivery, tags, created, entered, webmim in rows:
        if sid not in columns:
            continue
        tag_names = tag_list(tags)
        if point_id and pid != point_id:
            continue
        if tag and tag not in tag_names:
            continue
        if not board_matches(wanted, cid, name, phone, first.get(cid, ""), webmim or ""):
            continue
        stage = columns[sid]["stage"]
        seconds = _seconds_on_stage(entered, now)
        overdue = bool(stage["limit_hours"]) and stage["kind"] == "open" and seconds > stage["limit_hours"] * 3600
        total = round(sums.get(cid, 0.0), 2)
        items_text = first.get(cid, "") + (" +%d" % (count[cid] - 1) if count.get(cid, 0) > 1 else "")
        columns[sid]["cards"].append({"id": cid, "client": name or phone or "Без имени", "items_text": items_text, "point": names.get(pid, ""),
                                      "delivery_date": delivery, "in_stock": cid in stocked and stage["kind"] == "open", "sum": total if with_sums else None, "days": int(seconds // 86400), "overdue": overdue,
                                      "tags": tag_names})
        columns[sid]["count"] += 1
        columns[sid]["sum"] = round(columns[sid]["sum"] + total, 2)
        if stage["kind"] == "open":
            summary["open"] += 1
            summary["sum_open"] = round(summary["sum_open"] + total, 2)
            summary["overdue"] += 1 if overdue else 0
        if created[:10] == now.date().isoformat():
            summary["new_today"] += 1
    if not with_sums:
        summary["sum_open"] = None
        for column in columns.values():
            column["sum"] = None
    return {"columns": [columns[s["id"]] for s in stages], "summary": summary}


def record_sales(conn, card_id, day: str, now: str, author: str, today: str) -> List[int]:
    """One sale per position in «Продажи» (money as fixed in the order). Once only."""
    card = get_card(conn, card_id)
    if card["stage"]["kind"] != "won":
        raise OrderError("Продажу можно записать, когда заказ на успешном этапе («Выполнено»).")
    if card["sale_ids"]:
        raise OrderError("Продажа по этому заказу уже записана.")
    sold = [it for it in card["items"] if int(it["qty"]) >= 1]       # a position emptied by «Взять с ОТХ» (oth_sale) has nothing to sell
    if not sold:
        raise OrderError("В заказе нет позиций: записывать нечего.")
    unknown = [it["name"] for it in sold if not float(it["supplier_price"]) > 0]      # a point tyre before its purchase: never a made-up profit
    if unknown:
        raise OrderError("Не указана закупка: «%s». Впишите «Закупка за 1 шт» в позиции и запишите продажу снова." % "», «".join(unknown))
    point = card["point"]["name"] if card["point"] else ""
    claim = conn.execute("UPDATE order_card SET sale_ids = '[-1]', updated_at = ? WHERE id = ? AND sale_ids = '[]' AND stage_id = ?",
                         (now, card_id, card["stage_id"]))
    if claim.rowcount != 1:                                  # a second request got there first (two workers, a double press)
        raise OrderError("Продажа по этому заказу уже записывается или записана.")
    conn.commit()
    ids: List[int] = []
    try:
        for it in sold:
            ids.append(sales_store.add_sale(
                conn, day=day, goods_id=it["goods_id"], brand_key=it["brand_key"] or "-", season=it["season"], diameter=it["diameter"],
                name=it["name"], ad_kind="single", pieces=it["qty"], sale_total=float(it["sale_unit"]) * it["qty"],
                supplier_price=float(it["supplier_price"]), terms=it["terms"] or None, fee_pct=float(it["fee_pct"]), promo_pct=float(it["promo_pct"]),
                account_id=None, point=point, note="Заказ №%d" % card_id, now=now, author=author, today=today, source="order",
                point_id=card["point"]["id"] if card["point"] else None))
    except Exception as err:
        for sale_id in ids:
            sales_store.reverse_sale(conn, sale_id, now, author, from_order=True)
        conn.execute("UPDATE order_card SET sale_ids = '[]' WHERE id = ?", (card_id,))      # give the claim back
        conn.commit()
        raise OrderError(str(err)) if isinstance(err, ValueError) else err
    conn.execute("UPDATE order_card SET sale_ids = ?, updated_at = ? WHERE id = ?", (json.dumps(ids), now, card_id))
    from . import oth_sale
    oth_sale.note_sales(conn, card, sold, ids)                          # the sales of the tyres taken from «На ОТХ»: at the old order's purchase
    add_event(conn, card_id, now, author, "sale", "Записана продажа: %d поз." % len(ids))
    conn.commit()
    return ids


def reverse_sales(conn, card_id, now: str, author: str) -> int:
    card = get_card(conn, card_id)
    count = 0
    ids = [i for i in card["sale_ids"] if i > 0]
    if -1 in card["sale_ids"]:                         # a record that never finished: the sales written before the crash are found by their note
        for (sale_id,) in conn.execute("SELECT id FROM sale WHERE note = ? AND source = 'order' AND reversed_at IS NULL", ("Заказ №%d" % card_id,)).fetchall():
            if sale_id not in ids:
                ids.append(sale_id)
    for sale_id in ids:
        try:
            sales_store.reverse_sale(conn, sale_id, now, author, from_order=True)
            count += 1
        except ValueError:
            pass                                  # already reversed by hand
    if card["sale_ids"]:
        conn.execute("UPDATE order_card SET sale_ids = '[]', updated_at = ? WHERE id = ?", (now, card_id))
        add_event(conn, card_id, now, author, "sale", "Продажа сторнирована: %d поз." % count)
        conn.commit()
    return count


def open_order_conflict(conn, phone, point_id=None, besides: Optional[int] = None) -> Optional[dict]:
    """The open order a new order of this phone for `point_id` (None: no point) may not stand beside (the owner 2026-10-27): one of the same
    point or of no point; for an order of no point, any. Our own numbers and dummies (clients.usable) are no client: nothing. Reading only."""
    from . import clients
    if not clients.usable(clients.normalize(phone), clients.own_phones(conn)):
        return None
    return open_card_by_phone(conn, phone, besides, point_id)


def open_card_by_phone(conn, phone, besides: Optional[int] = None, point_id=None) -> Optional[dict]:
    """The newest open order (its stage is of kind «open») of this client phone, or None: {"id", "created_at", "point_id"}. The phone is
    compared the way the feed writes it (connections.normalize_phone), however the card has it typed; a phone that is no phone matches nothing.
    `besides`: an order not to count (the one being made); `point_id`: only orders of that point or of no point. Reading only."""
    from . import connections
    wanted = connections.normalize_phone(phone)
    if not wanted:
        return None
    for card_id, created_at, card_point, card_phone in conn.execute(
            "SELECT c.id, c.created_at, c.point_id, c.client_phone FROM order_card c JOIN order_stage s ON s.id = c.stage_id "
            "WHERE s.kind = 'open' AND c.client_phone != '' AND c.id != ? ORDER BY c.id DESC", (besides or 0,)):
        if connections.normalize_phone(card_phone) == wanted and (not point_id or not card_point or int(card_point) == int(point_id)):
            return {"id": card_id, "created_at": created_at, "point_id": card_point}
    return None
