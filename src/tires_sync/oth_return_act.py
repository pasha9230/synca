"""The act of a return to the supplier as a file (the owner 2026-11-12). The office attaches one PDF to a pair «point + date of the return»
(the supplier's driver takes the tyres of every order of the point agreed for that date by one act); the point prints it on the day
(«Скачать акт» on «Рабочее место» and in «Сегодня возврат!»). The file itself is kept in the base (oth_return_act.data), as the labels of
the transport company (order_intercity): a PDF only, up to 1.9 MB. A second file of the pair replaces the first. Every change is in the
journal of actions, by Moscow time. Nothing is sent anywhere from here."""
from __future__ import annotations

import os
from datetime import date
from typing import List, Optional, Tuple

from . import order_points as points, store
from .order_intercity import LABEL_MAX, NAME_MAX

ACT_MAX = LABEL_MAX                              # bytes: the panel takes 2 MB a request at most
FIELDS = "id, point_id, return_date, name, size, uploaded_at, uploaded_by"


class ActError(ValueError):
    """Something a person can fix: the text is shown to them."""


def _meta(row) -> dict:
    return dict(zip(("id", "point_id", "return_date", "name", "size", "uploaded_at", "uploaded_by"), row))


def find(conn, point_id, day: str) -> Optional[dict]:
    """The act of the point for the date of the return (without the file itself), or None."""
    row = conn.execute("SELECT %s FROM oth_return_act WHERE point_id = ? AND return_date = ?" % FIELDS, (point_id, str(day or ""))).fetchone() \
        if point_id is not None else None
    return _meta(row) if row else None


def listed(conn, limit: int = 100) -> List[dict]:
    """The acts attached, the latest date first, each with the name of its point."""
    names = {p["id"]: p["name"] for p in points.list_points(conn)}
    return [dict(_meta(r), point=names.get(r[1]) or "точка №%d" % r[1]) for r in conn.execute(
        "SELECT %s FROM oth_return_act ORDER BY return_date DESC, point_id LIMIT ?" % FIELDS, (int(limit),))]


def file(conn, act_id) -> Optional[dict]:
    """The act with its file («data»), or None."""
    row = conn.execute("SELECT %s, data FROM oth_return_act WHERE id = ?" % FIELDS, (act_id,)).fetchone()
    return dict(_meta(row[:-1]), data=bytes(row[-1])) if row else None


def check(filename, data: bytes) -> str:
    """The file of an act as it is to be kept: its name; ActError when there is none, it is not a PDF or it is too big."""
    name = os.path.basename(str(filename or "").replace("\\", "/")).strip()[:NAME_MAX]
    if not data:
        raise ActError("Выберите файл акта (PDF).")
    if not name.lower().endswith(".pdf") or bytes(data[:5]) != b"%PDF-":
        raise ActError("Акт возврата: только файл PDF.")
    if len(data) > ACT_MAX:
        raise ActError("Файл больше %.1f МБ: сохраните акт в PDF меньшего размера." % (ACT_MAX / 1_000_000))
    return name


def save(conn, point_id, day, filename, data: bytes, now: str, author: str) -> Tuple[int, bool]:
    """Attach the PDF to the point and the date; the file of the same pair is replaced (its id stays). Returns (id, replaced).
    ActError: no such point, not a date, no file, not a PDF, too big."""
    try:
        point = points.get_point(conn, int(point_id))
    except (points.PointError, TypeError, ValueError):
        raise ActError("Выберите точку.") from None
    try:
        day = date.fromisoformat(str(day or "").strip()).isoformat()
    except ValueError:
        raise ActError("Укажите дату возврата.") from None
    name = check(filename, data)
    old = find(conn, point["id"], day)
    detail = {"point": point["id"], "point_name": point["name"], "date": day, "name": name}
    if old:
        conn.execute("UPDATE oth_return_act SET name = ?, size = ?, data = ?, uploaded_at = ?, uploaded_by = ? WHERE id = ?",
                     (name, len(data), bytes(data), now, author, old["id"]))
        store.write_audit(conn, now, author, "oth_act_file_replaced", dict(detail, was=old["name"]))     # commits both
        return old["id"], True
    act_id = conn.execute("INSERT INTO oth_return_act (point_id, return_date, name, size, data, uploaded_at, uploaded_by) VALUES (?,?,?,?,?,?,?)",
                          (point["id"], day, name, len(data), bytes(data), now, author)).lastrowid
    store.write_audit(conn, now, author, "oth_act_file_added", detail)
    return act_id, False


def delete(conn, act_id, now: str, author: str) -> None:
    """«Удалить»; ActError when the file is gone already."""
    act = file(conn, act_id)
    if act is None:
        raise ActError("Такого акта нет.")
    conn.execute("DELETE FROM oth_return_act WHERE id = ?", (act["id"],))
    names = {p["id"]: p["name"] for p in points.list_points(conn)}
    store.write_audit(conn, now, author, "oth_act_file_deleted",
                      {"point": act["point_id"], "point_name": names.get(act["point_id"], ""), "date": act["return_date"], "name": act["name"]})
