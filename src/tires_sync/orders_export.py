"""The orders as an Excel file: the same orders the page lists under the same filters, one row each, newest first. Read only: nothing is written
anywhere. Every text goes into the file as plain text (never as a formula) and without the signs Excel does not accept."""
from __future__ import annotations

import io
import re
from datetime import datetime
from typing import List, Optional

from . import orders

HEAD = ["№ заказа", "Создан", "Этап", "Клиент", "Телефон", "Точка", "Ответственный", "Источник", "Теги", "Позиции", "Штук", "Сумма, ₽",
        "Дата доставки", "Номер заказа WebMIM", "Статус WebMIM", "Комментарий", "Чат Авито"]
WIDTHS = [10, 17, 22, 28, 18, 16, 16, 18, 24, 60, 8, 14, 14, 18, 18, 40, 16]
FORBIDDEN = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")        # signs a sheet cannot hold (XML 1.0 forbids these too)


def _text(value) -> str:
    return FORBIDDEN.sub("", str(value or ""))


def _created(value: str) -> str:
    try:
        return datetime.fromisoformat(str(value)).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return _text(value)


def _row(card: dict) -> list:
    items = card["items"]
    money = orders.totals(items)
    return [card["id"], _created(card["created_at"]), card["stage"]["name"], card["client_name"], card["client_phone"],
            card["point"]["name"] if card["point"] else "", card["responsible"], orders.SOURCES.get(card["source"], card["source"]),
            ", ".join(card["tags"]), "; ".join("%s × %d" % (i["name"], i["qty"]) for i in items), money["qty"], money["sale"],
            card["delivery_date"], card["webmim_number"], card["webmim_status"], card["comment"], card["avito_chat"]]


def build(conn, now: datetime, point_id: Optional[int] = None, tag: str = "", query: str = "") -> bytes:
    """The file (bytes) with the orders the page would list under these filters."""
    import openpyxl
    from openpyxl.styles import Font
    data = orders.board(conn, now, point_id=point_id, tag=tag, query=query)
    ids = sorted((c["id"] for col in data["columns"] for c in col["cards"]), reverse=True)
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Заказы"
    sheet.append(HEAD)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for card_id in ids:
        values = _row(orders.get_card(conn, card_id))
        sheet.append([None] * len(values))
        for column, value in enumerate(values, start=1):
            cell = sheet.cell(row=sheet.max_row, column=column)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                cell.value = value
            else:
                cell.value = _text(value)
                cell.data_type = "s"                                    # a text, never a formula, whatever it starts with
    for index, width in enumerate(WIDTHS, start=1):
        sheet.column_dimensions[openpyxl.utils.get_column_letter(index)].width = width
    sheet.freeze_panes = "A2"
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()
