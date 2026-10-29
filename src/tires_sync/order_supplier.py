"""«Заказ поставщику» of an order's page (the owner 2026-10-29): «Сверка» — every line a person checks by hand before ordering, ✓ or what is
wrong — then «Заказать в МИМ» (webmim_create, the window «Подтверждение» shows webmim_create.fields: what goes) or «Заказать в 4 Точки».
Nothing here asks a supplier: the lines are of the panel's own data and of the suppliers' last unload (panel/data.load_merged).

The lines that keep «Заказать в МИМ» shut are exactly the refusals of webmim_create.checks (one function, the button and the lines never
disagree); the stock and the price of the unload only warn (the unload is hours old, WebMIM itself answers for its stock; prices are never
changed here). 4 Точки has no API order (the owner 2026-10-29): the window gives the text for its cabinet, and «Оформил вручную» keeps the
number of the order of 4 Точки (order_card.supplier_number) and moves the order to «Заказ оформлен» the ordinary way, as a person's move
(the rules of the stage as they are; no message of webmim's on_placed)."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import List, Optional, Tuple

from . import orders, suppliers, tochki_feed, webmim_create
from .order_points import get_point

OK, WARN, BAD = "ok", "warn", "bad"
LATE_TEXT = "Заказ на сегодня нужно передать в работу до 16:00 (предупреждение WebMIM)."
# the lines of «Сверка», in their order; the keys of webmim_create.checks fall into them
LINES = (("items", "Позиции у поставщика"), ("price", "Цена закупки"), ("territory", "Один поставщик и территория"),
         ("point", "Точка и адрес доставки"), ("phone", "Контактный телефон (телефон точки)"), ("time", "Время работы клиента"),
         ("contact", "Контакт и примечание"), ("date", "Дата доставки"), ("stage", "Перевод на «Заказ оформлен»"))
KEY_LINE = {"items": "items", "territory": "territory", "point": "point", "address": "point", "phone": "phone", "time": "time", "date": "date"}
MAX_NUMBER = 40


def number_of(conn, card_id) -> str:
    """The number of the order of 4 Точки written by «Оформил вручную» ('' without it)."""
    row = conn.execute("SELECT supplier_number FROM order_card WHERE id = ?", (card_id,)).fetchone()
    return str(row[0] or "") if row else ""


def supplier_of(conn, card: dict) -> str:
    """«webmim» (every position of WebMIM), «tochki» (every one of 4 Точки), «other» (of one other supplier), «mixed» (of several),
    «» (no position)."""
    items = [i for i in card.get("items") or [] if int(i["qty"] or 0) > 0]
    if not items:
        return ""
    tochki = tochki_feed.config(conn)["supplier_id"]
    kinds = set()
    for item in items:
        sid = suppliers.supplier_of_id(str(item["goods_id"]))
        kinds.add("tochki" if tochki and sid == tochki else "webmim" if sid == 1 and not str(item.get("brand_key") or "").startswith("S") else "other")
    return kinds.pop() if len(kinds) == 1 else "mixed"


def _stock_lines(card: dict, parsed) -> Tuple[List[str], List[str]]:
    """(stock warnings, price warnings) of the positions against the last unload; a price not compared (no position in the unload) warns too."""
    offers = getattr(parsed, "offers", {}) or {}
    stocks = getattr(parsed, "stocks", {}) or {}
    stock, price = [], []
    for item in card.get("items") or []:
        qty = int(item["qty"] or 0)
        if qty <= 0:
            continue
        offer = offers.get(str(item["goods_id"]))
        left = offer.stock if offer is not None else stocks.get(str(item["goods_id"]))
        if left is None:
            stock.append("«%s»: нет в последней выгрузке поставщика" % item["name"])
        elif left < qty:
            stock.append("«%s»: в выгрузке осталось %d шт, нужно %d" % (item["name"], left, qty))
        if offer is None or not offer.base_price:
            price.append("«%s»: не сверить, нет цены в выгрузке" % item["name"])
        elif round(float(offer.base_price), 2) != round(float(item["supplier_price"] or 0), 2):
            price.append("«%s»: в выгрузке %s ₽, в заказе %s ₽" % (item["name"], _rub(offer.base_price), _rub(item["supplier_price"])))
    return stock, price


def _day(value) -> str:
    try:
        return date.fromisoformat(str(value or "")).strftime("%d.%m.%Y")
    except ValueError:
        return str(value or "")


def _rub(value) -> str:
    return ("%.2f" % float(value or 0)).rstrip("0").rstrip(".")


def _tochki_checks(conn, card: dict, today: date) -> List[Tuple[str, str]]:
    """What keeps the text for 4 Точки from being whole: the point, its phone, the time, the date."""
    out = []
    point = get_point(conn, card["point_id"]) if card.get("point_id") else None
    if point is None:
        out.append(("point", "Не выбрана точка выдачи."))
    else:
        if not point["address"]:
            out.append(("point", "У точки «%s» не указан адрес (Подключения → Точки)." % point["name"]))
        if not webmim_create.webmim_phone(point["manager_phone"]):
            out.append(("phone", "У точки «%s» телефон не российский мобильный или не указан." % point["name"]))
        if not (card.get("delivery_time") or point["default_time"]):
            out.append(("time", "Не указано время доставки (в заказе или время точки по умолчанию)."))
    if not card.get("delivery_date"):
        out.append(("date", "Не указана дата доставки."))
    elif card["delivery_date"] < today.isoformat():
        out.append(("date", "Дата доставки в прошлом."))
    return out


def view(conn, card: dict, parsed, now: str) -> Optional[dict]:
    """The block «Заказ поставщику»: None when there is nothing to order (a deal of the point's stock, a number of the supplier there already,
    a closed order). {"supplier", "lines": [{"key", "label", "state", "texts"}], "blocked", "late", "fields", "fingerprint", "tochki_text"}."""
    if card.get("deal_kind") == "stock" or card.get("webmim_id") or card.get("webmim_number") or number_of(conn, card["id"]):
        return None
    if card["stage"]["kind"] != "open":
        return None
    today = datetime.fromisoformat(now).date()
    kind = supplier_of(conn, card)
    state = webmim_create.get_state(conn, card["id"])
    lost = bool(state and not state["webmim_id"] and state["state"] in ("unknown", "running"))       # as _claim: it looks for the order first
    found = webmim_create.checks(conn, card, today, "lost" if lost else "new") if kind != "tochki" else _tochki_checks(conn, card, today)
    if kind == "mixed":
        found.append(("territory", "В заказе позиции разных поставщиков: оформите их отдельными заказами."))
    bad = {}
    for key, text in found:
        bad.setdefault(KEY_LINE.get(key, "items"), []).append(text)
    stock, price = _stock_lines(card, parsed)
    late = webmim_create.late(card, now) and "date" not in bad
    gaps = orders.gaps_to_place(conn, card)
    warn = {"items": stock, "price": price, "date": [LATE_TEXT] if late else [],
            "stage": ["После оформления этап не сменится сам: нет %s." % ", ".join(gaps)] if gaps else []}
    point = get_point(conn, card["point_id"]) if card.get("point_id") and "point" not in bad else None
    good = {"items": "позиций %d, все есть в выгрузке" % sum(1 for i in card["items"] if int(i["qty"] or 0) > 0),
            "price": "совпадает с выгрузкой", "territory": "МИМ" if kind == "webmim" else "4 Точки" if kind == "tochki" else "",
            "point": ("«%s», адрес WebMIM %s" % (point["name"], point["webmim_address_id"]) if kind == "webmim" else
                      "«%s», %s" % (point["name"], point["address"])) if point else "",
            "phone": webmim_create.webmim_phone(point["manager_phone"]) if point else "",
            "time": (card.get("delivery_time") or point["default_time"]) if point else "",
            "contact": "%s · «%s»" % (webmim_create.CONTACT, webmim_create.NOTE_FOR_SUPPLIER),
            "date": _day(card.get("delivery_date")), "stage": "данных хватает"}
    lines = []
    for key, label in LINES:
        if key == "territory" and kind not in ("webmim", "mixed") and key not in bad:
            continue
        state = BAD if bad.get(key) else WARN if warn.get(key) else OK
        lines.append({"key": key, "label": label, "state": state, "texts": bad.get(key) or warn.get(key) or [good[key]]})
    blocked = bool(found) or kind not in ("webmim", "tochki")
    out = {"supplier": kind, "lines": lines, "blocked": blocked, "late": late, "fields": None, "fingerprint": "", "tochki_text": "",
           "tomorrow": (today + timedelta(days=1)).isoformat(), "late_text": LATE_TEXT}
    if not blocked:
        out["fields"] = webmim_create.fields(conn, card)
        out["fingerprint"] = webmim_create.fingerprint(conn, card)
        if kind == "tochki":
            out["tochki_text"] = tochki_text(conn, card)
    return out


def _code(item: dict) -> str:
    gid = str(item["goods_id"])
    return str(item.get("code") or "").strip() or (gid.split("-", 1)[1] if "-" in gid else gid)


def tochki_text(conn, card: dict) -> str:
    """The text to copy into the cabinet of 4 Точки (nothing is sent: no API order, the owner 2026-10-29)."""
    point = get_point(conn, card["point_id"])
    day = date.fromisoformat(card["delivery_date"]).strftime("%d.%m.%Y")
    lines = ["Заказ панели №%d" % card["id"], "Позиции:"]
    for n, item in enumerate((i for i in card["items"] if int(i["qty"] or 0) > 0), 1):
        lines.append("%d. %s — код %s — %d шт" % (n, item["name"], _code(item), int(item["qty"])))
    lines += ["Способ: доставка", "Адрес: %s (точка «%s»)" % (point["address"], point["name"]), "Дата доставки: %s" % day,
              "Время работы клиента: %s" % (card.get("delivery_time") or point["default_time"]),
              "Контактное лицо: %s" % webmim_create.CONTACT, "Контактный телефон: %s" % webmim_create.webmim_phone(point["manager_phone"]),
              "Примечание для поставщика: %s" % webmim_create.NOTE_FOR_SUPPLIER]
    return "\n".join(lines)


def mark_tochki(conn, card_id, number: str, now: str, author: str) -> Tuple[str, str]:
    """«Оформил вручную»: the number of the order of 4 Точки goes to the card (a journal record), then the order on an open stage before
    «Заказ оформлен» goes there by the ordinary move. Returns (the move, why the stage did not change). OrderError: nothing written."""
    text = " ".join(str(number or "").split())
    if not text or len(text) > MAX_NUMBER:
        raise orders.OrderError("Номер заказа 4 Точек: от 1 до %d символов." % MAX_NUMBER)
    card = orders.get_card(conn, card_id)
    if supplier_of(conn, card) != "tochki":
        raise orders.OrderError("В заказе не только позиции 4 Точек: номер 4 Точек не записан.")
    if card["stage"]["kind"] != "open":
        raise orders.OrderError("Заказ закрыт («%s»): номер 4 Точек не записан." % card["stage"]["name"])
    before = number_of(conn, card_id)
    if before == text:
        return "", ""
    conn.execute("UPDATE order_card SET supplier_number = ?, updated_at = ? WHERE id = ?", (text, now, card_id))
    orders.add_event(conn, card_id, now, author, "field", "Номер заказа 4 Точек: %s (оформлен вручную в кабинете 4 Точек)%s"
                     % (text, ", был %s" % before if before else ""))
    conn.commit()
    return webmim_create._to_placed(conn, card_id, now, author)


def tomorrow(conn, card_id, now: str, author: str) -> str:
    """«Перенести на завтра»: the delivery date of the order becomes tomorrow (the ordinary change of the field, its journal record)."""
    day = (datetime.fromisoformat(now).date() + timedelta(days=1)).isoformat()
    orders.update_card(conn, card_id, {"delivery_date": day}, now, author)
    return day
