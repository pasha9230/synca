"""The view «Список» of «Заказы» (the owner 2026-10-29): a table like the owner's Google table of orders, a row per position, its values
counted by the same code as the table's (order_sheet.card_rows), with the totals of the whole filter. A person chooses and orders the
columns (user_prefs, PREF_KEY); a person without the right «money» (a manager of a point among them) never gets the columns of MONEY: they
are not in the rows given to the page, whatever is saved; a manager of a point gets none of SALE either."""
from __future__ import annotations

from typing import Dict, List, Optional

from . import order_sheet, orders, user_prefs

COLUMNS = ("Дата счета", "№ заказа MIM", "Перемещение MIM", "Артикул", "Наименование", "Кол-во", "Цена MIM", "Сумма MIM", "Цена продажи",
           "Сумма продажи", "Имя", "Телефон", "ПВЗ", "Этап", "Реализация", "Списание", "Прибыль %", "Прибыль ₽", "Примечание")
MONEY = frozenset({"Цена MIM", "Сумма MIM", "Прибыль %", "Прибыль ₽"})          # the purchase and the profit: the right «money» only
SALE = frozenset({"Цена продажи", "Сумма продажи"})                            # a point: no sums in the list (the owner 2026-10-26), the card has them
NUMBERS = frozenset({"Кол-во", "Цена MIM", "Сумма MIM", "Цена продажи", "Сумма продажи", "Прибыль %", "Прибыль ₽"})
POSITION = ("Артикул", "Наименование", "Кол-во", "Цена MIM", "Сумма MIM", "Цена продажи", "Сумма продажи", "Прибыль %", "Прибыль ₽")
PREF_KEY = "orders.list.columns"
PAGE = 50                                                                      # orders on a page of the list


def allowed(money: bool, point: bool = False) -> tuple:
    banned = (frozenset() if money else MONEY) | (SALE if point else frozenset())
    return tuple(c for c in COLUMNS if c not in banned)


def choice(conn, login: str, money: bool, point: bool = False) -> List[dict]:
    """The columns the person may have, in the person's order: [{"name", "shown"}]. A column the saved order does not name (a new one, or
    the setting was saved without it) comes at its default place among the others; what is not allowed is never there."""
    ok = allowed(money, point)
    saved = user_prefs.get(conn, login, PREF_KEY, {})
    saved = saved if isinstance(saved, dict) else {}
    order = [c for c in saved.get("order") or [] if isinstance(c, str) and c in ok]
    order = list(dict.fromkeys(order))
    for c in ok:
        if c not in order:
            before = ok[:ok.index(c)]
            at = max((order.index(b) + 1 for b in before if b in order), default=0)
            order.insert(at, c)
    hidden = {c for c in saved.get("hidden") or [] if isinstance(c, str)}
    return [{"name": c, "shown": c not in hidden} for c in order]


def save(conn, login: str, money: bool, order: List[str], shown: List[str], now: str, point: bool = False) -> bool:
    """Save the person's columns: only what is allowed, at least one shown. False: nothing to show, nothing saved."""
    ok = allowed(money, point)
    order = list(dict.fromkeys(c for c in order if c in ok))
    order += [c for c in ok if c not in order]
    if not any(c in shown for c in order):
        return False
    user_prefs.put(conn, login, PREF_KEY, {"order": order, "hidden": [c for c in order if c not in shown]}, now)
    return True


def reset(conn, login: str) -> None:
    user_prefs.drop(conn, login, PREF_KEY)


def rows(conn, card_ids: List[int], money: bool, point: bool = False) -> List[dict]:
    """The rows of the orders, newest first, a position each (an order without positions: one row without them): {"order": id,
    "values": {column: value}} with the allowed columns only. `point`: no SALE, the note without an Avito account's name."""
    ok = allowed(money, point)
    out = []
    for card_id in card_ids:
        card = orders.get_card(conn, card_id)
        found = list(order_sheet.card_rows(conn, card, "").values()) or [dict(order_sheet.common_of(conn, card, ""), **{c: "" for c in POSITION})]
        for values in found:
            if point:
                values["Примечание"] = orders.without_account_names(conn, values["Примечание"])
            out.append({"order": card_id, "values": {c: values[c] for c in ok}})
    return out


def totals(rows_: List[dict], money: bool) -> Dict[str, Optional[float]]:
    """Sums of the rows (every page of the filter): «Сумма MIM», «Сумма продажи», «Прибыль ₽», and «Прибыль %» = the profit over the sale
    of the positions that have a profit (a sale written; None when none has). Without `money`: nothing (a point sees no totals)."""
    if not money:
        return {}
    def total(col):
        return round(sum(r["values"][col] for r in rows_ if _number(r["values"][col])), 2)
    out = {c: total(c) for c in ("Сумма MIM", "Сумма продажи", "Прибыль ₽")}
    counted = [r["values"] for r in rows_ if _number(r["values"]["Прибыль ₽"])]
    sale = sum(v["Сумма продажи"] for v in counted)
    out["Прибыль %"] = round(sum(v["Прибыль ₽"] for v in counted) * 100.0 / sale, 1) if sale else None
    return out


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
