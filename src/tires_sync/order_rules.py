"""Rules of the stages (what to do when a card enters a stage or hangs in it) and the manager's command words."""
from __future__ import annotations

import re
from typing import List, Optional

from .orders import OrderError, get_stage

TRIGGERS = {"enter": "заказ вошёл в этап", "stale": "заказ завис на этапе"}
ACTIONS = {"message": "подготовить сообщение по шаблону", "remind": "напоминание", "tag_add": "добавить тег", "tag_remove": "убрать тег",
           "move": "перенести на этап", "record_sale": "записать продажу"}
MODES = {"ask": "спросить", "auto": "сразу"}
FIELDS = "id, stage_id, trigger, hours, action, template_id, arg, mode, point_id, position, enabled"
MAX_HOURS, MAX_REMIND, MAX_TAG, MAX_WORD = 8760, 200, 30, 30
WORD = re.compile(r"^[^\W\d_]+(?:-[^\W\d_]+)*$")


def _row(row) -> dict:
    return dict(zip([f.strip() for f in FIELDS.split(",")], row))


def list_rules(conn, stage_id: Optional[int] = None) -> List[dict]:
    sql, args = "SELECT %s FROM order_rule" % FIELDS, ()
    if stage_id is not None:
        sql, args = sql + " WHERE stage_id = ?", (stage_id,)
    return [_row(r) for r in conn.execute(sql + " ORDER BY stage_id, position, id", args)]


def get_rule(conn, rule_id) -> dict:
    row = conn.execute("SELECT %s FROM order_rule WHERE id = ?" % FIELDS, (rule_id,)).fetchone()
    if row is None:
        raise OrderError("Такого правила нет.")
    return _row(row)


def _check(conn, stage_id, trigger, hours, action, template_id, arg, mode, point_id):
    get_stage(conn, stage_id)
    if trigger not in TRIGGERS:
        raise OrderError("Условие правила выбрано неверно.")
    if trigger == "stale":
        try:
            hours = int(hours)
        except (TypeError, ValueError):
            hours = 0
        if not 1 <= hours <= MAX_HOURS:
            raise OrderError("Сколько часов ждать: от 1 до %d." % MAX_HOURS)
    else:
        hours = 0
    if action not in ACTIONS:
        raise OrderError("Действие выбрано неверно.")
    if mode not in MODES:
        raise OrderError("Режим правила выбран неверно.")
    arg = str(arg or "").strip()
    if action == "message":
        if template_id is None or conn.execute("SELECT 1 FROM order_template WHERE id = ?", (template_id,)).fetchone() is None:
            raise OrderError("Выберите шаблон сообщения.")
        arg = ""
    else:
        template_id = None
    if action == "remind" and not 1 <= len(arg) <= MAX_REMIND:
        raise OrderError("Текст напоминания: от 1 до %d знаков." % MAX_REMIND)
    if action in ("tag_add", "tag_remove") and (not 1 <= len(arg) <= MAX_TAG or "," in arg):
        raise OrderError("Тег: от 1 до %d знаков, без запятых." % MAX_TAG)
    if action == "move":
        if not (arg.isascii() and arg.isdigit()):
            raise OrderError("Выберите этап, куда переносить.")
        target = get_stage(conn, int(arg))
        if target["id"] == stage_id:
            raise OrderError("Нельзя переносить заказ в тот же этап.")
        arg = str(target["id"])
    if action in ("record_sale",):
        arg = ""
    if point_id is not None and conn.execute("SELECT 1 FROM point WHERE id = ?", (point_id,)).fetchone() is None:
        raise OrderError("Такой точки нет.")
    return hours, template_id, arg


def add_rule(conn, stage_id, trigger, hours, action, template_id, arg, mode, point_id) -> int:
    hours, template_id, arg = _check(conn, stage_id, trigger, hours, action, template_id, arg, mode, point_id)
    position = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM order_rule WHERE stage_id = ?", (stage_id,)).fetchone()[0]
    cur = conn.execute("INSERT INTO order_rule (stage_id, trigger, hours, action, template_id, arg, mode, point_id, position) VALUES (?,?,?,?,?,?,?,?,?)",
                       (stage_id, trigger, hours, action, template_id, arg, mode, point_id, position))
    conn.commit()
    return cur.lastrowid


def update_rule(conn, rule_id, trigger, hours, action, template_id, arg, mode, point_id) -> None:
    rule = get_rule(conn, rule_id)
    hours, template_id, arg = _check(conn, rule["stage_id"], trigger, hours, action, template_id, arg, mode, point_id)
    conn.execute("UPDATE order_rule SET trigger = ?, hours = ?, action = ?, template_id = ?, arg = ?, mode = ?, point_id = ? WHERE id = ?",
                 (trigger, hours, action, template_id, arg, mode, point_id, rule_id))
    conn.commit()


def set_enabled(conn, rule_id, enabled: bool) -> None:
    get_rule(conn, rule_id)
    conn.execute("UPDATE order_rule SET enabled = ? WHERE id = ?", (1 if enabled else 0, rule_id))
    conn.commit()


def delete_rule(conn, rule_id) -> None:
    get_rule(conn, rule_id)
    conn.execute("DELETE FROM order_rule WHERE id = ?", (rule_id,))
    conn.commit()


def list_commands(conn, stage_id: Optional[int] = None) -> List[dict]:
    sql, args = "SELECT id, stage_id, word, to_stage_id FROM order_command", ()
    if stage_id is not None:
        sql, args = sql + " WHERE stage_id = ?", (stage_id,)
    return [dict(zip(("id", "stage_id", "word", "to_stage_id"), r)) for r in conn.execute(sql + " ORDER BY stage_id, id", args)]


def normalise_word(word) -> str:
    return " ".join(str(word or "").lower().split())


def find_command(conn, stage_id, word) -> Optional[dict]:
    word = normalise_word(word)
    return next((c for c in list_commands(conn, stage_id) if c["word"] == word), None)


def add_command(conn, stage_id, word, to_stage_id) -> int:
    get_stage(conn, stage_id)
    get_stage(conn, to_stage_id)
    word = normalise_word(word)
    if not 1 <= len(word) <= MAX_WORD or not WORD.match(word):
        raise OrderError("Кодовое слово: одно слово до %d знаков, только буквы." % MAX_WORD)
    if stage_id == to_stage_id:
        raise OrderError("Команда должна переносить на другой этап.")
    if find_command(conn, stage_id, word):
        raise OrderError("Такое слово на этом этапе уже есть.")
    cur = conn.execute("INSERT INTO order_command (stage_id, word, to_stage_id) VALUES (?,?,?)", (stage_id, word, to_stage_id))
    conn.commit()
    return cur.lastrowid


def delete_command(conn, command_id) -> None:
    conn.execute("DELETE FROM order_command WHERE id = ?", (command_id,))
    conn.commit()
