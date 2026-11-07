"""«Письмо поставщику» (the owner 2026-11-07): the letter to the supplier to agree a return, made by the system and sent by the operator from
their own mail. The panel has no mail of its own and no keys for one: the button is a «mailto:» link (the address, the subject and the text),
with «Скопировать текст» beside it. The text: the WebMIM number, the day of the invoice, the positions and their pieces, the request to agree
the return and to name the day of the pick-up; the template is in «Шаблоны сообщений» (message_texts SUPPLIER_RETURN). Never the purchase, the
sale or the profit (the purchase price is left out too: the owner «сомневаешься — не включай»). The address: «Почта для возвратов» in the
supplier's settings (connection «supplier_mail» of the supplier's id; no table changes). The press is a record of the order's journal; the
stage and the task stay as they are (the operator closes them). A manager of a point never sees it. Nothing here sends anything."""
from __future__ import annotations

import re
import urllib.parse
from typing import Optional

from . import connections, message_texts, orders, store
from .suppliers import supplier_of_id

KIND = "supplier_mail"                       # connection kind, owner_id = the supplier's id: {"email"}
EVENT = "supplier_mail"                      # order_event.kind of the press (grey; hidden from a point in its card)
TEXT_KEY = message_texts.SUPPLIER_RETURN
SUBJECT = "Возврат по заказу № %s"
STAGE = orders.RETURN_GIVEN
EMAIL = re.compile(r"^[^@\s<>,;:\"()\[\]\\]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")
MAX_EMAIL = 254


def clean_email(value) -> str:
    """The address as it may be kept ('' for none); ValueError when it is not one address."""
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) > MAX_EMAIL or not EMAIL.match(text):
        raise ValueError("Почта для возвратов: один адрес вида name@example.com.")
    return text


def email_of(conn, supplier_id: int) -> str:
    return str(connections.get_config(conn, KIND, int(supplier_id)).get("email") or "")


def set_email(conn, supplier_id: int, value, now: str, author: str) -> None:
    connections.set_config(conn, KIND, int(supplier_id), {"email": clean_email(value)}, now, author)     # its audit commits


def _positions(card: dict) -> list:
    return [i for i in card["items"] if int(i["qty"] or 0) >= 1]


def _shown_day(day: str) -> str:
    return "%s.%s.%s" % (day[8:10], day[5:7], day[0:4]) if re.match(r"^\d{4}-\d{2}-\d{2}$", day or "") else (day or "")


def draft(conn, card: dict) -> dict:
    """{"to", "subject", "body", "href", "warning", "supplier"} of the order's letter. The address only when every position is of one supplier
    and that supplier has one; else the letter opens without it and `warning` says why."""
    from . import order_sheet
    items = _positions(card)
    mim = card.get("webmim_number") or card.get("webmim_id") or "—"
    day = _shown_day(order_sheet._invoice_day(conn, card)) or "—"
    lines = "\n".join("- %s — %d шт." % (" ".join(str(i["name"] or "").split()), int(i["qty"])) for i in items) or "- (позиций нет)"
    body = message_texts.text(conn, TEXT_KEY, mim=mim, invoice_date=day, items=lines)
    subject = SUBJECT % mim
    ids = sorted({supplier_of_id(str(i["goods_id"])) for i in items})
    supplier = connections.get_supplier(conn, ids[0]) if len(ids) == 1 else None
    to, warning = "", ""
    if len(ids) > 1:
        warning = "В заказе позиции разных поставщиков: адрес не подставлен, укажите его в письме сами."
    elif supplier is None:
        warning = "Поставщик заказа не найден: адрес не подставлен, укажите его в письме сами."
    else:
        to = email_of(conn, supplier["id"])
        if not to:
            warning = "У поставщика «%s» не указана почта для возвратов (Подключения → WebMIM): письмо откроется без адреса." % supplier["name"]
    query = urllib.parse.urlencode({"subject": subject, "body": body.replace("\n", "\r\n")}, quote_via=urllib.parse.quote)
    href = "mailto:%s?%s" % (urllib.parse.quote(to, safe="@"), query)     # «@» as it is: older mail clients
    return {"to": to, "subject": subject, "body": body, "href": href, "warning": warning, "supplier": supplier["name"] if supplier else ""}


def note_press(conn, card_id: int, now: str, who: str) -> Optional[str]:
    """The press of «Письмо поставщику»: one grey record of the order's journal (who, to which address) and the audit. Nothing else changes.
    Returns None, or why it is refused (the order is not on «На возврат»)."""
    card = orders.get_card(conn, int(card_id))
    if card["stage"]["code"] != STAGE:
        return "Заказ не на этапе «На возврат»."
    mail = draft(conn, card)
    orders.add_event(conn, card["id"], now, who, EVENT, "Письмо поставщику о возврате открыто в почте оператора (%s)" % (
        "адрес %s" % mail["to"] if mail["to"] else "без адреса"), level="grey")
    store.write_audit(conn, now, who, "supplier_mail", {"order": card["id"], "to": mail["to"]})        # commits
    return None
