"""Message templates of the Orders section: text with {variables} for the client or the point's manager."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import List, Tuple

from .orders import POINT_GOODS, OrderError

AUDIENCES = {"client": "клиенту", "manager": "менеджеру"}
VARIABLES = {
    "client_name": "Клиент", "client_phone": "Телефон", "order_no": "Номер заказа", "items": "Позиции", "total": "Сумма", "point": "Точка",
    "point_address": "Адрес точки", "manager": "Менеджер", "delivery_date": "Дата доставки", "delivery_time": "Время доставки",
    "items_full": "Позиции подробно", "webmim_number": "Номер заказа WebMIM", "transfer_number": "Номер перемещения", "hold_until": "Резерв до", "order_date": "Дата оформления",
    "point_hours": "График точки", "point_map": "Точка на Яндекс Картах"}
HOLD_DAYS = 3                                    # the point keeps the reservation three days from the arrival of the goods
SAMPLES = (
    ("Клиенту: заказ оформлен", "client",
     "Заказ {webmim_number} оформлен на перемещение.\nТерритория {point_address}\n\n{items_full}\n\nХранение резерва до {hold_until}\n\n"
     "Покупатель {client_name} {client_phone}\n\n! Обратите внимание. К оплате принимаем наличные.\n\n"
     "По факту перемещения шин в пункт выдачи будет сообщение (тут в чате) и локация на Яндекс Картах."),
    ("Менеджеру: заказ оформлен", "manager",
     "Заказ {webmim_number} оформлен на перемещение.\nТерритория {point_address}\n\n{items_full}\n\nПокупатель {client_name} {client_phone}"),
)
ARRIVED_SAMPLE = ("Клиенту: товар на точке, можно забирать", "client",              # goes by itself after the point's «приехал» (order_manager); the owner's text 2026-10-21
                  "Заказ {webmim_number} приехал в пункт выдачи, можно забирать.\nТерритория {point_address}\nГрафик работы {point_hours}\n"
                  "Локация на Яндекс Картах {point_map}\n\n{items}\n\nХранение резерва до {hold_until}\n\nПокупатель {client_name} {client_phone}\n\n"
                  "! Обратите внимание. К оплате принимаем наличные.")
RESERVE_SAMPLE = ("Клиенту: срок резерва истекает", "client",              # goes by itself on the last day of the reservation (order_reserve); the owner's text 2026-10-23
                  "Добрый день. Срок резерва истекает. Вы отказываетесь от выкупа шин или нужно продлить ожидание? "
                  "Готовы перевести предоплату в 10%, чтобы они вас ещё ждали?")
SAMPLES = SAMPLES + (ARRIVED_SAMPLE, RESERVE_SAMPLE)
FIELDS = "id, name, audience, body"
MAX_NAME, MAX_BODY = 80, 2000
PLACEHOLDER = re.compile(r"\{([^{}]*)\}")


def _row(row) -> dict:
    return dict(zip([f.strip() for f in FIELDS.split(",")], row))


def list_templates(conn) -> List[dict]:
    return [_row(r) for r in conn.execute("SELECT %s FROM order_template ORDER BY id" % FIELDS)]


def get_template(conn, template_id) -> dict:
    row = conn.execute("SELECT %s FROM order_template WHERE id = ?" % FIELDS, (template_id,)).fetchone()
    if row is None:
        raise OrderError("Такого шаблона нет.")
    return _row(row)


def _check(name, audience, body):
    name, body = str(name or "").strip(), str(body or "").strip()
    if not name or len(name) > MAX_NAME:
        raise OrderError("Название шаблона: от 1 до %d знаков." % MAX_NAME)
    if audience not in AUDIENCES:
        raise OrderError("Адресат шаблона выбран неверно.")
    if not body or len(body) > MAX_BODY:
        raise OrderError("Текст шаблона: от 1 до %d знаков." % MAX_BODY)
    unknown = sorted({m for m in PLACEHOLDER.findall(body) if m not in VARIABLES})
    if unknown:
        raise OrderError("Неизвестная подстановка: %s. Доступны: %s." % (", ".join("{%s}" % u for u in unknown), ", ".join("{%s}" % v for v in VARIABLES)))
    return name, audience, body


def add_template(conn, name, audience, body) -> int:
    name, audience, body = _check(name, audience, body)
    cur = conn.execute("INSERT INTO order_template (name, audience, body) VALUES (?,?,?)", (name, audience, body))
    conn.commit()
    return cur.lastrowid


def update_template(conn, template_id, name, audience, body) -> None:
    get_template(conn, template_id)
    name, audience, body = _check(name, audience, body)
    conn.execute("UPDATE order_template SET name = ?, audience = ?, body = ? WHERE id = ?", (name, audience, body, template_id))
    conn.commit()


def delete_template(conn, template_id) -> None:
    get_template(conn, template_id)
    if conn.execute("SELECT COUNT(*) FROM order_rule WHERE template_id = ?", (template_id,)).fetchone()[0]:
        raise OrderError("Шаблон используется правилами: сначала уберите их.")
    conn.execute("DELETE FROM order_template WHERE id = ?", (template_id,))
    conn.commit()


def money(value) -> str:
    """1254567 -> '1 254 567', 313641.75 -> '313 641.75' (no exponent, no trailing zeros)."""
    text = ("{:,.2f}".format(float(value))).replace(",", " ")
    return text[:-3] if text.endswith(".00") else text


def _date(value: str) -> str:
    parts = str(value or "").split("-")
    return "%s.%s.%s" % (parts[2], parts[1], parts[0]) if len(parts) == 3 else str(value or "")


def _order_date(conn, card: dict, now) -> str:
    """The day the order last entered «Заказ оформлен» (from the journal); today by panel time (Moscow, UTC+3) if it never did."""
    from .orders import list_events, list_stages
    placed = next((st["name"] for st in list_stages(conn) if st["code"] == "placed"), None)
    if placed:
        suffix = "→ «%s»" % placed
        for event in list_events(conn, card["id"]):              # newest first
            if event["kind"] == "stage" and event["text"].endswith(suffix):
                return _date(str(event["at"])[:10])
    if (card.get("stage") or {}).get("code") == "placed" and card.get("entered_at"):      # the stage was renamed after the entry: the stay in it began then
        return _date(str(card["entered_at"])[:10])
    today = datetime.fromisoformat(now) if now else datetime.now(timezone.utc) + timedelta(hours=3)
    return today.strftime("%d.%m.%Y")


def values(conn, card: dict, now=None) -> dict:
    """The value of every variable for a card; an empty string where the card has nothing. `now`: local ISO time of the panel."""
    point = card.get("point") or {}
    address = point.get("address") or ""                 # the address written for the point; else the one of its Avito address
    if not address and point.get("avito_address_id"):
        row = conn.execute("SELECT address_text FROM avito_address WHERE address_id = ? AND address_text IS NOT NULL LIMIT 1",
                           (point["avito_address_id"],)).fetchone()
        address = row[0] if row else ""
    items = card.get("items") or []
    full = "\n\n".join("Товар %s\n%sКол-во %d шт\nЦена ед. %s ₽\nСумма %s ₽" % (
        i["name"], "" if i["goods_id"] == POINT_GOODS else "Код %s\n" % (i.get("code") or i["goods_id"]), i["qty"], money(i["sale_unit"]),
        money(float(i["sale_unit"]) * i["qty"])) for i in items)                    # a tyre of the point has no code: no line «Код»
    try:                                  # the reserve runs from the day the goods arrive at the point; before they arrive: the planned delivery day + 3
        base = card.get("reserve_until") or ""
        if not base and card.get("delivery_date"):
            base = (date.fromisoformat(card["delivery_date"]) + timedelta(days=HOLD_DAYS)).isoformat()
        hold = date.fromisoformat(base).strftime("%d.%m.%Y") if base else ""
    except (KeyError, ValueError):
        hold = ""
    from .orders import totals
    total = totals(items)["sale"] if items else 0
    return {
        "client_name": card.get("client_name", ""), "client_phone": card.get("client_phone", ""), "order_no": str(card["id"]),
        "items": "; ".join("%s × %d" % (i["name"], i["qty"]) for i in items), "total": money(total) if items else "",
        "point": point.get("name", ""), "point_address": address, "manager": point.get("name", ""),
        "delivery_date": _date(card.get("delivery_date", "")), "delivery_time": card.get("delivery_time", ""),
        "items_full": full, "webmim_number": card.get("webmim_number", ""), "transfer_number": card.get("transfer_number", ""), "hold_until": hold, "order_date": _order_date(conn, card, now),
        "point_hours": point.get("hours") or "", "point_map": point.get("map_url") or ""}


def render(conn, card: dict, body: str, now=None) -> Tuple[str, List[str]]:
    """The text of a template for a card and the names of the values it lacks (those variables are left out of the text)."""
    data = values(conn, card, now)
    missing: List[str] = []

    def put(match):
        key = match.group(1)
        if not data.get(key):
            label = VARIABLES.get(key, key)
            if label not in missing:
                missing.append(label)
            return ""
        return data[key]
    return PLACEHOLDER.sub(put, body), missing


def add_samples(conn) -> int:
    """The owner's texts as templates (a template of the same name is left alone). Returns how many were added."""
    have = {t["name"] for t in list_templates(conn)}
    added = 0
    for name, audience, body in SAMPLES:
        if name not in have:
            add_template(conn, name, audience, body)
            added += 1
    return added
